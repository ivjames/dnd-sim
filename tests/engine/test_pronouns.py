"""What the sheet does with `pronouns` and `gender`: carry them, and nothing else.

How the two are *read* — a stated set as written, a legacy gender through
`agents.views.PRONOUNS`, everything else they/them — is decided by
`pronouns_for` and pinned in `tests/orchestrator/test_narration_attribution.py`.
This is the other half: the engine holds both keys, persists both, and lets
neither touch a rule.
"""

from __future__ import annotations

from engine.characters import CharacterSheet, build_character
from engine.dice import RNG


def spec(**over):
    base = {"id": "pc_1", "name": "Sadi", "race": "Human", "klass": "Rogue", "level": 3}
    base.update(over)
    return base


def sheet(**over):
    return build_character(spec(**over), RNG(1))


def test_the_sheet_carries_both_verbatim():
    """Verbatim on purpose: the sheet records what the author wrote, and the
    reader decides what it means. Resolving here would write `they/them` into
    the sheet of a character who said nothing, which is the one thing an
    unstated answer must not become."""
    assert sheet(pronouns="ey/em").pronouns == "ey/em"
    assert sheet(gender="female").gender == "female"
    assert sheet(gender="female").pronouns == ""
    blank = sheet()
    assert blank.pronouns == "" and blank.gender == ""


def test_both_survive_the_round_trip_a_restart_puts_them_through():
    """A game is reloaded from SQLite after a restart; a sheet that lost
    either field there would misgender the character, or re-cast its voice,
    for the rest of the game."""
    original = sheet(pronouns="she/her", gender="female")
    back = CharacterSheet.from_dict(original.to_dict())
    assert back.pronouns == "she/her" and back.gender == "female"

    # A row written before either field existed still loads, and says nothing
    # rather than guessing — which is what `pronouns_for` then reads as
    # they/them.
    old = original.to_dict()
    del old["pronouns"]
    assert CharacterSheet.from_dict(old).pronouns == ""
    del old["gender"]
    assert CharacterSheet.from_dict(old).gender == ""


def test_neither_field_touches_a_rule():
    """Gender has no mechanical meaning in 5e here and neither does a pronoun.
    Two sheets from one spec differ in exactly the fields that describe who the
    character is — not their AC, their HP, their saves or their slots."""
    a = sheet(pronouns="she/her", gender="female").to_dict()
    b = sheet(pronouns="he/him", gender="male").to_dict()
    assert {k for k in a if a[k] != b[k]} == {"pronouns", "gender"}
