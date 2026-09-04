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
from tts.voices import STANDARD_ENGLISH, accent_for, cast_for, is_child_voice
from web.routes.tts import MAX_CAST_SEATS
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
    # Two engines, two rates: advertising only the table's describes a game's
    # spend wrongly wherever monsters do much of the talking.
    tts.engine, tts.monster_engine = "neural", "standard"
    tts.price_per_million = 16.0
    rates = tts_client.get("/api/tts").get_json()
    assert rates["price_per_million_chars"] == 16.0
    assert rates["monster_price_per_million_chars"] == 4.0

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
    row = led["by_role"]["narrator"]
    assert row["chars"] == len(text) and row["clips"] == 1
    assert led["total_usd"] == pytest.approx(len(text) * 4.0 / 1_000_000, rel=1e-6)

    # Narration is not a model call and must not be counted as one: the
    # orchestrator's end-of-game line reports this figure as "model calls".
    assert row["calls"] == 0 and led["calls"] == 0

    # A cache hit costs nothing, so it is charged nothing.
    assert tts_client.get(speak_url(game, text=text)).status_code == 200
    assert entry.game.ledger.by_role["narrator"]["clips"] == 1


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


# -- age ---------------------------------------------------------------------

def test_a_pc_is_only_cast_as_a_child_if_its_own_party_list_says_so(tts_client, sample_config):
    """The report this fixes: a cleric called Father Bexley read by a
    nine-year-old, because Polly's children's voices sat in the pool every seat
    was dealt from."""
    party = [
        {"id": "pc_1", "name": "Father Bexley Crane", "race": "Dwarf (Hill)", "klass": "Cleric",
         "level": 3, "gender": "male"},                  # no age stated: an adult
        {"id": "pc_2", "name": "Wren", "race": "Human", "klass": "Rogue",
         "level": 3, "gender": "female", "age": 40},
        {"id": "pc_3", "name": "Pip", "race": "Human", "klass": "Rogue",
         "level": 3, "gender": "female", "age": "child"},
    ]
    game = create(tts_client, dict(sample_config, party=party))["id"]

    def voice(key):
        rv = tts_client.get(speak_url(game, key=key))
        assert rv.status_code == 200
        return rv.headers["X-Dnd-Voice"]

    assert not is_child_voice(voice("pc_1"))
    assert not is_child_voice(voice("pc_2"))
    assert is_child_voice(voice("pc_3"))
    # The DM, an NPC and a monster have no character record to read an age
    # from, and are adults for the same reason an unstated seat is.
    for key in ("dm", "npc", "monster:goblin_1"):
        assert not is_child_voice(voice(key))


def test_the_caller_cannot_ask_to_be_a_child(tts_client, sample_config):
    """Age is read from the game, exactly as gender is, and for the same
    reason: this endpoint spends money."""
    party = [{"id": "pc_1", "name": "Father Bexley Crane", "race": "Dwarf (Hill)",
              "klass": "Cleric", "level": 3, "gender": "male"}]
    game = create(tts_client, dict(sample_config, party=party))["id"]

    honest = tts_client.get(speak_url(game, key="pc_1"))
    asked = tts_client.get(speak_url(game, key="pc_1") + "&age=child")
    assert honest.headers["X-Dnd-Voice"] == asked.headers["X-Dnd-Voice"]
    assert honest.headers["ETag"] == asked.headers["ETag"]      # and it is the same clip


def test_an_age_the_config_states_oddly_is_still_read(tts_client, sample_config):
    """`normalize_age` is the only thing that decides what an answer means, so
    the web layer passes `9`, `"9"` and `"kid"` through unchanged rather than
    having an opinion of its own."""
    party = [{"id": "pc_%d" % i, "name": "Pip %d" % i, "race": "Human", "klass": "Rogue",
              "level": 3, "age": said}
             for i, said in enumerate((9, "9", "kid", "grown-up", "", None), start=1)]
    game = create(tts_client, dict(sample_config, party=party))["id"]

    def voice(key):
        rv = tts_client.get(speak_url(game, key=key))
        assert rv.status_code == 200
        return rv.headers["X-Dnd-Voice"]

    assert all(is_child_voice(voice("pc_%d" % i)) for i in (1, 2, 3))
    assert not any(is_child_voice(voice("pc_%d" % i)) for i in (4, 5, 6))


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

    def slow(key, text, gender="", age=""):
        time.sleep(0.2)          # every other request is admitted or refused meanwhile
        return real(key, text, gender, age)

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
        s._voices = {e: (tuple(STANDARD_ENGLISH), None)          # (pool, believe-until)
                     for e in ("standard", "neural", "long-form", "generative")}
        return s

    base = svc().config_id()
    assert base and base == svc().config_id()
    assert svc(engine="long-form").config_id() != base
    assert svc(monster_engine="generative").config_id() != base    # both engines count
    assert svc(dm_voice="Joanna").config_id() != base
    assert svc(language="en-GB").config_id() != base


def test_the_shipped_parties_state_a_gender_only_where_their_persona_does():
    """The rule the examples follow, kept honest.

    Inventing a gender for a character whose persona states none would be
    writing a fact into someone else's character; leaving one off a character
    whose persona says "she" would be ignoring what is already written.
    """
    import glob
    import json
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    said = re.compile(r"\b(she|her|herself|hers)\b|^(Dame|Sister|Mother) ", re.I)
    his = re.compile(r"\b(he|him|himself|his)\b|^(Brother|Father) ", re.I)

    seen = 0
    for path in sorted(glob.glob(os.path.join(root, "examples", "*.json"))):
        for member in json.load(open(path, encoding="utf-8"))["party"]:
            seen += 1
            text = member["name"] + " " + member.get("persona", "")
            female, male = bool(said.search(text)), bool(his.search(text))
            stated = member.get("gender", "")
            if female and not male:
                assert stated == "female", (path, member["name"])
            elif male and not female:
                assert stated == "male", (path, member["name"])
            elif not female and not male:
                assert not stated, (path, member["name"], "persona states no gender")
    assert seen >= 28


def test_a_stranger_cannot_raise_the_ceiling_by_asking(tts_app, tts_client, tts,
                                                       sample_config, monkeypatch):
    """`budget_usd` arrives in the request body on a route that takes no
    credential (TTS-COSTS.md §1), so narration stops at the lower of the game's
    own budget and one this server owns."""
    monkeypatch.setenv("DND_TTS_MAX_USD", "0.0001")     # ~25 characters
    game = create(tts_client, dict(sample_config, budget_usd=1_000_000))["id"]
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.ledger = Ledger()

    assert tts_client.get(speak_url(game, text="Twenty characters..")).status_code == 200
    over = tts_client.get(speak_url(game, text="And now some more of them."))
    assert over.status_code == 402
    assert "server ceiling" in over.get_json()["error"]

    # The game's own budget still binds when it is the lower of the two, and
    # the message says which one stopped it.
    monkeypatch.setenv("DND_TTS_MAX_USD", "1000")
    poor = create(tts_client, dict(sample_config, budget_usd=0))["id"]
    said = tts_client.get(speak_url(poor, text="Anything.")).get_json()["error"]
    assert "budget of $0.00" in said


def test_a_bad_ceiling_is_the_default_not_a_crash(tts_app, tts_client, tts, game, monkeypatch):
    monkeypatch.setenv("DND_TTS_MAX_USD", "not a number")
    assert tts_client.get(speak_url(game)).status_code == 200


def test_two_tabs_after_one_line_are_one_clip_not_one_refusal(tts_app, tts_client, tts,
                                                              sample_config):
    """Identical requests must not each reserve the cost of the same clip.

    With budget for exactly one synthesis, the second asker would be refused
    for a clip the first is a moment from making free — and the page reads a
    402 as settled and gives up on server voices for the whole game.
    """
    text = "The cart still smoulders."
    one_clip = len(text) * 4.0 / 1_000_000
    game = create(tts_client, dict(sample_config, budget_usd=one_clip * 1.5))["id"]
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.ledger = Ledger()

    real = tts.render

    def slow(key, body, gender="", age=""):
        time.sleep(0.2)
        return real(key, body, gender, age)

    tts.render = slow
    codes = []

    def ask():
        codes.append(tts_app.test_client().get(speak_url(game, text=text)).status_code)

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert codes == [200, 200, 200, 200]
    assert len(tts.calls) == 1                                  # paid for once
    assert entry.game.ledger.total_usd == pytest.approx(one_clip, rel=1e-6)


def test_the_charge_lands_before_the_reservation_is_released(tts_app, tts_client, tts,
                                                             sample_config):
    """A waiter must never see a ledger that does not yet know about a clip
    that has already been synthesized.

    The window is between releasing the reservation and recording the charge,
    so the delay has to go inside the charge — a slow synthesis holds the
    reservation and proves nothing.
    """
    one_clip = len("Line number 0.") * 4.0 / 1_000_000
    game = create(tts_client, dict(sample_config, budget_usd=one_clip * 1.5))["id"]
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.ledger = Ledger()

    charging = threading.Event()
    real_add = entry.game.ledger.add_usd

    def slow_add(role, usd, **counters):
        charging.set()
        time.sleep(0.25)                 # the gap, held open
        return real_add(role, usd, **counters)

    entry.game.ledger.add_usd = slow_add
    codes = []

    def ask(n):
        codes.append(tts_app.test_client().get(
            speak_url(game, text="Line number %d." % n)).status_code)

    first = threading.Thread(target=ask, args=(0,))
    first.start()
    assert charging.wait(timeout=5)
    second = threading.Thread(target=ask, args=(1,))     # a DIFFERENT line
    second.start()
    for t in (first, second):
        t.join(timeout=15)

    assert sorted(codes) == [200, 402]
    assert entry.game.ledger.total_usd == pytest.approx(one_clip, rel=1e-6)


def test_a_nonstandard_engine_with_no_roster_says_so(tts_app, tts_client, tts):
    """The built-in roster is standard-only. Casting one of those voices with
    `Engine=neural` is the 502 loop engine-aware SSML exists to avoid."""
    tts.voices = lambda engine="": ()
    body = tts_client.get("/api/tts").get_json()
    assert body["available"] is False and "could be listed" in body["reason"]


def test_a_budget_that_cannot_be_compared_is_not_a_budget(tts_client, tts, sample_config):
    """`float("NaN")` passes coercion, and NaN compares False against
    everything — so a NaN budget is not a large budget, it is no budget check
    at all. It defeats `Game._check_budget` too, not just this endpoint."""
    for bad in ("NaN", "nan", "inf", "-inf", "Infinity"):
        rv = tts_client.post("/api/games", json={"config": dict(sample_config, budget_usd=bad)})
        assert rv.status_code == 400, bad
        assert "finite" in rv.get_json()["error"], bad
    assert tts.calls == []


def test_a_nan_budget_already_in_the_row_is_not_a_blank_cheque(tts_app, tts_client, tts,
                                                               sample_config):
    """Rows written before that check exists, or by hand."""
    game = create(tts_client, sample_config)["id"]
    db = tts_app.config["DND_DB"]
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.config = dict(entry.config, budget_usd=float("nan"))
    entry.game.ledger = Ledger()
    entry.game.ledger.add_usd("dm", 50.0)          # far past any sane ceiling

    rv = tts_client.get(speak_url(game, text="Anything at all."))
    assert rv.status_code == 402
    assert tts.calls == []
    assert db.get_game(game) is not None


def test_concurrent_charges_do_not_lose_each_other(tts_app, tts_client, tts, sample_config):
    """`persist_snapshot()` reads the ledger total and writes it ABSOLUTELY.

    The window is between that read and that write: a charge whose write stalls
    there resumes and lands its now-stale total on top of a newer one. The
    in-memory ledger stays right and the row gives the difference back at the
    next restart, so the delay has to go inside the write, not before it.
    """
    game = create(tts_client, dict(sample_config, budget_usd=10.0))["id"]
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.ledger = Ledger()
    db = tts_app.config["DND_DB"]

    real_save = db.save_snapshot
    seen = []
    newer_landed = threading.Event()

    def racing_save(game_id, snapshot, status=None, cost_usd=None):
        mine = len(seen)
        seen.append(cost_usd)
        if mine == 0:
            newer_landed.wait(timeout=0.5)      # let a newer write land first
        real_save(game_id, snapshot, status=status, cost_usd=cost_usd)
        if mine == 1:
            newer_landed.set()

    db.save_snapshot = racing_save
    try:
        lines = ["Line number 0.", "Line number 1."]
        threads = [threading.Thread(
            target=lambda t=t: tts_app.test_client().get(speak_url(game, text=t)))
            for t in lines]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
    finally:
        db.save_snapshot = real_save

    expected = sum(len(t) for t in lines) * 4.0 / 1_000_000
    assert entry.game.ledger.total_usd == pytest.approx(expected, rel=1e-6)
    # The row must agree with the ledger, not with whichever write landed last.
    assert db.get_game(game)["cost_usd"] == pytest.approx(expected, rel=1e-6)


def test_a_seat_is_reserved_at_its_own_engines_rate(tts_app, tts_client, tts, sample_config):
    """A monster renders on a different engine from the table, so it is billed
    at a different rate. Reserving at the table's rate either refuses a clip
    the game can afford or admits one it cannot."""
    tts.engine, tts.monster_engine = "neural", "standard"
    tts.price_per_million = 16.0                       # the table's rate

    text = "Fee fi fo fum."
    monster_cost = len(text) * 4.0 / 1_000_000         # what a monster actually costs
    game = create(tts_client, dict(sample_config, budget_usd=monster_cost * 1.5))["id"]
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.ledger = Ledger()

    # Affordable at the monster's own rate, four times over budget at the
    # table's — so reserving at the table's rate refuses it.
    rv = tts_client.get(speak_url(game, key="monster:goblin_1", text=text))
    assert rv.status_code == 200, rv.get_json()
    assert entry.game.ledger.total_usd == pytest.approx(monster_cost, rel=1e-6)


def test_the_probe_checks_every_engine_the_table_uses(tts_client, tts):
    """A monster engine with no roster is a 503 on its first monster line,
    which the page reads as settled and uses to switch server voices off for
    the whole game — taking the seats that were working with it."""
    tts.engine, tts.monster_engine = "standard", "neural"
    rosters = {"standard": tuple(STANDARD_ENGLISH), "neural": ()}
    tts.voices = lambda engine="": rosters[engine or tts.engine]

    body = tts_client.get("/api/tts").get_json()
    assert body["available"] is False
    assert "neural" in body["reason"]                  # names the one that is missing

    rosters["neural"] = tuple(STANDARD_ENGLISH)
    assert tts_client.get("/api/tts").get_json()["available"] is True


def test_a_charge_is_not_overwritten_by_another_snapshot_writer(tts_app, tts_client, tts,
                                                                sample_config):
    """`persist_snapshot` has four callers — the game thread, the monitor, the
    control routes and a narration charge — and it writes an ABSOLUTE total.
    Serializing charge against charge left the other three able to persist a
    ledger read from before the charge landed."""
    game = create(tts_client, dict(sample_config, budget_usd=10.0))["id"]
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.ledger = Ledger()
    db = tts_app.config["DND_DB"]

    # Another writer — here the control route's — reads the ledger, stalls, and
    # commits after the charge has persisted.
    real_save = db.save_snapshot
    reading = threading.Event()
    charged = threading.Event()

    def slow_save(game_id, snapshot, status=None, cost_usd=None):
        if not reading.is_set():
            reading.set()
            charged.wait(timeout=2)
        real_save(game_id, snapshot, status=status, cost_usd=cost_usd)

    db.save_snapshot = slow_save
    try:
        other = threading.Thread(target=entry.persist_snapshot)
        other.start()
        assert reading.wait(timeout=5)
        text = "The fire gutters."
        assert tts_client.get(speak_url(game, text=text)).status_code == 200
        charged.set()
        other.join(timeout=10)
    finally:
        db.save_snapshot = real_save

    expected = len(text) * 4.0 / 1_000_000
    assert entry.game.ledger.total_usd == pytest.approx(expected, rel=1e-6)
    assert db.get_game(game)["cost_usd"] == pytest.approx(expected, rel=1e-6)


def test_a_line_polly_refuses_costs_nothing_and_holds_nothing(tts_app, tts_client, tts,
                                                              game, caplog):
    """A monster line failing is the refusal this app is least likely to
    notice: the page speaks that one line in the browser's own voice, so the
    spectator hears speech either way and only `dndsim logs` shows it. The
    least it can do is be free — nothing charged, nothing cached under a key
    served `immutable` for a year, and no budget still held against the game
    once the request is over.
    """
    from web.routes.tts import _RESERVED

    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.ledger = Ledger()
    tts.engine, tts.monster_engine = "neural", "standard"   # the shipped split
    tts.fail = "InvalidSsmlException: vtl on neural"

    with caplog.at_level("WARNING", logger=tts_app.logger.name):
        rv = tts_client.get(speak_url(game, key="monster:goblin_1"))
    assert rv.status_code == 502

    # Which seat, on which engine, in which voice. A monster fails on its own
    # engine with SSML no other seat writes, and nothing else in the system
    # says so — the page speaks the line and the spectator hears speech.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "monster:goblin_1" in logged and "standard/" in logged

    assert entry.game.ledger.total_usd == 0.0
    assert "narrator" not in entry.game.ledger.by_role
    assert _RESERVED.get(game) is None            # the reservation was given back

    # And the failure is not remembered: the same line is asked for again
    # rather than being answered from a cache entry that never held audio.
    tts.fail = ""
    again = tts_client.get(speak_url(game, key="monster:goblin_1"))
    assert again.status_code == 200
    assert entry.game.ledger.by_role["narrator"]["clips"] == 1


# -- the cast preview --------------------------------------------------------

def cast_preview(client, party):
    rv = client.post("/api/tts/cast", json={"party": party})
    assert rv.status_code == 200, rv.get_data(as_text=True)
    return rv.get_json()


def test_the_panel_is_told_who_will_read_each_seat(tts_client):
    """Voice, accent and gender, for the seats the panel is about to buy."""
    body = cast_preview(tts_client, [
        {"id": "pc_1", "gender": "male"},
        {"id": "pc_2", "gender": "female"},
    ])
    assert body["available"] is True
    seats = body["seats"]
    assert [s["id"] for s in seats] == ["pc_1", "pc_2"]
    men = {v.id for v in STANDARD_ENGLISH if v.gender == "Male"}
    women = {v.id for v in STANDARD_ENGLISH if v.gender == "Female"}
    assert seats[0]["voice"] in men and seats[0]["gender"] == "male"
    assert seats[1]["voice"] in women and seats[1]["gender"] == "female"
    for seat in seats:
        assert seat["accent"] == accent_for(seat["language"])
        assert seat["accent"] and not seat["accent"].startswith("en-")


def test_the_preview_is_the_casting_the_game_will_actually_use(tts_client, sample_config):
    """Not a second implementation that agrees until someone edits one of them.

    The panel would be worse than nothing if it named a voice the game then
    did not use, so the preview is checked against the clip the paid endpoint
    serves for the same seat.
    """
    party = [
        {"id": "pc_1", "name": "Thorin", "klass": "Fighter", "level": 3, "gender": "male"},
        {"id": "pc_2", "name": "Ivy", "klass": "Rogue", "level": 3, "age": "child"},
    ]
    previewed = cast_preview(tts_client, party)["seats"]
    game = create(tts_client, dict(sample_config, party=party))["id"]
    for seat in previewed:
        rv = tts_client.get(speak_url(game, key=seat["id"]))
        assert rv.status_code == 200
        assert rv.headers["X-Dnd-Voice"] == seat["voice"], seat["id"]


def test_asking_for_a_child_changes_the_preview_before_the_game_exists(tts_client):
    """The panel's one control has to visibly do something."""
    adult = cast_preview(tts_client, [{"id": "pc_1"}])["seats"][0]
    child = cast_preview(tts_client, [{"id": "pc_1", "age": "child"}])["seats"][0]
    assert not is_child_voice(adult["voice"])
    assert is_child_voice(child["voice"])


def test_a_seat_with_no_id_is_not_quietly_cast_as_the_narrator(tts_client):
    """`cast_for` reads an empty key as the DM; showing the narrator's voice
    against a player's name would be a confident wrong answer."""
    seats = cast_preview(tts_client, [{"name": "nameless"}, {"id": "pc_2"}])["seats"]
    assert seats[0]["voice"] is None
    assert seats[1]["voice"]


def test_the_preview_spends_nothing_and_renders_nothing(tts_client, tts):
    """It is `cast_for` over a roster, not a clip: no synthesis, no cache."""
    before = list(tts.calls)
    cast_preview(tts_client, [{"id": "pc_1"}, {"id": "pc_2"}, {"id": "monster:goblin_1"}])
    assert tts.calls == before
    assert not tts.clips


def test_a_server_with_no_polly_previews_nothing(client):
    """A cast nobody will hear is worse than no cast: the browser's own voices
    will read the game, and they are not this roster."""
    body = client.post("/api/tts/cast", json={"party": [{"id": "pc_1"}]}).get_json()
    assert body["available"] is False and body["seats"] == []


def test_the_preview_refuses_a_body_it_cannot_read(tts_client):
    assert tts_client.post("/api/tts/cast", json={"party": "everyone"}).status_code == 400
    too_many = [{"id": "pc_%d" % i} for i in range(MAX_CAST_SEATS + 1)]
    assert tts_client.post("/api/tts/cast", json={"party": too_many}).status_code == 400
    # A member that is not an object is a seat with no id, not a 500.
    seats = cast_preview(tts_client, [None, 7, {"id": "pc_1"}])["seats"]
    assert [s["voice"] for s in seats[:2]] == [None, None]
