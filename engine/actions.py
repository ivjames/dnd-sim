"""Combat actions: enumerate what a combatant may do, then resolve it.

CONTRACTS.md §1.6. Pure and deterministic: every function takes a GameState
and returns a NEW GameState plus the events that explain what happened. All
randomness comes from the RNG snapshot carried in `state.rng`. Nothing here
does I/O, spawns threads, or talks to a model — the LLMs only ever pick one of
the `ActionTemplate`s this module hands them.

The resolver reads the SRD tables in engine/data as they are: the closed
`effect` vocabulary in spells.json, the `extra` riders / `multiattack` /
`bonus_actions` / `spellcasting` fields of monsters.json, the per-condition
flags in conditions.json, and `equipment[*].use`. See the engine Amendment in
CONTRACTS.md for the exact keys consumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from engine import srd
from engine.dice import RNG, RollResult, average_of, parse_expr
from engine.events import Event
from engine.state import Combatant, Condition, GameState, Grid

__all__ = [
    "ActionTemplate", "Action", "IllegalAction",
    "legal_actions", "apply", "start_combat", "advance_turn", "combat_over",
    "reactions_for", "skill_check",
]

MAX_LABEL = 80
MAX_RANGED_TARGETS = 5
MAX_SPELL_TARGETS = 4
MAX_SUGGESTED = 6
CANTRIP_STEPS = (5, 11, 17)  # cantrip damage dice grow at these caster levels

# ---- condition semantics, straight from conditions.json ------------------------
_COND: dict[str, dict] = {name: srd.condition(name) for name in srd.list_conditions()}
_ATTACKER_DISADVANTAGE = tuple(n for n, c in _COND.items() if c.get("attack_own") == "disadvantage")
_ATTACKER_ADVANTAGE = tuple(n for n, c in _COND.items() if c.get("attack_own") == "advantage")
_TARGET_ADVANTAGE = tuple(n for n, c in _COND.items() if c.get("attack_by") == "advantage")
_TARGET_DISADVANTAGE = tuple(n for n, c in _COND.items() if c.get("attack_by") == "disadvantage")
_AUTO_CRIT = tuple(n for n, c in _COND.items() if c.get("auto_crit_within_5"))
_AUTO_FAIL_SAVES = {n: tuple(c.get("auto_fail_saves") or ()) for n, c in _COND.items()}
_SAVE_DISADVANTAGE = {n: tuple(c.get("save_disadvantage") or ()) for n, c in _COND.items()}
# Engine-only markers that are not SRD conditions: "hidden" (after a successful
# Hide) and "turned" (Turn Undead). They ride on Combatant.conditions too.
_TURN_FLAGS = (
    "dodging", "disengaged", "sneak_attack_used", "martial_advantage_used",
    "light_weapon_attacked", "cast_bonus_spell", "cast_action_spell",
    "no_reactions", "multi_index", "moves_taken", "dashes_taken", "turn_ended",
)


# ---------------------------------------------------------------- dataclasses
@dataclass
class ActionTemplate:
    id: str
    type: str
    label: str
    params: dict = field(default_factory=dict)
    needs: list[str] = field(default_factory=list)
    cost: str = "action"

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "label": self.label,
                "params": dict(self.params), "needs": list(self.needs), "cost": self.cost}


@dataclass
class Action:
    actor: str
    template_id: str
    params: dict = field(default_factory=dict)
    speech: str | None = None

    def to_dict(self) -> dict:
        return {"actor": self.actor, "template_id": self.template_id,
                "params": dict(self.params or {}), "speech": self.speech}


class IllegalAction(ValueError):
    """The requested action is not legal for this actor in this state."""


# ---------------------------------------------------------------- small helpers
def _pos(c: Combatant) -> tuple[int, int]:
    return (int(c.position[0]), int(c.position[1]))


def _dist(a: Combatant, b: Combatant) -> int:
    return Grid.distance_ft(_pos(a), _pos(b))


def _label(s: str) -> str:
    s = " ".join(s.split())
    return s if len(s) <= MAX_LABEL else s[: MAX_LABEL - 1] + "…"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def _alive(c: Combatant) -> bool:
    """Not dead and above 0 HP (may still be incapacitated)."""
    return (not c.dead) and c.hp > 0


def _can_act(c: Combatant) -> bool:
    return _alive(c) and not c.incapacitated


def _can_react(c: Combatant) -> bool:
    return (_can_act(c) and not c.turn.get("reaction", False)
            and not c.flags.get("no_reactions") and not c.has_condition("turned"))


def _hostile(a: Combatant, b: Combatant) -> bool:
    return a.side != b.side and a.side != "neutral" and b.side != "neutral"


def _with_mod(expr: str, mod: int) -> str:
    return expr if mod == 0 else f"{expr}{mod:+d}"


def _max_of(expr: str) -> int:
    return sum(sign * (count * faces if faces else count) for sign, count, faces in parse_expr(expr))


def _scale_dice(expr: str, times: int) -> str:
    """'1d10' x3 -> '3d10' (cantrip scaling); flat parts untouched."""
    out = []
    for i, (sign, count, faces) in enumerate(parse_expr(expr)):
        s = "" if (i == 0 and sign > 0) else ("+" if sign > 0 else "-")
        out.append(f"{s}{count * times}d{faces}" if faces else f"{s}{count}")
    return "".join(out)


def _rng_of(state: GameState) -> RNG:
    if state.rng:
        return RNG.from_state(state.rng)
    return RNG(state.seed)


def _ev(state: GameState, events: list[Event], kind: str, text: str,
        actor: str | None = None, **data: Any) -> Event:
    state.event_seq += 1
    ev = Event(seq=state.event_seq, round=state.round, kind=kind, actor=actor,
               text=text, data=data)
    events.append(ev)
    return ev


def _roll_text(r: RollResult) -> str:
    if r.mode != "normal":
        return f"{r.expr} [{'/'.join(str(x) for x in r.rolls)} {r.mode[:3]}] → {r.total}"
    return f"{r.expr} → {r.total}"


def _cover_bonus(cover: str | None) -> int:
    return {"half": 2, "three_quarters": 5}.get(cover or "", 0)


def _trait(c: Combatant, key: str) -> dict | None:
    """Monster trait by id, or by slugified name when the record has no id."""
    for t in (c.stat_block or {}).get("traits", []):
        if t.get("id") == key or _slug(t.get("name", "")) == key:
            return t
    return None


def _bonus_actions(c: Combatant) -> list[str]:
    return [str(x).lower() for x in (c.stat_block or {}).get("bonus_actions", []) or []]


# ---- buffs (timed modifiers carried in Combatant.flags["buffs"]) ----------------
def _buffs(c: Combatant) -> list[dict]:
    return c.flags.setdefault("buffs", [])


def _has_buff(c: Combatant, name: str) -> bool:
    return any(b.get("name") == name for b in c.flags.get("buffs", []))


def _buff_any(c: Combatant, key: str) -> bool:
    return any(b.get(key) for b in c.flags.get("buffs", []))


def _recompute(c: Combatant) -> None:
    """Fold the active buff list into the flat modifiers state.py consults."""
    buffs = c.flags.get("buffs", [])
    ac = sum(int(b.get("ac", 0)) for b in buffs)
    pen = sum(int(b.get("speed_penalty", 0)) for b in buffs)
    mult = 1
    for b in buffs:
        mult *= int(b.get("speed_multiplier", 1) or 1)
    for key, val, default in (("ac_bonus", ac, 0), ("speed_penalty", pen, 0), ("speed_multiplier", mult, 1)):
        if val != default:
            c.flags[key] = val
        else:
            c.flags.pop(key, None)
    if not buffs:
        c.flags.pop("buffs", None)


def _add_buff(c: Combatant, buff: dict) -> None:
    buffs = _buffs(c)
    buffs[:] = [b for b in buffs if b.get("name") != buff.get("name")]
    buffs.append(buff)
    _recompute(c)


def _remove_buffs(c: Combatant, *, name: str | None = None, source: str | None = None) -> list[dict]:
    buffs = c.flags.get("buffs", [])
    gone = [b for b in buffs
            if (name is not None and b.get("name") == name) or (source is not None and b.get("source") == source)]
    if gone:
        c.flags["buffs"] = [b for b in buffs if b not in gone]
        for b in gone:
            if b.get("max_hp"):
                c.max_hp = max(1, c.max_hp - int(b["max_hp"]))
                c.hp = min(c.hp, c.max_hp)
            if b.get("extra_action"):
                c.flags["lethargic"] = True  # Haste ends: the target loses its next turn
        _recompute(c)
    return gone


def _bonus_die_total(rng: RNG, c: Combatant, key: str) -> tuple[int, str]:
    """Roll every buff die of `key` (attack_die/save_die/check_die); consume `uses`."""
    total = 0
    parts = []
    for b in list(c.flags.get("buffs", [])):
        die = b.get(key)
        if not die:
            continue
        r = rng.roll(die)
        total += r.total
        parts.append(f"+{r.total} ({b['name']})")
        if b.get("uses") is not None:
            b["uses"] = int(b["uses"]) - 1
            if b["uses"] <= 0:
                _remove_buffs(c, name=b["name"])
    return total, "".join(f" {p}" for p in parts)


# ---- casters ------------------------------------------------------------------
def _caster_level(c: Combatant) -> int:
    if c.sheet:
        return int(c.sheet.level)
    sc = (c.stat_block or {}).get("spellcasting") or {}
    return int(sc.get("level", 1))


def _known_spells(c: Combatant) -> list[str]:
    if c.sheet:
        return list(c.sheet.spells_known)
    sc = (c.stat_block or {}).get("spellcasting") or {}
    out = list(sc.get("cantrips", []))
    spells = sc.get("spells") or {}
    if isinstance(spells, dict):
        for lv in sorted(spells, key=lambda k: int(k)):
            out.extend(spells[lv])
    else:
        out.extend(spells)
    return out


def _slots(c: Combatant) -> dict[int, int]:
    slots = c.resources.get("spell_slots") or {}
    return {int(k): int(v) for k, v in slots.items()}


def _proficient_with(c: Combatant, row: dict) -> bool:
    if not c.sheet:
        return True
    profs: list[str] = []
    for getter, name in ((srd.klass, c.sheet.klass), (srd.race, c.sheet.race)):
        try:
            profs += getter(name).get("weapon_proficiencies", [])
        except srd.SRDLookupError:
            pass
    cat = row["category"].lower()
    for p in profs:
        pl = p.lower()
        if pl == row["name"].lower() or (pl in ("simple", "martial") and cat.startswith(pl)):
            return True
    return False


# ---------------------------------------------------------------- attack specs
def _pc_weapon_specs(c: Combatant) -> list[dict]:
    """One melee spec (and one thrown/ranged spec where relevant) per distinct weapon."""
    out: list[dict] = []
    seen: set[str] = set()
    names = list(c.sheet.weapons) if c.sheet else []
    for wname in names:
        try:
            row = srd.weapon(wname)
        except srd.SRDLookupError:
            continue
        if row["name"] in seen:
            continue
        seen.add(row["name"])
        props = [p.lower() for p in row.get("properties", [])]
        finesse = bool(row.get("finesse")) or "finesse" in props
        thrown = bool(row.get("thrown")) or "thrown" in props
        light = bool(row.get("light")) or "light" in props
        prof = c.proficiency if _proficient_with(c, row) else 0
        str_mod, dex_mod = c.mod("STR"), c.mod("DEX")
        base = {"damage_type": row["damage_type"], "on_hit": None, "is_spell": False,
                "properties": props, "light": light, "finesse": finesse}
        if row["ranged"]:
            mod = max(str_mod, dex_mod) if (thrown and finesse) else dex_mod
            out.append({**base, "name": row["name"], "bonus": mod + prof,
                        "damage": _with_mod(row["damage"], mod), "dice": row["damage"], "mod": mod,
                        "ranged": True, "reach": 5,
                        "range": tuple(row["range"]) if row.get("range") else (30, 120)})
            continue
        mod = max(str_mod, dex_mod) if finesse else str_mod
        dice = row["damage"]
        if row.get("versatile") and not (c.sheet and c.sheet.shield):
            dice = row["versatile"]
        out.append({**base, "name": row["name"], "bonus": mod + prof,
                    "damage": _with_mod(dice, mod), "dice": dice, "mod": mod,
                    "ranged": False, "reach": int(row.get("reach") or 5), "range": None})
        if thrown:
            out.append({**base, "name": f"{row['name']} (thrown)", "weapon": row["name"], "bonus": mod + prof,
                        "damage": _with_mod(row["damage"], mod), "dice": row["damage"], "mod": mod,
                        "ranged": True, "reach": 5, "range": tuple(row["range"] or (20, 60))})
    if not any(not s["ranged"] for s in out):
        try:
            row = srd.weapon("Unarmed strike")
            mod = c.mod("STR")
            out.append({"name": row["name"], "bonus": mod + c.proficiency,
                        "damage": _with_mod(row["damage"], mod), "dice": row["damage"], "mod": mod,
                        "damage_type": row["damage_type"], "ranged": False, "reach": 5, "range": None,
                        "properties": [], "light": False, "finesse": False, "on_hit": None, "is_spell": False})
        except srd.SRDLookupError:
            pass
    return out


_RECHARGE_RE = re.compile(r"recharge\s+(\d)(?:\s*[-–]\s*(\d))?", re.IGNORECASE)


def _recharge_min(action: dict) -> int | None:
    m = _RECHARGE_RE.search(action.get("desc") or "")
    return int(m.group(1)) if m else None


def _monster_specs(c: Combatant) -> list[dict]:
    out: list[dict] = []
    block = c.stat_block or {}
    recharge = c.resources.get("recharge") or {}
    for a in block.get("actions", []):
        kind = a.get("kind")
        if kind not in ("melee_weapon", "ranged_weapon", "melee_or_ranged"):
            continue
        if _recharge_min(a) is not None and not recharge.get(a["name"], True):
            continue
        base = {
            "name": a["name"], "bonus": int(a.get("attack_bonus", 0)),
            "damage": a.get("damage") or "0", "dice": a.get("damage") or "0", "mod": 0,
            "damage_type": a.get("damage_type", "bludgeoning"),
            "properties": [], "light": False, "finesse": False,
            "on_hit": list(a.get("extra") or []), "is_spell": False,
            "recharge": _recharge_min(a),
        }
        if kind in ("melee_weapon", "melee_or_ranged"):
            out.append({**base, "ranged": False, "reach": int(a.get("reach") or 5), "range": None})
        if kind in ("ranged_weapon", "melee_or_ranged"):
            spec = {**base, "ranged": True, "reach": 5, "range": tuple(a.get("range") or (30, 120))}
            if kind == "melee_or_ranged":
                spec["name"] = f"{a['name']} (thrown)"
                spec["weapon"] = a["name"]
            out.append(spec)
    return out


def _attack_specs(c: Combatant) -> list[dict]:
    return _pc_weapon_specs(c) if c.kind == "pc" else _monster_specs(c)


def _best_melee_spec(c: Combatant) -> dict | None:
    best = None
    for s in _attack_specs(c):
        if s["ranged"]:
            continue
        try:
            avg = average_of(s["damage"])
        except Exception:  # noqa: BLE001 - "0" or odd expressions
            avg = 0
        if best is None or avg > best[0]:
            best = (avg, s)
    return best[1] if best else None


def _multiattack(c: Combatant) -> list[dict]:
    """monsters.json: "multiattack": ["Scimitar", "Scimitar"] (ordered names)."""
    out = []
    for entry in (c.stat_block or {}).get("multiattack") or []:
        out.append({"name": entry} if isinstance(entry, str) else dict(entry))
    return out


def _attacks_per_action(c: Combatant) -> int:
    if c.kind == "pc":
        return 2 if c.has_feature("extra_attack") else 1
    return max(1, len(_multiattack(c)))


def _spec_in_range(state: GameState, a: Combatant, t: Combatant, spec: dict) -> bool:
    d = _dist(a, t)
    if spec["ranged"]:
        rng = spec.get("range") or (30, 120)
        return d <= rng[1] and state.grid.has_line_of_sight(_pos(a), _pos(t))
    return d <= spec.get("reach", 5)


def _enemy_targets(state: GameState, a: Combatant) -> list[Combatant]:
    """Living hostile creatures, nearest first (stable order)."""
    out = [c for c in state.combatants.values() if _hostile(a, c) and _alive(c)]
    out.sort(key=lambda c: (_dist(a, c), c.id))
    return out


def _usable_items(c: Combatant) -> list[tuple[str, dict]]:
    """Inventory entries whose equipment record declares a `use` (Potion of healing)."""
    out = []
    for item in dict.fromkeys(c.inventory):
        try:
            row = srd.equipment(item)
        except srd.SRDLookupError:
            continue
        use = row.get("use")
        if use and use.get("kind") == "heal":
            out.append((row["name"], use))
    return out


# ---------------------------------------------------------------- legal actions
def legal_actions(state: GameState, actor_id: str) -> list[ActionTemplate]:
    if actor_id not in state.combatants:
        raise IllegalAction(f"no such combatant {actor_id!r}")
    actor = state.combatants[actor_id]
    out: list[ActionTemplate] = []
    counter = [0]

    def add(type_: str, label: str, params: dict | None = None, needs: list[str] | None = None,
            cost: str = "action") -> ActionTemplate:
        counter[0] += 1
        t = ActionTemplate(id=f"a{counter[0]}", type=type_, label=_label(label),
                           params=params or {}, needs=needs or [], cost=cost)
        out.append(t)
        return t

    turn = actor.turn
    if not _can_act(actor) or actor.flags.get("lethargic"):
        add("end_turn", "End turn", {}, [], "free")
        return out

    action_free = not turn.get("action", False)
    haste_free = bool(turn.get("haste_action", False))
    bonus_free = not turn.get("bonus", False)
    attacks_left = int(turn.get("attacks_left", 0))
    movement_left = int(turn.get("movement_left", 0))
    turned = actor.has_condition("turned")
    adjacent_enemies = [c for c in _enemy_targets(state, actor)
                        if _dist(actor, c) <= c.reach_ft() and _can_act(c)]

    if not turned:
        # ---- weapon attacks
        if action_free or attacks_left > 0 or haste_free:
            specs = _attack_specs(actor)
            seq = _multiattack(actor)
            if attacks_left > 0 and seq:
                idx = int(actor.flags.get("multi_index", 0))
                if idx < len(seq):
                    want = seq[idx]["name"]
                    specs = [s for s in specs if s.get("weapon", s["name"]) == want]
            for spec in specs:
                _attack_templates(state, actor, spec, add)
        # ---- off-hand (two-weapon fighting)
        if bonus_free and actor.kind == "pc" and actor.flags.get("light_weapon_attacked"):
            _offhand_templates(state, actor, add)
        # ---- spiritual weapon strike
        if bonus_free and actor.flags.get("spiritual_weapon"):
            sw = actor.flags["spiritual_weapon"]
            for t in _enemy_targets(state, actor):
                if _dist(actor, t) <= 60 and state.grid.has_line_of_sight(_pos(actor), _pos(t)):
                    add("attack", f"Attack {t.name} with Spiritual Weapon ({sw['bonus']:+d}, {sw['damage']} force)",
                        {"target": t.id, "spiritual_weapon": True}, [], "bonus")
        # ---- flaming sphere ram
        if bonus_free and actor.flags.get("flaming_sphere"):
            fs = actor.flags["flaming_sphere"]
            pts = _sphere_ram_points(state, actor, tuple(fs["pos"]))
            if pts:
                add("cast", f"Move Flaming Sphere (30 ft) and ram: DEX DC {fs['dc']}, {fs['damage']} fire half; choose point",
                    {"spell": "Flaming Sphere", "slot": 0, "ram": True, "suggested": [list(p) for p in pts]},
                    ["point"], "bonus")
        # ---- spells
        if action_free or bonus_free:
            _spell_templates(state, actor, add, action_free, bonus_free)
        # ---- class features
        if bonus_free and actor.has_feature("second_wind") and actor.resources.get("second_wind", 0) > 0 \
                and actor.hp < actor.max_hp:
            add("second_wind", f"Second Wind: regain 1d10+{actor.sheet.level} HP", {}, [], "bonus")
        if actor.has_feature("action_surge") and actor.resources.get("action_surge", 0) > 0 \
                and not action_free and attacks_left == 0:
            add("action_surge", "Action Surge: take one additional action this turn", {}, [], "free")
        if action_free and actor.has_feature("channel_divinity_turn_undead") \
                and actor.resources.get("channel_divinity", 0) > 0:
            undead = [c for c in _enemy_targets(state, actor)
                      if c.is_undead() and _dist(actor, c) <= 30
                      and state.grid.has_line_of_sight(_pos(actor), _pos(c))]
            if undead:
                add("channel_divinity",
                    f"Channel Divinity: Turn Undead (WIS DC {actor.spell_dc}, {len(undead)} undead in 30 ft)",
                    {"feature": "turn_undead"}, [], "action")
        # ---- items
        if action_free:
            for item, use in _usable_items(actor):
                if actor.hp < actor.max_hp:
                    add("use_item", f"Drink {item} ({use['amount']} HP)", {"item": item, "target": actor.id}, [], "action")
                for ally in state.allies_of(actor.id):
                    if not ally.dead and ally.hp <= 0 and _dist(actor, ally) <= 5:
                        add("use_item", f"Give {item} to {ally.name} ({use['amount']} HP)",
                            {"item": item, "target": ally.id}, [], "action")
        # ---- generic actions
        if action_free:
            add("dodge", "Dodge: attacks against you have disadvantage until your next turn", {}, [], "action")
            allies_up = [c for c in state.allies_of(actor.id) if _can_act(c)]
            if allies_up:
                for e in adjacent_enemies[:3]:
                    add("help", f"Help an ally attack {e.name} (their next attack has advantage)",
                        {"target": e.id}, [], "action")
        if action_free or haste_free:
            if adjacent_enemies:
                add("disengage", "Disengage: your movement provokes no opportunity attacks", {}, [], "action")
            if not actor.has_condition("hidden") and not adjacent_enemies:
                add("hide", f"Hide (Stealth {actor.skill_bonus('Stealth'):+d} vs passive Perception)", {}, [], "action")
        cunning = actor.has_feature("cunning_action")
        nimble = _trait(actor, "nimble_escape") is not None or {"disengage", "hide"} & set(_bonus_actions(actor))
        if bonus_free and (cunning or nimble):
            src = "Cunning Action" if cunning else "Nimble Escape"
            if cunning and actor.effective_speed() > 0:
                add("cunning_action", f"{src}: Dash (+{actor.effective_speed()} ft movement)",
                    {"mode": "dash"}, [], "bonus")
            if adjacent_enemies:
                add("cunning_action", f"{src}: Disengage", {"mode": "disengage"}, [], "bonus")
            if not actor.has_condition("hidden") and not adjacent_enemies:
                add("cunning_action", f"{src}: Hide (Stealth {actor.skill_bonus('Stealth'):+d})",
                    {"mode": "hide"}, [], "bonus")
        if bonus_free and "dash_toward_enemy" in _bonus_actions(actor) and actor.effective_speed() > 0:
            add("cunning_action", f"Aggressive: Dash toward an enemy (+{actor.effective_speed()} ft)",
                {"mode": "dash"}, [], "bonus")
    if (action_free or haste_free) and actor.effective_speed() > 0:
        add("dash", f"Dash (+{actor.effective_speed()} ft movement this turn)", {}, [], "action")

    # ---- movement
    _move_template(state, actor, movement_left, add, flee=turned)

    add("end_turn", "End turn", {}, [], "free")
    return out


def _attack_templates(state: GameState, actor: Combatant, spec: dict, add) -> None:
    targets = [t for t in _enemy_targets(state, actor) if _spec_in_range(state, actor, t, spec)]
    if spec["ranged"]:
        targets = targets[:MAX_RANGED_TARGETS]
    for t in targets:
        add("attack",
            f"Attack {t.name} with {spec['name']} ({spec['bonus']:+d}, {spec['damage']} {spec['damage_type']})",
            {"target": t.id, "weapon": spec["name"]}, [], "action")


def _offhand_templates(state: GameState, actor: Combatant, add) -> None:
    used = actor.flags.get("light_weapon_attacked")
    weapons = list(actor.sheet.weapons) if actor.sheet else []
    names = []
    for w in weapons:
        try:
            names.append(srd.weapon(w)["name"])
        except srd.SRDLookupError:
            pass
    for spec in _pc_weapon_specs(actor):
        if spec["ranged"] or not spec.get("light"):
            continue
        if spec["name"] == used and names.count(used) < 2:
            continue
        mod = spec["mod"]
        dmg = spec["dice"] if mod >= 0 else _with_mod(spec["dice"], mod)
        for t in _enemy_targets(state, actor):
            if _dist(actor, t) <= spec["reach"]:
                add("attack",
                    f"Off-hand attack {t.name} with {spec['name']} ({spec['bonus']:+d}, {dmg} {spec['damage_type']})",
                    {"target": t.id, "weapon": spec["name"], "offhand": True}, [], "bonus")


def _move_template(state: GameState, actor: Combatant, movement_left: int, add, flee: bool = False) -> None:
    if movement_left <= 0 or actor.effective_speed() <= 0:
        return
    # Movement may be split around an action in 5e, but each split is another
    # model call: allow one move per turn, plus one more per Dash taken.
    if int(actor.flags.get("moves_taken", 0)) > int(actor.flags.get("dashes_taken", 0)):
        return
    budget = movement_left
    stand = 0
    if actor.has_condition("prone"):
        stand = max(5, actor.speed // 2)
        if budget < stand:
            return
        budget -= stand
    if budget <= 0:
        return
    reach = _reachable(state, actor, budget)
    if not reach:
        return
    suggested = _suggest_destinations(state, actor, reach, flee=flee)
    if not suggested:
        return
    label = f"Move up to {budget} ft" + (" (stand up first)" if stand else "") + " — pick a path or suggested square"
    add("move", label, {"suggested": [list(p) for p in suggested], "max_ft": budget}, ["path"], "movement")


def _reachable(state: GameState, actor: Combatant, budget: int) -> dict[tuple[int, int], int]:
    """Uniform-cost flood from the actor's square: square -> movement cost."""
    grid = state.grid
    start = _pos(actor)
    blocked = set(grid.occupied(state, ignore=actor.id))
    dist: dict[tuple[int, int], int] = {start: 0}
    frontier = [(0, start)]
    while frontier:
        frontier.sort()
        cost, node = frontier.pop(0)
        if cost > dist.get(node, 1 << 30):
            continue
        for nxt in sorted(grid.neighbors(node)):
            if nxt in blocked:
                continue
            nc = cost + grid.cost_of(nxt)
            if nc > budget:
                continue
            if nc < dist.get(nxt, 1 << 30):
                dist[nxt] = nc
                frontier.append((nc, nxt))
    dist.pop(start, None)
    return dist


def _suggest_destinations(state: GameState, actor: Combatant, reach: dict[tuple[int, int], int],
                          flee: bool = False) -> list[tuple[int, int]]:
    enemies = [c for c in _enemy_targets(state, actor) if _alive(c)]
    picks: list[tuple[int, int]] = []
    if flee or not enemies:
        src = None
        turned = actor.get_condition("turned")
        if turned and turned.source in state.combatants:
            src = state.combatants[turned.source]
        refs = [src] if src else enemies
        scored = sorted(reach, key=lambda p: (-min((Grid.distance_ft(p, _pos(e)) for e in refs), default=0), p))
        return scored[:MAX_SUGGESTED]
    my_reach = actor.reach_ft()
    for e in enemies[:4]:
        best = min(reach, key=lambda p: (max(Grid.distance_ft(p, _pos(e)), my_reach), reach[p], p))
        if Grid.distance_ft(best, _pos(e)) < _dist(actor, e) and best not in picks:
            picks.append(best)
        if len(picks) >= 4:
            break
    far = sorted(reach, key=lambda p: (-min(Grid.distance_ft(p, _pos(e)) for e in enemies), p))
    for p in far[:2]:
        if len(picks) >= MAX_SUGGESTED:
            break
        if p not in picks:
            picks.append(p)
    return picks[:MAX_SUGGESTED]


# ---------------------------------------------------------------- spells (reading the record)
def _eff_range(eff: dict) -> int:
    return int(eff.get("range") or 0)


def _self_origin(row: dict) -> bool:
    """Cones, lines and Thunderwave's cube start at the caster ("Self (15-foot cone)")."""
    shape = ((row["effect"].get("area") or {}).get("shape")) or None
    return shape in ("cone", "line") or str(row.get("range", "")).lower().startswith("self")


def _target_side(row: dict) -> str:
    eff = row["effect"]
    if eff.get("self_only"):
        return "self"
    if eff.get("ally_only"):
        return "ally"
    if eff["kind"] in ("attack", "save", "debuff"):
        return "enemy"
    if eff["kind"] == "buff":
        return "ally"
    return "any"


def _is_aura(row: dict) -> bool:
    eff = row["effect"]
    area = eff.get("area") or {}
    return bool(eff.get("persistent_aura")) and area.get("shape") == "sphere" and _eff_range(eff) <= int(area.get("size") or 0)


def _combat_spell(row: dict) -> bool:
    """Is there anything the resolver can do with this record inside a fight?"""
    eff = row["effect"]
    if row.get("casting_time") == "reaction":
        return False
    if eff["kind"] != "utility":
        return True
    return any(eff.get(k) for k in ("stabilize", "removes_conditions", "teleport_ft", "dispel_level"))


def _parse_upcast(eff: dict, spell_level: int, slot: int) -> tuple[str, int, int]:
    """(extra dice expr like '+2d6' or '', extra targets/rays, extra flat)."""
    u = eff.get("upcast")
    if not u or slot <= spell_level or spell_level == 0:
        return "", 0, 0
    m = re.match(r"\+(\d+)(d\d+)?\s*([a-z ]*?)\s*/(\d*)slots?", str(u).strip().lower())
    if not m:
        return "", 0, 0
    n, dice, what, per = int(m.group(1)), m.group(2), (m.group(3) or "").strip(), int(m.group(4) or 1)
    steps = (slot - spell_level) // max(1, per)
    if steps <= 0:
        return "", 0, 0
    if dice:
        return f"+{n * steps}{dice}", 0, 0
    if any(w in what for w in ("target", "dart", "ray", "missile")):
        return "", n * steps, 0
    return "", 0, n * steps


def _spell_damage(eff: dict, caster: Combatant, slot: int, spell_level: int) -> str | None:
    dmg = eff.get("damage")
    if not dmg:
        return None
    if spell_level == 0 and eff.get("cantrip_scaling"):
        lvl = _caster_level(caster)
        dmg = _scale_dice(dmg, 1 + sum(1 for step in CANTRIP_STEPS if lvl >= step))
    extra, _, _ = _parse_upcast(eff, spell_level, slot)
    return dmg + extra


def _spell_targets_count(eff: dict, slot: int, spell_level: int) -> int:
    n = int(eff.get("targets", 1) or 0)
    if eff.get("rays"):
        n = int(eff["rays"])
    _, extra_targets, _ = _parse_upcast(eff, spell_level, slot)
    return n + extra_targets


def _type_ok(t: Combatant, eff: dict) -> bool:
    ctype = t.creature_type.lower()
    only = [x.lower() for x in eff.get("only_types", [])]
    if only and not any(x in ctype for x in only):
        return False
    immune = [x.lower() for x in eff.get("immune_types", [])]
    if immune and any(x in ctype for x in immune):
        return False
    return True


def _spell_target_ok(state: GameState, caster: Combatant, t: Combatant, row: dict) -> bool:
    eff = row["effect"]
    side = _target_side(row)
    if side == "self" and t.id != caster.id:
        return False
    if side == "enemy":
        if not _hostile(caster, t) or not _alive(t) or not _type_ok(t, eff):
            return False
    if side == "ally" and (t.side != caster.side or t.dead):
        return False
    kind = eff["kind"]
    if kind == "heal":
        if t.hp >= t.max_hp or t.is_undead() or "construct" in t.creature_type.lower():
            return False
    if kind == "buff":
        if _has_buff(t, _slug(row["name"])) or (
                eff.get("conditions_applied") and all(t.has_condition(c) for c in eff["conditions_applied"])):
            return False
        if eff.get("set_base_ac") and (t.sheet is None or t.sheet.armor):
            return False  # Mage Armor: "a willing creature who isn't wearing armor"
        if eff.get("temp_hp") and t.temp_hp > 0:
            return False
    if kind == "utility":
        if eff.get("stabilize"):
            return (not t.dead) and t.hp <= 0 and not t.stable
        if eff.get("removes_conditions"):
            return any(t.has_condition(c) for c in eff["removes_conditions"])
        if eff.get("dispel_level"):
            return bool(t.flags.get("buffs")) or t.concentration is not None or any(
                c.source and ":" in str(c.source) for c in t.conditions)
    rng = _eff_range(eff)
    d = _dist(caster, t)
    if rng == 0 or side == "self":
        return t.id == caster.id
    if d > rng:
        return False
    if t.id != caster.id and not state.grid.has_line_of_sight(_pos(caster), _pos(t)):
        return False
    return True


def _spell_desc(row: dict, caster: Combatant, slot: int) -> str:
    eff = row["effect"]
    lvl = int(row["level"])
    kind = eff["kind"]
    dmg = _spell_damage(eff, caster, slot, lvl)
    if kind == "attack":
        n = _spell_targets_count(eff, slot, lvl)
        if eff.get("auto_hit"):
            return f"{n}x {dmg} {eff.get('damage_type')} auto-hit"
        d = dmg + (f"+{caster.spellcasting_mod()}" if eff.get("add_mod") else "")
        return f"{caster.spell_attack_bonus:+d}, " + (f"{n}x " if n > 1 else "") + f"{d} {eff.get('damage_type')}"
    if kind in ("save", "debuff"):
        bits = []
        if eff.get("save"):
            bits.append(f"{eff['save']} DC {caster.spell_dc}")
        if dmg:
            bits.append(f"{dmg} {eff.get('damage_type')}" + (" half" if eff.get("half_on_save") else ""))
        if eff.get("conditions_applied"):
            bits.append("/".join(eff["conditions_applied"]))
        return ", ".join(bits)
    if kind == "heal":
        mod = caster.spellcasting_mod() if eff.get("add_mod") else 0
        return f"heals {dmg}{mod:+d}" if mod else f"heals {dmg}"
    if kind == "buff":
        bits = []
        if eff.get("ac_bonus"):
            bits.append(f"+{eff['ac_bonus']} AC")
        if eff.get("set_base_ac"):
            bits.append(f"AC {eff['set_base_ac']}+DEX")
        if eff.get("attack_bonus_die"):
            bits.append(f"+{eff['attack_bonus_die']} attacks")
        if eff.get("save_bonus_die"):
            bits.append(f"+{eff['save_bonus_die']} saves")
        if eff.get("check_bonus_die"):
            bits.append(f"+{eff['check_bonus_die']} to a check")
        if eff.get("temp_hp"):
            bits.append(f"{eff['temp_hp']} temp HP")
        if eff.get("max_hp_bonus"):
            bits.append(f"+{eff['max_hp_bonus']} max HP")
        if eff.get("attackers_disadvantage"):
            bits.append("attackers have disadvantage")
        if eff.get("speed_multiplier"):
            bits.append(f"speed x{eff['speed_multiplier']}")
        if eff.get("extra_action"):
            bits.append("extra action")
        if eff.get("fly_speed"):
            bits.append(f"fly {eff['fly_speed']} ft")
        if eff.get("max_healing"):
            bits.append("max healing")
        if eff.get("conditions_applied"):
            bits.append("/".join(eff["conditions_applied"]))
        return ", ".join(bits) or "buff"
    if eff.get("stabilize"):
        return "stabilize"
    if eff.get("removes_conditions"):
        return "cure " + "/".join(eff["removes_conditions"])
    if eff.get("teleport_ft"):
        return f"teleport {eff['teleport_ft']} ft"
    if eff.get("dispel_level"):
        return "dispel magic"
    return "utility"


def _spell_templates(state: GameState, actor: Combatant, add, action_free: bool, bonus_free: bool) -> None:
    known = _known_spells(actor)
    if not known:
        return
    slots = _slots(actor)
    for name in known:
        try:
            row = srd.spell(name)
        except srd.SRDLookupError:
            continue
        eff = row.get("effect") or {}
        if not _combat_spell(row):
            continue
        ct = row.get("casting_time", "action")
        lvl = int(row["level"])
        if ct == "bonus":
            if not bonus_free or actor.flags.get("cast_bonus_spell"):
                continue
            if actor.flags.get("cast_action_spell") and lvl > 0:
                continue
        else:
            if not action_free or int(actor.turn.get("attacks_left", 0)) > 0:
                continue
            if actor.flags.get("cast_bonus_spell") and lvl > 0:
                continue
        if lvl == 0:
            slot_levels = [0]
        else:
            avail = sorted(s for s, n in slots.items() if s >= lvl and n > 0)
            if not avail:
                continue
            slot_levels = [avail[0]]
            # Upcast variants only where they are a single extra template
            # (area / multi-target spells); per-target spells stay at base slot.
            if eff.get("upcast") and avail[-1] != avail[0] and (
                    (eff.get("area") or {}).get("shape") or _spell_targets_count(eff, lvl, lvl) > 1):
                slot_levels.append(avail[-1])
        for slot in slot_levels:
            _spell_templates_for_slot(state, actor, row, slot, add, ct)


def _spell_templates_for_slot(state: GameState, actor: Combatant, row: dict, slot: int, add, ct: str) -> None:
    eff = row["effect"]
    name = row["name"]
    cost = "bonus" if ct == "bonus" else "action"
    tag = f" (L{slot})" if slot else ""
    desc = _spell_desc(row, actor, slot)
    lvl = int(row["level"])
    area = eff.get("area") or {}
    base = {"spell": name, "slot": slot}

    if _is_aura(row):
        if not (actor.concentration and actor.concentration.get("spell") == name):
            add("cast", f"Cast {name}{tag}: {area['size']} ft aura, {desc}", dict(base), [], cost)
        return
    if eff.get("teleport_ft"):
        pts = _teleport_points(state, actor, int(eff["teleport_ft"]))
        if pts:
            add("cast", f"Cast {name}{tag}: {desc}; choose point",
                {**base, "suggested": [list(p) for p in pts]}, ["point"], cost)
        return
    if area.get("shape"):
        pts = _area_points(state, actor, row, slot)
        if pts:
            add("cast", f"Cast {name}{tag}: {area['shape']} {area['size']} ft, {desc}; choose point",
                {**base, "suggested": [list(p) for p in pts]}, ["point"], cost)
        return
    n_targets = _spell_targets_count(eff, slot, lvl)
    cands = [t for t in state.combatants.values() if _spell_target_ok(state, actor, t, row)]
    cands.sort(key=lambda t: (_dist(actor, t), t.id))
    if not cands:
        return
    side = _target_side(row)
    if n_targets > 1:
        who = "enemies" if side == "enemy" else "allies"
        add("cast", f"Cast {name}{tag} on up to {n_targets} {who}: {desc}",
            {**base, "suggested": [t.id for t in cands[:n_targets]], "max_targets": n_targets},
            ["targets"], cost)
        return
    if side == "self" or _eff_range(eff) == 0:
        add("cast", f"Cast {name}{tag}: {desc}", {**base, "target": actor.id}, [], cost)
        return
    for t in cands[:MAX_SPELL_TARGETS]:
        who = "yourself" if t.id == actor.id else t.name
        add("cast", f"Cast {name}{tag} on {who} ({desc})", {**base, "target": t.id}, [], cost)


def _teleport_points(state: GameState, actor: Combatant, ft: int) -> list[tuple[int, int]]:
    grid = state.grid
    occupied = set(grid.occupied(state, ignore=actor.id))
    me = _pos(actor)
    enemies = _enemy_targets(state, actor)
    pts = []
    r = ft // 5
    for x in range(me[0] - r, me[0] + r + 1):
        for y in range(me[1] - r, me[1] + r + 1):
            p = (x, y)
            if p == me or not grid.passable(p) or p in occupied or Grid.distance_ft(me, p) > ft:
                continue
            pts.append(p)
    if enemies:
        pts.sort(key=lambda p: (-min(Grid.distance_ft(p, _pos(e)) for e in enemies), p))
    else:
        pts.sort()
    return pts[:MAX_SUGGESTED]


def _sphere_ram_points(state: GameState, actor: Combatant, sphere: tuple[int, int]) -> list[tuple[int, int]]:
    pts = []
    for e in _enemy_targets(state, actor):
        p = _pos(e)
        if Grid.distance_ft(sphere, p) <= 30 and state.grid.in_bounds(p):
            pts.append(p)
    return pts[:MAX_SUGGESTED]


def _area_squares(state: GameState, caster: Combatant, row: dict, point: tuple[int, int]) -> set[tuple[int, int]]:
    area = row["effect"]["area"]
    return state.grid.area_squares(area["shape"], int(area["size"]), _pos(caster), tuple(point))


def _creatures_in_area(state: GameState, caster: Combatant, row: dict, point: tuple[int, int]) -> list[Combatant]:
    squares = _area_squares(state, caster, row, point)
    self_origin = _self_origin(row)
    out = [c for c in state.combatants.values()
           if not c.dead and _pos(c) in squares and not (self_origin and c.id == caster.id)]
    out.sort(key=lambda c: c.id)
    return out


def _area_points(state: GameState, actor: Combatant, row: dict, slot: int) -> list[tuple[int, int]]:
    eff = row["effect"]
    area = eff["area"]
    rng = _eff_range(eff)
    grid = state.grid
    me = _pos(actor)
    enemies = _enemy_targets(state, actor)
    if not enemies:
        return []
    candidates: list[tuple[int, int]] = []
    if _self_origin(row) and area["shape"] == "cube":
        candidates = sorted(grid.neighbors(me))
    elif _self_origin(row):
        candidates = [_pos(e) for e in enemies if grid.has_line_of_sight(me, _pos(e))]
    else:
        for e in enemies:
            p = _pos(e)
            if Grid.distance_ft(me, p) <= rng and grid.has_line_of_sight(me, p):
                candidates.append(p)
        for i, a in enumerate(enemies[:4]):
            for b in enemies[i + 1:4]:
                mid = ((_pos(a)[0] + _pos(b)[0]) // 2, (_pos(a)[1] + _pos(b)[1]) // 2)
                if Grid.distance_ft(me, mid) <= rng and grid.in_bounds(mid) and grid.has_line_of_sight(me, mid):
                    candidates.append(mid)
    seen: set[tuple[int, int]] = set()
    scored = []
    protect = 1 + slot if actor.has_feature("sculpt_spells") and row.get("school") == "evocation" else 0
    only_enemies = bool(eff.get("enemies_only"))
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        hit = _creatures_in_area(state, actor, row, p)
        n_enemy = sum(1 for c in hit if _hostile(actor, c) and _alive(c))
        n_ally = 0 if only_enemies else max(0, sum(1 for c in hit if c.side == actor.side) - protect)
        if n_enemy == 0:
            continue
        scored.append((-(n_enemy - 2 * n_ally), p))
    scored.sort()
    return [p for _, p in scored[:MAX_SUGGESTED]]


# ---------------------------------------------------------------- apply
def apply(state: GameState, action: Action) -> tuple[GameState, list[Event]]:
    actor_id = action.actor
    if actor_id not in state.combatants:
        raise IllegalAction(f"no such combatant {actor_id!r}")
    if state.mode != "combat":
        raise IllegalAction("not in combat")
    if state.active_id() != actor_id:
        raise IllegalAction(f"it is not {actor_id}'s turn")
    templates = {t.id: t for t in legal_actions(state, actor_id)}
    tpl = templates.get(action.template_id)
    if tpl is None:
        raise IllegalAction(f"{action.template_id!r} is not a legal action for {actor_id} right now")
    params = {**tpl.params, **(action.params or {})}

    new = state.copy()
    actor = new.combatants[actor_id]
    rng = _rng_of(new)
    events: list[Event] = []

    kind = tpl.type
    if kind == "end_turn":
        _ev(new, events, "turn_end", f"{actor.name} ends the turn.", actor.id)
        actor.turn["action"] = True
        actor.turn["bonus"] = True
        actor.turn["attacks_left"] = 0
        actor.turn["movement_left"] = 0
        actor.turn["haste_action"] = False
        actor.flags["turn_ended"] = True
    elif kind == "attack":
        _do_attack(new, events, rng, actor, tpl, params)
    elif kind == "cast":
        if params.get("ram"):
            _ram_flaming_sphere(new, events, rng, actor, params)
        else:
            _do_cast(new, events, rng, actor, tpl, params)
    elif kind == "move":
        _do_move(new, events, rng, actor, tpl, params)
    elif kind == "dash":
        _spend_action(actor)
        actor.flags["dashes_taken"] = int(actor.flags.get("dashes_taken", 0)) + 1
        actor.turn["movement_left"] = int(actor.turn.get("movement_left", 0)) + actor.effective_speed()
        _ev(new, events, "system", f"{actor.name} dashes (+{actor.effective_speed()} ft).", actor.id,
            movement_left=actor.turn["movement_left"])
    elif kind == "dodge":
        actor.turn["action"] = True
        actor.flags["dodging"] = True
        _ev(new, events, "system", f"{actor.name} takes the Dodge action.", actor.id, dodge=True)
    elif kind == "disengage":
        _spend_action(actor)
        actor.flags["disengaged"] = True
        _ev(new, events, "system", f"{actor.name} disengages.", actor.id, disengage=True)
    elif kind == "help":
        actor.turn["action"] = True
        target = new.combatants[params["target"]]
        target.flags["helped_against"] = {"helper": actor.id, "side": actor.side}
        _ev(new, events, "system", f"{actor.name} helps: the next ally to attack {target.name} has advantage.",
            actor.id, target=target.id)
    elif kind == "hide":
        _spend_action(actor)
        _do_hide(new, events, rng, actor)
    elif kind == "cunning_action":
        actor.turn["bonus"] = True
        mode = params.get("mode")
        if mode == "dash":
            actor.flags["dashes_taken"] = int(actor.flags.get("dashes_taken", 0)) + 1
            actor.turn["movement_left"] = int(actor.turn.get("movement_left", 0)) + actor.effective_speed()
            _ev(new, events, "system", f"{actor.name} dashes as a bonus action (+{actor.effective_speed()} ft).",
                actor.id, movement_left=actor.turn["movement_left"])
        elif mode == "disengage":
            actor.flags["disengaged"] = True
            _ev(new, events, "system", f"{actor.name} disengages as a bonus action.", actor.id, disengage=True)
        elif mode == "hide":
            _do_hide(new, events, rng, actor)
        else:
            raise IllegalAction(f"unknown cunning action mode {mode!r}")
    elif kind == "second_wind":
        actor.turn["bonus"] = True
        actor.resources["second_wind"] = int(actor.resources.get("second_wind", 0)) - 1
        expr = f"1d10+{actor.sheet.level}"
        roll = rng.roll(expr)
        _heal(new, events, actor, roll.total, "Second Wind", roll=roll, expr=expr)
    elif kind == "action_surge":
        actor.resources["action_surge"] = int(actor.resources.get("action_surge", 0)) - 1
        actor.turn["action"] = False
        actor.turn["attacks_left"] = 0
        _ev(new, events, "system", f"{actor.name} surges: one more action this turn!", actor.id, action_surge=True)
    elif kind == "channel_divinity":
        actor.turn["action"] = True
        actor.resources["channel_divinity"] = int(actor.resources.get("channel_divinity", 0)) - 1
        _turn_undead(new, events, rng, actor)
    elif kind == "use_item":
        actor.turn["action"] = True
        item = params.get("item")
        target = new.combatants[params.get("target", actor.id)]
        use = dict(srd.equipment(item).get("use") or {})
        if use.get("kind") != "heal":
            raise IllegalAction(f"cannot use {item!r}")
        held = next((x for x in actor.inventory if x.lower() == item.lower()), None)
        if held is None:
            raise IllegalAction(f"{actor.name} has no {item}")
        actor.inventory.remove(held)
        roll = rng.roll(use["amount"])
        _ev(new, events, "system",
            f"{actor.name} " + ("drinks" if target.id == actor.id else f"gives {target.name}") + f" a {item}.",
            actor.id, item=item, target=target.id)
        _heal(new, events, target, roll.total, item, roll=roll, source_id=actor.id, expr=use["amount"])
    else:
        raise IllegalAction(f"unsupported action type {kind!r}")

    new.rng = rng.state()
    return new, events


def _spend_action(actor: Combatant) -> None:
    """Use the main action, or the Haste action if the main one is gone."""
    if actor.turn.get("action") and actor.turn.get("haste_action"):
        actor.turn["haste_action"] = False
    else:
        actor.turn["action"] = True


# ---------------------------------------------------------------- attacks
def _do_attack(new: GameState, events: list[Event], rng: RNG, actor: Combatant, tpl: ActionTemplate, params: dict) -> None:
    target = new.combatants.get(params.get("target"))
    if target is None:
        raise IllegalAction("attack needs a target")
    if params.get("spiritual_weapon"):
        sw = actor.flags["spiritual_weapon"]
        actor.turn["bonus"] = True
        spec = {"name": "Spiritual Weapon", "bonus": int(sw["bonus"]), "damage": sw["damage"],
                "dice": sw["damage"], "mod": 0, "damage_type": "force", "ranged": False,
                "reach": 60, "range": None, "properties": [], "on_hit": None, "is_spell": True,
                "no_range_penalty": True}
        _resolve_attack(new, events, rng, actor, target, spec)
        return
    wname = params.get("weapon")
    spec = next((s for s in _attack_specs(actor) if s["name"] == wname), None)
    if spec is None:
        raise IllegalAction(f"{actor.name} has no attack {wname!r}")
    if params.get("offhand"):
        actor.turn["bonus"] = True
        spec = dict(spec)
        spec["damage"] = spec["dice"] if spec["mod"] >= 0 else _with_mod(spec["dice"], spec["mod"])
        spec["offhand"] = True
    else:
        seq = _multiattack(actor)
        if int(actor.turn.get("attacks_left", 0)) > 0:
            actor.turn["attacks_left"] -= 1
            if seq:
                idx = int(actor.flags.get("multi_index", 0))
                if idx < len(seq) and seq[idx].get("disadvantage"):
                    spec = {**spec, "disadvantage": True}
                actor.flags["multi_index"] = idx + 1
        elif actor.turn.get("action") and actor.turn.get("haste_action"):
            actor.turn["haste_action"] = False  # Haste: one extra weapon attack
        else:
            actor.turn["action"] = True
            if seq and spec.get("weapon", spec["name"]) == seq[0]["name"]:
                actor.turn["attacks_left"] = len(seq) - 1
                actor.flags["multi_index"] = 1
            else:
                actor.turn["attacks_left"] = _attacks_per_action(actor) - 1
        if spec.get("light") and not spec["ranged"]:
            actor.flags["light_weapon_attacked"] = spec["name"]
        if spec.get("recharge") is not None:
            actor.resources.setdefault("recharge", {})[spec.get("weapon", spec["name"])] = False
    _resolve_attack(new, events, rng, actor, target, spec)


def _attack_mode(new: GameState, attacker: Combatant, target: Combatant, spec: dict) -> tuple[str, list[str]]:
    adv: list[str] = []
    dis: list[str] = []
    d = _dist(attacker, target)
    for cname in _ATTACKER_DISADVANTAGE:
        if attacker.has_condition(cname):
            dis.append(f"attacker {cname}")
    for cname in _ATTACKER_ADVANTAGE:
        if attacker.has_condition(cname):
            adv.append(f"attacker {cname}")
    if attacker.exhaustion_level() >= 3:
        dis.append("exhaustion")
    if attacker.has_condition("hidden"):
        adv.append("unseen attacker")
    if target.has_condition("prone"):  # conditions.json: attack_by "special"
        (adv if d <= 5 else dis).append("target prone")
    for cname in _TARGET_ADVANTAGE:
        if target.has_condition(cname):
            adv.append(f"target {cname}")
    for cname in _TARGET_DISADVANTAGE:
        if target.has_condition(cname):
            dis.append(f"target {cname}")
    if target.has_condition("hidden"):
        dis.append("unseen target")
    if _buff_any(target, "attackers_disadvantage"):
        dis.append("blur")
    if target.flags.get("dodging") and _can_act(target) and target.effective_speed() > 0:
        dis.append("dodging")
    if spec.get("ranged") and not spec.get("no_range_penalty"):
        if any(_hostile(attacker, e) and _can_act(e) and _dist(attacker, e) <= 5
               for e in new.combatants.values()):
            dis.append("enemy adjacent")
        rng = spec.get("range")
        if rng and d > rng[0]:
            dis.append("long range")
    helped = target.flags.get("helped_against")
    if helped and helped.get("side") == attacker.side:
        adv.append("help")
    if target.flags.get("advantage_against"):
        adv.append("guiding bolt")
    if _trait(attacker, "pack_tactics") and any(
            c.id != attacker.id and c.side == attacker.side and _can_act(c) and _dist(c, target) <= 5
            for c in new.combatants.values()):
        adv.append("pack tactics")
    if spec.get("disadvantage"):
        dis.append("second attack")
    if adv and dis:
        return "normal", adv + dis
    if adv:
        return "advantage", adv
    if dis:
        return "disadvantage", dis
    return "normal", []


def _shield_slot(target: Combatant, total: int, ac: int) -> int | None:
    """Slot Shield would be cast from if it turns this hit into a miss, else None."""
    if "Shield" not in _known_spells(target) or not _can_react(target):
        return None
    bonus = int(srd.spell("Shield")["effect"].get("ac_bonus", 5))
    if total >= ac + bonus:
        return None
    avail = sorted(s for s, n in _slots(target).items() if s >= 1 and n > 0)
    return avail[0] if avail else None


def _cast_shield_reaction(new: GameState, events: list[Event], target: Combatant, slot: int, ac: int) -> None:
    row = srd.spell("Shield")
    bonus = int(row["effect"].get("ac_bonus", 5))
    target.resources["spell_slots"][slot] = _slots(target)[slot] - 1
    target.turn["reaction"] = True
    _add_buff(target, {"name": "shield", "ac": bonus, "rounds": int(row["effect"].get("duration_rounds") or 1),
                       "tick": "start", "source": f"{target.id}:Shield"})
    _ev(new, events, "spell_cast",
        f"{target.name} casts Shield as a reaction (L{slot}): AC {ac} → {ac + bonus} until its next turn; the attack misses.",
        target.id, spell="Shield", slot=slot, target=target.id, reaction=True, ac_before=ac, ac_after=ac + bonus)


def _lucky_reroll(rng: RNG, attacker: Combatant, roll: RollResult, mode: str, bonus: int) -> tuple[RollResult, str]:
    """Halfling Lucky: reroll a natural 1 on attack rolls once."""
    if roll.natural == 1 and attacker.sheet and "lucky" in attacker.sheet.features:
        again = rng.roll_d20(bonus, "normal")
        return again, " (lucky reroll)"
    return roll, ""


def _resolve_attack(new: GameState, events: list[Event], rng: RNG, attacker: Combatant,
                    target: Combatant, spec: dict, *, opportunity: bool = False,
                    provoke: tuple[tuple[int, int], tuple[int, int]] | None = None) -> dict:
    """Roll one attack and apply everything that follows from it.

    `provoke` is the step that triggered an opportunity attack. It is named in
    the event because the move event that follows reports only where the mover
    started and where it was stopped — never the square it was leaving when the
    reaction fired — and a reader who assumes those are the same thing reads a
    legal opportunity attack as an illegal one.
    """
    mode, reasons = _attack_mode(new, attacker, target, spec)
    if provoke is not None:
        (ax, ay), (bx, by) = provoke
        reasons = list(reasons) + [f"leaving reach ({ax},{ay})→({bx},{by})"]
    cover = 0 if spec.get("ignore_cover") else _cover_bonus(new.grid.cover_between(_pos(attacker), _pos(target)))
    ac = target.effective_ac() + cover
    roll = rng.roll_d20(int(spec["bonus"]), mode)
    roll, lucky_txt = _lucky_reroll(rng, attacker, roll, mode, int(spec["bonus"]))
    total = roll.total
    die_total, extra_txt = _bonus_die_total(rng, attacker, "attack_die")
    total += die_total
    natural = roll.natural or 0
    threshold = 19 if (attacker.has_feature("improved_critical") and not spec.get("is_spell")) else 20
    crit = False
    if natural == 1:
        hit = False
    elif natural >= threshold:
        hit, crit = True, True
    else:
        hit = total >= ac
    shield_slot = _shield_slot(target, total, ac) if (hit and not crit) else None
    if shield_slot is not None:
        hit = False
    d = _dist(attacker, target)
    if hit and d <= 5 and any(target.has_condition(c) for c in _AUTO_CRIT):
        crit = True
    dtype = spec.get("damage_type", "bludgeoning")
    parts: list[str] = []
    total_dmg = 0
    if hit:
        dmg_expr = spec["damage"]
        if dmg_expr and dmg_expr != "0":
            dr = rng.roll_damage(dmg_expr, crit)
            total_dmg += dr.total
            parts.append(_roll_text(dr))
        # Sneak Attack: finesse/ranged weapon, once per turn, advantage or an ally adjacent to the target.
        if (attacker.has_feature("sneak_attack") and not spec.get("is_spell")
                and not attacker.flags.get("sneak_attack_used")
                and (spec.get("finesse") or spec.get("ranged"))):
            ally_adjacent = any(c.id != attacker.id and c.side == attacker.side and _can_act(c) and _dist(c, target) <= 5
                                for c in new.combatants.values())
            if mode == "advantage" or (ally_adjacent and mode != "disadvantage"):
                n = int(attacker.resources.get("sneak_attack_dice", 1))
                sr = rng.roll_damage(f"{n}d6", crit)
                total_dmg += sr.total
                attacker.flags["sneak_attack_used"] = True
                parts.append(f"sneak attack {_roll_text(sr)}")
        ma = _trait(attacker, "martial_advantage")
        if ma and not attacker.flags.get("martial_advantage_used") and any(
                c.id != attacker.id and c.side == attacker.side and _can_act(c) and _dist(c, target) <= 5
                for c in new.combatants.values()):
            mr = rng.roll_damage(ma.get("damage", "2d6"), crit)
            total_dmg += mr.total
            attacker.flags["martial_advantage_used"] = True
            parts.append(f"martial advantage {_roll_text(mr)}")
        total_dmg = max(0, total_dmg)
    verb = "makes an opportunity attack on" if opportunity else "attacks"
    what = spec["name"]
    if natural == 1:
        outcome = "natural 1, miss"
    elif shield_slot is not None:
        outcome = "hit — Shield! miss"
    elif crit:
        outcome = "CRITICAL HIT"
    else:
        outcome = "hit" if hit else "miss"
    text = (f"{attacker.name} {verb} {target.name} with {what}: {_roll_text(roll)}{lucky_txt}{extra_txt} vs AC {ac}"
            + (f" (+{cover} cover)" if cover else "") + f", {outcome}")
    if hit:
        text += f", {'; '.join(parts)}" + (f" = {total_dmg}" if len(parts) > 1 else "") + f" {dtype}"
    if reasons:
        text += f" [{', '.join(reasons)}]"
    _ev(new, events, "attack", text, attacker.id, target=target.id, weapon=what, roll=roll.to_dict(),
        total=total, ac=ac, hit=hit, crit=crit, mode=mode, reasons=reasons, opportunity=opportunity,
        damage=total_dmg if hit else 0, damage_type=dtype)
    if shield_slot is not None:
        _cast_shield_reaction(new, events, target, shield_slot, ac)
    if attacker.has_condition("hidden"):
        attacker.remove_condition("hidden")
        _ev(new, events, "condition_remove", f"{attacker.name} is no longer hidden.", attacker.id, condition="hidden")
    _break_invisibility(new, events, attacker)
    helped = target.flags.get("helped_against")
    if helped and helped.get("side") == attacker.side:
        target.flags.pop("helped_against", None)
    if target.flags.get("advantage_against"):
        target.flags.pop("advantage_against", None)
    result = {"hit": hit, "crit": crit, "damage": 0}
    if not hit:
        return result
    if target.has_feature("uncanny_dodge") and _can_react(target) and not spec.get("is_spell") and total_dmg > 0:
        target.turn["reaction"] = True
        total_dmg //= 2
        _ev(new, events, "system", f"{target.name} halves the blow with Uncanny Dodge ({total_dmg} damage).",
            target.id, uncanny_dodge=True, amount=total_dmg)
    result["damage"] = _deal_damage(new, events, rng, target, total_dmg, dtype, attacker.id, crit=crit)
    on_hit = spec.get("on_hit")
    if on_hit and not target.dead:
        riders = on_hit if isinstance(on_hit, list) else [on_hit]
        for rider in riders:
            _apply_rider(new, events, rng, attacker, target, rider, result["damage"])
    return result


def _apply_rider(new: GameState, events: list[Event], rng: RNG, attacker: Combatant,
                 target: Combatant, rider: dict, dealt: int) -> None:
    """One `extra` entry of a monster action, or a spell's on-hit effect."""
    if rider.get("immune_races") and target.sheet and any(
            r.lower() in target.sheet.race.lower() for r in rider["immune_races"]):
        return
    if rider.get("speed_reduction"):
        _add_buff(target, {"name": "slowed", "speed_penalty": int(rider["speed_reduction"]),
                           "rounds": int(rider.get("effect_duration_rounds") or 1), "tick": "start",
                           "source": attacker.id})
        _ev(new, events, "condition_add", f"{target.name}'s speed is reduced by {rider['speed_reduction']} ft.",
            target.id, condition="slowed", source=attacker.id)
    if rider.get("no_reactions_rounds"):
        target.flags["no_reactions"] = True
        _ev(new, events, "condition_add", f"{target.name} can't take reactions until its next turn.",
            target.id, condition="no_reactions", source=attacker.id)
    if rider.get("no_healing_rounds"):
        _add_buff(target, {"name": "no_healing", "no_healing": True, "rounds": int(rider["no_healing_rounds"]),
                           "tick": "start", "source": attacker.id})
        _ev(new, events, "condition_add", f"{target.name} can't regain hit points until the start of {attacker.name}'s next turn.",
            target.id, condition="no_healing", source=attacker.id)
    if rider.get("grants_advantage_rounds"):
        target.flags["advantage_against"] = {"until": attacker.id, "armed": False}
        _ev(new, events, "condition_add", f"The next attack against {target.name} has advantage.",
            target.id, condition="advantage_against", source=attacker.id)
    if rider.get("drain_half") and dealt > 0:
        _heal(new, events, attacker, dealt // 2, "Vampiric Touch")
    if rider.get("ability_drain"):
        amt = rng.roll(rider.get("amount", "1d4")).total
        ab = rider["ability_drain"]
        target.abilities[ab] = max(1, int(target.abilities.get(ab, 10)) - amt)
        _ev(new, events, "condition_add", f"{target.name}'s {ab} is drained by {amt} (now {target.abilities[ab]}).",
            target.id, condition="ability_drain", ability=ab, amount=amt)
        if ab == "STR" and target.abilities[ab] <= 0:
            _die(new, events, target, "strength drained to 0")
    if rider.get("save"):
        tags = [rider.get("damage_type", ""), rider.get("condition", "")]
        ok, _ = _saving_throw(new, events, rng, target, rider["save"], int(rider["dc"]),
                              source_name=attacker.name, tags=tags)
        if rider.get("damage"):
            dmg = rng.roll(rider["damage"]).total
            if ok and rider.get("half_on_save"):
                dmg //= 2
            elif ok:
                dmg = 0
            if dmg:
                _deal_damage(new, events, rng, target, dmg, rider.get("damage_type", "poison"), attacker.id)
        if not ok and rider.get("condition"):
            repeat = bool(rider.get("repeat_save")) or rider.get("escape_dc") is not None
            cond = Condition(rider["condition"], duration=rider.get("duration"), source=attacker.id,
                             save_dc=int(rider.get("escape_dc") or rider["dc"]) if repeat else None,
                             save_ability=("STR" if rider.get("escape_dc") is not None else rider["save"]) if repeat else None,
                             extra={"repeat_save": repeat})
            _add_condition(new, events, target, cond, attacker.name)
        if not ok and rider.get("max_hp_reduction") and dealt > 0:
            target.max_hp = max(1, target.max_hp - dealt)
            target.hp = min(target.hp, target.max_hp)
            _ev(new, events, "condition_add", f"{target.name}'s hit point maximum is reduced by {dealt}.",
                target.id, condition="max_hp_reduced", amount=dealt)
    elif rider.get("condition"):
        cond = Condition(rider["condition"], duration=rider.get("duration"), source=attacker.id,
                         extra={"repeat_save": False})
        _add_condition(new, events, target, cond, attacker.name)


# ---------------------------------------------------------------- damage / healing
def _deal_damage(new: GameState, events: list[Event], rng: RNG, target: Combatant, amount: int,
                 dtype: str, source_id: str | None, *, crit: bool = False) -> int:
    dtype = (dtype or "bludgeoning").lower()
    note = ""
    if dtype in target.damage_immunities():
        amount, note = 0, " (immune)"
    elif target.has_condition("petrified") or dtype in target.damage_resistances():
        amount, note = amount // 2, " (resisted)"
    elif dtype in target.damage_vulnerabilities():
        amount, note = amount * 2, " (vulnerable)"
    absorbed = 0
    if target.temp_hp > 0 and amount > 0:
        absorbed = min(target.temp_hp, amount)
        target.temp_hp -= absorbed
        amount -= absorbed
    before = target.hp
    was_down = before <= 0
    target.hp = max(0, before - amount)
    text = f"{target.name} takes {amount} {dtype} damage{note}"
    if absorbed:
        text += f" ({absorbed} absorbed by temp HP)"
    text += f" ({before} → {target.hp} HP)"
    _ev(new, events, "damage", text, source_id, target=target.id, amount=amount, damage_type=dtype,
        hp_before=before, hp_after=target.hp, absorbed=absorbed, crit=crit)
    regen = _trait(target, "regeneration")
    if regen and dtype in [x.lower() for x in regen.get("stopped_by", [])]:
        target.flags["no_regen"] = True
    if amount <= 0 and not was_down:
        return amount
    if target.hp <= 0:
        overflow = amount - max(0, before)
        _drop_to_zero(new, events, rng, target, amount, overflow, crit, dtype, source_id, was_down)
        return amount
    if target.has_condition("turned"):
        target.remove_condition("turned")
        _ev(new, events, "condition_remove", f"{target.name} is no longer turned.", target.id, condition="turned")
    if target.concentration:
        dc = max(10, amount // 2)
        ok, _ = _saving_throw(new, events, rng, target, "CON", dc, source_name="concentration")
        if not ok:
            _end_concentration(new, events, target, reason="broken")
    return amount


def _drop_to_zero(new: GameState, events: list[Event], rng: RNG, target: Combatant, amount: int,
                  overflow: int, crit: bool, dtype: str, source_id: str | None, was_down: bool) -> None:
    if was_down:
        if target.dead:
            return
        if amount >= target.max_hp:
            _die(new, events, target, "massive damage while down")
            return
        fails = 2 if crit else 1
        target.death_saves["failure"] = int(target.death_saves.get("failure", 0)) + fails
        target.stable = False
        _ev(new, events, "death_save",
            f"{target.name} suffers {fails} death save failure{'s' if fails > 1 else ''} from the hit "
            f"({target.death_saves['success']} successes / {target.death_saves['failure']} failures)",
            target.id, success=False, failures=target.death_saves["failure"], successes=target.death_saves["success"])
        if target.death_saves["failure"] >= 3:
            _die(new, events, target, "three death save failures")
        return
    if overflow >= target.max_hp:
        _die(new, events, target, "massive damage")
        return
    if target.kind == "monster":
        if _trait(target, "undead_fortitude") and dtype != "radiant" and not crit:
            ok, _ = _saving_throw(new, events, rng, target, "CON", 5 + amount, source_name="Undead Fortitude")
            if ok:
                target.hp = 1
                _ev(new, events, "system", f"{target.name}'s Undead Fortitude keeps it standing at 1 HP.",
                    target.id, undead_fortitude=True)
                return
        _die(new, events, target, "reduced to 0 HP")
        return
    target.hp = 0
    target.stable = False
    target.death_saves = {"success": 0, "failure": 0}
    _end_concentration(new, events, target, reason="fell unconscious")
    for cname in ("hidden", "invisible"):
        target.remove_condition(cname)
    target.flags.pop("dodging", None)
    target.add_condition(Condition("unconscious", source="dying"))
    target.add_condition(Condition("prone", source="dying"))
    _ev(new, events, "down", f"{target.name} drops to 0 HP and falls unconscious!", target.id,
        target=target.id, source=source_id)


def _die(new: GameState, events: list[Event], target: Combatant, why: str) -> None:
    target.hp = 0
    target.dead = True
    target.stable = False
    _end_concentration(new, events, target, reason="died")
    target.conditions = []
    target.flags = {}
    _ev(new, events, "dead", f"{target.name} dies ({why}).", target.id, target=target.id, reason=why)


def _heal(new: GameState, events: list[Event], target: Combatant, amount: int, source_name: str,
          roll: RollResult | None = None, source_id: str | None = None, expr: str | None = None,
          mod: int = 0) -> int:
    if target.dead:
        _ev(new, events, "system", f"{source_name} cannot help {target.name}; they are dead.", target.id)
        return 0
    if _buff_any(target, "no_healing"):
        _ev(new, events, "system", f"{target.name} cannot regain hit points right now (Chill Touch).", target.id,
            target=target.id, source=source_name)
        return 0
    if expr and _buff_any(target, "max_healing"):
        amount = _max_of(expr) + mod  # Beacon of Hope
    amount = max(0, int(amount))
    before = target.hp
    target.hp = min(target.max_hp, before + amount)
    healed = target.hp - before
    txt = f"{target.name} regains {healed} HP from {source_name}"
    if roll is not None:
        # The modifier belongs in the text: "1d4 -> 1" beside "regains 7 HP"
        # reads as a bug in the engine rather than as Wisdom plus Disciple of Life.
        txt += f" ({_roll_text(roll)}{f' + {mod}' if mod else ''})"
    txt += f" ({before} → {target.hp} HP)"
    _ev(new, events, "heal", txt, source_id or target.id, target=target.id, amount=healed,
        hp_before=before, hp_after=target.hp, source=source_name)
    if before <= 0 < target.hp:
        target.death_saves = {"success": 0, "failure": 0}
        target.stable = False
        target.remove_condition("unconscious")
        _ev(new, events, "condition_remove", f"{target.name} regains consciousness.", target.id,
            condition="unconscious")
    return healed


def _add_condition(new: GameState, events: list[Event], target: Combatant, cond: Condition,
                   source_name: str | None = None) -> bool:
    if cond.name in target.condition_immunities():
        _ev(new, events, "system", f"{target.name} is immune to being {cond.name}.", target.id, condition=cond.name)
        return False
    fresh = target.add_condition(cond)
    if cond.name == "unconscious" and fresh:
        target.add_condition(Condition("prone", source=cond.source))
    if cond.name == "exhaustion":
        lvl = target.exhaustion_level()
        _ev(new, events, "condition_add", f"{target.name} gains exhaustion (level {lvl}).", target.id,
            condition="exhaustion", level=lvl)
        if lvl >= 6:
            _die(new, events, target, "exhaustion")
        return True
    if fresh:
        dur = f" for {cond.duration} rounds" if cond.duration else ""
        src = f" ({source_name})" if source_name else ""
        _ev(new, events, "condition_add", f"{target.name} is {cond.name}{dur}{src}.", target.id,
            condition=cond.name, duration=cond.duration, source=cond.source)
        if (_COND.get(cond.name) or {}).get("incapacitated"):
            _end_concentration(new, events, target, reason=cond.name)
    return fresh


def _remove_condition(new: GameState, events: list[Event], target: Combatant, name: str, why: str = "") -> bool:
    if target.remove_condition(name):
        if name == "unconscious":
            target.remove_condition("prone")
        _ev(new, events, "condition_remove", f"{target.name} is no longer {name}{(' — ' + why) if why else ''}.",
            target.id, condition=name)
        return True
    return False


def _break_invisibility(new: GameState, events: list[Event], c: Combatant) -> None:
    if c.has_condition("invisible"):
        cond = c.get_condition("invisible")
        c.remove_condition("invisible")
        _ev(new, events, "condition_remove", f"{c.name} becomes visible.", c.id, condition="invisible")
        if cond and cond.source:
            for caster in new.combatants.values():
                conc = caster.concentration
                if conc and f"{caster.id}:{conc.get('spell')}" == cond.source:
                    try:
                        if srd.spell(conc["spell"])["effect"].get("ends_on_attack"):
                            _end_concentration(new, events, caster, reason="target attacked")
                    except srd.SRDLookupError:
                        pass


# ---------------------------------------------------------------- saves
def _saving_throw(new: GameState, events: list[Event], rng: RNG, target: Combatant, ability: str, dc: int,
                  *, source_name: str = "", tags: Iterable[str] | None = None, cover: int = 0,
                  is_spell: bool = False) -> tuple[bool, RollResult | None]:
    ability = ability.upper()
    for cname, fails in _AUTO_FAIL_SAVES.items():
        if ability in fails and target.has_condition(cname):
            _ev(new, events, "save", f"{target.name} automatically fails the {ability} save vs DC {dc} ({source_name}).",
                target.id, target=target.id, ability=ability, dc=dc, success=False, auto=True, source=source_name)
            return False, None
    adv: list[str] = []
    dis: list[str] = []
    tag_set = {str(t).lower() for t in (tags or []) if t}
    if "poison" in tag_set or "poisoned" in tag_set:
        tag_set |= {"poison", "poisoned"}
    for cname, abilities in _SAVE_DISADVANTAGE.items():
        if ability in abilities and target.has_condition(cname):
            dis.append(cname)
    if target.exhaustion_level() >= 3:
        dis.append("exhaustion")
    if target.sheet:
        for adv_tag in target.sheet.save_advantages:
            if adv_tag.lower() in tag_set:
                adv.append(adv_tag)
    for b in target.flags.get("buffs", []):
        if ability in [a.upper() for a in (b.get("save_advantage") or [])]:
            adv.append(b["name"])
    if _trait(target, "magic_resistance") and is_spell:
        adv.append("magic resistance")
    if _trait(target, "dark_devotion") and tag_set & {"charmed", "frightened"}:
        adv.append("dark devotion")
    if _trait(target, "turning_defiance") and "turn" in tag_set:
        adv.append("turning defiance")
    two = _trait(target, "two_heads")
    if two and tag_set & {c.lower() for c in two.get("save_advantage_conditions", [])}:
        adv.append("two heads")
    mode = "normal"
    if adv and not dis:
        mode = "advantage"
    elif dis and not adv:
        mode = "disadvantage"
    bonus = target.save_bonus(ability)
    if cover and ability == "DEX":
        bonus += cover
    roll = rng.roll_d20(bonus, mode)
    total = roll.total
    die_total, extra = _bonus_die_total(rng, target, "save_die")
    total += die_total
    ok = total >= dc
    reasons = f" [{', '.join(adv + dis)}]" if (adv or dis) else ""
    _ev(new, events, "save",
        f"{target.name} {ability} save vs DC {dc} ({source_name}): {_roll_text(roll)}{extra}"
        + (f" (+{cover} cover)" if cover and ability == "DEX" else "")
        + f", {'success' if ok else 'failure'}{reasons}",
        target.id, target=target.id, ability=ability, dc=dc, roll=roll.to_dict(), total=total, success=ok,
        source=source_name)
    return ok, roll


# ---------------------------------------------------------------- concentration
def _end_concentration(new: GameState, events: list[Event], caster: Combatant, reason: str = "") -> None:
    conc = caster.concentration
    if not conc:
        return
    spell = conc.get("spell")
    tag = f"{caster.id}:{spell}"
    for c in new.combatants.values():
        gone = [cd for cd in c.conditions if cd.source == tag]
        if gone:
            c.conditions = [cd for cd in c.conditions if cd.source != tag]
            for cd in gone:
                if cd.name == "unconscious":
                    c.remove_condition("prone")
                _ev(new, events, "condition_remove", f"{c.name} is no longer {cd.name} ({spell} ends).",
                    c.id, condition=cd.name, spell=spell)
        _remove_buffs(c, source=tag)
    for key in ("spirit_guardians", "flaming_sphere"):
        if (caster.flags.get(key) or {}).get("source") == tag:
            caster.flags.pop(key, None)
    caster.concentration = None
    _ev(new, events, "concentration_broken", f"{caster.name}'s concentration on {spell} ends ({reason}).",
        caster.id, spell=spell, reason=reason)


# ---------------------------------------------------------------- spells (resolution)
def _do_cast(new: GameState, events: list[Event], rng: RNG, actor: Combatant, tpl: ActionTemplate, params: dict) -> None:
    name = params.get("spell")
    row = srd.spell(name)
    eff = row["effect"]
    slot = int(params.get("slot", 0))
    lvl = int(row["level"])
    if tpl.cost == "bonus":
        actor.turn["bonus"] = True
        actor.flags["cast_bonus_spell"] = True
    else:
        actor.turn["action"] = True
        actor.flags["cast_action_spell"] = True
    if lvl > 0:
        slots = _slots(actor)
        if slots.get(slot, 0) <= 0:
            raise IllegalAction(f"no level {slot} slot left for {name}")
        actor.resources.setdefault("spell_slots", {})[slot] = slots[slot] - 1
    _break_invisibility(new, events, actor)
    if actor.has_condition("hidden"):
        _remove_condition(new, events, actor, "hidden", "casting reveals them")

    # ---- targets
    targets: list[Combatant] = []
    point: tuple[int, int] | None = None
    if "point" in tpl.needs:
        raw = params.get("point")
        if raw is None:
            raise IllegalAction(f"{name} needs a point")
        point = _parse_point(raw)
        if not new.grid.in_bounds(point):
            raise IllegalAction(f"point {point} is off the grid")
        rng_ft = _eff_range(eff)
        if rng_ft and not _self_origin(row) and Grid.distance_ft(_pos(actor), point) > rng_ft:
            raise IllegalAction(f"point {point} is beyond {name}'s range")
    elif "targets" in tpl.needs:
        n = int(params.get("max_targets", _spell_targets_count(eff, slot, lvl)))
        ids = [t for t in (params.get("targets") or []) if isinstance(t, str)]
        ids = [t for t in ids if t in new.combatants and _spell_target_ok(new, actor, new.combatants[t], row)]
        if not ids:
            ids = list(params.get("suggested") or [])
        if not ids:
            raise IllegalAction(f"{name} needs targets")
        ids = ids[:n]
        if eff.get("rays") and len(ids) < n:  # spare darts/rays go round-robin over the chosen targets
            i = 0
            while len(ids) < n:
                ids.append(ids[i % len(ids)])
                i += 1
        targets = [new.combatants[t] for t in ids]
    else:
        tid = params.get("target", actor.id)
        if tid not in new.combatants:
            raise IllegalAction(f"unknown target {tid!r}")
        targets = [new.combatants[tid]]

    tag = f"{actor.id}:{name}"
    if eff.get("concentration"):
        if actor.concentration:
            _end_concentration(new, events, actor, reason=f"casting {name}")
        actor.concentration = {"spell": name, "targets": [t.id for t in targets], "started_round": new.round,
                               "rounds_left": int(eff.get("duration_rounds") or 10), "slot": slot}
    tgt_txt = ""
    if point is not None:
        tgt_txt = f" at ({point[0]},{point[1]})"
    elif targets and not (len(targets) == 1 and targets[0].id == actor.id):
        tgt_txt = " at " + ", ".join(dict.fromkeys(t.name for t in targets))
    _ev(new, events, "spell_cast",
        f"{actor.name} casts {name}" + (f" (L{slot})" if slot else "") + tgt_txt + ".",
        actor.id, spell=name, slot=slot, targets=[t.id for t in targets],
        point=list(point) if point else None, concentration=bool(eff.get("concentration")))

    kind = eff["kind"]
    if kind == "attack":
        _cast_attack(new, events, rng, actor, row, slot, targets, params)
    elif kind in ("save", "debuff"):
        if _is_aura(row):
            actor.flags["spirit_guardians"] = {
                "spell": name, "dc": actor.spell_dc, "damage": _spell_damage(eff, actor, slot, lvl),
                "damage_type": eff.get("damage_type", "radiant"), "radius": int(eff["area"]["size"]),
                "save": eff.get("save", "WIS"), "half_on_save": bool(eff.get("half_on_save")),
                "enemies_only": bool(eff.get("enemies_only")), "source": tag,
            }
            _ev(new, events, "condition_add", f"{name} fills a {eff['area']['size']} ft radius around {actor.name}.",
                actor.id, condition=_slug(name))
        elif eff.get("persistent_aura") and point is not None:
            actor.flags["flaming_sphere"] = {
                "spell": name, "pos": [point[0], point[1]], "dc": actor.spell_dc,
                "damage": _spell_damage(eff, actor, slot, lvl), "damage_type": eff.get("damage_type", "fire"),
                "save": eff.get("save", "DEX"), "half_on_save": bool(eff.get("half_on_save")),
                "radius": int(eff["area"]["size"]), "source": tag,
            }
            _sphere_burn(new, events, rng, actor, point)
        else:
            _cast_save(new, events, rng, actor, row, slot, targets, point, tag)
    elif kind == "heal":
        expr = _spell_damage(eff, actor, slot, lvl)
        for t in targets:
            roll = rng.roll(expr)
            mod = actor.spellcasting_mod() if eff.get("add_mod") else 0
            if actor.has_feature("disciple_of_life") and lvl > 0:
                mod += 2 + slot
            _heal(new, events, t, roll.total + mod, name, roll=roll, source_id=actor.id, expr=expr, mod=mod)
    elif kind == "buff":
        for t in targets:
            _apply_buff(new, events, rng, actor, t, row, slot, tag)
    elif kind == "utility":
        _cast_utility(new, events, rng, actor, row, slot, targets, point)
    else:  # "summon_none" or anything unknown: the cast event is the whole effect
        _ev(new, events, "system", f"{name} has no further mechanical effect.", actor.id, spell=name)

    if eff.get("summoned_weapon"):
        actor.flags["spiritual_weapon"] = {
            "rounds": int(eff.get("duration_rounds") or 10),
            "damage": _with_mod(_spell_damage(eff, actor, slot, lvl), actor.spellcasting_mod() if eff.get("add_mod") else 0),
            "bonus": actor.spell_attack_bonus,
        }


def _parse_point(raw: Any) -> tuple[int, int]:
    if isinstance(raw, dict):
        raw = raw.get("point") or raw.get("to") or [raw.get("x"), raw.get("y")]
    if isinstance(raw, str):
        nums = re.findall(r"-?\d+", raw)
        if len(nums) >= 2:
            return int(nums[0]), int(nums[1])
        raise IllegalAction(f"bad point {raw!r}")
    if isinstance(raw, (list, tuple)) and len(raw) == 2 and all(isinstance(v, (int, float)) for v in raw):
        return int(raw[0]), int(raw[1])
    if isinstance(raw, (list, tuple)) and len(raw) == 1:
        return _parse_point(raw[0])
    raise IllegalAction(f"bad point {raw!r}")


def _cast_attack(new: GameState, events: list[Event], rng: RNG, actor: Combatant, row: dict, slot: int,
                 targets: list[Combatant], params: dict) -> None:
    eff = row["effect"]
    lvl = int(row["level"])
    dmg = _spell_damage(eff, actor, slot, lvl)
    if eff.get("add_mod"):
        dmg = _with_mod(dmg, actor.spellcasting_mod())
    dtype = eff.get("damage_type", "force")
    choices = [x.lower() for x in eff.get("choose_damage_type") or []]
    if choices and str(params.get("damage_type", "")).lower() in choices:
        dtype = str(params["damage_type"]).lower()
    if eff.get("auto_hit"):
        for t in targets:
            if t.dead:
                continue
            if _has_buff(t, "shield"):
                _ev(new, events, "system", f"{t.name}'s Shield absorbs the {row['name']} dart.", t.id, target=t.id)
                continue
            r = rng.roll(dmg)
            _ev(new, events, "roll", f"{row['name']} dart hits {t.name}: {_roll_text(r)} {dtype}", actor.id,
                target=t.id, amount=r.total)
            _deal_damage(new, events, rng, t, r.total, dtype, actor.id)
        return
    rider = {k: eff[k] for k in ("speed_reduction", "effect_duration_rounds", "no_healing_rounds",
                                 "no_reactions_rounds", "grants_advantage_rounds", "drain_half") if eff.get(k)}
    spec = {"name": row["name"], "bonus": actor.spell_attack_bonus, "damage": dmg, "dice": dmg, "mod": 0,
            "damage_type": dtype, "ranged": eff.get("attack_type") == "ranged", "reach": 5,
            "range": (_eff_range(eff) or 60, _eff_range(eff) or 60),
            "properties": [], "on_hit": rider or None, "is_spell": True}
    for t in targets:
        if not t.dead:
            _resolve_attack(new, events, rng, actor, t, spec)


def _cast_save(new: GameState, events: list[Event], rng: RNG, actor: Combatant, row: dict, slot: int,
               targets: list[Combatant], point: tuple[int, int] | None, tag: str) -> None:
    eff = row["effect"]
    name = row["name"]
    lvl = int(row["level"])
    dc = actor.spell_dc
    dmg_expr = _spell_damage(eff, actor, slot, lvl)
    dtype = eff.get("damage_type", "force")
    affected = list(targets)
    protected: list[str] = []
    if point is not None:
        affected = [c for c in _creatures_in_area(new, actor, row, point) if _type_ok(c, eff)]
        if eff.get("enemies_only"):
            affected = [c for c in affected if _hostile(actor, c)]
        if actor.has_feature("sculpt_spells") and row.get("school") == "evocation":
            allies = [c for c in affected if c.side == actor.side]
            protected = [c.id for c in allies[: 1 + slot]]
            affected = [c for c in affected if c.id not in protected]
            if protected:
                names = ", ".join(new.combatants[i].name for i in protected)
                _ev(new, events, "system", f"{actor.name} sculpts the spell around {names}.", actor.id,
                    protected=protected)
    if not affected:
        _ev(new, events, "system", f"{name} affects no one.", actor.id, spell=name)
        return
    shared = rng.roll(dmg_expr) if (dmg_expr and point is not None) else None
    for t in affected:
        if t.dead:
            continue
        cover = 0 if eff.get("ignores_cover") else _cover_bonus(new.grid.cover_between(_pos(actor), _pos(t)))
        ok, _ = _saving_throw(new, events, rng, t, eff["save"], dc, source_name=name, cover=cover, is_spell=True,
                              tags=[dtype] + list(eff.get("conditions_applied", [])))
        if dmg_expr:
            r = shared or rng.roll(dmg_expr)
            amount = r.total
            if ok:
                amount = amount // 2 if eff.get("half_on_save") else 0
            if amount or not ok:
                _deal_damage(new, events, rng, t, amount, dtype, actor.id)
        if not ok:
            duration = eff.get("condition_duration") or eff.get("duration_rounds")
            repeat = bool(eff.get("repeat_save"))
            for cname in eff.get("conditions_applied", []):
                cond = Condition(cname, duration=duration,
                                 source=tag if eff.get("concentration") else f"{actor.id}:{name}:{new.round}",
                                 save_dc=dc if repeat else None, save_ability=eff["save"] if repeat else None,
                                 extra={"repeat_save": repeat, "spell": name})
                _add_condition(new, events, t, cond, name)
            if eff.get("push_ft"):
                _push(new, events, actor, t, int(eff["push_ft"]))


def _sphere_burn(new: GameState, events: list[Event], rng: RNG, caster: Combatant, point: tuple[int, int],
                 only: Combatant | None = None) -> None:
    """Flaming Sphere: creatures within 5 ft of the sphere save or burn."""
    fs = caster.flags.get("flaming_sphere")
    if not fs:
        return
    squares = new.grid.sphere_squares(tuple(point), int(fs["radius"]))
    victims = [only] if only else sorted((c for c in new.combatants.values()
                                          if not c.dead and _pos(c) in squares and c.id != caster.id),
                                         key=lambda c: c.id)
    for t in victims:
        ok, _ = _saving_throw(new, events, rng, t, fs["save"], int(fs["dc"]), source_name=fs["spell"], is_spell=True)
        r = rng.roll(fs["damage"])
        amount = (r.total // 2 if fs.get("half_on_save") else 0) if ok else r.total
        if amount:
            _deal_damage(new, events, rng, t, amount, fs["damage_type"], caster.id)


def _ram_flaming_sphere(new: GameState, events: list[Event], rng: RNG, actor: Combatant, params: dict) -> None:
    fs = actor.flags.get("flaming_sphere")
    if not fs:
        raise IllegalAction("no Flaming Sphere to move")
    point = _parse_point(params.get("point"))
    if not new.grid.in_bounds(point) or Grid.distance_ft(tuple(fs["pos"]), point) > 30:
        raise IllegalAction(f"the sphere cannot reach {point}")
    actor.turn["bonus"] = True
    fs["pos"] = [point[0], point[1]]
    _ev(new, events, "move", f"{actor.name} rolls the Flaming Sphere to ({point[0]},{point[1]}).", actor.id,
        to=list(point), sphere=True)
    _sphere_burn(new, events, rng, actor, point)


def _push(new: GameState, events: list[Event], src: Combatant, t: Combatant, ft: int) -> None:
    sx, sy = _pos(src)
    tx, ty = _pos(t)
    dx = (tx > sx) - (tx < sx)
    dy = (ty > sy) - (ty < sy)
    if dx == 0 and dy == 0:
        dx = 1
    occupied = set(new.grid.occupied(new, ignore=t.id))
    pos = (tx, ty)
    for _ in range(ft // 5):
        nxt = (pos[0] + dx, pos[1] + dy)
        if not new.grid.passable(nxt) or nxt in occupied:
            break
        pos = nxt
    if pos != (tx, ty):
        t.position = pos
        _ev(new, events, "move", f"{t.name} is pushed from ({tx},{ty}) to ({pos[0]},{pos[1]}).", t.id,
            **{"from": [tx, ty], "to": list(pos), "forced": True})


def _apply_buff(new: GameState, events: list[Event], rng: RNG, actor: Combatant, t: Combatant, row: dict,
                slot: int, tag: str) -> None:
    eff = row["effect"]
    name = row["name"]
    lvl = int(row["level"])
    rounds = int(eff.get("duration_rounds") or 10)
    src = tag if eff.get("concentration") else f"{actor.id}:{name}:{new.round}"
    if eff.get("temp_hp"):
        _, _, flat = _parse_upcast(eff, lvl, slot)
        r = rng.roll(eff["temp_hp"])
        amount = r.total + flat
        if amount > t.temp_hp:
            t.temp_hp = amount
        _ev(new, events, "heal", f"{t.name} gains {amount} temporary HP from {name} ({_roll_text(r)}).", actor.id,
            target=t.id, temp_hp=t.temp_hp, amount=amount)
        return
    if eff.get("conditions_applied"):
        duration = eff.get("condition_duration") or rounds
        for cname in eff["conditions_applied"]:
            _add_condition(new, events, t, Condition(cname, duration=duration, source=src, extra={"spell": name}), name)
        return
    buff: dict[str, Any] = {"name": _slug(name), "rounds": rounds, "tick": "end", "source": src}
    bits = []
    if eff.get("ac_bonus"):
        buff["ac"] = int(eff["ac_bonus"])
        bits.append(f"+{eff['ac_bonus']} AC")
    if eff.get("set_base_ac"):
        shield = 2 if (t.sheet and t.sheet.shield) else 0
        delta = int(eff["set_base_ac"]) + t.mod("DEX") + shield - t.ac
        buff["ac"] = max(0, delta)
        bits.append(f"AC {t.ac + buff['ac']}")
    for key in ("attack_bonus_die", "save_bonus_die", "check_bonus_die"):
        if eff.get(key):
            buff[key.replace("_bonus_", "_")] = eff[key]
            bits.append(f"+{eff[key]} {key.split('_')[0]}s")
    if eff.get("uses"):
        buff["uses"] = int(eff["uses"])
    if eff.get("max_hp_bonus"):
        _, _, flat = _parse_upcast(eff, lvl, slot)
        bonus = int(eff["max_hp_bonus"]) + flat
        t.max_hp += bonus
        t.hp += bonus
        buff["max_hp"] = bonus
        bits.append(f"+{bonus} max HP")
    for key in ("attackers_disadvantage", "extra_action", "max_healing"):
        if eff.get(key):
            buff[key] = True
            bits.append(key.replace("_", " "))
    if eff.get("speed_multiplier"):
        buff["speed_multiplier"] = int(eff["speed_multiplier"])
        bits.append(f"speed x{eff['speed_multiplier']}")
    if eff.get("save_advantage"):
        buff["save_advantage"] = [a.upper() for a in eff["save_advantage"]]
        bits.append("advantage on " + "/".join(buff["save_advantage"]) + " saves")
    if eff.get("fly_speed"):
        buff["fly_speed"] = int(eff["fly_speed"])
        bits.append(f"flying {eff['fly_speed']} ft")
    _add_buff(t, buff)
    if buff.get("extra_action"):
        t.turn["haste_action"] = True
    _ev(new, events, "condition_add", f"{t.name} gains {name} ({', '.join(bits) or 'buff'}).", actor.id,
        target=t.id, condition=buff["name"], duration=rounds, ac_bonus=buff.get("ac", 0))


def _cast_utility(new: GameState, events: list[Event], rng: RNG, actor: Combatant, row: dict, slot: int,
                  targets: list[Combatant], point: tuple[int, int] | None) -> None:
    eff = row["effect"]
    name = row["name"]
    if eff.get("stabilize"):
        for t in targets:
            if not t.dead and t.hp <= 0 and not t.stable:
                t.stable = True
                t.death_saves = {"success": 0, "failure": 0}
                _ev(new, events, "stable", f"{t.name} is stabilized by {name}.", actor.id, target=t.id)
        return
    if eff.get("removes_conditions"):
        for t in targets:
            for cname in eff["removes_conditions"]:
                if _remove_condition(new, events, t, cname, name):
                    break
        return
    if eff.get("teleport_ft"):
        if point is None:
            raise IllegalAction(f"{name} needs a point")
        if not new.grid.passable(point) or point in set(new.grid.occupied(new, ignore=actor.id)):
            raise IllegalAction(f"{point} is not an empty square")
        old = _pos(actor)
        actor.position = point
        _ev(new, events, "move", f"{actor.name} teleports from ({old[0]},{old[1]}) to ({point[0]},{point[1]}).",
            actor.id, **{"from": list(old), "to": list(point), "teleport": True})
        return
    if eff.get("dispel_level"):
        for t in targets:
            for b in list(t.flags.get("buffs", [])):
                _remove_buffs(t, name=b.get("name"))
                _ev(new, events, "condition_remove", f"{t.name}'s {b.get('name')} is dispelled.", actor.id,
                    target=t.id, condition=b.get("name"))
            for cd in list(t.conditions):
                if cd.source and ":" in str(cd.source):
                    _remove_condition(new, events, t, cd.name, "dispelled")
            if t.concentration:
                _end_concentration(new, events, t, reason="dispelled")
        return
    _ev(new, events, "system", f"{name} takes effect.", actor.id, spell=name)


# ---------------------------------------------------------------- movement
def _parse_path(raw: Any) -> list[tuple[int, int]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("path") or raw.get("to") or raw.get("point")
    if isinstance(raw, str):
        nums = [int(x) for x in re.findall(r"-?\d+", raw)]
        return [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
    if isinstance(raw, (list, tuple)):
        if len(raw) == 2 and all(isinstance(v, (int, float)) for v in raw):
            return [(int(raw[0]), int(raw[1]))]
        out = []
        for item in raw:
            out.extend(_parse_path(item))
        return out
    raise IllegalAction(f"bad path {raw!r}")


def reactions_for(state: GameState, trigger: dict) -> list[ActionTemplate]:
    """Reaction templates a trigger would allow. Phase 1: opportunity attacks
    when `trigger = {"type": "move", "mover": id, "from": (x,y), "to": (x,y)}`.
    The engine resolves these itself inside `apply`; the list is informational."""
    out: list[ActionTemplate] = []
    if trigger.get("type") != "move":
        return out
    mover = state.combatants.get(trigger.get("mover"))
    if mover is None:
        return out
    if mover.has_condition("invisible") or mover.flags.get("disengaged"):
        return out
    src = tuple(trigger["from"])
    dst = tuple(trigger["to"])
    n = 0
    for e in sorted(state.combatants.values(), key=lambda c: c.id):
        if not _hostile(e, mover) or not _can_react(e):
            continue
        reach = e.reach_ft()
        if Grid.distance_ft(_pos(e), src) <= reach < Grid.distance_ft(_pos(e), dst):
            spec = _best_melee_spec(e)
            if spec is None:
                continue
            n += 1
            out.append(ActionTemplate(
                id=f"r{n}", type="attack",
                label=_label(f"{e.name}: opportunity attack on {mover.name} with {spec['name']} ({spec['bonus']:+d}, {spec['damage']})"),
                params={"reactor": e.id, "target": mover.id, "weapon": spec["name"]}, needs=[], cost="reaction"))
    return out


def threat_map(state: GameState, mover: Combatant) -> dict[tuple[int, int], frozenset[str]]:
    """Squares where standing costs the mover a hit if they step out of them.

    Same predicate as `reactions_for`, evaluated square by square instead of
    edge by edge, so `Grid.path` can prefer an equally short route that walks
    around a threatened square rather than through it.
    """
    if mover.has_condition("invisible") or mover.flags.get("disengaged"):
        return {}
    out: dict[tuple[int, int], set[str]] = {}
    for e in state.combatants.values():
        if not _hostile(e, mover) or not _can_react(e) or _best_melee_spec(e) is None:
            continue
        ex, ey = _pos(e)
        span = e.reach_ft() // 5
        for x in range(ex - span, ex + span + 1):
            for y in range(ey - span, ey + span + 1):
                if state.grid.in_bounds((x, y)):
                    out.setdefault((x, y), set()).add(e.id)
    return {sq: frozenset(ids) for sq, ids in out.items()}


def _do_move(new: GameState, events: list[Event], rng: RNG, actor: Combatant, tpl: ActionTemplate, params: dict) -> None:
    waypoints = _parse_path(params.get("path"))
    if not waypoints:
        raise IllegalAction("move needs a path (a destination square or list of squares)")
    grid = new.grid
    if actor.has_condition("prone"):
        cost = max(5, actor.speed // 2)
        if int(actor.turn.get("movement_left", 0)) < cost:
            raise IllegalAction("not enough movement to stand up")
        actor.turn["movement_left"] -= cost
        actor.remove_condition("prone")
        _ev(new, events, "condition_remove", f"{actor.name} stands up ({cost} ft).", actor.id, condition="prone")
    budget = int(actor.turn.get("movement_left", 0))
    threat = threat_map(new, actor)
    full: list[tuple[int, int]] = []
    pos = _pos(actor)
    spent = 0
    for wp in waypoints:
        wp = (int(wp[0]), int(wp[1]))
        if wp == pos:
            continue
        seg = grid.path(new, pos, wp, budget - spent, mover_id=actor.id, threat=threat)
        if seg is None:
            raise IllegalAction(f"{actor.name} cannot reach ({wp[0]},{wp[1]}) with {budget - spent} ft left")
        full.extend(seg)
        spent += grid.path_cost(seg)
        pos = wp
    if not full:
        raise IllegalAction("move goes nowhere")
    actor.flags["moves_taken"] = int(actor.flags.get("moves_taken", 0)) + 1
    start = _pos(actor)
    stopped = False
    for sq in full:
        prev = _pos(actor)
        trig = {"type": "move", "mover": actor.id, "from": prev, "to": sq}
        for react in reactions_for(new, trig):
            e = new.combatants[react.params["reactor"]]
            spec = next((s for s in _attack_specs(e) if s["name"] == react.params["weapon"]), None)
            if spec is None:
                continue
            e.turn["reaction"] = True
            _resolve_attack(new, events, rng, e, actor, spec, opportunity=True, provoke=(prev, sq))
            if not _can_act(actor):
                stopped = True
                break
        if stopped:
            break
        actor.position = sq
        actor.turn["movement_left"] = int(actor.turn.get("movement_left", 0)) - grid.cost_of(sq)
    end = _pos(actor)
    ft = grid.path_cost(full[: full.index(end) + 1]) if end in full else 0
    if end == start:
        _ev(new, events, "move", f"{actor.name} is stopped before moving.", actor.id,
            **{"from": list(start), "to": list(end), "ft": 0})
    else:
        _ev(new, events, "move",
            f"{actor.name} moves from ({start[0]},{start[1]}) to ({end[0]},{end[1]}) ({ft} ft"
            + (", interrupted" if stopped else "") + ").",
            actor.id, **{"from": list(start), "to": list(end), "ft": ft, "path": [list(p) for p in full]})


# ---------------------------------------------------------------- hide / turn undead
def _do_hide(new: GameState, events: list[Event], rng: RNG, actor: Combatant) -> None:
    mode = "normal"
    if actor.exhaustion_level() >= 1 or actor.has_condition("poisoned") or actor.has_condition("frightened"):
        mode = "disadvantage"
    if actor.sheet and actor.sheet.armor:
        try:
            if srd.armor(actor.sheet.armor).get("stealth_disadvantage"):
                mode = "disadvantage"
        except srd.SRDLookupError:
            pass
    roll = rng.roll_d20(actor.skill_bonus("Stealth"), mode)
    watchers = [c for c in _enemy_targets(new, actor) if _can_act(c)]
    passive = max((c.passive_perception for c in watchers), default=10)
    ok = roll.total >= passive
    _ev(new, events, "skill_check",
        f"{actor.name} tries to hide: Stealth {_roll_text(roll)} vs passive Perception {passive}, "
        f"{'hidden' if ok else 'spotted'}",
        actor.id, skill="Stealth", dc=passive, success=ok, roll=roll.to_dict())
    if ok:
        _add_condition(new, events, actor, Condition("hidden", source=actor.id), "Hide")


def _destroy_undead_cr(actor: Combatant) -> float:
    """classes.json feature ids: destroy_undead_half -> CR 1/2, destroy_undead_1 -> CR 1, ..."""
    best = -1.0
    for f in (actor.sheet.features if actor.sheet else []):
        m = re.match(r"destroy_undead(?:_(half|quarter|\d+))?$", f)
        if m:
            word = m.group(1) or "half"
            best = max(best, {"half": 0.5, "quarter": 0.25}.get(word, float(word) if word.isdigit() else 0.5))
    return best


def _turn_undead(new: GameState, events: list[Event], rng: RNG, actor: Combatant) -> None:
    dc = actor.spell_dc
    _ev(new, events, "spell_cast", f"{actor.name} presents a holy symbol: Turn Undead (WIS DC {dc}).", actor.id,
        feature="turn_undead", dc=dc)
    destroy_cr = _destroy_undead_cr(actor)
    for c in _enemy_targets(new, actor):
        if not c.is_undead() or _dist(actor, c) > 30 or not new.grid.has_line_of_sight(_pos(actor), _pos(c)):
            continue
        ok, _ = _saving_throw(new, events, rng, c, "WIS", dc, source_name="Turn Undead", tags=["turn"])
        if ok:
            continue
        if float((c.stat_block or {}).get("cr", 0)) <= destroy_cr:
            _die(new, events, c, "destroyed by Turn Undead")
            continue
        _add_condition(new, events, c, Condition("turned", duration=10, source=actor.id), "Turn Undead")


# ---------------------------------------------------------------- turn structure
def _reset_turn(c: Combatant) -> None:
    c.turn = {
        "action": False, "bonus": False, "reaction": False,
        "movement_left": c.effective_speed(), "attacks_left": 0, "free_object": False,
    }
    if _buff_any(c, "extra_action"):
        c.turn["haste_action"] = True
    for k in _TURN_FLAGS:
        c.flags.pop(k, None)


def _start_of_turn(new: GameState, events: list[Event], rng: RNG, c: Combatant) -> bool:
    """Start-of-turn bookkeeping. Returns False if the creature cannot take a turn."""
    _reset_turn(c)
    for other in new.combatants.values():
        h = other.flags.get("helped_against")
        if h and h.get("helper") == c.id:
            other.flags.pop("helped_against", None)
    if c.exhaustion_level() >= 6 and not c.dead:
        _die(new, events, c, "exhaustion")
        return False
    for b in list(c.flags.get("buffs", [])):
        if b.get("tick") == "start":
            b["rounds"] = int(b.get("rounds", 1)) - 1
            if b["rounds"] <= 0:
                _remove_buffs(c, name=b["name"])
                _ev(new, events, "condition_remove", f"{c.name}'s {b['name']} fades.", c.id, condition=b["name"])
    regen = _trait(c, "regeneration")
    if regen and _alive(c):
        if c.flags.pop("no_regen", False):
            _ev(new, events, "system", f"{c.name} cannot regenerate this turn.", c.id)
        elif c.hp < c.max_hp:
            _heal(new, events, c, int(regen.get("amount", 10)), "Regeneration")
    recharge = c.resources.get("recharge") or {}
    for aname in sorted(recharge):
        if not recharge[aname]:
            action = next((a for a in (c.stat_block or {}).get("actions", []) if a["name"] == aname), {})
            need = _recharge_min(action) or 5
            r = rng.roll("1d6")
            if r.total >= need:
                recharge[aname] = True
                _ev(new, events, "roll", f"{c.name}'s {aname} recharges ({_roll_text(r)}).", c.id, action=aname)
            else:
                _ev(new, events, "roll", f"{c.name}'s {aname} does not recharge ({_roll_text(r)}).", c.id, action=aname)
    # Auras: Spirit Guardians around a caster; Stench around a ghast.
    for caster in sorted(new.combatants.values(), key=lambda x: x.id):
        sg = caster.flags.get("spirit_guardians")
        if sg and _alive(caster) and caster.id != c.id and _dist(caster, c) <= int(sg["radius"]) \
                and (_hostile(caster, c) or not sg.get("enemies_only")):
            ok, _ = _saving_throw(new, events, rng, c, sg["save"], int(sg["dc"]), source_name=sg["spell"], is_spell=True)
            r = rng.roll(sg["damage"])
            amount = (r.total // 2 if sg.get("half_on_save") else 0) if ok else r.total
            if amount:
                _deal_damage(new, events, rng, c, amount, sg["damage_type"], caster.id)
            if not _alive(c):
                return False
        stench = _trait(caster, "stench_aura")
        if stench and _alive(caster) and _hostile(caster, c) and _dist(caster, c) <= int(stench.get("radius", 5)) \
                and stench.get("condition", "poisoned") not in c.condition_immunities() \
                and not c.has_condition(stench.get("condition", "poisoned")):
            ok, _ = _saving_throw(new, events, rng, c, stench.get("save", "CON"), int(stench.get("dc", 10)),
                                  source_name=f"{caster.name}'s Stench", tags=["poison", stench.get("condition", "poisoned")])
            if not ok:
                _add_condition(new, events, c, Condition(stench.get("condition", "poisoned"), duration=1, source=caster.id), "Stench")
    if c.flags.pop("lethargic", False):
        _ev(new, events, "system", f"{c.name} is overcome by lethargy as Haste ends and cannot act this turn.", c.id)
        c.turn.update({"action": True, "bonus": True, "movement_left": 0, "haste_action": False})
        _ev(new, events, "turn_start", f"{c.name}'s turn (round {new.round}).", c.id)
        return True
    if not _alive(c):
        return False
    _ev(new, events, "turn_start", f"{c.name}'s turn (round {new.round}).", c.id)
    return True


def _end_of_turn(new: GameState, events: list[Event], rng: RNG, c: Combatant) -> None:
    if not c.flags.get("turn_ended"):
        _ev(new, events, "turn_end", f"{c.name}'s turn ends.", c.id)
    c.flags.pop("turn_ended", None)
    # Flaming Spheres: a creature that ends its turn within 5 ft of one burns.
    for caster in sorted(new.combatants.values(), key=lambda x: x.id):
        fs = caster.flags.get("flaming_sphere")
        if fs and caster.id != c.id and _alive(c) and Grid.distance_ft(_pos(c), tuple(fs["pos"])) <= int(fs["radius"]):
            _sphere_burn(new, events, rng, caster, tuple(fs["pos"]), only=c)
    for cond in list(c.conditions):
        if cond.name not in [x.name for x in c.conditions]:
            continue
        if cond.extra.get("repeat_save") and cond.save_ability and cond.save_dc:
            ok, _ = _saving_throw(new, events, rng, c, cond.save_ability, int(cond.save_dc),
                                  source_name=f"{cond.extra.get('spell') or cond.name} (repeat)",
                                  tags=[cond.name])
            if ok:
                _remove_condition(new, events, c, cond.name, "shakes it off")
                continue
        if cond.duration is not None:
            cond.duration -= 1
            if cond.duration <= 0:
                _remove_condition(new, events, c, cond.name, "it wears off")
    for b in list(c.flags.get("buffs", [])):
        if b.get("tick", "end") == "end":
            b["rounds"] = int(b.get("rounds", 1)) - 1
            if b["rounds"] <= 0:
                _remove_buffs(c, name=b["name"])
                _ev(new, events, "condition_remove", f"{c.name}'s {b['name']} wears off.", c.id, condition=b["name"])
    if c.concentration:
        c.concentration["rounds_left"] = int(c.concentration.get("rounds_left", 1)) - 1
        if c.concentration["rounds_left"] <= 0:
            _end_concentration(new, events, c, reason="duration expired")
    sw = c.flags.get("spiritual_weapon")
    if sw:
        sw["rounds"] = int(sw.get("rounds", 1)) - 1
        if sw["rounds"] <= 0:
            c.flags.pop("spiritual_weapon", None)
            _ev(new, events, "condition_remove", f"{c.name}'s Spiritual Weapon fades.", c.id, condition="spiritual_weapon")
    for other in new.combatants.values():
        ga = other.flags.get("advantage_against")
        if ga and ga.get("until") == c.id:
            if ga.get("armed"):
                other.flags.pop("advantage_against", None)
            else:
                ga["armed"] = True


def _death_save(new: GameState, events: list[Event], rng: RNG, c: Combatant) -> None:
    roll = rng.roll_d20(0)
    nat = roll.natural or roll.total
    ds = c.death_saves
    if nat == 20:
        ds["success"], ds["failure"] = 0, 0
        c.hp = 1
        c.stable = False
        c.remove_condition("unconscious")
        _ev(new, events, "death_save", f"{c.name} death save: natural 20! {c.name} regains 1 HP.", c.id,
            roll=roll.to_dict(), success=True, revived=True)
        _ev(new, events, "condition_remove", f"{c.name} regains consciousness.", c.id, condition="unconscious")
        return
    if nat == 1:
        ds["failure"] = int(ds.get("failure", 0)) + 2
        outcome = "natural 1, two failures"
    elif roll.total >= 10:
        ds["success"] = int(ds.get("success", 0)) + 1
        outcome = "success"
    else:
        ds["failure"] = int(ds.get("failure", 0)) + 1
        outcome = "failure"
    _ev(new, events, "death_save",
        f"{c.name} death save: {_roll_text(roll)}, {outcome} ({ds['success']} successes / {ds['failure']} failures)",
        c.id, roll=roll.to_dict(), success=(outcome == "success"), successes=ds["success"], failures=ds["failure"])
    if ds["failure"] >= 3:
        _die(new, events, c, "three death save failures")
    elif ds["success"] >= 3:
        c.stable = True
        ds["success"], ds["failure"] = 0, 0
        _ev(new, events, "stable", f"{c.name} is stable.", c.id)


def _advance(new: GameState, events: list[Event], rng: RNG) -> None:
    """Move to the next combatant who can take a turn, handling round wrap,
    dead creatures (skipped) and dying ones (auto death save, then skipped)."""
    n = len(new.initiative)
    for _ in range(n + 1):
        new.turn_index += 1
        if new.turn_index >= n:
            new.turn_index = 0
            new.round += 1
            _ev(new, events, "round_start", f"Round {new.round} begins.", None, round=new.round)
        cid = new.initiative[new.turn_index][0]
        c = new.combatants.get(cid)
        if c is None or c.dead:
            continue
        if c.hp <= 0:
            if not c.stable:
                _death_save(new, events, rng, c)
            if c.hp <= 0 or c.dead:
                continue
        if _start_of_turn(new, events, rng, c):
            return


def start_combat(state: GameState, rng_state: dict | None = None) -> tuple[GameState, list[Event]]:
    new = state.copy()
    events: list[Event] = []
    rng = RNG.from_state(rng_state) if rng_state else _rng_of(new)
    new.mode = "combat"
    rolled = []
    for cid, c in new.combatants.items():
        if c.dead:
            continue
        roll = rng.roll_d20(c.mod("DEX"))
        rolled.append((cid, roll.total, c.abilities.get("DEX", 10), roll))
    rolled.sort(key=lambda t: (-t[1], -t[2], t[0]))
    new.initiative = [(cid, total) for cid, total, _, _ in rolled]
    for c in new.combatants.values():
        _reset_turn(c)
    order = ", ".join(f"{new.combatants[cid].name} {total}" for cid, total in new.initiative)
    _ev(new, events, "combat_start", f"Roll for initiative! Order: {order}.", None,
        initiative=[[cid, total] for cid, total in new.initiative],
        rolls={cid: r.to_dict() for cid, _, _, r in rolled})
    new.round = 0
    new.turn_index = len(new.initiative) - 1
    _advance(new, events, rng)
    new.rng = rng.state()
    return new, events


def advance_turn(state: GameState) -> tuple[GameState, list[Event]]:
    new = state.copy()
    events: list[Event] = []
    if new.mode != "combat" or not new.initiative:
        return new, events
    rng = _rng_of(new)
    cur = new.active()
    if cur is not None and not cur.dead and cur.hp > 0:
        _end_of_turn(new, events, rng, cur)
    _advance(new, events, rng)
    new.rng = rng.state()
    return new, events


def combat_over(state: GameState) -> str | None:
    if state.mode != "combat":
        return None
    party = any(c.side == "party" and _alive(c) and not c.flags.get("fled") for c in state.combatants.values())
    enemy = any(c.side == "enemy" and _alive(c) and not c.flags.get("fled") for c in state.combatants.values())
    if not party:
        return "enemy"
    if not enemy:
        return "party"
    return None


# ---------------------------------------------------------------- skill checks
def skill_check(state: GameState, actor_id: str, skill: str, dc: int) -> tuple[GameState, list[Event]]:
    if actor_id not in state.combatants:
        raise IllegalAction(f"no such combatant {actor_id!r}")
    skill = str(skill).strip().title() if str(skill).lower() != "sleight of hand" else "Sleight of Hand"
    if skill not in srd.SKILL_ABILITY:
        raise IllegalAction(f"unknown skill {skill!r}")
    new = state.copy()
    events: list[Event] = []
    rng = _rng_of(new)
    actor = new.combatants[actor_id]
    reasons = []
    if actor.exhaustion_level() >= 1:
        reasons.append("exhaustion")
    if actor.has_condition("poisoned"):
        reasons.append("poisoned")
    if actor.has_condition("frightened"):
        reasons.append("frightened")
    if actor.has_condition("blinded") and skill in ("Perception", "Investigation"):
        reasons.append("blinded")
    mode = "disadvantage" if reasons else "normal"
    bonus = actor.skill_bonus(skill)
    roll = rng.roll_d20(bonus, mode)
    total = roll.total
    die_total, extra = _bonus_die_total(rng, actor, "check_die")
    total += die_total
    ok = total >= int(dc)
    _ev(new, events, "skill_check",
        f"{actor.name} {skill} check vs DC {dc}: {_roll_text(roll)}{extra}, {'success' if ok else 'failure'}"
        + (f" [{', '.join(reasons)}]" if reasons else ""),
        actor.id, skill=skill, dc=int(dc), roll=roll.to_dict(), total=total, success=ok)
    new.rng = rng.state()
    return new, events
