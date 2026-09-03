"""One test per rule in CONTRACTS.md §1.6 "Rules coverage required in Phase 1"."""

from __future__ import annotations

import pytest

from engine import actions as A
from engine.state import Condition, Grid

from .conftest import attack, cast, do, find, kinds, make_mon, make_pc, make_state, templates


# ---------------------------------------------------------------- initiative
def test_initiative_dex_tiebreak(script):
    pc = make_pc("pc_1", abilities={"STR": 16, "DEX": 12, "CON": 14, "INT": 10, "WIS": 10, "CHA": 10})
    gob = make_mon("Goblin", "mon_1", (5, 5))  # DEX 14
    st = make_state(pc, gob, start=False)
    st.mode = "combat"
    script(11, 10)  # pc: 11+1 = 12 ; goblin: 10+2 = 12 -> tie, higher DEX first
    st, ev = A.start_combat(st, None)
    assert st.initiative == [("mon_1", 12), ("pc_1", 12)]
    assert kinds(ev)[:2] == ["combat_start", "round_start"]
    assert st.active_id() == "mon_1"
    assert st.round == 1


# ---------------------------------------------------------------- attack rolls
def test_attack_roll_hit_and_miss_vs_ac(script):
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))
    script(10)  # 10 + 5 = 15 vs AC 15: ties hit
    st, ev = attack(st, "pc_1", "Goblin", "Longsword")
    atk = find(ev, "attack")
    assert atk.data["hit"] and atk.data["ac"] == 15 and "hit" in atk.text
    assert find(ev, "damage") is not None
    st.combatants["pc_1"].turn["action"] = False
    script(9)
    st, ev = attack(st, "pc_1", "Goblin", "Longsword")
    assert not find(ev, "attack").data["hit"]
    assert find(ev, "damage") is None


@pytest.mark.parametrize("cond,who,dist,expect", [
    ("prone", "attacker", 1, "disadvantage"),
    ("prone", "target", 1, "advantage"),
    ("prone", "target", 3, "disadvantage"),
    ("restrained", "target", 1, "advantage"),
    ("blinded", "attacker", 1, "disadvantage"),
    ("blinded", "target", 1, "advantage"),
    ("invisible", "attacker", 1, "advantage"),
    ("invisible", "target", 1, "disadvantage"),
    ("poisoned", "attacker", 1, "disadvantage"),
    ("frightened", "attacker", 1, "disadvantage"),
    ("stunned", "target", 1, "advantage"),
])
def test_attack_advantage_disadvantage_from_conditions(script, cond, who, dist, expect):
    pc = make_pc("pc_1")
    gob = make_mon("Goblin", "mon_1", (dist, 0))
    (pc if who == "attacker" else gob).add_condition(Condition(cond))
    st = make_state(pc, gob)
    script(15, 15)
    weapon = "Longsword" if dist == 1 else "Light Crossbow"
    st, ev = attack(st, "pc_1", "Goblin", weapon)
    assert find(ev, "attack").data["mode"] == expect


def test_paralyzed_and_unconscious_targets_are_auto_crit_within_5ft(script):
    for cond in ("paralyzed", "unconscious"):
        gob = make_mon("Goblin", "mon_1", (1, 0))
        gob.add_condition(Condition(cond))
        st = make_state(make_pc("pc_1"), gob)
        script(12, 12)  # advantage -> two dice; hit but not a natural 20
        st, ev = attack(st, "pc_1", "Goblin", "Longsword")
        atk = find(ev, "attack")
        assert atk.data["mode"] == "advantage" and atk.data["hit"] and atk.data["crit"], atk.text


def test_dodge_imposes_disadvantage(script):
    gob = make_mon("Goblin", "mon_1", (1, 0))
    st = make_state(gob, make_pc("pc_1"))
    st, ev = do(st, "mon_1", templates(st, "mon_1", "dodge")[0])
    assert st.combatants["mon_1"].flags.get("dodging")
    st, _ = A.advance_turn(st)
    script(15, 15)
    st, ev = attack(st, "pc_1", "Goblin", "Longsword")
    assert find(ev, "attack").data["mode"] == "disadvantage"
    assert "dodging" in find(ev, "attack").data["reasons"]


# ---------------------------------------------------------------- crits
def test_natural_20_crits_and_doubles_dice(script):
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))
    script(20)
    st, ev = attack(st, "pc_1", "Goblin", "Longsword")
    atk = find(ev, "attack")
    assert atk.data["crit"] and "CRITICAL" in atk.text and "(crit)" in atk.text
    dmg = find(ev, "damage")
    assert dmg.data["crit"] and dmg.data["amount"] >= 2 + 3  # two d8 minimum plus STR


def test_natural_1_always_misses(script):
    gob = make_mon("Goblin", "mon_1", (1, 0))
    gob.ac = 1
    st = make_state(make_pc("pc_1"), gob)
    script(1)
    st, ev = attack(st, "pc_1", "Goblin", "Longsword")
    assert not find(ev, "attack").data["hit"]


def test_champion_improved_critical_crits_on_19(script):
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))  # Fighter 3 = Champion
    script(19)
    st, ev = attack(st, "pc_1", "Goblin", "Longsword")
    assert find(ev, "attack").data["crit"]


# ---------------------------------------------------------------- cover
def test_cover_adds_ac_and_dex_save_bonus(script):
    grid = Grid(width=20, height=20, walls={(1, 0)})
    pc = make_pc("pc_1")
    wiz = make_pc("pc_2", "Wizard", pos=(0, 1), spells=["Acid Splash"])
    gob = make_mon("Goblin", "mon_1", (2, 0))
    st = make_state(pc, wiz, gob, grid=grid)
    script(14)
    st, ev = attack(st, "pc_1", "Goblin", "Light Crossbow")
    atk = find(ev, "attack")
    assert atk.data["ac"] == 15 + 5 and "cover" in atk.text and not atk.data["hit"]
    st, _ = A.advance_turn(st)
    grid.walls = {(1, 1)}
    st.grid = grid
    script(8)
    st, ev = cast(st, "pc_2", "Acid Splash", targets=["mon_1"])
    sv = find(ev, "save")
    assert "(+5 cover)" in sv.text and sv.data["total"] == 8 + 2 + 5


# ---------------------------------------------------------------- reach vs range
def test_ranged_attack_with_adjacent_enemy_has_disadvantage(script):
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)), make_mon("Goblin", "mon_2", (5, 0), label="Far Goblin"))
    script(15, 15)
    st, ev = attack(st, "pc_1", "Far Goblin", "Light Crossbow")
    atk = find(ev, "attack")
    assert atk.data["mode"] == "disadvantage" and "enemy adjacent" in atk.data["reasons"]
    assert not templates(st, "pc_1", "attack", "Far Goblin with Longsword")


def test_long_range_disadvantage_and_reach_weapons(script):
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (17, 0)))  # 85 ft > 80 normal
    script(15, 15)
    st, ev = attack(st, "pc_1", "Goblin", "Light Crossbow")
    assert "long range" in find(ev, "attack").data["reasons"]
    glaive = make_pc("pc_2", equipment={"weapons": ["Glaive"], "armor": "Chain Mail", "shield": False, "items": []})
    st2 = make_state(glaive, make_mon("Goblin", "mon_1", (2, 0)))
    assert templates(st2, "pc_2", "attack", "Glaive")
    assert not templates(st2, "pc_2", "attack", "Longsword")


# ---------------------------------------------------------------- two-weapon fighting
def test_two_weapon_fighting_offhand_without_ability_mod(script):
    rogue = make_pc("pc_1", "Rogue")  # Rapier, Shortbow, Dagger, Dagger
    st = make_state(rogue, make_mon("Goblin", "mon_1", (1, 0)))
    assert not templates(st, "pc_1", "attack", "Off-hand")
    script(15)
    st, ev = attack(st, "pc_1", "Goblin", "Dagger")
    off = templates(st, "pc_1", "attack", "Off-hand")
    assert off and off[0].cost == "bonus" and "Shortsword" in off[0].label
    assert "1d6 piercing" in off[0].label and "1d6+" not in off[0].label  # no ability mod off-hand
    script(15)
    st, ev = do(st, "pc_1", off[0])
    assert find(ev, "attack") and st.combatants["pc_1"].turn["bonus"]
    assert not templates(st, "pc_1", "attack", "Off-hand")


# ---------------------------------------------------------------- extra attack
def test_extra_attack_grants_second_attack_at_level_5(script):
    st = make_state(make_pc("pc_1", level=5), make_mon("Goblin", "mon_1", (1, 0)), make_mon("Goblin", "mon_2", (0, 1), label="Goblin B"))
    script(2, 2, 2)
    st, ev = attack(st, "pc_1", "Goblin", "Longsword")
    assert st.combatants["pc_1"].turn["action"] and st.combatants["pc_1"].turn["attacks_left"] == 1
    assert templates(st, "pc_1", "attack", "Longsword")
    st, ev = attack(st, "pc_1", "Goblin B", "Longsword")
    assert st.combatants["pc_1"].turn["attacks_left"] == 0
    assert not templates(st, "pc_1", "attack", "Longsword")


# ---------------------------------------------------------------- sneak attack
def test_sneak_attack_with_adjacent_ally_once_per_turn(script):
    rogue = make_pc("pc_1", "Rogue")
    ally = make_pc("pc_2", pos=(0, 1))
    gob = make_mon("Goblin", "mon_1", (1, 0))
    gob.hp = 40
    st = make_state(rogue, ally, gob)
    script(15)
    st, ev = attack(st, "pc_1", "Goblin", "Dagger")
    assert "sneak attack" in find(ev, "attack").text
    script(15)
    st, ev = do(st, "pc_1", templates(st, "pc_1", "attack", "Off-hand")[0])
    assert "sneak attack" not in find(ev, "attack").text


def test_sneak_attack_needs_advantage_or_ally_and_finesse(script):
    rogue = make_pc("pc_1", "Rogue")
    st = make_state(rogue, make_mon("Goblin", "mon_1", (1, 0)))
    script(15)
    st, ev = attack(st, "pc_1", "Goblin", "Rapier")
    assert "sneak attack" not in find(ev, "attack").text
    gob = make_mon("Goblin", "mon_1", (1, 0))
    gob.add_condition(Condition("restrained"))
    st = make_state(make_pc("pc_1", "Rogue"), gob)
    script(15, 15)
    st, ev = attack(st, "pc_1", "Goblin", "Rapier")
    assert "sneak attack" in find(ev, "attack").text


# ---------------------------------------------------------------- fighter features
def test_second_wind_heals_once_per_rest():
    pc = make_pc("pc_1")
    pc.hp = 5
    st = make_state(pc, make_mon("Goblin", "mon_1", (5, 5)))
    t = templates(st, "pc_1", "second_wind")
    assert t and t[0].cost == "bonus"
    st, ev = do(st, "pc_1", t[0])
    heal = find(ev, "heal")
    assert heal and st.combatants["pc_1"].hp > 5
    assert st.combatants["pc_1"].resources["second_wind"] == 0
    assert not templates(st, "pc_1", "second_wind")


def test_action_surge_grants_a_second_action(script):
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))
    assert not templates(st, "pc_1", "action_surge")
    script(2)
    st, _ = attack(st, "pc_1", "Goblin", "Longsword")
    assert not templates(st, "pc_1", "attack", "Longsword")
    surge = templates(st, "pc_1", "action_surge")
    assert surge
    st, ev = do(st, "pc_1", surge[0])
    assert templates(st, "pc_1", "attack", "Longsword")
    assert st.combatants["pc_1"].resources["action_surge"] == 0


# ---------------------------------------------------------------- cunning action
def test_cunning_action_dash_disengage_hide_as_bonus(script):
    rogue = make_pc("pc_1", "Rogue")
    st = make_state(rogue, make_mon("Goblin", "mon_1", (1, 0)))
    modes = {t.params["mode"] for t in templates(st, "pc_1", "cunning_action")}
    assert modes == {"dash", "disengage"}  # adjacent enemy: no hiding
    dash = [t for t in templates(st, "pc_1", "cunning_action") if t.params["mode"] == "dash"][0]
    before = st.combatants["pc_1"].turn["movement_left"]
    st, ev = do(st, "pc_1", dash)
    assert st.combatants["pc_1"].turn["movement_left"] == before + rogue.speed
    assert st.combatants["pc_1"].turn["bonus"] and not st.combatants["pc_1"].turn["action"]
    st2 = make_state(make_pc("pc_1", "Rogue"), make_mon("Goblin", "mon_1", (6, 0)))
    assert {t.params["mode"] for t in templates(st2, "pc_1", "cunning_action")} == {"dash", "hide"}


# ---------------------------------------------------------------- spells
def test_spell_attack_and_save_spells(script):
    wiz = make_pc("pc_1", "Wizard", spells=["Fire Bolt"])
    cle = make_pc("pc_2", "Cleric", pos=(0, 1), spells=["Sacred Flame"])
    gob = make_mon("Goblin", "mon_1", (3, 0))
    gob.hp = 50
    st = make_state(wiz, cle, gob)
    script(15)
    st, ev = cast(st, "pc_1", "Fire Bolt", "Goblin")
    assert find(ev, "spell_cast") and find(ev, "attack").data["hit"] and find(ev, "damage").data["damage_type"] == "fire"
    assert find(ev, "attack").data["total"] == 15 + wiz.spell_attack_bonus
    st, _ = A.advance_turn(st)
    script(1)
    st, ev = cast(st, "pc_2", "Sacred Flame", "Goblin")
    sv = find(ev, "save")
    assert sv.data["ability"] == "DEX" and sv.data["dc"] == cle.spell_dc and not sv.data["success"]
    assert find(ev, "damage").data["damage_type"] == "radiant"
    st.combatants["pc_2"].turn["action"] = False
    st.combatants["pc_2"].flags.pop("cast_action_spell", None)
    script(20)
    st, ev = cast(st, "pc_2", "Sacred Flame", "Goblin")
    assert find(ev, "save").data["success"] and find(ev, "damage") is None


def test_aoe_shapes_on_grid(script):
    grid = Grid(width=20, height=20)
    wiz = make_pc("pc_1", "Wizard", level=5, spells=["Fireball", "Burning Hands", "Thunderwave", "Lightning Bolt"])
    near = make_mon("Goblin", "mon_1", (1, 0), label="Near")
    line = make_mon("Goblin", "mon_2", (7, 0), label="Down the line")
    off = make_mon("Goblin", "mon_3", (3, 3), label="Off axis")
    behind = make_mon("Goblin", "mon_4", (0, 5), label="Behind")
    for g in (near, line, off, behind):
        g.hp = 200
    st = make_state(wiz, near, line, off, behind, grid=grid)

    def affected(spell, point):
        s = make_state(wiz, near, line, off, behind, grid=grid)
        s2, ev = cast(s, "pc_1", spell, point=point)
        return {s2.combatants[e.data["target"]].name for e in ev if e.kind == "save"}

    assert affected("Fireball", (5, 1)) == {"Near", "Down the line", "Off axis"}
    assert affected("Burning Hands", (3, 0)) == {"Near", "Off axis"}
    assert affected("Lightning Bolt", (1, 0)) == {"Near", "Down the line"}
    assert affected("Thunderwave", (1, 1)) == {"Near"}
    # the caster is never inside their own self-originating shape
    s2, ev = cast(st, "pc_1", "Thunderwave", point=(1, 1))
    assert all(e.data["target"] != "pc_1" for e in ev if e.kind == "save")


def test_half_damage_on_successful_save(script):
    wiz = make_pc("pc_1", "Wizard", level=5, spells=["Fireball"])
    a = make_mon("Goblin", "mon_1", (5, 0), label="Fails")
    b = make_mon("Goblin", "mon_2", (5, 1), label="Saves")
    a.hp = b.hp = 200
    st = make_state(wiz, a, b)
    script(1, 20)
    st, ev = cast(st, "pc_1", "Fireball", point=(5, 0))
    dmg = {e.data["target"]: e.data["amount"] for e in ev if e.kind == "damage"}
    assert dmg["mon_2"] == dmg["mon_1"] // 2 and dmg["mon_1"] >= 8


def test_upcasting_adds_dice_and_targets(script):
    wiz = make_pc("pc_1", "Wizard", level=5, spells=["Fireball", "Magic Missile"])
    gob = make_mon("Goblin", "mon_1", (5, 0))
    gob.hp = 200
    st = make_state(wiz, gob)
    from engine import srd
    eff = srd.spell("Fireball")["effect"]
    assert A._spell_damage(eff, wiz, 3, 3) == "8d6"
    assert A._spell_damage(eff, wiz, 4, 3) == "8d6+1d6"
    assert A._spell_targets_count(srd.spell("Magic Missile")["effect"], 2, 1) == 4
    # per-target spells are offered at the base slot and the highest available one
    assert {t.params["slot"] for t in templates(st, "pc_1", "cast") if t.params["spell"] == "Magic Missile"} == {1, 3}
    st, ev = cast(st, "pc_1", "Magic Missile", slot=3, targets=[])
    assert len([e for e in ev if e.kind == "roll" and "dart" in e.text]) == 5
    assert st.combatants["pc_1"].resources["spell_slots"][3] == 1
    # cantrips scale with level, not slots
    assert A._spell_damage(srd.spell("Fire Bolt")["effect"], wiz, 0, 0) == "2d10"


def test_concentration_save_and_second_spell_ends_first(script):
    cle = make_pc("pc_1", "Cleric", spells=["Bless", "Shield of Faith"])
    gob = make_mon("Goblin", "mon_1", (1, 0))
    st = make_state(cle, gob)
    st, ev = cast(st, "pc_1", "Bless", targets=[])
    assert st.combatants["pc_1"].concentration["spell"] == "Bless"
    assert A._has_buff(st.combatants["pc_1"], "bless")
    # SRD: a bonus-action spell can't share a turn with another leveled spell, so wait a round
    assert not [t for t in templates(st, "pc_1", "cast") if t.params["spell"] == "Shield of Faith"]
    st, _ = A.advance_turn(st)
    st, _ = do(st, "mon_1", templates(st, "mon_1", "end_turn")[0])
    st, _ = A.advance_turn(st)
    assert st.active_id() == "pc_1" and st.round == 2
    st, ev = cast(st, "pc_1", "Shield of Faith", "yourself")
    assert find(ev, "concentration_broken") and "Bless" in find(ev, "concentration_broken").text
    assert st.combatants["pc_1"].concentration["spell"] == "Shield of Faith"
    assert not A._has_buff(st.combatants["pc_1"], "bless") and A._has_buff(st.combatants["pc_1"], "shield_of_faith")
    st, _ = A.advance_turn(st)
    script(16, 1)  # 16 + 4 = 20 vs AC 18 + 2 (Shield of Faith): hit; cleric fails the CON save
    st, ev = attack(st, "mon_1", "Cleric", "Scimitar")
    sv = find(ev, "save")
    assert sv.data["ability"] == "CON" and sv.data["dc"] == 10 and not sv.data["success"]
    assert find(ev, "concentration_broken") and st.combatants["pc_1"].concentration is None
    assert not A._has_buff(st.combatants["pc_1"], "shield_of_faith")


def test_conditions_tick_down_and_repeat_saves_at_end_of_turn(script):
    wiz = make_pc("pc_1", "Wizard", level=5, spells=["Hold Person"])
    gob = make_mon("Goblin", "mon_1", (3, 0))
    st = make_state(wiz, gob)
    script(1)
    st, ev = cast(st, "pc_1", "Hold Person", "Goblin")
    cond = st.combatants["mon_1"].get_condition("paralyzed")
    assert cond and cond.duration == 10 and cond.extra["repeat_save"]
    assert [t.type for t in A.legal_actions(st, "mon_1")] == ["end_turn"]
    st, ev = A.advance_turn(st)                       # -> goblin's turn
    assert st.active_id() == "mon_1"
    script(2)
    st, ev = A.advance_turn(st)                       # goblin's turn ends: repeat save fails, duration ticks
    assert find(ev, "save") and not find(ev, "save").data["success"]
    assert st.combatants["mon_1"].get_condition("paralyzed").duration == 9
    st, ev = A.advance_turn(st)                       # wizard ends (does nothing) -> goblin
    script(20)
    st, ev = A.advance_turn(st)
    assert find(ev, "condition_remove") and not st.combatants["mon_1"].has_condition("paralyzed")
    assert find(ev, "concentration_broken") is None   # spell still up; target just shook it off


# ---------------------------------------------------------------- movement
def test_difficult_terrain_costs_double():
    grid = Grid(width=20, height=1, difficult={(1, 0)})  # a corridor: no way around the mud
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (10, 0)), grid=grid)
    st, ev = do(st, "pc_1", templates(st, "pc_1", "move")[0], path=[[2, 0]])
    mv = find(ev, "move")
    assert mv.data["ft"] == 15 and st.combatants["pc_1"].turn["movement_left"] == 30 - 15
    assert st.combatants["pc_1"].position == (2, 0)
    # given room, the pathfinder walks around difficult terrain instead
    open_grid = Grid(width=20, height=20, difficult={(1, 0)})
    st2 = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (10, 10)), grid=open_grid)
    st2, ev = do(st2, "pc_1", templates(st2, "pc_1", "move")[0], path=[[2, 0]])
    assert find(ev, "move").data["ft"] == 10


def test_opportunity_attack_on_leaving_reach_and_disengage_negates(script):
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))
    script(15)
    st, ev = do(st, "pc_1", templates(st, "pc_1", "move")[0], path=[[4, 0]])
    oa = find(ev, "attack")
    assert oa and oa.data["opportunity"] and oa.actor == "mon_1" and oa.data["target"] == "pc_1"
    assert st.combatants["mon_1"].turn["reaction"]
    fresh = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))
    assert A.reactions_for(fresh, {"type": "move", "mover": "pc_1", "from": (0, 0), "to": (3, 0)})
    assert not A.reactions_for(fresh, {"type": "move", "mover": "pc_1", "from": (0, 0), "to": (2, 1)})  # still in reach
    st2 = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))
    st2, _ = do(st2, "pc_1", templates(st2, "pc_1", "disengage")[0])
    st2, ev = do(st2, "pc_1", templates(st2, "pc_1", "move")[0], path=[[4, 0]])
    assert find(ev, "attack") is None and st2.combatants["pc_1"].position == (4, 0)


def test_help_gives_the_next_ally_attack_advantage(script):
    fighter = make_pc("pc_1")
    rogue = make_pc("pc_2", "Rogue", pos=(0, 1))
    gob = make_mon("Goblin", "mon_1", (1, 0))
    st = make_state(fighter, rogue, gob)
    st, ev = do(st, "pc_1", templates(st, "pc_1", "help")[0])
    assert st.combatants["mon_1"].flags["helped_against"]["side"] == "party"
    st, _ = A.advance_turn(st)
    script(15, 15)
    st, ev = attack(st, "pc_2", "Goblin", "Rapier")
    atk = find(ev, "attack")
    assert atk.data["mode"] == "advantage" and "help" in atk.data["reasons"]
    assert "helped_against" not in st.combatants["mon_1"].flags


def test_hide_is_stealth_vs_passive_perception(script):
    rogue = make_pc("pc_1", "Rogue")
    gob = make_mon("Goblin", "mon_1", (5, 0))  # passive Perception 9
    st = make_state(rogue, gob)
    script(1)
    st, ev = do(st, "pc_1", templates(st, "pc_1", "hide")[0])
    assert find(ev, "skill_check").data["dc"] == 9 and not st.combatants["pc_1"].has_condition("hidden")
    st.combatants["pc_1"].turn["action"] = False
    script(10)
    st, ev = do(st, "pc_1", templates(st, "pc_1", "hide")[0])
    assert st.combatants["pc_1"].has_condition("hidden")
    st, _ = A.advance_turn(st)
    st, _ = A.advance_turn(st)
    script(15, 15)
    st, ev = attack(st, "pc_1", "Goblin", "Shortbow")
    assert find(ev, "attack").data["mode"] == "advantage"
    assert not st.combatants["pc_1"].has_condition("hidden")


# ---------------------------------------------------------------- healing / hp
def test_healing_caps_at_max_and_wakes_the_unconscious():
    cle = make_pc("pc_1", "Cleric", spells=["Cure Wounds", "Healing Word"])
    ally = make_pc("pc_2", pos=(1, 0))
    ally.hp = ally.max_hp - 2
    st = make_state(cle, ally, make_mon("Goblin", "mon_1", (9, 9)))
    st, ev = cast(st, "pc_1", "Cure Wounds", "Fighter")
    assert st.combatants["pc_2"].hp == ally.max_hp and find(ev, "heal").data["amount"] == 2
    down = st.combatants["pc_2"]
    down.hp = 0
    down.add_condition(Condition("unconscious"))
    down.add_condition(Condition("prone"))
    down.death_saves = {"success": 1, "failure": 2}
    # Healing Word is a bonus-action leveled spell: not on the same turn as Cure Wounds
    assert not [t for t in templates(st, "pc_1", "cast") if t.params["spell"] == "Healing Word"]
    st, _ = A.advance_turn(st)
    while st.active_id() != "pc_1":
        st, _ = do(st, st.active_id(), templates(st, st.active_id(), "end_turn")[0])
        st, _ = A.advance_turn(st)
    st, ev = cast(st, "pc_1", "Healing Word", "Fighter")
    assert st.combatants["pc_2"].hp > 0 and not st.combatants["pc_2"].has_condition("unconscious")
    assert st.combatants["pc_2"].death_saves == {"success": 0, "failure": 0}
    assert find(ev, "condition_remove", "consciousness")


def test_temp_hp_absorbs_first_and_does_not_stack(script):
    wiz = make_pc("pc_1", "Wizard", spells=["False Life"])
    gob = make_mon("Goblin", "mon_1", (1, 0))
    st = make_state(wiz, gob)
    st, ev = cast(st, "pc_1", "False Life")
    temp = st.combatants["pc_1"].temp_hp
    assert temp >= 5 and find(ev, "heal")
    assert not [t for t in templates(st, "pc_1", "cast") if t.params.get("spell") == "False Life"]
    st.combatants["pc_1"].temp_hp = 3
    st, _ = A.advance_turn(st)
    script(20)  # crit so the damage exceeds 3
    st, ev = attack(st, "mon_1", "Wizard", "Scimitar")
    dmg = find(ev, "damage")
    assert dmg.data["absorbed"] == 3 and st.combatants["pc_1"].temp_hp == 0
    assert dmg.data["amount"] == find(ev, "attack").data["damage"] - 3
    assert st.combatants["pc_1"].hp == wiz.max_hp - dmg.data["amount"]


def test_zero_hp_unconscious_and_death_saves(script):
    pc = make_pc("pc_1")
    pc.hp = 1
    gob = make_mon("Goblin", "mon_1", (1, 0))
    st = make_state(gob, pc)
    script(15)
    st, ev = attack(st, "mon_1", "Fighter", "Scimitar")
    assert find(ev, "down") and st.combatants["pc_1"].hp == 0
    assert st.combatants["pc_1"].has_condition("unconscious") and st.combatants["pc_1"].has_condition("prone")
    # each of the PC's turns rolls a death save automatically
    script(5)
    st, ev = A.advance_turn(st)
    assert find(ev, "death_save") and st.combatants["pc_1"].death_saves == {"success": 0, "failure": 1}
    assert st.active_id() == "mon_1"  # dying creatures are skipped
    st, _ = do(st, "mon_1", templates(st, "mon_1", "end_turn")[0])
    script(1)  # natural 1 = two failures -> dead
    st, ev = A.advance_turn(st)
    assert find(ev, "dead") and st.combatants["pc_1"].dead
    assert A.combat_over(st) == "enemy"


def test_death_save_natural_20_and_three_successes(script):
    for faces, want in (([20], "revived"), ([10, 10, 10], "stable")):
        pc = make_pc("pc_1")
        pc.hp = 0
        pc.add_condition(Condition("unconscious"))
        gob = make_mon("Goblin", "mon_1", (5, 5))
        st = make_state(gob, pc)
        for f in faces:
            script(f)
            st, ev = A.advance_turn(st)
            if st.active_id() == "mon_1":
                st, _ = do(st, "mon_1", templates(st, "mon_1", "end_turn")[0])
        c = st.combatants["pc_1"]
        if want == "revived":
            assert c.hp == 1 and not c.has_condition("unconscious") and find(ev, "death_save").data.get("revived")
        else:
            assert c.stable and c.hp == 0 and find(ev, "stable")


def test_damage_while_down_is_a_death_save_failure(script):
    pc = make_pc("pc_1")
    pc.hp = 0
    pc.add_condition(Condition("unconscious"))
    gob = make_mon("Goblin", "mon_1", (1, 0))
    st = make_state(gob, pc)
    # Downed creatures are deliberately left out of the enumerated attack list
    # (see CONTRACTS Amendments), so drive the resolver directly.
    assert not templates(st, "mon_1", "attack")
    rng = script(16, 16)  # advantage vs unconscious; 20 vs AC 19 hits -> auto-crit within 5 ft -> two failures
    events = []
    spec = A._best_melee_spec(st.combatants["mon_1"])
    A._resolve_attack(st, events, rng, st.combatants["mon_1"], st.combatants["pc_1"], spec)
    assert find(events, "attack").data["crit"]
    assert st.combatants["pc_1"].death_saves["failure"] == 2 and find(events, "death_save")


def test_instant_death_on_massive_damage():
    pc = make_pc("pc_1")
    pc.hp = 5
    st = make_state(pc, make_mon("Goblin", "mon_1", (5, 5)))
    events = []
    A._deal_damage(st, events, A._rng_of(st), st.combatants["pc_1"], 5 + pc.max_hp, "bludgeoning", "mon_1")
    assert st.combatants["pc_1"].dead and "massive damage" in find(events, "dead").text
    st2 = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (5, 5)))
    st2.combatants["pc_1"].hp = 5
    events = []
    A._deal_damage(st2, events, A._rng_of(st2), st2.combatants["pc_1"], 5 + pc.max_hp - 1, "bludgeoning", "mon_1")
    assert not st2.combatants["pc_1"].dead and find(events, "down")


def test_stabilizing_with_spare_the_dying():
    cle = make_pc("pc_1", "Cleric", spells=["Spare the Dying"])
    ally = make_pc("pc_2", pos=(1, 0))
    ally.hp = 0
    ally.add_condition(Condition("unconscious"))
    ally.death_saves = {"success": 0, "failure": 2}
    st = make_state(cle, ally, make_mon("Goblin", "mon_1", (9, 9)))
    st, ev = cast(st, "pc_1", "Spare the Dying", "Fighter")
    c = st.combatants["pc_2"]
    assert c.stable and find(ev, "stable") and c.death_saves == {"success": 0, "failure": 0}
    st, _ = A.advance_turn(st)
    assert st.active_id() == "mon_1" and not [e for e in _ if e.kind == "death_save"]


# ---------------------------------------------------------------- turn undead
def test_turn_undead_turns_and_destroys(script):
    cle = make_pc("pc_1", "Cleric")
    skel = make_mon("Skeleton", "mon_1", (3, 0))
    ghoul = make_mon("Ghoul", "mon_2", (0, 3))
    gob = make_mon("Goblin", "mon_3", (4, 4))
    st = make_state(cle, skel, ghoul, gob)
    t = templates(st, "pc_1", "channel_divinity")
    assert t and "2 undead" in t[0].label
    script(1, 1)
    st, ev = do(st, "pc_1", t[0])
    assert st.combatants["mon_1"].has_condition("turned") and st.combatants["mon_2"].has_condition("turned")
    assert not st.combatants["mon_3"].has_condition("turned")
    assert st.combatants["pc_1"].resources["channel_divinity"] == 0
    st, _ = A.advance_turn(st)
    assert {x.type for x in A.legal_actions(st, "mon_1")} <= {"dash", "move", "end_turn"}
    # a level-5 Life cleric destroys CR <= 1/2 undead outright
    st2 = make_state(make_pc("pc_1", "Cleric", level=5), make_mon("Skeleton", "mon_1", (3, 0)), make_mon("Ghoul", "mon_2", (0, 3)))
    script(1, 1)
    st2, ev = do(st2, "pc_1", templates(st2, "pc_1", "channel_divinity")[0])
    assert st2.combatants["mon_1"].dead and "destroyed" in find(ev, "dead").text
    assert st2.combatants["mon_2"].has_condition("turned") and not st2.combatants["mon_2"].dead


def test_turned_ends_when_damaged(script):
    cle = make_pc("pc_1", "Cleric")
    skel = make_mon("Skeleton", "mon_1", (1, 0))
    skel.add_condition(Condition("turned", duration=10, source="pc_1"))
    st = make_state(cle, skel)
    script(15)
    st, ev = attack(st, "pc_1", "Skeleton", "Mace")
    assert not st.combatants["mon_1"].has_condition("turned")


# ---------------------------------------------------------------- monsters
def test_monster_multiattack_sequence(script):
    boss = make_mon("Goblin Boss", "mon_1", (1, 0))
    pc = make_pc("pc_1")
    st = make_state(boss, pc)
    labels = [t.label for t in templates(st, "mon_1", "attack")]
    assert any("Scimitar" in l for l in labels) and any("Javelin" in l for l in labels)
    script(2)
    st, ev = attack(st, "mon_1", "Fighter", "Scimitar")
    assert st.combatants["mon_1"].turn["attacks_left"] == 1
    labels = [t.label for t in templates(st, "mon_1", "attack")]
    assert labels and all("Scimitar" in l for l in labels)
    script(2)
    st, ev = attack(st, "mon_1", "Fighter", "Scimitar")
    assert find(ev, "attack") and st.combatants["mon_1"].turn["attacks_left"] == 0
    assert not templates(st, "mon_1", "attack")
    owl = make_mon("Owlbear", "mon_2", (1, 0))
    st2 = make_state(owl, make_pc("pc_1"))
    script(2)
    st2, _ = attack(st2, "mon_2", "Fighter", "Beak")
    assert all("Claws" in t.label for t in templates(st2, "mon_2", "attack"))


def test_ghoul_claws_paralyze_but_not_elves(script):
    ghoul = make_mon("Ghoul", "mon_1", (1, 0))
    human = make_pc("pc_1")
    elf = make_pc("pc_2", race="Elf (High)", pos=(0, 1))
    st = make_state(ghoul, human, elf)
    script(15, 1)  # hit, fail CON save
    st, ev = attack(st, "mon_1", "Fighter pc_1", "Claws")
    assert st.combatants["pc_1"].has_condition("paralyzed") and find(ev, "save").data["ability"] == "CON"
    st.combatants["mon_1"].turn["action"] = False
    script(15, 1)
    st, ev = attack(st, "mon_1", "Fighter pc_2", "Claws")
    assert not st.combatants["pc_2"].has_condition("paralyzed") and find(ev, "save") is None


def test_pack_tactics_and_undead_fortitude(script):
    wolf = make_mon("Wolf", "mon_1", (1, 0))
    wolf2 = make_mon("Wolf", "mon_2", (0, 1))
    pc = make_pc("pc_1")
    st = make_state(wolf, wolf2, pc)
    script(15, 15, 20)
    st, ev = attack(st, "mon_1", "Fighter", "Bite")
    assert "pack tactics" in find(ev, "attack").data["reasons"]
    zombie = make_mon("Zombie", "mon_3", (1, 0))
    zombie.hp = 1
    st2 = make_state(make_pc("pc_1"), zombie)
    script(15, 20)  # hit; zombie makes its CON save
    st2, ev = attack(st2, "pc_1", "Zombie", "Longsword")
    assert st2.combatants["mon_3"].hp == 1 and not st2.combatants["mon_3"].dead and find(ev, "system", "Undead Fortitude")


# ---------------------------------------------------------------- exhaustion
def test_exhaustion_levels(script):
    pc = make_pc("pc_1")
    pc.add_condition(Condition("exhaustion", extra={"level": 2}))
    assert pc.effective_speed() == pc.speed // 2
    st = make_state(pc, make_mon("Goblin", "mon_1", (1, 0)))
    assert st.combatants["pc_1"].turn["movement_left"] == pc.speed // 2
    pc.add_condition(Condition("exhaustion", extra={"level": 1}))
    assert pc.exhaustion_level() == 3
    script(15, 15)
    st, ev = attack(st, "pc_1", "Goblin", "Longsword")
    assert "exhaustion" in find(ev, "attack").data["reasons"] and find(ev, "attack").data["mode"] == "disadvantage"
    st, ev = A.skill_check(st, "pc_1", "Athletics", 10)
    assert find(ev, "skill_check").data["roll"]["mode"] == "disadvantage"
    gob = make_mon("Goblin", "mon_1", (5, 5))
    pc2 = make_pc("pc_2")
    pc2.add_condition(Condition("exhaustion", extra={"level": 6}))
    st2 = make_state(gob, pc2)
    st2, ev = A.advance_turn(st2)
    assert st2.combatants["pc_2"].dead and "exhaustion" in find(ev, "dead").text


# ---------------------------------------------------------------- reactions: shield
def test_shield_auto_casts_only_when_it_turns_a_hit_into_a_miss(script):
    wiz = make_pc("pc_1", "Wizard", spells=["Shield", "Magic Missile"])  # AC 12
    gob = make_mon("Goblin", "mon_1", (1, 0))
    st = make_state(gob, wiz)
    script(8)  # 8 + 4 = 12: hits AC 12, not AC 17
    st, ev = attack(st, "mon_1", "Wizard", "Scimitar")
    atk = find(ev, "attack")
    assert not atk.data["hit"] and "Shield" in atk.text and find(ev, "spell_cast").data["spell"] == "Shield"
    assert st.combatants["pc_1"].resources["spell_slots"][1] == 3 and st.combatants["pc_1"].turn["reaction"]
    assert st.combatants["pc_1"].effective_ac() == 17 and find(ev, "damage") is None
    st2 = make_state(make_mon("Goblin", "mon_1", (1, 0)), make_pc("pc_1", "Wizard", spells=["Shield"]))
    script(15)  # 19 beats even AC 17: no point casting
    st2, ev = attack(st2, "mon_1", "Wizard", "Scimitar")
    assert find(ev, "attack").data["hit"] and find(ev, "spell_cast") is None
    assert st2.combatants["pc_1"].resources["spell_slots"][1] == 4


def test_shield_blocks_magic_missile():
    a = make_pc("pc_1", "Wizard", spells=["Magic Missile"])
    b = make_pc("pc_2", "Wizard", pos=(3, 0), spells=["Shield"], side="enemy")
    A._add_buff(b, {"name": "shield", "ac": 5, "rounds": 1, "tick": "start", "source": "pc_2:Shield"})
    st = make_state(a, b)
    st, ev = cast(st, "pc_1", "Magic Missile", targets=["pc_2"])
    assert find(ev, "damage") is None and find(ev, "system", "absorbs")


# ---------------------------------------------------------------- contract plumbing
def test_end_turn_always_offered_and_illegal_actions_raise():
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))
    assert [t for t in A.legal_actions(st, "pc_1") if t.type == "end_turn"]
    with pytest.raises(A.IllegalAction):
        A.apply(st, A.Action(actor="mon_1", template_id="a1", params={}))  # not its turn
    with pytest.raises(A.IllegalAction):
        A.apply(st, A.Action(actor="pc_1", template_id="a999", params={}))
    with pytest.raises(A.IllegalAction):
        A.apply(st, A.Action(actor="pc_1", template_id=templates(st, "pc_1", "move")[0].id, params={"path": [[19, 19]]}))
    ids = [t.id for t in A.legal_actions(st, "pc_1")]
    assert ids == [f"a{i + 1}" for i in range(len(ids))]
    assert all(len(t.label) <= 80 for t in A.legal_actions(st, "pc_1"))


def test_apply_is_pure_and_state_round_trips():
    st = make_state(make_pc("pc_1"), make_mon("Goblin", "mon_1", (1, 0)))
    before = st.to_dict()
    st2, ev = attack(st, "pc_1", "Goblin", "Longsword")
    assert st.to_dict() == before and st2 is not st
    assert st2.event_seq == len(ev) and [e.seq for e in ev] == list(range(1, len(ev) + 1))
    from engine.state import GameState
    import json
    d = st2.to_dict()
    assert GameState.from_dict(json.loads(json.dumps(d))).to_dict() == json.loads(json.dumps(d))


def test_skill_check_uses_proficiency_and_guidance(script):
    rogue = make_pc("pc_1", "Rogue")  # Stealth expertise
    st = make_state(rogue, make_mon("Goblin", "mon_1", (5, 5)))
    script(10)
    st, ev = A.skill_check(st, "pc_1", "Stealth", 15)
    sc = find(ev, "skill_check")
    assert sc.data["total"] == 10 + rogue.skill_bonus("Stealth") and sc.data["success"]
    A._add_buff(st.combatants["pc_1"], {"name": "guidance", "check_die": "1d4", "uses": 1, "rounds": 10, "tick": "end", "source": "x"})
    script(10)
    st, ev = A.skill_check(st, "pc_1", "Athletics", 30)
    assert "guidance" in find(ev, "skill_check").text and not A._has_buff(st.combatants["pc_1"], "guidance")


def test_combat_over_and_potion_use():
    pc = make_pc("pc_1")
    pc.hp = 3
    gob = make_mon("Goblin", "mon_1", (5, 5))
    st = make_state(pc, gob)
    assert A.combat_over(st) is None
    st, ev = do(st, "pc_1", templates(st, "pc_1", "use_item")[0])
    assert find(ev, "heal") and not any("potion" in x.lower() for x in st.combatants["pc_1"].inventory)
    st.combatants["mon_1"].dead = True
    assert A.combat_over(st) == "party"
    st.mode = "exploration"
    assert A.combat_over(st) is None
