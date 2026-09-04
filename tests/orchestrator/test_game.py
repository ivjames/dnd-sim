"""The full loop: run to finished, determinism, controls, budget, DM notes."""

import threading
import time

import pytest

from llm.client import LLMResponse, MockLLMClient
from orchestrator.bus import EventBus
from orchestrator.config import GameConfig
from orchestrator import game as game_mod
from orchestrator.game import SELF_REPEAT, Game

from . import fake_engine as eng


def make_game(cfg, seed=None, client=None, on_event=None):
    bus = EventBus()
    client = client or MockLLMClient(seed=seed if seed is not None else cfg.seed)
    return Game(cfg, client, bus, on_event=on_event, engine=eng), bus


def texts(bus):
    return [(e.kind, e.text) for e in bus.history()]


# --- happy path ------------------------------------------------------------


def test_game_runs_to_finished(cfg):
    game, bus = make_game(cfg)
    game.run()
    assert game.status == "finished", game.error
    kinds = {e.kind for e in bus.history()}
    assert {"system", "scene", "narration", "combat_start", "attack", "combat_end"} <= kinds
    assert bus.closed


def test_event_sequence_numbers_are_unique_and_monotonic(cfg):
    game, bus = make_game(cfg)
    game.run()
    seqs = [e.seq for e in bus.history()]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_snapshot_shape(cfg):
    game, bus = make_game(cfg)
    game.run()
    snap = game.snapshot()
    assert set(snap) >= {"state", "summary", "ledger", "status", "round"}
    assert snap["status"] == "finished"
    assert snap["state"]["combatants"]
    assert snap["ledger"]["total_usd"] > 0
    assert snap["round"] >= 1


def test_players_and_dm_both_spend_from_the_ledger(cfg):
    game, _ = make_game(cfg)
    game.run()
    roles = game.ledger.by_role
    assert "dm" in roles
    assert any(r.startswith("player:") for r in roles)
    assert game.ledger.total_usd == pytest.approx(sum(v["usd"] for v in roles.values()), rel=1e-6)


def test_combat_resolves_and_someone_takes_damage(cfg):
    game, bus = make_game(cfg)
    game.run()
    damage = [e for e in bus.history() if e.kind == "damage"]
    assert damage, "no damage was dealt in a whole fight"
    assert all("→" in e.text or "HP" in e.text for e in damage)


def test_narration_follows_the_mechanics(cfg):
    game, bus = make_game(cfg)
    game.run()
    hist = bus.history()
    first_attack = next(i for i, e in enumerate(hist) if e.kind == "attack")
    assert any(e.kind == "narration" for e in hist[first_attack:])


# --- dialogue --------------------------------------------------------------


def test_one_spoken_line_per_turn(cfg):
    """A turn is several actions; it is still one character speaking once."""
    game, bus = make_game(cfg)
    game.run()
    per_turn: dict[str, int] = {}
    actor = None
    for ev in bus.history():
        if ev.kind == "turn_start":
            actor = ev.actor
            per_turn.setdefault(actor, 0)
        elif ev.kind == "turn_end":
            actor = None
        elif ev.kind == "dialogue" and actor is not None:
            per_turn[actor] += 1
            assert per_turn[actor] <= 1, f"{actor} spoke twice in one turn"
    assert any(e.kind == "dialogue" for e in bus.history())


def test_dialogue_carries_the_speaker_out_of_band(cfg):
    game, bus = make_game(cfg)
    game.run()
    lines = [e for e in bus.history() if e.kind == "dialogue"]
    assert lines
    for ev in lines:
        speaker = ev.data.get("speaker")
        assert speaker, "dialogue must name its speaker in data"
        # ... and must not repeat it inside the text, or the UI prints it twice
        assert not ev.text.startswith(f"{speaker}:")


def test_a_speaker_does_not_repeat_itself(cfg):
    game, bus = make_game(cfg)
    game.run()
    seen: dict[str, set] = {}
    for ev in bus.history():
        if ev.kind != "dialogue":
            continue
        said = seen.setdefault(ev.actor, set())
        assert ev.text not in said, f"{ev.actor} said {ev.text!r} twice"
        said.add(ev.text)


def test_say_drops_near_repeats_and_echoes(cfg):
    game, _ = make_game(cfg)
    said = []
    game._emit_new = lambda kind, text, actor=None, data=None: said.append((actor, text))

    assert game._say("pc_1", "Ysolde", "Sir Zombie, your desecration ends here! By my oath, I commit you back to earth!")
    # the same speaker, rephrased: dropped
    assert not game._say("pc_1", "Ysolde", "Sir Zombie, your desecration ends NOW. By my oath, I commit you to proper rest!")
    # another character parroting it back: dropped
    assert not game._say("pc_2", "Crick", "Sir Zombie, your desecration ends here! By my oath, I commit you back to earth!")
    # something new: kept
    assert game._say("pc_2", "Crick", "Tracks in the gravel — somebody walked out of here carrying the reliquary.")
    assert not game._say("pc_2", "Crick", "   ")
    assert [a for a, _ in said] == ["pc_1", "pc_2"]


def test_say_keeps_a_line_that_contradicts_the_one_before_it(cfg):
    """"Not the seal" is the opposite of "the seal", not a repeat of it."""
    game, _ = make_game(cfg)
    said = []
    game._emit_new = lambda kind, text, actor=None, data=None: said.append(text)

    assert game._say("pc_1", "Ysolde", "Open the door.")
    assert game._say("pc_1", "Ysolde", "Do not open the door.")     # same speaker, reversing
    assert game._say("pc_2", "Crick", "Don't open the door!")       # and disagreeing
    assert not game._say("pc_2", "Crick", "Open the door.")         # but this is the echo
    assert len(said) == 3


@pytest.mark.parametrize(
    "negative",
    [
        "We do not open the door.",
        "We don't open the door.",
        "We don\u2019t open the door.",          # a curly apostrophe, as a model writes it
        "We cannot open the door.",
        "We can't open the door.",
        "We never open the door.",
        "We won't open the door.",
        "We shouldn't open the door.",
        "Nobody opens the door.",
        "No — we open the door another way.",
    ],
)
def test_negation_survives_the_word_filter(negative):
    """Every one of these means the opposite of "We open the door.\""""
    assert not game_mod._line_key("We open the door.")[1]
    assert game_mod._line_key(negative)[1], f"{negative!r} did not register as a negation"


@pytest.mark.parametrize("negative", ["We do not open the door.", "We cannot open the door."])
def test_the_words_alone_would_not_have_saved_a_negation(negative):
    """Which is why _say compares the flag before it compares the overlap."""
    positive, _ = game_mod._line_key("We open the door.")
    words, _ = game_mod._line_key(negative)
    assert game_mod._overlap(words, positive) >= SELF_REPEAT


@pytest.mark.parametrize(
    "first, second",
    [
        ("Heal me!", "Heal him!"),                       # different patient
        ("Attack the goblin", "Attack the orc"),         # different target
        ("I go left.", "I go right."),                   # different direction
        ("Give it to Crick.", "Give it to Nyra."),       # different hands
    ],
)
def test_say_keeps_a_line_that_changes_the_target(cfg, first, second):
    """Two-letter words carry the instruction; length is no test of meaning."""
    for speaker in ("pc_1", "pc_2"):  # the same mouth correcting itself, or another
        game, _ = make_game(cfg)
        game._emit_new = lambda *a, **k: None
        assert game._say("pc_1", "Ysolde", first)
        assert game._say(speaker, "Crick", second), f"{second!r} suppressed by {first!r}"


@pytest.mark.parametrize(
    "first, second",
    [
        ("\u041e\u0442\u0441\u0442\u0443\u043f\u0430\u0435\u043c!", "\u0412\u043f\u0435\u0440\u0451\u0434!"),   # Cyrillic: "fall back" / "forward"
        ("\u64a4\u9000\uff01", "\u524d\u3078\uff01"),                       # Japanese, and a full-width bang
        ("\u00c9coutez\u00a0: le sceau est bris\u00e9.", "Ouvrez la porte."),  # accents and a nbsp
    ],
)
def test_say_judges_lines_that_are_not_ascii(cfg, first, second):
    """An ASCII-only tokenizer empties every line and silences the whole game."""
    game, _ = make_game(cfg)
    game._emit_new = lambda *a, **k: None
    assert game._say("pc_1", "Ysolde", first)
    assert game._say("pc_2", "Crick", second), f"{second!r} suppressed by {first!r}"
    # ... and the guard still works in that script: a real repeat is still dropped
    assert not game._say("pc_2", "Crick", first)


def test_say_never_suppresses_a_line_it_cannot_read(cfg):
    """No words to compare is no evidence of repetition."""
    game, _ = make_game(cfg)
    said = []
    game._emit_new = lambda kind, text, actor=None, data=None: said.append(text)
    assert game._say("pc_1", "Ysolde", "\u2026!")
    assert game._say("pc_2", "Crick", "?!")
    assert len(said) == 2


def test_say_drops_an_identical_bark_from_another_monster(cfg):
    game, _ = make_game(cfg)
    game._emit_new = lambda *a, **k: None
    assert game._say("mon_1", "Skeleton 1", "Click.")
    assert not game._say("mon_2", "Skeleton 2", "click... click...")


# --- determinism -----------------------------------------------------------


def test_same_seed_same_events(cfg):
    runs = []
    for _ in range(2):
        game, bus = make_game(cfg)
        game.run()
        assert game.status == "finished"
        runs.append(texts(bus))
    assert runs[0] == runs[1]


def test_different_seed_diverges(cfg):
    game_a, bus_a = make_game(cfg)
    game_a.run()
    cfg_b = GameConfig.from_dict({**cfg.to_dict(), "seed": cfg.seed + 1})
    game_b, bus_b = make_game(cfg_b)
    game_b.run()
    assert texts(bus_a) != texts(bus_b)


# --- controls --------------------------------------------------------------


def test_pause_and_resume_from_another_thread(cfg):
    cfg.tempo_ms = 5
    game, bus = make_game(cfg)
    game.start()
    time.sleep(0.05)
    game.pause()
    assert game.status == "paused"
    time.sleep(0.05)
    frozen = len(bus.history())
    time.sleep(0.15)
    assert len(bus.history()) - frozen <= 1  # at most the in-flight event
    game.resume()
    assert game.status == "running"
    game.join(timeout=20)
    assert game.status == "finished"
    assert len(bus.history()) > frozen


def test_stop_from_another_thread(cfg):
    cfg.tempo_ms = 5
    game, bus = make_game(cfg)
    game.start()
    time.sleep(0.05)
    game.stop()
    game.join(timeout=20)
    assert game.status == "stopped"
    assert any(e.kind == "system" and "stopped" in e.text.lower() for e in bus.history())
    assert bus.closed


def test_stop_while_paused_releases_the_loop(cfg):
    cfg.tempo_ms = 5
    game, _ = make_game(cfg)
    game.start()
    time.sleep(0.05)
    game.pause()
    time.sleep(0.05)
    game.stop()
    game.join(timeout=20)
    assert game.status == "stopped"


def test_subscriber_receives_live_events_and_a_close_sentinel(cfg):
    game, bus = make_game(cfg)
    q = bus.subscribe()
    received = []

    def drain():
        while True:
            ev = q.get(timeout=20)
            if ev is None:
                return
            received.append(ev)

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    game.run()
    t.join(timeout=20)
    assert not t.is_alive()
    assert len(received) == len(bus.history())


# --- budget ----------------------------------------------------------------


def test_budget_exceeded_stops_the_game(cfg):
    cfg.budget_usd = 0.0000001
    game, bus = make_game(cfg)
    game.run()
    assert game.status == "budget_exceeded"
    cost = [e for e in bus.history() if e.kind == "cost"]
    assert cost and "Budget" in cost[0].text
    assert game.ledger.total_usd > 0


def test_generous_budget_does_not_trip(cfg):
    cfg.budget_usd = 100.0
    game, _ = make_game(cfg)
    game.run()
    assert game.status == "finished"


# --- dm notes --------------------------------------------------------------


class NoteWatcher:
    """Mock client that also records every prompt the DM was given."""

    def __init__(self, seed):
        self.inner = MockLLMClient(seed=seed)
        self.prompts = []
        self.note_seen = threading.Event()

    def complete(self, **kw):
        prompt = "\n".join(m.get("content", "") for m in kw.get("messages", []))
        self.prompts.append(prompt)
        if "DM NOTE FROM TABLE" in prompt:
            self.note_seen.set()
        return self.inner.complete(**kw)


def test_inject_dm_note_emits_an_event_and_reaches_the_next_dm_prompt(cfg):
    cfg.tempo_ms = 3
    client = NoteWatcher(cfg.seed)
    game, bus = make_game(cfg, client=client)
    game.start()
    time.sleep(0.05)
    game.inject_dm_note("a raven lands on the cart and watches")
    assert client.note_seen.wait(timeout=20), "note never reached a DM prompt"
    game.stop()
    game.join(timeout=20)

    note_events = [e for e in bus.history() if e.kind == "dm_note"]
    assert len(note_events) == 1
    assert "a raven lands on the cart" in note_events[0].text
    assert note_events[0].data["text"].startswith("a raven")

    with_note = [p for p in client.prompts if "DM NOTE FROM TABLE" in p]
    assert len(with_note) == 1, "the note must be delivered exactly once"
    assert "a raven lands on the cart and watches" in with_note[0]


def test_note_injected_before_start_is_delivered(cfg):
    client = NoteWatcher(cfg.seed)
    game, bus = make_game(cfg, client=client)
    game.inject_dm_note("start in medias res")
    game.run()
    assert any("DM NOTE FROM TABLE: start in medias res" in p for p in client.prompts)
    assert any(e.kind == "dm_note" for e in bus.history())


# --- fallbacks -------------------------------------------------------------


class BrokenClient:
    """Always returns unusable output, forcing every agent fallback path."""

    def complete(self, **kw):
        return LLMResponse("I refuse.", 10, 5, 0, 0, kw.get("model", "m"), "end_turn")


def test_agent_failures_fall_back_to_end_turn_and_still_finish(cfg):
    game, bus = make_game(cfg, client=BrokenClient())
    game.run()
    assert game.status == "finished", game.error
    errors = [e for e in bus.history() if e.kind == "error"]
    assert errors, "agent failures should surface as error events"
    assert any("hesitated" in e.text for e in errors)
    assert any(e.kind == "combat_end" for e in bus.history())


class ExplodingEngine:
    def __getattr__(self, name):
        raise RuntimeError("engine is on fire")


def test_engine_explosion_becomes_an_error_status(cfg):
    bus = EventBus()
    game = Game(cfg, MockLLMClient(seed=1), bus, engine=ExplodingEngine())
    game.run()
    assert game.status == "error"
    assert any(e.kind == "error" for e in bus.history())
    assert "engine is on fire" in (game.error or "")


def test_on_event_sink_is_called_for_every_event(cfg):
    seen = []
    game, bus = make_game(cfg, on_event=seen.append)
    game.run()
    assert len(seen) == len(bus.history())


def test_a_failing_sink_does_not_kill_the_game(cfg):
    def boom(_ev):
        raise ValueError("sink is broken")

    game, _ = make_game(cfg, on_event=boom)
    game.run()
    assert game.status == "finished"


# --- config / cli ----------------------------------------------------------


def test_config_roundtrip(cfg):
    assert GameConfig.from_dict(cfg.to_dict()).to_dict() == cfg.to_dict()


def test_example_configs_load():
    for name in ("goblin_ambush", "crypt"):
        c = GameConfig.load(f"examples/{name}.json")
        assert len(c.party) == 4
        assert all(p.get("persona") for p in c.party)
        assert c.encounters()
        for enc in c.encounters():
            grid = enc["grid"]
            assert len(grid["party_start"]) == 4
            assert len(grid["enemy_start"]) >= sum(m["count"] for m in enc["monsters"]) - 1


def test_cli_runs_a_mock_game(tmp_path, monkeypatch, capsys):
    import json

    from orchestrator import cli

    raw = json.loads(open("examples/goblin_ambush.json").read())
    raw["scenario"]["max_scenes"] = 1
    raw["scenario"]["beats_per_scene"] = 0
    raw["max_rounds_per_combat"] = 3
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(raw))

    monkeypatch.setattr(cli.Game, "__init__", _patched_game_init(cli.Game.__init__))
    code = cli.main(["--config", str(path), "--mock", "--tempo", "0", "--json"])
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 0
    assert len(out) > 5
    first = json.loads(out[0])
    assert set(first) == {"seq", "round", "kind", "actor", "text", "data"}


def test_cli_prose_output_names_who_is_speaking(tmp_path, monkeypatch, capsys):
    import json

    from orchestrator import cli

    raw = json.loads(open("examples/goblin_ambush.json").read())
    raw["scenario"]["max_scenes"] = 1
    raw["scenario"]["beats_per_scene"] = 1
    raw["max_rounds_per_combat"] = 3
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(raw))

    monkeypatch.setattr(cli.Game, "__init__", _patched_game_init(cli.Game.__init__))
    cli.main(["--config", str(path), "--mock", "--tempo", "0"])
    out = capsys.readouterr().out
    names = [p["name"] for p in raw["party"]]
    # a prose line, not "  [attack] Goblin 4 attacks Thorin Cragmantle: ..."
    spoken = [ln for ln in out.splitlines() if any(ln.startswith(f"{n}: ") for n in names)]
    assert spoken, "no dialogue line named its speaker"


def _patched_game_init(orig):
    """Force the CLI's Game to use the fake engine (the real one may not exist)."""

    def init(self, cfg, client, bus, on_event=None, engine=None):
        orig(self, cfg, client, bus, on_event=on_event, engine=engine or eng)

    return init
