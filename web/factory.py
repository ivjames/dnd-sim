"""Default game factory: turns a config dict into a live (Game, EventBus) pair.

Imports of ``orchestrator``/``llm`` are deliberately lazy so the web layer can
be imported and unit-tested (with an injected fake factory) before those layers
exist, and so a missing ``anthropic`` install only hurts live mode.

See CONTRACTS.md Amendment 2026-09-03 (web): ``create_app(game_factory=...)``.
"""

from __future__ import annotations

import os
from typing import Any, Callable

DEFAULT_DM_MODEL = "claude-sonnet-5"
DEFAULT_PLAYER_MODEL = "claude-haiku-4-5-20251001"


def mock_mode(config: dict[str, Any] | None = None) -> bool:
    if os.environ.get("DND_SIM_MOCK", "").strip() in ("1", "true", "yes", "on"):
        return True
    if config and bool(config.get("mock")):
        return True
    return False


def apply_env_defaults(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    cfg.setdefault("dm_model", os.environ.get("DND_DM_MODEL", DEFAULT_DM_MODEL))
    cfg.setdefault("player_model", os.environ.get("DND_PLAYER_MODEL", DEFAULT_PLAYER_MODEL))
    cfg.setdefault(
        "summary_model",
        os.environ.get("DND_SUMMARY_MODEL", cfg.get("player_model") or DEFAULT_PLAYER_MODEL),
    )
    cfg["mock"] = mock_mode(cfg)
    return cfg


def _make_client(cfg: dict[str, Any]) -> Any:
    seed = int(cfg.get("seed") or 0)
    if cfg.get("mock"):
        from llm.client import MockLLMClient  # noqa: PLC0415

        try:
            return MockLLMClient(seed=seed)
        except TypeError:
            return MockLLMClient()
    from llm.client import AnthropicClient  # noqa: PLC0415

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Start with DND_SIM_MOCK=1 for mock mode."
        )
    try:
        return AnthropicClient(api_key=api_key)
    except TypeError:
        return AnthropicClient()


def default_game_factory(
    config: dict[str, Any], on_event: Callable[[Any], None]
) -> tuple[Any, Any]:
    """Build a Game + EventBus from a raw config dict. Does not start the game."""
    from orchestrator.bus import EventBus  # noqa: PLC0415
    from orchestrator.config import GameConfig  # noqa: PLC0415
    from orchestrator.game import Game  # noqa: PLC0415

    cfg_dict = apply_env_defaults(config)
    cfg = GameConfig.from_dict(cfg_dict)
    client = _make_client(cfg_dict)
    bus = EventBus()
    game = Game(cfg, client, bus, on_event=on_event)
    return game, bus
