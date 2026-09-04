"""SSE endpoint tests: replay, live tail, heartbeat headers, reconnect, end."""

from __future__ import annotations

import threading
import time

from web.app import create_app
from web.auth import ENV_VAR as WRITE_TOKEN_ENV
from web.tests.conftest import WRITE_TOKEN, fake_factory, write_client
from web.tests.test_api import create, wait_for


def read_stream(client, url, headers=None, timeout=25.0):
    """Consume an SSE response to completion (it ends itself on `event: end`)."""
    chunks: list[str] = []
    err: list[BaseException] = []

    def run() -> None:
        try:
            rv = client.get(url, headers=headers or {})
            chunks.append("__HEADERS__" + rv.headers.get("Content-Type", "") + "|"
                          + rv.headers.get("Cache-Control", "") + "|"
                          + rv.headers.get("X-Accel-Buffering", ""))
            for raw in rv.iter_encoded():
                chunks.append(raw.decode("utf-8"))
        except BaseException as exc:  # pragma: no cover
            err.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "SSE stream did not terminate within %ss" % timeout
    if err:
        raise err[0]
    return chunks[0], "".join(chunks[1:])


def parse_events(text: str) -> list[tuple[str, str, str]]:
    """-> [(id, event, data)] for each SSE message block."""
    out = []
    for block in text.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        eid = kind = data = ""
        for line in block.split("\n"):
            if line.startswith("id: "):
                eid = line[4:]
            elif line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if kind:
            out.append((eid, kind, data))
    return out


def test_stream_replays_then_streams_live(client, sample_config, monkeypatch):
    monkeypatch.setenv("FAKE_STEP_DELAY", "0.25")
    gid = create(client, sample_config)["id"]

    headers, body = read_stream(client, "/api/games/%s/stream?after=-1" % gid)
    assert "text/event-stream" in headers
    assert "no-cache" in headers
    assert headers.endswith("|no")

    events = parse_events(body)
    kinds = [k for _, k, _ in events]
    # the two events emitted synchronously in start() are replayed...
    assert kinds[0] == "scene"
    assert "combat_start" in kinds
    # ...and the thread-emitted ones arrive live
    assert "attack" in kinds and "narration" in kinds and "combat_end" in kinds
    assert kinds[-1] == "end"
    # ids are the event seqs, monotonic, no duplicates
    seqs = [int(i) for i, k, _ in events if k != "end" and i]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    assert "retry: 3000" in body


def test_stream_after_resumes(client, sample_config):
    gid = create(client, sample_config)["id"]
    wait_for(lambda: client.get("/api/games/" + gid).get_json()["status"] == "finished")

    _, body = read_stream(client, "/api/games/%s/stream?after=3" % gid)
    events = [e for e in parse_events(body) if e[1] != "end"]
    assert events and all(int(e[0]) > 3 for e in events)


def test_stream_last_event_id_header(client, sample_config):
    gid = create(client, sample_config)["id"]
    wait_for(lambda: client.get("/api/games/" + gid).get_json()["status"] == "finished")

    # EventSource reconnect: stale ?after= in the URL, fresh Last-Event-ID header
    _, body = read_stream(
        client, "/api/games/%s/stream?after=0" % gid, headers={"Last-Event-ID": "4"}
    )
    events = [e for e in parse_events(body) if e[1] != "end"]
    assert all(int(e[0]) > 4 for e in events)


def test_stream_of_dead_game_replays_from_db_and_ends(db_file, sample_config):
    cfg = {WRITE_TOKEN_ENV: WRITE_TOKEN}
    app1 = create_app(game_factory=fake_factory, db_path=db_file, config=dict(cfg))
    c1 = write_client(app1)
    gid = create(c1, sample_config)["id"]
    wait_for(lambda: len(c1.get("/api/games/%s/events?after=-1" % gid).get_json()) >= 7)
    app1.config["DND_REGISTRY"].shutdown()

    app2 = create_app(game_factory=fake_factory, db_path=db_file, config=dict(cfg))
    c2 = write_client(app2)
    _, body = read_stream(c2, "/api/games/%s/stream?after=-1" % gid, timeout=10)
    events = parse_events(body)
    assert events[-1][1] == "end"
    assert len(events) >= 8  # 7 replayed + end


def test_stream_ends_on_stop(client, sample_config, monkeypatch):
    monkeypatch.setenv("FAKE_STEP_DELAY", "0.5")
    gid = create(client, sample_config)["id"]

    def stopper():
        time.sleep(0.4)
        client.post("/api/games/%s/stop" % gid)

    threading.Thread(target=stopper, daemon=True).start()
    _, body = read_stream(client, "/api/games/%s/stream?after=-1" % gid, timeout=20)
    events = parse_events(body)
    assert events[-1][1] == "end"
    assert '"status":"stopped"' in events[-1][2]
