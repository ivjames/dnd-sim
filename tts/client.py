"""Amazon Polly, and the accounting that comes with it.

Narration used to be free: `speechSynthesis` in the spectator's own browser,
no key, no bytes leaving the device, and whatever voice the OS shipped. Polly
is better and costs money, so everything here is arranged around the second
fact — the standard engine at $4.00 per million characters (read 2026-09-04
from https://aws.amazon.com/polly/pricing/), every clip cached on disk so a
line is paid for once, and the spend charged to the game's own budget.

Layering: `web → tts`, and `tts` imports nothing from the rest of the app.
It is a sibling of `llm/` — an outside service with a price list — rather than
something under it, because narration is not the table talking.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from tts.cache import AudioCache, cache_key
from tts.dsp import SAMPLE_RATE
from tts.dsp import apply as apply_fx
from tts.dsp import wav
from tts.voices import (
    STANDARD_ENGLISH,
    Cast,
    Voice,
    billable_chars,
    cast_for,
    is_monster_key,
    source_fingerprint,
    Tune,
    retune,
    ssml_for,
)

__all__ = [
    "PRICE_USD_PER_MILLION_CHARS",
    "DEFAULT_ENGINE",
    "DEFAULT_MONSTER_ENGINE",
    "DEFAULT_MONSTER_FX",
    "MPEG",
    "WAVE",
    "TTSError",
    "TTSResult",
    "PollyTTS",
    "from_env",
    "env_flag",
]

log = logging.getLogger("dnd-sim.tts")

#: $ per million *billed* characters, by Polly engine. SSML tags are not
#: billed (https://docs.aws.amazon.com/polly/latest/dg/limits.html), so the
#: count is the plain line — see `voices.billable_chars`.
PRICE_USD_PER_MILLION_CHARS: dict[str, float] = {
    "standard": 4.0,
    "neural": 16.0,
    "long-form": 100.0,
    "generative": 30.0,
}

DEFAULT_ENGINE = "neural"
#: The engine a speaking monster is cast on. Empty means "the table's", which
#: is the default now that the size shift is ours to do (`tts/dsp.py`).
#:
#: It used to be `standard`, unconditionally, and for one reason:
#: `<amazon:effect vocal-tract-length>` exists on no other engine. That bought
#: a goblin and an ogre sounding like different sizes at the price of every
#: monster line being read by the older, flatter engine — audibly a different
#: production from the narrator speaking over it. Post-processing buys the same
#: distinction without the engine, so a monster stays where the table is.
#:
#: `DND_TTS_MONSTER_ENGINE` still names an engine if a deployment wants the
#: split for its own reasons, and `DND_TTS_MONSTER_FX=0` restores the whole old
#: arrangement — that engine AND the SSML that needed it.
DEFAULT_MONSTER_ENGINE = ""
#: Whether a monster is made to sound like one after synthesis (`tts/dsp.py`)
#: rather than by standard-only SSML. `DND_TTS_MONSTER_FX=0` says no.
DEFAULT_MONSTER_FX = True
#: What a clip is served as. A treated monster is a WAV because the treatment
#: happens between Polly and the listener and an MP3 would have to be
#: re-encoded to get back; everything else is Polly's own MP3, untouched.
MPEG = "audio/mpeg"
WAVE = "audio/wav"
DEFAULT_LANGUAGE = "en-US"
DEFAULT_DM_VOICE = "Brian"
DEFAULT_MAX_CHARS = 400          # a `speech.js` chunk is capped at 220
DEFAULT_CACHE_MB = 512
#: How long an EMPTY roster is believed. A `DescribeVoices` that fails
#: transiently — a blip, an IAM change being rolled out — would otherwise be
#: cached for the life of the process and leave narration off long after the
#: outage cleared. A roster that comes back is cached for good.
EMPTY_ROSTER_TTL = 60.0


class TTSError(RuntimeError):
    """Synthesis failed. The caller falls back to the browser's own voices."""


@dataclass
class TTSResult:
    audio: bytes
    cast: Cast
    chars: int          # billed characters; 0 on a cache hit, which costs nothing
    usd: float
    cached: bool
    key: str            # the cache key, which doubles as a stable ETag
    media_type: str = MPEG      # WAVE for a treated monster; see `media_type_for`


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class PollyTTS:
    """Synthesize a line, cached, and say what it cost.

    `client` is a boto3 Polly client; it is injectable so the tests can drive
    every path here without an AWS account. Left None, one is built lazily on
    first use — so importing this module costs nothing and a missing boto3 or
    missing credentials surfaces as `available() is False` rather than an
    import error at start-up.
    """

    def __init__(
        self,
        cache: AudioCache,
        *,
        client: Any = None,
        region: str = "",
        engine: str = DEFAULT_ENGINE,
        monster_engine: str = DEFAULT_MONSTER_ENGINE,
        monster_fx: bool = DEFAULT_MONSTER_FX,
        language: str = DEFAULT_LANGUAGE,
        dm_voice: str = DEFAULT_DM_VOICE,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self.cache = cache
        self.engine = (engine or DEFAULT_ENGINE).strip().lower()
        self.monster_engine = (monster_engine or self.engine).strip().lower()
        self.monster_fx = bool(monster_fx)
        self.language = (language or DEFAULT_LANGUAGE).strip()
        self.dm_voice = (dm_voice or "").strip()
        self.max_chars = int(max_chars)
        self.region = (region or "").strip()
        self._client = client
        self._client_tried = client is not None
        # per engine: the pool, and when to stop believing it (None = never,
        # which is what a non-empty roster gets)
        self._voices: dict[str, tuple[tuple[Voice, ...], float | None]] = {}
        self._lock = threading.Lock()
        self._inflight: dict[str, tuple[threading.Lock, int]] = {}   # gate, holders+waiters

    # -- price ---------------------------------------------------------------

    @property
    def price_per_million(self) -> float:
        return self.rate_for(self.engine)

    def rate_for(self, engine: str) -> float:
        return PRICE_USD_PER_MILLION_CHARS.get(
            str(engine or self.engine), PRICE_USD_PER_MILLION_CHARS["standard"]
        )

    def price_of(self, chars: int, engine: str = "") -> float:
        return max(0, int(chars)) * self.rate_for(engine or self.engine) / 1_000_000.0

    def engine_for(self, key: str) -> str:
        """Which engine speaks this seat.

        The table's, for everyone, unless a deployment has split them —
        `DND_TTS_MONSTER_ENGINE`, which `DND_TTS_MONSTER_FX=0` sets to
        `standard` because the SSML it goes back to exists nowhere else.
        """
        return self.monster_engine if is_monster_key(key) else self.engine

    def media_type_for(self, cast: Cast) -> str:
        """What a clip for this cast is. Decided by the cast rather than by the
        bytes, so a cache hit is served with the same header the synthesis
        would have carried."""
        return WAVE if cast.fx else MPEG

    # -- the AWS client ------------------------------------------------------

    def client(self) -> Any:
        """The boto3 Polly client, or None if there is not one to be had."""
        if self._client is not None or self._client_tried:
            return self._client
        self._client_tried = True
        self._client = self._build_client()
        return self._client

    def _build_client(self) -> Any:
        try:
            import boto3  # noqa: PLC0415
        except ImportError:
            log.info("boto3 not installed; server voices off")
            return None
        try:
            session = boto3.session.Session(region_name=self.region or None)
            # boto3 builds a client perfectly happily with no credentials at
            # all — it only finds out at the first call. That would make the
            # capability probe a lie: the page would choose server voices and
            # then fall back three failed lines later. Resolve the chain now,
            # so "available" means a clip can actually be had.
            if session.get_credentials() is None:
                log.info("no AWS credentials found; server voices off")
                return None
            return session.client("polly")
        except Exception as exc:  # no region, a bad profile, IMDS unreachable
            log.info("polly client unavailable (%s); server voices off", exc)
            return None

    def available(self) -> bool:
        return self.client() is not None

    # -- casting -------------------------------------------------------------

    def voices(self, engine: str = "") -> tuple[Voice, ...]:
        """The pool: every voice of this engine whose language matches.

        Asked of `DescribeVoices` once and remembered. `STANDARD_ENGLISH` is
        the fallback for a call that fails, so a network blip or an IAM policy
        without `polly:DescribeVoices` costs a possibly-stale roster rather
        than a silent narrator.
        """
        engine = str(engine or self.engine).strip().lower()
        now = time.monotonic()
        with self._lock:
            hit = self._voices.get(engine)
            if hit is not None:
                pool, until = hit
                if until is None or now < until:
                    return pool
        pool = self._describe(engine) or self._fallback_pool(engine)
        with self._lock:
            # An empty pool is not an answer, it is the absence of one: believe
            # it only briefly, so the next probe after the outage clears finds
            # the voices rather than the memory of not finding them.
            self._voices[engine] = (pool, None if pool else now + EMPTY_ROSTER_TTL)
        return pool

    def _fallback_pool(self, engine: str = "") -> tuple[Voice, ...]:
        """The built-in roster, which is exactly what its name says.

        `STANDARD_ENGLISH` is the voices Polly serves on the STANDARD engine.
        Handing one to a neural request casts a voice that engine does not
        have, and Polly answers with an error — the same repeated 502 that
        engine-aware SSML exists to avoid. On any other engine a failed
        `DescribeVoices` leaves no roster worth vouching for, so this reports
        none and the page uses the browser's voices instead of hearing nothing.
        """
        if str(engine or self.engine) != "standard":
            return ()
        prefix = self.language.split("-")[0].lower()
        # And English, which is the other half of what its name says. Reading
        # French with an English voice is not a degraded narrator, it is a
        # wrong one — and the capability probe would report it as working. No
        # roster is the honest answer, as it is for the other engines.
        return tuple(v for v in STANDARD_ENGLISH if v.language.lower().startswith(prefix))

    def _describe(self, engine: str = "") -> tuple[Voice, ...]:
        client = self.client()
        if client is None:
            return ()
        prefix = self.language.split("-")[0].lower()
        want = str(engine or self.engine).strip().lower()
        out: list[Voice] = []
        try:
            token = None
            while True:
                kwargs = {"NextToken": token} if token else {}
                resp = client.describe_voices(**kwargs) or {}
                for row in resp.get("Voices") or []:
                    engines = [str(e).lower() for e in (row.get("SupportedEngines") or [])]
                    lang = str(row.get("LanguageCode") or "")
                    if want in engines and lang.lower().startswith(prefix):
                        out.append(Voice.from_api(row))
                token = resp.get("NextToken")
                if not token:
                    break
        except Exception as exc:  # pragma: no cover - network/permissions
            log.info("describe_voices failed (%s); using the built-in roster", exc)
            return ()
        return tuple(out)

    def cast(self, key: str, gender: str = "", age="", *, size="",
             tune: Tune | None = None) -> Cast:
        """The seat `key` sits in, on the engine that will speak it.

        `gender` and `age` come from the character, where the game states them
        — they narrow the pool, they do not pick the voice. The first is the
        constraint the character's `pronouns` name (`gender_for_pronouns`, and
        the older `gender` key it replaced), not a fact about the character. An
        unstated age is an adult, so Polly's children's voices are dealt only
        where a character asks for one.

        `size` is the creature's SRD size and applies to monsters alone: it
        picks the band the size shift is dealt from, so an ogre is bigger than
        a goblin rather than merely later in the initiative order. Keyword-only
        because it is not a trait of the same kind as the other two — it
        narrows no pool, it sets the treatment.

        `tune` is the listener's own override for this seat, applied last and
        against this engine's roster (`retune`): the casting decides what a
        seat sounds like until somebody says otherwise.
        """
        engine = self.engine_for(key)
        pool = self.voices(engine)
        return retune(
            cast_for(key, pool, self.dm_voice, gender, engine, age,
                     monster_fx=self.monster_fx, size=size),
            tune,
            pool,
        )

    # -- synthesis -----------------------------------------------------------

    def config_id(self) -> str:
        """A short token over everything process-level that changes a clip.

        The clip URL names the game, the seat and the words; it does not name
        the engine, the language, the DM's voice or the roster `DescribeVoices`
        returned, and those decide the audio too. The page carries this in the
        URL so that reconfiguring the server retires the browser's copies
        rather than leaving them to be replayed for a year.
        """
        # `monster_fx` is process-level and changes a monster's clip without
        # changing anything else the URL names, exactly like the engine.
        parts = [self.language, self.dm_voice, source_fingerprint(),
                 "fx" if self.monster_fx else "ssml"]
        for engine in dict.fromkeys((self.engine, self.monster_engine)):
            parts += [engine, ",".join(v.id for v in self.voices(engine))]
        return cache_key(*parts)[:12]

    def cached(self, ckey: str) -> bytes | None:
        """A clip already paid for, or None. The caller checks this before the
        budget: a cache hit is not spend, so it is not refused for lack of it."""
        return self.cache.get(ckey)

    def ssml(self, text: str, cast: Cast) -> str:
        return ssml_for(text, cast)      # the engine rides on the cast

    def cache_key_for(self, key: str, text: str, gender: str = "",
                      age="", *, size="", tune: Tune | None = None) -> tuple[Cast, str]:
        # Keyed on the document that will actually be sent, not on the cast it
        # came from: an engine that drops pitch makes two casts that differ
        # only in pitch the same audio, and they should be the same file.
        cast = self.cast(key, gender, age, size=size, tune=tune)
        # Plus the treatment, which is the half of the audio the document does
        # not describe: two monsters can be dealt one voice and one rate and
        # differ entirely in what happens to the samples afterwards.
        #
        # APPENDED, not passed as an empty string. `cache_key` writes a NUL
        # after every part it is given, so a fourth empty part is a different
        # digest from three parts — which would have re-keyed every DM, PC and
        # NPC clip on a deployed cache and made the whole table pay for its
        # narration a second time (and answered 402 for a game already at its
        # budget, where a cache hit is served whatever the budget says). An
        # untreated seat keys on exactly what it always did.
        parts = [cast.engine, cast.voice_id, self.ssml(text, cast)]
        if cast.fx:
            parts.append(cast.fx.token())
        return cast, cache_key(*parts)

    def _check(self, text: str) -> str:
        text = str(text or "").strip()
        if not text:
            raise TTSError("nothing to say")
        if len(text) > self.max_chars:
            raise TTSError(f"line is {len(text)} characters; the cap is {self.max_chars}")
        return text

    @contextmanager
    def exclusive(self, ckey: str):
        """Hold the one-synthesis-per-line gate for `ckey`.

        Exposed because the caller has to do more inside it than synthesize:
        the web layer checks the cache and reserves budget in here, so that two
        tabs asking for the SAME line cannot each reserve the cost of it and
        have the second refused for a clip the first is about to make free.
        """
        with self._lock:
            gate, waiting = self._inflight.get(ckey) or (threading.Lock(), 0)
            self._inflight[ckey] = (gate, waiting + 1)
        try:
            with gate:
                yield
        finally:
            # Reference-counted, not simply popped. Retiring the entry while a
            # queued holder still owns that lock lets the next arrival mint a
            # SECOND lock and render alongside them — which, now that the
            # budget reservation happens in here, is a duplicate charge or a
            # 402 that ends server voices for the game. It still cannot be left
            # behind: one lock per distinct line ever spoken is a slow leak.
            with self._lock:
                gate_now, waiting = self._inflight.get(ckey) or (gate, 1)
                if waiting <= 1:
                    self._inflight.pop(ckey, None)
                else:
                    self._inflight[ckey] = (gate_now, waiting - 1)

    def render(self, key: str, text: str, gender: str = "", age="", *,
               size="", tune: Tune | None = None) -> TTSResult:
        """Synthesize unconditionally, and cache it.

        No gate and no cache read: the caller holds `exclusive` and has already
        looked. Raises `TTSError` if it cannot be had.
        """
        text = self._check(text)
        cast, ckey = self.cache_key_for(key, text, gender, age, size=size, tune=tune)
        audio = self._synthesize_now(text, cast)
        self.cache.put(ckey, audio)
        chars = billable_chars(text)
        return TTSResult(audio, cast, chars, self.price_of(chars, cast.engine), False, ckey,
                         self.media_type_for(cast))

    def synthesize(self, key: str, text: str, gender: str = "", age="", *,
                   size="", tune: Tune | None = None) -> TTSResult:
        """Audio for one line in one seat, from the cache or from Polly."""
        text = self._check(text)
        cast, ckey = self.cache_key_for(key, text, gender, age, size=size, tune=tune)
        hit = self.cache.get(ckey)
        if hit is not None:
            return TTSResult(hit, cast, 0, 0.0, True, ckey, self.media_type_for(cast))
        with self.exclusive(ckey):
            # Two tabs asking at the same moment is otherwise two identical
            # Polly bills: the second waits here and then finds the clip.
            hit = self.cache.get(ckey)
            if hit is not None:
                return TTSResult(hit, cast, 0, 0.0, True, ckey, self.media_type_for(cast))
            return self.render(key, text, gender, age, size=size, tune=tune)

    def _synthesize_now(self, text: str, cast: Cast) -> bytes:
        """The one request, and — for a monster — what happens to what comes back.

        A treated cast asks for `pcm` instead of `mp3`, because `pcm` is the
        one format that can be worked on without a codec: "signed 16-bit, 1
        channel (mono), little-endian", 8000 or 16000 Hz
        (API_SynthesizeSpeech.html, read 2026-09-04). The treatment then hands
        back samples and the rate to play them at, and the WAV header carries
        both. Nobody else pays for any of this: an untreated seat takes the
        same MP3 path it always has, bytes untouched.
        """
        client = self.client()
        if client is None:
            raise TTSError("no Polly client (boto3 missing, or no AWS credentials)")
        # `bool(cast.fx)`, not `is not None`: a treatment whose every knob is 0
        # IS the plain voice, and `MonsterFX` says so — "a seat with no
        # treatment must key, and sound, exactly like the plain voice it is".
        # `media_type_for` and `cache_key_for` have always read it that way, and
        # this line reading it the other way was a real disagreement rather than
        # a stylistic one: a listener who dragged a monster's three sliders to 0
        # got PCM wrapped in a WAV, a header saying `audio/mpeg` because
        # `media_type_for` disagreed, and those bytes filed under the key an
        # untreated seat on the same voice and line computes.
        pcm = bool(cast.fx)
        stream = None
        try:
            resp = client.synthesize_speech(
                Text=self.ssml(text, cast),
                TextType="ssml",
                VoiceId=cast.voice_id,
                Engine=cast.engine,
                OutputFormat="pcm" if pcm else "mp3",
                **({"SampleRate": str(SAMPLE_RATE)} if pcm else {}),
            )
            stream = (resp or {}).get("AudioStream")
            audio = stream.read() if stream is not None else b""
        except Exception as exc:
            raise TTSError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - defensive
                    pass
        if not audio:
            raise TTSError("Polly returned no audio")
        if pcm:
            treated, rate = apply_fx(audio, cast.fx)
            return wav(treated, rate)
        return audio


def from_env(cache_dir: str) -> PollyTTS | None:
    """Build the service from the process environment, or None if it is off.

    Off means: `DND_TTS=0`, or the game is running in mock mode without an
    explicit `DND_TTS=1`. Mock mode is the "costs nothing" mode (CLAUDE.md) and
    Polly is not free, so it stays out of the way there unless asked for.
    Credentials come from wherever boto3 finds them — `.env` on the droplet,
    an instance profile anywhere else.
    """
    asked = os.environ.get("DND_TTS", "").strip()
    if not env_flag("DND_TTS", True):
        return None
    # Mock mode is the mode that costs nothing (CLAUDE.md), and Polly is not
    # free. `DND_TTS=1` is how you say you meant it.
    if not asked and env_flag("DND_SIM_MOCK"):
        return None
    def _engine(var: str, default: str) -> str:
        name = (os.environ.get(var) or default).strip().lower()
        if not name:
            return ""      # unset and no default: whatever the table is using
        if name not in PRICE_USD_PER_MILLION_CHARS:
            log.warning("unknown %s %r; using %s", var, name, default or "the table's engine")
            return default
        return name

    monster_fx = env_flag("DND_TTS_MONSTER_FX", DEFAULT_MONSTER_FX)
    engine = _engine("DND_TTS_ENGINE", DEFAULT_ENGINE)
    # Without the post-processing there is only one engine that can make a
    # monster sound like one, so turning it off moves the monsters back to it.
    # A stated `DND_TTS_MONSTER_ENGINE` still wins over both defaults.
    monster_engine = _engine(
        "DND_TTS_MONSTER_ENGINE", DEFAULT_MONSTER_ENGINE if monster_fx else "standard"
    )
    try:
        cache_mb = float(os.environ.get("DND_TTS_CACHE_MB") or DEFAULT_CACHE_MB)
    except ValueError:
        cache_mb = DEFAULT_CACHE_MB
    try:
        max_chars = int(os.environ.get("DND_TTS_MAX_CHARS") or DEFAULT_MAX_CHARS)
    except ValueError:
        max_chars = DEFAULT_MAX_CHARS
    return PollyTTS(
        AudioCache(os.environ.get("DND_TTS_CACHE") or cache_dir, int(cache_mb * 1024 * 1024)),
        region=os.environ.get("DND_TTS_REGION") or os.environ.get("AWS_REGION") or "",
        engine=engine,
        monster_engine=monster_engine,
        monster_fx=monster_fx,
        language=os.environ.get("DND_TTS_LANG") or DEFAULT_LANGUAGE,
        dm_voice=os.environ.get("DND_TTS_DM_VOICE") or DEFAULT_DM_VOICE,
        max_chars=max_chars,
    )
