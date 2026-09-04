"""A minimal, deterministic stand-in for `engine/` implementing CONTRACTS.md §1.

Only enough rules to drive the orchestrator: initiative, single attacks with a
flat to-hit, damage, unconsciousness, movement, and end_turn. It is NOT a 5e
implementation — the real engine is built in parallel by another builder.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from typing import Any

# --- dice ------------------------------------------------------------------


@dataclass
class RollResult:
    expr: str
    rolls: list
    kept: list
    modifier: int
    total: int
    mode: str = "normal"
    natural: int | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class RNG:
    def __init__(self, seed: int):
        self.seed = seed
        self._r = random.Random(seed)

    def roll(self, expr: str) -> RollResult:
        n, _, rest = expr.partition("d")
        n = int(n or 1)
        faces, plus, mod = rest.partition("+")
        modifier = int(mod) if plus else 0
        faces = int(faces or 6)
        rolls = [self._r.randint(1, faces) for _ in range(n)]
        return RollResult(expr, rolls, rolls, modifier, sum(rolls) + modifier)

    def roll_d20(self, mod: int = 0, mode: str = "normal") -> RollResult:
        rolls = [self._r.randint(1, 20)]
        if mode in ("advantage", "disadvantage"):
            rolls.append(self._r.randint(1, 20))
        kept = [max(rolls) if mode == "advantage" else min(rolls) if mode == "disadvantage" else rolls[0]]
        return RollResult("1d20", rolls, kept, mod, kept[0] + mod, mode, kept[0])

    def randint(self, a: int, b: int) -> int:
        return self._r.randint(a, b)

    def choice(self, seq):
        return self._r.choice(list(seq))

    def state(self) -> dict:
        return {"seed": self.seed, "state": self._r.getstate()}

    @classmethod
    def from_state(cls, d: dict) -> "RNG":
        r = cls(d["seed"])
        r._r.setstate(d["state"])
        return r


# --- data ------------------------------------------------------------------

_MONSTERS = {
    "Goblin": {"hp": 7, "ac": 15, "attack": 4, "damage": "1d6+2", "speed": 30},
    "Goblin Boss": {"hp": 21, "ac": 17, "attack": 4, "damage": "1d6+2", "speed": 30},
    "Skeleton": {"hp": 13, "ac": 13, "attack": 4, "damage": "1d6+2", "speed": 30},
    "Zombie": {"hp": 22, "ac": 8, "attack": 3, "damage": "1d6+1", "speed": 20},
    "Ghoul": {"hp": 22, "ac": 12, "attack": 4, "damage": "2d4+2", "speed": 30},
}


def rules_digest() -> str:
    return "Fake digest: roll d20 + modifier against AC; the engine decides everything."


# --- characters / state ----------------------------------------------------


@dataclass
class CharacterSheet:
    id: str
    name: str
    race: str
    klass: str
    level: int
    abilities: dict
    max_hp: int
    ac: int
    speed: int
    proficiency: int
    saves: list = field(default_factory=list)
    skills: list = field(default_factory=list)
    weapons: list = field(default_factory=list)
    armor: str | None = None
    shield: bool = False
    spells_known: list = field(default_factory=list)
    spell_slots: dict = field(default_factory=dict)
    spellcasting_ability: str | None = None
    features: list = field(default_factory=list)
    persona: str = ""
    pronouns: str = ""
    gender: str = ""


@dataclass
class Condition:
    name: str
    duration: int | None = None
    source: str | None = None
    save_dc: int | None = None
    save_ability: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Combatant:
    id: str
    name: str
    side: str
    kind: str
    sheet: Any = None
    stat_block: dict | None = None
    hp: int = 1
    max_hp: int = 1
    temp_hp: int = 0
    ac: int = 10
    speed: int = 30
    abilities: dict = field(default_factory=dict)
    save_profs: list = field(default_factory=list)
    skill_profs: list = field(default_factory=list)
    proficiency: int = 2
    position: tuple = (0, 0)
    size: str = "M"
    conditions: list = field(default_factory=list)
    concentration: dict | None = None
    death_saves: dict = field(default_factory=lambda: {"success": 0, "failure": 0})
    stable: bool = False
    dead: bool = False
    resources: dict = field(default_factory=dict)
    turn: dict = field(default_factory=dict)
    inventory: list = field(default_factory=list)


@dataclass
class Grid:
    width: int = 12
    height: int = 10
    difficult: set = field(default_factory=set)
    walls: set = field(default_factory=set)
    cover: dict = field(default_factory=dict)

    def distance_ft(self, a, b) -> int:
        return 5 * max(abs(a[0] - b[0]), abs(a[1] - b[1]))

    def path(self, state, start, goal, max_ft):
        return [goal]


@dataclass
class GameState:
    seed: int
    rng: dict
    mode: str
    round: int
    turn_index: int
    combatants: dict
    initiative: list
    grid: Grid
    scene: dict
    event_seq: int = 0

    def active_id(self) -> str | None:
        if not self.initiative:
            return None
        return self.initiative[self.turn_index % len(self.initiative)][0]

    def to_dict(self) -> dict:
        return {
            "seed": self.seed,
            "mode": self.mode,
            "round": self.round,
            "turn_index": self.turn_index,
            "scene": self.scene,
            "initiative": self.initiative,
            "combatants": {
                cid: {
                    "id": c.id,
                    "name": c.name,
                    "side": c.side,
                    "hp": c.hp,
                    "max_hp": c.max_hp,
                    "ac": c.ac,
                    "position": list(c.position),
                    "dead": c.dead,
                    "conditions": [cd.name for cd in c.conditions],
                }
                for cid, c in self.combatants.items()
            },
        }


@dataclass
class Event:
    seq: int
    round: int
    kind: str
    actor: str | None
    text: str
    data: dict = field(default_factory=dict)


@dataclass
class ActionTemplate:
    id: str
    type: str
    label: str
    params: dict = field(default_factory=dict)
    needs: list = field(default_factory=list)
    cost: str = "action"


@dataclass
class Action:
    actor: str
    template_id: str
    params: dict = field(default_factory=dict)
    speech: str | None = None


class IllegalAction(Exception):
    pass


_CLASS_HP = {"Fighter": 10, "Cleric": 8, "Rogue": 8, "Wizard": 6}
_CLASS_AC = {"Fighter": 18, "Cleric": 16, "Rogue": 14, "Wizard": 12}


def build_character(spec: dict, rng: RNG) -> CharacterSheet:
    klass = spec.get("klass", "Fighter")
    level = int(spec.get("level", 1))
    abilities = spec.get("abilities")
    if not isinstance(abilities, dict):
        abilities = {"STR": 15, "DEX": 14, "CON": 13, "INT": 12, "WIS": 10, "CHA": 8}
    return CharacterSheet(
        id=spec.get("id", "pc"),
        name=spec.get("name", "Someone"),
        race=spec.get("race", "Human"),
        klass=klass,
        level=level,
        abilities=dict(abilities),
        max_hp=_CLASS_HP.get(klass, 8) * level,
        ac=_CLASS_AC.get(klass, 12),
        speed=30,
        proficiency=2 + (level - 1) // 4,
        saves=["STR", "CON"],
        skills=["Perception", "Stealth"],
        weapons=spec.get("weapons") or ["Longsword"],
        spells_known=[],
        spell_slots={1: 2} if klass in ("Cleric", "Wizard") else {},
        features=[],
        persona=spec.get("persona", ""),
        pronouns=str(spec.get("pronouns", "") or ""),
        gender=str(spec.get("gender", "") or ""),
    )


def monster_to_combatant(name: str, cid: str, rng: RNG, roll_hp: bool = False) -> Combatant:
    block = _MONSTERS.get(name)
    if block is None:
        raise KeyError(f"unknown monster {name!r}")
    return Combatant(
        id=cid,
        name=name,
        side="enemy",
        kind="monster",
        stat_block=dict(block),
        hp=block["hp"],
        max_hp=block["hp"],
        ac=block["ac"],
        speed=block["speed"],
        abilities={"STR": 12, "DEX": 14, "CON": 12, "INT": 8, "WIS": 8, "CHA": 8},
        proficiency=2,
        position=(8, 4),
        resources={},
        turn={"action": False, "bonus": False, "reaction": False, "movement_left": block["speed"]},
    )


# --- rules -----------------------------------------------------------------


def _rng_of(state: GameState) -> RNG:
    return RNG.from_state(state.rng)


def _ev(state: GameState, kind: str, actor: str | None, text: str, data: dict | None = None) -> Event:
    state.event_seq += 1
    return Event(state.event_seq, state.round, kind, actor, text, data or {})


def _mod(score: int) -> int:
    return (score - 10) // 2


def _attack_bonus(c: Combatant) -> int:
    if c.kind == "monster":
        return int((c.stat_block or {}).get("attack", 3))
    return c.proficiency + _mod(c.abilities.get("STR", 10))


def _damage_expr(c: Combatant) -> str:
    if c.kind == "monster":
        return (c.stat_block or {}).get("damage", "1d6")
    return f"1d8+{_mod(c.abilities.get('STR', 10))}"


def _conscious(c: Combatant) -> bool:
    return not c.dead and c.hp > 0


def legal_actions(state: GameState, actor_id: str) -> list[ActionTemplate]:
    actor = state.combatants[actor_id]
    out: list[ActionTemplate] = []
    n = 0
    if not actor.turn.get("action"):
        for cid, target in state.combatants.items():
            if target.side == actor.side or not _conscious(target):
                continue
            if state.grid.distance_ft(actor.position, target.position) <= 5:
                n += 1
                out.append(
                    ActionTemplate(
                        id=f"a{n}",
                        type="attack",
                        label=f"Attack {target.name} (+{_attack_bonus(actor)}, {_damage_expr(actor)})",
                        params={"target": cid},
                        needs=[],
                        cost="action",
                    )
                )
    if actor.turn.get("movement_left", 0) > 0:
        enemies = [c for c in state.combatants.values() if c.side != actor.side and _conscious(c)]
        suggested = [list(e.position) for e in enemies[:4]]
        n += 1
        out.append(
            ActionTemplate(
                id=f"a{n}",
                type="move",
                label="Move up to your speed",
                params={"suggested": suggested},
                needs=["path"],
                cost="movement",
            )
        )
    n += 1
    out.append(ActionTemplate(id=f"a{n}", type="end_turn", label="End turn", params={}, needs=[], cost="free"))
    return out


def apply(state: GameState, action: Action) -> tuple[GameState, list[Event]]:
    state = replace(state, combatants={k: replace(v) for k, v in state.combatants.items()})
    actor = state.combatants.get(action.actor)
    if actor is None:
        raise IllegalAction(f"no such actor {action.actor}")
    templates = {t.id: t for t in legal_actions(state, action.actor)}
    tpl = templates.get(action.template_id)
    if tpl is None:
        raise IllegalAction(f"illegal template {action.template_id}")
    events: list[Event] = []
    rng = _rng_of(state)

    if tpl.type == "attack":
        target = state.combatants[tpl.params["target"]]
        roll = rng.roll_d20(_attack_bonus(actor))
        hit = roll.total >= target.ac
        events.append(
            _ev(
                state,
                "attack",
                actor.id,
                f"{actor.name} attacks {target.name}: 1d20+{_attack_bonus(actor)} → "
                f"{roll.total} vs AC {target.ac}, {'hit' if hit else 'miss'}",
                {"target": target.id, "hit": hit, "roll": roll.to_dict()},
            )
        )
        if hit:
            dmg = rng.roll(_damage_expr(actor))
            before = target.hp
            target.hp = max(0, target.hp - dmg.total)
            events.append(
                _ev(
                    state,
                    "damage",
                    actor.id,
                    f"{target.name} takes {dmg.total} damage ({before} → {target.hp} HP)",
                    {"target": target.id, "amount": dmg.total, "hp_before": before, "hp_after": target.hp},
                )
            )
            if target.hp == 0:
                if target.kind == "monster":
                    target.dead = True
                    events.append(_ev(state, "dead", target.id, f"{target.name} drops dead.", {}))
                else:
                    target.conditions.append(Condition("unconscious"))
                    events.append(_ev(state, "down", target.id, f"{target.name} falls unconscious.", {}))
        actor.turn["action"] = True
    elif tpl.type == "move":
        path = action.params.get("path") or []
        dest = tuple(path[-1]) if path else actor.position
        dest = (
            max(0, min(state.grid.width - 1, int(dest[0]))),
            max(0, min(state.grid.height - 1, int(dest[1]))),
        )
        old = actor.position
        actor.position = dest
        actor.turn["movement_left"] = 0
        events.append(
            _ev(state, "move", actor.id, f"{actor.name} moves from {old} to {dest}.", {"from": list(old), "to": list(dest)})
        )
    elif tpl.type == "end_turn":
        events.append(_ev(state, "turn_end", actor.id, f"{actor.name} ends their turn.", {}))
        actor.turn["action"] = True
        actor.turn["movement_left"] = 0
    state.rng = rng.state()
    return state, events


def start_combat(state: GameState, rng_state: dict) -> tuple[GameState, list[Event]]:
    rng = RNG.from_state(rng_state) if rng_state else _rng_of(state)
    state.mode = "combat"
    state.round = 1
    state.turn_index = 0
    order = []
    for cid, c in state.combatants.items():
        if c.dead:
            continue
        score = rng.roll_d20(_mod(c.abilities.get("DEX", 10))).total
        order.append((cid, score))
        c.turn = {"action": False, "bonus": False, "reaction": False, "movement_left": c.speed, "attacks_left": 1}
    order.sort(key=lambda t: (-t[1], t[0]))
    state.initiative = order
    state.rng = rng.state()
    events = [_ev(state, "combat_start", None, "Roll for initiative!", {"initiative": order})]
    events.append(_ev(state, "round_start", None, "Round 1 begins.", {"round": 1}))
    return state, events


def advance_turn(state: GameState) -> tuple[GameState, list[Event]]:
    events: list[Event] = []
    if not state.initiative:
        return state, events
    state.turn_index += 1
    if state.turn_index % len(state.initiative) == 0:
        state.round += 1
        events.append(_ev(state, "round_start", None, f"Round {state.round} begins.", {"round": state.round}))
    actor_id = state.active_id()
    actor = state.combatants.get(actor_id) if actor_id else None
    if actor is not None:
        actor.turn = {
            "action": False,
            "bonus": False,
            "reaction": False,
            "movement_left": actor.speed,
            "attacks_left": 1,
        }
        if actor.dead or actor.hp <= 0:
            actor.turn["action"] = True
            actor.turn["movement_left"] = 0
            if not actor.dead:
                events.append(_ev(state, "death_save", actor.id, f"{actor.name} is dying.", {}))
        else:
            events.append(_ev(state, "turn_start", actor.id, f"{actor.name}'s turn.", {}))
    return state, events


def combat_over(state: GameState) -> str | None:
    if state.mode != "combat":
        return None
    party = any(c.side == "party" and _conscious(c) for c in state.combatants.values())
    enemy = any(c.side == "enemy" and _conscious(c) for c in state.combatants.values())
    if not enemy:
        return "party"
    if not party:
        return "enemy"
    return None


def reactions_for(state: GameState, trigger: dict) -> list[ActionTemplate]:
    return []


def skill_check(state: GameState, actor_id: str, skill: str, dc: int) -> tuple[GameState, list[Event]]:
    actor = state.combatants[actor_id]
    rng = _rng_of(state)
    roll = rng.roll_d20(actor.proficiency)
    state.rng = rng.state()
    ok = roll.total >= dc
    ev = _ev(
        state,
        "skill_check",
        actor_id,
        f"{actor.name} rolls {skill}: {roll.total} vs DC {dc} — {'success' if ok else 'failure'}",
        {"skill": skill, "dc": dc, "success": ok, "roll": roll.to_dict()},
    )
    return state, [ev]
