"""Who says a line, and how it is written down for Polly.

Pure: no AWS account, no network. The one thing here that reaches outside is
the parity check against `web/static/speech.js`, which matters because the two
halves of the narrator have to agree on which actor sits in which seat — the
browser sends the voice key, the server deals the voice.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tts.voices import (
    CHILD_VOICE_IDS,
    MONSTER_VTL,
    STANDARD_ENGLISH,
    Cast,
    Voice,
    billable_chars,
    cast_for,
    gender_for_pronouns,
    hash_key,
    is_child_voice,
    normalize_age,
    normalize_gender,
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
    """Polly has no novelty voices, so the costume is the vocal tract."""
    for ident in ("goblin_1", "ogre_1", "wolf_1", "dragon"):
        c = cast_for("monster:" + ident, STANDARD_ENGLISH, "Brian")
        assert c.vtl_pct in MONSTER_VTL and c.vtl_pct != 0
        assert -20 <= c.pitch_pct <= 10 and 90 <= c.rate_pct <= 105
    # An ordinary seat is never given the treatment.
    for key in ("dm", "npc", "pc_1"):
        assert cast_for(key, STANDARD_ENGLISH, "Brian").vtl_pct == 0


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

    monster = cast_for("monster:ogre_1", STANDARD_ENGLISH, "Brian")
    doc = ssml_for("Fee fi fo.", monster)
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
    monster = cast_for("monster:goblin_1", STANDARD_ENGLISH, "Brian")
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


def test_the_engine_travels_with_the_cast():
    """A line cast for one engine and rendered on another is a 502.

    Keeping the two together is why `Cast` carries the engine rather than
    `ssml_for` taking it from a setting that may have moved on.
    """
    monster = cast_for("monster:ogre_1", STANDARD_ENGLISH, "Brian", "", "standard")
    assert monster.engine == "standard"
    assert "vocal-tract-length" in ssml_for("Fee fi.", monster)

    on_neural = cast_for("monster:ogre_1", STANDARD_ENGLISH, "Brian", "", "neural")
    assert on_neural.engine == "neural"
    assert "vocal-tract-length" not in ssml_for("Fee fi.", on_neural)

    # Same seat, same voice — only what can be said about it differs.
    assert on_neural.voice_id == monster.voice_id
    assert on_neural.cache_key() != monster.cache_key()
