"""Character construction from compact specs, and monster instantiation."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from engine import srd
from engine.dice import RNG, average_of

__all__ = ["CharacterSheet", "build_character", "monster_to_combatant",
           "pc_to_combatant", "starting_resources", "fresh_turn",
           "CharacterBuildError", "ability_mod"]

ABILITIES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]


class CharacterBuildError(ValueError):
    pass


def ability_mod(score: int) -> int:
    return (int(score) - 10) // 2


@dataclass
class CharacterSheet:
    id: str
    name: str
    race: str
    klass: str
    level: int
    abilities: dict[str, int]
    max_hp: int
    ac: int
    speed: int
    proficiency: int
    saves: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    weapons: list[str] = field(default_factory=list)
    armor: str | None = None
    shield: bool = False
    spells_known: list[str] = field(default_factory=list)
    spell_slots: dict[int, int] = field(default_factory=dict)
    spellcasting_ability: str | None = None
    features: list[str] = field(default_factory=list)
    persona: str = ""
    # extras the engine consults; not part of the minimal contract signature
    expertise: list[str] = field(default_factory=list)
    inventory: list[str] = field(default_factory=list)
    size: str = "M"
    damage_resistances: list[str] = field(default_factory=list)
    save_advantages: list[str] = field(default_factory=list)
    hit_dice: int = 8

    # ---- derived ----------------------------------------------------
    def mod(self, ability: str) -> int:
        return ability_mod(self.abilities[ability])

    @property
    def spell_dc(self) -> int | None:
        if not self.spellcasting_ability:
            return None
        return 8 + self.proficiency + self.mod(self.spellcasting_ability)

    @property
    def spell_attack_bonus(self) -> int | None:
        if not self.spellcasting_ability:
            return None
        return self.proficiency + self.mod(self.spellcasting_ability)

    def skill_bonus(self, skill: str) -> int:
        abil = srd.SKILL_ABILITY.get(skill)
        if abil is None:
            raise CharacterBuildError(f"unknown skill {skill!r}")
        b = self.mod(abil)
        if skill in self.skills:
            b += self.proficiency
        if skill in self.expertise:
            b += self.proficiency
        return b

    @property
    def passive_perception(self) -> int:
        return 10 + self.skill_bonus("Perception")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "race": self.race, "klass": self.klass,
            "level": self.level, "abilities": dict(self.abilities),
            "max_hp": self.max_hp, "ac": self.ac, "speed": self.speed,
            "proficiency": self.proficiency, "saves": list(self.saves),
            "skills": list(self.skills), "weapons": list(self.weapons),
            "armor": self.armor, "shield": self.shield,
            "spells_known": list(self.spells_known),
            "spell_slots": {str(k): v for k, v in self.spell_slots.items()},
            "spellcasting_ability": self.spellcasting_ability,
            "features": list(self.features), "persona": self.persona,
            "expertise": list(self.expertise), "inventory": list(self.inventory),
            "size": self.size,
            "damage_resistances": list(self.damage_resistances),
            "save_advantages": list(self.save_advantages),
            "hit_dice": self.hit_dice,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CharacterSheet":
        return cls(
            id=d["id"], name=d["name"], race=d["race"], klass=d["klass"],
            level=int(d["level"]), abilities={k: int(v) for k, v in d["abilities"].items()},
            max_hp=int(d["max_hp"]), ac=int(d["ac"]), speed=int(d["speed"]),
            proficiency=int(d["proficiency"]), saves=list(d.get("saves", [])),
            skills=list(d.get("skills", [])), weapons=list(d.get("weapons", [])),
            armor=d.get("armor"), shield=bool(d.get("shield", False)),
            spells_known=list(d.get("spells_known", [])),
            spell_slots={int(k): int(v) for k, v in (d.get("spell_slots") or {}).items()},
            spellcasting_ability=d.get("spellcasting_ability"),
            features=list(d.get("features", [])), persona=d.get("persona", ""),
            expertise=list(d.get("expertise", [])), inventory=list(d.get("inventory", [])),
            size=d.get("size", "M"),
            damage_resistances=list(d.get("damage_resistances", [])),
            save_advantages=list(d.get("save_advantages", [])),
            hit_dice=int(d.get("hit_dice", 8)),
        )


# ------------------------------------------------------------------ build
def _assign_abilities(spec_abilities: Any, klass_name: str, race_row: dict) -> dict[str, int]:
    """Resolve the ability spec into post-racial scores."""
    meta = srd.meta()
    if isinstance(spec_abilities, dict):
        base = {a: int(spec_abilities.get(a, 10)) for a in ABILITIES}
        preassigned = True
    else:
        key = spec_abilities if isinstance(spec_abilities, str) else "standard_array"
        if key not in ("standard_array", "point_buy_default"):
            raise CharacterBuildError(f"unknown ability spec {spec_abilities!r}")
        array = list(meta[key])
        priority = meta["ability_priority"].get(klass_name, ABILITIES)
        base = {}
        for ability, score in zip(priority, array):
            base[ability] = score
        for a in ABILITIES:
            base.setdefault(a, 10)
        preassigned = False
    # racial bonuses. If the caller handed us explicit scores we treat them as
    # *pre*-racial only when they look like a raw array (<=15); otherwise as final.
    bonuses = race_row.get("ability_bonuses", {})
    if not preassigned or max(base.values()) <= 15:
        for a, b in bonuses.items():
            base[a] = base.get(a, 10) + int(b)
    for a in ABILITIES:
        if not (1 <= base[a] <= 30):
            raise CharacterBuildError(f"ability {a} out of range: {base[a]}")
    return base


def _apply_asi(abilities: dict[str, int], klass_name: str, level: int) -> None:
    """Fighter/Rogue/Cleric/Wizard all get one ASI at 4th level in this range."""
    if level < 4:
        return
    priority = srd.meta()["ability_priority"].get(klass_name, ABILITIES)
    remaining = 2
    for a in priority:
        while remaining > 0 and abilities[a] < 20:
            abilities[a] += 1
            remaining -= 1
            if abilities[a] % 2 == 0:  # spread to keep modifiers efficient
                break
        if remaining == 0:
            break


def compute_ac(abilities: dict[str, int], armor_name: str | None, shield: bool,
               klass_name: str | None = None) -> int:
    dex = ability_mod(abilities["DEX"])
    if armor_name:
        a = srd.armor(armor_name)
        base = int(a["base_ac"])
        cap = a["dex_cap"]
        if cap is None:
            ac = base + dex
        else:
            ac = base + min(dex, int(cap))
    else:
        ac = 10 + dex
    if shield:
        ac += 2
    return ac


def _default_spells(klass_row: dict, level: int, mod_wis: int) -> list[str]:
    lists = klass_row.get("default_spells") or {}
    known = list(lists.get("cantrips", []))[: int(klass_row["cantrips_known"].get(str(level), 3))]
    max_slot = max((int(k) for k in srd.spell_slots_for(klass_row["name"], level)), default=0)
    prepared_budget = max(1, level + mod_wis)
    # Interleave the per-level lists so a caster with 3rd-level slots actually
    # prepares a 3rd-level spell instead of exhausting the budget on 1st-level.
    columns = [list(lists.get(str(lv), [])) for lv in range(1, max_slot + 1)]
    leveled: list[str] = []
    while any(columns):
        for col in columns:
            if col:
                leveled.append(col.pop(0))
    return known + leveled[:max(prepared_budget, 4)]


def build_character(spec: dict, rng: RNG) -> CharacterSheet:
    """Build a CharacterSheet from a compact spec.

    spec keys: id, name, race, klass, level, abilities, equipment, spells, persona.
    HP uses the fixed average-per-level rule (deterministic); `rng` is accepted
    for signature compatibility and used only if spec["roll_hp"] is true.
    """
    if not isinstance(spec, dict):
        raise CharacterBuildError("spec must be a dict")
    for key in ("id", "name", "race", "klass"):
        if not spec.get(key):
            raise CharacterBuildError(f"spec missing {key!r}")
    level = int(spec.get("level", 1))
    if not 1 <= level <= 5:
        raise CharacterBuildError(f"Phase 1 supports levels 1-5, got {level}")

    race_row = srd.race(spec["race"])
    klass_row = srd.klass(spec["klass"])
    klass_name = klass_row["name"]

    abilities = _assign_abilities(spec.get("abilities", "standard_array"), klass_name, race_row)
    _apply_asi(abilities, klass_name, level)

    proficiency = srd.proficiency_for_level(level)
    hit_die = int(klass_row["hit_die"])
    con = ability_mod(abilities["CON"])

    if spec.get("roll_hp"):
        hp = hit_die + con
        for _ in range(level - 1):
            hp += max(1, rng.roll(f"1d{hit_die}").total + con)
    else:
        per_level = hit_die // 2 + 1
        hp = hit_die + con + (level - 1) * (per_level + con)
    hp += int(race_row.get("hp_per_level", 0)) * level
    hp = max(1, hp)

    # ---- equipment
    equip = spec.get("equipment", "default")
    default_eq = klass_row["default_equipment"]
    if equip == "default" or equip is None:
        weapons = list(default_eq["weapons"])
        armor_name = default_eq["armor"]
        shield = bool(default_eq["shield"])
        items = list(default_eq["items"])
    elif isinstance(equip, dict):
        weapons = list(equip.get("weapons", default_eq["weapons"]))
        armor_name = equip.get("armor", default_eq["armor"])
        shield = bool(equip.get("shield", default_eq["shield"]))
        items = list(equip.get("items", default_eq["items"]))
    elif isinstance(equip, list):
        weapons = [x for x in equip if srd.has("weapon", x)]
        armors = [x for x in equip if srd.has("armor", x) and srd.armor(x)["category"] != "shield"]
        armor_name = armors[0] if armors else None
        shield = any(isinstance(x, str) and x.lower() == "shield" for x in equip)
        items = [x for x in equip if not srd.has("weapon", x) and not srd.has("armor", x)]
    else:
        raise CharacterBuildError(f"bad equipment spec {equip!r}")
    for wname in weapons:
        srd.weapon(wname)  # validates
    if armor_name:
        srd.armor(armor_name)

    # Mage Armor-less wizards etc. keep 10+DEX.
    ac = compute_ac(abilities, armor_name, shield, klass_name)
    speed = int(race_row["speed"])
    # Defense fighting style: +1 AC while wearing armor.
    if armor_name and "fighting_style_defense" in _features_for(klass_row, level):
        ac += 1

    # ---- proficiencies
    saves = list(klass_row["saving_throws"])
    skills = list(dict.fromkeys(
        list(race_row.get("skill_proficiencies", [])) + list(klass_row["default_skills"])
    ))
    if spec.get("skills"):
        skills = list(dict.fromkeys(list(race_row.get("skill_proficiencies", [])) + list(spec["skills"])))
    expertise = list(klass_row.get("expertise_default", [])) if "expertise" in _features_for(klass_row, level) else []

    # ---- features
    features = _features_for(klass_row, level) + list(race_row.get("features", []))

    # ---- spells
    spellcasting_ability = klass_row.get("spellcasting_ability")
    slots = srd.spell_slots_for(klass_name, level)
    spell_spec = spec.get("spells", "default")
    if spellcasting_ability:
        if spell_spec == "default" or spell_spec is None:
            wis = ability_mod(abilities[spellcasting_ability])
            spells_known = _default_spells(klass_row, level, wis)
        elif isinstance(spell_spec, list):
            spells_known = list(spell_spec)
        else:
            raise CharacterBuildError(f"bad spells spec {spell_spec!r}")
        for s in spells_known:
            srd.spell(s)  # validates
    else:
        spells_known = list(spell_spec) if isinstance(spell_spec, list) else []
        for s in spells_known:
            srd.spell(s)
    # High Elf bonus wizard cantrip
    if "elf_cantrip" in features and spellcasting_ability is None and not spells_known:
        spells_known = ["Fire Bolt"]
        spellcasting_ability = "INT"

    sheet = CharacterSheet(
        id=str(spec["id"]), name=str(spec["name"]),
        race=race_row["name"], klass=klass_name, level=level,
        abilities=abilities, max_hp=hp, ac=ac, speed=speed, proficiency=proficiency,
        saves=saves, skills=skills, weapons=weapons, armor=armor_name, shield=shield,
        spells_known=spells_known, spell_slots=slots,
        spellcasting_ability=spellcasting_ability,
        features=features, persona=str(spec.get("persona", "")),
        expertise=expertise, inventory=items, size=race_row.get("size", "M"),
        damage_resistances=list(race_row.get("damage_resistances", [])),
        save_advantages=list(race_row.get("saving_throw_advantages", [])),
        hit_dice=hit_die,
    )
    return sheet


def _features_for(klass_row: dict, level: int) -> list[str]:
    out: list[str] = []
    for lv in range(1, level + 1):
        out.extend(klass_row["levels"].get(str(lv), {}).get("features", []))
    return [f for f in out if f != "asi"]


def starting_resources(sheet: CharacterSheet) -> dict:
    """Per-combat / per-rest resource pools implied by a sheet's features."""
    res: dict[str, Any] = {
        "spell_slots": {int(k): int(v) for k, v in sheet.spell_slots.items()},
        "hit_dice": sheet.level,
    }
    if "second_wind" in sheet.features:
        res["second_wind"] = 1
    if "action_surge" in sheet.features:
        res["action_surge"] = 1
    if "channel_divinity_turn_undead" in sheet.features:
        res["channel_divinity"] = 1
    if "arcane_recovery" in sheet.features:
        res["arcane_recovery"] = 1
    if "sneak_attack" in sheet.features:
        klass_row = srd.klass(sheet.klass)
        res["sneak_attack_dice"] = int(
            klass_row.get("sneak_attack_dice", {}).get(str(sheet.level), 1))
    if "uncanny_dodge" in sheet.features:
        res["uncanny_dodge"] = 1
    return res


def fresh_turn(speed: int, attacks: int = 0) -> dict:
    """A turn budget with nothing spent yet (CONTRACTS §1.4 `Combatant.turn`)."""
    return {
        "action": False,
        "bonus": False,
        "reaction": False,
        "movement_left": int(speed),
        "attacks_left": int(attacks),
        "free_object": False,
    }


def pc_to_combatant(sheet: CharacterSheet, position: tuple[int, int] = (0, 0)):
    """Turn a CharacterSheet into a fresh party Combatant (see Amendment B.2)."""
    from engine.state import Combatant  # local import: state imports this module

    return Combatant(
        id=sheet.id,
        name=sheet.name,
        side="party",
        kind="pc",
        sheet=sheet,
        stat_block=None,
        hp=sheet.max_hp,
        max_hp=sheet.max_hp,
        temp_hp=0,
        ac=sheet.ac,
        speed=sheet.speed,
        abilities=dict(sheet.abilities),
        save_profs=list(sheet.saves),
        skill_profs=list(sheet.skills),
        proficiency=sheet.proficiency,
        position=(int(position[0]), int(position[1])),
        size=sheet.size,
        resources=starting_resources(sheet),
        turn=fresh_turn(sheet.speed),
        inventory=list(sheet.inventory),
    )


# ------------------------------------------------------------- monsters
def monster_to_combatant(name: str, cid: str, rng: RNG, roll_hp: bool = False):
    """Instantiate a monster stat block as a Combatant."""
    from engine.state import Combatant  # local import: state imports this module

    block = copy.deepcopy(srd.monster(name))
    if roll_hp:
        hp = max(1, rng.roll(block["hit_dice"]).total)
    else:
        hp = int(block["hp"])

    cr = float(block["cr"])
    prof = 2 if cr < 5 else 3
    return Combatant(
        id=cid,
        name=block["name"],
        side="enemy",
        kind="monster",
        sheet=None,
        stat_block=block,
        hp=hp,
        max_hp=hp,
        temp_hp=0,
        ac=int(block["ac"]),
        speed=int(block["speed"]),
        abilities=dict(block["abilities"]),
        save_profs=[a for a in block.get("saving_throws", {})],
        skill_profs=[s for s in block.get("skills", {})],
        proficiency=prof,
        position=(0, 0),
        size=block.get("size", "M"),
        resources=_monster_resources(block),
        turn=fresh_turn(int(block["speed"])),
        inventory=[],
    )


def _monster_resources(block: dict) -> dict:
    res: dict[str, Any] = {}
    sc = block.get("spellcasting")
    if sc:
        res["spell_slots"] = {int(k): int(v) for k, v in (sc.get("slots") or {}).items()}
    for a in block.get("actions", []):
        if a.get("recharge") or "Recharge" in (a.get("desc") or ""):
            res.setdefault("recharge", {})[a["name"]] = True
    return res
