"""The per-seat SRD reference that pads a player's cached system prefix.

Why this exists at all: Anthropic prompt caching only takes hold above a
minimum prefix length, and for the Haiku seats that minimum is 4,096 tokens.
The player's system block — persona, role rules and the rules digest — came
to about 1,900 tokens, so the `cache_control` marker on it was decoration:
across 823 player calls in the first sixteen live games not one token was
read from cache. This module makes the block long enough to cache, and makes
the length useful while it is at it: the full SRD text of every spell on the
sheet, the character's own class features, its weapons and armour, its racial
traits, and the conditions table, followed by the SRD's own wording of the
combat actions and the dying rules that the digest only summarises.

Everything here is a pure function of the `CharacterSheet`. Nothing reads game
state, a round number, or the clock — the block must be byte-identical on
every call of a game, or the cache is missed on every call of the game.

`engine.srd` is imported lazily and every lookup is guarded: the agents were
built to run against a partial engine, and a sheet that names a weapon this
SRD subset does not carry gets a line saying so rather than an exception. The
feature texts are SRD 5.1 (CC-BY-4.0; attribution in
`engine/data/LICENSE-SRD.txt`), keyed by the ids `classes.json` uses.
"""

from __future__ import annotations

from typing import Any

from .common import load_prompt

__all__ = ["seat_reference", "FEATURE_TEXT"]


#: SRD 5.1 text for the class features `engine/data/classes.json` names by id.
#: Race features are not here: `races.json` carries its traits with their own
#: text, and those are rendered from the data.
FEATURE_TEXT: dict[str, tuple[str, str]] = {
    "fighting_style_defense": (
        "Fighting Style: Defense",
        "While you are wearing armor, you gain a +1 bonus to AC.",
    ),
    "second_wind": (
        "Second Wind",
        "You have a limited well of stamina that you can draw on to protect "
        "yourself from harm. On your turn, you can use a bonus action to regain "
        "hit points equal to 1d10 + your fighter level. Once you use this "
        "feature, you must finish a short or long rest before you can use it "
        "again.",
    ),
    "action_surge": (
        "Action Surge",
        "Starting at 2nd level, you can push yourself beyond your normal limits "
        "for a moment. On your turn, you can take one additional action on top "
        "of your regular action and a possible bonus action. Once you use this "
        "feature, you must finish a short or long rest before you can use it "
        "again.",
    ),
    "improved_critical": (
        "Improved Critical (Champion)",
        "Beginning when you choose this archetype at 3rd level, your weapon "
        "attacks score a critical hit on a roll of 19 or 20.",
    ),
    "extra_attack": (
        "Extra Attack",
        "Beginning at 5th level, you can attack twice, instead of once, whenever "
        "you take the Attack action on your turn.",
    ),
    "sneak_attack": (
        "Sneak Attack",
        "Beginning at 1st level, you know how to strike subtly and exploit a "
        "foe's distraction. Once per turn, you can deal extra damage to one "
        "creature you hit with an attack if you have advantage on the attack "
        "roll. The attack must use a finesse or a ranged weapon. You don't need "
        "advantage on the attack roll if another enemy of the target is within "
        "5 feet of it, that enemy isn't incapacitated, and you don't have "
        "disadvantage on the attack roll. The extra damage is 1d6 at 1st level, "
        "2d6 at 3rd and 3d6 at 5th.",
    ),
    "expertise": (
        "Expertise",
        "At 1st level, choose two of your skill proficiencies. Your proficiency "
        "bonus is doubled for any ability check you make that uses either of "
        "the chosen proficiencies.",
    ),
    "thieves_cant": (
        "Thieves' Cant",
        "During your rogue training you learned thieves' cant, a secret mix of "
        "dialect, jargon, and code that allows you to hide messages in "
        "seemingly normal conversation. Only another creature that knows "
        "thieves' cant understands such messages.",
    ),
    "cunning_action": (
        "Cunning Action",
        "Starting at 2nd level, your quick thinking and agility allow you to "
        "move and act quickly. You can take a bonus action on each of your "
        "turns in combat. This action can be used only to take the Dash, "
        "Disengage, or Hide action.",
    ),
    "fast_hands": (
        "Fast Hands (Thief)",
        "Starting at 3rd level, you can use the bonus action granted by your "
        "Cunning Action to make a Dexterity (Sleight of Hand) check, use your "
        "thieves' tools to disarm a trap or open a lock, or take the Use an "
        "Object action.",
    ),
    "uncanny_dodge": (
        "Uncanny Dodge",
        "Starting at 5th level, when an attacker that you can see hits you with "
        "an attack, you can use your reaction to halve the attack's damage "
        "against you. The engine applies this for you.",
    ),
    "spellcasting": (
        "Spellcasting",
        "You cast the spells you have prepared by expending a spell slot of the "
        "spell's level or higher; cantrips cost no slot. Your spellcasting "
        "ability modifier is added to your spell attack rolls, and your spell "
        "save DC is 8 + your proficiency bonus + that modifier. You regain all "
        "expended spell slots when you finish a long rest.",
    ),
    "disciple_of_life": (
        "Disciple of Life (Life Domain)",
        "Also starting at 1st level, your healing spells are more effective. "
        "Whenever you use a spell of 1st level or higher to restore hit points "
        "to a creature, the creature regains additional hit points equal to "
        "2 + the spell's level.",
    ),
    "channel_divinity_turn_undead": (
        "Channel Divinity: Turn Undead",
        "As an action, you present your holy symbol and speak a prayer censuring "
        "the undead. Each undead that can see or hear you within 30 feet of you "
        "must make a Wisdom saving throw. If the creature fails its saving "
        "throw, it is turned for 1 minute or until it takes any damage. A "
        "turned creature must spend its turns trying to move as far away from "
        "you as it can, and it can't willingly move to a space within 30 feet "
        "of you. It also can't take reactions. For its action, it can use only "
        "the Dash action or try to escape from an effect that prevents it from "
        "moving. You can use your Channel Divinity once, and regain the use "
        "when you finish a short or long rest.",
    ),
    "channel_divinity_preserve_life": (
        "Channel Divinity: Preserve Life (Life Domain)",
        "Starting at 2nd level, you can use your Channel Divinity to heal the "
        "badly injured. As an action, you present your holy symbol and evoke "
        "healing energy that can restore a number of hit points equal to five "
        "times your cleric level, divided as you choose among creatures within "
        "30 feet of you, to no more than half of each one's hit point maximum. "
        "(This engine offers Channel Divinity only as Turn Undead; Preserve "
        "Life will not appear in your action list.)",
    ),
    "destroy_undead_half": (
        "Destroy Undead",
        "Starting at 5th level, when an undead fails its saving throw against "
        "your Turn Undead feature, the creature is instantly destroyed if its "
        "challenge rating is at or below 1/2.",
    ),
    "arcane_recovery": (
        "Arcane Recovery",
        "You have learned to regain some of your magical energy by studying your "
        "spellbook. Once per day when you finish a short rest, you can choose "
        "expended spell slots to recover, of a combined level equal to or less "
        "than half your wizard level (rounded up). It happens between fights, "
        "never during one.",
    ),
    "evocation_savant": (
        "Evocation Savant (School of Evocation)",
        "Beginning when you select this school at 2nd level, the gold and time "
        "you must spend to copy an evocation spell into your spellbook is "
        "halved.",
    ),
    "sculpt_spells": (
        "Sculpt Spells",
        "Beginning at 2nd level, you can create pockets of relative safety "
        "within the effects of your evocation spells. When you cast an "
        "evocation spell that affects other creatures that you can see, you can "
        "choose a number of them equal to 1 + the spell's level. The chosen "
        "creatures automatically succeed on their saving throws against the "
        "spell, and they take no damage if they would normally take half damage "
        "on a successful save. The engine sculpts around your allies for you.",
    ),
}

_ORDINAL = {0: "cantrip", 1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th",
            6: "6th", 7: "7th", 8: "8th", 9: "9th"}


def _srd() -> Any:
    try:
        from engine import srd  # noqa: PLC0415

        return srd
    except Exception:  # noqa: BLE001 - engine absent or partial
        return None


def _lookup(srd: Any, table: str, name: str) -> dict | None:
    fn = getattr(srd, table, None) if srd is not None else None
    if fn is None or not isinstance(name, str) or not name.strip():
        return None
    try:
        row = fn(name)
    except Exception:  # noqa: BLE001 - not in this SRD subset
        return None
    return row if isinstance(row, dict) else None


def _wrap(text: str) -> str:
    return " ".join(str(text or "").split())


# --- sections ----------------------------------------------------------------


def _equipment(sheet: Any, srd: Any) -> list[str]:
    out: list[str] = []
    for name in getattr(sheet, "weapons", None) or []:
        w = _lookup(srd, "weapon", name)
        if w is None:
            out.append(f"- {name}: not in this SRD subset; the action list states its numbers.")
            continue
        bits = [f"{w.get('damage', '?')} {w.get('damage_type', '')}".strip()]
        if w.get("versatile"):
            bits.append(f"versatile ({w['versatile']} two-handed)")
        rng = w.get("range")
        if w.get("ranged") and rng:
            bits.append(f"range {rng[0]}/{rng[1]} ft")
        elif w.get("thrown") and rng:
            bits.append(f"reach {w.get('reach', 5)} ft or thrown {rng[0]}/{rng[1]} ft")
        else:
            bits.append(f"reach {w.get('reach', 5)} ft")
        props = [p for p in (w.get("properties") or []) if p != "versatile"]
        if props:
            bits.append("properties: " + ", ".join(props))
        out.append(f"- {w.get('name', name)} ({w.get('category', 'weapon')}): " + "; ".join(bits))
    armor = getattr(sheet, "armor", None)
    if armor:
        a = _lookup(srd, "armor", armor)
        if a is None:
            out.append(f"- Armor: {armor}.")
        else:
            cap = a.get("dex_cap")
            dex = ("no DEX bonus" if cap == 0 else f"DEX bonus up to +{cap}" if cap else "full DEX bonus")
            line = f"- Armor: {a.get('name', armor)} ({a.get('category', '?')}), base AC {a.get('base_ac', '?')}, {dex}"
            if a.get("stealth_disadvantage"):
                line += "; disadvantage on Stealth checks"
            out.append(line + ".")
    else:
        out.append("- Armor: none (AC is 10 + DEX modifier, plus any spell such as Mage Armor).")
    if getattr(sheet, "shield", False):
        out.append("- Shield: +2 AC while wielded; it occupies one hand.")
    for item in getattr(sheet, "inventory", None) or []:
        e = _lookup(srd, "equipment", item)
        note = _wrap((e or {}).get("note", "")) if e else ""
        use = (e or {}).get("use") if e else None
        if use and use.get("kind") == "heal":
            out.append(f"- {item}: {note or 'regain hit points'} It appears in your action list as Use an item "
                       f"({use.get('cost', 'action')}) while you carry one, and can be given to an adjacent ally.")
        elif note:
            out.append(f"- {item}: {note}")
    return out


def _race_traits(sheet: Any, srd: Any) -> list[str]:
    race = getattr(sheet, "race", "") or ""
    row = _lookup(srd, "race", race)
    if row is None:
        return [f"- {race or 'unknown race'}: no traits recorded in this SRD subset."]
    out: list[str] = []
    speed = row.get("speed")
    dark = row.get("darkvision")
    head = f"- {row.get('name', race)}: size {row.get('size', 'M')}, speed {speed} ft"
    if dark:
        head += f", darkvision {dark} ft"
    out.append(head + ".")
    for t in row.get("traits") or []:
        name = t.get("name") if isinstance(t, dict) else None
        desc = _wrap(t.get("desc", "")) if isinstance(t, dict) else _wrap(str(t))
        if name == "Age" or not desc:
            continue
        out.append(f"- {name}: {desc}" if name else f"- {desc}")
    res = row.get("damage_resistances") or []
    if res:
        out.append("- Resistant to " + ", ".join(res) + " damage.")
    return out


def _class_features(sheet: Any, srd: Any) -> list[str]:
    klass_name = getattr(sheet, "klass", "") or ""
    level = int(getattr(sheet, "level", 1) or 1)
    row = _lookup(srd, "klass", klass_name)
    out: list[str] = []
    if row is not None:
        head = f"- {row.get('name', klass_name)} {level}: hit die d{row.get('hit_die', '?')}"
        saves = row.get("saving_throws") or []
        if saves:
            head += ", proficient in " + " and ".join(saves) + " saving throws"
        out.append(head + ".")
    ids = [f for f in (getattr(sheet, "features", None) or []) if f in FEATURE_TEXT]
    if not ids and row is not None:
        # A sheet built without features (a test fake, an older snapshot):
        # read the class table for the levels reached.
        for lv in range(1, level + 1):
            ids.extend(
                f for f in (row.get("levels") or {}).get(str(lv), {}).get("features", [])
                if f in FEATURE_TEXT
            )
    seen: set[str] = set()
    for fid in ids:
        if fid in seen:
            continue
        seen.add(fid)
        name, text = FEATURE_TEXT[fid]
        out.append(f"- {name}: {text}")
    if not out:
        out.append("- (no class features recorded)")
    return out


def _spell_mechanics(effect: dict) -> str:
    """The engine's own reading of a spell record, in one line."""
    bits: list[str] = []
    kind = effect.get("kind")
    if kind == "attack":
        bits.append(f"{effect.get('attack_type') or 'spell'} spell attack")
    elif kind == "save" or (kind == "debuff" and effect.get("save")):
        bits.append(f"{effect.get('save')} save" + (", half damage on a success" if effect.get("half_on_save") else ""))
    elif kind == "heal":
        bits.append("healing")
    elif kind == "buff":
        bits.append("buff")
    elif kind == "debuff":
        bits.append("debuff")
    elif kind == "utility":
        bits.append("utility")
    if effect.get("damage"):
        bits.append(f"{effect['damage']} {effect.get('damage_type') or ''}".strip())
    area = effect.get("area") or {}
    if area.get("shape"):
        bits.append(f"{area['shape']} {area.get('size', '')} ft".strip())
    if effect.get("targets") and int(effect.get("targets") or 0) > 1:
        bits.append(f"up to {effect['targets']} targets")
    if effect.get("conditions_applied"):
        bits.append("applies " + ", ".join(str(c) for c in effect["conditions_applied"]))
    if effect.get("upcast"):
        bits.append(f"upcast {effect['upcast']}")
    if effect.get("concentration"):
        bits.append("CONCENTRATION")
    return "; ".join(bits)


def _spells(sheet: Any, srd: Any) -> list[str]:
    names = list(getattr(sheet, "spells_known", None) or [])
    if not names:
        return []
    out: list[str] = []
    rows = []
    for name in names:
        row = _lookup(srd, "spell", name)
        rows.append((int((row or {}).get("level", 0) or 0), name, row))
    for level, name, row in sorted(rows, key=lambda r: (r[0], r[1])):
        if row is None:
            out.append(f"- {name}: not in this SRD subset; the action list states its numbers.")
            continue
        head = f"{_ORDINAL.get(level, str(level))}"
        if level:
            head += f"-level {row.get('school', '')}".rstrip()
        else:
            head = f"{row.get('school', '')} cantrip".strip()
        meta = [head]
        for key in ("casting_time", "range", "components", "duration"):
            if row.get(key):
                meta.append(str(row[key]))
        mech = _spell_mechanics(row.get("effect") or {})
        out.append(f"- {row.get('name', name)} ({'; '.join(meta)}). {_wrap(row.get('desc', ''))}"
                   + (f" [Engine: {mech}]" if mech else ""))
    return out


def _conditions(srd: Any) -> list[str]:
    names = []
    try:
        names = list(srd.list_conditions()) if srd is not None else []
    except Exception:  # noqa: BLE001
        names = []
    out: list[str] = []
    for name in names:
        row = _lookup(srd, "condition", name)
        if row is None:
            continue
        effects = " ".join(_wrap(e) for e in (row.get("effects") or []))
        out.append(f"- {str(row.get('name', name)).capitalize()}: {effects}")
    return out


def seat_reference(sheet: Any) -> str:
    """The reference block for one seat. Deterministic; state-free."""
    srd = _srd()
    parts: list[str] = [
        "SRD REFERENCE FOR THIS CHARACTER",
        "What follows is the rules text behind your sheet, for planning. The action list "
        "you are given each turn is still the only source of what you can do right now, "
        "and its numbers are the engine's, not yours to recompute.",
        "",
        "YOUR WEAPONS AND ARMOR",
        *_equipment(sheet, srd),
        "",
        "YOUR RACIAL TRAITS",
        *_race_traits(sheet, srd),
        "",
        "YOUR CLASS FEATURES",
        *_class_features(sheet, srd),
    ]
    spells = _spells(sheet, srd)
    if spells:
        parts += ["", "YOUR SPELLS (full SRD text)", *spells]
    parts += ["", load_prompt("srd_combat_actions.txt").rstrip()]
    parts += ["", load_prompt("srd_dying.txt").rstrip()]
    conds = _conditions(srd)
    if conds:
        parts += ["", "CONDITIONS (SRD)", *conds]
    return "\n".join(parts)
