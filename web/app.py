"""Flask app factory for dnd-sim.

    python -m web.app          # host 127.0.0.1, port $PORT (default 8071)
    DND_SIM_MOCK=1 python -m web.app

Env:
    PORT              listen port (default 8071)
    DND_SIM_DB        SQLite path (default ./data/dndsim.sqlite3)
    DND_SIM_MOCK      "1" → MockLLMClient, no API calls
    ANTHROPIC_API_KEY required for live mode (on lab980: /etc/environment)
"""

from __future__ import annotations

import atexit
import logging
import os
from typing import Any, Callable

from flask import Flask, jsonify

from web.db import Database
from web.factory import default_game_factory, mock_mode
from web.registry import GameRegistry
from web.routes import register_blueprints

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: signature of the injectable factory (CONTRACTS.md Amendment 2026-09-03, web)
GameFactory = Callable[[dict, Callable[[Any], None]], "tuple[Any, Any]"]


def create_app(
    game_factory: GameFactory | None = None,
    db_path: str | None = None,
    config: dict[str, Any] | None = None,
) -> Flask:
    """Build the Flask app.

    ``game_factory(config: dict, on_event: Callable[[Event], None]) -> (Game, EventBus)``
    is injectable so the web layer can be tested with fakes, independent of
    orchestrator/llm.
    """
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
    app.config.update(config or {})
    app.config["JSON_SORT_KEYS"] = False

    db = Database(db_path or app.config.get("DND_DB_PATH"))
    app.config["DND_DB"] = db
    app.config["DND_REGISTRY"] = GameRegistry()
    app.config["DND_GAME_FACTORY"] = game_factory or default_game_factory
    app.config["DND_MOCK"] = mock_mode()

    # Restart safety: this process owns no games yet, so nothing in the DB can
    # legitimately still be running.
    stale = db.mark_stale_games_stopped()
    if stale:
        app.logger.info("marked %d stale game(s) as stopped", stale)

    register_blueprints(app)

    @app.errorhandler(404)
    def not_found(_e):  # type: ignore[misc]
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(500)
    def server_error(e):  # type: ignore[misc]
        app.logger.exception("unhandled error")
        return jsonify({"error": str(e)}), 500

    @app.after_request
    def no_store_api(resp):  # type: ignore[misc]
        if resp.mimetype == "application/json":
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    atexit.register(app.config["DND_REGISTRY"].shutdown)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("DND_SIM_LOGLEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    port = int(os.environ.get("PORT", "8071"))
    host = os.environ.get("HOST", "127.0.0.1")
    app = create_app()
    log = logging.getLogger("dnd-sim")
    log.info("dnd-sim on http://%s:%d (mock=%s, db=%s)", host, port, app.config["DND_MOCK"],
             app.config["DND_DB"].path)
    if not app.config["DND_MOCK"] and not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY not set — live games will fail to start")
    # threaded=True is required: SSE connections are long-lived and the game
    # runs in its own daemon thread.
    app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
