"""JSON API routes (CONTRACTS.md 5)."""

from __future__ import annotations

import math
import uuid
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from web.auth import (
    HEADER as WRITE_HEADER,
    authenticated,
    require_write,
    writes_configured,
)
from web.presets import load_presets, title_from_config
from web.registry import GameEntry
from web.serialize import to_jsonable

bp = Blueprint("api", __name__, url_prefix="/api")


# -- helpers -----------------------------------------------------------------

def _db():
    return current_app.config["DND_DB"]


def _registry():
    return current_app.config["DND_REGISTRY"]


def _factory():
    return current_app.config["DND_GAME_FACTORY"]


def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def _game_or_404(game_id: str):
    """Returns (entry, db_row). Either may be None; 404 handled by caller."""
    return _registry().get(game_id), _db().get_game(game_id)


# -- routes ------------------------------------------------------------------

@bp.get("/health")
def health():
    from web.factory import mock_mode  # noqa: PLC0415

    return jsonify(
        {
            "ok": True,
            "mock": bool(current_app.config.get("DND_MOCK", mock_mode())),
            "games_running": _registry().running_count(),
        }
    )


@bp.get("/presets")
def presets():
    return jsonify(load_presets())


@bp.get("/auth")
def auth():
    """Can the caller write, and does this server take a credential at all?

    A read, and anonymous like the other reads: it reports only what a write
    attempt would report a moment later, so it is no more of an oracle than
    `POST /api/games` already is — and it lets the page decide whether to show
    controls that would only 401, instead of finding out by creating a game.

    `writes` is `"token"` when a secret is configured and `"unconfigured"` when
    none is, which is the difference between "you need the token" and "nobody
    can write here yet".
    """
    return jsonify(
        {
            "writes": "token" if writes_configured() else "unconfigured",
            "header": WRITE_HEADER,
            "authenticated": authenticated(),
        }
    )


@bp.post("/games")
@require_write
def create_game():
    body = request.get_json(silent=True) or {}
    config = body.get("config", body)
    if not isinstance(config, dict) or not config:
        return _err("body must be {\"config\": {...}}")

    # UI-level overrides sent alongside the config
    for key in ("seed", "setting", "tone", "budget_usd", "tempo_ms"):
        if key in body and body[key] not in (None, ""):
            config[key] = body[key]
    try:
        if "seed" in config:
            config["seed"] = int(config["seed"])
        if "budget_usd" in config:
            # `float()` accepts "NaN" and "inf", and NaN defeats every budget
            # check in the app rather than raising one: `total_usd >= nan` is
            # False, so `Game._check_budget` never halts, and so is every
            # comparison the narration endpoint makes. A budget that cannot be
            # compared is not a budget.
            budget = float(config["budget_usd"])
            if not math.isfinite(budget):
                return _err("budget_usd must be a finite number")
            config["budget_usd"] = budget
        if "tempo_ms" in config:
            config["tempo_ms"] = int(config["tempo_ms"])
    except (TypeError, ValueError) as exc:
        return _err(f"bad numeric field: {exc}")

    title = body.get("title") or title_from_config(config)
    entry = GameEntry("pending", _db(), config, title)
    try:
        game, bus = _factory()(config, entry.on_event)
    except Exception as exc:  # bad config, missing API key, missing layer
        current_app.logger.exception("game creation failed")
        return _err(f"{type(exc).__name__}: {exc}", 400)

    game_id = str(getattr(game, "id", "") or "") or "g_" + uuid.uuid4().hex[:12]
    entry.id = game_id
    entry.game = game
    entry.bus = bus

    status = str(getattr(game, "status", "created") or "created")
    _db().create_game(game_id, config, title=title, status=status, created_at=entry.created_at)
    _registry().add(entry)
    entry.start_monitor()

    try:
        game.start()
    except Exception as exc:  # pragma: no cover - defensive
        current_app.logger.exception("game start failed")
        _db().set_status(game_id, "error")
        return _err(f"start failed: {exc}", 500)

    status = str(getattr(game, "status", status) or status)
    _db().set_status(game_id, status)
    return jsonify({"id": game_id, "status": status}), 201


@bp.get("/games")
def list_games():
    out = []
    for row in _db().list_games():
        entry = _registry().get(row["id"])
        status = entry.status() if entry else row["status"]
        cost = entry.cost_usd if entry else row["cost_usd"]
        rnd = entry.round if entry else _round_of(row.get("snapshot"))
        out.append(
            {
                "id": row["id"],
                "status": status,
                "created_at": row["created_at"],
                "title": row["title"],
                "round": rnd,
                "cost_usd": round(float(cost or 0.0), 6),
                "live": entry is not None,
            }
        )
    return jsonify(out)


def _round_of(snapshot: Any) -> int:
    if isinstance(snapshot, dict):
        try:
            return int(snapshot.get("round") or (snapshot.get("state") or {}).get("round") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


@bp.get("/games/<game_id>")
def get_game(game_id: str):
    """The game: config, ledger, status and a snapshot of the board.

    ``?at_seq=<n>`` asks for the board **as of** event ``n`` rather than as of
    now. The spectator page reveals the transcript at the pace of the spoken
    narration while the game runs ahead of it, so it asks for the board at the
    last line it has put on screen; without that the map and the hit points
    would be the one thing that ran ahead of the voice. Only ``snapshot.state``
    and ``round`` come from the archive — status, ledger and cost stay live,
    because money and whether the game is still running are not narrated —
    and ``snapshot_seq`` says which seq the board actually is (``null`` when
    the archive was not consulted or had nothing).
    """
    entry, row = _game_or_404(game_id)
    if entry is None and row is None:
        return _err("no such game", 404)
    at_seq_raw = request.args.get("at_seq")
    at_seq: int | None = None
    if at_seq_raw is not None:
        try:
            at_seq = int(at_seq_raw)
        except (TypeError, ValueError):
            return _err("at_seq must be an integer")
    if entry is not None:
        snapshot = entry.snapshot()
        status = entry.status()
        ledger = entry.ledger()
        config = entry.config
        title = entry.title
        created_at = entry.created_at
        last_seq = entry.last_seq
    else:
        snapshot = row["snapshot"] or {}
        status = row["status"]
        ledger = snapshot.get("ledger") if isinstance(snapshot, dict) else {}
        config = row["config"]
        title = row["title"]
        created_at = row["created_at"]
        last_seq = -1
    board_seq: int | None = None
    if at_seq is not None and entry is not None:
        board = entry.board_at(at_seq)
        if board is not None:
            snapshot = dict(snapshot or {})
            snapshot["state"] = board["state"]
            snapshot["round"] = board["round"]
            board_seq = board["seq"]
    return jsonify(
        {
            "id": game_id,
            "title": title,
            "status": status,
            "created_at": created_at,
            "config": to_jsonable(config),
            "snapshot": to_jsonable(snapshot),
            "ledger": to_jsonable(ledger or {}),
            # An archived board carries its own round, zero included: a game
            # out of combat is round 0 and must not borrow the live one.
            "round": (
                snapshot.get("round", 0)
                if board_seq is not None
                else (_round_of(snapshot) or (entry.round if entry else 0))
            ),
            "cost_usd": round(
                float((entry.cost_usd if entry else row["cost_usd"]) or 0.0), 6
            ),
            "last_seq": last_seq,
            "snapshot_seq": board_seq,
            "live": entry is not None,
        }
    )


@bp.get("/games/<game_id>/events")
def get_events(game_id: str):
    entry, row = _game_or_404(game_id)
    if entry is None and row is None:
        return _err("no such game", 404)
    try:
        after = int(request.args.get("after", -1))
    except (TypeError, ValueError):
        after = -1
    limit = min(int(request.args.get("limit", 5000) or 5000), 20000)
    return jsonify(_db().events_after(game_id, after, limit))


def _control(game_id: str, method: str, *args: Any):
    entry, row = _game_or_404(game_id)
    if entry is None:
        if row is None:
            return _err("no such game", 404)
        return _err("game is not running in this process", 409)
    fn = getattr(entry.game, method, None)
    if not callable(fn):
        return _err(f"game does not support {method}", 501)
    try:
        fn(*args)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", 500)
    status = entry.status()
    entry.persist_snapshot()
    return jsonify({"id": game_id, "status": status}), 202


@bp.post("/games/<game_id>/pause")
@require_write
def pause(game_id: str):
    return _control(game_id, "pause")


@bp.post("/games/<game_id>/resume")
@require_write
def resume(game_id: str):
    return _control(game_id, "resume")


@bp.post("/games/<game_id>/stop")
@require_write
def stop(game_id: str):
    return _control(game_id, "stop")


@bp.post("/games/<game_id>/hold")
def hold(game_id: str):
    """Hold the game for a spectator's narration (a renewable lease, in seconds).

    Not `pause`: it expires by itself, so a browser that dies mid-hold costs the
    game a few seconds rather than freezing it, and it leaves `status` alone so
    the table's own pause/resume stays meaningful. Called every few seconds
    while a spectator's spoken narration is behind, so unlike the other controls
    it does not write a snapshot.

    It is also the one POST that takes no write token. Every anonymous listener
    renews a lease every few seconds and narration is unusable without it; it
    spends nothing, changes no status, expires on its own (≤30 s) and is
    bounded at 64 clients, so the worst an anonymous caller can do with it is
    slow a game down — which is what a spectator with the page open does
    anyway.
    """
    entry, row = _game_or_404(game_id)
    if entry is None:
        if row is None:
            return _err("no such game", 404)
        return _err("game is not running in this process", 409)
    body = request.get_json(silent=True) or {}
    raw = body.get("seconds", 0)
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return _err("seconds must be a number")
    # One lease per spectator: the loop waits for whoever is furthest behind,
    # so a second tab catching up cannot cut short a first tab that is not.
    client = str(body.get("client") or "")[:64]
    fn = getattr(entry.game, "hold", None)
    if not callable(fn):
        return _err("game does not support hold", 501)
    granted = fn(seconds, client)
    return jsonify({"id": game_id, "status": entry.status(), "holding": granted}), 202


@bp.post("/games/<game_id>/note")
@require_write
def note(game_id: str):
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return _err("text required")
    return _control(game_id, "inject_dm_note", text[:2000])
