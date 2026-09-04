"""Combat state: grid, combatants, conditions, and the whole-game snapshot."""

from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from engine import srd
from engine.characters import CharacterSheet, ability_mod

__all__ = ["Condition", "Combatant", "Grid", "GameState", "ABILITIES"]

ABILITIES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]

# Conditions that make a creature unable to take actions or reactions.
INCAPACITATING = {"incapacitated", "paralyzed", "petrified", "stunned", "unconscious"}
# Conditions that zero out speed.
IMMOBILIZING = {"grappled", "paralyzed", "petrified", "restrained", "stunned", "unconscious"}


@dataclass
class Condition:
    name: str
    duration: int | None = None          # rounds remaining; None = until removed
    source: str | None = None            # combatant id or spell name
    save_dc: int | None = None
    save_ability: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "duration": self.duration, "source": self.source,
                "save_dc": self.save_dc, "save_ability": self.save_ability,
                "extra": dict(self.extra)}

    @classmethod
    def from_dict(cls, d: dict) -> "Condition":
        return cls(name=d["name"], duration=d.get("duration"), source=d.get("source"),
                   save_dc=d.get("save_dc"), save_ability=d.get("save_ability"),
                   extra=dict(d.get("extra", {})))


@dataclass
class Combatant:
    id: str
    name: str
    side: str                 # "party" | "enemy" | "neutral"
    kind: str                 # "pc" | "monster"
    sheet: CharacterSheet | None = None
    stat_block: dict | None = None
    hp: int = 1
    max_hp: int = 1
    temp_hp: int = 0
    ac: int = 10
    speed: int = 30
    abilities: dict[str, int] = field(default_factory=lambda: {a: 10 for a in ABILITIES})
    save_profs: list[str] = field(default_factory=list)
    skill_profs: list[str] = field(default_factory=list)
    proficiency: int = 2
    position: tuple[int, int] = (0, 0)
    size: str = "M"
    conditions: list[Condition] = field(default_factory=list)
    concentration: dict | None = None
    death_saves: dict = field(default_factory=lambda: {"success": 0, "failure": 0})
    stable: bool = False
    dead: bool = False
    resources: dict = field(default_factory=dict)
    turn: dict = field(default_factory=dict)
    inventory: list[str] = field(default_factory=list)
    # transient per-turn / per-round flags the engine sets
    flags: dict = field(default_factory=dict)

    # ------------------------------------------------------------ derived
    def mod(self, ability: str) -> int:
        return ability_mod(self.abilities[ability])

    @property
    def conscious(self) -> bool:
        return (not self.dead) and self.hp > 0 and not self.has_condition("unconscious")

    @property
    def down(self) -> bool:
        return (not self.dead) and self.hp <= 0

    @property
    def incapacitated(self) -> bool:
        return any(c.name in INCAPACITATING for c in self.conditions) or self.dead

    @property
    def creature_type(self) -> str:
        if self.kind == "pc":
            return "humanoid"
        return (self.stat_block or {}).get("type", "humanoid")

    def is_undead(self) -> bool:
        return "undead" in self.creature_type.lower()

    def has_condition(self, name: str) -> bool:
        return any(c.name == name for c in self.conditions)

    def get_condition(self, name: str) -> Condition | None:
        for c in self.conditions:
            if c.name == name:
                return c
        return None

    def exhaustion_level(self) -> int:
        c = self.get_condition("exhaustion")
        return int(c.extra.get("level", 1)) if c else 0

    def add_condition(self, cond: Condition) -> bool:
        """Add (or refresh) a condition. Returns True if newly applied."""
        if cond.name in self.condition_immunities():
            return False
        if cond.name == "exhaustion":
            existing = self.get_condition("exhaustion")
            add = int(cond.extra.get("level", 1))
            if existing:
                existing.extra["level"] = min(6, existing.extra.get("level", 1) + add)
                return True
            cond.extra["level"] = min(6, add)
            self.conditions.append(cond)
            return True
        existing = self.get_condition(cond.name)
        if existing:
            # refresh to the longer duration
            if cond.duration is None or (existing.duration is not None
                                         and cond.duration > existing.duration):
                existing.duration = cond.duration
            return False
        self.conditions.append(cond)
        return True

    def remove_condition(self, name: str) -> bool:
        before = len(self.conditions)
        self.conditions = [c for c in self.conditions if c.name != name]
        return len(self.conditions) < before

    def condition_immunities(self) -> set[str]:
        if self.stat_block:
            return {c.lower() for c in self.stat_block.get("condition_immunities", [])}
        return set()

    def damage_resistances(self) -> set[str]:
        if self.stat_block:
            return {c.lower() for c in self.stat_block.get("damage_resistances", [])}
        if self.sheet:
            return {c.lower() for c in self.sheet.damage_resistances}
        return set()

    def damage_immunities(self) -> set[str]:
        if self.stat_block:
            return {c.lower() for c in self.stat_block.get("damage_immunities", [])}
        return set()

    def damage_vulnerabilities(self) -> set[str]:
        if self.stat_block:
            return {c.lower() for c in self.stat_block.get("damage_vulnerabilities", [])}
        return set()

    def effective_speed(self) -> int:
        if any(c.name in IMMOBILIZING for c in self.conditions):
            return 0
        sp = self.speed
        ex = self.exhaustion_level()
        if ex >= 5:
            return 0
        if ex >= 2:
            sp //= 2
        sp -= int(self.flags.get("speed_penalty", 0))
        if self.flags.get("speed_multiplier"):
            sp = int(sp * self.flags["speed_multiplier"])
        return max(0, sp)

    def effective_ac(self) -> int:
        return self.ac + int(self.flags.get("ac_bonus", 0))

    def save_bonus(self, ability: str) -> int:
        b = self.mod(ability)
        if self.stat_block:
            st = self.stat_block.get("saving_throws", {})
            if ability in st:
                return int(st[ability])
            return b
        if ability in self.save_profs:
            b += self.proficiency
        return b

    def skill_bonus(self, skill: str) -> int:
        if self.stat_block:
            sk = self.stat_block.get("skills", {})
            if skill in sk:
                return int(sk[skill])
            abil = srd.SKILL_ABILITY.get(skill, "DEX")
            return self.mod(abil)
        abil = srd.SKILL_ABILITY.get(skill)
        if abil is None:
            return 0
        b = self.mod(abil)
        if self.sheet:
            if skill in self.sheet.skills:
                b += self.proficiency
            if skill in self.sheet.expertise:
                b += self.proficiency
        return b

    @property
    def passive_perception(self) -> int:
        if self.stat_block:
            return int(self.stat_block.get("passive_perception", 10))
        return 10 + self.skill_bonus("Perception")

    @property
    def spell_dc(self) -> int:
        if self.sheet and self.sheet.spell_dc:
            return self.sheet.spell_dc
        if self.stat_block and self.stat_block.get("spellcasting"):
            return int(self.stat_block["spellcasting"].get("dc", 12))
        return 10

    @property
    def spell_attack_bonus(self) -> int:
        if self.sheet and self.sheet.spell_attack_bonus is not None:
            return self.sheet.spell_attack_bonus
        if self.stat_block and self.stat_block.get("spellcasting"):
            return int(self.stat_block["spellcasting"].get("attack_bonus", 4))
        return 2

    def spellcasting_mod(self) -> int:
        if self.sheet and self.sheet.spellcasting_ability:
            return self.mod(self.sheet.spellcasting_ability)
        if self.stat_block and self.stat_block.get("spellcasting"):
            return self.mod(self.stat_block["spellcasting"].get("ability", "WIS"))
        return 0

    def reach_ft(self) -> int:
        """Longest melee reach among available attacks (for opportunity attacks)."""
        best = 5
        if self.stat_block:
            for a in self.stat_block.get("actions", []):
                if a.get("kind") in ("melee_weapon", "melee_or_ranged"):
                    best = max(best, int(a.get("reach") or 5))
        elif self.sheet:
            for w in self.sheet.weapons:
                try:
                    row = srd.weapon(w)
                except srd.SRDLookupError:
                    continue
                if not row["ranged"]:
                    best = max(best, int(row.get("reach") or 5))
        return best

    def hp_band(self) -> str:
        if self.dead:
            return "dead"
        if self.hp <= 0:
            return "down"
        frac = self.hp / max(1, self.max_hp)
        if frac >= 0.85:
            return "healthy"
        if frac >= 0.5:
            return "wounded"
        if frac >= 0.25:
            return "bloodied"
        return "critical"

    def has_feature(self, fid: str) -> bool:
        return bool(self.sheet and fid in self.sheet.features)

    def trait(self, tid: str) -> dict | None:
        for t in (self.stat_block or {}).get("traits", []):
            if t.get("id") == tid:
                return t
        return None

    # ------------------------------------------------------------ serde
    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "side": self.side, "kind": self.kind,
            "sheet": self.sheet.to_dict() if self.sheet else None,
            "stat_block": self.stat_block,
            "hp": self.hp, "max_hp": self.max_hp, "temp_hp": self.temp_hp,
            "ac": self.ac, "speed": self.speed,
            "abilities": dict(self.abilities),
            "save_profs": list(self.save_profs), "skill_profs": list(self.skill_profs),
            "proficiency": self.proficiency,
            "position": list(self.position), "size": self.size,
            "conditions": [c.to_dict() for c in self.conditions],
            "concentration": copy.deepcopy(self.concentration),
            "death_saves": dict(self.death_saves),
            "stable": self.stable, "dead": self.dead,
            "resources": _jsonable(self.resources),
            "turn": dict(self.turn), "inventory": list(self.inventory),
            "flags": _jsonable(self.flags),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Combatant":
        res = d.get("resources", {}) or {}
        if "spell_slots" in res:
            res = dict(res)
            res["spell_slots"] = {int(k): int(v) for k, v in res["spell_slots"].items()}
        return cls(
            id=d["id"], name=d["name"], side=d["side"], kind=d["kind"],
            sheet=CharacterSheet.from_dict(d["sheet"]) if d.get("sheet") else None,
            stat_block=d.get("stat_block"),
            hp=int(d["hp"]), max_hp=int(d["max_hp"]), temp_hp=int(d.get("temp_hp", 0)),
            ac=int(d["ac"]), speed=int(d["speed"]),
            abilities={k: int(v) for k, v in d["abilities"].items()},
            save_profs=list(d.get("save_profs", [])),
            skill_profs=list(d.get("skill_profs", [])),
            proficiency=int(d.get("proficiency", 2)),
            position=tuple(d.get("position", (0, 0))),
            size=d.get("size", "M"),
            conditions=[Condition.from_dict(c) for c in d.get("conditions", [])],
            concentration=d.get("concentration"),
            death_saves=dict(d.get("death_saves", {"success": 0, "failure": 0})),
            stable=bool(d.get("stable", False)), dead=bool(d.get("dead", False)),
            resources=res, turn=dict(d.get("turn", {})),
            inventory=list(d.get("inventory", [])),
            flags=dict(d.get("flags", {})),
        )


def _jsonable(obj):
    """Convert int-keyed dicts (spell slots) to a JSON-safe shape, recursively."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    return obj


# ------------------------------------------------------------------ grid
@dataclass
class Grid:
    width: int = 20
    height: int = 20
    difficult: set[tuple[int, int]] = field(default_factory=set)
    walls: set[tuple[int, int]] = field(default_factory=set)
    cover: dict[tuple[int, int], str] = field(default_factory=dict)

    def in_bounds(self, p: tuple[int, int]) -> bool:
        return 0 <= p[0] < self.width and 0 <= p[1] < self.height

    def passable(self, p: tuple[int, int]) -> bool:
        return self.in_bounds(p) and tuple(p) not in self.walls

    @staticmethod
    def distance_ft(a: tuple[int, int], b: tuple[int, int]) -> int:
        """5e simplified diagonals: every diagonal counts as 5 ft."""
        return 5 * max(abs(int(a[0]) - int(b[0])), abs(int(a[1]) - int(b[1])))

    def cost_of(self, p: tuple[int, int]) -> int:
        return 10 if tuple(p) in self.difficult else 5

    def neighbors(self, p: tuple[int, int]) -> Iterable[tuple[int, int]]:
        x, y = p
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                q = (x + dx, y + dy)
                if self.passable(q):
                    yield q

    def occupied(self, state: "GameState", ignore: str | None = None) -> dict[tuple[int, int], str]:
        out: dict[tuple[int, int], str] = {}
        for cid, c in state.combatants.items():
            if cid == ignore or c.dead:
                continue
            out[tuple(c.position)] = cid
        return out

    def path(self, state: "GameState", start: tuple[int, int], goal: tuple[int, int],
             max_ft: int, mover_id: str | None = None,
             threat: dict[tuple[int, int], frozenset[str]] | None = None) -> list[tuple[int, int]] | None:
        """Cheapest path from start to goal within max_ft, or None.

        Uniform-cost search over 8-way movement; difficult terrain costs double.
        Squares occupied by other creatures are impassable (you may not end or
        pass through them in this simplified model).

        `threat` maps a square to the ids of enemies whose reach covers it. When
        it is given, cost is compared as `(feet, opportunity attacks provoked)`:
        the cheapest route in feet still wins, and only a tie between equally
        long routes is settled by which one hands out fewer free hits. Without
        it the tie went to whichever square sorted lowest, which is how a
        creature walked into a rogue's reach and back out of it for no reason.
        """
        start = tuple(start)
        goal = tuple(goal)
        if start == goal:
            return []
        if not self.passable(goal):
            return None
        blocked = set(self.occupied(state, ignore=mover_id))
        if goal in blocked:
            return None
        threat = threat or {}

        def provoked(a: tuple[int, int], b: tuple[int, int]) -> int:
            """Enemies whose reach covers `a` but not `b` — they get a swing."""
            return len(threat.get(a, frozenset()) - threat.get(b, frozenset()))

        # Lexicographic Dijkstra with a small frontier list (grids are tiny):
        # feet first, opportunity attacks as the tie-break.
        far = (1 << 30, 1 << 30)
        dist: dict[tuple[int, int], tuple[int, int]] = {start: (0, 0)}
        prev: dict[tuple[int, int], tuple[int, int]] = {}
        frontier: list[tuple[tuple[int, int], tuple[int, int]]] = [((0, 0), start)]
        while frontier:
            frontier.sort()
            cost, node = frontier.pop(0)
            if cost > dist.get(node, far):
                continue
            if node == goal:
                break
            for nxt in self.neighbors(node):
                if nxt in blocked:
                    continue
                nc = (cost[0] + self.cost_of(nxt), cost[1] + provoked(node, nxt))
                if nc[0] > max_ft:
                    continue
                if nc < dist.get(nxt, far):
                    dist[nxt] = nc
                    prev[nxt] = node
                    frontier.append((nc, nxt))
        if goal not in dist:
            return None
        out: list[tuple[int, int]] = []
        node = goal
        while node != start:
            out.append(node)
            node = prev[node]
        out.reverse()
        return out

    def path_cost(self, path: list[tuple[int, int]]) -> int:
        return sum(self.cost_of(p) for p in path)

    def line_squares(self, origin: tuple[int, int], toward: tuple[int, int],
                     length_ft: int) -> set[tuple[int, int]]:
        """Squares in a line of `length_ft` from origin toward a point."""
        ox, oy = origin
        tx, ty = toward
        dx, dy = tx - ox, ty - oy
        steps = length_ft // 5
        if dx == 0 and dy == 0:
            return set()
        norm = max(abs(dx), abs(dy))
        sx, sy = dx / norm, dy / norm
        out = set()
        for i in range(1, steps + 1):
            p = (ox + round(sx * i), oy + round(sy * i))
            if not self.in_bounds(p):
                break
            out.add(p)
            if p in self.walls:
                break
        return out

    def cone_squares(self, origin: tuple[int, int], toward: tuple[int, int],
                     size_ft: int) -> set[tuple[int, int]]:
        """Squares in a cone of `size_ft` from origin, pointed at `toward`.

        Simplified: a square is in the cone if it is within range and lies in
        the 90-degree wedge around the aim direction.
        """
        ox, oy = origin
        dx, dy = toward[0] - ox, toward[1] - oy
        if dx == 0 and dy == 0:
            dx = 1
        norm = max(abs(dx), abs(dy)) or 1
        ux, uy = dx / norm, dy / norm
        out = set()
        rng = size_ft // 5
        for x in range(ox - rng, ox + rng + 1):
            for y in range(oy - rng, oy + rng + 1):
                p = (x, y)
                if p == (ox, oy) or not self.in_bounds(p):
                    continue
                if self.distance_ft((ox, oy), p) > size_ft:
                    continue
                vx, vy = x - ox, y - oy
                vnorm = max(abs(vx), abs(vy)) or 1
                # dot product of unit-ish vectors; >= 0.5 keeps a ~90 degree wedge
                if (vx / vnorm) * ux + (vy / vnorm) * uy >= 0.5:
                    out.add(p)
        return out

    def sphere_squares(self, center: tuple[int, int], radius_ft: int) -> set[tuple[int, int]]:
        cx, cy = center
        r = radius_ft // 5
        out = set()
        for x in range(cx - r, cx + r + 1):
            for y in range(cy - r, cy + r + 1):
                p = (x, y)
                if self.in_bounds(p) and self.distance_ft(center, p) <= radius_ft:
                    out.add(p)
        return out

    def cube_squares(self, origin: tuple[int, int], size_ft: int) -> set[tuple[int, int]]:
        n = max(1, size_ft // 5)
        cx, cy = origin
        half = n // 2
        out = set()
        for x in range(cx - half, cx - half + n):
            for y in range(cy - half, cy - half + n):
                p = (x, y)
                if self.in_bounds(p):
                    out.add(p)
        return out

    def area_squares(self, shape: str, size_ft: int, origin: tuple[int, int],
                     point: tuple[int, int]) -> set[tuple[int, int]]:
        if shape == "sphere":
            return self.sphere_squares(point, size_ft)
        if shape == "cube":
            return self.cube_squares(point, size_ft)
        if shape == "cone":
            return self.cone_squares(origin, point, size_ft)
        if shape == "line":
            return self.line_squares(origin, point, size_ft)
        return {tuple(point)}

    def cover_between(self, attacker: tuple[int, int], target: tuple[int, int]) -> str | None:
        """Cover the target enjoys against an attack from `attacker`.

        Simplified: walk the straight line between the two squares; a wall grants
        three-quarters cover, a `cover` square grants what it declares.
        """
        best: str | None = None
        rank = {"half": 1, "three_quarters": 2}
        ax, ay = attacker
        tx, ty = target
        steps = max(abs(tx - ax), abs(ty - ay))
        if steps <= 1:
            return None
        for i in range(1, steps):
            x = ax + round((tx - ax) * i / steps)
            y = ay + round((ty - ay) * i / steps)
            p = (x, y)
            if p in (attacker, target):
                continue
            if p in self.walls:
                cand = "three_quarters"
            elif p in self.cover:
                cand = self.cover[p]
            else:
                continue
            if best is None or rank.get(cand, 0) > rank.get(best, 0):
                best = cand
        return best

    def has_line_of_sight(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        ax, ay = a
        bx, by = b
        steps = max(abs(bx - ax), abs(by - ay))
        if steps == 0:
            return True
        blocked = 0
        for i in range(1, steps):
            x = ax + round((bx - ax) * i / steps)
            y = ay + round((by - ay) * i / steps)
            if (x, y) in self.walls:
                blocked += 1
        # A single wall square gives cover, not total blockage; two blocks sight.
        return blocked < 2

    def to_dict(self) -> dict:
        return {
            "width": self.width, "height": self.height,
            "difficult": [list(p) for p in sorted(self.difficult)],
            "walls": [list(p) for p in sorted(self.walls)],
            "cover": {f"{p[0]},{p[1]}": v for p, v in sorted(self.cover.items())},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Grid":
        cover = {}
        for k, v in (d.get("cover") or {}).items():
            if isinstance(k, str):
                x, y = k.split(",")
                cover[(int(x), int(y))] = v
            else:
                cover[tuple(k)] = v
        return cls(
            width=int(d.get("width", 20)), height=int(d.get("height", 20)),
            difficult={tuple(p) for p in d.get("difficult", [])},
            walls={tuple(p) for p in d.get("walls", [])},
            cover=cover,
        )


# ------------------------------------------------------------ game state
@dataclass
class GameState:
    seed: int = 0
    rng: dict = field(default_factory=dict)
    mode: str = "exploration"        # "combat" | "exploration" | "social"
    round: int = 0
    turn_index: int = 0
    combatants: dict[str, Combatant] = field(default_factory=dict)
    initiative: list[tuple[str, int]] = field(default_factory=list)
    grid: Grid = field(default_factory=Grid)
    scene: dict = field(default_factory=dict)
    event_seq: int = 0

    def active_id(self) -> str | None:
        if self.mode != "combat" or not self.initiative:
            return None
        if not 0 <= self.turn_index < len(self.initiative):
            return None
        return self.initiative[self.turn_index][0]

    def active(self) -> Combatant | None:
        cid = self.active_id()
        return self.combatants.get(cid) if cid else None

    def get(self, cid: str) -> Combatant:
        try:
            return self.combatants[cid]
        except KeyError:
            raise KeyError(f"no such combatant: {cid!r}") from None

    def side_of(self, cid: str) -> str:
        return self.combatants[cid].side

    def allies_of(self, cid: str) -> list[Combatant]:
        side = self.side_of(cid)
        return [c for c in self.combatants.values() if c.side == side and c.id != cid]

    def enemies_of(self, cid: str) -> list[Combatant]:
        side = self.side_of(cid)
        return [c for c in self.combatants.values()
                if c.side != side and c.side != "neutral"]

    def living(self, side: str | None = None) -> list[Combatant]:
        return [c for c in self.combatants.values()
                if c.conscious and (side is None or c.side == side)]

    def distance(self, a: str, b: str) -> int:
        return Grid.distance_ft(self.combatants[a].position, self.combatants[b].position)

    def to_dict(self) -> dict:
        return {
            "seed": self.seed, "rng": copy.deepcopy(self.rng), "mode": self.mode,
            "round": self.round, "turn_index": self.turn_index,
            "combatants": {k: v.to_dict() for k, v in self.combatants.items()},
            "initiative": [[cid, score] for cid, score in self.initiative],
            "grid": self.grid.to_dict(),
            "scene": copy.deepcopy(self.scene),
            "event_seq": self.event_seq,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GameState":
        return cls(
            seed=int(d.get("seed", 0)), rng=dict(d.get("rng", {})),
            mode=d.get("mode", "exploration"),
            round=int(d.get("round", 0)), turn_index=int(d.get("turn_index", 0)),
            combatants={k: Combatant.from_dict(v) for k, v in (d.get("combatants") or {}).items()},
            initiative=[(str(x[0]), int(x[1])) for x in d.get("initiative", [])],
            grid=Grid.from_dict(d.get("grid", {})),
            scene=dict(d.get("scene", {})),
            event_seq=int(d.get("event_seq", 0)),
        )

    def copy(self) -> "GameState":
        return copy.deepcopy(self)
