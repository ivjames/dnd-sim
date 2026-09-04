"""The narration endpoints: what they serve, what they refuse, what they charge.

Driven with `FakeTTS` (conftest) rather than Polly, so this is about the rules
around synthesis — budget, caching, refusals, the fallback the browser is
promised — not about AWS.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlencode

import pytest

from llm.cost import Ledger
from tts.voices import STANDARD_ENGLISH, cast_for
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


# -- gender ------------------------------------------------------------------

def test_a_pc_is_cast_from_the_gender_its_own_party_list_states(tts_client, sample_config):
    party = [
        {"id": "pc_1", "name": "Thorin", "race": "Dwarf (Hill)", "klass": "Fighter",
         "level": 3, "gender": "male"},
        {"id": "pc_2", "name": "Vessa", "race": "Halfling (Lightfoot)", "klass": "Rogue",
         "level": 3, "gender": "female"},
        {"id": "pc_3", "name": "Crick", "race": "Halfling (Lightfoot)", "klass": "Rogue",
         "level": 3},                                    # no gender stated
    ]
    game = create(tts_client, dict(sample_config, party=party))["id"]

    def voice(key):
        rv = tts_client.get(speak_url(game, key=key))
        assert rv.status_code == 200
        return rv.headers["X-Dnd-Voice"]

    men = {v.id for v in STANDARD_ENGLISH if v.gender == "Male"}
    women = {v.id for v in STANDARD_ENGLISH if v.gender == "Female"}
    assert voice("pc_1") in men
    assert voice("pc_2") in women
    # Unstated is dealt from the whole pool — which is the point of leaving it
    # unstated, not a failure to state it.
    assert voice("pc_3") == cast_for("pc_3", STANDARD_ENGLISH, "Brian").voice_id

    # The DM, an NPC and a monster have no character record to read a gender
    # from, and are cast exactly as they were before any of this.
    for key in ("dm", "npc", "monster:goblin_1"):
        assert voice(key) == cast_for(key, STANDARD_ENGLISH, "Brian").voice_id


def test_the_caller_cannot_choose_the_voice(tts_client, sample_config):
    """Gender is read from the game, never from the request.

    This endpoint spends money: a gender in the query string would be a way to
    walk the whole roster, minting a paid cache entry per step.
    """
    party = [{"id": "pc_1", "name": "Thorin", "race": "Dwarf (Hill)", "klass": "Fighter",
              "level": 3, "gender": "male"}]
    game = create(tts_client, dict(sample_config, party=party))["id"]

    honest = tts_client.get(speak_url(game, key="pc_1"))
    asked = tts_client.get(speak_url(game, key="pc_1") + "&gender=female")
    assert honest.headers["X-Dnd-Voice"] == asked.headers["X-Dnd-Voice"]
    assert honest.headers["ETag"] == asked.headers["ETag"]      # and it is the same clip


def test_gender_survives_the_process_that_ran_the_game(tts_app, tts_client, sample_config):
    """The party list is in the row too, so an archived game still casts right."""
    party = [{"id": "pc_1", "name": "Vessa", "race": "Halfling (Lightfoot)", "klass": "Rogue",
              "level": 3, "gender": "female"}]
    game = create(tts_client, dict(sample_config, party=party))["id"]
    tts_app.config["DND_REGISTRY"].get(game).shutdown()
    tts_app.config["DND_REGISTRY"]._games.pop(game)

    rv = tts_client.get(speak_url(game, key="pc_1"))
    assert rv.status_code == 200
    assert rv.headers["X-Dnd-Voice"] in {v.id for v in STANDARD_ENGLISH if v.gender == "Female"}


# -- the three things a concurrent, long-lived process gets wrong -------------

def test_simultaneous_clips_cannot_walk_past_the_budget(tts_app, tts_client, tts, sample_config):
    """Different words are different cache keys, so nothing serialises them.

    The check and the charge used to sit either side of the synthesis, so eight
    spectators asking for eight different lines all read the same below-budget
    total and all eight went to Polly. A synthesis that takes a moment is what
    makes that window visible; the budget here fits exactly one clip.
    """
    one_clip = len("Line number 0.") * 4.0 / 1_000_000
    game = create(tts_client, dict(sample_config, budget_usd=one_clip * 1.5))["id"]
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.ledger = Ledger()

    real = tts.synthesize

    def slow(key, text, gender=""):
        time.sleep(0.2)          # every other request is admitted or refused meanwhile
        return real(key, text, gender)

    tts.synthesize = slow
    codes = []

    def ask(n):
        codes.append(tts_app.test_client().get(
            speak_url(game, text="Line number %d." % n)).status_code)

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert sorted(codes) == sorted([200] + [402] * 7)
    assert entry.game.ledger.total_usd == pytest.approx(one_clip, rel=1e-6)
    assert len(tts.calls) == 1


def test_a_finished_games_charges_are_written_down(tts_app, tts_client, tts, game):
    """A terminal entry stays in the registry but its monitor has returned.

    Nothing else persists after that, so a replay's charge would live in memory
    until a restart handed the budget straight back.
    """
    db = tts_app.config["DND_DB"]
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.ledger = Ledger()
    entry.game.stop()
    entry.shutdown()                       # the monitor thread is gone
    assert entry.status() in ("stopped", "finished")

    text = "The fire has burned down to embers."
    assert tts_client.get(speak_url(game, text=text)).status_code == 200

    expected = len(text) * 4.0 / 1_000_000
    assert entry.cost_usd == pytest.approx(expected, rel=1e-6)
    assert db.get_game(game)["cost_usd"] == pytest.approx(expected, rel=1e-6)


def test_a_clip_already_paid_for_outlives_the_budget(tts_app, tts_client, tts, game):
    """The budget governs spend. Re-reading a line is not spend."""
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.ledger = Ledger()
    text = "Roll for initiative!"
    assert tts_client.get(speak_url(game, text=text)).status_code == 200

    entry.game.ledger.add_usd("dm", entry.config["budget_usd"])
    assert tts_client.get(speak_url(game, text=text)).status_code == 200      # cached
    assert tts_client.get(speak_url(game, text="Something new.")).status_code == 402
    assert len(tts.calls) == 1


def test_an_unstated_budget_is_the_default_not_a_blank_cheque(tts_app, tts_client, tts,
                                                              sample_config):
    """A game created without `budget_usd` persists a config that has no such
    key, so after a restart there is nothing in the row to read. Reading that
    as zero — and zero as "no cap" — removed the cap from exactly the games
    that never asked for one."""
    cfg = dict(sample_config)
    cfg.pop("budget_usd")
    game = create(tts_client, cfg)["id"]
    tts_app.config["DND_REGISTRY"].get(game).shutdown()
    tts_app.config["DND_REGISTRY"]._games.pop(game)          # as after a restart

    db = tts_app.config["DND_DB"]
    assert "budget_usd" not in (db.get_game(game)["config"] or {})
    assert tts_client.get(speak_url(game, text="A little.")).status_code == 200

    db.set_cost(game, 1.00)                                  # GameConfig's default
    assert tts_client.get(speak_url(game, text="A little more.")).status_code == 402


def test_a_zero_budget_refuses_everything(tts_app, tts_client, tts, sample_config):
    """`Game._check_budget` halts at `total_usd >= budget_usd`, so a zero budget
    is a game already over — not a game with no ceiling."""
    for budget in (0, -1):
        game = create(tts_client, dict(sample_config, budget_usd=budget))["id"]
        rv = tts_client.get(speak_url(game, text="Anything at all."))
        assert rv.status_code == 402, budget
    assert tts.calls == []


def test_reconfiguring_the_server_retires_the_browsers_copies(tts_client, tts):
    """Clip URLs are immutable for a year and name none of the settings that
    decide the audio, so the probe hands the page a token that moves when they
    do."""
    assert tts_client.get("/api/tts").get_json()["config"] == "fake-config"

    # The real one moves with the engine, the language, the DM's voice and the
    # roster — and with nothing else, so it is stable across a restart.
    from tts.cache import AudioCache
    from tts.client import PollyTTS

    def svc(**kw):
        s = PollyTTS(AudioCache("/nonexistent", 0), client=object(), **kw)
        s._voices = tuple(STANDARD_ENGLISH)
        return s

    base = svc().config_id()
    assert base and base == svc().config_id()
    assert svc(engine="neural").config_id() != base
    assert svc(dm_voice="Joanna").config_id() != base
    assert svc(language="en-GB").config_id() != base
