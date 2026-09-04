"""Server-rendered narration: Polly clips for the spectator's player.

The browser still decides *what* is said and *who* says it — `speech.js` turns
an event into a phrase and a voice key, exactly as it did when the OS spoke the
line — and asks here for the audio. Keeping the wording in the browser is what
makes this a migration rather than a rewrite: one narrator, two ways of making
a sound, and the old one is still there when this one cannot answer.

Money is the reason for most of the rules below. A clip costs
$4.00/1M characters (`tts/client.py`), so it is cached on disk, charged to the
game's own `budget_usd`, and refused once that budget is gone — at which point
the page falls back to the browser's voices and the game is still audible,
just less well.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request

from tts.client import TTSError

bp = Blueprint("tts", __name__, url_prefix="/api")

MAX_KEY = 64
#: a year: the URL names the words and the seat, so the bytes never change
IMMUTABLE = "public, max-age=31536000, immutable"


def _service():
    return current_app.config.get("DND_TTS")


def _db():
    return current_app.config["DND_DB"]


def _registry():
    return current_app.config["DND_REGISTRY"]


def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def _mock_game(entry: Any, row: Any) -> bool:
    return bool(_config_of(entry, row).get("mock"))


def _config_of(entry: Any, row: Any) -> dict:
    cfg = (entry.config if entry is not None else (row or {}).get("config")) or {}
    return cfg if isinstance(cfg, dict) else {}


def _gender_for(entry: Any, row: Any, key: str) -> str:
    """The character's gender, from the game's own party list.

    Read here rather than taken from the request on purpose: this endpoint
    spends money, and a gender in the query string would be a way to pick any
    voice on the roster (and to mint a fresh cache entry per pick). Only a party
    member has a gender to state — `dm`, `npc` and `monster:<id>` have no
    character record and are cast from the whole pool, as before.
    """
    for member in _config_of(entry, row).get("party") or []:
        if isinstance(member, dict) and str(member.get("id") or "") == key:
            return str(member.get("gender") or "")
    return ""


def _default_budget() -> float:
    """`GameConfig.budget_usd`'s default, asked of the class that owns it.

    A game created through the API without a `budget_usd` gets this from
    `GameConfig` while it is live — but the config PERSISTED is the raw body,
    which never had the key, so after a restart there is nothing in the row to
    read. Falling back to the same number the orchestrator would have used
    keeps the cap on. The web layer is meant to be importable without
    `orchestrator` (see web/tests/conftest.py), hence the guard.
    """
    try:
        from orchestrator.config import GameConfig  # noqa: PLC0415

        return float(GameConfig().budget_usd)
    except Exception:  # pragma: no cover - orchestrator absent
        return 1.00


def _budget_of(entry: Any, row: Any) -> float:
    """This game's `budget_usd`, from wherever it can still be read.

    Never 0 for "unknown": zero is a real and meaningful value — the
    orchestrator halts at `total_usd >= budget_usd`, so a zero budget is a game
    that is already over, not a game with no cap. Answering "unknown" with zero
    would have turned the one case that must refuse everything into the one
    case that refuses nothing.
    """
    game = getattr(entry, "game", None) if entry is not None else None
    cfg = getattr(game, "cfg", None)
    for candidate in (
        getattr(cfg, "budget_usd", None),
        (entry.config or {}).get("budget_usd") if entry is not None else None,
        ((row or {}).get("config") or {}).get("budget_usd"),
    ):
        if candidate is not None:
            try:
                return float(candidate)
            except (TypeError, ValueError):
                continue
    return _default_budget()


def _ledger_of(entry: Any):
    """The live `Ledger` object, if this process is running the game."""
    game = getattr(entry, "game", None) if entry is not None else None
    ledger = getattr(game, "ledger", None)
    return ledger if hasattr(ledger, "add_usd") else None


def _spent(entry: Any, game_id: str) -> float:
    """What this game has spent, read fresh.

    Fresh matters: this is read again inside the admission lock, and a `row`
    dict fetched at the top of the request would answer with whatever was true
    before the last spectator's clip.
    """
    ledger = _ledger_of(entry)
    if ledger is not None:
        return float(getattr(ledger, "total_usd", 0.0) or 0.0)
    if entry is not None:
        return float(entry.cost_usd or 0.0)
    return float((_db().get_game(game_id) or {}).get("cost_usd") or 0.0)


# Admission control. The budget check and the charge are two moments, and a
# clip is synthesized between them: without this, every spectator asking for a
# DIFFERENT line at the same time reads the same below-budget total and every
# one of them goes to Polly. The per-cache-key lock in `PollyTTS` does not help
# — it only collapses requests for the SAME words.
#
# So a synthesis about to happen holds its own cost against the game until it
# is either charged or abandoned. In-process is the whole story: `instances: 1`
# is a hard ceiling in ecosystem.config.js (games live in this process), so
# there is no second process to race with.
_ADMIT = threading.Lock()
_RESERVED: dict[str, float] = {}


class _NoBudget(Exception):
    """Raised inside `_admission`: this clip would take the game over."""

    def __init__(self, committed: float) -> None:
        super().__init__(committed)
        self.committed = committed


@contextmanager
def _admission(game_id: str, entry: Any, usd: float, budget: float):
    with _ADMIT:
        held = _RESERVED.get(game_id, 0.0)
        committed = _spent(entry, game_id) + held
        # Strictly: a clip that WOULD take the game over is refused, where the
        # model-call check stops the game only once it already has. Erring
        # toward stopping is the house style (llm/cost.py picks peak rates for
        # the same reason). No `budget > 0` escape: a zero or negative budget is
        # a game the orchestrator considers already exhausted, so it refuses
        # everything rather than nothing.
        if committed + usd > budget:
            raise _NoBudget(committed)
        _RESERVED[game_id] = held + usd
    try:
        yield
    finally:
        with _ADMIT:
            left = _RESERVED.get(game_id, 0.0) - usd
            if left > 1e-9:
                _RESERVED[game_id] = left
            else:
                _RESERVED.pop(game_id, None)


def _charge(game_id: str, entry: Any, chars: int, usd: float) -> None:
    """Put a synthesis on the game's tab.

    A game this process is running has a `Ledger`, and charging it there is
    what makes narration count against `budget_usd` the way model calls do —
    the game's own budget check stops it. A game that outlived its process has
    only the row, so the row's running total is incremented directly; snapshots
    are no longer being written over it.
    """
    ledger = _ledger_of(entry)
    if ledger is not None:
        ledger.add_usd("tts", usd, chars=chars)
        # ...and write it down. A `GameEntry` stays in the registry for the
        # life of the process, but its monitor thread returns at the first
        # terminal status — so for a finished game, which is exactly the game
        # people replay, nothing else would ever persist this. The charge would
        # live in memory until a restart handed the budget back.
        try:
            entry.persist_snapshot()
        except Exception:  # pragma: no cover - accounting must not kill playback
            current_app.logger.exception("could not persist tts cost")
        return
    try:
        _db().add_cost(game_id, usd)
    except Exception:  # pragma: no cover - accounting must not kill playback
        current_app.logger.exception("could not record tts cost")


@bp.get("/tts")
def capability():
    """What the page needs to decide between Polly and its own voices."""
    svc = _service()
    if svc is None:
        return jsonify({"available": False, "reason": "server voices are switched off"})
    if not svc.available():
        return jsonify(
            {
                "available": False,
                "reason": "no Polly client — boto3 is missing, or AWS credentials are not set",
            }
        )
    return jsonify(
        {
            "available": True,
            "engine": svc.engine,
            "language": svc.language,
            "max_chars": svc.max_chars,
            "price_per_million_chars": svc.price_per_million,
            # The page puts this in every clip URL. A clip's bytes depend on
            # process-level settings the URL does not otherwise name — engine,
            # language, the DM's voice, the roster Polly reported — and the
            # response is cached `immutable` for a year. Without a token that
            # moves when they do, a browser that has heard a line keeps hearing
            # the old voice for it forever after the server is reconfigured.
            "config": svc.config_id(),
        }
    )


@bp.get("/games/<game_id>/tts")
def speak(game_id: str):
    """One line, in one seat's voice, as `audio/mpeg`.

    Every refusal is a JSON error the page can fall back from, never a hang:
    503 no service, 402 no budget left, 400 nothing sayable, 502 Polly said no.
    """
    svc = _service()
    if svc is None:
        return _err("server voices are switched off", 503)

    entry = _registry().get(game_id)
    row = _db().get_game(game_id)
    if entry is None and row is None:
        return _err("no such game", 404)
    if _mock_game(entry, row):
        # A mock game costs nothing by construction (CLAUDE.md); paying Polly
        # to read one out would make that false. `DND_TTS=1` overrides it.
        from tts.client import env_flag  # noqa: PLC0415

        if not env_flag("DND_TTS", False):
            return _err("this game is a mock run; server voices stay off", 503)

    text = (request.args.get("text") or "").strip()
    key = (request.args.get("key") or "dm").strip()[:MAX_KEY]
    if not text:
        return _err("text required")
    if len(text) > svc.max_chars:
        return _err(f"text is {len(text)} characters; the cap is {svc.max_chars}")

    # An unchanged clip is the common case — the playhead goes backwards, tabs
    # reload, two spectators hear the same line — so answer the revalidation
    # before touching the cache, let alone Polly.
    gender = _gender_for(entry, row, key)
    try:
        cast, ckey = svc.cache_key_for(key, text, gender)
    except Exception as exc:  # a pool that cannot be cast from
        current_app.logger.warning("tts casting failed: %s", exc)
        return _err(f"{type(exc).__name__}: {exc}", 503)
    etag = '"' + ckey + '"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag, "Cache-Control": IMMUTABLE})

    # A clip already paid for is served whatever the budget says. The budget
    # governs SPEND, and re-reading a line costs nothing — a game that has run
    # out of money stays listenable right to the end of its transcript.
    hit = svc.cached(ckey)
    if hit is not None:
        return _audio(hit, cast.voice_id, etag)

    budget = _budget_of(entry, row)
    try:
        with _admission(game_id, entry, svc.price_of(len(text)), budget):
            result = svc.synthesize(key, text, gender)
    except _NoBudget as exc:
        return _err(
            f"budget of ${budget:.2f} is spent (${exc.committed:.4f}); "
            "narration falls back to the browser's voices",
            402,
        )
    except TTSError as exc:
        current_app.logger.info("tts unavailable: %s", exc)
        return _err(str(exc), 502)

    if not result.cached and result.usd:
        _charge(game_id, entry, result.chars, result.usd)

    return _audio(result.audio, result.cast.voice_id, etag)


def _audio(data: bytes, voice_id: str, etag: str):
    return Response(
        data,
        mimetype="audio/mpeg",
        headers={
            "Cache-Control": IMMUTABLE,
            "ETag": etag,
            "Content-Length": str(len(data)),
            "X-Dnd-Voice": voice_id,
        },
    )
