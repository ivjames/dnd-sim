"""What `advance_turn` does with the dying once the fight is decided."""

from __future__ import annotations

from engine import actions as A
from engine.state import Condition
from tests.engine.conftest import attack, find, kinds, make_mon, make_pc, make_state


def downed(cid: str, pos):
    c = make_pc(cid, "Cleric", pos=pos)
    c.hp = 0
    c.add_condition(Condition("unconscious", source=None))
    c.add_condition(Condition("prone", source=None))
    return c


def test_no_death_save_is_rolled_once_the_last_enemy_is_dead(script):
    gob = make_mon("Goblin", "mon_1", (1, 0))
    gob.hp = 1
    st = make_state(make_pc("pc_1"), gob, downed("pc_2", (0, 1)))
    script(15)
    st, ev = attack(st, "pc_1", "Goblin", "Longsword")
    assert st.combatants["mon_1"].dead and A.combat_over(st) == "party"
    st, ev = A.advance_turn(st)
    assert "death_save" not in kinds(ev), kinds(ev)
    assert st.combatants["pc_2"].death_saves == {"success": 0, "failure": 0}
    assert st.active_id() == "pc_1"  # the only one left who can take a turn


def test_the_death_save_still_rolls_while_the_fight_is_on(script):
    gob = make_mon("Goblin", "mon_1", (5, 0))
    st = make_state(make_pc("pc_1"), gob, downed("pc_2", (0, 1)))
    st, _ = A.advance_turn(st)  # goblin's turn
    script(4)
    st, ev = A.advance_turn(st)  # the cleric's turn comes round: a save, a failure
    assert find(ev, "death_save") and st.combatants["pc_2"].death_saves["failure"] == 1


def test_no_death_save_when_the_party_is_all_down_either(script):
    gob = make_mon("Goblin", "mon_1", (5, 0))
    st = make_state(gob, downed("pc_1", (0, 0)), downed("pc_2", (0, 1)))
    assert A.combat_over(st) == "enemy"
    st, ev = A.advance_turn(st)
    assert "death_save" not in kinds(ev)
