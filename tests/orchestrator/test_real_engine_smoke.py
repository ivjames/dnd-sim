"""Integration smoke test against the real engine — skipped until it exists.

This is the seam between builder A and builder B: if this passes, the
orchestrator drives the real rules engine end to end on the mock LLM.
"""

import pytest

from llm.client import MockLLMClient
from orchestrator.bus import EventBus
from orchestrator.config import GameConfig
from orchestrator.game import Game, default_engine


@pytest.fixture
def real_engine():
    try:
        return default_engine()
    except ImportError as exc:
        pytest.skip(f"engine not built yet: {exc}")


def test_mock_game_on_the_real_engine(real_engine):
    cfg = GameConfig.load("examples/goblin_ambush.json")
    cfg.tempo_ms = 0
    cfg.mock = True
    cfg.max_rounds_per_combat = 8
    cfg.scenario["max_scenes"] = 1
    cfg.scenario["beats_per_scene"] = 1
    bus = EventBus()
    game = Game(cfg, MockLLMClient(seed=cfg.seed), bus, engine=real_engine)
    game.run()
    assert game.status == "finished", game.error
    kinds = {e.kind for e in bus.history()}
    assert {"combat_start", "attack", "combat_end"} <= kinds
    assert not [e for e in bus.history() if e.kind == "error"], [
        e.text for e in bus.history() if e.kind == "error"
    ]


def test_real_engine_run_is_deterministic(real_engine):
    def once():
        cfg = GameConfig.load("examples/goblin_ambush.json")
        cfg.tempo_ms = 0
        cfg.mock = True
        cfg.max_rounds_per_combat = 5
        cfg.scenario["max_scenes"] = 1
        cfg.scenario["beats_per_scene"] = 0
        bus = EventBus()
        Game(cfg, MockLLMClient(seed=cfg.seed), bus, engine=default_engine()).run()
        return [(e.kind, e.text) for e in bus.history()]

    assert once() == once()
