"""A scripted full combat on the goblin_ambush party, plus determinism.

Seeded RNG picks random legal actions through legal_actions -> apply ->
advance_turn until combat_over. Per step: event seq monotonic, turn budgets
>= 0, HP within [0, max], initiative unchanged, to_dict/from_dict round-trips.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from engine import actions as A
from engine.characters import build_character, monster_to_combatant, pc_to_combatant
from engine.dice import RNG
from engine.state import GameState, Grid

ROOT = Path(__file__).resolve().parents[2]


def build_ambush(seed: int) -> tuple[GameState, RNG]:
    cfg = json.loads((ROOT / "examples" / "goblin_ambush.json").read_text())
    rng = RNG(seed)
    enc = cfg["scenario"]["encounters"][0]
    g = enc["grid"]
    combatants = {}
    for i, spec in enumerate(cfg["party"]):
        sheet = build_character(spec, rng)
        combatants[sheet.id] = pc_to_combatant(sheet, position=tuple(g["party_start"][i]))
    k = 0
    for entry in enc["monsters"]:
        for ordinal in range(1, entry["count"] + 1):
            k += 1
            mon = monster_to_combatant(entry["name"], f"mon_{k}", rng)
            mon.position = tuple(g["enemy_start"][k - 1])
            if entry["count"] > 1:
                mon.name = f"{mon.name} {ordinal}"
            combatants[mon.id] = mon
    grid = Grid(width=g["width"], height=g["height"],
                difficult={tuple(p) for p in g["difficult"]}, walls={tuple(p) for p in g["walls"]})
    st = GameState(seed=seed, rng=rng.state(), mode="combat", combatants=combatants, grid=grid,
                   scene={"title": cfg["title"]})
    return st, rng


def run_combat(seed: int, max_rounds: int = 40) -> tuple[GameState, list[str]]:
    st, rng = build_ambush(seed)
    assert len([c for c in st.combatants.values() if c.kind == "pc"]) == 4
    assert len([c for c in st.combatants.values() if c.kind == "monster"]) == 5
    st, events = A.start_combat(st, rng.state())
    texts = [e.text for e in events]
    picker = random.Random(seed)
    initiative = list(st.initiative)
    last_seq = 0
    max_hp = {cid: c.max_hp for cid, c in st.combatants.items()}

    def check(state: GameState, evs) -> None:
        nonlocal last_seq
        for e in evs:
            assert e.seq > last_seq, (e.seq, last_seq)
            last_seq = e.seq
        assert state.initiative == initiative
        for cid, c in state.combatants.items():
            assert 0 <= c.hp <= max(c.max_hp, max_hp[cid]), (c.name, c.hp, c.max_hp)
            for key in ("movement_left", "attacks_left"):
                assert int(c.turn.get(key, 0)) >= 0, (c.name, c.turn)
        d = json.loads(json.dumps(state.to_dict()))
        assert GameState.from_dict(d).to_dict() == d

    check(st, events)
    while A.combat_over(st) is None and st.round <= max_rounds:
        actor = st.active_id()
        assert actor is not None
        for _ in range(12):
            tpls = A.legal_actions(st, actor)
            assert tpls and tpls[-1].type == "end_turn"
            assert len({t.id for t in tpls}) == len(tpls)
            t = picker.choice(tpls)
            params: dict = {}
            if "path" in t.needs:
                params["path"] = [picker.choice(t.params["suggested"])]
            if "point" in t.needs:
                params["point"] = picker.choice(t.params["suggested"])
            if "targets" in t.needs:
                params["targets"] = []
            st, evs = A.apply(st, A.Action(actor=actor, template_id=t.id, params=params))
            texts.extend(e.text for e in evs)
            check(st, evs)
            if t.type == "end_turn":
                break
        st, evs = A.advance_turn(st)
        texts.extend(e.text for e in evs)
        check(st, evs)
    return st, texts


@pytest.mark.parametrize("seed", [42, 7, 1234])
def test_scripted_full_combat_runs_to_a_result(seed):
    st, texts = run_combat(seed)
    winner = A.combat_over(st)
    assert winner in ("party", "enemy"), f"no result after {st.round} rounds"
    assert any("attacks" in t for t in texts)
    assert any("dies" in t for t in texts)
    for c in st.combatants.values():
        if c.side == ("enemy" if winner == "party" else "party"):
            assert c.dead or c.hp <= 0


def test_same_seed_gives_identical_event_text():
    a = run_combat(42)[1]
    b = run_combat(42)[1]
    assert a == b and len(a) > 50


def test_different_seed_diverges():
    assert run_combat(42)[1] != run_combat(43)[1]
