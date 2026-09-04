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
one place the app writes `<amazon:effect vocal-tract-length>`, and the one
place it routes to a different engine (`DND_TTS_MONSTER_ENGINE`, standard) to
do it, because that tag exists on no other. Polly ERRORS on a tag its engine
does not support rather than ignoring it, so a wrong monster document is an
`InvalidSsmlException` per line — which `web/routes/tts.py` answers with a 502,
which the page answers by speaking that one line in the browser's own voice.
The spectator hears a voice either way. Nothing on the page says which. A
table line succeeding proves nothing about a monster line, so this sends both.

**What it does not touch.** Its cache is a temporary directory, removed on the
way out, so the app's `data/tts` is neither read nor written — a clip made here
is paid for here and not left for the app to serve. It goes nowhere near a
game, a ledger or the database: it does not import `web` or `orchestrator`, and
there is no game id for it to charge.

    .venv/bin/python -m tools.polly_check              # two clips, ~$0.0005
    .venv/bin/python -m tools.polly_check --dry-run    # print the requests, send nothing
    .venv/bin/python -m tools.polly_check --out /tmp/clips     # keep the mp3s to listen to

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
    PollyTTS,
    TTSError,
)
from tts.voices import STANDARD_ENGLISH, billable_chars

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
        or DEFAULT_MONSTER_ENGINE,
        language=args.lang or os.environ.get("DND_TTS_LANG") or DEFAULT_LANGUAGE,
        dm_voice=args.dm_voice or os.environ.get("DND_TTS_DM_VOICE") or DEFAULT_DM_VOICE,
    )


def report_rosters(svc: PollyTTS, rep: Report) -> None:
    """What `DescribeVoices` says, and whether the built-in roster agrees.

    `STANDARD_ENGLISH` is the roster the app falls back to when
    `DescribeVoices` fails, so a voice in it the standard engine does not
    actually serve is an `EngineNotSupportedException` on every line dealt to
    it — and precisely in the outage where nothing else is working either.
    Nothing but a live listing can check that.
    """
    for engine in dict.fromkeys((svc.engine, svc.monster_engine)):
        pool = svc.voices(engine)
        rep.check(bool(pool), f"{engine}: a roster for {svc.language}",
                  f"{len(pool)} voices" if pool else "none listed — /api/tts reports unavailable")
        if pool:
            rep.say(f"         {', '.join(v.id for v in pool)}")

    live = {v.id for v in svc.voices("standard")}
    if not live:
        rep.note("no live standard roster to check the built-in one against")
        return
    missing = sorted(v.id for v in STANDARD_ENGLISH if v.id not in live)
    rep.check(not missing, "the built-in fallback roster is all standard voices",
              f"not served here: {', '.join(missing)}" if missing else "")
    extra = sorted(live - {v.id for v in STANDARD_ENGLISH})
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
        return cast_for(key, STANDARD_ENGLISH, svc.dm_voice, "", engine), False


def speak(svc: PollyTTS, rec: Recorder, key: str, text: str, rep: Report,
          out_dir: str = "") -> tuple[bytes, dict]:
    """One line, through the real client, reported in full."""
    cast = svc.cast(key)
    ssml = svc.ssml(text, cast)
    rep.say()
    rep.say(f"{key}")
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
        path = os.path.join(out_dir, key.replace(":", "_") + ".mp3")
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
    ap.add_argument("--out", default="", help="directory to save the two mp3s in")
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
              f"table {probe.engine} · monsters {probe.monster_engine} · "
              f"language {probe.language} · DM voice {probe.dm_voice}")

        # `--dry-run` before the credential check on purpose: printing the
        # documents this app would send is exactly what is worth doing on a
        # laptop that has no AWS account.
        if args.dry_run:
            return dry_run(probe, args, rep)

        if not probe.available():
            print("no Polly client: boto3 is missing, or AWS credentials are not set.")
            print("On the droplet the keys live in /var/www/dndsim/.env — see DEPLOY.md.")
            return 2

        rec = Recorder(probe.client())
        svc = build(args, cache_dir, rec)

        if args.out:
            os.makedirs(args.out, exist_ok=True)

        print()
        report_rosters(svc, rep)

        table, table_req = speak(svc, rec, TABLE_KEY, args.text, rep, args.out)
        monster, monster_req = speak(svc, rec, MONSTER_KEY, args.monster_text, rep, args.out)

        print()
        print("the split")
        monster_cast = svc.cast(MONSTER_KEY)
        wants_vtl = monster_cast.engine == "standard" and monster_cast.vtl_pct != 0
        monster_sent = monster_req.get("Text", "")
        table_sent = table_req.get("Text", "")
        rep.check(("vocal-tract-length" in monster_sent) == wants_vtl,
                  "the monster line carried vocal-tract-length"
                  if wants_vtl else
                  "the monster line carried no vocal-tract-length (its engine has none)")
        rep.check("vocal-tract-length" not in table_sent,
                  "the table line carried no vocal-tract-length")
        rep.check(bool(table) and bool(monster) and table != monster,
                  "both lines came back, and as different audio")

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
