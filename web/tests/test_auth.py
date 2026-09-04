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


# -- the gate's own rendering, driven through node ----------------------------
#
# `renderWriteAccess()` decides what an anonymous visitor may see, and it is the
# one piece of the gate that lives in JavaScript. `app.js` is an IIFE and cannot
# be `require`d, so the function's real source is lifted out of the shipped file
# and run against stub elements — the test exercises what deploys, not a
# transcription of it.

import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(ROOT, "web", "static", "app.js")

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def function_source(name: str) -> str:
    """The text of a top-level `function name() {...}` in app.js."""
    with open(APP_JS, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("function %s(" % name)
    depth, i = 0, src.index("{", start)
    for i in range(i, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("unbalanced braces in %s" % name)


ELEMENTS = ["btn-new", "btn-unlock", "write-controls", "write-locked", "unlock"]


def render(writes: str, authed: bool) -> dict:
    """Run the real renderWriteAccess() in this state; return the DOM it left."""
    script = """
    const IN = JSON.parse(process.argv[1]);
    const els = {};
    // `hidden: null` = untouched, so a test can tell "left alone" from "set".
    for (const id of IN.elements) els[id] = { hidden: null, textContent: '', title: '' };
    const $ = (id) => { if (!els[id]) throw new Error('no such element: ' + id); return els[id]; };
    const S = { writes: IN.writes, authed: IN.authed };
    const canWrite = () => S.authed;
    %s
    renderWriteAccess();
    process.stdout.write(JSON.stringify(els));
    """ % function_source("renderWriteAccess")
    arg = json.dumps({"writes": writes, "authed": authed, "elements": ELEMENTS})
    proc = subprocess.run(
        ["node", "-e", script, arg], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@needs_node
def test_an_anonymous_visitor_sees_no_write_controls():
    dom = render("token", False)
    assert dom["btn-new"]["hidden"] is True
    assert dom["write-controls"]["hidden"] is True
    assert dom["btn-unlock"]["hidden"] is False       # the way in
    assert dom["write-locked"]["hidden"] is False
    assert "Unlock" in dom["write-locked"]["textContent"]


@needs_node
def test_forgetting_a_token_stays_reachable_once_unlocked():
    """The panel holding Forget opens from this button and nothing else, so
    hiding it when authenticated would leave a shared browser holding the
    credential with no way out short of clearing site data."""
    dom = render("token", True)
    assert dom["btn-new"]["hidden"] is False
    assert dom["write-controls"]["hidden"] is False
    assert dom["btn-unlock"]["hidden"] is False
    assert dom["btn-unlock"]["title"]
    assert dom["write-locked"]["hidden"] is True


@needs_node
def test_the_panel_is_left_alone_by_the_render():
    """`renderWriteAccess` runs on every auth answer and every refusal, so it
    must not slam the modal shut on someone who opened it to press Forget.
    `hidden` stays null here, meaning the function never assigned it —
    `submitUnlock`, `forgetToken` and Cancel each close the panel themselves."""
    for writes, authed in (("token", True), ("token", False), ("unconfigured", False)):
        assert render(writes, authed)["unlock"]["hidden"] is None, (writes, authed)


@needs_node
def test_a_server_with_no_token_offers_nothing_to_unlock():
    dom = render("unconfigured", False)
    assert dom["btn-new"]["hidden"] is True
    assert dom["btn-unlock"]["hidden"] is True        # there is nothing to enter
    assert dom["write-controls"]["hidden"] is True
    assert "no write token set" in dom["write-locked"]["textContent"]


def submit(*, token: str, authed: bool, typed: str, answer: dict | None,
           fail: str = "", forget: bool = False) -> dict:
    """Run the real submitUnlock() against a stubbed `/api/auth` answer.

    With `forget=True` the probe is slow and Forget is pressed while it is in
    flight, which is the race the generation counter exists for.
    """
    script = """
    const IN = JSON.parse(process.argv[1]);
    const els = {};
    for (const id of ['ul-token', 'ul-error', 'ul-save', 'unlock'])
      els[id] = { hidden: null, textContent: '', value: '', disabled: false };
    els['ul-token'].value = IN.typed;
    const $ = (id) => { if (!els[id]) throw new Error('no such element: ' + id); return els[id]; };
    const S = { writes: 'token', token: IN.token, authed: IN.authed, authGen: 0 };
    let stored = IN.token;
    const tokenStore = (v) => { stored = v; };
    let renders = 0;
    const renderWriteAccess = () => { renders++; };
    // A slow probe when the race is being exercised, so Forget lands first.
    const settle = (fn) => new Promise((res, rej) =>
      setTimeout(() => fn(res, rej), IN.forget ? 15 : 0));
    const api = () => IN.fail
      ? settle((res, rej) => rej(new Error(IN.fail)))
      : settle((res) => res(IN.answer));
    %s
    %s
    submitUnlock({ preventDefault() {} });
    if (IN.forget) forgetToken();
    setTimeout(() => process.stdout.write(
      JSON.stringify({ S, stored, els, renders })), 60);
    """ % (function_source("submitUnlock"), function_source("forgetToken"))
    arg = json.dumps(
        {"token": token, "authed": authed, "typed": typed, "answer": answer,
         "fail": fail, "forget": forget}
    )
    proc = subprocess.run(
        ["node", "-e", script, arg], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


ACCEPTED = {"writes": "token", "header": "X-Dnd-Token", "authenticated": True}
REJECTED = {"writes": "token", "header": "X-Dnd-Token", "authenticated": False}


@needs_node
def test_a_good_token_is_kept_and_the_panel_closes():
    out = submit(token="", authed=False, typed="good", answer=ACCEPTED)
    assert out["S"]["token"] == "good"
    assert out["S"]["authed"] is True
    assert out["stored"] == "good"
    assert out["els"]["unlock"]["hidden"] is True


@needs_node
def test_a_rejected_token_is_not_kept():
    out = submit(token="", authed=False, typed="wrong", answer=REJECTED)
    assert out["S"]["token"] == ""
    assert out["S"]["authed"] is False
    assert out["stored"] == ""                      # never written
    assert "not accepted" in out["els"]["ul-error"]["textContent"]
    assert out["els"]["unlock"]["hidden"] is None    # the panel stays open


@needs_node
def test_a_mistyped_replacement_does_not_lock_out_a_working_token():
    """The panel is reachable from the unlocked state, so typing a wrong token
    while already unlocked is an ordinary slip. Restoring the string but not
    the flag would take the write controls away from a browser still holding a
    token the server accepts."""
    out = submit(token="good", authed=True, typed="wrong", answer=REJECTED)
    assert out["S"]["token"] == "good"
    assert out["S"]["authed"] is True
    assert out["stored"] == "good"
    assert "not accepted" in out["els"]["ul-error"]["textContent"]


@needs_node
def test_a_failed_probe_leaves_the_previous_state_whole():
    """A network error says nothing about either token."""
    out = submit(token="good", authed=True, typed="maybe", answer=None, fail="offline")
    assert out["S"]["token"] == "good"
    assert out["S"]["authed"] is True
    assert out["els"]["ul-error"]["textContent"] == "offline"
    assert out["els"]["ul-save"]["disabled"] is False   # the button comes back


@needs_node
def test_an_empty_submission_does_not_touch_the_stored_token():
    out = submit(token="good", authed=True, typed="   ", answer=REJECTED)
    assert out["S"]["token"] == "good"
    assert out["S"]["authed"] is True
    assert out["renders"] == 0
    assert "Forget" in out["els"]["ul-error"]["textContent"]


@needs_node
def test_forget_wins_a_race_with_an_accepted_probe():
    """Forget is deliberately not disabled while `/api/auth` is in flight — it
    is the one control you never want taken away — so it can land mid-probe.
    The accepted branch calls `tokenStore(value)`, which without the guard
    writes the just-cleared token straight back into localStorage on a browser
    whose user had asked for it gone."""
    out = submit(token="", authed=False, typed="good", answer=ACCEPTED, forget=True)
    assert out["stored"] == ""
    assert out["S"]["token"] == ""
    assert out["S"]["authed"] is False


@needs_node
def test_forget_wins_a_race_with_a_rejected_probe():
    """The rejected branch restores `previous`/`wasAuthed`, which after a
    Forget is a credential the user has just discarded."""
    out = submit(token="good", authed=True, typed="wrong", answer=REJECTED, forget=True)
    assert out["stored"] == ""
    assert out["S"]["token"] == ""
    assert out["S"]["authed"] is False


@needs_node
def test_forget_wins_a_race_with_a_failed_probe():
    out = submit(token="good", authed=True, typed="maybe", answer=None,
                 fail="offline", forget=True)
    assert out["stored"] == ""
    assert out["S"]["token"] == ""
    assert out["S"]["authed"] is False


@needs_node
def test_a_stale_probe_does_not_leave_unlock_disabled():
    """The button is re-enabled outside the guard: a superseded probe must not
    kill the next attempt."""
    out = submit(token="", authed=False, typed="good", answer=ACCEPTED, forget=True)
    assert out["els"]["ul-save"]["disabled"] is False
