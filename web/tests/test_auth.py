"""The write perimeter: which routes take a credential, and which must not.

Two things are being asserted, and the second matters as much as the first.
The write routes refuse an anonymous caller — that is the hole this closes. And
everything a spectator does stays anonymous: reading a game, listing games,
the SSE stream, the narration probe, the paid narration endpoint itself and the
narration hold. The site is a public spectator UI; auth that reached those
would break the product rather than protect it.
"""

from __future__ import annotations

import re

from web.app import create_app
from web.auth import ENV_VAR as WRITE_TOKEN_ENV, HEADER as WRITE_HEADER
from web.tests.conftest import (
    WRITE_TOKEN,
    fake_factory,
    write_client,
)
from web.tests.test_api import create

#: Every route that mutates a game or spends money, as (method, path-template).
#: `/hold` is deliberately absent — see `test_hold_is_anonymous`.
WRITE_ROUTES = [
    ("post", "/api/games"),
    ("post", "/api/games/{gid}/pause"),
    ("post", "/api/games/{gid}/resume"),
    ("post", "/api/games/{gid}/stop"),
    ("post", "/api/games/{gid}/note"),
]

#: Bodies that are otherwise valid, so a refusal can only be about the token.
BODIES = {
    "/api/games": {"config": {"seed": 1, "party": [], "scenario": {}}},
    "note": {"text": "A raven lands."},
}


def body_for(path: str, config: dict) -> dict:
    if path.endswith("/note"):
        return BODIES["note"]
    if path == "/api/games":
        return {"config": config, "title": "Test game"}
    return {}


def anon(app):
    """A client with no write token at all."""
    return app.test_client()


def bad(app):
    """A client presenting the wrong write token."""
    client = app.test_client()
    client.environ_base["HTTP_" + WRITE_HEADER.upper().replace("-", "_")] = "not-the-token"
    return client


# -- writes are gated --------------------------------------------------------

def test_every_write_route_refuses_an_anonymous_caller(app, sample_config):
    gid = create(write_client(app), sample_config)["id"]
    client = anon(app)
    for method, template in WRITE_ROUTES:
        path = template.format(gid=gid)
        rv = getattr(client, method)(path, json=body_for(path, sample_config))
        assert rv.status_code == 401, f"{method.upper()} {path} → {rv.status_code}"
        assert rv.get_json()["code"] == "unauthorized"


def test_every_write_route_refuses_a_wrong_token(app, sample_config):
    gid = create(write_client(app), sample_config)["id"]
    client = bad(app)
    for method, template in WRITE_ROUTES:
        path = template.format(gid=gid)
        rv = getattr(client, method)(path, json=body_for(path, sample_config))
        assert rv.status_code == 401, f"{method.upper()} {path} → {rv.status_code}"


def test_the_right_token_gets_through(client, sample_config):
    gid = create(client, sample_config)["id"]
    assert client.post("/api/games/%s/pause" % gid).status_code == 202
    assert client.post("/api/games/%s/note" % gid, json={"text": "Hi."}).status_code == 202


def test_a_refused_create_starts_no_game(app, sample_config):
    before = len(write_client(app).get("/api/games").get_json())
    assert anon(app).post(
        "/api/games", json={"config": sample_config}
    ).status_code == 401
    assert len(write_client(app).get("/api/games").get_json()) == before


def test_a_refused_note_reaches_no_game(app, sample_config):
    c = write_client(app)
    gid = create(c, sample_config)["id"]
    game = app.config["DND_REGISTRY"].get(gid).game
    assert anon(app).post(
        "/api/games/%s/note" % gid, json={"text": "spend my money"}
    ).status_code == 401
    assert game.notes == []


def test_the_gate_runs_before_the_body_is_validated(app):
    """A 401 rather than a 400 for a bad body: an anonymous caller learns
    nothing about what a valid request looks like, and no work is done."""
    rv = anon(app).post("/api/games", json={})
    assert rv.status_code == 401


def test_token_comparison_is_not_a_prefix_match(app, sample_config):
    client = app.test_client()
    key = "HTTP_" + WRITE_HEADER.upper().replace("-", "_")
    for wrong in (WRITE_TOKEN[:-1], WRITE_TOKEN + "x", WRITE_TOKEN.upper(), " "):
        client.environ_base[key] = wrong
        rv = client.post("/api/games", json={"config": sample_config})
        assert rv.status_code == 401, wrong


# -- reads stay anonymous ----------------------------------------------------

def test_reads_are_anonymous(app, sample_config):
    gid = create(write_client(app), sample_config)["id"]
    client = anon(app)
    for path in (
        "/",
        "/api/health",
        "/api/presets",
        "/api/auth",
        "/api/games",
        "/api/games/%s" % gid,
        "/api/games/%s/events?after=-1" % gid,
        "/api/tts",
    ):
        assert client.get(path).status_code == 200, path


def test_the_stream_is_anonymous(app, sample_config):
    """The SSE stream is the whole point of the site and takes no credential."""
    gid = create(write_client(app), sample_config)["id"]
    rv = anon(app).get("/api/games/%s/stream?after=-1" % gid, buffered=False)
    assert rv.status_code == 200
    assert rv.mimetype == "text/event-stream"
    rv.close()


def test_hold_is_anonymous(app, sample_config):
    """Narration keeps step through the hold, and every listener renews one.

    It spends nothing, leaves `status` alone and expires by itself, so it is
    the one POST left open — gating it would take server narration away from
    exactly the anonymous audience this site exists for.
    """
    gid = create(write_client(app), sample_config)["id"]
    rv = anon(app).post("/api/games/%s/hold" % gid, json={"seconds": 5})
    assert rv.status_code == 202
    assert rv.get_json()["holding"] == 5.0


def test_narration_is_anonymous(tts_app, sample_config):
    """The paid endpoint stays open: it is capped by the game's budget and by
    `DND_TTS_MAX_USD`, and an anonymous spectator cannot hear the game without
    it."""
    gid = create(write_client(tts_app), sample_config)["id"]
    rv = tts_app.test_client().get(
        "/api/games/%s/tts?key=dm&text=The+cart+burns." % gid
    )
    assert rv.status_code == 200
    assert rv.mimetype == "audio/mpeg"


# -- the probe ---------------------------------------------------------------

def test_auth_probe_reports_what_the_caller_can_do(app):
    assert anon(app).get("/api/auth").get_json() == {
        "writes": "token",
        "header": WRITE_HEADER,
        "authenticated": False,
    }
    assert write_client(app).get("/api/auth").get_json()["authenticated"] is True


def test_auth_probe_never_returns_the_token(app):
    body = write_client(app).get("/api/auth").get_data(as_text=True)
    assert WRITE_TOKEN not in body


# -- an unconfigured server --------------------------------------------------

def unconfigured_app(db_file, monkeypatch):
    monkeypatch.delenv(WRITE_TOKEN_ENV, raising=False)
    app = create_app(game_factory=fake_factory, db_path=db_file, config={"DND_TTS": None})
    app.config["TESTING"] = True
    return app


def test_unset_token_refuses_writes_rather_than_opening_them(db_file, monkeypatch, sample_config):
    """`dndsim deploy` adopts keys from /etc/environment and never overwrites
    `.env`, so the first deploy carrying this code lands on a droplet with no
    token set. Failing open there would ship the hole, still open and now
    believed closed."""
    app = unconfigured_app(db_file, monkeypatch)
    try:
        client = app.test_client()
        rv = client.post("/api/games", json={"config": sample_config})
        assert rv.status_code == 503
        assert rv.get_json()["code"] == "writes_unconfigured"
        # ...and a token cannot be guessed into existence
        client.environ_base["HTTP_" + WRITE_HEADER.upper().replace("-", "_")] = ""
        assert client.post("/api/games", json={"config": sample_config}).status_code == 503
        assert client.get("/api/auth").get_json()["writes"] == "unconfigured"
    finally:
        app.config["DND_REGISTRY"].shutdown()


def test_unset_token_leaves_reads_and_the_app_working(db_file, monkeypatch):
    """A missing key degrades: the app starts, and everything a spectator does
    still works."""
    app = unconfigured_app(db_file, monkeypatch)
    try:
        client = app.test_client()
        for path in ("/", "/api/health", "/api/presets", "/api/games", "/api/tts"):
            assert client.get(path).status_code == 200, path
    finally:
        app.config["DND_REGISTRY"].shutdown()


def test_a_blank_token_is_no_token(db_file, monkeypatch, sample_config):
    """Whitespace in `.env` must not read as a configured secret that an empty
    header then matches."""
    monkeypatch.setenv(WRITE_TOKEN_ENV, "   ")
    app = create_app(game_factory=fake_factory, db_path=db_file, config={"DND_TTS": None})
    app.config["TESTING"] = True
    try:
        rv = app.test_client().post("/api/games", json={"config": sample_config})
        assert rv.status_code == 503
    finally:
        app.config["DND_REGISTRY"].shutdown()


def test_the_token_is_read_from_the_header_not_the_query_string(app, sample_config):
    """A query string is in nginx's access log, in `Referer` and in history."""
    rv = app.test_client().post(
        "/api/games?token=%s" % WRITE_TOKEN, json={"config": sample_config}
    )
    assert rv.status_code == 401


# -- the page and the server agree -------------------------------------------

def test_the_page_sends_the_header_the_server_reads():
    import os

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "web", "static", "app.js"), encoding="utf-8") as fh:
        js = fh.read()
    assert re.search(r"var WRITE_HEADER = '%s';" % re.escape(WRITE_HEADER), js), (
        "app.js and web/auth.py disagree on the header name"
    )
