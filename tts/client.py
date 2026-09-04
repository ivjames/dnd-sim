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
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from tts.cache import AudioCache, cache_key
from tts.voices import (
    STANDARD_ENGLISH,
    Cast,
    Voice,
    billable_chars,
    cast_for,
    is_monster_key,
    source_fingerprint,
    ssml_for,
)

__all__ = [
    "PRICE_USD_PER_MILLION_CHARS",
    "DEFAULT_ENGINE",
    "DEFAULT_MONSTER_ENGINE",
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
#: The engine a speaking monster is cast on, whatever the table is using.
#: `<amazon:effect vocal-tract-length>` is standard-only, and it is the whole
#: reason a goblin and an ogre sound like different sizes rather than merely
#: different voices — TTS-COSTS.md §4 concluded the novelty-voiced monsters had
#: no vendor equivalent, and this is it. Set `DND_TTS_MONSTER_ENGINE` equal to
#: `DND_TTS_ENGINE` to put the whole table on one engine.
DEFAULT_MONSTER_ENGINE = "standard"
DEFAULT_LANGUAGE = "en-US"
DEFAULT_DM_VOICE = "Brian"
DEFAULT_MAX_CHARS = 400          # a `speech.js` chunk is capped at 220
DEFAULT_CACHE_MB = 512


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
        language: str = DEFAULT_LANGUAGE,
        dm_voice: str = DEFAULT_DM_VOICE,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self.cache = cache
        self.engine = (engine or DEFAULT_ENGINE).strip().lower()
        self.monster_engine = (monster_engine or self.engine).strip().lower()
        self.language = (language or DEFAULT_LANGUAGE).strip()
        self.dm_voice = (dm_voice or "").strip()
        self.max_chars = int(max_chars)
        self.region = (region or "").strip()
        self._client = client
        self._client_tried = client is not None
        self._voices: dict[str, tuple[Voice, ...]] = {}   # per engine
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
        """Which engine speaks this seat. A monster keeps the one that can
        still change its timbre; everyone else gets the table's."""
        return self.monster_engine if is_monster_key(key) else self.engine

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
        with self._lock:
            hit = self._voices.get(engine)
        if hit is not None:
            return hit
        pool = self._describe(engine) or self._fallback_pool(engine)
        with self._lock:
            self._voices[engine] = pool
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

    def cast(self, key: str, gender: str = "") -> Cast:
        """The seat `key` sits in, on the engine that will speak it.

        `gender` is the character's, where the game states one — it narrows the
        pool, it does not pick the voice.
        """
        engine = self.engine_for(key)
        return cast_for(key, self.voices(engine), self.dm_voice, gender, engine)

    # -- synthesis -----------------------------------------------------------

    def config_id(self) -> str:
        """A short token over everything process-level that changes a clip.

        The clip URL names the game, the seat and the words; it does not name
        the engine, the language, the DM's voice or the roster `DescribeVoices`
        returned, and those decide the audio too. The page carries this in the
        URL so that reconfiguring the server retires the browser's copies
        rather than leaving them to be replayed for a year.
        """
        parts = [self.language, self.dm_voice, source_fingerprint()]
        for engine in dict.fromkeys((self.engine, self.monster_engine)):
            parts += [engine, ",".join(v.id for v in self.voices(engine))]
        return cache_key(*parts)[:12]

    def cached(self, ckey: str) -> bytes | None:
        """A clip already paid for, or None. The caller checks this before the
        budget: a cache hit is not spend, so it is not refused for lack of it."""
        return self.cache.get(ckey)

    def ssml(self, text: str, cast: Cast) -> str:
        return ssml_for(text, cast)      # the engine rides on the cast

    def cache_key_for(self, key: str, text: str, gender: str = "") -> tuple[Cast, str]:
        # Keyed on the document that will actually be sent, not on the cast it
        # came from: an engine that drops pitch makes two casts that differ
        # only in pitch the same audio, and they should be the same file.
        cast = self.cast(key, gender)
        return cast, cache_key(cast.engine, cast.voice_id, self.ssml(text, cast))

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

    def render(self, key: str, text: str, gender: str = "") -> TTSResult:
        """Synthesize unconditionally, and cache it.

        No gate and no cache read: the caller holds `exclusive` and has already
        looked. Raises `TTSError` if it cannot be had.
        """
        text = self._check(text)
        cast, ckey = self.cache_key_for(key, text, gender)
        audio = self._synthesize_now(text, cast)
        self.cache.put(ckey, audio)
        chars = billable_chars(text)
        return TTSResult(audio, cast, chars, self.price_of(chars, cast.engine), False, ckey)

    def synthesize(self, key: str, text: str, gender: str = "") -> TTSResult:
        """Audio for one line in one seat, from the cache or from Polly."""
        text = self._check(text)
        cast, ckey = self.cache_key_for(key, text, gender)
        hit = self.cache.get(ckey)
        if hit is not None:
            return TTSResult(hit, cast, 0, 0.0, True, ckey)
        with self.exclusive(ckey):
            # Two tabs asking at the same moment is otherwise two identical
            # Polly bills: the second waits here and then finds the clip.
            hit = self.cache.get(ckey)
            if hit is not None:
                return TTSResult(hit, cast, 0, 0.0, True, ckey)
            return self.render(key, text, gender)

    def _synthesize_now(self, text: str, cast: Cast) -> bytes:
        client = self.client()
        if client is None:
            raise TTSError("no Polly client (boto3 missing, or no AWS credentials)")
        stream = None
        try:
            resp = client.synthesize_speech(
                Text=self.ssml(text, cast),
                TextType="ssml",
                VoiceId=cast.voice_id,
                Engine=cast.engine,
                OutputFormat="mp3",
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
        if name not in PRICE_USD_PER_MILLION_CHARS:
            log.warning("unknown %s %r; using %s", var, name, default)
            return default
        return name

    engine = _engine("DND_TTS_ENGINE", DEFAULT_ENGINE)
    monster_engine = _engine("DND_TTS_MONSTER_ENGINE", DEFAULT_MONSTER_ENGINE)
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
        language=os.environ.get("DND_TTS_LANG") or DEFAULT_LANGUAGE,
        dm_voice=os.environ.get("DND_TTS_DM_VOICE") or DEFAULT_DM_VOICE,
        max_chars=max_chars,
    )
