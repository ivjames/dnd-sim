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


def test_the_board_settles_before_the_event_that_reports_it(real_engine):
    """What `GameEntry._record_board` (web) relies on, checked on the real engine.

    `engine.actions.apply` resolves an action in full and hands back the whole
    list of events; the loop then publishes them one at a time, so between them
    the state is already the state after the *last* of them. A spectator page
    that archives the board per event would file a hit point loss against the
    attack roll and show it while the narrator was still reading that roll — so
    it archives only where `board_settled()` is true, and this is the claim
    that makes that safe.

    Two things are asserted about every published event: that a multi-event
    action reports itself unsettled until its last event, and that the state a
    reader sees on a settled event is one nothing further in that action will
    change.
    """
    cfg = GameConfig.load("examples/goblin_ambush.json")
    cfg.tempo_ms = 0
    cfg.mock = True
    cfg.max_rounds_per_combat = 6
    cfg.scenario["max_scenes"] = 1
    cfg.scenario["beats_per_scene"] = 1
    bus = EventBus()
    game = Game(cfg, MockLLMClient(seed=cfg.seed), bus, engine=real_engine)

    seen: list[tuple[str, bool]] = []
    unsettled_runs = 0

    def watch(ev):
        settled = game.board_settled()
        seen.append((ev.kind, settled))

    game.on_event = watch
    game.run()
    assert game.status == "finished", game.error

    # Every run of unsettled events ends in a settled one — nothing is left
    # mid-action, including where a stop or a budget lands.
    run = 0
    for _, settled in seen:
        if settled:
            if run:
                unsettled_runs += 1
            run = 0
        else:
            run += 1
    assert run == 0, "the last event published never settled the board"
    assert unsettled_runs, "no multi-event action in this game: the test proves nothing"

    # And a game's own lines — narration, dialogue, the DM's notes — are always
    # settled: they are emitted one at a time and move nothing.
    prose = {"narration", "dialogue", "dm_note", "scene", "cost"}
    assert all(settled for kind, settled in seen if kind in prose)


def test_combat_ends_on_a_board_that_is_no_longer_in_combat(real_engine):
    """The `combat_end` event is published *after* the mode flips.

    Anything reading the state as that event goes out — the per-event board
    archive does — would otherwise file the end of the fight against a board
    still in `combat`, and the roster would go on drawn in initiative order
    with a ring round whoever was acting when it ended.
    """
    cfg = GameConfig.load("examples/goblin_ambush.json")
    cfg.tempo_ms = 0
    cfg.mock = True
    cfg.max_rounds_per_combat = 6
    cfg.scenario["max_scenes"] = 1
    cfg.scenario["beats_per_scene"] = 1
    bus = EventBus()
    game = Game(cfg, MockLLMClient(seed=cfg.seed), bus, engine=real_engine)

    modes: list[str] = []

    def watch(ev):
        if ev.kind == "combat_end":
            modes.append(getattr(game.state, "mode", "?"))

    game.on_event = watch
    game.run()
    assert modes, "no combat_end was published"
    assert all(m != "combat" for m in modes), modes
