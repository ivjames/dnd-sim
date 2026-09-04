"""The board archive: `GameEntry` keeps the state as of each event, and
`GET /api/games/<id>?at_seq=` serves it.

The spectator page reveals the transcript at the pace of the spoken narration,
so it asks for the board at the line it has just put on screen. Everything
else about a game — the transcript, the cost meter, the SSE stream — is
already something the page can hold back by itself. The board is not: it comes
from the server's own state, which is wherever the game has got to. Without
this the map and the hit points would be the one thing that still ran minutes
ahead of the voice, which is the whole complaint the archive answers.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from web.db import Database
from web.registry import BOARD_HISTORY, BOARD_SKIP_KINDS, GameEntry


class StepGame:
    """A game whose board actually moves, so an archive of it can be wrong.

    `FakeGame` in conftest returns one constant snapshot, which cannot tell a
    board pinned to a seq from a board read live.
    """

    def __init__(self) -> None:
        self.id = "g_step"
        self.status = "running"
        self.hp = 30
        self.round = 1

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "round": self.round,
            "ledger": {"total_usd": 0.5},
            "state": {
                "round": self.round,
                "combatants": {"pc_1": {"id": "pc_1", "name": "Thorin", "hp": self.hp}},
            },
        }


class Ev:
    def __init__(self, seq: int, kind: str, text: str = "") -> None:
        self.seq, self.kind, self.text = seq, kind, text
        self.round, self.actor, self.data = 1, None, {}

    def to_dict(self) -> dict:
        return {"seq": self.seq, "round": self.round, "kind": self.kind,
                "actor": self.actor, "text": self.text, "data": self.data}


@pytest.fixture()
def entry(db_file) -> GameEntry:
    e = GameEntry("g_step", Database(db_file), {"seed": 1}, "Board archive")
    e.game = StepGame()
    return e


def hp_at(entry: GameEntry, seq: int) -> Any:
    board = entry.board_at(seq)
    assert board is not None
    return board["state"]["combatants"]["pc_1"]["hp"]


def test_the_board_is_kept_as_it_stood_at_each_event(entry):
    entry.on_event(Ev(1, "turn_start"))
    entry.game.hp = 21
    entry.on_event(Ev(2, "damage"))
    entry.game.hp = 12
    entry.on_event(Ev(3, "damage"))

    assert hp_at(entry, 1) == 30
    assert hp_at(entry, 2) == 21
    assert hp_at(entry, 3) == 12
    # And the live game is somewhere else entirely, which is the point.
    entry.game.hp = 4
    assert hp_at(entry, 2) == 21
    assert entry.snapshot()["state"]["combatants"]["pc_1"]["hp"] == 4


def test_prose_is_not_archived_but_answers_with_the_state_it_was_written_about(entry):
    """A paragraph moves nothing, so it keeps no board of its own.

    It still has to *answer* — the page asks at the last line it revealed, and
    that line is very often the narration — and the answer is the state at the
    event before it, which is exactly the state the paragraph describes.
    """
    entry.on_event(Ev(1, "turn_start"))
    entry.game.hp = 21
    entry.on_event(Ev(2, "damage"))
    for seq, kind in enumerate(sorted(BOARD_SKIP_KINDS), start=3):
        entry.on_event(Ev(seq, kind))
    assert set(entry._boards) == {1, 2}
    last = 2 + len(BOARD_SKIP_KINDS)
    assert hp_at(entry, last) == 21
    assert entry.board_at(last)["seq"] == 2


def test_asking_before_the_archive_starts_gets_the_oldest_it_has(entry):
    """A listener further behind than the ring is deep gets the least wrong
    answer available, and is told which seq it actually is."""
    for seq in range(1, BOARD_HISTORY + 20):
        entry.game.hp = 1000 - seq
        entry.on_event(Ev(seq, "move"))
    assert len(entry._boards) == BOARD_HISTORY
    oldest = min(entry._boards)
    board = entry.board_at(1)
    assert board["seq"] == oldest
    assert board["state"]["combatants"]["pc_1"]["hp"] == 1000 - oldest


def test_nothing_archived_yet_is_none(entry):
    assert entry.board_at(5) is None


# -- the route ---------------------------------------------------------------

def register(app, entry: GameEntry) -> None:
    app.config["DND_DB"].create_game(entry.id, entry.config, title=entry.title,
                                     status="running", created_at=entry.created_at)
    app.config["DND_REGISTRY"].add(entry)


def test_at_seq_serves_the_archived_board_and_says_which_seq_it_is(app, client, db_file):
    e = GameEntry("g_step", app.config["DND_DB"], {"seed": 1}, "Board archive")
    e.game = StepGame()
    register(app, e)

    e.on_event(Ev(1, "turn_start"))
    e.game.hp = 21
    e.on_event(Ev(2, "damage"))
    e.game.hp = 3
    e.game.round = 4
    e.on_event(Ev(3, "damage"))

    live = client.get("/api/games/g_step").get_json()
    assert live["snapshot"]["state"]["combatants"]["pc_1"]["hp"] == 3
    assert live["snapshot_seq"] is None

    past = client.get("/api/games/g_step?at_seq=2").get_json()
    assert past["snapshot"]["state"]["combatants"]["pc_1"]["hp"] == 21
    assert past["snapshot_seq"] == 2
    assert past["round"] == 1                      # the round the board was in
    # Money and whether the game is still running are live facts, not narrated
    # ones: they are the same in both answers.
    assert past["status"] == live["status"]
    assert past["cost_usd"] == live["cost_usd"]
    assert past["ledger"] == live["ledger"]


def test_a_bad_at_seq_is_a_400(app, client):
    e = GameEntry("g_step", app.config["DND_DB"], {"seed": 1}, "Board archive")
    e.game = StepGame()
    register(app, e)
    r = client.get("/api/games/g_step?at_seq=soon")
    assert r.status_code == 400
    assert "at_seq" in r.get_json()["error"]


def test_at_seq_on_a_game_this_process_is_not_running_is_ignored(app, client):
    """A game only in the database has no archive; the reply is its last saved
    snapshot rather than an error, because a transcript is still readable."""
    app.config["DND_DB"].create_game("g_dead", {"seed": 1}, title="Old", status="stopped")
    app.config["DND_DB"].save_snapshot("g_dead", json.loads('{"state": {"combatants": {}}}'),
                                       status="stopped", cost_usd=0.0)
    r = client.get("/api/games/g_dead?at_seq=99")
    assert r.status_code == 200
    assert r.get_json()["snapshot_seq"] is None
