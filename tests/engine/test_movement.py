"""Moves that fail part-way, and the Spirit Guardians entry trigger.

Both came out of live games: every rejected action in a corpus of sixteen was
a move refused whole for a waypoint chain that overspent by one leg or ended
on someone else's square, and creatures walked into a Spirit Guardians aura
without a save because only the start-of-turn trigger existed.
"""

from __future__ import annotations

import pytest

from engine import actions as A
from engine.state import Grid
from tests.engine.conftest import do, find, kinds, make_mon, make_pc, make_state, templates


def move(st, cid, path):
    tpl = templates(st, cid, "move")
    assert tpl, [t.label for t in A.legal_actions(st, cid)]
    return do(st, cid, tpl[0], path=path)


# ---------------------------------------------------------------- partial moves
def test_a_chain_that_overspends_is_walked_as_far_as_it_goes():
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (15, 15)))
    st, ev = move(st, "pc_1", [[3, 0], [6, 0], [9, 0]])  # 45 ft asked of a 30 ft speed
    mv = find(ev, "move")
    assert st.combatants["pc_1"].position == (6, 0)
    assert mv.data["to"] == [6, 0] and mv.data["ft"] == 30
    assert mv.data["requested"] == [9, 0] and mv.data["truncated_at"] == [6, 0]
    assert "15 ft away; you have 0 ft" in mv.data["truncated_reason"]
    assert "could not continue to (9,0)" in mv.text
    assert st.combatants["pc_1"].turn["movement_left"] == 0


def test_a_later_leg_onto_an_occupied_square_stops_short_and_names_the_occupant():
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (4, 0), label="Goblin 2"))
    st, ev = move(st, "pc_1", [[2, 0], [4, 0]])
    mv = find(ev, "move")
    assert st.combatants["pc_1"].position == (2, 0)
    assert mv.data["truncated_at"] == [2, 0] and mv.data["requested"] == [4, 0]
    assert mv.data["truncated_reason"] == "(4,0) is occupied by Goblin 2"


def test_a_complete_move_carries_requested_but_no_truncation():
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (15, 15)))
    st, ev = move(st, "pc_1", [[2, 0], [4, 0]])
    mv = find(ev, "move")
    assert mv.data["requested"] == [4, 0] and mv.data["to"] == [4, 0]
    assert "truncated_at" not in mv.data and "truncated_reason" not in mv.data


def test_a_first_leg_that_cannot_be_walked_is_refused_with_the_reason():
    gob = make_mon("Goblin", "mon_1", (1, 0), label="Goblin 2")
    grid = Grid(width=10, height=10, walls={(0, 3)})
    st = make_state(make_pc("pc_1"), gob, grid=grid)
    with pytest.raises(A.IllegalAction, match=r"\(1,0\) is occupied by Goblin 2"):
        move(st, "pc_1", [[1, 0]])
    with pytest.raises(A.IllegalAction, match=r"\(0,3\) is a wall"):
        move(st, "pc_1", [[0, 3]])
    with pytest.raises(A.IllegalAction, match=r"\(12,0\) is off the grid"):
        move(st, "pc_1", [[12, 0]])
    with pytest.raises(A.IllegalAction, match=r"\(9,0\) is 45 ft away; you have 30 ft"):
        move(st, "pc_1", [[9, 0]])
    assert st.combatants["pc_1"].position == (0, 0)  # nothing was applied


def test_a_walled_in_destination_says_there_is_no_route():
    grid = Grid(width=10, height=10, walls={(4, 0), (4, 1), (5, 1), (6, 1), (6, 0)})
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (9, 9)), grid=grid)
    with pytest.raises(A.IllegalAction, match=r"no route to \(5,0\)"):
        move(st, "pc_1", [[5, 0]])


def test_the_ft_reported_is_what_a_backtracking_path_actually_cost():
    st = make_state(make_pc("pc_1", pos=(5, 3)), make_mon("Goblin", "mon_1", (15, 15)))
    st, ev = move(st, "pc_1", [[4, 2], [3, 3], [4, 2]])
    mv = find(ev, "move")
    assert mv.data["ft"] == 15 and mv.data["path"] == [[4, 2], [3, 3], [4, 2]]
    assert st.combatants["pc_1"].turn["movement_left"] == 15
    assert mv.data["from"] == [5, 3] and mv.data["to"] == [4, 2]


def test_difficult_terrain_on_the_last_leg_is_what_truncates_a_chain():
    # The chooser counted 5 ft a square; the mud costs 10, and the chain is one leg over.
    grid = Grid(width=20, height=1, difficult={(3, 0), (4, 0)})
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (19, 0)), grid=grid)
    st, ev = move(st, "pc_1", [[2, 0], [4, 0], [6, 0]])  # 10 + 20 + 10 = 40 ft of a 30 ft speed
    mv = find(ev, "move")
    assert st.combatants["pc_1"].position == (4, 0) and mv.data["ft"] == 30
    assert mv.data["truncated_at"] == [4, 0] and "(6,0) is 10 ft away; you have 0 ft" == mv.data["truncated_reason"]


# ---------------------------------------------------------------- Spirit Guardians on entry
def aura(caster, radius=15, dc=13, damage="3d8"):
    caster.flags["spirit_guardians"] = {
        "spell": "Spirit Guardians", "dc": dc, "damage": damage, "damage_type": "radiant",
        "radius": radius, "save": "WIS", "half_on_save": True, "enemies_only": True,
        "source": f"{caster.id}:Spirit Guardians",
    }
    return caster


def test_walking_into_the_aura_forces_the_save(script):
    cle = aura(make_pc("pc_1", "Cleric"))
    gob = make_mon("Goblin", "mon_1", (8, 0))
    st = make_state(gob, cle)
    script(1)  # the goblin fails
    st, ev = move(st, "mon_1", [[3, 0]])
    sv = find(ev, "save")
    assert sv and sv.data["target"] == "mon_1" and sv.data["ability"] == "WIS"
    dmg = find(ev, "damage")
    assert dmg and dmg.data["target"] == "mon_1" and dmg.data["damage_type"] == "radiant"
    assert st.combatants["mon_1"].position == (3, 0)


def test_the_aura_hits_once_per_turn_however_often_the_creature_crosses_its_edge(script):
    cle = aura(make_pc("pc_1", "Cleric"))
    gob = make_mon("Goblin", "mon_1", (8, 0))
    st = make_state(gob, cle)
    script(20, 20, 20)
    st, ev = move(st, "mon_1", [[3, 0], [8, 0], [3, 0]])  # in, out, in — one save
    assert kinds(ev).count("save") == 1
    assert st.combatants["mon_1"].position == (3, 0)


def test_starting_inside_then_leaving_and_re_entering_is_not_hit_twice(script):
    cle = aura(make_pc("pc_1", "Cleric"))
    gob = make_mon("Goblin", "mon_1", (2, 0))
    st = make_state(cle, gob)
    script(20, 20)
    st, ev = A.advance_turn(st)  # the goblin's turn starts inside the aura
    assert kinds(ev).count("save") == 1 and st.active_id() == "mon_1"
    st, ev = move(st, "mon_1", [[5, 0], [2, 0]])  # out (25 ft from the cleric) and back in, 30 ft
    assert kinds(ev).count("save") == 0
    st, ev = A.advance_turn(st)
    st, ev = A.advance_turn(st)  # next round: the goblin's turn starts inside again
    assert kinds(ev).count("save") == 1


def test_a_creature_the_aura_drops_stops_where_it_entered(script):
    cle = aura(make_pc("pc_1", "Cleric"))
    gob = make_mon("Goblin", "mon_1", (8, 0))
    gob.hp = 1
    st = make_state(gob, cle)
    script(1)
    st, ev = move(st, "mon_1", [[3, 0], [1, 0]])
    mv = find(ev, "move")
    assert st.combatants["mon_1"].hp == 0 and "interrupted" in mv.text
    assert st.combatants["mon_1"].position == (3, 0) and mv.data["to"] == [3, 0]


def test_being_pushed_into_the_aura_counts_as_entering(script):
    cle = aura(make_pc("pc_1", "Cleric"))
    wiz = make_pc("pc_2", "Wizard", pos=(9, 0))
    gob = make_mon("Goblin", "mon_1", (5, 0))  # 25 ft from the cleric: outside
    st = make_state(wiz, cle, gob)
    rng = script(1)
    events = []
    A._push(st, events, rng, wiz, gob, 10)
    assert st.combatants["mon_1"].position == (3, 0)  # 15 ft: inside
    assert find(events, "move").data.get("forced") and find(events, "save") and find(events, "damage")


def test_the_aura_does_not_hit_its_own_side_or_a_creature_already_inside(script):
    cle = aura(make_pc("pc_1", "Cleric"))
    ally = make_pc("pc_2", pos=(8, 0))
    st = make_state(ally, cle, make_mon("Goblin", "mon_1", (19, 19)))
    script(20)
    st, ev = move(st, "pc_2", [[3, 0]])
    assert find(ev, "save") is None
