"""What Amazon Polly documents, held against what `tts/` actually emits.

Every other test in this suite drives a fake — `FakeTTS` in
`web/tests/conftest.py`, the recording stubs in `tests/tts/test_client.py` —
and a fake accepts whatever it is handed. None of them has ever seen Polly, so
none of them can tell a document Polly will read from one it will reject. That
distinction is the whole risk here, because **Polly errors on a tag its engine
does not support rather than ignoring it**:

    "If you use unsupported SSML tags in standard, neural, or long-form
    format, you will get an error."
    https://docs.aws.amazon.com/polly/latest/dg/supportedtags.html

A rejected line is an `InvalidSsmlException`, which `web/routes/tts.py` turns
into a 502, which the page answers by speaking that one line in the browser's
own voice — audibly fine, silently wrong, and only visible in `dndsim logs`.
The monster path is the one at risk: it is the only seat that writes
`<amazon:effect vocal-tract-length>` and the only one routed to a different
engine to do it, so a table line succeeding proves nothing about it.

So this file transcribes the grammar, the ranges and the per-engine tag matrix
from Amazon's own pages (URL and read date on each) and holds every document
the app can emit against them. It is the OFFLINE half of the check: it can
prove a document matches what Polly documents, and it cannot prove Polly
accepts it. The online half is `tools/polly_check.py`, which sends one monster
line and one table line to real Polly from the droplet.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from tts.cache import AudioCache
from tts.client import DEFAULT_ENGINE, DEFAULT_MONSTER_ENGINE, PollyTTS, TTSError
from tts.dsp import SAMPLE_RATE, MonsterFX
from tts.voices import (
    CHILD_VOICE_IDS,
    ENGINE_SSML,
    MONSTER_CAVE,
    MONSTER_GROWL,
    MONSTER_SIZE,
    MONSTER_TEMPO,
    MONSTER_VTL,
    STANDARD_ENGLISH,
    Cast,
    Voice,
    cast_for,
    ssml_for,
)

DOCS_READ = "2026-09-05"


# -- what Amazon documents ---------------------------------------------------
#
# Transcribed, not recalled. Each block names the page it came from.

#: Which of `ssml_for`'s three treatments each engine will accept.
#:
#: https://docs.aws.amazon.com/polly/latest/dg/supportedtags.html
#:   <amazon:effect vocal-tract-length>  Neural / Long-Form / Generative: "Not available"
#:   <prosody>                           Neural / Long-Form / Generative: "Partial availability"
#: https://docs.aws.amazon.com/polly/latest/dg/prosody-tag.html
#:   "Prosody tag attributes are fully supported by the standard TTS voices.
#:    Generative, Neural, and Long-Form voices support the volume and rate
#:    attributes, but don't support the pitch attribute. For Generative voices,
#:    the prosody tag can be used only around full sentences."
#: https://docs.aws.amazon.com/polly/latest/dg/vocaltractlength-tag.html
#:   "This tag is currently supported only by the standard TTS format."
#: https://docs.aws.amazon.com/polly/latest/dg/supportedtags.html (read
#: 2026-09-05, for `volume` and `drc`):
#:   "All tags except for `<amazon:domain name="news">` are supported for
#:    Standard voices."
#:   <amazon:effect name="drc">  Neural: "Full availability" · Long-Form:
#:                               "Full availability" · Generative: "Not available"
#: `volume` rides on the same prosody row and the same prosody-tag sentence
#: quoted above: it is one of the two attributes the non-standard engines take.
DOCUMENTED_ENGINE_SSML = {
    "standard": frozenset({"pitch", "rate", "vtl", "volume", "drc"}),
    "neural": frozenset({"rate", "volume", "drc"}),
    "long-form": frozenset({"rate", "volume", "drc"}),
    # `rate`, `volume` and `drc` are all documented as available here and are
    # deliberately not used. `drc` is not a prosody attribute and could be
    # written on its own — but the reason the rest are excluded is that a chunk
    # can be a mid-sentence fragment and generative's prosody tag is "only
    # around full sentences", and a document carrying the compressor alone
    # would be a fourth arrangement nothing on this box produces or tests.
    # See ENGINE_SSML's own note.
    "generative": frozenset(),
}

#: `<prosody volume>`: "+ndB or -ndB: Changes the volume … A value of +0dB
#: means no change, +6dB is approximately twice the current amplitude, and
#: -6dB is approximately half." (prosody-tag.html). The signed decibel form is
#: the one written here; the named values (`loud`, `x-soft` …) are not used,
#: so the grammar is the whole of the check.
VOLUME_DB = re.compile(r"^[+-][0-9]+dB$")

#: `<prosody pitch>`: "+n% or -n%: Adjusts pitch by a relative percentage."
#: The percentage form is documented with a sign and with no stated numeric
#: range (prosody-tag.html), so the grammar is the whole of the check.
PITCH_PCT = re.compile(r"^[+-][0-9]+%$")

#: `<prosody rate>`: "n%: A non-negative percentage change in the speaking
#: rate. … This value has a range of 20-200%." (prosody-tag.html) — unsigned,
#: unlike the other two.
RATE_PCT = re.compile(r"^[0-9]+%$")
RATE_MIN, RATE_MAX = 20, 200

#: `<amazon:effect vocal-tract-length>`: "+n% or -n%: Adjusts the vocal tract
#: length by a relative percentage change in the current voice. … Valid values
#: range from +100% to -50%. Values outside this range are clipped."
#: (vocaltractlength-tag.html). An absolute `n%` form exists and is not used;
#: staying inside the range is what makes the written value the heard one.
VTL_PCT = re.compile(r"^[+-][0-9]+%$")
VTL_MIN, VTL_MAX = -50, 100

#: What `pcm` may be asked for at. "Valid values for pcm are '8000' and
#: '16000'" — and an `InvalidSampleRateException` if it is anything else
#: (https://docs.aws.amazon.com/polly/latest/APIReference/API_SynthesizeSpeech.html,
#: read 2026-09-04). This matters now that a monster's clip is `pcm`: it is the
#: one format that can be post-processed without a codec, and the price of it
#: is a 16 kHz ceiling against the 24 kHz the table's MP3 gets.
DOCUMENTED_PCM_SAMPLE_RATES = {"8000", "16000"}

#: The standard engine's English voices, with the Gender and LanguageCode
#: `DescribeVoices` reports. From the "Standard voice" column of
#: https://docs.aws.amazon.com/polly/latest/dg/voicelist.html , cross-read
#: against https://docs.aws.amazon.com/polly/latest/dg/standard-voices.html .
#: This is what `STANDARD_ENGLISH` claims to be, and it is load-bearing: it is
#: the roster a failed `DescribeVoices` falls back to, and a voice in it that
#: the standard engine does not serve is an `EngineNotSupportedException` on
#: every line cast to it.
DOCUMENTED_STANDARD_ENGLISH = {
    ("Nicole", "en-AU", "Female"),
    ("Russell", "en-AU", "Male"),
    ("Amy", "en-GB", "Female"),
    ("Emma", "en-GB", "Female"),
    ("Brian", "en-GB", "Male"),
    ("Geraint", "en-GB-WLS", "Male"),
    ("Aditi", "en-IN", "Female"),
    ("Raveena", "en-IN", "Female"),
    ("Ivy", "en-US", "Female"),
    ("Joanna", "en-US", "Female"),
    ("Kendra", "en-US", "Female"),
    ("Kimberly", "en-US", "Female"),
    ("Salli", "en-US", "Female"),
    ("Joey", "en-US", "Male"),
    ("Kevin", "en-US", "Male"),
}

#: The voices the list annotates as children's: "Ivy … Female (child)",
#: "Justin … Male (child)", "Kevin … Male (child)" — all en-US, and the only
#: three so marked in any language
#: (https://docs.aws.amazon.com/polly/latest/dg/available-voices.html, read
#: 2026-09-04). `DescribeVoices` has no age field, so `CHILD_VOICE_IDS` is
#: transcribed from this page and nothing at runtime can catch it being wrong:
#: a missing id is an adventurer read by a nine-year-old, and an id that is not
#: really a child's is a child cast as an adult.
DOCUMENTED_CHILD_VOICES = {"Ivy", "Justin", "Kevin"}

#: The English rows of voicelist.html in full: voice id → the engines that page
#: says serve it. Used to give the strict fake below the same
#: `EngineNotSupportedException` the real service raises.
DOCUMENTED_ENGINES_BY_VOICE = {
    # en-AU
    "Nicole": {"standard"}, "Russell": {"standard"},
    "Olivia": {"neural", "generative"},
    # en-GB
    "Amy": {"standard", "neural", "generative"},
    "Emma": {"standard", "neural"},
    "Brian": {"standard", "neural", "generative"},
    "Arthur": {"neural"},
    # en-GB-WLS
    "Geraint": {"standard"},
    # en-IN
    "Aditi": {"standard"}, "Raveena": {"standard"},
    "Kajal": {"neural", "generative"},
    # en-IE / en-NZ / en-SG / en-ZA
    "Niamh": {"neural", "generative"}, "Aria": {"neural", "generative"},
    "Jasmine": {"neural", "generative"}, "Ayanda": {"neural", "generative"},
    # en-US
    "Ivy": {"standard", "neural"},
    "Joanna": {"standard", "neural", "generative"},
    "Kendra": {"standard", "neural"},
    "Kimberly": {"standard", "neural"},
    "Salli": {"standard", "neural", "generative"},
    "Joey": {"standard", "neural"},
    "Kevin": {"standard", "neural"},
    "Justin": {"neural"},
    "Matthew": {"neural", "generative"},
    "Danielle": {"neural", "generative", "long-form"},
    "Gregory": {"neural", "long-form"},
    "Ruth": {"neural", "generative", "long-form"},
    "Stephen": {"neural", "generative"},
    "Tiffany": {"generative"},
    "Patrick": {"long-form"},
}


# -- reading a document back -------------------------------------------------

def treatments_in(doc: str) -> set[str]:
    """Which of pitch/rate/vtl a `<speak>` document actually uses.

    Parsed rather than substring-matched, so a malformed document fails here
    rather than passing on the strength of a tag name appearing in the text.
    """
    # `amazon:` is a prefix Polly accepts undeclared; ElementTree will not, so
    # bind it to a dummy namespace to parse.
    root = ET.fromstring(doc.replace("<speak>", '<speak xmlns:amazon="urn:amazon">', 1))
    assert root.tag == "speak"
    used: set[str] = set()
    for node in root.iter():
        if node.tag == "speak":
            continue
        if node.tag == "{urn:amazon}effect":
            # Two different effects share this tag and are told apart by which
            # attribute they carry: the timbre one takes a percentage, the
            # compressor takes `name="drc"`. Anything else under this tag is a
            # document nobody meant to write.
            if node.attrib.get("name") == "drc":
                assert set(node.attrib) == {"name"}, node.attrib
                used.add("drc")
            else:
                assert set(node.attrib) == {"vocal-tract-length"}, node.attrib
                used.add("vtl")
        elif node.tag == "prosody":
            assert set(node.attrib) <= {"pitch", "rate", "volume"}, node.attrib
            assert node.attrib, "<prosody> must carry at least one attribute"
            used.update(node.attrib)
        else:  # pragma: no cover - a tag nobody meant to write
            raise AssertionError(f"unexpected tag {node.tag!r} in {doc!r}")
    return used


def assert_documented(doc: str, engine: str) -> None:
    """Hold one document against Amazon's grammar, ranges and tag matrix."""
    root = ET.fromstring(doc.replace("<speak>", '<speak xmlns:amazon="urn:amazon">', 1))
    for node in root.iter():
        vtl = node.get("vocal-tract-length")
        if vtl is not None:
            assert VTL_PCT.match(vtl), f"{vtl!r} is not the documented +n%/-n%"
            assert VTL_MIN <= int(vtl[:-1]) <= VTL_MAX, f"{vtl!r} is outside +100%..-50%"
        pitch = node.get("pitch")
        if pitch is not None:
            assert PITCH_PCT.match(pitch), f"{pitch!r} is not the documented +n%/-n%"
        rate = node.get("rate")
        if rate is not None:
            assert RATE_PCT.match(rate), f"{rate!r} is not the documented unsigned n%"
            assert RATE_MIN <= int(rate[:-1]) <= RATE_MAX, f"{rate!r} is outside 20-200%"
        volume = node.get("volume")
        if volume is not None:
            assert VOLUME_DB.match(volume), f"{volume!r} is not the documented +ndB/-ndB"

    allowed = DOCUMENTED_ENGINE_SSML[engine]
    assert treatments_in(doc) <= allowed, f"{engine} cannot read {doc!r}"


def every_reachable_monster_cast(monster_fx: bool = True):
    """Every `Cast` the monster branch of `cast_for` can produce.

    Its treatments are dealt from independent slices of one hash, so the cross
    product is what a live game can actually reach — enumerating it costs
    nothing and enumerating the hash space costs a minute.

    Both arrangements are reachable: the treated one is the default, and
    `DND_TTS_MONSTER_FX=0` is the other.
    """
    if not monster_fx:
        for vtl in MONSTER_VTL:
            for pitch in range(-20, 11, 5):          # -20 + (h>>8 % 7)*5
                for rate in (90, 95, 100, 105):      # 90 + (h>>16 % 4)*5
                    yield Cast("monster:x", "Joey", "en-US", "standard",
                               pitch_pct=pitch, rate_pct=rate, vtl_pct=vtl)
        return
    for size in MONSTER_SIZE:
        for growl in MONSTER_GROWL:
            for cave in MONSTER_CAVE:
                for tempo in MONSTER_TEMPO:
                    fx = MonsterFX(size_pct=size, growl_pct=growl, cave_pct=cave)
                    yield Cast("monster:x", "Joey", "en-US", "neural",
                               rate_pct=round(fx.rate_pct() * tempo / 100), fx=fx)


# -- the matrix --------------------------------------------------------------

def test_the_engine_matrix_is_what_amazon_publishes():
    """`ENGINE_SSML` against the three doc pages it cites (see `DOCS_READ`)."""
    assert dict(ENGINE_SSML) == DOCUMENTED_ENGINE_SSML


def test_the_built_in_roster_is_the_standard_engines_english_voices():
    """A voice in the fallback roster the standard engine does not serve is an
    `EngineNotSupportedException` on every line dealt to it — and the fallback
    is exactly the path taken when `DescribeVoices` is unavailable, so nothing
    else would catch it."""
    assert {(v.id, v.language, v.gender) for v in STANDARD_ENGLISH} == \
        DOCUMENTED_STANDARD_ENGLISH
    for v in STANDARD_ENGLISH:
        assert "standard" in DOCUMENTED_ENGINES_BY_VOICE[v.id]


def test_the_childrens_voices_are_the_documented_ones():
    assert {i.lower() for i in DOCUMENTED_CHILD_VOICES} == set(CHILD_VOICE_IDS)
    # Each is a voice the page actually lists, spelled the way it lists it.
    assert DOCUMENTED_CHILD_VOICES <= set(DOCUMENTED_ENGINES_BY_VOICE)
    # Two of the three serve the standard engine, so a table cast from the
    # fallback roster meets them; Justin is neural-only, so the default engine
    # meets all three. Either way the pool has children's voices in it, which
    # is why the casting has to know.
    assert {v for v in DOCUMENTED_CHILD_VOICES
            if "standard" in DOCUMENTED_ENGINES_BY_VOICE[v]} == {"Ivy", "Kevin"}
    assert DOCUMENTED_ENGINES_BY_VOICE["Justin"] == {"neural"}


# -- the documents ------------------------------------------------------------

def test_the_exact_monster_document_a_treated_monster_sends():
    """What the droplet speaks by default now, pinned character for character.

    Almost nothing: the size shift is a sample rate rather than markup, so the
    document is the one tag every engine takes. That is the whole point of
    doing the treatment ourselves — this line is legal on neural, where the
    VTL document below is not.
    """
    fx = MonsterFX(size_pct=24, growl_pct=55, cave_pct=35)
    goblin = Cast("monster:goblin_1", "Joey", "en-US", "neural",
                  rate_pct=round(fx.rate_pct() * 95 / 100), fx=fx)
    assert goblin.rate_pct == 118
    for engine in ("standard", "neural", "long-form"):
        assert ssml_for("Fee fi.", goblin, engine) == \
            '<speak><prosody rate="118%">Fee fi.</prosody></speak>'
    assert ssml_for("Fee fi.", goblin, "generative") == "<speak>Fee fi.</speak>"

    # `rate_pct` is exactly the compensation for what playing at
    # `playback_rate` does to duration, so a monster dealt no tempo of its own
    # is heard at the length it was written.
    assert fx.rate_pct() == 124 and fx.playback_rate(SAMPLE_RATE) == round(16000 / 1.24)


def test_the_exact_monster_document_on_each_engine():
    """Pinned character for character, because this is the string that has
    never been sent to Polly. A change here is a change to what the droplet
    speaks with `DND_TTS_MONSTER_FX=0` and has to be made on purpose."""
    goblin = Cast("monster:goblin_1", "Joey", "en-US", "standard",
                  pitch_pct=-15, rate_pct=95, vtl_pct=30)

    assert ssml_for("Fee fi.", goblin, "standard") == (
        '<speak><amazon:effect vocal-tract-length="+30%">'
        '<prosody pitch="-15%" rate="95%">Fee fi.</prosody>'
        "</amazon:effect></speak>"
    )
    # The documented nesting is exactly this way round: vocaltractlength-tag.html
    # shows `<amazon:effect vocal-tract-length="-15%"><prosody pitch="+20%">…
    # </prosody></amazon:effect>` under "Combining Multiple Tags".
    assert ssml_for("Fee fi.", goblin, "neural") == '<speak><prosody rate="95%">Fee fi.</prosody></speak>'
    assert ssml_for("Fee fi.", goblin, "long-form") == '<speak><prosody rate="95%">Fee fi.</prosody></speak>'
    assert ssml_for("Fee fi.", goblin, "generative") == "<speak>Fee fi.</speak>"

    # A monster whose rate happens to land on 100% writes no rate at all: an
    # attribute that means "no change" is markup Polly has to parse for nothing.
    ogre = Cast("monster:ogre_1", "Joey", "en-US", "standard",
                pitch_pct=-15, rate_pct=100, vtl_pct=40)
    assert ssml_for("Fee fi.", ogre, "standard") == (
        '<speak><amazon:effect vocal-tract-length="+40%">'
        '<prosody pitch="-15%">Fee fi.</prosody>'
        "</amazon:effect></speak>"
    )


def test_the_exact_table_document_on_each_engine():
    """The seats that were confirmed working on the droplet — pinned so the
    monster fix that is not needed today cannot break them tomorrow."""
    dm = Cast("dm", "Brian", "en-GB", "neural")
    assert ssml_for("The cart still smoulders.", dm) == \
        "<speak>The cart still smoulders.</speak>"

    pc = Cast("pc_1", "Aditi", "en-IN", "neural", pitch_pct=-5)
    assert ssml_for("I go left.", pc) == "<speak>I go left.</speak>"   # neural drops pitch
    assert ssml_for("I go left.", pc, "standard") == \
        '<speak><prosody pitch="-5%">I go left.</prosody></speak>'


def test_no_monster_is_dealt_no_treatment_at_all():
    """`MONSTER_SIZE` holds no 0, and neither does `MONSTER_VTL`.

    The size shift is the one treatment every monster gets — grit and a room
    are characteristics, dealt to some of them — so a 0 in that tuple would
    make one monster in six an ordinary voice reading a monster's lines.
    """
    assert 0 not in MONSTER_SIZE
    for cast in every_reachable_monster_cast():
        assert cast.fx and cast.fx.size_pct != 0
        # And nothing standard-only, on any engine: that is what lets the
        # monster sit on the table's.
        assert treatments_in(ssml_for("Fee fi.", cast)) <= {"rate"}


def test_no_monster_is_dealt_no_treatment_at_all_with_the_fx_off():
    """`MONSTER_VTL` holds no 0, and that is load-bearing twice over.

    CONTRACTS.md §6 puts it as "never 0%": a monster whose only treatment
    rounded to no treatment is a goblin that sounds like the barmaid. It is
    also what lets a reader — and `tools/polly_check.py`, which checks the
    document it sent without re-casting — conclude "on standard" ⇒ "carries
    the effect". A 0 in this tuple would make one monster in six silently
    untreated and that inference wrong.
    """
    assert 0 not in MONSTER_VTL
    for cast in every_reachable_monster_cast(monster_fx=False):
        assert cast.vtl_pct != 0
        assert "vocal-tract-length" in ssml_for("Fee fi.", cast, "standard")


@pytest.mark.parametrize("monster_fx", [True, False])
@pytest.mark.parametrize("engine", sorted(DOCUMENTED_ENGINE_SSML))
def test_every_monster_a_game_can_deal_is_documented_ssml(engine, monster_fx):
    """The cross product of the monster branch's spreads, in both
    arrangements, on every engine."""
    seen = 0
    for cast in every_reachable_monster_cast(monster_fx):
        assert_documented(ssml_for("The goblin snarls.", cast, engine), engine)
        seen += 1
    assert seen == ((len(MONSTER_SIZE) * len(MONSTER_GROWL) * len(MONSTER_CAVE)
                     * len(MONSTER_TEMPO)) if monster_fx else len(MONSTER_VTL) * 7 * 4)


@pytest.mark.parametrize("engine", sorted(DOCUMENTED_ENGINE_SSML))
def test_every_seat_a_LISTENER_can_ask_for_is_documented_ssml(engine):
    """The casting is no longer the only thing that writes a document.

    A seat can be retuned from the voice lab — a volume, the compressor, a
    rate, a pitch, and on a monster the treatment — and those values reach
    `ssml_for` by the same path a dealt cast does. Everything above walks what
    `cast_for` can DEAL, which is exactly the set that never carries a volume
    or a `drc`: an unsupported tag is an `InvalidSsmlException`, one 502, and a
    seat silently back on the browser's voices, so the tuned documents need the
    same grammar check the dealt ones get.

    The corners of the tune, not a sample of it: both ends of the volume clamp
    and zero, the compressor on and off, against a plain seat and a treated
    one.
    """
    from tts.voices import VOLUME_MAX_DB, VOLUME_MIN_DB, retune, tune_from

    seats = [
        cast_for("dm", STANDARD_ENGLISH, "Brian", "", engine),
        cast_for("pc_1", STANDARD_ENGLISH, "Brian", "", engine),
        cast_for("monster:ogre_2", STANDARD_ENGLISH, "Brian", "", engine, size="L"),
    ]
    seen = 0
    for cast in seats:
        for volume in (VOLUME_MIN_DB, 0, VOLUME_MAX_DB, None):
            for drc in (True, False, None):
                tuned = retune(cast, tune_from(volume=volume, drc=drc), STANDARD_ENGLISH)
                assert_documented(ssml_for("The gate groans open.", tuned, engine), engine)
                seen += 1
    assert seen == len(seats) * 4 * 3


@pytest.mark.parametrize("engine", sorted(DOCUMENTED_ENGINE_SSML))
def test_every_seat_a_game_can_deal_is_documented_ssml(engine):
    """The other branches of `cast_for`, dealt from the pools they can meet:
    the full roster, a roster small enough to take the `few` path (which is the
    only place a non-monster gets a rate), and a single voice."""
    small = STANDARD_ENGLISH[:3]
    keys = ["dm", "npc", "monster:goblin_1", "monster:ogre_2", "monster:wolf_3"]
    keys += [f"pc_{i}" for i in range(1, 9)]
    for pool in (STANDARD_ENGLISH, small, STANDARD_ENGLISH[:1]):
        for key in keys:
            for gender in ("", "female", "male"):
                for age in ("", "child", "adult", 9, 40):
                    for fx in (True, False):
                        cast = cast_for(key, pool, "Brian", gender, engine, age,
                                        monster_fx=fx)
                        assert_documented(
                            ssml_for("Grog & <the> \"boss\" said 'no'.", cast), engine)


def test_a_line_with_xml_in_it_still_parses():
    """`escape` is what stands between a name with an ampersand in it and an
    `InvalidSsmlException`; `assert_documented` above parses, so this states
    the case it is protecting."""
    cast = cast_for("monster:goblin_1", STANDARD_ENGLISH, "Brian")
    doc = ssml_for("Grog & <the> \"boss\" said 'no'.", cast)
    root = ET.fromstring(doc.replace("<speak>", '<speak xmlns:amazon="urn:amazon">', 1))
    assert "".join(root.itertext()) == "Grog & <the> \"boss\" said 'no'."


# -- a Polly that says no ----------------------------------------------------

class StrictPolly:
    """A fake that refuses what the documentation says Polly refuses.

    The suite's other fakes accept anything, which is why an engine-aware SSML
    bug could live in a green tree. This one raises on the two errors the API
    reference lists for this shape of mistake — `InvalidSsmlException` for a
    tag the engine cannot read, `EngineNotSupportedException` for a voice the
    engine does not serve
    (https://docs.aws.amazon.com/polly/latest/APIReference/API_SynthesizeSpeech.html)
    — so a test that drives the real `PollyTTS` through it is testing the
    routing rather than the mock.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def describe_voices(self, **_kw):
        return {
            "Voices": [
                {"Id": vid, "LanguageCode": lang, "Gender": gender,
                 "SupportedEngines": sorted(DOCUMENTED_ENGINES_BY_VOICE[vid])}
                for vid, lang, gender in sorted(DOCUMENTED_STANDARD_ENGLISH)
            ]
        }

    def synthesize_speech(self, **kw):
        self.sent.append(kw)
        engine = kw["Engine"]
        # "Valid values for pcm are '8000' and '16000'" — and the reference
        # lists `InvalidSampleRateException` for anything else. A monster's
        # clip is `pcm` now, so this is a mistake the app can newly make.
        if kw.get("OutputFormat") == "pcm":
            rate = kw.get("SampleRate")
            if rate is not None and rate not in DOCUMENTED_PCM_SAMPLE_RATES:
                raise RuntimeError(f"InvalidSampleRateException: {rate!r} is not valid for pcm")
        if engine not in DOCUMENTED_ENGINE_SSML:
            raise RuntimeError(f"ValidationException: unknown engine {engine!r}")
        if engine not in DOCUMENTED_ENGINES_BY_VOICE.get(kw["VoiceId"], set()):
            raise RuntimeError(
                f"EngineNotSupportedException: {kw['VoiceId']} is not a {engine} voice"
            )
        if kw.get("TextType") == "ssml":
            used = treatments_in(kw["Text"])
            if not used <= DOCUMENTED_ENGINE_SSML[engine]:
                raise RuntimeError(
                    "InvalidSsmlException: "
                    f"{sorted(used - DOCUMENTED_ENGINE_SSML[engine])} on {engine}"
                )
        return {"AudioStream": _Stream(b"\xff\xfb" + kw["Text"].encode())}


class _Stream:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data

    def close(self) -> None:
        pass


def shipped(tmp_path, client):
    """The service as `from_env` builds it on the droplet today."""
    return PollyTTS(
        AudioCache(str(tmp_path), 0),
        client=client,
        engine=DEFAULT_ENGINE,
        monster_engine=DEFAULT_MONSTER_ENGINE,
    )


def with_the_fx_off(tmp_path, client):
    """And as `DND_TTS_MONSTER_FX=0` builds it: the standard-engine split."""
    return PollyTTS(
        AudioCache(str(tmp_path), 0),
        client=client,
        engine=DEFAULT_ENGINE,
        monster_engine="standard",
        monster_fx=False,
    )


def test_the_old_split_still_survives_the_same_polly(tmp_path):
    """`DND_TTS_MONSTER_FX=0` is the escape hatch, so it has to keep working:
    the monster line goes to the standard engine, carrying the one tag that
    exists only there, and comes back as an MP3 like everything else."""
    polly = StrictPolly()
    svc = with_the_fx_off(tmp_path, polly)

    table = svc.synthesize("dm", "The cart still smoulders.")
    monster = svc.synthesize("monster:goblin_1", "You will not leave this cave alive.")

    assert [s["Engine"] for s in polly.sent] == ["neural", "standard"]
    assert all(s["OutputFormat"] == "mp3" for s in polly.sent)
    assert "vocal-tract-length" not in polly.sent[0]["Text"]
    assert "vocal-tract-length" in polly.sent[1]["Text"]

    # Priced at the engine each seat rendered on, not at the table's.
    assert table.usd == pytest.approx(table.chars * 16.0 / 1_000_000)
    assert monster.usd == pytest.approx(monster.chars * 4.0 / 1_000_000)
    assert monster.media_type == "audio/mpeg"


def test_the_shipped_arrangement_survives_a_polly_that_refuses(tmp_path):
    """The end of the audit: a monster line and a table line, through the real
    `PollyTTS` on the shipped defaults, against a Polly that raises the way the
    documented one does."""
    polly = StrictPolly()
    svc = shipped(tmp_path, polly)
    assert (svc.engine, svc.monster_engine) == ("neural", "neural")

    table = svc.synthesize("dm", "The cart still smoulders.")
    monster = svc.synthesize("monster:goblin_1", "You will not leave this cave alive.")

    assert table.cast.engine == "neural" and monster.cast.engine == "neural"
    assert [s["Engine"] for s in polly.sent] == ["neural", "neural"]
    assert all(s["TextType"] == "ssml" for s in polly.sent)

    # The monster asks for the one format that can be treated without a codec,
    # at the only rate `pcm` serves that is worth having; the table takes
    # Polly's own MP3 untouched.
    assert polly.sent[0]["OutputFormat"] == "mp3"
    assert polly.sent[1]["OutputFormat"] == "pcm"
    assert polly.sent[1]["SampleRate"] in DOCUMENTED_PCM_SAMPLE_RATES
    assert polly.sent[1]["SampleRate"] == str(SAMPLE_RATE)

    # Neither line carries a tag this engine would refuse, which is what the
    # old split existed to arrange and what the treatment makes unnecessary.
    assert all("vocal-tract-length" not in s["Text"] for s in polly.sent)

    # One engine, so one rate on the bill.
    assert table.usd == pytest.approx(table.chars * 16.0 / 1_000_000)
    assert monster.usd == pytest.approx(monster.chars * 16.0 / 1_000_000)

    # And the monster comes back as a RIFF, because the treatment happened
    # between Polly and the listener.
    assert monster.media_type == "audio/wav" and monster.audio[:4] == b"RIFF"
    assert table.media_type == "audio/mpeg" and table.audio[:2] == b"\xff\xfb"


def test_the_strict_polly_has_teeth(tmp_path):
    """A negative control: without it the test above passes for a fake that
    never refuses anything, which is the failure mode it exists to rule out."""
    polly = StrictPolly()
    goblin = Cast("monster:goblin_1", "Joey", "en-US", "standard",
                  pitch_pct=-15, rate_pct=95, vtl_pct=30)

    # The monster's own document, sent to the neural engine: the mistake the
    # whole split is arranged to prevent. It has to go to the client by hand,
    # because `PollyTTS` cannot express it — `_synthesize_now` writes the SSML
    # from the `Cast` it is rendering, so the document and the `Engine` on the
    # request are always derived from the same field.
    with pytest.raises(RuntimeError, match="InvalidSsmlException"):
        polly.synthesize_speech(
            Engine="neural", VoiceId="Joey", TextType="ssml", OutputFormat="mp3",
            Text=ssml_for("Fee fi.", goblin, "standard"),
        )

    # A standard-only voice sent to the neural engine does reach the client
    # through the service, and is the other half of what the fake refuses.
    with pytest.raises(TTSError, match="EngineNotSupportedException"):
        shipped(tmp_path, polly)._synthesize_now(
            "Fee fi.", Cast("dm", "Geraint", "en-GB-WLS", "neural")
        )


def test_putting_the_whole_table_on_one_engine_stays_legal(tmp_path):
    """`DND_TTS_MONSTER_ENGINE=DND_TTS_ENGINE` is a supported choice. The
    monster keeps its seat and loses its timbre rather than 502-ing."""
    polly = StrictPolly()
    svc = PollyTTS(AudioCache(str(tmp_path), 0), client=polly,
                   engine="neural", monster_engine="neural")
    result = svc.synthesize("monster:goblin_1", "You will not leave this cave alive.")
    assert result.cast.engine == "neural"
    assert "vocal-tract-length" not in polly.sent[0]["Text"]


def test_a_refused_line_leaves_nothing_behind(tmp_path):
    """A 502 must not be cached: the cache key is content-addressed and served
    `immutable` for a year, so a stored failure would outlive the fault."""
    cache = AudioCache(str(tmp_path), 0)
    svc = PollyTTS(cache, client=StrictPolly(), engine="neural", monster_engine="neural")
    refused = Cast("monster:goblin_1", "Geraint", "en-GB-WLS", "neural")

    with pytest.raises(TTSError, match="EngineNotSupportedException"):
        svc._synthesize_now("Fee fi.", refused)
    assert cache.total_bytes() == 0        # nothing written under any key
