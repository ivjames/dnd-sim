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
    ordinary voice and a `MonsterFX` — a size shift, and sometimes grit or a
    room — applied to the audio after Polly hands it over (`tts/dsp.py`). It
    used to get `<amazon:effect vocal-tract-length>` instead, which is
    standard-engine only and so held every monster on the engine the rest of
    the table had left. Doing the size shift ourselves is what lets a monster
    be cast on the table's engine; `MONSTER_VTL` and the tag are still here for
    `DND_TTS_MONSTER_FX=0`, which puts the old arrangement back.
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

from tts.dsp import MAX_SIZE_PCT, MonsterFX
from tts.dsp import source_fingerprint as dsp_fingerprint

__all__ = [
    "Voice",
    "Cast",
    "MonsterFX",
    "STANDARD_ENGLISH",
    "hash_key",
    "cast_for",
    "ssml_for",
    "billable_chars",
    "escape",
    "is_monster_key",
    "normalize_gender",
    "gender_for_pronouns",
    "normalize_age",
    "accent_for",
    "ACCENTS",
    "is_child_voice",
    "GENDERS",
    "PRONOUN_GENDERS",
    "AGES",
    "CHILD_VOICE_IDS",
    "CHILD_MAX_AGE",
    "ENGINE_SSML",
    "allowed_ssml",
    "Tune",
    "tune_from",
    "retune",
    "PITCH_MIN_PCT",
    "PITCH_MAX_PCT",
    "RATE_MIN_PCT",
    "RATE_MAX_PCT",
    "MONSTER_VTL",
    "MONSTER_SIZE",
    "MONSTER_SIZE_BANDS",
    "MONSTER_GROWL_ALWAYS",
    "AUDIBLE_SIZE_PCT",
    "CREATURE_SIZES",
    "DEFAULT_SIZE_BAND",
    "normalize_creature_size",
    "MONSTER_GROWL",
    "MONSTER_CAVE",
    "MONSTER_TEMPO",
    "source_fingerprint",
]

_SOURCE_FP: str | None = None


def source_fingerprint() -> str:
    """A digest of this module's own source, and of `tts/dsp.py`'s.

    Everything that turns a voice key and a line into audio lives in the two —
    the roster, the hash, the pitch and timbre spreads, the SSML here, and the
    monster treatment there. A deployment that changes any of it changes the
    audio while the engine, the language, the DM voice and the voice ids all
    stay put, so a fingerprint built only from those would not move and every
    browser would go on replaying its year-long-immutable copies of the old
    casting.

    Hashed rather than a hand-bumped constant because a constant is only
    correct for as long as someone remembers it. Reading the source can fail
    (a frozen or zipped deploy), and "unknown" is then stable, which is the
    same behaviour as having no version at all rather than a worse one.
    """
    global _SOURCE_FP
    if _SOURCE_FP is None:
        try:
            with open(__file__, "rb") as fh:
                digest = hashlib.sha256(fh.read())
        except OSError:  # pragma: no cover - unreadable source
            _SOURCE_FP = "unknown"
        else:
            digest.update(dsp_fingerprint().encode())
            _SOURCE_FP = digest.hexdigest()[:8]
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

#: The subject pronouns that name a voice on Polly's roster — and the only two
#: that can, because the roster is `Female` and `Male`.
#:
#: A party spec states `pronouns`, not a gender: a character's pronouns are a
#: fact its own persona already carries ("she keeps the ledger", `Father`
#: Bexley), where a gender is a second fact someone has to infer from them. The
#: inference is this table, it is one-way, and it answers one question — which
#: voices this character may be dealt from. It is not a claim that a pronoun
#: *is* a gender.
#:
#: Everything else a character may go by — `they/them`, a neopronoun set, a
#: spelling this table has never seen — leaves the pool whole, which is the
#: same casting an unstated pronoun gets and the only honest one: a roster with
#: two kinds of voice on it cannot answer a third, and pushing such a character
#: into one of the two to fill the gap would launder the roster's limitation
#: into someone's character sheet.
PRONOUN_GENDERS = {"he": "male", "she": "female"}

#: The first run of letters in a stated pronoun set: the subject pronoun.
_FIRST_PRONOUN = re.compile(r"[a-z]+")

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
# Only reachable with `DND_TTS_MONSTER_FX=0`; `MONSTER_SIZE` is what a monster
# is dealt by default. Kept because that switch keeps the standard-engine
# arrangement available, and because it is the thing to compare against.
MONSTER_VTL: tuple[int, ...] = (-20, -10, 10, 20, 30, 40)

#: How big the creature is: `MonsterFX.size_pct`, signed the way VTL is —
#: positive is a longer vocal tract, a bigger creature, a lower voice. Never 0,
#: for `MONSTER_VTL`'s reason.
#:
#: **Keyed on the creature's SRD size**, one band each, because the seat is
#: not. A voice key is `monster:mon_6` and `mon_6` is spawn order
#: (`orchestrator/game.py: _spawn_monsters`), so a hash over the key alone
#: knows only which slot a creature occupies — which is how, before this, an
#: Ogre landed on +9% while a Gnoll in the same fight took +34% and sounded
#: bigger than it. The band comes from the stat block; the hash picks within
#: the band, so two gnolls still differ from each other and neither is ever
#: mistaken for the ogre.
#:
#: Bands are monotonic and do not overlap, so "bigger creature" and "lower
#: voice" cannot disagree. The spread is narrower on the negative side and
#: wider on the positive because this is a resample and VTL was not: it takes
#: pitch with it, so -20% here is a squeak where -20% there was only a small
#: skull. `M` sits either side of the voice as recorded and never on it —
#: a person-sized creature should sound close to the voice it was dealt,
#: but no monster is dealt no treatment at all.
MONSTER_SIZE_BANDS: dict[str, tuple[int, ...]] = {
    "T": (-26, -22, -18),        # Tiny: a talking rat, a pixie
    "S": (-16, -13, -10),        # Small: goblin, kobold
    "M": (-8, -4, 4, 8),         # Medium: orc, gnoll, bandit, skeleton
    "L": (12, 16, 20),           # Large: ogre, troll, worg
    "H": (26, 30, 34),           # Huge: a giant
    "G": (38, 44, 50),           # Gargantuan: a dragon, a kraken
}

#: What a creature whose size nothing could say is dealt.
#:
#: `M`'s band, because a monster that talks is usually person-shaped and
#: because guessing wrong small is less wrong than guessing wrong huge. It is
#: reached by a name that is not in the SRD list, and by a replay of a game
#: whose snapshot no longer names the speaker — see `_creature_size_for` in
#: `web/routes/tts.py`, which is also where the cost of that is written down.
DEFAULT_SIZE_BAND = "M"

#: Every size shift a monster can be dealt, which is the union of the bands.
#: Kept as its own name because the contract tests enumerate it.
MONSTER_SIZE: tuple[int, ...] = tuple(
    sorted({v for band in MONSTER_SIZE_BANDS.values() for v in band})
)

#: What a stat block's `size` may say, in the two spellings anything writes it:
#: the SRD letter and the word. Read case-insensitively; anything else is
#: nothing said, which is `DEFAULT_SIZE_BAND`.
CREATURE_SIZES = {
    "t": "T", "tiny": "T",
    "s": "S", "small": "S",
    "m": "M", "medium": "M",
    "l": "L", "large": "L",
    "h": "H", "huge": "H",
    "g": "G", "gargantuan": "G",
}

#: Saturation, per monster. Two of the five are 0: grit is a characteristic,
#: not a uniform, and a table where every monster rasps has no rasping monster
#: in it. The size shift is what guarantees every monster is treated.
MONSTER_GROWL: tuple[int, ...] = (0, 0, 30, 55, 80)

#: A room, per monster. Mostly absent for the same reason, and more so: a comb
#: filter on a line the DM has just narrated dry is the one effect here a
#: listener can mistake for a fault.
MONSTER_CAVE: tuple[int, ...] = (0, 0, 0, 35, 55)

#: Below this, a size shift is a different person rather than a different kind
#: of thing. Roughly a semitone and a half: at 4% a resample is under a
#: semitone and reads as nothing at all.
AUDIBLE_SIZE_PCT = 10

#: The grit a monster gets when nothing else would have marked it as one — the
#: non-zero half of `MONSTER_GROWL`.
#:
#: `MONSTER_SIZE_BANDS` has to be monotonic and non-overlapping, and `M`
#: straddles zero because a person-sized creature IS the size of the voice it
#: was dealt. Those two together mean a Medium creature's shift can only ever
#: be small — and a Medium creature dealt no grit and no room was then an
#: ordinary voice reading a monster's lines, which is the barmaid problem
#: `MONSTER_VTL` excluded 0 to avoid. It reached about one Medium monster in
#: four, and Medium is the commonest size at a table that talks. So where size
#: cannot say "creature", the voice does.
MONSTER_GROWL_ALWAYS: tuple[int, ...] = tuple(g for g in MONSTER_GROWL if g)

#: How fast a monster talks, as a percentage of normal, ON TOP of the
#: compensation `MonsterFX.rate_pct` asks for. Same spread the VTL arrangement
#: dealt.
MONSTER_TEMPO: tuple[int, ...] = (90, 95, 100, 105)

#: `<prosody rate>` "has a range of 20-200%"
#: (https://docs.aws.amazon.com/polly/latest/dg/prosody-tag.html, read
#: 2026-09-04). The dealt spreads land far inside it; the clamp is here because
#: a rate outside it is an `InvalidSsmlException`, which is a silent fallback
#: to the browser's voice for that seat and nothing else.
RATE_MIN_PCT, RATE_MAX_PCT = 20, 200

#: `<prosody pitch>` accepts far more than this — the clamp is a listenability
#: bound rather than an API one. Past about half an octave either way a Polly
#: voice stops sounding like a person with a different voice and starts
#: sounding like a tape played wrong, which is not a casting choice anybody
#: wants to keep. A monster that *should* sound wrong gets there through
#: `tts/dsp.py`, not through this.
PITCH_MIN_PCT, PITCH_MAX_PCT = -50, 50


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
    #: What is done to the audio AFTER Polly (`tts/dsp.py`); monsters only, and
    #: None for every other seat. A cast that carries one is rendered from
    #: `pcm` and served as a WAV, because the treatment has to happen between
    #: the two — see `PollyTTS._synthesize_now`.
    fx: MonsterFX | None = None

    def cache_key(self) -> str:
        base = f"{self.engine}|{self.voice_id}|{self.pitch_pct}|{self.rate_pct}|{self.vtl_pct}"
        # Appended only when there is one, so an untreated cast spells exactly
        # what it always did — the same rule, and the same reason, as
        # `PollyTTS.cache_key_for`.
        return base + ("|" + self.fx.token() if self.fx else "")


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
#: keyed on the whole code rather than on the `en-GB` it starts with.
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


def gender_for_pronouns(pronouns) -> str:
    """Which voices a character who goes by `pronouns` may be cast from.

    `"he/him"` -> `"male"`, `"she/her"` -> `"female"`, everything else -> `""`,
    which is the whole pool. Read from the FIRST pronoun listed, so `"he/him"`,
    `"he/him/his"` and a bare `"He"` are one answer and a character who writes
    `"she/they"` is cast the way they wrote it.

    The answer is about the roster and nothing else. It does not say what a
    character's pronouns *are*: `"they/them"`, a neopronoun set and a stated
    nothing all come back `""` here, because Polly reports `Female` and `Male`
    and there is no third voice to deal. See `PRONOUN_GENDERS`.
    """
    found = _FIRST_PRONOUN.search(str(pronouns or "").strip().lower())
    return PRONOUN_GENDERS.get(found.group(0), "") if found else ""


def is_child_voice(voice) -> bool:
    """Whether `voice` (a `Voice`, or a bare id) is one of Polly's children's."""
    ident = getattr(voice, "id", voice)
    return str(ident or "").strip().lower() in CHILD_VOICE_IDS


def normalize_creature_size(size) -> str:
    """An SRD size letter — `"T"`…`"G"` — or `""` for nothing recognised.

    Takes the letter a stat block carries (`{"size": "L"}` in
    `engine/data/monsters.json`) and the word a person would write, because
    this is read from game state that other things also write. Anything else
    is nothing said rather than a guess: `cast_for` answers that with
    `DEFAULT_SIZE_BAND`, and a creature dealt the wrong band sounds wrong for
    the life of its cached clips.
    """
    return CREATURE_SIZES.get(str(size or "").strip().lower(), "")


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
             engine: str = "standard", age="", monster_fx: bool = True,
             size="") -> Cast:
    """Deal `key` a voice out of `pool`, deterministically.

    `pool` is any iterable of `Voice`; it is sorted by id here so the casting
    does not depend on the order `DescribeVoices` happened to return. Raises
    `ValueError` on an empty pool — an empty pool is a configuration failure,
    and casting silently to nothing would be heard as the narrator going quiet.

    `gender` narrows the pool to voices Polly reports as that gender, and `age`
    decides whether the character is dealt a child's voice. Both come from the
    character, not from whoever asked for the clip.

    `monster_fx` says how a monster is made to sound like one: with a
    `MonsterFX` applied after synthesis (the default, and what lets a monster
    be cast on the table's engine), or with the standard-only SSML that used to
    be the only way — `DND_TTS_MONSTER_FX=0`. It changes nothing for any other
    seat.

    `size` is the creature's SRD size (`"L"`, `"large"`, …), and it decides
    which band of `MONSTER_SIZE_BANDS` the size shift is dealt from — so an
    ogre is bigger than a goblin rather than merely later in the initiative
    order. Read for monsters and ignored for everyone else, who have no stat
    block and are not creatures.
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
        # A monster is the one seat where sounding wrong is the point. Size
        # does the work — a longer vocal tract is a bigger creature — with grit
        # and a room behind it, so the ogre and the goblin are told apart even
        # when they are dealt the same voice.
        #
        # Each treatment comes off its own slice of the one hash, so a monster
        # keeps everything about how it sounds for as long as its id does.
        if not monster_fx:
            # The standard-engine arrangement, kept whole behind the switch.
            return Cast(
                key,
                voice.id,
                voice.language,
                engine,
                pitch_pct=-20 + ((h >> 8) % 7) * 5,      # -20 … +10
                vtl_pct=MONSTER_VTL[(h >> 12) % len(MONSTER_VTL)],
                rate_pct=90 + ((h >> 16) % 4) * 5,       # 90 … 105
            )
        # The band is the creature's; the value within it is this creature's.
        # Both halves matter: without the band an ogre is only a slot number,
        # and without the hash four goblins are one goblin four times.
        band = MONSTER_SIZE_BANDS[normalize_creature_size(size) or DEFAULT_SIZE_BAND]
        size_pct = band[(h >> 12) % len(band)]
        growl = MONSTER_GROWL[(h >> 8) % len(MONSTER_GROWL)]
        cave = MONSTER_CAVE[(h >> 20) % len(MONSTER_CAVE)]
        # Every monster has to be audibly one. A shift under `AUDIBLE_SIZE_PCT`
        # is a different person rather than a different kind of thing, so where
        # the creature is person-sized and nothing else was dealt, it gets grit
        # — off its own slice of the hash, so it is still this creature's.
        if abs(size_pct) < AUDIBLE_SIZE_PCT and not growl and not cave:
            growl = MONSTER_GROWL_ALWAYS[(h >> 24) % len(MONSTER_GROWL_ALWAYS)]
        fx = MonsterFX(size_pct=size_pct, growl_pct=growl, cave_pct=cave)
        # Two things ride on one rate: undoing what the size shift does to
        # duration (`rate_pct`, exactly `100 + size_pct`) and how fast this
        # monster talks. They multiply — a creature dealt 90% tempo and a 24%
        # size shift is spoken at 112% and heard, after the shift, at 90%.
        #
        # On an engine that will not take `<prosody rate>` at all — generative,
        # per ENGINE_SSML — the compensation is dropped with the tag and the
        # line runs long or short by the size shift. Nothing here defaults to
        # that engine and no roster falls back to it.
        tempo = MONSTER_TEMPO[(h >> 16) % len(MONSTER_TEMPO)]
        rate = round(fx.rate_pct() * tempo / 100)
        return Cast(
            key,
            voice.id,
            voice.language,
            engine,
            rate_pct=max(RATE_MIN_PCT, min(RATE_MAX_PCT, rate)),
            fx=fx,
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


def allowed_ssml(engine: str) -> frozenset[str]:
    """Which of pitch/rate/vtl this engine will accept.

    The one place the `ENGINE_SSML` lookup happens, including its default: an
    engine nobody has heard of is written for the one this app is built around
    rather than sent bare, because being wrong loudly (an error naming the
    engine) beats being wrong quietly (a flat reading nothing reports).

    A function rather than a bare `.get` because more than one caller needs the
    answer — `ssml_for` writes the document, `tools/polly_check.py` reports what
    the document will contain — and a second copy of the rule is a second copy
    of the default to get wrong. It was: the tool spelled the fallback as "no
    tags", so an unrecognised engine was reported as carrying no
    vocal-tract-length while `ssml_for` went on writing one.
    """
    return ENGINE_SSML.get(str(engine or "standard").strip().lower(), ENGINE_SSML["standard"])


def ssml_for(text: str, cast: Cast, engine: str = "") -> str:
    """The `<speak>` document for a line in its seat's voice.

    The engine comes from the cast; `engine` overrides it only for callers that
    want to ask "what would this sound like on X".

    Only what the engine supports is written. The consequence on anything but
    `standard` is real and is the reason `standard` is the default: with no
    pitch and no vocal-tract-length, two characters dealt the same voice cannot
    be told apart, and a monster is only a voice rather than a big one.
    """
    allowed = allowed_ssml(engine or cast.engine)
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


@dataclass(frozen=True)
class Tune:
    """A listener's own choice for one seat, over the top of the casting.

    The casting rules deal a voice from the roster and bend it by a hash
    (`cast_for`), which is a good guess and no more: it cannot know that two of
    your four players sound alike to you, or that the goblin you have to listen
    to for an hour is the one voice you cannot stand. This is where that is
    overruled, one seat at a time.

    Every field is optional and an unset one changes nothing, so a tune that
    only moves the rate keeps the voice the casting chose — including when a
    later roster change moves it. The empty tune is the casting itself, which
    is what "auto" saves.

    The last three are the monster treatment (`tts/dsp.py`) and apply to a
    monster seat alone, because that is the only seat a treatment exists on:
    `fx` is None everywhere else, and switching it on for a PC would change
    what a clip *is* — pcm and a WAV rather than Polly's own MP3 — which is a
    different decision from recasting one.
    """

    voice_id: str = ""
    rate_pct: int | None = None
    pitch_pct: int | None = None
    size_pct: int | None = None
    growl_pct: int | None = None
    cave_pct: int | None = None

    def __bool__(self) -> bool:
        return bool(self.voice_id) or any(
            v is not None
            for v in (self.rate_pct, self.pitch_pct,
                      self.size_pct, self.growl_pct, self.cave_pct)
        )


def _clamped_int(value, lo: int, hi: int) -> int | None:
    """`value` as a whole percent inside [lo, hi], or None if it is not a number.

    Clamped rather than refused: these arrive from a slider, and the useful
    answer to one dragged past the end is the end. A rate outside Polly's own
    20–200 is an `InvalidSsmlException`, which the page hears as a seat that
    silently fell back to the browser's voice — see `RATE_MIN_PCT`.
    """
    if value is None or value == "":
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def tune_from(voice_id="", rate=None, pitch=None,
              size=None, growl=None, cave=None) -> Tune:
    """A `Tune` from three loose values (query strings, JSON, whatever).

    Nothing here validates the voice id against a roster: that needs the pool,
    which is the service's to know, and `retune` does it at the point of use.
    """
    return Tune(
        voice_id=str(voice_id or "").strip()[:64],
        rate_pct=_clamped_int(rate, RATE_MIN_PCT, RATE_MAX_PCT),
        pitch_pct=_clamped_int(pitch, PITCH_MIN_PCT, PITCH_MAX_PCT),
        size_pct=_clamped_int(size, -MAX_SIZE_PCT, MAX_SIZE_PCT),
        growl_pct=_clamped_int(growl, 0, 100),
        cave_pct=_clamped_int(cave, 0, 100),
    )


def retune(cast: Cast, tune: Tune | None, pool=()) -> Cast:
    """`cast` with the listener's choices applied — voice, rate, pitch, and on
    a monster the treatment itself.

    The engine is never changed, and neither is *whether* there is a treatment:
    which engine speaks a seat is a deployment's decision (`engine_for`), and a
    seat either is a monster or is not. A tune recasts how a seat sounds, not
    what kind of seat it is — so `size_pct`/`growl_pct`/`cave_pct` are ignored
    where `cast.fx` is None.

    **Rate means two different things, and this is where that is honoured.** On
    an ordinary seat it is the speaking rate and replaces what was dealt. On a
    monster it is the *tempo*, and it multiplies onto the compensation the size
    shift needs (`MonsterFX.rate_pct`, exactly `100 + size_pct`) — the same
    arrangement `cast_for` deals. Replacing the rate outright there would throw
    the compensation away, and the line would arrive as much too long as the
    creature is big; a tempo slider should not be able to do that. The dealt
    tempo is read back out of the cast, so changing the size alone keeps how
    fast this particular creature talks.

    A voice id that is not in `pool` is ignored rather than refused. The pool is
    what Polly listed for this engine a moment ago, and a stored tune outlives
    it: a roster change, a language change, or a deployment that moved the
    table to an engine this voice does not serve would otherwise turn every
    line of that seat into a 400. Falling back to the casting is the same
    answer `speech.js` gives for a browser voice that has gone.
    """
    if not tune:
        return cast

    voice_id = cast.voice_id
    if tune.voice_id and any(v.id == tune.voice_id for v in pool):
        voice_id = tune.voice_id

    fx = cast.fx
    treated = fx is not None
    fx_touched = treated and any(
        v is not None for v in (tune.size_pct, tune.growl_pct, tune.cave_pct)
    )
    if fx_touched:
        fx = MonsterFX(
            size_pct=fx.size_pct if tune.size_pct is None else tune.size_pct,
            growl_pct=fx.growl_pct if tune.growl_pct is None else tune.growl_pct,
            cave_pct=fx.cave_pct if tune.cave_pct is None else tune.cave_pct,
        )

    rate_pct = cast.rate_pct
    if treated and (fx_touched or tune.rate_pct is not None):
        # Left exactly as dealt when neither was touched: recomputing rounds,
        # and a rounded rate is a different SSML document — a different cache
        # key for a clip nobody asked to change.
        was = cast.fx.rate_pct() or 100
        dealt_tempo = cast.rate_pct * 100.0 / was
        tempo = dealt_tempo if tune.rate_pct is None else float(tune.rate_pct)
        rate_pct = max(RATE_MIN_PCT,
                       min(RATE_MAX_PCT, round(fx.rate_pct() * tempo / 100.0)))
    elif not treated and tune.rate_pct is not None:
        rate_pct = tune.rate_pct

    return Cast(
        cast.key,
        voice_id,
        cast.language,
        cast.engine,
        pitch_pct=cast.pitch_pct if tune.pitch_pct is None else tune.pitch_pct,
        rate_pct=rate_pct,
        vtl_pct=cast.vtl_pct,
        fx=fx,
    )
