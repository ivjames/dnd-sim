"""Direct tests for the SQLite layer and the serializers."""

from __future__ import annotations

from dataclasses import dataclass, field

from web.db import Database
from web.serialize import event_to_dict, to_jsonable


def test_games_crud(tmp_path):
    db = Database(str(tmp_path / "t.sqlite3"))
    db.create_game("g1", {"seed": 1}, title="One", status="created")
    row = db.get_game("g1")
    assert row["config"]["seed"] == 1 and row["status"] == "created"

    db.set_status("g1", "running")
    db.save_snapshot("g1", {"round": 3}, cost_usd=0.25)
    row = db.get_game("g1")
    assert row["status"] == "running" and row["snapshot"]["round"] == 3
    assert row["cost_usd"] == 0.25

    assert db.get_game("missing") is None
    assert [g["id"] for g in db.list_games()] == ["g1"]


def test_events_and_after(tmp_path):
    db = Database(str(tmp_path / "t.sqlite3"))
    db.create_game("g1", {})
    for i in range(5):
        db.add_event("g1", i, "roll", {"seq": i, "kind": "roll", "text": "d20"})
    assert db.event_count("g1") == 5
    assert [e["seq"] for e in db.events_after("g1", 1)] == [2, 3, 4]
    # idempotent re-insert (same primary key)
    db.add_event("g1", 4, "roll", {"seq": 4, "kind": "roll", "text": "again"})
    assert db.event_count("g1") == 5
    assert db.events_after("g1", 3)[0]["text"] == "again"


def test_mark_stale_games_stopped(tmp_path):
    path = str(tmp_path / "t.sqlite3")
    db = Database(path)
    db.create_game("a", {}, status="running")
    db.create_game("b", {}, status="paused")
    db.create_game("c", {}, status="finished")
    assert Database(path).mark_stale_games_stopped() == 2
    statuses = {g["id"]: g["status"] for g in db.list_games()}
    assert statuses == {"a": "stopped", "b": "stopped", "c": "finished"}


@dataclass
class _Ev:
    seq: int
    round: int
    kind: str
    actor: str | None
    text: str
    data: dict = field(default_factory=dict)


def test_event_to_dict_shapes():
    d = event_to_dict(_Ev(3, 1, "attack", "pc_1", "hits", {"total": 17}))
    assert d == {"seq": 3, "round": 1, "kind": "attack", "actor": "pc_1",
                 "text": "hits", "data": {"total": 17}}
    # dict passthrough with defaults filled in
    d2 = event_to_dict({"kind": "narration", "text": "dusk"})
    assert d2["seq"] == 0 and d2["round"] == 0 and d2["data"] == {}


def test_to_jsonable_handles_sets_and_tuples():
    out = to_jsonable({"walls": {(1, 2)}, "pos": (3, 4)})
    assert out["pos"] == [3, 4]
    assert out["walls"] == [[1, 2]]
