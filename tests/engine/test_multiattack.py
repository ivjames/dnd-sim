"""Monster multiattack routines held to the SRD's text.

Three records deviated: the Goblin Boss's second scimitar had no
disadvantage, and the Bandit Captain and Wight had one routine where the SRD
gives an either/or. The engine also let any attack open a routine — a
javelin then two scimitars for the Goblin Boss in three live games.
"""

from __future__ import annotations

from engine import actions as A
from engine import srd
from tests.engine.conftest import attack, find, make_mon, make_pc, make_state, templates


def offered(st, cid):
    return sorted({t.params["weapon"] for t in templates(st, cid, "attack")})


# ---------------------------------------------------------------- data audit
def test_the_three_srd_deviations_are_encoded():
    boss = srd.monster("Goblin Boss")["multiattack"]
    assert boss == ["Scimitar", {"name": "Scimitar", "disadvantage": True}]
    captain = A._multiattack_options(make_mon("Bandit Captain", "mon_1"))
    assert [[e["name"] for e in opt] for opt in captain] == [["Scimitar", "Scimitar", "Dagger"], ["Dagger", "Dagger"]]
    assert captain[0][2]["mode"] == "melee" and all(e["mode"] == "ranged" for e in captain[1])
    wight = A._multiattack_options(make_mon("Wight", "mon_1"))
    assert [[e["name"] for e in opt] for opt in wight] == [
        ["Longsword", "Longsword"], ["Longsword", "Life Drain"], ["Longbow", "Longbow"]]


def test_every_routine_entry_names_an_action_of_its_monster():
    for name in srd.list_monsters():
        mon = make_mon(name, "mon_1")
        actions = {a["name"] for a in mon.stat_block["actions"]}
        for opt in A._multiattack_options(mon):
            for e in opt:
                assert e["name"] in actions, (name, e)
                assert set(e) <= {"name", "disadvantage", "mode"}, (name, e)


# ---------------------------------------------------------------- Goblin Boss
def test_goblin_boss_second_scimitar_has_disadvantage(script):
    st = make_state(make_mon("Goblin Boss", "mon_1", (1, 0)), make_pc("pc_1"))
    script(15, 15, 15)
    st, ev = attack(st, "mon_1", "Fighter", "Scimitar")
    first = find(ev, "attack")
    assert first.data["mode"] == "normal" and "second attack" not in first.data["reasons"]
    st, ev = attack(st, "mon_1", "Fighter", "Scimitar")
    second = find(ev, "attack")
    assert second.data["mode"] == "disadvantage" and "second attack" in second.data["reasons"]
    assert st.combatants["mon_1"].turn["attacks_left"] == 0 and not templates(st, "mon_1", "attack")


def test_goblin_boss_javelin_is_a_single_attack_not_the_first_of_two(script):
    st = make_state(make_mon("Goblin Boss", "mon_1", (1, 0)), make_pc("pc_1"))
    script(15)
    st, ev = attack(st, "mon_1", "Fighter", "Javelin")
    assert st.combatants["mon_1"].turn["attacks_left"] == 0
    assert not templates(st, "mon_1", "attack")


def test_goblin_boss_cannot_follow_a_scimitar_with_a_javelin(script):
    st = make_state(make_mon("Goblin Boss", "mon_1", (1, 0)), make_pc("pc_1"))
    script(15)
    st, _ = attack(st, "mon_1", "Fighter", "Scimitar")
    assert offered(st, "mon_1") == ["Scimitar"]


# ---------------------------------------------------------------- Bandit Captain
def test_bandit_captain_melee_routine_is_two_scimitars_and_a_dagger(script):
    st = make_state(make_mon("Bandit Captain", "mon_1", (1, 0)), make_pc("pc_1"))
    script(15, 15, 15)
    st, _ = attack(st, "mon_1", "Fighter", "Dagger")  # the melee dagger, in any order
    assert st.combatants["mon_1"].turn["attacks_left"] == 2
    assert offered(st, "mon_1") == ["Scimitar"]  # a thrown dagger would be the other routine
    st, _ = attack(st, "mon_1", "Fighter", "Scimitar")
    st, ev = attack(st, "mon_1", "Fighter", "Scimitar")
    assert find(ev, "attack") and st.combatants["mon_1"].turn["attacks_left"] == 0
    assert not templates(st, "mon_1", "attack")


def test_bandit_captain_ranged_routine_is_two_thrown_daggers_only(script):
    st = make_state(make_mon("Bandit Captain", "mon_1", (3, 0)), make_pc("pc_1"))
    script(15, 15)
    st, _ = attack(st, "mon_1", "Fighter", "Dagger (thrown)")
    assert st.combatants["mon_1"].turn["attacks_left"] == 1
    assert [t.params["weapon"] for t in templates(st, "mon_1", "attack")] == ["Dagger (thrown)"]
    st, ev = attack(st, "mon_1", "Fighter", "Dagger (thrown)")
    assert find(ev, "attack") and not templates(st, "mon_1", "attack")


def test_bandit_captain_cannot_mix_a_thrown_dagger_into_the_melee_routine(script):
    st = make_state(make_mon("Bandit Captain", "mon_1", (1, 0)), make_pc("pc_1"))
    script(15)
    st, _ = attack(st, "mon_1", "Fighter", "Scimitar")
    assert offered(st, "mon_1") == ["Dagger", "Scimitar"]
    assert all(t.params["weapon"] != "Dagger (thrown)" for t in templates(st, "mon_1", "attack"))


# ---------------------------------------------------------------- Wight
def test_wight_life_drain_replaces_one_longsword_attack(script):
    st = make_state(make_mon("Wight", "mon_1", (1, 0)), make_pc("pc_1"))
    script(15, 15, 20)
    st, _ = attack(st, "mon_1", "Fighter", "Longsword")
    assert offered(st, "mon_1") == ["Life Drain", "Longsword"]
    st, ev = attack(st, "mon_1", "Fighter", "Life Drain")
    assert find(ev, "attack") and not templates(st, "mon_1", "attack")
    st2 = make_state(make_mon("Wight", "mon_1", (1, 0)), make_pc("pc_1"))
    script(15, 20)
    st2, _ = attack(st2, "mon_1", "Fighter", "Life Drain")
    assert offered(st2, "mon_1") == ["Longsword"]  # not a second Life Drain, not a longbow


def test_wight_longbow_routine_is_two_longbow_attacks(script):
    st = make_state(make_mon("Wight", "mon_1", (4, 0)), make_pc("pc_1"))
    script(15, 15)
    st, _ = attack(st, "mon_1", "Fighter", "Longbow")
    assert offered(st, "mon_1") == ["Longbow"]
    st, ev = attack(st, "mon_1", "Fighter", "Longbow")
    assert find(ev, "attack") and not templates(st, "mon_1", "attack")


# ---------------------------------------------------------------- the rest
def test_hill_giant_rock_is_a_single_attack(script):
    st = make_state(make_mon("Hill Giant", "mon_1", (3, 0)), make_pc("pc_1"))
    script(15)
    st, _ = attack(st, "mon_1", "Fighter", "Rock")
    assert st.combatants["mon_1"].turn["attacks_left"] == 0 and not templates(st, "mon_1", "attack")


def test_routine_order_is_not_a_rule(script):
    st = make_state(make_mon("Owlbear", "mon_1", (1, 0)), make_pc("pc_1"))
    script(15, 15)
    st, _ = attack(st, "mon_1", "Fighter", "Claws")
    assert offered(st, "mon_1") == ["Beak"]
    st, ev = attack(st, "mon_1", "Fighter", "Beak")
    assert find(ev, "attack") and not templates(st, "mon_1", "attack")


def test_a_second_attack_of_the_same_kind_is_refused_where_the_routine_has_one(script):
    st = make_state(make_mon("Black Bear", "mon_1", (1, 0)), make_pc("pc_1"))
    script(15)
    st, _ = attack(st, "mon_1", "Fighter", "Bite")
    assert offered(st, "mon_1") == ["Claws"]
