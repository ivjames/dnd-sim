"""Send one monster line and one table line to real Amazon Polly, and say what
came back.

A dev tool, like `tools/audio/` — it sits outside the layering, nothing on the
runtime path imports it, and it is not started by `run.sh` or pm2. Run it by
hand on the droplet, where the credentials are.

**Why it exists.** Every test in this repo drives a fake: `FakeTTS` in
`web/tests/conftest.py`, the recording stubs in `tests/tts/`, and the strict
one in `tests/tts/test_polly_contract.py` that refuses what Amazon's
documentation says Polly refuses. Together they prove the app emits documents
that MATCH what Polly documents. Only Polly can prove Polly reads them.

That gap matters most for the monster seats, and only for them. They are the
one place the app asks Polly for `pcm` rather than `mp3` — because a monster's
clip is post-processed between Polly and the listener (`tts/dsp.py`), and `pcm`
is the one format that can be worked on without a codec — and, with
`DND_TTS_MONSTER_FX=0`, the one place it writes `<amazon:effect
vocal-tract-length>` and routes to the standard engine to be allowed to. Polly
ERRORS on a tag or a sample rate its engine does not support rather than
ignoring it, so a wrong monster request is an `InvalidSsmlException` or an
`InvalidSampleRateException` per line — which `web/routes/tts.py` answers with a
502, which the page answers by speaking that one line in the browser's own
voice. The spectator hears a voice either way. Nothing on the page says which. A
table line succeeding proves nothing about a monster line, so this sends both.

It is also where the monster treatment can be **listened to**, which no test
can do: `--ab --out DIR` renders the monster line both ways — treated on the
table's engine, and the old standard-engine VTL document — so the two can be
played back to back. Whether the new one is better is a judgement, and this is
what it is judged on.

**What it does not touch.** Its cache is a temporary directory, removed on the
way out, so the app's `data/tts` is neither read nor written — a clip made here
is paid for here and not left for the app to serve. It goes nowhere near a
game, a ledger or the database: it does not import `web` or `orchestrator`, and
there is no game id for it to charge.

On the droplet, source `.env` first, the way `run.sh` does before it execs
python — the credentials and the `DND_TTS*` overrides are in that file and in no
shell's environment, so without it this checks default engines with no keys:

    cd /var/www/dndsim && (set -a; . ./.env; set +a; \
        .venv/bin/python -m tools.polly_check)         # two clips, ~$0.0005

    .venv/bin/python -m tools.polly_check --dry-run    # print the requests, send nothing
    .venv/bin/python -m tools.polly_check --out /tmp/clips     # keep the clips to listen to
    .venv/bin/python -m tools.polly_check --ab --out /tmp/clips   # + the old monster voice

The first line of output names the engines, language and DM voice it resolved,
so a run against the wrong configuration is visible rather than inferred.

Exit status is 0 when every check passed, 1 when one failed, and 2 when the
check could not be run at all (no boto3, no credentials).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from typing import Any

from tts.cache import AudioCache
from tts.client import (
    DEFAULT_DM_VOICE,
    DEFAULT_ENGINE,
    DEFAULT_LANGUAGE,
    DEFAULT_MONSTER_ENGINE,
    DEFAULT_MONSTER_FX,
    WAVE,
    PollyTTS,
    TTSError,
    env_flag,
)
from tts.voices import STANDARD_ENGLISH, allowed_ssml, billable_chars

#: The two lines. Short on purpose — this is a check, not a demo — and
#: different from each other so a mixed-up cache entry could not hide.
TABLE_LINE = "The cart still smoulders, and the road ahead is dark."
MONSTER_LINE = "You will not leave this cave alive."

TABLE_KEY = "dm"
MONSTER_KEY = "monster:goblin_1"


class Recorder:
    """Wraps the boto3 Polly client and keeps what crossed it.

    `PollyTTS` reads and closes the audio stream and returns only the bytes, so
    the request it built and Polly's own `RequestCharacters` — the billed count,
    from the `x-amzn-RequestCharacters` response header — are gone by the time
    the caller sees a `TTSResult`. Recording them here is what lets this report
    what was actually sent rather than what we believe would be.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.sent: list[dict] = []
        self.billed: list[int | None] = []

    def describe_voices(self, **kw):
        return self.inner.describe_voices(**kw)

    def synthesize_speech(self, **kw):
        resp = self.inner.synthesize_speech(**kw)
        self.sent.append(kw)
        self.billed.append(_request_characters(resp))
        return resp


def _request_characters(resp: Any) -> int | None:
    """Polly's own count of what it billed for, or None if it did not say.

    boto3 models the `x-amzn-RequestCharacters` response header as
    `RequestCharacters`; the raw header is read as a fallback in case a future
    SDK stops modelling it.
    """
    try:
        n = (resp or {}).get("RequestCharacters")
        if n is not None:
            return int(n)
        headers = ((resp or {}).get("ResponseMetadata") or {}).get("HTTPHeaders") or {}
        raw = headers.get("x-amzn-requestcharacters")
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _looks_like_mp3(data: bytes) -> bool:
    """An MPEG frame header, or an ID3 tag in front of one."""
    if data[:3] == b"ID3":
        return True
    return len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _looks_like_wav(data: bytes) -> bool:
    """A RIFF/WAVE container, which is what a treated monster comes back as."""
    return data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _wav_rate(data: bytes) -> int | None:
    """The sample rate a RIFF header declares, which for a monster IS the size
    of the creature — the whole shift is that number."""
    if not _looks_like_wav(data) or len(data) < 28:
        return None
    return int.from_bytes(data[24:28], "little")


def _suffix(media_type: str) -> str:
    return ".wav" if media_type == WAVE else ".mp3"


class Report:
    """Prints as it goes and remembers whether anything failed.

    Prints as it goes because the interesting output of a failed run is the
    lines before the failure — the SSML that was sent, the voice it was sent
    in — and a run that dies on the second clip should still have printed the
    first.
    """

    def __init__(self) -> None:
        self.failures = 0

    def say(self, line: str = "") -> None:
        print(line)

    def check(self, ok: bool, what: str, detail: str = "") -> bool:
        mark = "ok  " if ok else "FAIL"
        if not ok:
            self.failures += 1
        print(f"  [{mark}] {what}{(' — ' + detail) if detail else ''}")
        return ok

    def note(self, what: str) -> None:
        """Something worth reading that is not a pass or a fail."""
        print(f"  [note] {what}")


def build(args: argparse.Namespace, cache_dir: str, client: Any) -> PollyTTS:
    """The service as the app would build it, minus `from_env`'s cache.

    `from_env` is deliberately not used: it reads `DND_TTS_CACHE`, and this
    must not write to the cache the app serves from. Everything else is read
    from the same environment variables so that what this checks is what the
    droplet is configured to do.
    """
    return PollyTTS(
        AudioCache(cache_dir, 0),          # 0 = never prune; the directory is temporary
        client=client,
        region=args.region or os.environ.get("DND_TTS_REGION")
        or os.environ.get("AWS_REGION") or "",
        engine=args.engine or os.environ.get("DND_TTS_ENGINE") or DEFAULT_ENGINE,
        monster_engine=args.monster_engine or os.environ.get("DND_TTS_MONSTER_ENGINE")
        or (DEFAULT_MONSTER_ENGINE if monster_fx(args) else "standard"),
        monster_fx=monster_fx(args),
        language=args.lang or os.environ.get("DND_TTS_LANG") or DEFAULT_LANGUAGE,
        dm_voice=args.dm_voice or os.environ.get("DND_TTS_DM_VOICE") or DEFAULT_DM_VOICE,
    )


def _how_monsters_are_made(svc: PollyTTS) -> str:
    """The header's one-phrase summary of the monster arrangement.

    Asked of `allowed_ssml` rather than assumed, because the answer for the
    untreated arrangement depends on the engine: `<amazon:effect
    vocal-tract-length>` exists only where the matrix says it does, and an
    untreated monster anywhere else is a plain voice reading a monster's lines.
    That configuration is reachable (`DND_TTS_MONSTER_FX=0` beside a
    `DND_TTS_MONSTER_ENGINE` that is not `standard`) and is exactly what this
    tool is for, so the header says so rather than naming a tag that will not
    be written.

    Through the same function `ssml_for` writes the document with, so the two
    cannot disagree about an engine neither of them recognises.
    """
    if svc.monster_fx:
        return "+ post-processing"
    if "vtl" in allowed_ssml(svc.monster_engine):
        return "(vocal-tract-length)"
    return "(UNTREATED: this engine has no vocal-tract-length)"


def _the_other_way(svc: PollyTTS, client: Any, cache_dir: str) -> PollyTTS:
    """`svc` with the monsters rendered the other way, and nothing else moved.

    Derived from the service rather than rebuilt from the arguments, because
    the run being compared against is whatever this deployment is configured
    for: under `--no-monster-fx` (or `DND_TTS_MONSTER_FX=0`) the primary render
    IS the old voice, and rebuilding "the old voice" from the same arguments
    would save two copies of it and call them a comparison.

    The untreated arrangement is always put on `standard`, whatever
    `--monster-engine` said, because that is the only engine
    `<amazon:effect vocal-tract-length>` exists on — an untreated monster
    anywhere else is not the old voice, it is a plain one.
    """
    fx = not svc.monster_fx
    return PollyTTS(
        AudioCache(cache_dir, 0),
        client=client,
        region=svc.region,
        engine=svc.engine,
        monster_engine=svc.engine if fx else "standard",
        monster_fx=fx,
        language=svc.language,
        dm_voice=svc.dm_voice,
    )


def monster_fx(args: argparse.Namespace) -> bool:
    """Whether monsters are post-processed, as `from_env` decides it —
    `--no-monster-fx` being the command-line way to say `DND_TTS_MONSTER_FX=0`."""
    if getattr(args, "no_monster_fx", False):
        return False
    return env_flag("DND_TTS_MONSTER_FX", DEFAULT_MONSTER_FX)


def live_roster(client: Any, language: str) -> dict[str, set[str]] | None:
    """`DescribeVoices`, asked here and not through `PollyTTS`. None if it
    could not be asked.

    `PollyTTS.voices()` cannot answer this question, and the way it cannot is
    the trap: on the standard engine a failed listing comes back as
    `STANDARD_ENGLISH`, so comparing `voices("standard")` with
    `STANDARD_ENGLISH` checks the fallback roster against itself and reports a
    pass for a call that never happened. That is precisely the shape of failure
    this whole tool exists to rule out, one level up — a check that says "ok"
    because it never looked.

    The language filter is `PollyTTS._describe`'s: the prefix, so `en-US`
    accepts every English variant including `en-GB-WLS`.
    """
    prefix = str(language or "").split("-")[0].lower()
    out: dict[str, set[str]] = {}
    try:
        token = None
        while True:
            resp = client.describe_voices(**({"NextToken": token} if token else {})) or {}
            for row in resp.get("Voices") or []:
                if not str(row.get("LanguageCode") or "").lower().startswith(prefix):
                    continue
                for engine in row.get("SupportedEngines") or []:
                    out.setdefault(str(engine).lower(), set()).add(str(row.get("Id") or ""))
            token = resp.get("NextToken")
            if not token:
                break
    except Exception:      # network, IAM, a region without Polly
        return None
    return out


def report_rosters(svc: PollyTTS, rec: Recorder, rep: Report) -> None:
    """What `DescribeVoices` says, and whether the built-in roster agrees.

    `STANDARD_ENGLISH` is the roster the app casts from when `DescribeVoices`
    fails, so a voice in it the standard engine does not actually serve is an
    `EngineNotSupportedException` on every line dealt to it — and precisely in
    the outage where nothing else is working either. Nothing but a live listing
    can check that, which is why the listing is read directly.
    """
    listed = live_roster(rec, svc.language)
    if listed is None:
        rep.check(False, "DescribeVoices answered",
                  "no live roster: the app would be casting from its built-in "
                  "fallback on standard and reporting /api/tts unavailable on any "
                  "other engine")
        return

    for engine in dict.fromkeys((svc.engine, svc.monster_engine)):
        pool = sorted(listed.get(engine, set()))
        rep.check(bool(pool), f"{engine}: {svc.language} voices listed",
                  f"{len(pool)} voices" if pool
                  else "none — /api/tts reports unavailable and the page uses "
                       "the browser's own voices")
        if pool:
            rep.say(f"         {', '.join(pool)}")

    # What the app would actually fall back to here, which is `_fallback_pool`'s
    # rule: `STANDARD_ENGLISH` narrowed to this language. Outside English that
    # is empty by design — a French line read by an English voice is a wrong
    # narrator, not a degraded one — so there is nothing to check rather than
    # fifteen voices to complain about.
    prefix = svc.language.split("-")[0].lower()
    fallback = [v.id for v in STANDARD_ENGLISH if v.language.lower().startswith(prefix)]
    standard = listed.get("standard", set())
    if not fallback:
        rep.note(f"the built-in fallback roster is English, so {svc.language} has no "
                 f"fallback to check — a failed DescribeVoices means no voices at all")
        return
    if not standard:
        rep.note("this region lists no standard voices for this language, so the "
                 "built-in fallback roster cannot be checked against it")
        return
    missing = sorted(vid for vid in fallback if vid not in standard)
    rep.check(not missing, "the built-in fallback roster is all served here",
              f"not served: {', '.join(missing)}" if missing else "")
    extra = sorted(standard - set(fallback))
    if extra:
        rep.note(f"standard voices this region has that the fallback roster lacks: "
                 f"{', '.join(extra)}")


def cast_or_builtin(svc: PollyTTS, key: str):
    """The seat this key sits in, or what it would sit in with no listing.

    Only for `--dry-run`, which has to work on a laptop with no credentials at
    all: `voices()` comes back empty there and `cast_for` refuses an empty
    pool, correctly. The built-in roster is what the app itself would fall back
    to on the standard engine, so it is the closest honest answer — and the
    caller says so rather than passing it off as the live casting.
    """
    from tts.voices import cast_for  # noqa: PLC0415

    try:
        return svc.cast(key), True
    except ValueError:
        engine = svc.engine_for(key)
        # `monster_fx` from the SERVICE, not from `cast_for`'s default. A dry
        # run is a configuration audit, and the one configuration it most has
        # to be able to show is a broken one: `--no-monster-fx
        # --monster-engine neural` is an untreated monster on an engine with no
        # `vocal-tract-length`, i.e. a plain voice reading a monster's lines.
        # Defaulting here reported it as treated and hid exactly that.
        return cast_for(key, STANDARD_ENGLISH, svc.dm_voice, "", engine,
                        monster_fx=svc.monster_fx), False


def speak(svc: PollyTTS, rec: Recorder, key: str, text: str, rep: Report,
          out_dir: str = "", label: str = "") -> tuple[bytes, dict]:
    """One line, through the real client, reported in full.

    `label` names the saved file where it is not simply the key — the A/B pass
    renders the same seat twice and the two must not overwrite each other.
    """
    label = label or key
    try:
        cast = svc.cast(key)
    except ValueError as exc:      # an empty pool: no roster for this engine
        rep.say()
        rep.say(f"{label}")
        rep.check(False, f"a {svc.engine_for(key)} voice to cast from", str(exc))
        return b"", {}
    ssml = svc.ssml(text, cast)
    rep.say()
    rep.say(f"{label}")
    rep.say(f"  engine   {cast.engine}   (${svc.rate_for(cast.engine):.2f}/1M chars)")
    rep.say(f"  voice    {cast.voice_id} [{cast.language}]")
    rep.say(f"  ssml     {ssml}")

    before = len(rec.sent)
    try:
        result = svc.render(key, text)
    except TTSError as exc:
        rep.check(False, "Polly synthesized it", str(exc))
        return b"", {}

    sent = rec.sent[before] if len(rec.sent) > before else {}
    billed = rec.billed[before] if len(rec.billed) > before else None
    rep.say(f"  audio    {len(result.audio)} bytes")
    rep.say(f"  billed   {result.chars} chars by this app"
            + (f", {billed} by Polly" if billed is not None else "")
            + f"  (${result.usd:.6f})")

    rep.check(bool(result.audio), "Polly returned audio")
    if cast.fx:
        # A treated monster is a WAV of `pcm` Polly rendered, and the rate in
        # its header is the size shift — the effect is that number and nothing
        # else, so a header that came back at 16000 is a monster that was not
        # treated at all.
        rep.check(sent.get("OutputFormat") == "pcm", "the request asked for pcm",
                  repr(sent.get("OutputFormat")))
        rep.check(_looks_like_wav(result.audio), "the bytes are a RIFF/WAVE",
                  "" if _looks_like_wav(result.audio) else repr(result.audio[:12]))
        rate = _wav_rate(result.audio)
        rep.check(rate is not None and rate == cast.fx.playback_rate(),
                  f"it plays at {cast.fx.playback_rate()} Hz "
                  f"(size {cast.fx.size_pct:+d}%)", f"header says {rate}")
        rep.say(f"  treated  size {cast.fx.size_pct:+d}%  growl {cast.fx.growl_pct}%  "
                f"cave {cast.fx.cave_pct}%")
    else:
        rep.check(_looks_like_mp3(result.audio), "the bytes are an mp3",
                  "" if _looks_like_mp3(result.audio) else repr(result.audio[:8]))
    rep.check(sent.get("Engine") == cast.engine,
              f"the request said Engine={cast.engine}", repr(sent.get("Engine")))
    rep.check(sent.get("VoiceId") == cast.voice_id,
              f"the request said VoiceId={cast.voice_id}", repr(sent.get("VoiceId")))
    rep.check(sent.get("TextType") == "ssml", "the request was SSML")

    # Not a failure: the ledger's per-character price comes from
    # `billable_chars`, and Polly's own count is the authority on what was
    # charged. If they ever disagree, the ledger is the thing to revisit.
    if billed is not None and billed != result.chars:
        rep.note(f"Polly billed {billed} characters where `billable_chars` counted "
                 f"{result.chars} — the ledger charges the second")

    if out_dir:
        path = os.path.join(out_dir, label.replace(":", "_") + _suffix(result.media_type))
        with open(path, "wb") as fh:
            fh.write(result.audio)
        rep.say(f"  saved    {path}")
    return result.audio, sent


def dry_run(svc: PollyTTS, args: argparse.Namespace, rep: Report) -> int:
    """Everything except the part that costs money."""
    rep.say("--dry-run: nothing is sent to Polly and nothing is charged.")
    for key, text in ((TABLE_KEY, args.text), (MONSTER_KEY, args.monster_text)):
        cast, live = cast_or_builtin(svc, key)
        rep.say()
        rep.say(f"{key}")
        rep.say(f"  engine   {cast.engine}")
        rep.say(f"  voice    {cast.voice_id} [{cast.language}]"
                + ("" if live else "   (from the built-in roster: nothing listed)"))
        rep.say(f"  ssml     {svc.ssml(text, cast)}")
        if cast.fx:
            rep.say(f"  treated  size {cast.fx.size_pct:+d}%  "
                    f"growl {cast.fx.growl_pct}%  cave {cast.fx.cave_pct}%  "
                    f"-> pcm at {cast.fx.playback_rate()} Hz, served as a WAV")
        rep.say(f"  would bill {billable_chars(text)} chars "
                f"(${svc.price_of(billable_chars(text), cast.engine):.6f})")
    return 0


def main(argv: list[str] | None = None, client: Any = None) -> int:
    """`client` is the boto3 Polly client, injectable exactly as `PollyTTS`'s
    is: `tests/tools/test_polly_check.py` drives every path here without an AWS
    account, which is the same reason the service takes one."""
    ap = argparse.ArgumentParser(
        prog="python -m tools.polly_check",
        description="Send one monster line and one table line to real Polly.",
    )
    ap.add_argument("--region", default="", help="AWS region (default: DND_TTS_REGION/AWS_REGION)")
    ap.add_argument("--engine", default="", help="table engine (default: DND_TTS_ENGINE)")
    ap.add_argument("--monster-engine", default="",
                    help="monster engine (default: DND_TTS_MONSTER_ENGINE)")
    ap.add_argument("--lang", default="", help="language (default: DND_TTS_LANG)")
    ap.add_argument("--dm-voice", default="", help="DM voice (default: DND_TTS_DM_VOICE)")
    ap.add_argument("--text", default=TABLE_LINE, help="the table line to speak")
    ap.add_argument("--monster-text", default=MONSTER_LINE, help="the monster line to speak")
    ap.add_argument("--out", default="", help="directory to save the clips in")
    ap.add_argument("--no-monster-fx", action="store_true",
                    help="render monsters the old way: standard engine, "
                         "vocal-tract-length (DND_TTS_MONSTER_FX=0)")
    ap.add_argument("--ab", action="store_true",
                    help="also render the monster line the old way, to compare "
                         "(one extra clip, a fraction of a cent)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the requests and send nothing")
    args = ap.parse_args(argv)

    rep = Report()
    cache_dir = tempfile.mkdtemp(prefix="polly-check-")
    try:
        # A bare service first, only to resolve boto3 and the credential chain
        # the same way the app does — `available()` is that resolution.
        probe = build(args, cache_dir, client)
        print(f"region {probe.region or '(from the environment)'} · "
              f"table {probe.engine} · monsters {probe.monster_engine} "
              f"{_how_monsters_are_made(probe)} · "
              f"language {probe.language} · DM voice {probe.dm_voice}")

        # `--dry-run` before the credential check on purpose: printing the
        # documents this app would send is exactly what is worth doing on a
        # laptop that has no AWS account.
        if args.dry_run:
            return dry_run(probe, args, rep)

        if not probe.available():
            print("no Polly client: boto3 is missing, or AWS credentials are not set.")
            print("On the droplet the keys are in .env, which `run.sh` sources and a")
            print("hand-run python does not. Source it the same way run.sh does:")
            print("  cd /var/www/dndsim && (set -a; . ./.env; set +a; "
                  ".venv/bin/python -m tools.polly_check)")
            return 2

        rec = Recorder(probe.client())
        svc = build(args, cache_dir, rec)

        if args.out:
            os.makedirs(args.out, exist_ok=True)

        print()
        report_rosters(svc, rec, rep)

        table, table_req = speak(svc, rec, TABLE_KEY, args.text, rep, args.out)
        monster, monster_req = speak(svc, rec, MONSTER_KEY, args.monster_text, rep, args.out)

        other = b""
        if args.ab and monster:
            # The same line, rendered the OTHER way — derived from the service
            # above rather than from the arguments, so it differs in exactly
            # one dimension whichever way this run is configured. Building it
            # from the arguments is how the comparison silently becomes two
            # copies of the same arrangement under `--no-monster-fx`.
            counterpart = _the_other_way(svc, rec, cache_dir)
            which = "new" if counterpart.monster_fx else "old"
            print()
            print(f"the {which} monster voice, for comparison")
            other, _req = speak(counterpart, rec, MONSTER_KEY, args.monster_text, rep,
                                args.out, label=f"monster_goblin_1.{which}")

        print()
        print("the monsters")
        if not table_req or not monster_req:
            # One of them never reached Polly, and `speak` has already said
            # which and why. Read from the requests rather than asking the
            # service again: re-casting here would raise the very ValueError
            # that was just reported, losing the summary and the exit code to
            # a traceback.
            rep.check(False, "both lines reached Polly",
                      "one of them did not — see above")
        else:
            # With the treatment on, the monster line must carry NOTHING the
            # table's engine would refuse — that is the whole reason it can sit
            # on the table's engine at all. With it off, a monster on standard
            # always carries the effect: `MONSTER_VTL` holds no 0, precisely so
            # that no monster ends up untreated.
            treated = svc.monster_fx
            wants_vtl = not treated and monster_req.get("Engine") == "standard"
            rep.check(("vocal-tract-length" in monster_req.get("Text", "")) == wants_vtl,
                      "the monster line carried vocal-tract-length"
                      if wants_vtl else
                      "the monster line carried no vocal-tract-length "
                      "(it is treated after synthesis)" if treated else
                      "the monster line carried no vocal-tract-length (its engine has none)")
            rep.check("vocal-tract-length" not in table_req.get("Text", ""),
                      "the table line carried no vocal-tract-length")
            rep.check(bool(table) and bool(monster) and table != monster,
                      "both lines came back, and as different audio")
            if treated:
                rep.check(monster_req.get("OutputFormat") == "pcm"
                          and table_req.get("OutputFormat") == "mp3",
                          "only the monster asked for pcm",
                          f"table={table_req.get('OutputFormat')!r} "
                          f"monster={monster_req.get('OutputFormat')!r}")
            if args.ab:
                rep.check(bool(other) and other != monster,
                          "the two monster voices are different audio")
                rep.note("play them back to back; the treated one should be the "
                         "same production as the DM line, only bigger")

        print()
        if rep.failures:
            print(f"{rep.failures} check(s) FAILED — see above. "
                  f"A failing monster line is a 502 the page hides by speaking "
                  f"the line itself; `dndsim logs` shows it.")
            return 1
        print("all checks passed: the monster path renders on real Polly.")
        return 0
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


if __name__ == "__main__":       # pragma: no cover - a hand-run tool
    sys.exit(main())
