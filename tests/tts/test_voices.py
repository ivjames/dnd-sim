"""Who says a line, and how it is written down for Polly.

Pure: no AWS account, no network. The one thing here that reaches outside is
the parity check against `web/static/speech.js`, which matters because the two
halves of the narrator have to agree on which actor sits in which seat — the
browser sends the voice key, the server deals the voice.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess

import pytest

from tts.voices import (
    ACCENTS,
    CHILD_VOICE_IDS,
    DEFAULT_SIZE_BAND,
    ENGINE_SSML,
    MONSTER_CAVE,
    MONSTER_GROWL,
    MONSTER_SIZE,
    MONSTER_SIZE_BANDS,
    MONSTER_TEMPO,
    MONSTER_VTL,
    STANDARD_ENGLISH,
    Cast,
    Voice,
    allowed_ssml,
    billable_chars,
    cast_for,
    gender_for_pronouns,
    hash_key,
    is_child_voice,
    normalize_age,
    normalize_creature_size,
    normalize_gender,
    accent_for,
    ssml_for,
)

CHILDREN = {v.id for v in STANDARD_ENGLISH if is_child_voice(v)}

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPEECH_JS = os.path.join(ROOT, "web", "static", "speech.js")

KEYS = ["dm", "npc", "pc_1", "pc_thorin", "monster:goblin_1", "monster:ogre_1", "Zoë", ""]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_hash_matches_the_browsers():
    """FNV-1a over UTF-16 code units, the same number on both sides.

    If these ever diverge the game still speaks — it just casts the wizard in
    the goblin's voice, which is the kind of bug nobody reports as a bug.
    """
    script = (
        "const S = require(%s);\n"
        "process.stdout.write(JSON.stringify(JSON.parse(process.argv[1]).map(S.hashString)));\n"
    ) % json.dumps(SPEECH_JS)
    proc = subprocess.run(
        ["node", "-e", script, json.dumps(KEYS)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [hash_key(k) for k in KEYS]


def test_the_dm_is_chosen_and_nobody_else_gets_that_voice():
    dm = cast_for("dm", STANDARD_ENGLISH, "Brian")
    assert dm.voice_id == "Brian" and dm.pitch_pct == 0 and dm.rate_pct == 100
    others = {cast_for(k, STANDARD_ENGLISH, "Brian").voice_id for k in
              ("npc", "pc_1", "pc_2", "pc_3", "monster:goblin_1")}
    assert "Brian" not in others

    # An unknown name is not silently honoured: the pool decides, alphabetically.
    assert cast_for("dm", STANDARD_ENGLISH, "Gandalf").voice_id == "Aditi"
    assert cast_for("dm", STANDARD_ENGLISH, "brian").voice_id == "Brian"    # case is not a spelling


def test_the_same_actor_lands_in_the_same_seat_every_time():
    a = cast_for("pc_thorin", STANDARD_ENGLISH, "Brian")
    b = cast_for("pc_thorin", list(reversed(STANDARD_ENGLISH)), "Brian")
    assert a == b                     # and the pool's order does not decide it
    assert isinstance(a, Cast) and a.key == "pc_thorin"


def test_a_monster_never_just_sounds_like_a_person():
    """Polly has no novelty voices, so the costume is made afterwards."""
    for ident in ("goblin_1", "ogre_1", "wolf_1", "dragon"):
        c = cast_for("monster:" + ident, STANDARD_ENGLISH, "Brian")
        assert c.fx is not None
        # The size shift is the one every monster gets, so no monster is dealt
        # nothing: grit and a room are characteristics, not a uniform.
        assert c.fx.size_pct in MONSTER_SIZE and c.fx.size_pct != 0
        assert c.fx.growl_pct in MONSTER_GROWL and c.fx.cave_pct in MONSTER_CAVE
        # The rate carries the compensation for what the size shift does to
        # duration, times how fast this one talks.
        assert any(c.rate_pct == round(c.fx.rate_pct() * t / 100) for t in MONSTER_TEMPO)
        # And none of the standard-only SSML, which is the point of it.
        assert c.vtl_pct == 0 and c.pitch_pct == 0
    # An ordinary seat is never given the treatment.
    for key in ("dm", "npc", "pc_1"):
        assert cast_for(key, STANDARD_ENGLISH, "Brian").fx is None


def test_how_big_a_monster_sounds_is_the_creature_and_not_the_slot():
    """The bug this fixes, stated as the case that showed it.

    A voice key is `monster:mon_N` where N is spawn order, so a hash over the
    key alone knows only which slot a creature took. In `gnoll_pyre` that put
    the Ogre (Large) at +9% and a Gnoll (Medium) at +34%: the gnoll sounded
    bigger than the ogre. The band now comes from the stat block.
    """
    ogre = cast_for("monster:mon_6", STANDARD_ENGLISH, "Brian", size="L")
    gnoll = cast_for("monster:mon_3", STANDARD_ENGLISH, "Brian", size="M")
    goblin = cast_for("monster:mon_3", STANDARD_ENGLISH, "Brian", size="S")
    assert ogre.fx.size_pct > gnoll.fx.size_pct > goblin.fx.size_pct

    # Whatever slot each of them lands in — the property has to hold for the
    # whole roster, not for the one arrangement that showed the bug.
    for slot in range(1, 40):
        by_size = [cast_for(f"monster:mon_{slot}", STANDARD_ENGLISH, "Brian", size=sz).fx.size_pct
                   for sz in ("T", "S", "M", "L", "H", "G")]
        assert by_size == sorted(by_size), (slot, by_size)
        assert len(set(by_size)) == len(by_size)      # and never a tie across bands


def test_two_of_one_creature_still_differ_from_each_other():
    """The band is the creature's; the value within it is this creature's.
    Four goblins in a fight must not be one goblin four times."""
    goblins = [cast_for(f"monster:mon_{i}", STANDARD_ENGLISH, "Brian", size="S")
               for i in range(1, 9)]
    assert len({c.fx.size_pct for c in goblins}) > 1
    # ...and every one of them is still a goblin.
    assert all(c.fx.size_pct in MONSTER_SIZE_BANDS["S"] for c in goblins)


def test_the_bands_are_monotonic_and_do_not_overlap():
    """"Bigger creature" and "lower voice" cannot be allowed to disagree, and
    an overlap at a boundary is exactly how they would: a Large at the bottom
    of its band sounding like a Medium at the top of its."""
    order = ["T", "S", "M", "L", "H", "G"]
    assert list(MONSTER_SIZE_BANDS) == order
    top = None
    for name in order:
        band = MONSTER_SIZE_BANDS[name]
        assert band == tuple(sorted(band)) and len(set(band)) == len(band)
        assert 0 not in band                       # no monster is dealt no treatment
        if top is not None:
            assert min(band) > top
        top = max(band)
    # Every value the union claims, and nothing else.
    assert set(MONSTER_SIZE) == {v for b in MONSTER_SIZE_BANDS.values() for v in b}


def test_a_size_nothing_states_is_the_default_band():
    """An unknown creature, or a replay whose snapshot cannot name the speaker.
    It has to be a real band rather than a silence or a spread of its own."""
    for said in ("", None, "colossal", "?", 7):
        cast = cast_for("monster:mon_1", STANDARD_ENGLISH, "Brian", size=said)
        assert cast.fx.size_pct in MONSTER_SIZE_BANDS[DEFAULT_SIZE_BAND]
    assert cast_for("monster:mon_1", STANDARD_ENGLISH, "Brian") == \
        cast_for("monster:mon_1", STANDARD_ENGLISH, "Brian", size=DEFAULT_SIZE_BAND)


def test_a_stated_size_is_read_the_two_ways_anything_writes_it():
    """The SRD letter is what `engine/data/monsters.json` carries; the word is
    what a person writes. Both reach this from game state."""
    assert normalize_creature_size("L") == normalize_creature_size("large") == "L"
    assert normalize_creature_size("  Large  ") == "L"
    assert normalize_creature_size("GARGANTUAN") == "G"
    for junk in ("", None, "big", "XL", 3, True):
        assert normalize_creature_size(junk) == ""


def test_size_is_read_for_monsters_and_nobody_else():
    """A PC has a stat sheet, not a stat block, and no size to be cast by."""
    for key in ("dm", "npc", "pc_1"):
        assert cast_for(key, STANDARD_ENGLISH, "Brian", size="G") == \
            cast_for(key, STANDARD_ENGLISH, "Brian")


def test_the_old_standard_only_treatment_is_still_there_behind_the_switch():
    """`DND_TTS_MONSTER_FX=0`, which `PollyTTS` passes through as
    `monster_fx=False`: no post-processing, and the SSML that needed the
    standard engine back in its place."""
    for ident in ("goblin_1", "ogre_1", "wolf_1", "dragon"):
        c = cast_for("monster:" + ident, STANDARD_ENGLISH, "Brian", monster_fx=False)
        assert c.fx is None
        assert c.vtl_pct in MONSTER_VTL and c.vtl_pct != 0
        assert -20 <= c.pitch_pct <= 10 and 90 <= c.rate_pct <= 105
    # It changes nothing for anyone else.
    for key in ("dm", "npc", "pc_1"):
        assert cast_for(key, STANDARD_ENGLISH, "Brian", monster_fx=False) == \
            cast_for(key, STANDARD_ENGLISH, "Brian")


def test_a_thin_pool_leans_on_pitch_instead():
    """Two voices installed is the case `speech.js` also has to survive."""
    pair = [Voice("Joanna", "en-US"), Voice("Brian", "en-GB")]
    casts = [cast_for(k, pair, "Brian") for k in ("pc_1", "pc_2", "pc_3")]
    assert {c.voice_id for c in casts} == {"Joanna"}          # Brian is the DM
    assert any(c.rate_pct != 100 for c in casts)              # so rate has to tell them apart

    one = cast_for("pc_1", [Voice("Joanna", "en-US")], "Joanna")
    assert one.voice_id == "Joanna"     # a one-voice pool still speaks
    with pytest.raises(ValueError):
        cast_for("pc_1", [], "Joanna")  # an empty one is a bug, not a silence


def test_ssml_is_well_formed_and_escaped():
    plain = cast_for("dm", STANDARD_ENGLISH, "Brian")
    assert ssml_for("Thorin swings.", plain) == "<speak>Thorin swings.</speak>"

    # Polly rejects a malformed document, so an ampersand in a name would be a
    # line the spectator never hears.
    said = ssml_for("Grog & <the> \"boss\"", plain)
    assert "&amp;" in said and "&lt;the&gt;" in said and "&quot;" in said
    assert "<the>" not in said

    # A treated monster writes only a rate — the size shift is not markup.
    monster = cast_for("monster:ogre_1", STANDARD_ENGLISH, "Brian")
    assert ssml_for("Fee fi fo.", monster) == \
        f'<speak><prosody rate="{monster.rate_pct}%">Fee fi fo.</prosody></speak>'

    untreated = cast_for("monster:ogre_1", STANDARD_ENGLISH, "Brian", monster_fx=False)
    doc = ssml_for("Fee fi fo.", untreated)
    assert doc.startswith("<speak><amazon:effect vocal-tract-length=")
    assert "<prosody" in doc and doc.endswith("</amazon:effect></speak>")


def test_only_the_words_are_billed():
    """"SSML tags are not counted as billed characters" — the Polly quotas page."""
    text = "The cart still smoulders."
    doc = ssml_for(text, cast_for("monster:ogre_1", STANDARD_ENGLISH, "Brian"))
    assert billable_chars(text) == len(text) < len(doc)


def test_a_stated_gender_narrows_the_pool_and_nothing_else():
    women = {v.id for v in STANDARD_ENGLISH if v.gender == "Female"}
    men = {v.id for v in STANDARD_ENGLISH if v.gender == "Male"} - {"Brian"}   # Brian is the DM

    for key in ("pc_1", "pc_2", "pc_3", "pc_4", "npc"):
        assert cast_for(key, STANDARD_ENGLISH, "Brian", "female").voice_id in women
        assert cast_for(key, STANDARD_ENGLISH, "Brian", "male").voice_id in men

    # Narrowing is all it does: the seat is still dealt by the same hash, so a
    # character keeps its voice as long as its gender and the roster hold.
    once = cast_for("pc_2", STANDARD_ENGLISH, "Brian", "female")
    assert once == cast_for("pc_2", list(reversed(STANDARD_ENGLISH)), "Brian", "FEMALE")
    assert once.pitch_pct == cast_for("pc_2", STANDARD_ENGLISH, "Brian").pitch_pct


def test_an_unstated_gender_is_not_a_guess():
    """Polly's roster is Female and Male; there is no third voice to cast.

    A character who is neither is dealt from the whole pool rather than pushed
    into one of the two.
    """
    open_pool = cast_for("pc_1", STANDARD_ENGLISH, "Brian")
    for said in ("", None, "nonbinary", "they/them", "unspecified", "  "):
        assert cast_for("pc_1", STANDARD_ENGLISH, "Brian", said) == open_pool

    assert normalize_gender("Female") == "female" and normalize_gender(" M ") == "male"
    assert normalize_gender("nonbinary") == ""


def test_pronouns_name_a_pool_and_that_is_all_they_name():
    """A party spec states pronouns; `gender_for_pronouns` is the one place
    that turns them into a set of voices, and it reads the first pronoun so
    "he/him", "he/him/his" and a bare "He" are one answer."""
    for said in ("he/him", "He/Him", " he/him/his ", "he", "he / him", "he him"):
        assert gender_for_pronouns(said) == "male", said
    for said in ("she/her", "SHE/HER", "she/her/hers", "she"):
        assert gender_for_pronouns(said) == "female", said

    # Written the way it is read: the pool, not the character.
    women = {v.id for v in STANDARD_ENGLISH if v.gender == "Female"}
    men = {v.id for v in STANDARD_ENGLISH if v.gender == "Male"} - {"Brian"}
    for key in ("pc_1", "pc_2", "pc_3", "pc_4", "npc"):
        assert cast_for(key, STANDARD_ENGLISH, "Brian",
                        gender_for_pronouns("she/her")).voice_id in women
        assert cast_for(key, STANDARD_ENGLISH, "Brian",
                        gender_for_pronouns("he/him")).voice_id in men


def test_pronouns_the_roster_has_no_voice_for_leave_the_pool_whole():
    """Polly reports `Female` and `Male` and there is no third voice to deal.

    they/them, a neopronoun set and a stated nothing are all cast from the
    whole roster — the same casting, because the alternative is to push a
    character into one of two boxes the roster happens to have and call that a
    fact about them.
    """
    open_pool = cast_for("pc_1", STANDARD_ENGLISH, "Brian")
    for said in ("they/them", "they", "ze/hir", "xe/xem", "any", "", None, "  ", "—"):
        assert gender_for_pronouns(said) == "", said
        assert cast_for("pc_1", STANDARD_ENGLISH, "Brian", gender_for_pronouns(said)) == open_pool

    # A multi-set is read by its first pronoun, which is the one its author
    # put first: "she/they" narrows, "they/she" does not.
    assert gender_for_pronouns("she/they") == "female"
    assert gender_for_pronouns("they/she") == ""


def test_a_gender_the_roster_cannot_answer_is_a_worse_match_not_a_silence():
    """Korean and Swedish ship one standard voice; Icelandic ships two men."""
    only_women = [Voice("Astrid", "sv-SE", "Female"), Voice("Elin", "sv-SE", "Female")]
    cast = cast_for("pc_1", only_women, "Astrid", "male")
    assert cast.voice_id == "Elin"          # dealt anyway, rather than raising


# -- age ---------------------------------------------------------------------

def test_the_fallback_roster_knows_which_of_its_voices_are_children():
    """Ivy and Kevin, and nothing else on it.

    `DescribeVoices` reports Gender and nothing about age, so this is the one
    part of the roster that cannot be read live — if the table in `voices.py`
    is wrong, the casting is wrong and nothing else notices.
    """
    assert CHILDREN == {"Ivy", "Kevin"}
    assert is_child_voice("justin") and is_child_voice(Voice("Ivy", "en-US", "Female"))
    assert not is_child_voice("Joey") and not is_child_voice("")


def test_a_child_voice_is_dealt_only_to_a_character_who_asks_for_one():
    """The bug this exists for: a cleric called Father Bexley read by a
    nine-year-old, because a child's voice sat in the pool every seat is dealt
    from and one seat in eight hashed onto it."""
    for key in ("dm", "npc", "pc_1", "pc_2", "pc_3", "pc_4", "monster:goblin_1"):
        for gender in ("", "female", "male"):
            for age in ("", None, "adult", "elder", 40, "31"):
                cast = cast_for(key, STANDARD_ENGLISH, "Brian", gender, "standard", age)
                assert cast.voice_id not in CHILDREN, (key, gender, age)


def test_a_character_who_is_a_child_gets_a_childs_voice():
    for age in ("child", "kid", "Child", 9, "11", 12):
        assert cast_for("pc_1", STANDARD_ENGLISH, "Brian", "", "standard", age).voice_id in CHILDREN
    # And gender still narrows within that: Ivy is the roster's only girl,
    # Kevin its only boy.
    assert cast_for("pc_1", STANDARD_ENGLISH, "Brian", "female", "standard", "child").voice_id == "Ivy"
    assert cast_for("pc_1", STANDARD_ENGLISH, "Brian", "male", "standard", "child").voice_id == "Kevin"


def test_an_age_is_read_for_intent_not_guessed_at():
    assert normalize_age("child") == "child" and normalize_age(" KID ") == "child"
    assert normalize_age(9) == "child" and normalize_age("12") == "child"
    assert normalize_age(13) == "adult" and normalize_age("40.5") == "adult"
    # Polly has no elderly voice, so "elder" is recorded and cast as an adult.
    assert normalize_age("elder") == "adult" and normalize_age("elderly") == "adult"
    # Nothing said, and nothing that can be read as an age, are the same thing:
    # no constraint. Both cast as an adult, because that is what the pool does.
    for said in ("", None, "  ", "ancient-ish", "old enough", True, False, 0, -3, 4000,
                 float("nan"), float("inf"), "1e999", 10 ** 400, [12], {"years": 9}):
        assert normalize_age(said) == "", said


def test_a_roster_with_no_children_still_casts_a_child():
    """Only en-US has children's voices; every other language ships none.

    A worse match, not a silence — the same trade as an unanswerable gender.
    """
    british = [v for v in STANDARD_ENGLISH if v.language == "en-GB"]
    cast = cast_for("pc_1", british, "Brian", "", "standard", "child")
    assert cast.voice_id in {"Amy", "Emma"}


def test_the_narrator_nobody_named_is_not_a_nine_year_old():
    """`voices[0]` is the DM's fallback, and on the en-US roster alone that is
    Ivy. A chosen name is still honoured, whatever age it is."""
    american = [v for v in STANDARD_ENGLISH if v.language == "en-US"]
    assert sorted(v.id for v in american)[0] == "Ivy"          # what it used to take
    assert cast_for("dm", american, "").voice_id == "Joanna"
    assert cast_for("dm", american, "Ivy").voice_id == "Ivy"   # asking for Ivy is asking for Ivy


def test_each_engine_is_only_sent_what_it_accepts():
    """Polly errors on an unsupported tag rather than ignoring it, so a line
    written for the wrong engine is a 502 and a fallback, not a flat reading.

    `pitch` and `vocal-tract-length` are standard-only; `rate` survives on
    neural and long-form; generative gets neither, because its prosody tag is
    documented as full-sentences-only and a chunk can be a fragment.
    """
    # The untreated cast, which is the only one that writes all three.
    monster = cast_for("monster:goblin_1", STANDARD_ENGLISH, "Brian", monster_fx=False)
    assert monster.pitch_pct and monster.rate_pct != 100 and monster.vtl_pct

    standard = ssml_for("Fee fi.", monster, "standard")
    assert "vocal-tract-length" in standard and "pitch=" in standard and "rate=" in standard

    for engine in ("neural", "long-form"):
        said = ssml_for("Fee fi.", monster, engine)
        assert "vocal-tract-length" not in said and "pitch=" not in said
        assert 'rate="95%"' in said

    assert ssml_for("Fee fi.", monster, "generative") == "<speak>Fee fi.</speak>"
    # An engine nobody has heard of is written for the one this is built around
    # rather than sent bare — being wrong loudly beats being wrong quietly.
    assert ssml_for("Fee fi.", monster, "chorus") == standard
    # And that default belongs to ONE function, because more than one caller
    # needs the answer: `tools/polly_check.py` reports what the document will
    # contain, and spelled the fallback as "no tags" while `ssml_for` went on
    # writing a vocal-tract-length for the same engine.
    assert allowed_ssml("chorus") == allowed_ssml("standard") == ENGINE_SSML["standard"]
    assert allowed_ssml("") == ENGINE_SSML["standard"]
    assert allowed_ssml("NEURAL") == ENGINE_SSML["neural"]      # named, and case-folded
    for engine, tags in ENGINE_SSML.items():
        assert allowed_ssml(engine) == tags


def test_the_engine_travels_with_the_cast():
    """A line cast for one engine and rendered on another is a 502.

    Keeping the two together is why `Cast` carries the engine rather than
    `ssml_for` taking it from a setting that may have moved on.
    """
    monster = cast_for("monster:ogre_1", STANDARD_ENGLISH, "Brian", "", "standard",
                       monster_fx=False)
    assert monster.engine == "standard"
    assert "vocal-tract-length" in ssml_for("Fee fi.", monster)

    on_neural = cast_for("monster:ogre_1", STANDARD_ENGLISH, "Brian", "", "neural",
                         monster_fx=False)
    assert on_neural.engine == "neural"
    assert "vocal-tract-length" not in ssml_for("Fee fi.", on_neural)

    # Same seat, same voice — only what can be said about it differs.
    assert on_neural.voice_id == monster.voice_id
    assert on_neural.cache_key() != monster.cache_key()


def test_every_voice_on_the_roster_can_say_where_it_is_from():
    """The panel prints this next to a character's name, so a roster voice
    without an accent would print a language tag at a reader."""
    for voice in STANDARD_ENGLISH:
        accent = accent_for(voice.language)
        assert accent, voice.id
        assert accent != voice.language, f"{voice.id} has no accent name for {voice.language}"
    # Welsh-accented English is not Welsh, and is keyed on the full code.
    assert accent_for("en-GB-WLS") == "Welsh"
    assert accent_for("en-GB") == "British"


def test_an_accent_nobody_has_named_is_reported_rather_than_guessed():
    """`voices()` reads the live roster, so a locale Amazon adds tomorrow has
    to be describable — and a guess would eventually be a lie about which
    voice a listener is hearing."""
    assert accent_for("en-GB-SCT") == "en-GB-SCT"
    assert accent_for("") == ""
    assert accent_for(None) == ""
    # Case is Polly's, not ours.
    assert accent_for("EN-us") == ACCENTS["en-us"]


def test_no_two_monsters_in_a_shipped_scenario_sound_alike():
    """The property the bands could plausibly have broken.

    Narrowing the size shift to the creature's own band takes distinctness out
    of it: four goblins now sit within six percent of each other where they
    used to span the whole palette. That is correct — four goblins SHOULD sound
    alike in size — but only if what is left (the voice, the grit, the room,
    the tempo) still tells them apart. So this walks every creature in every
    shipped scenario and asserts no two of them are dealt the same everything.

    The spawn order is `orchestrator/game.py: _spawn_monsters`' — encounters in
    order, `mon_N` counting up across the whole game — and is restated here
    rather than imported, because `tts/` may not import the orchestrator. If
    that numbering ever changes this keeps passing while testing the wrong
    roster; what it cannot do is pass on a roster that has a collision in it.
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "engine", "data", "monsters.json")) as fh:
        sizes = {m["name"]: m.get("size") for m in json.load(fh)}

    scenarios = sorted(glob.glob(os.path.join(root, "examples", "*.json")))
    assert scenarios, "no scenarios to check"
    for path in scenarios:
        with open(path) as fh:
            scenario = json.load(fh).get("scenario") or {}
        seen: dict[tuple, str] = {}
        slot = 0
        for encounter in scenario.get("encounters") or []:
            for entry in encounter.get("monsters") or []:
                for _ in range(int(entry.get("count", 1))):
                    slot += 1
                    cast = cast_for(f"monster:mon_{slot}", STANDARD_ENGLISH, "Brian",
                                    size=sizes.get(entry["name"], ""))
                    fx = cast.fx
                    heard = (cast.voice_id, fx.size_pct, fx.growl_pct, fx.cave_pct,
                             cast.rate_pct)
                    who = f"mon_{slot} ({entry['name']})"
                    assert heard not in seen, (
                        f"{os.path.basename(path)}: {who} sounds exactly like "
                        f"{seen[heard]}"
                    )
                    seen[heard] = who
