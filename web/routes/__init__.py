"""Blueprint registration."""

from __future__ import annotations

from flask import Flask

from web.routes.api import bp as api_bp
from web.routes.stream import bp as stream_bp
from web.routes.ui import bp as ui_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(ui_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(stream_bp)


__all__ = ["register_blueprints", "api_bp", "stream_bp", "ui_bp"]
