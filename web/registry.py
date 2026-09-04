"""In-process registry of running games.

The web layer holds the live ``Game`` objects; SQLite holds the durable record.
A restart empties the registry, which is why ``create_app`` marks any DB game
still marked running/paused/created as ``stopped`` at start-up.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from web.serialize import event_to_dict, to_jsonable

TERMINAL_STATUSES = {"finished", "stopped", "error", "budget_exceeded"}
SNAPSHOT_EVERY = 25

#: Board snapshots kept per live game, so the spectator page can ask for the
#: map and the hit points *as of* the line it has just put on screen rather
#: than as of now. The page reveals the transcript at the pace of the spoken
#: narration and the game runs ahead of it into a queue; without this the one
#: thing that queue cannot delay is the board, which would go on showing where
#: everyone ended up while the voice is still describing how they got there.
#: A small combat's state serializes to ~25 KB, so this is a few MB per live
#: game — a ceiling, not a target, and old entries are dropped.
BOARD_HISTORY = 128

#: State that churns on nearly every event without being anything the page
#: draws. Excluded from the key that decides whether a board is worth keeping,
#: so a paragraph or a die roll does not archive a board identical to the one
#: before it. Anything drawn that is *not* excluded here is compared, so the
#: only way this list can be wrong is by making the board a change late —
#: never by letting it run ahead, which is the direction that matters.
BOARD_NOISE = ("rng", "event_seq")
COMBATANT_NOISE = ("turn", "flags")


def _board_key(board: dict[str, Any]) -> str:
    """What the page would draw from this board, as one comparable string.

    Everything the spectator page reads off a state is in here; the dice
    generator, the running event count and a combatant's per-turn bookkeeping
    and flags are not, because they change on nearly every event and are drawn
    nowhere. Two boards with the same key are the same picture.
    """
    state = board.get("state")
    if not isinstance(state, dict):
        return json.dumps([board.get("round"), state], sort_keys=True, default=str)
    drawn = {k: v for k, v in state.items() if k not in BOARD_NOISE}
    combatants = drawn.get("combatants")
    if isinstance(combatants, dict):
        drawn["combatants"] = {
            cid: ({k: v for k, v in c.items() if k not in COMBATANT_NOISE}
                  if isinstance(c, dict) else c)
            for cid, c in combatants.items()
        }
    return json.dumps([board.get("round"), drawn], sort_keys=True, default=str)


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
        self._boards: dict[int, dict[str, Any]] = {}   # seq -> board, oldest first
        self._board_key_last = ""                     # ... and what the newest one draws as
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
        self._record_board(d["seq"])
        try:
            self.db.add_event(self.id, d["seq"], d["kind"], d)
        except Exception:  # pragma: no cover - persistence must never kill the game
            pass
        if due:
            self.persist_snapshot()

    def _record_board(self, seq: int) -> None:
        """Keep the board as it stood when this event was published.

        Called on the game thread, from ``on_event``, so what it reads is the
        state that event was emitted against. Only the board is kept — never
        the ledger, the status or the hold — because money, whether the game
        is still running and whether it is being held are live facts about the
        table rather than things anyone narrates.

        Two filters, and both are about not archiving a board **ahead** of the
        event it is filed under:

        * ``Game.board_settled()``. An engine action resolves in full and
          hands back a list of events, published one at a time, so between
          them the state is already the state after the last of them. Filed
          per event, the hit points would drop on the attack roll and the
          narration would explain it afterwards. Skipping the unsettled ones
          leaves the previous settled board answering for them, which is a
          fraction of a second stale and never early. A game that does not
          offer the method (a fake, another implementation) is taken at its
          word that every event settles.
        * The board is unchanged. A paragraph, a die roll or a cost line moves
          nothing, so it keeps no board of its own and the one before it goes
          on answering. This is what stops prose — most of a transcript — from
          filling the archive.
        """
        game = self.game
        if game is None:
            return
        settled = getattr(game, "board_settled", None)
        if callable(settled):
            try:
                if not settled():
                    return
            except Exception:  # pragma: no cover - defensive
                pass
        try:
            snap = game.snapshot() or {}
            board = {
                "state": to_jsonable(snap.get("state")),
                "round": int(snap.get("round") or 0),
            }
            key = _board_key(board)
        except Exception:  # pragma: no cover - a board must never kill the game
            return
        with self._lock:
            if key == self._board_key_last:
                return
            self._board_key_last = key
            self._boards[seq] = board
            # Seqs are monotonic, so insertion order is seq order and the
            # oldest is always the first key.
            while len(self._boards) > BOARD_HISTORY:
                del self._boards[next(iter(self._boards))]

    def board_at(self, seq: int) -> dict[str, Any] | None:
        """The board as of ``seq``: the newest one archived at or before it.

        Asked for something older than anything kept — a listener minutes
        behind the game — it answers with the oldest kept, which is the least
        far ahead this can be; the reply carries the seq it actually is, so
        the caller is never told a state is older than it is. ``None`` when
        nothing has been archived at all.
        """
        with self._lock:
            if not self._boards:
                return None
            best = None
            for s in self._boards:
                if s <= seq:
                    best = s
                else:
                    break
            if best is None:
                best = next(iter(self._boards))
            board = self._boards[best]
            return {"seq": best, "state": board["state"], "round": board["round"]}

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
