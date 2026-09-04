"""Who a character is, on the sheet that says what they can do.

`gender` was a voice-casting trait living only in the config: the engine did
not carry it, so nothing the DM or the players ever read said whether a
character was a she, a he or a they. A model handed a name and a class fills
that gap by inference, confidently and sometimes wrongly, and the table then
hears a cleric called Father Bexley Crane referred to as "she" for a scene.

So the sheet carries both, and `normalize_pronouns` is the one place that
decides what a config's answer means — which is the same reason
`tts.voices.normalize_age` exists.
"""

from __future__ import annotations

import pytest

from engine.characters import (
    DEFAULT_PRONOUNS,
    CharacterSheet,
    build_character,
    normalize_pronouns,
)
from engine.dice import RNG


def spec(**over):
    base = {"id": "pc_1", "name": "Sadi", "race": "Human", "klass": "Rogue", "level": 3}
    base.update(over)
    return base


def sheet(**over):
    return build_character(spec(**over), RNG(1))


# -- resolving --------------------------------------------------------------

@pytest.mark.parametrize("said", ["she/her", "She/Her", "she", "her", "she/her/hers", " she / her "])
def test_a_set_is_recognised_however_it_is_written(said):
    assert normalize_pronouns(said) == "she/her"


def test_pronouns_a_config_states_beat_the_gender_it_states():
    """The two can disagree, and the character's own word wins. A voice is
    cast from the gender; how the table speaks about them is this."""
    assert normalize_pronouns("they/them", "female") == "they/them"
    assert sheet(gender="male", pronouns="she/her").pronouns == "she/her"


def test_a_stated_gender_implies_a_pronoun_when_none_is_given():
    """The shipped parties state a gender wherever the persona already does
    and no pronouns at all, so without this every one of them would be
    narrated as they/them — including the ones written as Dame and Father."""
    assert sheet(gender="female").pronouns == "she/her"
    assert sheet(gender="m").pronouns == "he/him"


def test_the_gender_words_the_voice_casting_takes_are_the_ones_understood_here():
    """A character read aloud in a woman's voice and narrated as "they" is one
    character described two ways. The two vocabularies are separate modules by
    design — `engine/` may not import `tts/` — so they are held together here
    rather than by a comment."""
    from tts.voices import GENDERS  # a test may cross layers; the engine may not

    for said, canonical in GENDERS.items():
        expected = {"female": "she/her", "male": "he/him"}[canonical]
        assert normalize_pronouns("", said) == expected, said


def test_saying_nothing_is_they_them_rather_than_a_guess():
    """Five of the twenty-eight shipped characters state no gender because
    their persona states none. Inferring one from the name is exactly what
    this field exists to stop."""
    assert sheet().pronouns == DEFAULT_PRONOUNS == "they/them"
    assert normalize_pronouns(None, None) == "they/them"
    assert normalize_pronouns("", "") == "they/them"
    # A gender nobody can read is not half a guess either.
    assert normalize_pronouns("", "unspecified") == "they/them"


def test_a_set_nobody_listed_is_kept_rather_than_corrected():
    """Neopronouns are not a typo, and a narrator handed `ey/em` can use it."""
    assert normalize_pronouns("ey/em") == "ey/em"
    assert normalize_pronouns("xe/xem/xyr") == "xe/xem/xyr"
    # Only the shape is tidied.
    assert normalize_pronouns("  ey //em ") == "ey/em"


# -- carried by the sheet ---------------------------------------------------

def test_the_sheet_carries_both_and_survives_a_round_trip():
    """A game is reloaded from SQLite after a restart; a sheet that lost its
    pronouns there would misgender the character for the rest of the game."""
    original = sheet(gender="female", pronouns="she/her")
    assert original.gender == "female" and original.pronouns == "she/her"
    back = CharacterSheet.from_dict(original.to_dict())
    assert back.gender == "female" and back.pronouns == "she/her"

    # A sheet persisted before this field existed still loads, and resolves the
    # same way a fresh build would.
    old = original.to_dict()
    del old["pronouns"]
    assert CharacterSheet.from_dict(old).pronouns == "she/her"
    del old["gender"]
    assert CharacterSheet.from_dict(old).pronouns == "they/them"


def test_pronouns_change_nothing_mechanical():
    """Gender has no rules meaning in 5e here, and neither does this: the two
    sheets differ in exactly the fields that describe who the character is."""
    a = sheet(gender="female").to_dict()
    b = sheet(gender="male").to_dict()
    differing = {k for k in a if a[k] != b[k]}
    assert differing == {"gender", "pronouns"}
