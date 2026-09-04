"""Casting for server-rendered narration: which Polly voice says a line, and
the SSML it says it in.

Pure — no boto3, no network, no filesystem. Given a voice key and a pool of
voices this decides everything about how the line sounds, so the casting rules
can be tested without an AWS account (`tests/test_tts_voices.py`).

The voice keys are the browser's: `web/static/speech.js: voiceKeyFor` decides
who is speaking (`dm`, a party member's own id, `npc`, or `monster:<id>`) and
sends the key with the line. The hash below is the same FNV-1a the browser
uses, so a given actor lands on the same seat every time.

Two things differ from the browser's casting, both because Polly is not a
device voice list:

  * There are no novelty voices. `speech.js` casts a speaking monster out of
    Bubbles and Zarvox where the device has them; here a monster gets an
    ordinary voice put through `<amazon:effect vocal-tract-length>`, which
    changes the timbre rather than the pitch — a longer vocal tract is a bigger
    creature. That effect is standard-engine only, which is part of why the
    standard engine is the one this runs on.
  * The pool is the same for everyone. The browser has to keep novelty voices
    away from narration; here every voice can carry a line.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Voice",
    "Cast",
    "STANDARD_ENGLISH",
    "hash_key",
    "cast_for",
    "ssml_for",
    "billable_chars",
    "escape",
]


@dataclass(frozen=True)
class Voice:
    """One Polly voice: `DescribeVoices`' Id, LanguageCode and Gender."""

    id: str
    language: str = "en-US"
    gender: str = ""

    @classmethod
    def from_api(cls, d: dict) -> "Voice":
        return cls(
            id=str(d.get("Id") or ""),
            language=str(d.get("LanguageCode") or ""),
            gender=str(d.get("Gender") or ""),
        )


# The standard-engine English voices, read on 2026-09-04 from
# https://docs.aws.amazon.com/polly/latest/dg/standard-voices.html . Only a
# FALLBACK: `PollyTTS.voices()` asks `DescribeVoices` for the live list and
# uses this when the call fails, so a roster change costs a wrong-sounding
# session rather than a silent one. en-GB-WLS is Welsh-accented English and is
# in the list for the same reason the others are — one more distinguishable
# voice at a table that needs several.
STANDARD_ENGLISH: tuple[Voice, ...] = (
    Voice("Amy", "en-GB", "Female"),
    Voice("Aditi", "en-IN", "Female"),
    Voice("Brian", "en-GB", "Male"),
    Voice("Emma", "en-GB", "Female"),
    Voice("Geraint", "en-GB-WLS", "Male"),
    Voice("Ivy", "en-US", "Female"),
    Voice("Joanna", "en-US", "Female"),
    Voice("Joey", "en-US", "Male"),
    Voice("Kendra", "en-US", "Female"),
    Voice("Kevin", "en-US", "Male"),
    Voice("Kimberly", "en-US", "Female"),
    Voice("Nicole", "en-AU", "Female"),
    Voice("Raveena", "en-IN", "Female"),
    Voice("Russell", "en-AU", "Male"),
    Voice("Salli", "en-US", "Female"),
)

MONSTER_PREFIX = "monster:"

# Timbre shifts dealt to monsters: never 0, because a monster whose only
# treatment rounded to "no treatment" is a goblin that sounds like the barmaid.
MONSTER_VTL: tuple[int, ...] = (-20, -10, 10, 20, 30, 40)


@dataclass(frozen=True)
class Cast:
    """A seat at the table: the voice, and how it is bent for this actor."""

    key: str
    voice_id: str
    language: str
    pitch_pct: int = 0     # <prosody pitch>, percent, 0 = the voice as recorded
    rate_pct: int = 100    # <prosody rate>, percent of normal
    vtl_pct: int = 0       # <amazon:effect vocal-tract-length>, percent; monsters only

    def cache_key(self) -> str:
        return f"{self.voice_id}|{self.pitch_pct}|{self.rate_pct}|{self.vtl_pct}"


def hash_key(s: str) -> int:
    """FNV-1a, 32-bit, over UTF-16 code units.

    Byte-for-byte the `hashString` in `web/static/speech.js`, including its
    `charCodeAt` view of the string, so the two halves of the app agree on
    which actor sits in which seat. Ids are ASCII in practice; the surrogate
    pair handling is here so that claim is true rather than nearly true.
    """
    h = 0x811C9DC5
    data = str(s or "").encode("utf-16-le")
    for i in range(0, len(data), 2):
        code = data[i] | (data[i + 1] << 8)
        h ^= code
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def is_monster_key(key: str) -> bool:
    return str(key or "").startswith(MONSTER_PREFIX)


def _pick(pool: list[Voice], h: int) -> Voice:
    return pool[h % len(pool)]


def cast_for(key: str, pool, dm_voice: str = "") -> Cast:
    """Deal `key` a voice out of `pool`, deterministically.

    `pool` is any iterable of `Voice`; it is sorted by id here so the casting
    does not depend on the order `DescribeVoices` happened to return. Raises
    `ValueError` on an empty pool — an empty pool is a configuration failure,
    and casting silently to nothing would be heard as the narrator going quiet.
    """
    voices = sorted({v.id: v for v in pool}.values(), key=lambda v: v.id)
    if not voices:
        raise ValueError("no voices to cast from")
    key = str(key or "dm")

    # The DM is the one seat that is chosen rather than dealt: it narrates most
    # of the game, so it is worth being able to say which voice does it.
    dm = next((v for v in voices if v.id.lower() == str(dm_voice or "").lower()), voices[0])
    if key == "dm":
        return Cast("dm", dm.id, dm.language)

    # Everyone else is dealt out of the rest, so the DM's voice is not also a
    # player's. With one voice in the pool there is no "rest" to deal from.
    others = [v for v in voices if v.id != dm.id] or voices
    h = hash_key(key)
    voice = _pick(others, h)

    if is_monster_key(key):
        # A monster is the one seat where sounding wrong is the point. Timbre
        # does the work — a longer vocal tract is a bigger creature — with
        # pitch and rate behind it, so the ogre and the goblin are told apart
        # even when they are dealt the same voice.
        return Cast(
            key,
            voice.id,
            voice.language,
            pitch_pct=-20 + ((h >> 8) % 7) * 5,      # -20 … +10
            vtl_pct=MONSTER_VTL[(h >> 12) % len(MONSTER_VTL)],
            rate_pct=90 + ((h >> 16) % 4) * 5,       # 90 … 105
        )

    # A small per-actor pitch offset always: two actors can be dealt one voice.
    # With too few voices to go round, lean on pitch and rate much harder —
    # the same trade `speech.js` makes on a device with two voices installed.
    few = len(others) < 4
    if few:
        return Cast(
            key,
            voice.id,
            voice.language,
            pitch_pct=-15 + ((h >> 8) % 7) * 5,      # -15 … +15
            rate_pct=94 + ((h >> 16) % 4) * 4,       # 94 … 106
        )
    return Cast(key, voice.id, voice.language, pitch_pct=-10 + ((h >> 8) % 5) * 5)


def escape(text: str) -> str:
    """XML-escape a line for SSML. Polly rejects a document that is not
    well-formed, so an ampersand in a name is a failed line, not a typo."""
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def ssml_for(text: str, cast: Cast) -> str:
    """The `<speak>` document for a line in its seat's voice.

    `<prosody pitch>` and `<amazon:effect vocal-tract-length>` are supported on
    the standard engine and NOT on neural, long-form or generative — Polly
    errors on an unsupported tag rather than ignoring it, so an engine change
    is a change here too.
    """
    body = escape(text)
    prosody = []
    if cast.pitch_pct:
        prosody.append(f'pitch="{cast.pitch_pct:+d}%"')
    if cast.rate_pct != 100:
        prosody.append(f'rate="{cast.rate_pct:d}%"')
    if prosody:
        body = "<prosody " + " ".join(prosody) + ">" + body + "</prosody>"
    if cast.vtl_pct:
        body = (
            f'<amazon:effect vocal-tract-length="{cast.vtl_pct:+d}%">' + body + "</amazon:effect>"
        )
    return "<speak>" + body + "</speak>"


def billable_chars(text: str) -> int:
    """What Polly bills for a line: the text, not the markup.

    "The size of the input text can be up to 3000 billed characters (6000 total
    characters). SSML tags are not counted as billed characters." —
    https://docs.aws.amazon.com/polly/latest/dg/limits.html (read 2026-09-04).
    """
    return len(str(text or ""))
