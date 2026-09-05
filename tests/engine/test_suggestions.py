"""What the move template suggests, and what it says about each square.

The two squares farthest from every enemy used to ride on every offer, and
the players took them: ten corner retreats in six of seven runs of one
scenario. They are now offered only to a creature that is fleeing or below
half its hit points, and every suggestion carries a label the chooser reads.
"""

from __future__ import annotations

from engine import actions as A
from engine.state import Condition, Grid
from tests.engine.conftest import make_mon, make_pc, make_state, templates


def move_params(st, cid) -> dict:
    tpl = templates(st, cid, "move")
    assert tpl, [t.label for t in A.legal_actions(st, cid)]
    return tpl[0].params


def nearest_ft(p, *others) -> int:
    return min(Grid.distance_ft(tuple(p), o) for o in others)


def test_labels_match_suggested_one_to_one():
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (6, 0)), make_mon("Goblin", "mon_2", (0, 6), label="Goblin 2"))
    params = move_params(st, "pc_1")
    assert len(params["labels"]) == len(params["suggested"]) >= 2
    assert all(isinstance(why, str) and why for why in params["labels"])
    assert len(params["suggested"]) <= A.MAX_SUGGESTED


def test_a_healthy_creature_is_not_offered_the_far_corners():
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (6, 0)), make_mon("Goblin", "mon_2", (0, 6), label="Goblin 2"))
    params = move_params(st, "pc_1")
    for p, why in zip(params["suggested"], params["labels"]):
        assert nearest_ft(p, (6, 0), (0, 6)) <= 5, (p, why)
        assert "away from" not in why
    assert "adjacent to Goblin" in params["labels"][0]


def test_below_half_hp_the_far_squares_come_back_labelled():
    pc = make_pc("pc_1")
    pc.hp = pc.max_hp // 2 - 1
    st = make_state(pc, make_mon("Goblin", "mon_1", (6, 0)))
    params = move_params(st, "pc_1")
    far = [(p, why) for p, why in zip(params["suggested"], params["labels"]) if "away from all enemies" in why]
    assert len(far) == 2
    for p, why in far:
        assert nearest_ft(p, (6, 0)) >= 30 and f"nearest {nearest_ft(p, (6, 0))} ft" in why
    assert "adjacent to Goblin" in params["labels"][0]  # the approach square is still first


def test_a_turned_creature_is_only_offered_squares_away_from_its_turner():
    cle = make_pc("pc_1", "Cleric", pos=(10, 10))
    skel = make_mon("Skeleton", "mon_1", (11, 10))
    skel.add_condition(Condition("turned", duration=10, source="pc_1"))
    st = make_state(skel, cle)
    params = move_params(st, "mon_1")
    assert params["labels"] and all(why.startswith("away from Cleric") for why in params["labels"])
    assert all(Grid.distance_ft(tuple(p), (10, 10)) > 5 for p in params["suggested"])


def test_a_square_in_reach_of_a_second_enemy_says_so():
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (3, 0), label="Goblin 1"),
                    make_mon("Goblin", "mon_2", (3, 2), label="Goblin 2"))
    params = move_params(st, "pc_1")
    labelled = dict(zip(map(tuple, params["suggested"]), params["labels"]))
    # (2,1) is adjacent to both goblins; the approach to Goblin 1 lands there or beside it
    for p, why in labelled.items():
        both = Grid.distance_ft(p, (3, 0)) <= 5 and Grid.distance_ft(p, (3, 2)) <= 5
        assert ("also in reach of" in why) == both, (p, why)


def test_an_approach_that_falls_short_reports_the_distance_and_reach():
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (9, 0)))  # 45 ft off, 30 ft of speed
    params = move_params(st, "pc_1")
    assert params["suggested"][0] == [6, 0]
    assert params["labels"][0] == "15 ft from Goblin, out of its reach"


def test_already_engaged_and_healthy_the_offer_stays_in_reach():
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))
    params = move_params(st, "pc_1")
    assert params["suggested"], "a move must still be offered to a creature that can move"
    for p, why in zip(params["suggested"], params["labels"]):
        assert Grid.distance_ft(tuple(p), (1, 0)) <= 5 and "stays in its reach" in why


def test_max_suggested_is_kept():
    mons = [make_mon("Goblin", f"mon_{i}", (6 + i, 0), label=f"Goblin {i}") for i in range(1, 6)]
    pc = make_pc("pc_1")
    pc.hp = 1
    st = make_state(pc, *mons)
    params = move_params(st, "pc_1")
    assert len(params["suggested"]) == len(params["labels"]) <= A.MAX_SUGGESTED == 6
