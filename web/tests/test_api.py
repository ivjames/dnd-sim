"""API route tests against the fakes in conftest.py."""

from __future__ import annotations

import time

from web.app import create_app
from web.auth import ENV_VAR as WRITE_TOKEN_ENV
from web.tests.conftest import WRITE_TOKEN, fake_factory, write_client


def wait_for(fn, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = fn()
        if val:
            return val
        time.sleep(interval)
    return fn()


def create(client, config, title="Test game"):
    rv = client.post("/api/games", json={"config": config, "title": title})
    assert rv.status_code == 201, rv.get_data(as_text=True)
    return rv.get_json()


def test_health(client):
    rv = client.get("/api/health")
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["ok"] is True
    assert "mock" in body and "games_running" in body


def test_index_serves_ui(client):
    rv = client.get("/")
    assert rv.status_code == 200
    assert b"dnd" in rv.get_data().lower()


def test_presets(client):
    rv = client.get("/api/presets")
    assert rv.status_code == 200
    presets = rv.get_json()
    assert isinstance(presets, list) and presets
    assert {"name", "description", "config"} <= set(presets[0])


def test_create_list_and_snapshot(client, sample_config):
    created = create(client, sample_config)
    gid = created["id"]
    assert created["status"] in ("running", "created")

    games = client.get("/api/games").get_json()
    assert any(g["id"] == gid for g in games)
    row = [g for g in games if g["id"] == gid][0]
    assert row["title"] == "Test game"

    detail = client.get("/api/games/" + gid).get_json()
    assert detail["id"] == gid
    assert detail["config"]["seed"] == 7
    assert detail["snapshot"]["state"]["combatants"]["pc_1"]["name"] == "Thorin"
    assert detail["ledger"]["total_usd"] > 0

    # events land in SQLite
    events = wait_for(
        lambda: (client.get("/api/games/%s/events?after=-1" % gid).get_json() or [])
        if len(client.get("/api/games/%s/events?after=-1" % gid).get_json() or []) >= 7
        else None
    )
    assert len(events) >= 7
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "scene" and "narration" in kinds
    assert all("seq" in e and "text" in e and "data" in e for e in events)

    # ?after= filters
    tail = client.get("/api/games/%s/events?after=2" % gid).get_json()
    assert all(e["seq"] > 2 for e in tail)


def test_missing_game_404(client):
    assert client.get("/api/games/nope").status_code == 404
    assert client.get("/api/games/nope/events").status_code == 404
    assert client.post("/api/games/nope/pause").status_code == 404
    assert client.get("/api/games/nope/stream").status_code == 404


def test_bad_create_body(client):
    assert client.post("/api/games", json={}).status_code == 400
    assert client.post("/api/games", json={"config": {"seed": "abc"}}).status_code == 400


def test_controls_pause_resume_stop(client, sample_config):
    gid = create(client, sample_config)["id"]

    rv = client.post("/api/games/%s/pause" % gid)
    assert rv.status_code == 202 and rv.get_json()["status"] == "paused"

    rv = client.post("/api/games/%s/resume" % gid)
    assert rv.status_code == 202 and rv.get_json()["status"] == "running"

    rv = client.post("/api/games/%s/stop" % gid)
    assert rv.status_code == 202 and rv.get_json()["status"] == "stopped"

    # status persisted
    assert wait_for(
        lambda: client.get("/api/games/" + gid).get_json()["status"] == "stopped"
    )


def test_hold_is_a_lease_not_a_pause(client, sample_config):
    gid = create(client, sample_config)["id"]
    game = client.application.config["DND_REGISTRY"].get(gid).game

    rv = client.post("/api/games/%s/hold" % gid, json={"seconds": 12})
    assert rv.status_code == 202
    body = rv.get_json()
    assert body["holding"] == 12.0
    # a hold must not disturb the table's own pause/resume state
    assert body["status"] == game.status != "paused"
    assert game.hold_remaining() > 0

    assert client.post("/api/games/%s/hold" % gid, json={"seconds": 0}).get_json()["holding"] == 0
    assert game.hold_remaining() == 0


def test_hold_validates_and_caps(client, sample_config):
    gid = create(client, sample_config)["id"]
    assert client.post("/api/games/%s/hold" % gid, json={"seconds": "soon"}).status_code == 400
    # no body at all is a release, not an error
    assert client.post("/api/games/%s/hold" % gid).get_json()["holding"] == 0
    assert client.post("/api/games/%s/hold" % gid, json={"seconds": 9999}).get_json()["holding"] == 30.0


def test_hold_leases_are_per_client(client, sample_config):
    gid = create(client, sample_config)["id"]
    game = client.application.config["DND_REGISTRY"].get(gid).game
    url = "/api/games/%s/hold" % gid

    client.post(url, json={"seconds": 20, "client": "tab-a"})
    client.post(url, json={"seconds": 20, "client": "tab-b"})
    client.post(url, json={"seconds": 0, "client": "tab-b"})   # b caught up
    assert game.hold_remaining() > 0                            # a is still behind
    client.post(url, json={"seconds": 0, "client": "tab-a"})
    assert game.hold_remaining() == 0


def test_hold_on_an_unknown_or_dead_game(client, sample_config):
    assert client.post("/api/games/nope/hold", json={"seconds": 5}).status_code == 404


def test_dm_note(client, sample_config):
    gid = create(client, sample_config)["id"]
    assert client.post("/api/games/%s/note" % gid, json={"text": ""}).status_code == 400
    rv = client.post("/api/games/%s/note" % gid, json={"text": "A raven lands."})
    assert rv.status_code == 202

    events = wait_for(
        lambda: [
            e for e in client.get("/api/games/%s/events?after=-1" % gid).get_json()
            if e["kind"] == "dm_note"
        ]
    )
    assert events and events[0]["text"] == "A raven lands."


def test_restart_marks_stale_games_stopped(db_file, sample_config):
    cfg = {WRITE_TOKEN_ENV: WRITE_TOKEN}
    app1 = create_app(game_factory=fake_factory, db_path=db_file, config=dict(cfg))
    c1 = write_client(app1)
    gid = create(c1, sample_config)["id"]
    app1.config["DND_DB"].set_status(gid, "running")
    app1.config["DND_REGISTRY"].shutdown()

    # fresh process: registry is empty, so nothing can still be running
    app2 = create_app(game_factory=fake_factory, db_path=db_file, config=dict(cfg))
    c2 = write_client(app2)
    row = [g for g in c2.get("/api/games").get_json() if g["id"] == gid][0]
    assert row["status"] == "stopped"
    assert row["live"] is False

    # controls on a dead game are a 409, not a crash
    assert c2.post("/api/games/%s/pause" % gid).status_code == 409
    assert c2.post("/api/games/%s/hold" % gid, json={"seconds": 5}).status_code == 409
    # ...but its history is still readable
    assert c2.get("/api/games/%s/events?after=-1" % gid).status_code == 200
    detail = c2.get("/api/games/" + gid).get_json()
    assert detail["status"] == "stopped"
