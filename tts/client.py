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
from dataclasses import dataclass
from typing import Any

from tts.cache import AudioCache, cache_key
from tts.voices import STANDARD_ENGLISH, Cast, Voice, billable_chars, cast_for, ssml_for

__all__ = [
    "PRICE_USD_PER_MILLION_CHARS",
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

DEFAULT_ENGINE = "standard"
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
        language: str = DEFAULT_LANGUAGE,
        dm_voice: str = DEFAULT_DM_VOICE,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self.cache = cache
        self.engine = (engine or DEFAULT_ENGINE).strip().lower()
        self.language = (language or DEFAULT_LANGUAGE).strip()
        self.dm_voice = (dm_voice or "").strip()
        self.max_chars = int(max_chars)
        self.region = (region or "").strip()
        self._client = client
        self._client_tried = client is not None
        self._voices: tuple[Voice, ...] | None = None
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Lock] = {}

    # -- price ---------------------------------------------------------------

    @property
    def price_per_million(self) -> float:
        return PRICE_USD_PER_MILLION_CHARS.get(self.engine, PRICE_USD_PER_MILLION_CHARS["standard"])

    def price_of(self, chars: int) -> float:
        return max(0, int(chars)) * self.price_per_million / 1_000_000.0

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

    def voices(self) -> tuple[Voice, ...]:
        """The pool: every voice of this engine whose language matches.

        Asked of `DescribeVoices` once and remembered. `STANDARD_ENGLISH` is
        the fallback for a call that fails, so a network blip or an IAM policy
        without `polly:DescribeVoices` costs a possibly-stale roster rather
        than a silent narrator.
        """
        with self._lock:
            if self._voices is not None:
                return self._voices
        pool = self._describe() or self._fallback_pool()
        with self._lock:
            self._voices = pool
        return pool

    def _fallback_pool(self) -> tuple[Voice, ...]:
        prefix = self.language.split("-")[0].lower()
        pool = tuple(v for v in STANDARD_ENGLISH if v.language.lower().startswith(prefix))
        return pool or STANDARD_ENGLISH

    def _describe(self) -> tuple[Voice, ...]:
        client = self.client()
        if client is None:
            return ()
        prefix = self.language.split("-")[0].lower()
        want = self.engine
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
        """The seat `key` sits in. `gender` is the character's, where the game
        states one — it narrows the pool, it does not pick the voice."""
        return cast_for(key, self.voices(), self.dm_voice, gender)

    # -- synthesis -----------------------------------------------------------

    def cached(self, ckey: str) -> bytes | None:
        """A clip already paid for, or None. The caller checks this before the
        budget: a cache hit is not spend, so it is not refused for lack of it."""
        return self.cache.get(ckey)

    def ssml(self, text: str, cast: Cast) -> str:
        return ssml_for(text, cast, self.engine)

    def cache_key_for(self, key: str, text: str, gender: str = "") -> tuple[Cast, str]:
        # Keyed on the document that will actually be sent, not on the cast it
        # came from: an engine that drops pitch makes two casts that differ
        # only in pitch the same audio, and they should be the same file.
        cast = self.cast(key, gender)
        return cast, cache_key(self.engine, cast.voice_id, self.ssml(text, cast))

    def synthesize(self, key: str, text: str, gender: str = "") -> TTSResult:
        """Audio for one line in one seat. Raises `TTSError` if it cannot be had."""
        text = str(text or "").strip()
        if not text:
            raise TTSError("nothing to say")
        if len(text) > self.max_chars:
            raise TTSError(f"line is {len(text)} characters; the cap is {self.max_chars}")

        cast, ckey = self.cache_key_for(key, text, gender)
        hit = self.cache.get(ckey)
        if hit is not None:
            return TTSResult(hit, cast, 0, 0.0, True, ckey)

        # Two tabs asking for the same line at the same moment is otherwise
        # two identical Polly bills: the second asker waits on the first and
        # then finds the clip in the cache. Not a hard guarantee — a third
        # arriving in the instant the gate is retired makes its own — so the
        # cost of losing the race stays one duplicate clip, never a wrong one.
        with self._lock:
            gate = self._inflight.setdefault(ckey, threading.Lock())
        try:
            with gate:
                hit = self.cache.get(ckey)
                if hit is not None:
                    return TTSResult(hit, cast, 0, 0.0, True, ckey)
                audio = self._synthesize_now(text, cast)
                self.cache.put(ckey, audio)
        finally:
            # Whether it worked or not: a gate left behind is a slow leak of
            # one lock per distinct line the game ever speaks.
            with self._lock:
                self._inflight.pop(ckey, None)
        chars = billable_chars(text)
        return TTSResult(audio, cast, chars, self.price_of(chars), False, ckey)

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
                Engine=self.engine,
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
    engine = (os.environ.get("DND_TTS_ENGINE") or DEFAULT_ENGINE).strip().lower()
    if engine not in PRICE_USD_PER_MILLION_CHARS:
        log.warning("unknown DND_TTS_ENGINE %r; using %s", engine, DEFAULT_ENGINE)
        engine = DEFAULT_ENGINE
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
        language=os.environ.get("DND_TTS_LANG") or DEFAULT_LANGUAGE,
        dm_voice=os.environ.get("DND_TTS_DM_VOICE") or DEFAULT_DM_VOICE,
        max_chars=max_chars,
    )
