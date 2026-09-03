"""Shared builders for the engine tests.

`ScriptedRNG` lets a test dictate d20 faces (attack rolls, saves, death saves)
while everything else stays seeded, so each rule can be pinned to a known
outcome without hunting for a lucky seed.
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import actions as A  # noqa: E402
from engine.characters import build_character, monster_to_combatant, pc_to_combatant  # noqa: E402
from engine.dice import RNG  # noqa: E402
from engine.state import Combatant, GameState, Grid  # noqa: E402


class ScriptedRNG(RNG):
    """RNG whose d20 faces come from a queue until it runs dry."""

    def __init__(self, seed: int = 1, d20: list[int] | None = None):
        super().__init__(seed)
        self.d20 = deque(d20 or [])

    def _die(self, faces: int) -> int:
        if faces == 20 and self.d20:
            self.count += 1
            return self.d20.popleft()
        return super()._die(faces)


@pytest.fixture
def script(monkeypatch):
    """Return a function that scripts the next d20 faces the engine will see."""

    def _script(*faces: int, seed: int = 1) -> ScriptedRNG:
        rng = ScriptedRNG(seed, list(faces))
        monkeypatch.setattr(A, "_rng_of", lambda state: rng)
        return rng

    return _script


def make_pc(cid: str, klass: str = "Fighter", *, name: str | None = None, race: str = "Human", level: int = 3,
            abilities: dict | None = None, pos=(0, 0), spells=None, equipment="default", side: str = "party") -> Combatant:
    spec = {
        "id": cid, "name": name or f"{klass} {cid}", "race": race, "klass": klass, "level": level,
        "abilities": abilities or {"STR": 16, "DEX": 14, "CON": 14, "INT": 16, "WIS": 16, "CHA": 10},
        "equipment": equipment, "persona": "",
    }
    if spells is not None:
        spec["spells"] = spells
    sheet = build_character(spec, RNG(1))
    c = pc_to_combatant(sheet, position=pos)
    c.side = side
    return c


def make_mon(name: str, cid: str, pos=(1, 0), side: str = "enemy", label: str | None = None) -> Combatant:
    c = monster_to_combatant(name, cid, RNG(1))
    c.position = tuple(pos)
    c.side = side
    if label:
        c.name = label
    return c


def make_state(*combatants: Combatant, order: list[str] | None = None, seed: int = 1,
               grid: Grid | None = None, start: bool = True) -> GameState:
    """A combat state with a fixed initiative order (first id acts first)."""
    st = GameState(seed=seed, rng=RNG(seed).state(), mode="combat" if start else "exploration",
                   combatants={c.id: c for c in combatants}, grid=grid or Grid(width=20, height=20), scene={})
    if start:
        ids = order or [c.id for c in combatants]
        st.initiative = [(cid, 20 - i) for i, cid in enumerate(ids)]
        st.round = 1
        st.turn_index = 0
        for c in st.combatants.values():
            A._reset_turn(c)
    return st


def templates(st: GameState, cid: str, type_: str | None = None, contains: str | None = None) -> list[A.ActionTemplate]:
    out = A.legal_actions(st, cid)
    if type_:
        out = [t for t in out if t.type == type_]
    if contains:
        out = [t for t in out if contains.lower() in t.label.lower()]
    return out


def do(st: GameState, cid: str, tpl: A.ActionTemplate, **params):
    return A.apply(st, A.Action(actor=cid, template_id=tpl.id, params=params))


def attack(st: GameState, cid: str, target_name: str, weapon: str | None = None, **params):
    tpls = templates(st, cid, "attack", target_name)
    if weapon:
        tpls = [t for t in tpls if weapon.lower() in t.label.lower()]
    assert tpls, f"no attack template on {target_name} for {cid}: {[t.label for t in A.legal_actions(st, cid)]}"
    return do(st, cid, tpls[0], **params)


def cast(st: GameState, cid: str, spell: str, target_name: str | None = None, slot: int | None = None, **params):
    tpls = [t for t in templates(st, cid, "cast") if t.params.get("spell") == spell]
    if slot is not None:
        tpls = [t for t in tpls if t.params.get("slot") == slot]
    if target_name:
        tpls = [t for t in tpls if target_name.lower() in t.label.lower()]
    assert tpls, f"no cast template for {spell} ({target_name}, L{slot}): {[t.label for t in A.legal_actions(st, cid)]}"
    return do(st, cid, tpls[0], **params)


def kinds(events) -> list[str]:
    return [e.kind for e in events]


def find(events, kind: str, contains: str | None = None):
    for e in events:
        if e.kind == kind and (contains is None or contains.lower() in e.text.lower()):
            return e
    return None
