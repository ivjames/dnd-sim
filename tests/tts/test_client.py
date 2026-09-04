"""PollyTTS: what it asks AWS for, what it charges, and what it refuses.

Driven with a fake in place of the boto3 client, so every path here — a
successful synthesis, a cache hit, a roster that cannot be fetched, an AWS
error — runs without an AWS account and without spending anything.
"""

from __future__ import annotations

import io
import threading

import pytest

from tts.cache import AudioCache
from tts.client import PollyTTS, TTSError, from_env
from tts.voices import STANDARD_ENGLISH, ssml_for

MP3 = b"\xff\xfb\x90\x00pretend-audio"


class FakePolly:
    """The two calls `PollyTTS` makes, recorded."""

    def __init__(self, voices=None, fail=None, audio=MP3):
        self.calls = []
        self.described = 0
        self.audio = audio
        self.fail = fail
        self._voices = voices if voices is not None else [
            {"Id": "Joanna", "LanguageCode": "en-US", "Gender": "Female",
             "SupportedEngines": ["standard", "neural"]},
            {"Id": "Brian", "LanguageCode": "en-GB", "Gender": "Male",
             "SupportedEngines": ["standard", "neural"]},
            {"Id": "Ruth", "LanguageCode": "en-US", "Gender": "Female",
             "SupportedEngines": ["neural", "generative"]},        # not standard
            {"Id": "Céline", "LanguageCode": "fr-FR", "Gender": "Female",
             "SupportedEngines": ["standard"]},                     # not English
        ]

    def describe_voices(self, **kwargs):
        self.described += 1
        if isinstance(self._voices, Exception):
            raise self._voices
        return {"Voices": self._voices}

    def synthesize_speech(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise self.fail
        return {"AudioStream": io.BytesIO(self.audio)}


def service(tmp_path, client=None, **kw):
    return PollyTTS(AudioCache(str(tmp_path), 0), client=client or FakePolly(), **kw)


def test_the_pool_is_this_engine_in_this_language(tmp_path):
    fake = FakePolly()
    svc = service(tmp_path, fake)
    assert [v.id for v in svc.voices()] == ["Joanna", "Brian"]   # Ruth: neural only. Céline: French.
    svc.voices()
    assert fake.described == 1                                    # asked once, then remembered


def test_a_roster_that_cannot_be_fetched_falls_back_rather_than_going_quiet(tmp_path):
    fake = FakePolly(voices=RuntimeError("AccessDenied: polly:DescribeVoices"))
    svc = service(tmp_path, fake)
    assert {v.id for v in svc.voices()} == {v.id for v in STANDARD_ENGLISH}
    assert svc.cast("dm").voice_id == "Brian"


def test_a_line_is_synthesized_once_and_then_it_is_free(tmp_path):
    fake = FakePolly()
    svc = service(tmp_path, fake)
    first = svc.synthesize("dm", "The cart still smoulders.")
    assert first.audio == MP3 and not first.cached
    assert first.chars == len("The cart still smoulders.")
    assert first.usd == pytest.approx(first.chars * 4.0 / 1_000_000)

    again = svc.synthesize("dm", "The cart still smoulders.")
    assert again.cached and again.usd == 0.0 and again.chars == 0
    assert again.audio == MP3 and again.key == first.key
    assert len(fake.calls) == 1                     # Polly was asked exactly once

    sent = fake.calls[0]
    assert sent["Engine"] == "standard" and sent["OutputFormat"] == "mp3"
    assert sent["TextType"] == "ssml" and sent["VoiceId"] == first.cast.voice_id
    assert sent["Text"] == ssml_for("The cart still smoulders.", first.cast)


def test_two_spectators_on_one_line_are_one_bill(tmp_path):
    fake = FakePolly()
    svc = service(tmp_path, fake)
    out = []
    barrier = threading.Barrier(4)

    def ask():
        barrier.wait()
        out.append(svc.synthesize("npc", "Who goes there?"))

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == 4 and all(r.audio == MP3 for r in out)
    assert len(fake.calls) == 1
    assert sum(r.usd for r in out) == pytest.approx(len("Who goes there?") * 4.0 / 1_000_000)


def test_what_it_will_not_say(tmp_path):
    svc = service(tmp_path, max_chars=20)
    with pytest.raises(TTSError):
        svc.synthesize("dm", "   ")
    with pytest.raises(TTSError):
        svc.synthesize("dm", "x" * 21)

    broken = service(tmp_path, FakePolly(fail=RuntimeError("InvalidSsmlException")))
    with pytest.raises(TTSError) as exc:
        broken.synthesize("dm", "Hello.")
    assert "InvalidSsmlException" in str(exc.value)

    silent = service(tmp_path, FakePolly(audio=b""))
    with pytest.raises(TTSError):
        silent.synthesize("dm", "Hello.")

    # Nothing failed is cached: the next attempt must be allowed to succeed.
    assert silent.cache.get(silent.cache_key_for("dm", "Hello.")[1]) is None


def test_without_a_client_there_are_no_server_voices(tmp_path):
    svc = PollyTTS(AudioCache(str(tmp_path), 0))
    svc._client_tried = True          # as if boto3 or the credentials were missing
    assert svc.available() is False
    with pytest.raises(TTSError):
        svc.synthesize("dm", "Hello.")
    assert svc.cast("dm").voice_id == "Brian"      # casting still works off the built-in roster


def test_the_engine_sets_the_price(tmp_path):
    assert service(tmp_path).price_of(1_000_000) == pytest.approx(4.0)
    assert service(tmp_path, engine="neural").price_of(1_000_000) == pytest.approx(16.0)


def test_from_env_says_no_where_narration_is_supposed_to_be_free(tmp_path, monkeypatch):
    monkeypatch.delenv("DND_TTS", raising=False)
    monkeypatch.delenv("DND_SIM_MOCK", raising=False)
    assert from_env(str(tmp_path)) is not None

    monkeypatch.setenv("DND_TTS", "0")
    assert from_env(str(tmp_path)) is None

    # Mock mode is the mode that costs nothing, so Polly stays out of it...
    monkeypatch.delenv("DND_TTS", raising=False)
    monkeypatch.setenv("DND_SIM_MOCK", "1")
    assert from_env(str(tmp_path)) is None
    # ...unless you say you meant it.
    monkeypatch.setenv("DND_TTS", "1")
    assert from_env(str(tmp_path)) is not None


def test_from_env_reads_the_knobs(tmp_path, monkeypatch):
    monkeypatch.delenv("DND_SIM_MOCK", raising=False)
    monkeypatch.setenv("DND_TTS_ENGINE", "neural")
    monkeypatch.setenv("DND_TTS_DM_VOICE", "Joanna")
    monkeypatch.setenv("DND_TTS_MAX_CHARS", "128")
    monkeypatch.setenv("DND_TTS_CACHE", str(tmp_path / "clips"))
    svc = from_env(str(tmp_path))
    assert svc.engine == "neural" and svc.dm_voice == "Joanna" and svc.max_chars == 128
    assert svc.cache.root.endswith("clips")

    # An engine with no price row would make the budget stop blind; it is not
    # honoured, and the default is.
    monkeypatch.setenv("DND_TTS_ENGINE", "whisper-shout")
    assert from_env(str(tmp_path)).engine == "standard"


class FakeSession:
    def __init__(self, creds, client="polly-client"):
        self._creds = creds
        self._client = client
        self.region = None

    def get_credentials(self):
        return self._creds

    def client(self, name):
        assert name == "polly"
        return self._client


def fake_boto3(session):
    import types

    mod = types.ModuleType("boto3")
    mod.session = types.SimpleNamespace(Session=lambda **kw: session)
    return mod


def test_credentials_are_resolved_before_claiming_to_be_available(tmp_path, monkeypatch):
    """boto3 builds a client with no credentials and only fails at the call.

    Taking that as "available" would make the page choose server voices and
    then discover, three failed lines in, that there are none.
    """
    svc = PollyTTS(AudioCache(str(tmp_path), 0), region="us-east-1")
    monkeypatch.setitem(__import__("sys").modules, "boto3", fake_boto3(FakeSession(None)))
    assert svc.available() is False

    ok = PollyTTS(AudioCache(str(tmp_path), 0), region="us-east-1")
    monkeypatch.setitem(
        __import__("sys").modules, "boto3", fake_boto3(FakeSession(object(), "client!"))
    )
    assert ok.available() is True and ok.client() == "client!"


def test_a_client_that_cannot_be_built_is_not_an_exception(tmp_path, monkeypatch):
    import types

    broken = types.ModuleType("boto3")

    def explode(**kwargs):
        raise RuntimeError("NoRegionError: you must specify a region")

    broken.session = types.SimpleNamespace(Session=explode)
    monkeypatch.setitem(__import__("sys").modules, "boto3", broken)
    svc = PollyTTS(AudioCache(str(tmp_path), 0))
    assert svc.available() is False
