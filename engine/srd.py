"""SRD 5.1 data access.

Loads `engine/data/*.json` once at import. All lookups are case-insensitive and
return the stored dict (callers must not mutate it; use `copy.deepcopy` first).
"""

from __future__ import annotations

import copy
import json
import os
import re
from functools import lru_cache

__all__ = [
    "race", "klass", "spell", "monster", "weapon", "armor", "condition", "equipment",
    "meta", "list_spells", "list_monsters", "list_weapons", "list_armor",
    "list_races", "list_classes", "list_conditions", "rules_digest", "SRDLookupError",
    "SKILL_ABILITY", "ability_mod", "proficiency_for_level", "has",
]

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


class SRDLookupError(KeyError):
    """Raised when a named SRD entry does not exist."""


def _load(fname: str) -> list | dict:
    with open(os.path.join(_DATA_DIR, fname), "r", encoding="utf-8") as f:
        return json.load(f)


def _index(rows: list[dict]) -> dict[str, dict]:
    return {r["name"].lower(): r for r in rows}


_RACES = _index(_load("races.json"))
_CLASSES = _index(_load("classes.json"))
_SPELLS = _index(_load("spells.json"))
_MONSTERS = _index(_load("monsters.json"))
_WEAPONS = _index(_load("weapons.json"))
_ARMOR = _index(_load("armor.json"))
_CONDITIONS = _index(_load("conditions.json"))
_EQUIPMENT = _index(_load("equipment.json"))
_META = _load("meta.json")

SKILL_ABILITY: dict[str, str] = dict(_META["skills"])
ABILITIES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]


def _get(table: dict[str, dict], name: str, what: str) -> dict:
    if not isinstance(name, str):
        raise SRDLookupError(f"{what} name must be a string, got {name!r}")
    row = table.get(name.strip().lower())
    if row is None:
        raise SRDLookupError(f"unknown {what}: {name!r}")
    return row


_PAREN_RE = re.compile(r"^\s*([^()]+?)\s*\(([^()]+)\)\s*$")


def race(name: str) -> dict:
    """Race lookup. Accepts both "Hill Dwarf" and the examples' "Dwarf (Hill)"."""
    if isinstance(name, str):
        key = name.strip().lower()
        if key in _RACES:
            return _RACES[key]
        m = _PAREN_RE.match(name)
        if m:
            alt = f"{m.group(2).strip()} {m.group(1).strip()}".lower()
            if alt in _RACES:
                return _RACES[alt]
    return _get(_RACES, name, "race")


def klass(name: str) -> dict:
    return _get(_CLASSES, name, "class")


def spell(name: str) -> dict:
    return _get(_SPELLS, name, "spell")


def monster(name: str) -> dict:
    return _get(_MONSTERS, name, "monster")


def weapon(name: str) -> dict:
    return _get(_WEAPONS, name, "weapon")


def armor(name: str) -> dict:
    return _get(_ARMOR, name, "armor")


def condition(name: str) -> dict:
    return _get(_CONDITIONS, name, "condition")


def equipment(name: str) -> dict:
    return _get(_EQUIPMENT, name, "equipment")


def meta() -> dict:
    return _META


def has(kind: str, name: str) -> bool:
    table = {"race": _RACES, "class": _CLASSES, "spell": _SPELLS, "monster": _MONSTERS,
             "weapon": _WEAPONS, "armor": _ARMOR, "condition": _CONDITIONS,
             "equipment": _EQUIPMENT}.get(kind)
    if table is None:
        raise SRDLookupError(f"unknown table {kind!r}")
    return isinstance(name, str) and name.strip().lower() in table


# ---------------------------------------------------------------- listings
def list_spells(klass_name: str | None = None, level: int | None = None) -> list[str]:
    out = []
    for row in _SPELLS.values():
        if klass_name and klass_name.lower() not in [c.lower() for c in row["classes"]]:
            continue
        if level is not None and row["level"] != level:
            continue
        out.append(row["name"])
    return sorted(out, key=lambda n: (_SPELLS[n.lower()]["level"], n))


def list_monsters(cr_max: float | None = None) -> list[str]:
    out = [r["name"] for r in _MONSTERS.values()
           if cr_max is None or float(r["cr"]) <= cr_max]
    return sorted(out, key=lambda n: (float(_MONSTERS[n.lower()]["cr"]), n))


def list_weapons(category: str | None = None) -> list[str]:
    out = [r["name"] for r in _WEAPONS.values()
           if category is None or category.lower() in r["category"].lower()]
    return sorted(out)


def list_armor(category: str | None = None) -> list[str]:
    return sorted(r["name"] for r in _ARMOR.values()
                  if category is None or r["category"] == category)


def list_races() -> list[str]:
    return sorted(r["name"] for r in _RACES.values())


def list_classes() -> list[str]:
    return sorted(r["name"] for r in _CLASSES.values())


def list_conditions() -> list[str]:
    return sorted(r["name"] for r in _CONDITIONS.values())


def list_equipment() -> list[str]:
    return sorted(r["name"] for r in _EQUIPMENT.values())


# ---------------------------------------------------------------- helpers
def ability_mod(score: int) -> int:
    return (int(score) - 10) // 2


def proficiency_for_level(level: int) -> int:
    return int(_META["proficiency_by_level"].get(str(int(level)), 2 + (int(level) - 1) // 4))


def spell_slots_for(klass_name: str, level: int) -> dict[int, int]:
    c = klass(klass_name)
    table = c.get("spell_slots") or {}
    row = table.get(str(int(level))) or {}
    return {int(k): int(v) for k, v in row.items()}


def deep(obj):
    """Defensive copy of a returned SRD record."""
    return copy.deepcopy(obj)


# ---------------------------------------------------------------- digest
_RULES_DIGEST = """\
D&D 5e COMBAT — RULES DIGEST (engine-authoritative)

The engine rolls every die and applies every rule. You never decide whether an
attack hits or how much damage it deals; you only choose an action from the
enumerated list. If a number appears in an event, it is final.

ACTION ECONOMY. Each turn a creature gets: one ACTION, one BONUS ACTION,
movement up to its speed, one free object interaction, and (between its turns)
one REACTION. Spending one does not refund another. Extra Attack lets a Fighter
of 5th level make two weapon attacks with one Attack action. Action Surge
(Fighter 2) grants one extra action. You may move before and between attacks.

READING THE ACTION LIST. Each option is printed as "[a3] label". Reply with the
id only. The label states cost and expected numbers, e.g.
"[a3] Attack Goblin 2 with Longsword (+5, 1d8+3)" — +5 is your attack bonus,
1d8+3 the damage. Costs shown: (action), (bonus), (movement), (free).
Options that need a parameter say so: move needs "path" (or pick one of the
suggested destinations), area spells need "point" (a grid square), multi-target
spells need "targets" (a list of ids). Anything not in the list is illegal this
turn — the engine already filtered by range, line of sight, remaining budget,
spell slots, and conditions. "[aN] End turn" is always available.

ATTACKS. d20 + ability modifier + proficiency vs the target's AC; ties go to the
attacker (equal = hit). A natural 20 always hits and is a CRITICAL: roll the
damage dice twice, add modifiers once. A natural 1 always misses. Advantage
(roll 2d20, keep high) and disadvantage (keep low) never stack — any of each
cancels to a normal roll.

WHAT GRANTS ADVANTAGE / DISADVANTAGE. Attacking a prone target from within
5 ft: advantage; from farther: disadvantage. Attacking a blinded, restrained,
paralyzed, stunned, or unconscious target: advantage. Paralyzed or unconscious
targets are auto-CRIT if you hit from within 5 ft. An invisible attacker has
advantage; attacks against it have disadvantage. If you are poisoned,
frightened (of a visible source), prone, blinded, or restrained you attack with
disadvantage. Firing a ranged weapon or a ranged spell while an enemy is within
5 ft of you: disadvantage. Beyond a ranged weapon's normal range (up to its long
range): disadvantage. Attacking a Dodging creature: disadvantage. The Help
action gives one ally advantage on its next attack against a chosen target.

COVER. Half cover: +2 AC and +2 to DEX saves. Three-quarters cover: +5 AC and
+5 to DEX saves. Total cover cannot be targeted.

SAVING THROWS. d20 + ability modifier + proficiency if proficient, vs the
effect's DC. Spell save DC = 8 + proficiency + spellcasting ability modifier.
Save-for-half effects deal half damage (rounded down) on a success.

SPELLS. One spell per slot; casting from a higher slot upcasts (extra dice or
targets, shown in the label). Cantrips cost no slot. You may cast only one
leveled spell per turn if the other was cast as a bonus action. CONCENTRATION:
you can concentrate on only one spell at a time; casting a second concentration
spell ends the first. Taking damage forces a CON save at DC 10 or half the
damage taken, whichever is higher; failure ends the spell. AoE shapes are
resolved on the 5-ft grid: sphere (radius from a point), cone (from you),
cube (from a point), line (from you). Everyone in the shape is affected,
allies included, unless the spell says otherwise.

MOVEMENT. The grid is 5-ft squares; every diagonal costs 5 ft (simplified).
Difficult terrain costs double. You cannot move through enemies. Standing up
from prone costs half your speed. Moving out of an enemy's reach provokes an
OPPORTUNITY ATTACK — the engine resolves it automatically as the enemy's
reaction. The Disengage action prevents that for the rest of your turn; Dash
doubles your movement.

DAMAGE, DYING, HEALING. Temporary hit points absorb damage first and never
stack. At 0 hp a PC falls unconscious and makes a death save each turn:
d20, 10+ succeeds; three successes = stable, three failures = dead; a natural
20 restores 1 hp, a natural 1 counts as two failures. Any damage while down is
one failure (two if it was a crit). Massive damage — leftover damage at or above
your hit point maximum — kills outright. Healing above 0 clears death saves;
Stabilize/Spare the Dying makes a creature stable at 0 hp. Monsters simply die
at 0 hp. Resistance halves damage, vulnerability doubles it, immunity zeroes it.

CONDITIONS: prone, grappled and restrained (speed 0), poisoned and frightened
(disadvantage on attacks and checks), blinded, paralyzed and stunned
(incapacitated: no actions or reactions), unconscious, charmed, invisible,
deafened, petrified. Exhaustion stacks in six levels. Timed conditions tick down
at end of turn, and many allow a repeat save then.
"""


@lru_cache(maxsize=1)
def rules_digest() -> str:
    """Stable plain-English combat digest for prompt caching (<= ~1200 tokens)."""
    return _RULES_DIGEST


def license_text() -> str:
    with open(os.path.join(_DATA_DIR, "LICENSE-SRD.txt"), "r", encoding="utf-8") as f:
        return f.read()
