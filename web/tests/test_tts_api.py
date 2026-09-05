"""The narration endpoints: what they serve, what they refuse, what they charge.

Driven with `FakeTTS` (conftest) rather than Polly, so this is about the rules
around synthesis — budget, caching, refusals, the fallback the browser is
promised — not about AWS.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from llm.cost import Ledger
from tts.voices import STANDARD_ENGLISH, accent_for, cast_for, is_child_voice
from web.routes.tts import MAX_CAST_SEATS, MAX_KEY
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
    # Whether a monster is treated after synthesis or made out of standard-only
    # SSML, which decides both what its clip costs and what format it is.
    assert body["monster_fx"] is True
    tts.monster_fx = False
    assert tts_client.get("/api/tts").get_json()["monster_fx"] is False
    tts.monster_fx = True
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


def test_a_monster_is_served_as_a_wav_and_a_cache_hit_agrees(tts_client, tts, game):
    """A monster's clip is post-processed (`tts/dsp.py`) and so cannot be
    handed back in the format Polly sent it in. The type comes off the CAST, so
    the second request — answered from the cache without looking at the bytes —
    cannot disagree with the first about what it is serving."""
    url = speak_url(game, key="monster:goblin_1")
    fresh = tts_client.get(url)
    assert fresh.status_code == 200 and fresh.mimetype == "audio/wav"

    hit = tts_client.get(url)
    assert hit.status_code == 200 and hit.mimetype == "audio/wav"
    assert hit.data == fresh.data
    assert len(tts.calls) == 1                          # the second was the cache

    # And nobody else is treated, so nobody else changes format.
    assert tts_client.get(speak_url(game, key="npc")).mimetype == "audio/mpeg"

    # With the treatment off it is an ordinary clip again.
    tts.monster_fx = False
    tts.clips.clear()
    assert tts_client.get(url).mimetype == "audio/mpeg"


def test_a_monsters_size_comes_from_the_creature_not_the_slot(tts_app, tts_client, tts, game):
    """The seat says which slot a creature took; the stat block says how big it
    is. Without the second an Ogre and a Gnoll in one fight were told apart by
    spawn order alone, and the gnoll could come out the bigger of the two."""
    entry = tts_app.config["DND_REGISTRY"].get(game)
    # What a running game has: live `Combatant`s carrying their SRD size.
    entry.game.state = SimpleNamespace(combatants={
        "mon_1": SimpleNamespace(size="L"),          # an ogre
        "mon_2": SimpleNamespace(size="S"),          # a goblin
    })

    text = "You will not leave this cave alive."
    ogre = tts_client.get(speak_url(game, key="monster:mon_1", text=text))
    goblin = tts_client.get(speak_url(game, key="monster:mon_2", text=text))
    assert ogre.status_code == goblin.status_code == 200

    # The clip is keyed on the cast, so the ETag is what proves the size
    # reached it: the ogre's is the key for a Large, and not the key it would
    # have had if the route had said nothing about the creature.
    assert ogre.headers["ETag"] == '"%s"' % tts.cache_key_for(
        "monster:mon_1", text, size="L")[1]
    assert ogre.headers["ETag"] != '"%s"' % tts.cache_key_for("monster:mon_1", text)[1]
    assert goblin.headers["ETag"] == '"%s"' % tts.cache_key_for(
        "monster:mon_2", text, size="S")[1]

    # And the one thing a listener would notice.
    assert tts.cast("monster:mon_1", size="L").fx.size_pct > \
        tts.cast("monster:mon_2", size="S").fx.size_pct


def test_a_creature_the_game_cannot_name_is_cast_rather_than_refused(tts_app, tts_client,
                                                                     tts, game):
    """A combatant no snapshot holds is `DEFAULT_SIZE_BAND`, not a 500 and not
    a silence: the line still has to be spoken."""
    entry = tts_app.config["DND_REGISTRY"].get(game)
    entry.game.state = SimpleNamespace(combatants={})
    rv = tts_client.get(speak_url(game, key="monster:mon_9"))
    assert rv.status_code == 200
    assert rv.headers["ETag"] == '"%s"' % tts.cache_key_for(
        "monster:mon_9", "The cart still smoulders.")[1]


def test_the_size_is_read_from_the_row_when_the_process_no_longer_has_the_game(tts_app):
    """A finished game replayed after a restart has only its persisted
    snapshot. `size` has always been in `Combatant.to_dict`, so a row written
    before any of this still answers — which is what keeps a replay from
    re-casting and re-paying for every monster line."""
    from web.routes.tts import _creature_size_for  # noqa: PLC0415

    row = {"snapshot": {"state": {"combatants": {"mon_4": {"size": "H"}}}}}
    assert _creature_size_for(None, row, "monster:mon_4") == "H"
    assert _creature_size_for(None, row, "monster:mon_5") == ""
    assert _creature_size_for(None, row, "dm") == ""          # not a creature
    assert _creature_size_for(None, {"snapshot": None}, "monster:mon_4") == ""

    # The live combatant wins, being an attribute read where the snapshot is a
    # re-serialization of the whole board.
    entry = SimpleNamespace(game=SimpleNamespace(
        state=SimpleNamespace(combatants={"mon_4": SimpleNamespace(size="S")})))
    assert _creature_size_for(entry, row, "monster:mon_4") == "S"


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


# -- pronouns ----------------------------------------------------------------

def test_a_pc_is_cast_from_the_pronouns_its_own_party_list_states(tts_client, sample_config):
    party = [
        {"id": "pc_1", "name": "Thorin", "race": "Dwarf (Hill)", "klass": "Fighter",
         "level": 3, "pronouns": "he/him"},
        {"id": "pc_2", "name": "Vessa", "race": "Halfling (Lightfoot)", "klass": "Rogue",
         "level": 3, "pronouns": "she/her"},
        {"id": "pc_3", "name": "Crick", "race": "Halfling (Lightfoot)", "klass": "Rogue",
         "level": 3},                                    # no pronouns stated
        {"id": "pc_4", "name": "Ilbrandt", "race": "Human", "klass": "Cleric",
         "level": 3, "pronouns": "they/them"},
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
    # And so is they/them: Polly's roster is female and male, and there is no
    # third voice to deal. Stating it says who the character is; it does not
    # ask the roster for something it does not have.
    assert voice("pc_4") == cast_for("pc_4", STANDARD_ENGLISH, "Brian").voice_id

    # The DM, an NPC and a monster have no character record to read pronouns
    # from, and are cast exactly as they were before any of this.
    for key in ("dm", "npc", "monster:goblin_1"):
        assert voice(key) == cast_for(key, STANDARD_ENGLISH, "Brian").voice_id


def test_a_legacy_gender_is_still_read_and_stated_pronouns_win(tts_client, sample_config):
    """`gender` is what a party spec used to say, and a stranger's still might.

    A game persists its config and its clips are cached per cast, so dropping
    the old key would re-cast a running table mid-transcript and pay Polly
    again to do it. Where both are stated, the pronouns decide — including
    they/them, which narrows nothing: reading the old key underneath would
    quietly keep a narrowing the newer key removed.
    """
    party = [
        {"id": "pc_1", "name": "Vessa", "race": "Halfling (Lightfoot)", "klass": "Rogue",
         "level": 3, "gender": "female"},
        {"id": "pc_2", "name": "Crick", "race": "Halfling (Lightfoot)", "klass": "Rogue",
         "level": 3, "gender": "female", "pronouns": "he/him"},
        {"id": "pc_3", "name": "Pib", "race": "Human", "klass": "Cleric",
         "level": 3, "gender": "female", "pronouns": "they/them"},
    ]
    game = create(tts_client, dict(sample_config, party=party))["id"]

    def voice(key):
        return tts_client.get(speak_url(game, key=key)).headers["X-Dnd-Voice"]

    women = {v.id for v in STANDARD_ENGLISH if v.gender == "Female"}
    assert voice("pc_1") in women
    assert voice("pc_2") in {v.id for v in STANDARD_ENGLISH if v.gender == "Male"}
    assert voice("pc_3") == cast_for("pc_3", STANDARD_ENGLISH, "Brian").voice_id


def test_converting_a_config_to_pronouns_costs_nothing(tts_client, tts, sample_config):
    """The claim the migration rests on, checked rather than asserted.

    `she/her` resolves to the same pool constraint `female` did, so the cast is
    the same voice and the SSML is the same document — and the disk cache key
    is `(engine, voice id, SSML)` with no config fingerprint in it. So a
    converted scenario's clips are still hits, and nothing is re-synthesized.
    A change that quietly re-bought every clip a running table had already paid
    for would be a real cost, not a cosmetic one.
    """
    line = "The cart still smoulders."
    member = {"id": "pc_1", "name": "Vessa", "race": "Halfling (Lightfoot)",
              "klass": "Rogue", "level": 3}

    was = create(tts_client, dict(sample_config, party=[dict(member, gender="female")]))["id"]
    before = tts_client.get(speak_url(was, key="pc_1", text=line))
    assert before.status_code == 200 and len(tts.calls) == 1

    now = create(tts_client, dict(sample_config, party=[dict(member, pronouns="she/her")]))["id"]
    after = tts_client.get(speak_url(now, key="pc_1", text=line))
    assert after.status_code == 200
    assert after.headers["X-Dnd-Voice"] == before.headers["X-Dnd-Voice"]
    assert after.headers["ETag"] == before.headers["ETag"]
    assert len(tts.calls) == 1, "the converted config paid Polly for a clip it already had"


def test_the_caller_cannot_choose_the_voice(tts_client, sample_config):
    """A voice trait is read from the game, never from the request.

    This endpoint spends money: pronouns in the query string would be a way to
    walk the whole roster, minting a paid cache entry per step.
    """
    party = [{"id": "pc_1", "name": "Thorin", "race": "Dwarf (Hill)", "klass": "Fighter",
              "level": 3, "pronouns": "he/him"}]
    game = create(tts_client, dict(sample_config, party=party))["id"]

    honest = tts_client.get(speak_url(game, key="pc_1"))
    for asked in ("&pronouns=she/her", "&gender=female"):
        rv = tts_client.get(speak_url(game, key="pc_1") + asked)
        assert honest.headers["X-Dnd-Voice"] == rv.headers["X-Dnd-Voice"]
        assert honest.headers["ETag"] == rv.headers["ETag"]     # and it is the same clip


def test_pronouns_survive_the_process_that_ran_the_game(tts_app, tts_client, sample_config):
    """The party list is in the row too, so an archived game still casts right."""
    party = [{"id": "pc_1", "name": "Vessa", "race": "Halfling (Lightfoot)", "klass": "Rogue",
              "level": 3, "pronouns": "she/her"}]
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
         "level": 3, "pronouns": "he/him"},              # no age stated: an adult
        {"id": "pc_2", "name": "Wren", "race": "Human", "klass": "Rogue",
         "level": 3, "pronouns": "she/her", "age": 40},
        {"id": "pc_3", "name": "Pip", "race": "Human", "klass": "Rogue",
         "level": 3, "pronouns": "she/her", "age": "child"},
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
    """Age is read from the game, exactly as pronouns are, and for the same
    reason: this endpoint spends money."""
    party = [{"id": "pc_1", "name": "Father Bexley Crane", "race": "Dwarf (Hill)",
              "klass": "Cleric", "level": 3, "pronouns": "he/him"}]
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

    def slow(key, text, gender="", age="", *, size="", tune=None):
        time.sleep(0.2)          # every other request is admitted or refused meanwhile
        return real(key, text, gender, age, size=size, tune=tune)

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


def test_the_shipped_parties_state_pronouns_only_where_their_persona_does():
    """The rule the examples follow, kept honest.

    Inventing pronouns for a character whose persona states none would be
    writing a fact into someone else's character; leaving them off a character
    whose persona says "she" would be ignoring what is already written.

    The examples say `pronouns` and nothing says `gender` any more: the older
    key is still read (a stranger's config may use it) but a shipped party
    stating both would be a config arguing with itself.
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
            stated = member.get("pronouns", "")
            assert "gender" not in member, (path, member["name"])
            if female and not male:
                assert stated == "she/her", (path, member["name"])
            elif male and not female:
                assert stated == "he/him", (path, member["name"])
            elif not female and not male:
                assert not stated, (path, member["name"], "persona states no pronouns")
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

    def slow(key, body, gender="", age="", *, size="", tune=None):
        time.sleep(0.2)
        return real(key, body, gender, age, size=size, tune=tune)

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


def test_the_preview_narrows_on_the_key_the_panel_actually_sends(tts_client, sample_config):
    """The panel sends `pronouns` and never `gender` (`applyPartySeats`), and
    the pool is narrowed from whichever of the two the config states
    (`_pool_gender_of`). A preview that read `gender` alone would look right in
    a test that sends `gender` and cast every real request from the whole
    roster — so this sends what the panel sends, and checks it against the clip
    the paid endpoint then serves."""
    men = {v.id for v in STANDARD_ENGLISH if v.gender == "Male"}
    women = {v.id for v in STANDARD_ENGLISH if v.gender == "Female"}

    seats = cast_preview(tts_client, [
        {"id": "pc_1", "pronouns": "she/her"},
        {"id": "pc_1", "pronouns": "he/him"},
        {"id": "pc_1", "pronouns": "they/them"},
    ])["seats"]
    assert seats[0]["voice"] in women and seats[1]["voice"] in men
    # they/them narrows nothing, so it is dealt from the whole roster — and
    # that is a different answer from either of the two above.
    assert seats[2]["voice"] != seats[0]["voice"] or seats[2]["voice"] != seats[1]["voice"]

    # And the game agrees, for a party stating pronouns exactly as the panel
    # writes them.
    party = [{"id": "pc_1", "name": "Rooke", "klass": "Fighter", "level": 3,
              "pronouns": "she/her"}]
    previewed = cast_preview(tts_client, party)["seats"][0]
    game = create(tts_client, dict(sample_config, party=party))["id"]
    rv = tts_client.get(speak_url(game, key="pc_1"))
    assert rv.headers["X-Dnd-Voice"] == previewed["voice"] and previewed["voice"] in women


def test_stated_pronouns_beat_a_legacy_gender_in_the_preview_too(tts_client):
    """`_pool_gender_of`: a config that was updated to state `pronouns` beside
    an older `gender` must not keep the narrowing that update removed."""
    seat = cast_preview(tts_client, [
        {"id": "pc_1", "pronouns": "they/them", "gender": "female"},
    ])["seats"][0]
    whole_pool = cast_preview(tts_client, [{"id": "pc_1"}])["seats"][0]
    assert seat["voice"] == whole_pool["voice"]


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


def test_a_roster_that_could_not_be_listed_previews_nothing(tts_client, tts):
    """`available()` only says the credentials resolve. A failed
    `DescribeVoices`, or a language Amazon serves no voices for, leaves a
    working client with an empty pool — and `cast_for` raises on one, which on
    an anonymous route is a 500 for an ordinary deployed shape."""
    tts.voices = lambda engine="": ()
    rv = tts_client.post("/api/tts/cast", json={"party": [{"id": "pc_1"}]})
    assert rv.status_code == 200
    assert rv.get_json() == {"available": False, "seats": []}


def test_an_id_too_long_to_be_a_key_previews_what_the_game_will_do(tts_client, sample_config):
    """`speak` hashes the truncated key but finds the member by the id as
    written, so a seat whose id runs past MAX_KEY is cast there with no traits.
    Previewing the traits anyway would name a voice the game does not use —
    the one thing this route must not do."""
    long_id = "pc_" + "a" * (MAX_KEY + 10)
    party = [{"id": long_id, "name": "Rooke", "klass": "Fighter", "level": 3,
              "pronouns": "she/her"}]
    previewed = cast_preview(tts_client, party)["seats"][0]
    game = create(tts_client, dict(sample_config, party=party))["id"]
    rv = tts_client.get(speak_url(game, key=long_id))
    assert rv.status_code == 200
    assert rv.headers["X-Dnd-Voice"] == previewed["voice"]


def test_a_server_with_no_polly_previews_nothing(client):
    """A cast nobody will hear is worse than no cast: the browser's own voices
    will read the game, and they are not this roster."""
    body = client.post("/api/tts/cast", json={"party": [{"id": "pc_1"}]}).get_json()
    assert body["available"] is False and body["seats"] == []


def test_the_preview_refuses_a_body_it_cannot_read(tts_client):
    assert tts_client.post("/api/tts/cast", json={"party": "everyone"}).status_code == 400
    # Not every JSON body is an object. This route is anonymous, so a bare
    # list or string must be a refusal rather than a stack trace in the log.
    for body in ([{"id": "pc_1"}], "everyone", 7, None):
        rv = tts_client.post("/api/tts/cast", json=body)
        assert rv.status_code == 400, body
    assert tts_client.post("/api/tts/cast", data=b"not json",
                           content_type="application/json").status_code == 400
    too_many = [{"id": "pc_%d" % i} for i in range(MAX_CAST_SEATS + 1)]
    assert tts_client.post("/api/tts/cast", json={"party": too_many}).status_code == 400
    # A member that is not an object is a seat with no id, not a 500.
    seats = cast_preview(tts_client, [None, 7, {"id": "pc_1"}])["seats"]
    assert [s["voice"] for s in seats[:2]] == [None, None]


# -- the roster, and a listener's own choice for a seat ----------------------

def test_a_server_with_no_polly_lists_no_voices(client):
    """The picker is built from this, so a no here is what hides it."""
    body = client.get("/api/tts/voices").get_json()
    assert body["available"] is False and body["engines"] == {}


def test_the_roster_names_every_voice_a_seat_could_be_recast_to(tts_client, tts):
    body = tts_client.get("/api/tts/voices").get_json()
    assert body["available"] is True
    assert body["engine"] == "standard" and body["monster_engine"] == "standard"
    voices = body["engines"]["standard"]["voices"]
    assert [v.id for v in STANDARD_ENGLISH] == [v["id"] for v in voices]
    # Named the way a listener would say it, not the way Polly does.
    brian = next(v for v in voices if v["id"] == "Brian")
    assert brian["accent"] == accent_for(brian["language"]) == "British"
    assert brian["gender"] == "male"


def test_the_roster_is_per_engine_because_a_seat_can_only_move_within_one(tts_client, tts):
    """A monster on `standard` must not be offered a neural-only voice."""
    tts.engine, tts.monster_engine = "neural", "standard"
    body = tts_client.get("/api/tts/voices").get_json()
    assert sorted(body["engines"]) == ["neural", "standard"]
    # And each says what it will honour: a pitch slider that moves and does
    # nothing is worse than no pitch slider.
    assert body["engines"]["neural"]["ssml"] == ["rate"]
    assert body["engines"]["standard"]["ssml"] == ["pitch", "rate", "vtl"]


def test_a_tuned_seat_is_read_by_the_voice_the_listener_chose(tts_client, tts, game):
    plain = tts_client.get(speak_url(game))
    assert plain.status_code == 200
    cast = cast_for("dm", STANDARD_ENGLISH, "Brian", "", "standard")
    assert plain.headers["X-Dnd-Voice"] == cast.voice_id

    tuned = tts_client.get(speak_url(game) + "&voice=Amy&rate=130")
    assert tuned.status_code == 200
    assert tuned.headers["X-Dnd-Voice"] == "Amy"
    # A different clip, so it caches and revalidates on its own rather than
    # colliding with the untuned one.
    assert tuned.headers["ETag"] != plain.headers["ETag"]


def test_the_untuned_clip_keys_exactly_as_it_always_did(tts_client, tts, game):
    """An empty tune must not re-key the cache: every clip on a deployed box
    would be paid for a second time."""
    before = tts_client.get(speak_url(game)).headers["ETag"]
    after = tts_client.get(speak_url(game) + "&voice=&rate=&pitch=").headers["ETag"]
    assert after == before


def test_a_voice_the_roster_no_longer_has_falls_back_to_the_casting(tts_client, tts, game):
    """A stored tune outlives the roster it was made against. Silencing the
    seat would be the wrong answer to a voice Polly stopped serving."""
    rv = tts_client.get(speak_url(game) + "&voice=Ghost%20of%20Polly%20Past")
    assert rv.status_code == 200
    assert rv.headers["X-Dnd-Voice"] == cast_for("dm", STANDARD_ENGLISH, "Brian", "",
                                                 "standard").voice_id


def test_a_slider_dragged_past_the_end_is_clamped_not_refused(tts_client, tts, game):
    """Polly answers `InvalidSsmlException` outside 20–200%, which the page
    hears as a seat that silently stopped using server voices."""
    from tts.voices import RATE_MAX_PCT, tune_from

    assert tune_from(rate=9999).rate_pct == RATE_MAX_PCT
    assert tune_from(rate="not a number").rate_pct is None
    assert tts_client.get(speak_url(game) + "&rate=9999").status_code == 200


def test_a_tuned_clip_is_charged_and_cached_like_any_other(tts_client, tts, game):
    url = speak_url(game, text="Testing, one two.") + "&voice=Amy"
    assert tts_client.get(url).status_code == 200
    calls = len(tts.calls)
    assert tts_client.get(url).status_code == 200
    assert len(tts.calls) == calls        # the second is the cache, not Polly


# -- the monster treatment, under the listener's hand ------------------------

def test_the_roster_reports_the_bounds_a_control_may_offer(tts_client, tts):
    """From the code that enforces them, so a slider cannot be built with a
    range the server will silently clamp."""
    from tts.dsp import MAX_SIZE_PCT
    from tts.voices import PITCH_MAX_PCT, RATE_MAX_PCT, RATE_MIN_PCT

    body = tts_client.get("/api/tts/voices").get_json()
    assert body["limits"]["rate"] == {"min": RATE_MIN_PCT, "max": RATE_MAX_PCT, "auto": 100}
    assert body["limits"]["pitch"]["max"] == PITCH_MAX_PCT
    fx = body["fx"]
    assert fx["available"] is True
    assert fx["size"] == {"min": -MAX_SIZE_PCT, "max": MAX_SIZE_PCT}
    assert fx["growl"] == {"min": 0, "max": 100} and fx["cave"] == {"min": 0, "max": 100}
    # With the treatment switched off a monster is standard-only SSML instead,
    # and there is nothing on this bench to move.
    tts.monster_fx = False
    assert tts_client.get("/api/tts/voices").get_json()["fx"]["available"] is False
    tts.monster_fx = True


def monster_cast(key="monster:mon_6", size="L", **kw):
    from tts.voices import STANDARD_ENGLISH, cast_for

    return cast_for(key, STANDARD_ENGLISH, "Brian", "", "standard", size=size, **kw)


def test_the_treatment_is_the_listeners_to_change_one_field_at_a_time():
    from tts.voices import STANDARD_ENGLISH, retune, tune_from

    cast = monster_cast()
    assert cast.fx, "a monster is dealt a treatment"
    quiet = retune(cast, tune_from(growl=0, cave=0), STANDARD_ENGLISH)
    assert quiet.fx.growl_pct == 0 and quiet.fx.cave_pct == 0
    assert quiet.fx.size_pct == cast.fx.size_pct       # untouched fields stand
    big = retune(cast, tune_from(size=50), STANDARD_ENGLISH)
    assert big.fx.size_pct == 50 and big.fx.growl_pct == cast.fx.growl_pct


def test_a_treatment_asked_for_on_an_ordinary_seat_is_ignored():
    """A PC is not a monster. Switching a treatment on would change what the
    clip *is* — pcm and a WAV — which is not what a slider means."""
    from tts.voices import STANDARD_ENGLISH, cast_for, retune, tune_from

    pc = cast_for("pc_1", STANDARD_ENGLISH, "Brian", "", "standard")
    assert pc.fx is None
    assert retune(pc, tune_from(size=50, growl=90, cave=90), STANDARD_ENGLISH).fx is None


def test_a_monsters_rate_is_a_tempo_over_the_size_compensation():
    """The bug this bench found. `MonsterFX.rate_pct` is the compensation that
    undoes what the size shift does to duration, and `cast_for` multiplies the
    creature's tempo onto it. A rate override that REPLACED the rate threw the
    compensation away, and the line arrived as much too long as the creature
    was big."""
    from tts.voices import STANDARD_ENGLISH, retune, tune_from

    cast = monster_cast()
    compensation = cast.fx.rate_pct()                  # 100 + size_pct
    assert cast.rate_pct != compensation, "this creature was dealt a tempo too"

    at_100 = retune(cast, tune_from(rate=100), STANDARD_ENGLISH)
    assert at_100.rate_pct == compensation             # tempo 100%, not rate 100

    half_again = retune(cast, tune_from(rate=150), STANDARD_ENGLISH)
    assert half_again.rate_pct == round(compensation * 1.5)


def test_changing_only_the_size_keeps_how_fast_this_creature_talks():
    from tts.voices import STANDARD_ENGLISH, retune, tune_from

    cast = monster_cast()
    dealt_tempo = cast.rate_pct * 100.0 / cast.fx.rate_pct()
    bigger = retune(cast, tune_from(size=50), STANDARD_ENGLISH)
    assert bigger.rate_pct == round(150 * dealt_tempo / 100.0)


def test_an_untouched_monster_keys_exactly_as_it_was_dealt(tts_client, tts, game):
    """Recomputing the rate would round it, and a rounded rate is a different
    SSML document — a different cache key for a clip nobody asked to change."""
    url = speak_url(game, key="monster:mon_1")
    plain = tts_client.get(url)
    assert plain.status_code == 200
    assert tts_client.get(url + "&voice=").headers["ETag"] == plain.headers["ETag"]


def test_a_treated_clip_is_asked_for_and_served_as_a_wav(tts_client, tts, game):
    treated = tts_client.get(speak_url(game, key="monster:mon_1") + "&growl=90&size=30")
    assert treated.status_code == 200
    assert treated.mimetype == "audio/wav"
