"""Headless runner: `python -m orchestrator.cli --config examples/goblin_ambush.json --mock`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from typing import Any

from agents.common import event_text
from llm.client import LLMError, MockLLMClient
from llm.router import RouterClient

from .bus import EventBus
from .config import GameConfig
from .game import Game

_PROSE_KINDS = {"narration", "dialogue", "scene", "dm_note"}

_EXIT_OK = {"finished"}


def _event_dict(ev: Any) -> dict:
    if is_dataclass(ev):
        d = asdict(ev)
    else:
        d = dict(getattr(ev, "__dict__", {}) or {})
    return {
        "seq": d.get("seq"),
        "round": d.get("round"),
        "kind": d.get("kind"),
        "actor": d.get("actor"),
        "text": d.get("text"),
        "data": _jsonable(d.get("data") or {}),
    }


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if is_dataclass(obj):
        return _jsonable(asdict(obj))
    return str(obj)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestrator.cli", description="Run a dnd-sim game headless.")
    p.add_argument("--config", required=True, help="path to a game config JSON")
    p.add_argument("--mock", action="store_true", help="use the deterministic mock LLM")
    p.add_argument("--live", action="store_true", help="use the real APIs (one provider per seat, by model id)")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--tempo", type=int, default=None, help="ms between events (0 = as fast as possible)")
    p.add_argument("--budget", type=float, default=None, help="USD budget for this run")
    p.add_argument("--json", action="store_true", help="emit one JSON object per event")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = GameConfig.load(args.config)
    if args.seed is not None:
        cfg.seed = args.seed
    if args.tempo is not None:
        cfg.tempo_ms = args.tempo
    if args.budget is not None:
        cfg.budget_usd = args.budget
    mock = args.mock or (not args.live and os.environ.get("DND_SIM_MOCK") == "1")
    cfg.mock = mock

    if mock:
        client: Any = MockLLMClient(seed=cfg.seed)
    else:
        client = RouterClient()
        try:
            client.preflight(cfg.seat_models())
        except LLMError as exc:
            print(f"error: {exc}", file=sys.stderr, flush=True)
            return 2
    bus = EventBus()

    def sink(ev: Any) -> None:
        d = _event_dict(ev)
        if args.json:
            print(json.dumps(d, ensure_ascii=False), flush=True)
            return
        kind = d["kind"] or ""
        text = event_text(d) or d["text"] or ""  # dialogue keeps its speaker
        if kind in _PROSE_KINDS:
            print(f"\n{text}\n", flush=True)
        else:
            print(f"  [{kind}] {text}", flush=True)

    game = Game(cfg, client, bus, on_event=sink)
    game.run()

    if not args.json:
        led = game.ledger.to_dict()
        print(
            f"\n--- {game.status} | round {game.snapshot()['round']} | "
            f"${led['total_usd']:.4f} over {led['calls']} calls ---",
            flush=True,
        )
    return 0 if game.status in _EXIT_OK else 1


if __name__ == "__main__":
    sys.exit(main())
