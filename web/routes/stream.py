"""SSE stream: history replay then live events (CONTRACTS.md 5).

Wire format per message::

    id: <seq>
    event: <kind>
    data: <Event JSON>

Heartbeat comments (``: hb``) go out every 15s so proxies and iPad Safari keep
the connection open. When the game reaches a terminal status the stream emits
``event: end`` and closes.
"""

from __future__ import annotations

import queue
import time
from typing import Any, Iterator

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from web.registry import TERMINAL_STATUSES
from web.serialize import dumps, event_to_dict

bp = Blueprint("stream", __name__, url_prefix="/api")

HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 1.0
#: hard cap so a forgotten tab cannot hold a worker forever (client reconnects)
MAX_STREAM_SECONDS = 60 * 60 * 6


def _sse(kind: str, payload: str, seq: int | None = None) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {kind}\ndata: {payload}\n\n"


def _after_from_request() -> int:
    """Resume point: max(?after=, Last-Event-ID).

    EventSource auto-reconnects to the *original* URL (stale ``?after=``) but
    sends the freshest seq it saw in ``Last-Event-ID``; taking the max means a
    reconnect resumes rather than replaying the whole game.
    """
    best = -1
    for raw in (request.args.get("after"), request.headers.get("Last-Event-ID")):
        try:
            best = max(best, int(raw))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return best


def _replay(db: Any, bus: Any, game_id: str, after: int) -> list[dict[str, Any]]:
    """Union of persisted events and the bus's in-memory history.

    The game thread writes to SQLite and publishes to the bus in an order we do
    not control, so a subscriber that reads only one source can miss an event
    straddling the subscribe. Merging both closes that window.
    """
    merged: dict[int, dict[str, Any]] = {}
    try:
        for ev in db.events_after(game_id, after):
            d = event_to_dict(ev)
            merged[d["seq"]] = d
    except Exception:  # pragma: no cover
        pass
    if bus is not None and hasattr(bus, "history"):
        try:
            for ev in bus.history():
                d = event_to_dict(ev)
                if d["seq"] > after:
                    merged[d["seq"]] = d
        except Exception:  # pragma: no cover
            pass
    return [merged[k] for k in sorted(merged)]


@bp.get("/games/<game_id>/stream")
def stream(game_id: str):
    registry = current_app.config["DND_REGISTRY"]
    db = current_app.config["DND_DB"]
    entry = registry.get(game_id)
    row = db.get_game(game_id)
    if entry is None and row is None:
        return jsonify({"error": "no such game"}), 404

    after = _after_from_request()
    bus = entry.bus if entry is not None else None
    sub: Any = None
    if bus is not None and hasattr(bus, "subscribe"):
        # Subscribe BEFORE replaying so nothing published mid-replay is lost.
        sub = bus.subscribe()

    def generate() -> Iterator[str]:
        last_seq = after
        started = time.time()
        last_beat = started
        try:
            yield "retry: 3000\n\n"
            for d in _replay(db, bus, game_id, after):
                if d["seq"] <= last_seq:
                    continue
                last_seq = d["seq"]
                yield _sse(d["kind"], dumps(d), d["seq"])

            if sub is None:
                status = entry.status() if entry else (row or {}).get("status", "stopped")
                yield _sse("end", dumps({"game_id": game_id, "status": status, "seq": last_seq}))
                return

            while True:
                if time.time() - started > MAX_STREAM_SECONDS:
                    yield _sse(
                        "end",
                        dumps({"game_id": game_id, "status": entry.status(), "seq": last_seq,
                               "reason": "stream_timeout"}),
                    )
                    return
                try:
                    ev = sub.get(timeout=POLL_SECONDS)
                except queue.Empty:
                    now = time.time()
                    if now - last_beat >= HEARTBEAT_SECONDS:
                        last_beat = now
                        yield ": hb\n\n"
                    if entry.status() in TERMINAL_STATUSES:
                        # drain anything still queued, then finish
                        for d in _replay(db, bus, game_id, last_seq):
                            last_seq = d["seq"]
                            yield _sse(d["kind"], dumps(d), d["seq"])
                        yield _sse(
                            "end",
                            dumps({"game_id": game_id, "status": entry.status(), "seq": last_seq}),
                        )
                        return
                    continue
                if ev is None:  # bus closed
                    yield _sse(
                        "end",
                        dumps({"game_id": game_id, "status": entry.status(), "seq": last_seq}),
                    )
                    return
                d = event_to_dict(ev)
                if d["seq"] <= last_seq:
                    continue
                last_seq = d["seq"]
                yield _sse(d["kind"], dumps(d), d["seq"])
        finally:
            if sub is not None and bus is not None and hasattr(bus, "unsubscribe"):
                try:
                    bus.unsubscribe(sub)
                except Exception:  # pragma: no cover
                    pass

    resp = Response(stream_with_context(generate()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
    return resp
