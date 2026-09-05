"""SRD surprise: a surprised creature cannot move or act on its first turn of
the combat, and cannot take a reaction until that turn ends."""

from __future__ import annotations

import pytest

from engine import actions as A
from tests.engine.conftest import attack, do, find, kinds, make_mon, make_pc, make_state, templates


def opening(script, *combatants, surprised=None, faces=(20, 1, 1, 1)):
    """Combat started with the first combatant winning initiative."""
    st = make_state(*combatants, start=False)
    script(*faces)
    return A.start_combat(st, None, surprised=surprised)


def test_the_surprised_side_is_marked_and_the_events_say_so(script):
    st, ev = opening(script, make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)),
                     make_mon("Goblin", "mon_2", (5, 5), label="Goblin 2"), surprised="enemy")
    start = find(ev, "combat_start")
    assert start.data["surprised"] == "enemy" and "surprised" in start.text
    marks = [e for e in ev if e.kind == "condition_add" and e.data.get("condition") == "surprised"]
    assert sorted(e.actor for e in marks) == ["mon_1", "mon_2"]
    assert all(st.combatants[m].has_condition("surprised") for m in ("mon_1", "mon_2"))
    assert not st.combatants["pc_1"].has_condition("surprised")
    assert st.active_id() == "pc_1"


def test_nobody_is_surprised_by_default(script):
    st, ev = opening(script, make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))
    assert find(ev, "combat_start").data["surprised"] is None
    assert not any(c.has_condition("surprised") for c in st.combatants.values())
    st2, _ = opening(script, make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)), surprised="party")
    assert st2.combatants["pc_1"].has_condition("surprised") and not st2.combatants["mon_1"].has_condition("surprised")


def test_an_unknown_side_is_refused(script):
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)), start=False)
    with pytest.raises(A.IllegalAction, match="surprised must be"):
        A.start_combat(st, None, surprised="goblins")


def test_a_surprised_creature_can_only_end_its_first_turn(script):
    st, _ = opening(script, make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)), surprised="enemy")
    st, _ = A.advance_turn(st)  # the goblin's first turn
    assert st.active_id() == "mon_1"
    tpls = A.legal_actions(st, "mon_1")
    assert [t.type for t in tpls] == ["end_turn"] and "surprised" in tpls[0].label
    st, ev = A.advance_turn(st)  # ...ends: the surprise is spent
    gone = find(ev, "condition_remove")
    assert gone and gone.data["condition"] == "surprised" and gone.actor == "mon_1"
    assert not st.combatants["mon_1"].has_condition("surprised")
    st, _ = A.advance_turn(st)  # round 2: a normal turn
    assert st.active_id() == "mon_1" and any(t.type == "attack" for t in A.legal_actions(st, "mon_1"))


def test_no_reaction_until_the_first_turn_ends(script):
    st, _ = opening(script, make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)), surprised="enemy")
    assert not A.reactions_for(st, {"type": "move", "mover": "pc_1", "from": (0, 0), "to": (3, 0)})
    assert not A.threat_map(st, st.combatants["pc_1"])
    st, ev = do(st, "pc_1", templates(st, "pc_1", "move")[0], path=[[4, 0]])  # leaves reach: no swing
    assert find(ev, "attack") is None and st.combatants["pc_1"].position == (4, 0)
    st, _ = A.advance_turn(st)  # goblin's first turn
    st, _ = A.advance_turn(st)  # ...over; round 2, the fighter again
    assert st.active_id() == "pc_1"
    script(15)
    st, ev = do(st, "pc_1", templates(st, "pc_1", "move")[0], path=[[2, 0], [4, 0]])  # in and out again
    oa = find(ev, "attack")
    assert oa and oa.data["opportunity"] and oa.actor == "mon_1"


def test_a_surprised_creature_downed_before_its_turn_loses_the_marker_when_the_turn_passes(script):
    pc = make_pc("pc_1")
    pc.hp = 1
    st, _ = opening(script, make_mon("Goblin", "mon_1", (1, 0)), pc, make_pc("pc_2", "Wizard", pos=(9, 9)),
                    surprised="party")
    assert st.active_id() == "mon_1" and st.combatants["pc_1"].has_condition("surprised")
    script(15, 12)  # the scimitar lands; the fighter's death save on its skipped turn
    st, ev = attack(st, "mon_1", "Fighter pc_1", "Scimitar")
    assert find(ev, "down")
    st, ev = A.advance_turn(st)
    assert "death_save" in kinds(ev) and st.active_id() == "pc_2"
    assert not st.combatants["pc_1"].has_condition("surprised")


def test_surprise_survives_a_state_round_trip(script):
    st, _ = opening(script, make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)), surprised="enemy")
    from engine.state import GameState
    again = GameState.from_dict(st.to_dict())
    assert again.combatants["mon_1"].has_condition("surprised")
    assert not A.reactions_for(again, {"type": "move", "mover": "pc_1", "from": (0, 0), "to": (3, 0)})
