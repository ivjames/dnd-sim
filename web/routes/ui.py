"""Static spectator UI."""

from __future__ import annotations

from flask import Blueprint, current_app, send_from_directory

bp = Blueprint("ui", __name__)


@bp.get("/")
def index():
    resp = send_from_directory(current_app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.get("/favicon.ico")
def favicon():
    return ("", 204)
