"""Deterministic dice for the engine.

Pure stdlib. Never touches the global `random` module state: every RNG owns its
own `random.Random` instance so that a seed fully determines the roll sequence.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Sequence

__all__ = ["RNG", "RollResult", "DiceError", "parse_expr", "average_of"]

_EXPR_RE = re.compile(
    r"^\s*(?P<sign0>[+-])?\s*(?P<body>.+?)\s*$",
)
_TERM_RE = re.compile(
    r"(?P<sign>[+-])?\s*(?:(?P<count>\d*)d(?P<faces>\d+)|(?P<flat>\d+))",
    re.IGNORECASE,
)


class DiceError(ValueError):
    """Raised for a malformed dice expression."""


@dataclass
class RollResult:
    expr: str
    rolls: list[int]
    kept: list[int]
    modifier: int
    total: int
    mode: str = "normal"
    natural: int | None = None

    def to_dict(self) -> dict:
        return {
            "expr": self.expr,
            "rolls": list(self.rolls),
            "kept": list(self.kept),
            "modifier": self.modifier,
            "total": self.total,
            "mode": self.mode,
            "natural": self.natural,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RollResult":
        return cls(
            expr=d["expr"],
            rolls=list(d.get("rolls", [])),
            kept=list(d.get("kept", [])),
            modifier=int(d.get("modifier", 0)),
            total=int(d["total"]),
            mode=d.get("mode", "normal"),
            natural=d.get("natural"),
        )

    def __str__(self) -> str:
        return f"{self.expr} → {self.total}"


def parse_expr(expr: str) -> list[tuple[int, int, int]]:
    """Parse "2d6+1d4+3" into [(sign, count, faces), ...] with faces==0 for flats."""
    if not isinstance(expr, str) or not expr.strip():
        raise DiceError(f"empty dice expression: {expr!r}")
    s = expr.replace(" ", "")
    terms: list[tuple[int, int, int]] = []
    pos = 0
    while pos < len(s):
        m = _TERM_RE.match(s, pos)
        if not m or m.end() == pos:
            raise DiceError(f"bad dice expression: {expr!r}")
        pos = m.end()
        sign = -1 if m.group("sign") == "-" else 1
        if m.group("faces") is not None:
            count = int(m.group("count") or 1)
            faces = int(m.group("faces"))
            if faces < 1 or count < 0 or count > 200:
                raise DiceError(f"bad dice term in {expr!r}")
            terms.append((sign, count, faces))
        else:
            terms.append((sign, int(m.group("flat")), 0))
    if not terms:
        raise DiceError(f"bad dice expression: {expr!r}")
    return terms


def average_of(expr: str) -> float:
    """Deterministic average value of an expression (no RNG needed)."""
    total = 0.0
    for sign, count, faces in parse_expr(expr):
        if faces == 0:
            total += sign * count
        else:
            total += sign * count * (faces + 1) / 2.0
    return total


def _doubled_expr(terms: list[tuple[int, int, int]]) -> str:
    """Render crit terms as they were actually rolled: "1d8+3" -> "2d8+3".

    The expression is what a reader checks the total against, so it has to name
    the dice that were thrown. Printing the undoubled expression beside a
    doubled total reads as a crit that forgot to double.
    """
    out = ""
    for sign, count, faces in terms:
        body = f"{count * 2}d{faces}" if faces else str(count)
        if not out:
            out = ("-" if sign < 0 else "") + body
        else:
            out += ("-" if sign < 0 else "+") + body
    return out or "0"


class RNG:
    """Seeded random source. Snapshot with `state()`, restore with `from_state()`."""

    def __init__(self, seed: int):
        self.seed = int(seed)
        self._r = random.Random(self.seed)
        self.count = 0  # number of raw die faces drawn; useful for debugging

    # ---- raw ----------------------------------------------------------
    def _die(self, faces: int) -> int:
        self.count += 1
        return self._r.randint(1, faces)

    def randint(self, a: int, b: int) -> int:
        self.count += 1
        return self._r.randint(a, b)

    def choice(self, seq: Sequence[Any]) -> Any:
        seq = list(seq)
        if not seq:
            raise DiceError("choice from empty sequence")
        self.count += 1
        return seq[self._r.randrange(len(seq))]

    def shuffle(self, seq: list) -> None:
        self.count += 1
        self._r.shuffle(seq)

    def random(self) -> float:
        self.count += 1
        return self._r.random()

    # ---- rolls --------------------------------------------------------
    def roll(self, expr: str) -> RollResult:
        terms = parse_expr(expr)
        rolls: list[int] = []
        modifier = 0
        total = 0
        for sign, count, faces in terms:
            if faces == 0:
                modifier += sign * count
                total += sign * count
                continue
            for _ in range(count):
                v = self._die(faces)
                rolls.append(v)
                total += sign * v
        return RollResult(
            expr=expr,
            rolls=rolls,
            kept=list(rolls),
            modifier=modifier,
            total=total,
            mode="normal",
            natural=None,
        )

    def roll_d20(self, mod: int = 0, mode: str = "normal") -> RollResult:
        if mode not in ("normal", "advantage", "disadvantage"):
            raise DiceError(f"unknown d20 mode {mode!r}")
        if mode == "normal":
            faces = [self._die(20)]
            kept = faces[0]
        else:
            a, b = self._die(20), self._die(20)
            faces = [a, b]
            kept = max(a, b) if mode == "advantage" else min(a, b)
        sign = "+" if mod >= 0 else "-"
        expr = f"1d20{sign}{abs(mod)}" if mod else "1d20"
        return RollResult(
            expr=expr,
            rolls=faces,
            kept=[kept],
            modifier=mod,
            total=kept + mod,
            mode=mode,
            natural=kept,
        )

    def roll_damage(self, expr: str, crit: bool = False) -> RollResult:
        """Roll damage; on a crit the dice (not the flat modifiers) are doubled."""
        if not crit:
            return self.roll(expr)
        terms = parse_expr(expr)
        rolls: list[int] = []
        modifier = 0
        total = 0
        for sign, count, faces in terms:
            if faces == 0:
                modifier += sign * count
                total += sign * count
                continue
            for _ in range(count * 2):
                v = self._die(faces)
                rolls.append(v)
                total += sign * v
        return RollResult(
            expr=f"{_doubled_expr(terms)} (crit)",
            rolls=rolls,
            kept=list(rolls),
            modifier=modifier,
            total=total,
            mode="normal",
            natural=None,
        )

    # ---- snapshot -----------------------------------------------------
    def state(self) -> dict:
        st = self._r.getstate()
        # getstate() -> (version, tuple_of_ints, gauss_next)
        return {
            "seed": self.seed,
            "count": self.count,
            "version": st[0],
            "keys": list(st[1]),
            "gauss": st[2],
        }

    @classmethod
    def from_state(cls, d: dict) -> "RNG":
        rng = cls(int(d.get("seed", 0)))
        rng.count = int(d.get("count", 0))
        rng._r.setstate((d["version"], tuple(int(x) for x in d["keys"]), d.get("gauss")))
        return rng

    def clone(self) -> "RNG":
        return RNG.from_state(self.state())
