"""What a mover walks through, and what the log says about it afterwards.

Every case here comes from one recorded game — `examples/cellar_rats.json` at
seed 11, the brine cellar under the harbourmaster's office. Reading that
transcript, three opportunity attacks looked illegal and a Healing Word looked
like it healed four points more than it rolled. All four were legal. The engine
was right and the transcript could not show it, which is its own defect: a log
that cannot be checked gets disbelieved.

One of the three, though, was a real bug behind a legal move. Giant Rat 3 was
routed *into* a rogue's reach and straight back out of it, for an equal-length
route that would have avoided the rogue entirely, and died to the free attack.
"""

from __future__ import annotations

from .conftest import cast, do, find, make_mon, make_pc, make_state, templates

from engine import actions as A
from engine.dice import RNG
from engine.state import Grid


# The encounter grid from examples/cellar_rats.json.
CELLAR = dict(width=10, height=8,
              difficult={(4, 3), (4, 4), (5, 3), (5, 4)},
              walls={(5, 0), (5, 1), (5, 6), (5, 7)})


def _cellar_state():
    """The board as Giant Rat 3's turn opened, round 1."""
    pib = make_pc("pc_2", "Rogue", name="Pib Underbough", race="Lightfoot Halfling", level=1, pos=(5, 2))
    orla = make_pc("pc_3", "Cleric", name="Deacon Orla Vance", level=1, pos=(5, 3), spells="default")
    emmet = make_pc("pc_4", "Wizard", name="Emmet Sull", level=1, pos=(3, 3), spells="default")
    rat3 = make_mon("Giant Rat", "mon_3", (7, 1), label="Giant Rat 3")
    return make_state(rat3, pib, orla, emmet, grid=Grid(**CELLAR))


def test_equal_length_route_goes_around_a_threatened_square():
    """The recorded bug: (7,1) -> (5,5) went via (6,2), inside Pib's reach.

    (7,2) is the same 20 ft and never enters it. The rat took a rapier through
    the throat to save nothing.
    """
    st = _cellar_state()
    path = st.grid.path(st, (7, 1), (5, 5), 30, mover_id="mon_3",
                        threat=A.threat_map(st, st.combatants["mon_3"]))
    assert path is not None
    assert (6, 2) not in path, f"still routed through the rogue's reach: {path}"
    assert st.grid.path_cost(path) == 20, "the detour must not cost a foot more"


def test_the_rat_no_longer_walks_through_the_rogue():
    """End to end, on the recorded board.

    Orla's swing as the rat leaves (6,4) survives, and should: every route to
    (5,5) that dodges her costs 25 ft against this one's 20, and feet stay the
    primary cost. Pib's does not survive, because it never bought anything.
    """
    st = _cellar_state()
    st, ev = do(st, "mon_3", templates(st, "mon_3", "move")[0], path=[[5, 5]])
    oas = [e for e in ev if e.kind == "attack" and e.data.get("opportunity")]
    assert all(e.actor != "pc_2" for e in oas), f"still gave the rogue a free rapier: {oas}"
    walked = [tuple(sq) for sq in find(ev, "move").data["path"]]
    assert (6, 2) not in walked, f"still routed through the rogue's reach: {walked}"


def test_an_unavoidable_opportunity_attack_still_happens(script):
    """The tie-break must not become a way out of a reach you are standing in."""
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))
    script(15)
    st, ev = do(st, "pc_1", templates(st, "pc_1", "move")[0], path=[[4, 0]])
    oa = find(ev, "attack")
    assert oa and oa.data["opportunity"]


def test_the_opportunity_attack_names_the_step_that_provoked_it(script):
    """The move event reports where the mover stopped, not what it was leaving.

    Without the squares in the attack line, a reader compares the attacker to
    the move's `from`/`to` and concludes the engine fired on a creature moving
    *into* reach.
    """
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))
    script(15)
    _, ev = do(st, "pc_1", templates(st, "pc_1", "move")[0], path=[[4, 0]])
    oa = find(ev, "attack")
    assert "leaving reach (" in oa.text and "→" in oa.text


def test_a_heal_shows_the_modifier_it_added(script):
    """"1d4 → 1" beside "regains 7 HP" is arithmetic nobody can check."""
    orla = make_pc("pc_3", "Cleric", name="Deacon Orla Vance", level=1, pos=(0, 0), spells="default")
    emmet = make_pc("pc_4", "Wizard", name="Emmet Sull", level=1, pos=(1, 0), spells="default")
    emmet.hp = 1
    st = make_state(orla, emmet)
    _, ev = cast(st, "pc_3", "Healing Word", "Emmet")
    heal = find(ev, "heal")
    assert heal is not None
    roll = heal.data["roll"] if "roll" in heal.data else None
    assert " + " in heal.text, f"no modifier shown: {heal.text}"
    # the sum in the text is the HP that went in
    die, mod = heal.text.split("(")[1].split(")")[0].split(" + ")
    assert int(die.split("→")[1]) + int(mod) >= heal.data["amount"]


def test_a_crit_prints_the_dice_it_actually_rolled():
    """1d4+2 doubled is 2d4+2; printing "1d4+2 (crit)" reads as a missing double."""
    r = RNG(3).roll_damage("1d8+3", crit=True)
    assert r.expr == "2d8+3 (crit)"
    assert len(r.rolls) == 2
    assert RNG(3).roll_damage("1d8+3", crit=False).expr == "1d8+3"
