"""The narration endpoints: what they serve, what they refuse, what they charge.

Driven with `FakeTTS` (conftest) rather than Polly, so this is about the rules
around synthesis — budget, caching, refusals, the fallback the browser is
promised — not about AWS.
"""

from __future__ import annotations

from urllib.parse import urlencode

import pytest

from llm.cost import Ledger
from web.tests.test_api import create


def speak_url(game_id, text="The cart still smoulders.", key="dm"):
    return "/api/games/{}/tts?{}".format(game_id, urlencode({"key": key, "text": text}))


@pytest.fixture()
def game(tts_client, sample_config):
    return create(tts_client, sample_config)["id"]


# -- the capability probe ----------------------------------------------------

def test_a_server_with_no_polly_says_so_rather_than_erroring(client):
    """The page asks once and uses the browser's own voices on a no."""
    rv = client.get("/api/tts")
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["available"] is False and body["reason"]


def test_the_probe_says_what_the_page_needs_to_know(tts_client, tts):
    body = tts_client.get("/api/tts").get_json()
    assert body["available"] is True
    assert body["engine"] == "standard" and body["language"] == "en-US"
    assert body["max_chars"] == tts.max_chars
    assert body["price_per_million_chars"] == 4.0

    tts.up = False
    off = tts_client.get("/api/tts").get_json()
    assert off["available"] is False and "credentials" in off["reason"]


# -- serving a clip ----------------------------------------------------------

def test_a_line_comes_back_as_audio(tts_client, tts, game):
    rv = tts_client.get(speak_url(game))
    assert rv.status_code == 200
    assert rv.mimetype == "audio/mpeg"
    assert rv.data.startswith(b"\xff\xfb")
    assert rv.headers["X-Dnd-Voice"] == "Brian"          # the DM's seat
    assert "immutable" in rv.headers["Cache-Control"]
    assert tts.calls == [("dm", "The cart still smoulders.")]


def test_the_same_line_is_never_synthesized_twice(tts_client, tts, game):
    first = tts_client.get(speak_url(game))
    etag = first.headers["ETag"]
    assert etag

    # A revalidation is answered without going near the cache or Polly: the
    # playhead runs backwards all the time, and every replay would otherwise
    # be a fresh round trip.
    again = tts_client.get(speak_url(game), headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert len(tts.calls) == 1


def test_a_different_speaker_is_a_different_clip(tts_client, tts, game):
    dm = tts_client.get(speak_url(game, key="dm"))
    goblin = tts_client.get(speak_url(game, key="monster:goblin_1"))
    assert dm.headers["ETag"] != goblin.headers["ETag"]
    assert dm.headers["X-Dnd-Voice"] != goblin.headers["X-Dnd-Voice"]


def test_what_it_refuses(tts_client, tts, game):
    assert tts_client.get("/api/games/nope/tts?text=hi").status_code == 404
    assert tts_client.get("/api/games/%s/tts" % game).status_code == 400
    long_line = tts_client.get(speak_url(game, text="x" * (tts.max_chars + 1)))
    assert long_line.status_code == 400 and "cap" in long_line.get_json()["error"]
    assert tts.calls == []

    # Polly itself failing is a 502, and a 502 is a line the browser speaks.
    tts.fail = "InvalidSsmlException"
    broken = tts_client.get(speak_url(game, text="Something else."))
    assert broken.status_code == 502 and "InvalidSsml" in broken.get_json()["error"]


def test_no_service_is_a_clean_no(client, sample_config):
    game_id = create(client, sample_config)["id"]
    rv = client.get(speak_url(game_id))
    assert rv.status_code == 503 and rv.get_json()["error"]


def test_a_mock_game_stays_free(tts_client, tts, sample_config, monkeypatch):
    """`DND_SIM_MOCK` games cost nothing by construction; Polly would break that."""
    monkeypatch.delenv("DND_TTS", raising=False)
    mock_game = create(tts_client, dict(sample_config, mock=True))["id"]
    rv = tts_client.get(speak_url(mock_game))
    assert rv.status_code == 503 and "mock" in rv.get_json()["error"]
    assert tts.calls == []

    monkeypatch.setenv("DND_TTS", "1")          # ...unless you say you meant it
    assert tts_client.get(speak_url(mock_game)).status_code == 200


# -- money -------------------------------------------------------------------

def test_a_clip_is_charged_to_the_game_that_played_it(tts_app, tts_client, tts, game):
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.ledger = Ledger()                # the real thing, as a live game has

    text = "The cart still smoulders."
    assert tts_client.get(speak_url(game, text=text)).status_code == 200
    led = entry.game.ledger.to_dict()
    row = led["by_role"]["tts"]
    assert row["chars"] == len(text) and row["calls"] == 1
    assert led["total_usd"] == pytest.approx(len(text) * 4.0 / 1_000_000, rel=1e-6)

    # A cache hit costs nothing, so it is charged nothing.
    assert tts_client.get(speak_url(game, text=text)).status_code == 200
    assert entry.game.ledger.by_role["tts"]["calls"] == 1


def test_narration_stops_at_the_budget(tts_app, tts_client, tts, game):
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.ledger = Ledger()
    entry.game.ledger.add_usd("dm", entry.config["budget_usd"])

    rv = tts_client.get(speak_url(game))
    assert rv.status_code == 402
    assert "budget" in rv.get_json()["error"]
    assert tts.calls == []          # refused before anything was spent


def test_a_game_this_process_no_longer_runs_still_keeps_its_tab(tts_app, tts_client, tts, game):
    """Its `Ledger` died with the process; the row is what is left to charge."""
    db = tts_app.config["DND_DB"]
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.shutdown()
    tts_app.config["DND_REGISTRY"]._games.pop(game)

    text = "A distant horn."
    assert tts_client.get(speak_url(game, text=text)).status_code == 200
    assert db.get_game(game)["cost_usd"] == pytest.approx(len(text) * 4.0 / 1_000_000, rel=1e-6)

    # And the same budget stop applies to it.
    db.set_cost(game, entry.config["budget_usd"])
    assert tts_client.get(speak_url(game, text="More.")).status_code == 402
