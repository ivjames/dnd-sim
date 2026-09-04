"""Casting for server-rendered narration: which Polly voice says a line, and
the SSML it says it in.

Pure — no boto3, no network, no filesystem. Given a voice key and a pool of
voices this decides everything about how the line sounds, so the casting rules
can be tested without an AWS account (`tests/test_tts_voices.py`).

The voice keys are the browser's: `web/static/speech.js: voiceKeyFor` decides
who is speaking (`dm`, a party member's own id, `npc`, or `monster:<id>`) and
sends the key with the line. The hash below is the same FNV-1a the browser
uses, so a given actor lands on the same seat every time.

Three things differ from the browser's casting, all because Polly is not a
device voice list:

  * There are no novelty voices. `speech.js` casts a speaking monster out of
    Bubbles and Zarvox where the device has them; here a monster gets an
    ordinary voice put through `<amazon:effect vocal-tract-length>`, which
    changes the timbre rather than the pitch — a longer vocal tract is a bigger
    creature. That effect is standard-engine only, which is part of why the
    standard engine is the one this runs on.
  * The pool is the same for everyone. The browser has to keep novelty voices
    away from narration; here every voice can carry a line.
  * Polly has children's voices, and the browser's voice list does not say
    whether it has any. Ivy, Justin and Kevin are recorded as children by
    Amazon and by `CHILD_VOICE_IDS` here, and they are dealt only to a
    character whose party spec asks for one — an adventurer with no `age`
    stated is cast from the adult voices, because a cleric called Father
    Bexley read in a nine-year-old's voice is a casting bug and not a
    surprise worth keeping. `speech.js` cannot make the same distinction:
    `SpeechSynthesisVoice` reports no age, exactly as it reports no gender.
"""

from __future__ import annotations

import hashlib
import re
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
    "is_monster_key",
    "normalize_gender",
    "normalize_age",
    "accent_for",
    "ACCENTS",
    "is_child_voice",
    "GENDERS",
    "AGES",
    "CHILD_VOICE_IDS",
    "CHILD_MAX_AGE",
    "ENGINE_SSML",
    "source_fingerprint",
]

_SOURCE_FP: str | None = None


def source_fingerprint() -> str:
    """A digest of this module's own source.

    Everything that turns a voice key and a line into audio lives here — the
    roster, the hash, the pitch and timbre spreads, the SSML. A deployment that
    changes any of it changes the audio while the engine, the language, the DM
    voice and the voice ids all stay put, so a fingerprint built only from
    those would not move and every browser would go on replaying its
    year-long-immutable copies of the old casting.

    Hashed rather than a hand-bumped constant because a constant is only
    correct for as long as someone remembers it. Reading the source can fail
    (a frozen or zipped deploy), and "unknown" is then stable, which is the
    same behaviour as having no version at all rather than a worse one.
    """
    global _SOURCE_FP
    if _SOURCE_FP is None:
        try:
            with open(__file__, "rb") as fh:
                _SOURCE_FP = hashlib.sha256(fh.read()).hexdigest()[:8]
        except OSError:  # pragma: no cover - unreadable source
            _SOURCE_FP = "unknown"
    return _SOURCE_FP


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

# Polly reports a voice's `Gender` as exactly "Female" or "Male" — there is no
# third kind of voice to cast from. So a character whose gender is neither (or
# is not stated) is dealt from the WHOLE pool rather than being pushed into one
# of the two: the roster's limitation is not something to launder into a
# character sheet. `_gender` returns "" for everything it does not recognise,
# and "" means no constraint.
GENDERS = {"f": "female", "female": "female", "woman": "female",
           "m": "male", "male": "male", "man": "male"}

#: The voices Amazon records as children's, matched on the id, case-insensitively.
#:
#: Written down rather than read from the roster because `DescribeVoices`
#: cannot answer it: the API reports `Gender` and nothing else about the
#: speaker, so unlike the gender constraint there is no live field to narrow on
#: and this table is the only thing that knows. It is the whole set the voice
#: list annotates in any language — "Ivy … Female (child)", "Justin … Male
#: (child)", "Kevin … Male (child)", all three en-US
#: (https://docs.aws.amazon.com/polly/latest/dg/available-voices.html, read
#: 2026-09-04); Justin is neural-only and Ivy and Kevin serve both engines, so
#: the pool a table is cast from usually holds two or three of them.
CHILD_VOICE_IDS = frozenset({"ivy", "justin", "kevin"})

#: Above this many years a stated age is an adult. Polly's children's voices
#: are recorded by children, so the line is where a voice stops being usable
#: rather than where a jurisdiction puts it; there is no adolescent voice on the
#: roster to aim a teenager at.
CHILD_MAX_AGE = 12

# What a party spec's `age` may say. Only "child" changes the casting: every
# other answer, and no answer at all, is dealt from the adult voices, which is
# what an unstated age has to mean at a table of adventurers — a cleric called
# Father Bexley read in a nine-year-old's voice is the bug this exists to fix.
# So the words below are read for intent rather than for effect: "elder" is
# recorded because a config is entitled to say it, and it casts as an adult
# because Polly has no elderly voice to cast it as. `normalize_age` also takes
# a number, in years.
AGES = {"child": "child", "kid": "child", "boy": "child", "girl": "child",
        "adult": "adult", "grown": "adult", "grownup": "adult", "grown-up": "adult",
        "elder": "adult", "elderly": "adult", "old": "adult"}

#: The one numeric grammar an `age` may be written in: an optional sign, plain
#: decimal digits with at most one point, an optional exponent. Nothing else.
#:
#: Pinned with a pattern rather than left to `float()` because the panel in
#: `web/static/app.js` has to agree with this function about which strings are
#: numbers, and `float()` and JavaScript's `Number()` disagree in both
#: directions: `float("1_0")` is 10 where `Number("1_0")` is NaN, and
#: `Number("0xA")` is 10 where `float("0xA")` raises. Either disagreement is a
#: select that shows one thing and a server that casts another — and because
#: submitting the panel writes its answer back, opening the panel and touching
#: nothing would silently restate the character's age. So both sides accept
#: exactly this, and the shared corpus in `web/tests/test_newgame_panel.py`
#: runs the two implementations against each other. `inf` and `nan` are
#: excluded here as well, though the range check below would have caught them.
_NUMERIC_AGE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?\Z")

# Timbre shifts dealt to monsters: never 0, because a monster whose only
# treatment rounded to "no treatment" is a goblin that sounds like the barmaid.
MONSTER_VTL: tuple[int, ...] = (-20, -10, 10, 20, 30, 40)


@dataclass(frozen=True)
class Cast:
    """A seat at the table: the voice, the engine that will speak it, and how
    it is bent for this actor.

    The engine rides on the cast because it is not one setting for the whole
    game: a monster is cast on whichever engine can still change its timbre.
    Keeping the two together is what stops a line being cast for one engine and
    rendered on another.
    """

    key: str
    voice_id: str
    language: str
    engine: str = "standard"
    pitch_pct: int = 0     # <prosody pitch>, percent, 0 = the voice as recorded
    rate_pct: int = 100    # <prosody rate>, percent of normal
    vtl_pct: int = 0       # <amazon:effect vocal-tract-length>, percent; monsters only

    def cache_key(self) -> str:
        return f"{self.engine}|{self.voice_id}|{self.pitch_pct}|{self.rate_pct}|{self.vtl_pct}"


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


#: What a Polly LanguageCode sounds like, in the words a listener would use.
#:
#: The roster is dealt from by `cast_for` and the language rides along on the
#: `Cast`, so the page can already say *which* voice reads a seat — but "Aditi"
#: tells a spectator nothing and "en-IN" only slightly more. These are the
#: accents of the English roster Polly actually serves; anything else is
#: reported by its code rather than guessed at, because a wrong accent on a
#: character is worse than an unfamiliar language tag (`accent_for`).
#:
#: en-GB-WLS is Welsh-accented English rather than Welsh, which is why it is
#: keyed separately and why the lookup below tries the full code first.
ACCENTS: dict[str, str] = {
    "en-au": "Australian",
    "en-gb": "British",
    "en-gb-wls": "Welsh",
    "en-ie": "Irish",
    "en-in": "Indian",
    "en-nz": "New Zealand",
    "en-sg": "Singaporean",
    "en-us": "American",
    "en-za": "South African",
}


def accent_for(language: str) -> str:
    """A human accent name for a Polly LanguageCode, or the code itself.

    Falls back to the code because `PollyTTS.voices()` reads the live roster:
    a voice Amazon adds tomorrow in a locale not listed above must still be
    describable, and "en-GB-SCT" said plainly is honest where a guess at
    "Scottish" from a table written today would eventually be a lie about
    which voice a listener is hearing.
    """
    code = str(language or "").strip()
    if not code:
        return ""
    return ACCENTS.get(code.lower(), code)


def normalize_gender(gender: str) -> str:
    """"female", "male", or "" for no constraint. See `GENDERS`."""
    return GENDERS.get(str(gender or "").strip().lower(), "")


def is_child_voice(voice) -> bool:
    """Whether `voice` (a `Voice`, or a bare id) is one of Polly's children's."""
    ident = getattr(voice, "id", voice)
    return str(ident or "").strip().lower() in CHILD_VOICE_IDS


def normalize_age(age) -> str:
    """"child", "adult", or "" for nothing said. See `AGES`.

    A number is read as years, so `"age": 9` and `"age": "child"` are the same
    casting and `"age": 40` and no age at all are the same casting. A number
    that is not a plausible age — negative, zero, absurd — is read as nothing
    said rather than rounded into one of the two.
    """
    if isinstance(age, bool):        # True is not an age; bool is an int in Python
        return ""
    if isinstance(age, (int, float)):
        raw: object = age
    else:
        said = str(age or "").strip().lower()
        if not said:
            return ""
        if said in AGES:
            return AGES[said]
        if not _NUMERIC_AGE.match(said):
            return ""
        raw = said
    try:
        years = float(raw)               # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        # OverflowError is not hypothetical: a JSON config may carry an
        # integer too big for a float, and this endpoint answers anonymous
        # callers — an unreadable age is "nothing said", never a 500.
        return ""
    if years != years or years <= 0 or years > 1000:      # NaN, and nothing anyone has been
        return ""
    return "child" if years <= CHILD_MAX_AGE else "adult"


def cast_for(key: str, pool, dm_voice: str = "", gender: str = "",
             engine: str = "standard", age="") -> Cast:
    """Deal `key` a voice out of `pool`, deterministically.

    `pool` is any iterable of `Voice`; it is sorted by id here so the casting
    does not depend on the order `DescribeVoices` happened to return. Raises
    `ValueError` on an empty pool — an empty pool is a configuration failure,
    and casting silently to nothing would be heard as the narrator going quiet.

    `gender` narrows the pool to voices Polly reports as that gender, and `age`
    decides whether the character is dealt a child's voice. Both come from the
    character, not from whoever asked for the clip.
    """
    voices = sorted({v.id: v for v in pool}.values(), key=lambda v: v.id)
    if not voices:
        raise ValueError("no voices to cast from")
    key = str(key or "dm")
    adults = [v for v in voices if not is_child_voice(v)]

    # The DM is the one seat that is chosen rather than dealt: it narrates most
    # of the game, so it is worth being able to say which voice does it. A name
    # is honoured whatever age it is — asking for Ivy is asking for Ivy — but
    # the fallback, which is whatever sorts first, skips the children: on the
    # en-US roster alone that is Ivy, and a narrator nobody chose should not be
    # a nine-year-old.
    default_dm = (adults or voices)[0]
    dm = next((v for v in voices if v.id.lower() == str(dm_voice or "").lower()), default_dm)
    if key == "dm":
        return Cast("dm", dm.id, dm.language, engine)

    # Everyone else is dealt out of the rest, so the DM's voice is not also a
    # player's. With one voice in the pool there is no "rest" to deal from.
    others = [v for v in voices if v.id != dm.id] or voices

    # Age narrows first, and it narrows even when nothing was said: a character
    # with no `age` is an adult, because the alternative — a party of four
    # dealt from a roster with two children's voices in it — casts one
    # adventurer in eight as a child and calls it deterministic. A character
    # that asks for a child's voice gets one; nobody else can be dealt one.
    want_age = normalize_age(age)
    if want_age == "child":
        of_age = [v for v in others if is_child_voice(v)]
    else:
        of_age = [v for v in others if not is_child_voice(v)]
    # An empty result is a roster that cannot answer, not a silence — the same
    # trade as an unanswerable gender below. A language with no children's
    # voices (every language but en-US) casts a child from the adult voices.
    if of_age:
        others = of_age

    # A stated gender narrows who can be dealt, and only that: the choice
    # within the narrowed set is the same hash as ever, so a character keeps
    # its voice for as long as its gender and the roster do.
    want = normalize_gender(gender)
    if want:
        matching = [v for v in others if v.gender.lower() == want]
        # A language whose standard voices are all one gender (Korean ships
        # one, Swedish one) would otherwise go silent. A voice of the wrong
        # gender is a worse match, not a worse failure.
        if matching:
            others = matching

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
            engine,
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
            engine,
            pitch_pct=-15 + ((h >> 8) % 7) * 5,      # -15 … +15
            rate_pct=94 + ((h >> 16) % 4) * 4,       # 94 … 106
        )
    return Cast(key, voice.id, voice.language, engine, pitch_pct=-10 + ((h >> 8) % 5) * 5)


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


#: What each Polly engine will accept of a `Cast`. Polly ERRORS on a tag the
#: engine does not support rather than ignoring it, so a line written for the
#: wrong engine is a 502 and a fallback, not a slightly flat reading.
#:
#:   pitch  `<prosody pitch>` — "Generative, Neural, and Long-Form voices
#:          support the volume and rate attributes, but don't support the pitch
#:          attribute" (docs.aws.amazon.com/polly/latest/dg/prosody-tag.html)
#:   vtl    `<amazon:effect vocal-tract-length>` — "Not available" for all
#:          three (…/supportedtags.html)
#:   rate   `<prosody rate>` — everywhere, except that on generative the
#:          prosody tag "can be used only around full sentences", and a chunk
#:          can be a mid-sentence fragment when one sentence runs past the
#:          chunk cap. Not worth the risk for a rate nudge, so generative gets
#:          the plain text.
#:
#: Both dates read 2026-09-04.
ENGINE_SSML: dict[str, frozenset[str]] = {
    "standard": frozenset({"pitch", "rate", "vtl"}),
    "neural": frozenset({"rate"}),
    "long-form": frozenset({"rate"}),
    "generative": frozenset(),
}


def ssml_for(text: str, cast: Cast, engine: str = "") -> str:
    """The `<speak>` document for a line in its seat's voice.

    The engine comes from the cast; `engine` overrides it only for callers that
    want to ask "what would this sound like on X".

    Only what the engine supports is written. The consequence on anything but
    `standard` is real and is the reason `standard` is the default: with no
    pitch and no vocal-tract-length, two characters dealt the same voice cannot
    be told apart, and a monster is only a voice rather than a big one.
    """
    name = str(engine or cast.engine or "standard").strip().lower()
    allowed = ENGINE_SSML.get(name, ENGINE_SSML["standard"])
    body = escape(text)
    prosody = []
    if cast.pitch_pct and "pitch" in allowed:
        prosody.append(f'pitch="{cast.pitch_pct:+d}%"')
    if cast.rate_pct != 100 and "rate" in allowed:
        prosody.append(f'rate="{cast.rate_pct:d}%"')
    if prosody:
        body = "<prosody " + " ".join(prosody) + ">" + body + "</prosody>"
    if cast.vtl_pct and "vtl" in allowed:
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
