"""In-process registry of running games.

The web layer holds the live ``Game`` objects; SQLite holds the durable record.
A restart empties the registry, which is why ``create_app`` marks any DB game
still marked running/paused/created as ``stopped`` at start-up.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from web.serialize import event_to_dict, to_jsonable

TERMINAL_STATUSES = {"finished", "stopped", "error", "budget_exceeded"}
SNAPSHOT_EVERY = 25


class GameEntry:
    """One live game: the Game object, its bus, and its persistence bookkeeping."""

    def __init__(self, game_id: str, db: Any, config: dict[str, Any], title: str) -> None:
        self.id = game_id
        self.db = db
        self.config = config
        self.title = title
        self.created_at = time.time()
        self.game: Any = None
        self.bus: Any = None
        self.last_seq = -1
        self.round = 0
        self.cost_usd = 0.0
        self._events_since_snapshot = 0
        self._last_status = "created"
        self._lock = threading.Lock()
        # Every snapshot write goes through this. `persist_snapshot` reads the
        # ledger total and writes it ABSOLUTELY, so two writers that overlap
        # can commit out of order and leave the older figure in the row —
        # spend that the next restart hands back. There are four writers: the
        # game thread every 25 events, the monitor on a status change, the
        # control routes, and a narration charge.
        self._persist_lock = threading.Lock()
        self._monitor: threading.Thread | None = None
        self._monitor_stop = threading.Event()

    # -- persistence ---------------------------------------------------------

    def on_event(self, ev: Any) -> None:
        """Passed to ``Game(on_event=...)``; called from the game thread."""
        d = event_to_dict(ev)
        with self._lock:
            self.last_seq = max(self.last_seq, d["seq"])
            if d.get("round"):
                self.round = d["round"]
            if d["kind"] == "cost":
                try:
                    self.cost_usd = float(
                        (d.get("data") or {}).get("total_usd", self.cost_usd)
                    )
                except (TypeError, ValueError):
                    pass
            self._events_since_snapshot += 1
            due = self._events_since_snapshot >= SNAPSHOT_EVERY
            if due:
                self._events_since_snapshot = 0
        try:
            self.db.add_event(self.id, d["seq"], d["kind"], d)
        except Exception:  # pragma: no cover - persistence must never kill the game
            pass
        if due:
            self.persist_snapshot()

    def status(self) -> str:
        game = self.game
        if game is None:
            return self._last_status
        return getattr(game, "status", self._last_status) or self._last_status

    def snapshot(self) -> dict[str, Any]:
        game = self.game
        if game is None:
            return {}
        try:
            return to_jsonable(game.snapshot()) or {}
        except Exception:  # pragma: no cover
            return {}

    def ledger(self) -> dict[str, Any]:
        snap = self.snapshot()
        led = snap.get("ledger") if isinstance(snap, dict) else None
        if isinstance(led, dict):
            return led
        game = self.game
        if game is not None and getattr(game, "ledger", None) is not None:
            return to_jsonable(game.ledger) or {}
        return {}

    def persist_snapshot(self) -> None:
        with self._persist_lock:
            self._persist_locked()

    def record_cost(self, charge: Callable[[], None]) -> None:
        """Apply `charge` to the live ledger and write it down, atomically
        against every other snapshot writer.

        The two have to be one step: persisting separately leaves a window in
        which another writer reads a ledger that does not yet know about this
        charge and then persists that total over it.
        """
        with self._persist_lock:
            charge()
            self._persist_locked()

    def _persist_locked(self) -> None:
        snap = self.snapshot()
        status = self.status()
        led = snap.get("ledger") if isinstance(snap, dict) else None
        cost = self.cost_usd
        if isinstance(led, dict):
            try:
                cost = float(led.get("total_usd", cost))
            except (TypeError, ValueError):
                pass
        self.cost_usd = cost
        try:
            self.db.save_snapshot(self.id, snap, status=status, cost_usd=cost)
        except Exception:  # pragma: no cover
            pass

    # -- status monitor ------------------------------------------------------

    def start_monitor(self, interval: float = 0.4) -> None:
        if self._monitor is not None:
            return

        def loop() -> None:
            while not self._monitor_stop.wait(interval):
                status = self.status()
                if status != self._last_status:
                    self._last_status = status
                    self.persist_snapshot()
                if status in TERMINAL_STATUSES:
                    self.persist_snapshot()
                    return

        self._monitor = threading.Thread(
            target=loop, name=f"dndsim-monitor-{self.id}", daemon=True
        )
        self._monitor.start()

    def shutdown(self) -> None:
        self._monitor_stop.set()


class GameRegistry:
    def __init__(self) -> None:
        self._games: dict[str, GameEntry] = {}
        self._lock = threading.Lock()

    def add(self, entry: GameEntry) -> None:
        with self._lock:
            self._games[entry.id] = entry

    def get(self, game_id: str) -> GameEntry | None:
        with self._lock:
            return self._games.get(game_id)

    def all(self) -> list[GameEntry]:
        with self._lock:
            return list(self._games.values())

    def running_count(self) -> int:
        return sum(1 for e in self.all() if e.status() in ("running", "paused", "created"))

    def shutdown(self) -> None:
        for entry in self.all():
            entry.shutdown()
            game = entry.game
            stop: Callable[[], None] | None = getattr(game, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:  # pragma: no cover
                    pass
