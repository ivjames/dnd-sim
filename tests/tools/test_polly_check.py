"""The droplet verification tool, driven without an AWS account.

`tools/polly_check.py` is the one thing in this repo that talks to real Polly,
so it is the one thing whose own correctness nothing else can catch: a check
that reports "ok" because it never looked is worse than no check. These tests
drive its `main()` with an injected client — the same seam `PollyTTS` has, and
for the same reason — and assert that it fails when it should.
"""

from __future__ import annotations

import os
import re

from tools import polly_check as P
from tts.voices import STANDARD_ENGLISH

MP3 = b"\xff\xfb\x90\x00"          # an MPEG frame header, which is what Polly returns

#: What `DescribeVoices` would answer in an English region: the standard
#: roster the app ships as its fallback, plus one voice only the neural engine
#: serves — so the two engines really do have different pools here, as they do
#: on the droplet.
ENGLISH = [(v.id, v.language, v.gender, ["standard", "neural"]) for v in STANDARD_ENGLISH]
ENGLISH.append(("Olivia", "en-AU", "Female", ["neural"]))


class FakePolly:
    """Enough of the boto3 client for the tool, and nothing more."""

    def __init__(self, *, fail_on: str = "", fail_on_format: str = "") -> None:
        self.fail_on = fail_on            # an engine whose requests raise
        self.fail_on_format = fail_on_format   # an OutputFormat whose requests raise
        self.sent: list[dict] = []

    def describe_voices(self, **_kw):
        return {"Voices": [
            {"Id": vid, "LanguageCode": lang, "Gender": gender, "SupportedEngines": engines}
            for vid, lang, gender, engines in ENGLISH
        ]}

    def synthesize_speech(self, **kw):
        self.sent.append(kw)
        if self.fail_on and kw.get("Engine") == self.fail_on:
            raise RuntimeError("InvalidSsmlException: Invalid SSML request")
        if self.fail_on_format and kw.get("OutputFormat") == self.fail_on_format:
            raise RuntimeError("InvalidSampleRateException: The specified sample rate is not valid")
        return {
            "AudioStream": _Stream(MP3 + kw["Text"].encode()),
            # What Polly bills: the words, not the markup.
            "RequestCharacters": len(re.sub(r"<[^>]*>", "", kw["Text"])),
        }


class _Stream:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data

    def close(self) -> None:
        pass


def run(argv, client, capsys):
    code = P.main(argv, client=client)
    return code, capsys.readouterr().out


def test_a_working_polly_is_a_clean_pass(capsys):
    polly = FakePolly()
    code, out = run([], polly, capsys)
    assert code == 0, out
    assert "all checks passed" in out
    assert "FAIL" not in out

    # Both lines on the table's engine, in that order — the monster does not
    # need its own any more.
    assert [s["Engine"] for s in polly.sent] == ["neural", "neural"]
    assert all(s["TextType"] == "ssml" for s in polly.sent)
    # And only the monster asks for the format that can be post-processed.
    assert [s["OutputFormat"] for s in polly.sent] == ["mp3", "pcm"]
    assert polly.sent[1]["SampleRate"] == "16000"
    assert all("vocal-tract-length" not in s["Text"] for s in polly.sent)


def test_the_old_arrangement_still_passes_too(capsys):
    """`--no-monster-fx` is `DND_TTS_MONSTER_FX=0`, and it has to keep
    working: it is the way back if the treatment turns out to sound worse."""
    polly = FakePolly()
    code, out = run(["--no-monster-fx"], polly, capsys)
    assert code == 0, out
    assert [s["Engine"] for s in polly.sent] == ["neural", "standard"]
    assert all(s["OutputFormat"] == "mp3" for s in polly.sent)
    assert "vocal-tract-length" in polly.sent[1]["Text"]
    assert "vocal-tract-length" not in polly.sent[0]["Text"]


def test_the_ab_pass_renders_the_monster_line_both_ways(capsys, tmp_path):
    """Whether the treatment sounds better than the engine it replaced is a
    judgement, and this is the only place it can be made: three clips, two of
    them the same words in the same seat."""
    polly = FakePolly()
    out_dir = tmp_path / "clips"
    code, out = run(["--ab", "--out", str(out_dir)], polly, capsys)
    assert code == 0, out
    assert [s["Engine"] for s in polly.sent] == ["neural", "neural", "standard"]
    assert [s["OutputFormat"] for s in polly.sent] == ["mp3", "pcm", "mp3"]
    assert "vocal-tract-length" in polly.sent[2]["Text"]
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "dm.mp3", "monster_goblin_1.old.mp3", "monster_goblin_1.wav"]
    assert "the old monster voice" in out


def test_a_monster_line_polly_refuses_is_a_non_zero_exit(capsys):
    """The whole point. A table line succeeding must not be enough to pass —
    that is exactly the state the droplet could be in right now, and the
    browser would sound fine in it."""
    # The monster line is the only one that asks for `pcm`, so a Polly that
    # refuses that format refuses exactly the monsters — which is the shape of
    # the failure this tool exists for.
    polly = FakePolly(fail_on_format="pcm")
    code, out = run([], polly, capsys)
    assert code == 1
    assert "InvalidSampleRateException" in out
    assert "monster:goblin_1" in out
    # The table line still worked, and the run still failed.
    assert [s["OutputFormat"] for s in polly.sent] == ["mp3", "pcm"]


def test_a_table_line_polly_refuses_is_also_a_failure(capsys):
    code, out = run([], FakePolly(fail_on="neural"), capsys)
    assert code == 1 and "FAIL" in out


def test_it_says_which_engine_and_voice_each_line_used(capsys):
    _, out = run([], FakePolly(), capsys)
    assert "engine   neural" in out
    assert "billed   35 chars by this app, 35 by Polly" in out   # the monster line
    assert "bytes" in out
    # And what was done to the monster afterwards, which nothing else reports:
    # the size shift is a sample rate, so an untreated monster would show 16000.
    assert re.search(r"treated  size [+-]\d+%", out)
    assert re.search(r"it plays at \d+ Hz", out)

    _, old = run(["--no-monster-fx"], FakePolly(), capsys)
    assert "engine   standard" in old and "treated" not in old


def test_the_dry_run_sends_nothing_and_needs_no_credentials(capsys):
    polly = FakePolly()
    code, out = run(["--dry-run"], polly, capsys)
    assert code == 0
    assert polly.sent == []
    assert "nothing is sent to Polly" in out
    assert "-> pcm at" in out                 # what it would have done to the monster

    # And with no client at all, which is a laptop: it still prints the
    # documents rather than refusing, using the built-in roster to cast.
    code, out = run(["--dry-run"], None, capsys)
    assert code == 0 and "-> pcm at" in out
    assert "built-in roster" in out

    # The old arrangement prints the document it would have sent, which is the
    # one thing a dry run of it can show.
    code, out = run(["--dry-run", "--no-monster-fx"], FakePolly(), capsys)
    assert code == 0 and "vocal-tract-length" in out


def test_no_credentials_is_exit_2_not_a_pass(capsys):
    """"Could not check" and "checked, all good" must not look the same to a
    script, or to a person reading the tail of the output."""
    code, out = run([], None, capsys)
    assert code == 2
    assert "credentials" in out and "all checks passed" not in out


def test_it_leaves_the_apps_cache_alone(tmp_path, capsys, monkeypatch):
    """Its clips are paid for and thrown away: an app whose cache it filled
    would serve them for a year under keys this tool chose."""
    app_cache = tmp_path / "tts"
    app_cache.mkdir()
    monkeypatch.setenv("DND_TTS_CACHE", str(app_cache))
    made: list[str] = []
    real = P.tempfile.mkdtemp
    monkeypatch.setattr(P.tempfile, "mkdtemp", lambda **kw: made.append(real(**kw)) or made[-1])

    assert run([], FakePolly(), capsys)[0] == 0
    assert list(app_cache.iterdir()) == []          # DND_TTS_CACHE is not read
    assert made and not os.path.exists(made[0])     # and its own is gone


def test_it_can_keep_the_clips_to_listen_to(tmp_path, capsys):
    out_dir = tmp_path / "clips"
    assert run(["--out", str(out_dir)], FakePolly(), capsys)[0] == 0
    written = sorted(p.name for p in out_dir.iterdir())
    # Saved under what each clip actually is: the monster's has been through
    # `tts/dsp.py` and is a WAV, and a `.mp3` holding one plays nowhere.
    assert written == ["dm.mp3", "monster_goblin_1.wav"]
    assert (out_dir / "dm.mp3").read_bytes().startswith(MP3)
    assert (out_dir / "monster_goblin_1.wav").read_bytes()[:4] == b"RIFF"


def test_a_voice_the_standard_engine_does_not_serve_is_a_failure(capsys):
    """The built-in roster is what a failed `DescribeVoices` casts from, so a
    voice in it this region does not serve is a 502 in exactly the outage
    where nothing else is working either. Only a live listing can say."""
    polly = FakePolly()
    polly.describe_voices = lambda **_kw: {"Voices": [
        {"Id": "Joey", "LanguageCode": "en-US", "Gender": "Male",
         "SupportedEngines": ["standard", "neural"]},
        {"Id": "Brian", "LanguageCode": "en-GB", "Gender": "Male",
         "SupportedEngines": ["standard", "neural"]},
    ]}
    code, out = run([], polly, capsys)
    assert code == 1
    assert "built-in fallback roster" in out and "Amy" in out


def test_one_engine_for_the_whole_table_is_not_reported_as_broken(capsys):
    """`DND_TTS_MONSTER_ENGINE=DND_TTS_ENGINE` is a supported choice: the
    monster loses its timbre rather than its voice, and the check has to know
    the difference between that and a failure."""
    polly = FakePolly()
    code, out = run(["--engine", "neural", "--monster-engine", "neural"], polly, capsys)
    assert code == 0, out
    assert [s["Engine"] for s in polly.sent] == ["neural", "neural"]
    assert all("vocal-tract-length" not in s["Text"] for s in polly.sent)


def test_a_failed_listing_is_not_a_verified_roster(capsys):
    """The trap this tool is one level up from.

    `PollyTTS.voices("standard")` answers a failed `DescribeVoices` with
    `STANDARD_ENGLISH` — the very roster being checked — so asking it would
    compare the fallback with itself and report a pass for a call that never
    happened. The listing has to be read directly.
    """
    polly = FakePolly()

    def refuse(**_kw):
        raise RuntimeError("EndpointConnectionError: could not connect")

    polly.describe_voices = refuse
    code, out = run([], polly, capsys)
    assert code == 1
    assert "DescribeVoices answered" in out and "FAIL" in out
    # The claim it must not make.
    assert "built-in fallback roster is all served here" not in out
    # Neither line goes out: both seats are cast from the table's engine now,
    # and a failed listing leaves that engine with no roster at all. Under the
    # old split the monster would still have been sent, cast from the built-in
    # fallback — which is the situation the check is warning is unverified.
    assert polly.sent == []
    assert "a neural voice to cast from" in out


def test_an_engine_with_no_voices_for_the_language_is_a_failure(capsys):
    """`/api/tts` reports unavailable when any configured engine has no
    roster, which switches server voices off for the whole game."""
    polly = FakePolly()
    polly.describe_voices = lambda **_kw: {"Voices": [
        {"Id": vid, "LanguageCode": lang, "Gender": gender, "SupportedEngines": ["standard"]}
        for vid, lang, gender, _ in ENGLISH if vid != "Olivia"
    ]}
    code, out = run([], polly, capsys)
    assert code == 1
    assert "neural: en-US voices listed" in out
    assert "the browser's own voices" in out
    # No neural voice to cast the DM from, and it says so instead of raising.
    assert "a neural voice to cast from" in out


def test_the_listing_is_read_once_and_filtered_by_language(capsys):
    """A French deployment must not be told its English roster is fine."""
    polly = FakePolly()
    polly.describe_voices = lambda **_kw: {"Voices": [
        {"Id": "Lea", "LanguageCode": "fr-FR", "Gender": "Female",
         "SupportedEngines": ["standard", "neural"]},
        {"Id": "Joey", "LanguageCode": "en-US", "Gender": "Male",
         "SupportedEngines": ["standard"]},
    ]}
    code, out = run(["--lang", "fr-FR", "--dm-voice", "Lea"], polly, capsys)
    assert "Lea" in out
    # The English voice is not in the listing this deployment cast from, and
    # the English fallback roster is not something fr-FR would ever use — so
    # it is reported as not applicable rather than as fifteen missing voices.
    assert "fr-FR has no fallback to check" in out
    assert "not served:" not in out
    assert code == 0, out


def test_a_seat_with_no_voices_is_reported_not_raised(capsys):
    """`speak()` reports an uncastable seat instead of raising — and nothing
    after it may undo that by asking the service to cast the same key again.
    A traceback here loses the summary and the exit code both."""
    polly = FakePolly()
    polly.describe_voices = lambda **_kw: {"Voices": [       # standard only
        {"Id": v.id, "LanguageCode": v.language, "Gender": v.gender,
         "SupportedEngines": ["standard"]} for v in STANDARD_ENGLISH
    ]}
    code, out = run(["--engine", "standard", "--monster-engine", "neural"], polly, capsys)

    assert code == 1
    assert "a neural voice to cast from" in out
    assert "no voices to cast from" in out
    # It got all the way to the end: the summary section and the tally ran.
    assert "the monsters" in out and "check(s) FAILED" in out
    assert "Traceback" not in out
    # The table line still went out; the monster never reached Polly.
    assert [s["Engine"] for s in polly.sent] == ["standard"]
