"""PollyTTS: what it asks AWS for, what it charges, and what it refuses.

Driven with a fake in place of the boto3 client, so every path here — a
successful synthesis, a cache hit, a roster that cannot be fetched, an AWS
error — runs without an AWS account and without spending anything.
"""

from __future__ import annotations

import io
import threading
import time

import pytest

from tts.cache import AudioCache
from tts.client import DEFAULT_ENGINE, PollyTTS, TTSError, from_env
from tts.voices import STANDARD_ENGLISH, Cast, ssml_for

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
    """A service pinned to `standard` unless a test says otherwise.

    The shipped default is neural for the whole table, monsters included (see
    `test_a_monster_stays_on_the_tables_engine_and_is_treated_afterwards`);
    these tests are about mechanics that do not depend on which, so they pin
    one rather than re-deriving it.
    """
    kw.setdefault("engine", "standard")
    kw.setdefault("monster_engine", "standard")
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
    svc = PollyTTS(AudioCache(str(tmp_path), 0), engine="standard", monster_engine="standard")
    svc._client_tried = True          # as if boto3 or the credentials were missing
    assert svc.available() is False
    with pytest.raises(TTSError):
        svc.synthesize("dm", "Hello.")
    assert svc.cast("dm").voice_id == "Brian"      # casting still works off the built-in roster

    # ...but only on standard, whose roster is the one built in. On any other
    # engine there is nothing to cast from and the page uses browser voices.
    neural = PollyTTS(AudioCache(str(tmp_path), 0), engine="neural")
    neural._client_tried = True
    assert neural.voices() == ()
    with pytest.raises(ValueError):
        neural.cast("dm")


def test_the_engine_sets_the_price(tmp_path):
    assert service(tmp_path).price_of(1_000_000) == pytest.approx(4.0)
    assert service(tmp_path, engine="neural").price_of(1_000_000) == pytest.approx(16.0)
    # And per seat, since the table and its monsters need not be on one engine.
    split = service(tmp_path, engine="neural", monster_engine="standard")
    assert split.price_of(1_000_000, split.engine_for("dm")) == pytest.approx(16.0)
    assert split.price_of(1_000_000, split.engine_for("monster:goblin_1")) == pytest.approx(4.0)


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
    # No split by default any more: the monsters are treated after synthesis,
    # so they stay wherever the table is.
    assert svc.monster_engine == "neural" and svc.monster_fx is True
    assert svc.cache.root.endswith("clips")

    # An engine with no price row would make the budget stop blind; it is not
    # honoured, and the default is.
    monkeypatch.setenv("DND_TTS_ENGINE", "whisper-shout")
    assert from_env(str(tmp_path)).engine == DEFAULT_ENGINE

    # One engine for the whole table is a supported choice, not a special case.
    monkeypatch.setenv("DND_TTS_ENGINE", "neural")
    monkeypatch.setenv("DND_TTS_MONSTER_ENGINE", "neural")
    one = from_env(str(tmp_path))
    assert one.engine_for("monster:goblin_1") == "neural"

    # Turning the treatment off puts the monsters back on the only engine that
    # can make one sound like a monster without it.
    monkeypatch.delenv("DND_TTS_MONSTER_ENGINE")
    monkeypatch.setenv("DND_TTS_MONSTER_FX", "0")
    off = from_env(str(tmp_path))
    assert off.monster_fx is False and off.engine_for("monster:goblin_1") == "standard"
    assert off.cast("monster:goblin_1").vtl_pct                     # and the SSML with it

    # A stated engine still wins over that.
    monkeypatch.setenv("DND_TTS_MONSTER_ENGINE", "neural")
    assert from_env(str(tmp_path)).engine_for("monster:goblin_1") == "neural"


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


def test_the_cache_key_is_the_document_that_will_be_sent(tmp_path):
    """Not the cast it came from.

    An engine that drops a tag makes two casts differing only in that tag the
    same audio, and they should be the same file rather than two identical ones
    bought separately.
    """
    from tts.cache import cache_key  # noqa: PLC0415

    voices = [{"Id": "Joanna", "LanguageCode": "en-US", "Gender": "Female",
               "SupportedEngines": ["standard", "neural"]},
              {"Id": "Brian", "LanguageCode": "en-GB", "Gender": "Male",
               "SupportedEngines": ["standard", "neural"]}]
    neural = service(tmp_path, FakePolly(voices=voices), engine="neural")
    std = service(tmp_path, FakePolly(voices=voices))

    # The key is built from the CAST's engine, which for a monster is the one
    # that can still change its timbre rather than the table's.
    cast, ckey = neural.cache_key_for("monster:goblin_1", "Fee fi.")
    assert cast.engine == "standard"
    # Plus the treatment, which the document does not describe: two monsters
    # can share a voice and a rate and differ entirely in what is done to the
    # samples afterwards.
    assert ckey == cache_key(cast.engine, cast.voice_id, neural.ssml("Fee fi.", cast),
                             cast.fx.token())
    assert ckey == std.cache_key_for("monster:goblin_1", "Fee fi.")[1]   # same seat, same clip

    # A seat that does follow the table's engine keys differently under each.
    assert neural.cache_key_for("dm", "Fee fi.")[1] != std.cache_key_for("dm", "Fee fi.")[1]

    # An untreated seat keys on exactly what it always did — the token is empty
    # — so the table's clips are not orphaned by any of this.
    dm, dkey = neural.cache_key_for("dm", "Fee fi.")
    assert dm.fx is None
    assert dkey == cache_key(dm.engine, dm.voice_id, neural.ssml("Fee fi.", dm), "")

    # Two casts that this engine renders identically ARE the same clip.
    a = Cast("pc_1", "Joanna", "en-US", "neural", pitch_pct=-10)
    b = Cast("npc", "Joanna", "en-US", "neural", pitch_pct=10)
    assert neural.ssml("Fee fi.", a) == neural.ssml("Fee fi.", b)
    on_standard = [Cast(c.key, c.voice_id, c.language, "standard", pitch_pct=c.pitch_pct)
                   for c in (a, b)]
    assert std.ssml("Fee fi.", on_standard[0]) != std.ssml("Fee fi.", on_standard[1])


def test_only_what_the_engine_accepts_goes_on_the_wire(tmp_path):
    """Polly errors on an unsupported tag, so this is the difference between a
    working neural game and one that 502s every monster's line."""
    fake = FakePolly()
    # The whole table on neural, monsters included — the point here is what the
    # engine accepts, not the per-seat split.
    neural = service(tmp_path, fake, engine="neural", monster_engine="neural")
    neural.synthesize("monster:goblin_1", "Fee fi.")
    sent = fake.calls[0]["Text"]
    assert "vocal-tract-length" not in sent and "pitch=" not in sent
    assert sent.startswith("<speak>")


def test_the_fingerprint_moves_when_the_casting_code_does(tmp_path, monkeypatch):
    """Engine, language, DM voice and roster do not describe a clip on their
    own: `cast_for` and `ssml_for` decide the audio too, and a deployment that
    changes them leaves every browser replaying year-long-immutable copies of
    the old casting."""
    from tts import voices

    svc = PollyTTS(AudioCache(str(tmp_path), 0), client=FakePolly())
    before = svc.config_id()

    monkeypatch.setattr(voices, "_SOURCE_FP", "deadbeef")
    after = PollyTTS(AudioCache(str(tmp_path), 0), client=FakePolly()).config_id()
    assert after != before

    # It is a digest of the module, so it is stable across processes rather
    # than random per run.
    monkeypatch.setattr(voices, "_SOURCE_FP", None)
    assert PollyTTS(AudioCache(str(tmp_path), 0), client=FakePolly()).config_id() == before


def test_a_monster_stays_on_the_tables_engine_and_is_treated_afterwards(tmp_path):
    """The shipped default. A monster is not a worse-sounding engine any more:
    it is the same voice as everyone else with `tts/dsp.py` applied to it, so
    the whole table is one production."""
    svc = PollyTTS(AudioCache(str(tmp_path), 0), client=FakePolly(), engine="neural")

    for key in ("dm", "pc_1", "npc", "monster:goblin_1"):
        assert svc.engine_for(key) == "neural"

    goblin = svc.cast("monster:goblin_1")
    assert goblin.engine == "neural" and goblin.fx and goblin.vtl_pct == 0
    assert "vocal-tract-length" not in svc.ssml("Fee fi.", goblin)
    assert svc.media_type_for(goblin) == "audio/wav"
    assert svc.media_type_for(svc.cast("dm")) == "audio/mpeg"

    # One engine on the wire, and a monster asks for the one format that can be
    # worked on without a codec.
    assert svc.render("monster:goblin_1", "Fee fi.").media_type == "audio/wav"
    assert svc.render("dm", "Narration.").media_type == "audio/mpeg"
    calls = svc.client().calls
    assert {c["Engine"] for c in calls} == {"neural"}
    assert [c["OutputFormat"] for c in calls] == ["pcm", "mp3"]
    assert calls[0]["SampleRate"] == "16000" and "SampleRate" not in calls[1]

    # Which means one rate on the bill, where the split crossed two.
    assert svc.render("monster:ogre_1", "x" * 100).usd == pytest.approx(16.0 / 10_000)


def test_the_old_split_is_what_turning_the_treatment_off_restores(tmp_path):
    """`DND_TTS_MONSTER_FX=0`: the table on neural for the voices, monsters on
    standard because `vocal-tract-length` is the only vendor equivalent there
    is for a novelty voice, and it exists on no other engine."""
    svc = PollyTTS(AudioCache(str(tmp_path), 0), client=FakePolly(),
                   engine="neural", monster_engine="standard", monster_fx=False)

    assert svc.engine_for("dm") == "neural"
    assert svc.engine_for("pc_1") == "neural"
    assert svc.engine_for("npc") == "neural"
    assert svc.engine_for("monster:goblin_1") == "standard"

    goblin = svc.cast("monster:goblin_1")
    assert goblin.engine == "standard" and goblin.vtl_pct and goblin.fx is None
    assert "vocal-tract-length" in svc.ssml("Fee fi.", goblin)
    assert svc.media_type_for(goblin) == "audio/mpeg"       # no treatment, no wrapper

    dm = svc.cast("dm")
    assert dm.engine == "neural"
    assert "vocal-tract-length" not in svc.ssml("Narration.", dm)

    # Each seat is billed at its own engine's rate.
    assert svc.render("monster:goblin_1", "x" * 100).usd == pytest.approx(4.0 / 10_000)
    assert svc.render("dm", "y" * 100).usd == pytest.approx(16.0 / 10_000)

    sent = {call["VoiceId"]: call["Engine"] for call in svc.client().calls}
    assert set(sent.values()) == {"standard", "neural"}


def test_the_two_engines_have_their_own_rosters(tmp_path):
    """A voice must not be cast from one engine's list and sent to another."""
    fake = FakePolly(voices=[
        {"Id": "Joanna", "LanguageCode": "en-US", "Gender": "Female",
         "SupportedEngines": ["standard", "neural"]},
        {"Id": "Geraint", "LanguageCode": "en-GB-WLS", "Gender": "Male",
         "SupportedEngines": ["standard"]},          # standard only
        {"Id": "Arthur", "LanguageCode": "en-GB", "Gender": "Male",
         "SupportedEngines": ["neural"]},            # neural only
    ])
    svc = PollyTTS(AudioCache(str(tmp_path), 0), client=fake,
                   engine="neural", monster_engine="standard")

    assert {v.id for v in svc.voices("standard")} == {"Joanna", "Geraint"}
    assert {v.id for v in svc.voices("neural")} == {"Joanna", "Arthur"}
    assert fake.described == 2                        # asked once per engine, then remembered
    svc.voices("neural")
    assert fake.described == 2

    assert svc.cast("monster:goblin_1").voice_id in {"Joanna", "Geraint"}
    assert svc.cast("pc_1").voice_id in {"Joanna", "Arthur"}


def test_no_roster_beats_the_wrong_one(tmp_path):
    """The built-in roster is standard's AND English's.

    Reading French with an English voice is not a degraded narrator, it is a
    wrong one — and the capability probe would report it as working.
    """
    svc = PollyTTS(AudioCache(str(tmp_path), 0), engine="standard",
                   monster_engine="standard", language="fr-FR")
    svc._client_tried = True                      # DescribeVoices cannot be reached
    assert svc.voices() == ()

    english = PollyTTS(AudioCache(str(tmp_path), 0), engine="standard",
                       monster_engine="standard", language="en-GB")
    english._client_tried = True
    assert {v.language for v in english.voices()} <= {
        "en-GB", "en-GB-WLS", "en-US", "en-AU", "en-IN"}
    assert english.voices()


def test_the_gate_outlives_everyone_queued_on_it(tmp_path):
    """Retiring a gate while a queued holder still owns that lock lets the next
    arrival mint a second one and render alongside them — a duplicate charge,
    or a 402 that ends server voices for the game.

    The window only opens for a request that arrives AFTER the entry is
    retired: threads that queued up beforehand all hold the same lock object
    and are safe either way. So this sequences the arrivals rather than
    starting them together.
    """
    svc = service(tmp_path)
    inside, overlaps, guard = [], [], threading.Lock()
    a_in, a_go, b_in = (threading.Event() for _ in range(3))

    def body(name, entered=None, release=None, hold=0.0):
        with svc.exclusive("same-line"):
            with guard:
                inside.append(name)
                if len(inside) > 1:
                    overlaps.append(sorted(inside))
            if entered is not None:
                entered.set()
            if release is not None:
                release.wait(timeout=5)
            elif hold:
                time.sleep(hold)
            with guard:
                inside.remove(name)

    a = threading.Thread(target=body, args=("a",), kwargs={"entered": a_in, "release": a_go})
    b = threading.Thread(target=body, args=("b",), kwargs={"entered": b_in, "hold": 0.3})
    a.start()
    assert a_in.wait(timeout=5)
    b.start()
    time.sleep(0.05)              # b is now queued on a's gate
    a_go.set()                    # a leaves, and retires the entry if it may
    assert b_in.wait(timeout=5)   # b is inside, still holding that same lock

    c = threading.Thread(target=body, args=("c",), kwargs={"hold": 0.05})
    c.start()                     # arrives after the retirement: mints its own?
    for t in (a, b, c):
        t.join(timeout=10)

    assert overlaps == [], f"two holders at once: {overlaps}"
    assert svc._inflight == {}     # and nothing is left behind


def test_an_empty_roster_is_not_believed_forever(tmp_path):
    """A transiently failed `DescribeVoices` must not switch narration off for
    the life of the process. A roster that answers is cached for good."""
    fake = FakePolly(voices=RuntimeError("ThrottlingException"))
    svc = PollyTTS(AudioCache(str(tmp_path), 0), client=fake, engine="neural")

    assert svc.voices() == ()
    assert svc.voices() == ()
    assert fake.described == 1                  # believed, for a while

    pool, until = svc._voices["neural"]
    assert until is not None                    # ...but with a deadline on it
    svc._voices["neural"] = (pool, time.monotonic() - 1)     # which now passes

    fake._voices = [{"Id": "Arthur", "LanguageCode": "en-GB", "Gender": "Male",
                     "SupportedEngines": ["neural"]}]
    assert [v.id for v in svc.voices()] == ["Arthur"]
    assert fake.described == 2                  # asked again, and got an answer

    assert svc._voices["neural"][1] is None     # a real roster has no deadline
    svc.voices()
    assert fake.described == 2
