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

    def __init__(self, *, fail_on: str = "") -> None:
        self.fail_on = fail_on            # an engine whose requests raise
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

    # Both lines, on the two engines the split puts them on, in that order.
    assert [s["Engine"] for s in polly.sent] == ["neural", "standard"]
    assert all(s["TextType"] == "ssml" and s["OutputFormat"] == "mp3" for s in polly.sent)
    assert "vocal-tract-length" in polly.sent[1]["Text"]
    assert "vocal-tract-length" not in polly.sent[0]["Text"]


def test_a_monster_line_polly_refuses_is_a_non_zero_exit(capsys):
    """The whole point. A table line succeeding must not be enough to pass —
    that is exactly the state the droplet could be in right now, and the
    browser would sound fine in it."""
    polly = FakePolly(fail_on="standard")
    code, out = run([], polly, capsys)
    assert code == 1
    assert "InvalidSsmlException" in out
    assert "monster:goblin_1" in out
    # The table line still worked, and the run still failed.
    assert [s["Engine"] for s in polly.sent] == ["neural", "standard"]


def test_a_table_line_polly_refuses_is_also_a_failure(capsys):
    code, out = run([], FakePolly(fail_on="neural"), capsys)
    assert code == 1 and "FAIL" in out


def test_it_says_which_engine_and_voice_each_line_used(capsys):
    _, out = run([], FakePolly(), capsys)
    assert "engine   standard" in out and "engine   neural" in out
    assert "billed   35 chars by this app, 35 by Polly" in out   # the monster line
    assert "bytes" in out


def test_the_dry_run_sends_nothing_and_needs_no_credentials(capsys):
    polly = FakePolly()
    code, out = run(["--dry-run"], polly, capsys)
    assert code == 0
    assert polly.sent == []
    assert "nothing is sent to Polly" in out
    assert "vocal-tract-length" in out       # the document it would have sent

    # And with no client at all, which is a laptop: it still prints the
    # documents rather than refusing, using the built-in roster to cast.
    code, out = run(["--dry-run"], None, capsys)
    assert code == 0 and "vocal-tract-length" in out
    assert "built-in roster" in out


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
    assert written == ["dm.mp3", "monster_goblin_1.mp3"]
    assert (out_dir / "monster_goblin_1.mp3").read_bytes().startswith(MP3)


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
    # The monster line still goes out — on standard, cast from that very
    # fallback roster, which is exactly the situation the check is warning is
    # unverified. The DM has no neural roster to be cast from at all.
    assert [s["Engine"] for s in polly.sent] == ["standard"]
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
