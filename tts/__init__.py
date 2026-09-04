"""Server-rendered narration (Amazon Polly).

`web` builds one `PollyTTS` per process and asks it for a clip per line; the
browser plays the audio and falls back to its own `speechSynthesis` voices
whenever this layer says no. Nothing here imports `web`, `orchestrator`,
`agents` or `llm`.
"""

from tts.cache import AudioCache, cache_key
from tts.client import (
    PRICE_USD_PER_MILLION_CHARS,
    PollyTTS,
    TTSError,
    TTSResult,
    from_env,
)
from tts.voices import (
    Cast,
    Voice,
    billable_chars,
    cast_for,
    hash_key,
    source_fingerprint,
    ssml_for,
)

__all__ = [
    "AudioCache",
    "Cast",
    "PRICE_USD_PER_MILLION_CHARS",
    "PollyTTS",
    "TTSError",
    "TTSResult",
    "Voice",
    "billable_chars",
    "cache_key",
    "cast_for",
    "from_env",
    "hash_key",
    "source_fingerprint",
    "ssml_for",
]
