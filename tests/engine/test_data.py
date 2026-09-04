"""Every SRD record the engine ships must be usable by the resolver."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine import actions as A
from engine import srd
from engine.dice import parse_expr
from engine.state import Condition

from .conftest import make_mon, make_pc, make_state, templates

DATA = Path(__file__).resolve().parents[2] / "engine" / "data"
EFFECT_KINDS = {"attack", "save", "heal", "buff", "debuff", "summon_none", "utility"}


def test_license_and_files_present():
    for name in ("races", "classes", "spells", "monsters", "weapons", "armor", "conditions", "equipment", "meta"):
        assert (DATA / f"{name}.json").exists(), name
    assert "Creative Commons Attribution 4.0" in (DATA / "LICENSE-SRD.txt").read_text()


def test_spell_effects_use_the_closed_vocabulary():
    for name in srd.list_spells():
        row = srd.spell(name)
        eff = row["effect"]
        assert eff["kind"] in EFFECT_KINDS, name
        for key in ("attack_type", "save", "half_on_save", "damage", "damage_type", "upcast", "area", "range",
                    "duration_rounds", "concentration", "conditions_applied", "targets"):
            assert key in eff, (name, key)
        if eff.get("damage"):
            parse_expr(eff["damage"])
        if eff.get("heal"):
            parse_expr(eff["heal"])
        if eff.get("upcast"):
            assert A._parse_upcast(eff, row["level"], row["level"] + 2) != ("", 0, 0), name
        for cname in eff.get("conditions_applied", []):
            srd.condition(cname)


def _arena(spell_name: str):
    """A state where `spell_name` has at least one legal cast for the caster."""
    row = srd.spell(spell_name)
    klass = "Cleric" if "Cleric" in row["classes"] else "Wizard"
    caster = make_pc("pc_1", klass, level=5, spells=[spell_name],
                     equipment={"weapons": ["Dagger"], "armor": None, "shield": False, "items": []})
    ally = make_pc("pc_2", pos=(1, 0))
    ally.hp = ally.max_hp // 2
    ally.add_condition(Condition("poisoned", source="x:y"))
    down = make_pc("pc_3", "Rogue", pos=(0, 1))
    down.hp = 0
    down.add_condition(Condition("unconscious"))
    gob = make_mon("Goblin", "mon_1", (1, 1))  # adjacent: touch-range spells need a target
    gob.hp = 60
    gob2 = make_mon("Goblin", "mon_2", (3, 1), label="Goblin 2")
    gob2.hp = 60
    return make_state(caster, ally, down, gob, gob2)


@pytest.mark.parametrize("spell_name", srd.list_spells())
def test_every_spell_resolves(spell_name):
    row = srd.spell(spell_name)
    eff = row["effect"]
    st = _arena(spell_name)
    tpls = [t for t in templates(st, "pc_1", "cast") if t.params.get("spell") == spell_name]
    if not A._combat_spell(row):
        # reactions (Shield) and utility spells with nothing for the resolver to do (Light, Mage Hand...)
        assert not tpls, f"{spell_name} should not be offered as a combat action"
        return
    assert tpls, f"{spell_name} never becomes castable: {[t.label for t in A.legal_actions(st, 'pc_1')]}"
    t = tpls[0]
    params = {}
    if "point" in t.needs:
        assert t.params["suggested"], t.label
        params["point"] = t.params["suggested"][0]
    if "targets" in t.needs:
        assert t.params["suggested"], t.label
        params["targets"] = t.params["suggested"]
    st2, ev = A.apply(st, A.Action(actor="pc_1", template_id=t.id, params=params))
    assert ev and ev[0].kind == "spell_cast" and spell_name in ev[0].text
    if row["level"] > 0:
        assert st2.combatants["pc_1"].resources["spell_slots"][t.params["slot"]] == \
            st.combatants["pc_1"].resources["spell_slots"][t.params["slot"]] - 1
    if eff.get("concentration"):
        assert st2.combatants["pc_1"].concentration["spell"] == spell_name
    assert len(t.label) <= 80


@pytest.mark.parametrize("name", srd.list_monsters())
def test_every_monster_instantiates_and_can_act(name):
    mon = make_mon(name, "mon_1", (1, 0))
    st = make_state(mon, make_pc("pc_1"))
    assert mon.hp > 0 and mon.ac > 0 and mon.speed >= 0
    for a in mon.stat_block["actions"]:
        if a.get("damage"):
            parse_expr(a["damage"])
    for entry in A._multiattack(mon):
        assert any(a["name"] == entry["name"] for a in mon.stat_block["actions"]), (name, entry)
    tpls = A.legal_actions(st, "mon_1")
    assert any(t.type == "attack" for t in tpls), name
    sc = mon.stat_block.get("spellcasting")
    if sc:
        assert any(t.type == "cast" for t in tpls), name
        for s in A._known_spells(mon):  # cantrips + {"1": [...]} flattened
            srd.spell(s)
        assert set(sc.get("cantrips", [])) <= set(A._known_spells(mon))


def test_weapons_and_armor_tables_are_complete():
    assert len(srd.list_weapons()) >= 36
    weapons = {w.lower() for w in srd.list_weapons()}
    assert {"longsword", "longbow", "dagger", "greataxe", "hand crossbow", "net"} <= weapons
    assert {"leather", "chain mail", "plate", "shield"} <= {a.lower() for a in srd.list_armor()}
    assert len(srd.list_conditions()) >= 15
    assert srd.equipment("Potion of healing")["use"]["amount"] == "2d4+2"
    assert len(srd.list_monsters(cr_max=5)) >= 25
    assert len(srd.list_spells()) >= 40


def test_class_default_spells_exist_and_examples_build():
    for klass in srd.list_classes():
        for lists in (srd.klass(klass).get("default_spells") or {}).values():
            for s in lists:
                srd.spell(s)
    for path in sorted((DATA.parents[1] / "examples").glob("*.json")):
        cfg = json.loads(path.read_text())
        from engine.characters import build_character
        from engine.dice import RNG
        for spec in cfg["party"]:
            sheet = build_character(spec, RNG(1))
            assert sheet.max_hp > 0 and sheet.ac >= 10
        for enc in cfg["scenario"]["encounters"]:
            for m in enc["monsters"]:
                srd.monster(m["name"])
