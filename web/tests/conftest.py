"""Fakes so the web layer can be tested without engine/llm/orchestrator.

The fakes implement exactly the slice of CONTRACTS.md 2/4 that web touches:
``EventBus.subscribe/unsubscribe/publish/history``, ``Event`` fields, and
``Game.id/status/start/pause/resume/stop/hold/release/inject_dm_note/snapshot/ledger``.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from web.app import create_app  # noqa: E402


@dataclass
class FakeEvent:
    seq: int
    round: int
    kind: str
    actor: str | None
    text: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class FakeBus:
    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._history: list[FakeEvent] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, ev: Any) -> None:
        with self._lock:
            if ev is not None:
                self._history.append(ev)
            subs = list(self._subs)
        for q in subs:
            q.put(ev)

    def history(self) -> list[FakeEvent]:
        with self._lock:
            return list(self._history)


class FakeGame:
    """Emits a scripted script of events: two synchronously at start (so the
    SSE test always has something to replay), the rest from a thread."""

    SCRIPT = [
        ("scene", "The cart still smoulders."),
        ("combat_start", "Combat begins."),
        ("turn_start", "Thorin's turn."),
        ("attack", "Thorin attacks Goblin 2: 1d20+5 -> 17 vs AC 15, hit"),
        ("damage", "Goblin 2 takes 9 slashing (7 -> 0)"),
        ("narration", "The dwarf's axe finds the goblin's collarbone."),
        ("combat_end", "The party stands."),
    ]

    def __init__(self, config: dict, on_event: Callable[[Any], None], bus: FakeBus) -> None:
        self.id = "g_" + uuid.uuid4().hex[:10]
        self.config = config
        self.status = "created"
        self.bus = bus
        self.on_event = on_event
        self.ledger = {"total_usd": 0.0123, "by_role": {"dm": {"calls": 1, "in": 100, "out": 50, "usd": 0.0123}}}
        self.notes: list[str] = []
        self._seq = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self.held = 0.0
        self._hold_until = 0.0
        self.step_delay = float(os.environ.get("FAKE_STEP_DELAY", "0.02"))

    # -- helpers
    def _emit(self, kind: str, text: str) -> None:
        ev = FakeEvent(seq=self._seq, round=1, kind=kind, actor="pc_1", text=text, data={})
        self._seq += 1
        self.on_event(ev)
        self.bus.publish(ev)

    # -- Game interface
    def start(self) -> None:
        self.status = "running"
        for kind, text in self.SCRIPT[:2]:
            self._emit(kind, text)

        def run() -> None:
            for kind, text in self.SCRIPT[2:]:
                if self._stop.is_set():
                    break
                while self._paused.is_set() and not self._stop.is_set():
                    time.sleep(0.01)
                time.sleep(self.step_delay)
                self._emit(kind, text)
            if self.status == "running":
                self.status = "finished"

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def run(self) -> None:  # pragma: no cover - parity with contract
        self.start()
        if self._thread:
            self._thread.join()

    def pause(self) -> None:
        self._paused.set()
        self.status = "paused"

    def resume(self) -> None:
        self._paused.clear()
        self.status = "running"

    def stop(self) -> None:
        self._stop.set()
        self._paused.clear()
        self.release()
        self.status = "stopped"

    def hold(self, seconds: float = 0.0) -> float:
        secs = max(0.0, min(float(seconds), 30.0))
        self.held = secs
        self._hold_until = (time.monotonic() + secs) if secs else 0.0
        return secs

    def release(self) -> None:
        self.hold(0.0)

    def hold_remaining(self) -> float:
        return max(0.0, self._hold_until - time.monotonic())

    def inject_dm_note(self, text: str) -> None:
        self.notes.append(text)
        self._emit("dm_note", text)

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "holding": self.hold_remaining() > 0,
            "round": 1,
            "summary": "The party ambushed on the trail.",
            "ledger": self.ledger,
            "state": {
                "round": 1,
                "turn_index": 0,
                "mode": "combat",
                "initiative": [["pc_1", 19], ["mon_1", 12]],
                "grid": {"width": 6, "height": 5, "difficult": [[2, 2]], "walls": [], "cover": {}},
                "combatants": {
                    "pc_1": {
                        "id": "pc_1", "name": "Thorin", "side": "party", "kind": "pc",
                        "hp": 24, "max_hp": 30, "temp_hp": 0, "ac": 18,
                        "position": [1, 2], "conditions": [], "dead": False,
                        "resources": {"spell_slots": {}, "second_wind": 1},
                        "death_saves": {"success": 0, "failure": 0},
                    },
                    "mon_1": {
                        "id": "mon_1", "name": "Goblin 2", "side": "enemy", "kind": "monster",
                        "hp": 0, "max_hp": 7, "temp_hp": 0, "ac": 15,
                        "position": [4, 2], "conditions": [{"name": "prone", "duration": None}],
                        "dead": True, "resources": {},
                        "death_saves": {"success": 0, "failure": 0},
                    },
                },
            },
        }


def fake_factory(config: dict, on_event: Callable[[Any], None]):
    bus = FakeBus()
    return FakeGame(config, on_event, bus), bus


@pytest.fixture()
def db_file(tmp_path):
    return str(tmp_path / "test.sqlite3")


@pytest.fixture()
def app(db_file):
    app = create_app(game_factory=fake_factory, db_path=db_file)
    app.config["TESTING"] = True
    yield app
    app.config["DND_REGISTRY"].shutdown()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def sample_config():
    return {
        "seed": 7,
        "setting": "A damp pine forest.",
        "tone": "grim",
        "budget_usd": 0.5,
        "tempo_ms": 0,
        "party": [{"id": "pc_1", "name": "Thorin", "race": "Dwarf (Hill)", "klass": "Fighter", "level": 3}],
        "scenario": {"opening": "Ambush.", "encounters": [], "max_scenes": 1},
    }
