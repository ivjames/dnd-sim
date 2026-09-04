"""SQLite persistence for dnd-sim (CONTRACTS.md 5).

Schema:
    games(id TEXT PK, created_at, config_json, status, title, cost_usd, snapshot_json)
    events(game_id, seq, kind, json, PRIMARY KEY(game_id, seq))

One connection per call (short-lived); SQLite handles the locking. WAL mode so
the SSE readers never block the writer thread.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Iterable

DEFAULT_DB_PATH = os.path.join("data", "dndsim.sqlite3")

#: statuses that cannot survive a process restart
LIVE_STATUSES = ("running", "paused", "created")

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id            TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    config_json   TEXT NOT NULL,
    status        TEXT NOT NULL,
    title         TEXT,
    cost_usd      REAL NOT NULL DEFAULT 0.0,
    snapshot_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    game_id TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    json    TEXT NOT NULL,
    PRIMARY KEY (game_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_game_seq ON events(game_id, seq);
"""


def db_path_from_env() -> str:
    return os.environ.get("DND_SIM_DB", DEFAULT_DB_PATH)


class Database:
    """Thin SQLite wrapper. Safe to share across threads (no shared connection)."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or db_path_from_env()
        if self.path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
        # ":memory:" would be a fresh database per connection, which breaks the
        # one-connection-per-call model; give tests a shared in-memory file.
        self._shared_conn: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._shared_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._shared_conn.row_factory = sqlite3.Row
        self.init()

    # -- connection handling -------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self._shared_conn is not None:
            return self._shared_conn
        conn = sqlite3.connect(self.path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _close(self, conn: sqlite3.Connection) -> None:
        if conn is not self._shared_conn:
            conn.close()

    def init(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            self._close(conn)

    # -- games ---------------------------------------------------------------

    def create_game(
        self,
        game_id: str,
        config: dict[str, Any],
        title: str = "",
        status: str = "created",
        created_at: float | None = None,
    ) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO games (id, created_at, config_json, status, title,"
                " cost_usd, snapshot_json) VALUES (?,?,?,?,?,?,?)",
                (
                    game_id,
                    created_at if created_at is not None else time.time(),
                    json.dumps(config),
                    status,
                    title,
                    0.0,
                    None,
                ),
            )
            conn.commit()
        finally:
            self._close(conn)

    def set_status(self, game_id: str, status: str) -> None:
        conn = self.connect()
        try:
            conn.execute("UPDATE games SET status=? WHERE id=?", (status, game_id))
            conn.commit()
        finally:
            self._close(conn)

    def set_cost(self, game_id: str, cost_usd: float) -> None:
        conn = self.connect()
        try:
            conn.execute("UPDATE games SET cost_usd=? WHERE id=?", (float(cost_usd), game_id))
            conn.commit()
        finally:
            self._close(conn)

    def add_cost(self, game_id: str, usd: float) -> None:
        """Add to a game's running total, atomically.

        `set_cost` writes what the caller worked out; this adds to what is
        there. Spoken narration is charged from Flask request threads, where a
        read-then-write would lose one spectator's clip against another's.
        Only for games no process is running: a live game's total comes from
        its `Ledger` on the next snapshot and would overwrite this.
        """
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE games SET cost_usd = COALESCE(cost_usd, 0) + ? WHERE id=?",
                (float(usd), game_id),
            )
            conn.commit()
        finally:
            self._close(conn)

    def save_snapshot(
        self, game_id: str, snapshot: dict[str, Any], status: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        conn = self.connect()
        try:
            fields = ["snapshot_json=?"]
            args: list[Any] = [json.dumps(snapshot)]
            if status is not None:
                fields.append("status=?")
                args.append(status)
            if cost_usd is not None:
                fields.append("cost_usd=?")
                args.append(float(cost_usd))
            args.append(game_id)
            conn.execute("UPDATE games SET " + ", ".join(fields) + " WHERE id=?", args)
            conn.commit()
        finally:
            self._close(conn)

    def get_game(self, game_id: str) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        finally:
            self._close(conn)
        return _game_row(row) if row else None

    def list_games(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM games ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            self._close(conn)
        return [_game_row(r) for r in rows]

    def mark_stale_games_stopped(self) -> int:
        """On process start, no in-process Game objects exist: anything the DB
        still believes is alive was killed by the restart."""
        conn = self.connect()
        try:
            qs = ",".join("?" for _ in LIVE_STATUSES)
            cur = conn.execute(
                f"UPDATE games SET status='stopped' WHERE status IN ({qs})", LIVE_STATUSES
            )
            conn.commit()
            return cur.rowcount or 0
        finally:
            self._close(conn)

    # -- events --------------------------------------------------------------

    def add_event(self, game_id: str, seq: int, kind: str, payload: dict[str, Any]) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO events (game_id, seq, kind, json) VALUES (?,?,?,?)",
                (game_id, int(seq), kind, json.dumps(payload)),
            )
            conn.commit()
        finally:
            self._close(conn)

    def add_events(self, game_id: str, items: Iterable[tuple[int, str, dict[str, Any]]]) -> None:
        conn = self.connect()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO events (game_id, seq, kind, json) VALUES (?,?,?,?)",
                [(game_id, int(s), k, json.dumps(p)) for s, k, p in items],
            )
            conn.commit()
        finally:
            self._close(conn)

    def events_after(self, game_id: str, after: int = -1, limit: int = 5000) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT seq, kind, json FROM events WHERE game_id=? AND seq>? ORDER BY seq LIMIT ?",
                (game_id, int(after), int(limit)),
            ).fetchall()
        finally:
            self._close(conn)
        return [json.loads(r["json"]) for r in rows]

    def event_count(self, game_id: str) -> int:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE game_id=?", (game_id,)
            ).fetchone()
        finally:
            self._close(conn)
        return int(row["n"]) if row else 0


def _game_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "config": json.loads(row["config_json"]) if row["config_json"] else {},
        "status": row["status"],
        "title": row["title"] or "",
        "cost_usd": row["cost_usd"] or 0.0,
        "snapshot": json.loads(row["snapshot_json"]) if row["snapshot_json"] else None,
    }
