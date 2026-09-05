"""PlayerAgent / DMAgent parsing, retries, prompt hygiene, and view size."""

import json
import pathlib

import pytest

from agents.common import AgentOutputError, rules_digest
from agents.dm import DMAgent
from agents import player
from agents.player import PlayerAgent
from agents.summarizer import summarize
from agents.views import dm_view, party_summary, player_view, pronouns_for, render_actions
from llm.client import LLMResponse, MockLLMClient
from llm.cost import Ledger

from . import fake_engine as eng


class ScriptedClient:
    """Returns canned texts in order; records every prompt it saw."""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.prompts = []
        self.systems = []
        self.temperatures = []

    def complete(self, *, model, system, messages, max_tokens, temperature=0.7, json_only=False):
        self.systems.append(system)
        self.temperatures.append(temperature)
        self.prompts.append(messages[-1]["content"] if messages else "")
        text = self.texts.pop(0) if self.texts else "{}"
        return LLMResponse(text, 100, 20, 0, 0, model, "end_turn")


# --- fixtures --------------------------------------------------------------


def make_state(n_enemies=5):
    rng = eng.RNG(1)
    combatants = {}
    for i, (name, klass, who) in enumerate(
        [
            ("Thorin", "Fighter", {"gender": "male"}),          # implied he/him
            ("Vessa", "Rogue", {"pronouns": "they/them"}),      # stated
            ("Marigold", "Cleric", {"gender": "female"}),       # implied she/her
            ("Ilbrandt", "Wizard", {}),                         # nothing said
        ]
    ):
        sheet = eng.build_character(
            dict({"id": f"pc_{i+1}", "name": name, "klass": klass, "level": 3,
                  "persona": "brave"}, **who),
            rng,
        )
        combatants[sheet.id] = eng.Combatant(
            id=sheet.id,
            name=name,
            side="party",
            kind="pc",
            sheet=sheet,
            hp=sheet.max_hp - i,
            max_hp=sheet.max_hp,
            ac=sheet.ac,
            speed=30,
            abilities=sheet.abilities,
            proficiency=2,
            position=(1, 4 + i),
            resources={"spell_slots": {1: 2}, "second_wind": 1},
            turn={"action": False, "bonus": False, "movement_left": 30},
        )
    for j in range(n_enemies):
        c = eng.monster_to_combatant("Goblin", f"mon_{j+1}", rng)
        c.position = (9 + (j % 2), 3 + j)
        c.conditions = [eng.Condition("prone")] if j == 0 else []
        combatants[c.id] = c
    return eng.GameState(
        seed=1,
        rng=rng.state(),
        mode="combat",
        round=2,
        turn_index=0,
        combatants=combatants,
        initiative=[(cid, 10) for cid in combatants],
        grid=eng.Grid(12, 10),
        scene={"title": "Ambush", "description": "Mud and gorse.", "objectives": ["Survive"], "location": "Kingsroad"},
        event_seq=0,
    )


def events(n=14):
    return [eng.Event(i, 1, "attack", "pc_1", f"Thorin swings at Goblin {i}: hit for 6", {}) for i in range(n)]


def templates():
    return [
        eng.ActionTemplate("a1", "attack", "Attack Goblin 1 (+5, 1d8+3)", {"target": "mon_1"}, [], "action"),
        eng.ActionTemplate("a2", "move", "Move up to your speed", {"suggested": [[3, 4]]}, ["path"], "movement"),
        eng.ActionTemplate("a3", "end_turn", "End turn", {}, [], "free"),
    ]


def agent(client, sheet=None):
    state = make_state()
    return PlayerAgent(client, "m", sheet or state.combatants["pc_1"].sheet, Ledger(), engine=eng)


# --- views -----------------------------------------------------------------


def test_player_view_is_compact_and_hides_enemy_hp():
    state = make_state()  # 4 PCs + 5 goblins = 9 combatants
    view = player_view(state, "pc_1", events(), "The party was ambushed on the road.")
    approx_tokens = len(view) / 4
    assert approx_tokens < 700, f"player view too big: ~{approx_tokens:.0f} tokens"
    assert "Goblin" in view and "prone" in view
    # banded, not numeric, for others
    assert "7/7" not in view
    assert any(b in view for b in ("healthy", "wounded", "bloodied", "critical"))
    assert "SCENE:" in view and "RECENT:" in view
    assert view.count("\n- ") <= 12  # recent events capped


def test_dm_view_shows_exact_numbers_and_positions():
    state = make_state()
    view = dm_view(state, events(3), "")
    assert "7/7" in view
    assert "(9,3)" in view


def test_every_way_a_seat_can_answer_reaches_the_view():
    """The four cases a party spec can present, resolved through `pronouns_for`
    and into the fixture the view tests below run on. Which of `pronouns` and
    `gender` wins when a spec states both is not this — that is
    `test_narration_attribution.py::test_stated_pronouns_beat_a_legacy_gender_here_too`,
    and no seat here states both."""
    state = make_state()
    assert pronouns_for(state.combatants["pc_1"]) == "he/him"      # implied by gender
    assert pronouns_for(state.combatants["pc_2"]) == "they/them"   # stated outright
    assert pronouns_for(state.combatants["pc_3"]) == "she/her"     # implied by gender
    assert pronouns_for(state.combatants["pc_4"]) == "they/them"   # nothing said


def test_the_player_view_carries_the_pronoun_column_the_dm_view_has():
    """A player talks about their allies and the monsters both, and infers a
    gender from a name exactly as readily as the DM does."""
    view = player_view(make_state(), "pc_1", events(), "")
    assert "COMBATANTS (name | pronouns | side | health | pos | dist | conditions)" in view
    def row(cid):
        return next(l for l in view.splitlines() if l.startswith(f"{cid} "))

    for cid, pron in [("pc_1", "he/him"), ("pc_2", "they/them"),
                      ("pc_3", "she/her"), ("pc_4", "they/them")]:
        assert f"| {pron} |" in row(cid), cid
    # A monster states nothing and is not guessed at.
    assert "| they/them |" in row("mon_1")


def test_the_roster_that_opens_a_scene_introduces_each_character_properly():
    rows = party_summary(make_state()).splitlines()
    assert rows[0].startswith("Thorin (he/him, ")
    assert "Ilbrandt (they/them, " in "\n".join(rows)


def test_render_actions_format():
    text = render_actions(templates())
    assert text.splitlines()[0].startswith("[a1] Attack Goblin 1")
    assert "needs=['path']" in text and "suggested=" in text


def test_event_lines_name_the_speaker_of_a_dialogue_event():
    state = make_state()
    recent = [
        eng.Event(seq=1, round=1, kind="dialogue", actor="pc_1", text="Hold the line.",
                  data={"speaker": "Thorin"}),
        eng.Event(seq=2, round=1, kind="attack", actor="pc_1", text="Thorin attacks.", data={}),
    ]
    view = dm_view(state, recent, "")
    assert "Thorin: Hold the line." in view
    assert "Thorin attacks." in view


# --- player agent ----------------------------------------------------------


def test_choose_action_parses_fenced_json():
    client = ScriptedClient('```json\n{"action": "a1", "params": {}, "speech": "For the road!"}\n```')
    action = agent(client).choose_action("view", templates())
    assert (action.actor, action.template_id, action.speech) == ("pc_1", "a1", "For the road!")
    assert isinstance(action, eng.Action)


def test_choose_action_clamps_speech_to_the_word_cap():
    speech = " ".join(["word"] * 80)
    client = ScriptedClient('{"action": "a1", "params": {}, "speech": "%s"}' % speech)
    action = agent(client).choose_action("view", templates())
    assert len(action.speech.split()) <= player.SPEECH_WORDS + 1  # plus the ellipsis marker


def test_choose_action_without_speech_asks_for_none_and_keeps_none():
    client = ScriptedClient('{"action": "a1", "params": {}, "speech": "Again, villain!"}')
    action = agent(client).choose_action("view", templates(), speak=False)
    assert action.speech is None
    assert '"speech": null' in client.prompts[0]
    assert "already spoken this turn" in client.prompts[0]


def test_choose_action_with_speech_offers_the_word_cap():
    client = ScriptedClient('{"action": "a1", "params": {}, "speech": "Ware the flank!"}')
    action = agent(client).choose_action("view", templates())
    assert action.speech == "Ware the flank!"
    assert f"{player.SPEECH_WORDS} words max" in client.prompts[0]


def test_choose_action_retries_once_on_bad_id_then_succeeds():
    client = ScriptedClient('{"action": "a99", "params": {}}', '{"action": "a3", "params": {}}')
    action = agent(client).choose_action("view", templates())
    assert action.template_id == "a3"
    assert len(client.prompts) == 2
    assert "REJECTED" in client.prompts[1] and "a99" in client.prompts[1]


def test_choose_action_raises_after_two_failures():
    client = ScriptedClient("not json at all", '{"action": "nope"}')
    with pytest.raises(AgentOutputError):
        agent(client).choose_action("view", templates())
    assert len(client.prompts) == 2


def test_choose_action_rejects_missing_required_params():
    client = ScriptedClient('{"action": "a2", "params": {}}', '{"action": "a2", "params": {"path": [[3,4]]}}')
    action = agent(client).choose_action("view", templates())
    assert action.params["path"] == [[3, 4]]
    assert "needs a 'path'" in client.prompts[1]


def test_player_prompt_carries_persona_rules_and_shape():
    client = ScriptedClient('{"action": "a3", "params": {}}')
    a = agent(client)
    a.choose_action("THE VIEW", templates())
    system = client.systems[0][0]["text"]
    assert system.startswith("You are one player")
    assert "brave" in system  # persona
    assert rules_digest()[:40] in system
    assert "NEVER roll dice" in system or "never roll" in system.lower()
    assert client.systems[0][0]["cache_control"] == {"type": "ephemeral"}
    user = client.prompts[0]
    assert "RESPONSE_SHAPE: player_action" in user
    assert "THE VIEW" in user and "[a1]" in user


def test_system_block_is_stable_across_calls():
    client = ScriptedClient('{"action": "a3"}', '{"action": "a3"}')
    a = agent(client)
    a.choose_action("v1", templates())
    a.choose_action("v2", templates())
    assert client.systems[0] == client.systems[1]


def test_scene_choice_is_clamped_into_range():
    client = ScriptedClient('{"choice": 9, "speech": "Onward."}')
    out = agent(client).choose_scene_action("view", ["a", "b"])
    assert out == {"choice": 0, "speech": "Onward."}


def test_scene_choice_shows_what_the_party_already_said():
    client = ScriptedClient('{"choice": 1, "speech": ""}')
    agent(client).choose_scene_action("view", ["a", "b"], ["Ysolde: The seal first."])
    assert "Ysolde: The seal first." in client.prompts[0]


def test_scene_choice_says_so_when_nobody_has_spoken():
    client = ScriptedClient('{"choice": 0, "speech": "After me."}')
    agent(client).choose_scene_action("view", ["a", "b"])
    assert "nobody has spoken yet" in client.prompts[0]


def test_speak_falls_back_to_raw_text():
    client = ScriptedClient("Just prose, no JSON.")
    assert agent(client).speak("view", "What do you do?") == "Just prose, no JSON."


# --- dm agent --------------------------------------------------------------


def dm(client):
    return DMAgent(client, "m", Ledger(), "a grim frontier", "dark", engine=eng)


def test_dm_narrate_gets_the_resolved_events_and_a_no_contradiction_rule():
    client = ScriptedClient('{"narration": "Steel rings in the mud."}')
    text = dm(client).narrate("VIEW", events(3))
    assert text == "Steel rings in the mud."
    user = client.prompts[0]
    assert "RESPONSE_SHAPE: dm_narration" in user
    assert "Thorin swings" in user
    system = client.systems[0][0]["text"]
    assert "never contradict" in (system + user).lower() or "Never contradict" in system
    assert "a grim frontier" in system


def test_dm_monster_action_returns_engine_action():
    client = ScriptedClient('{"action": "a1", "params": {}, "speech": "Kill them!"}')
    action = dm(client).monster_action("VIEW", templates(), "mon_1", "Goblin")
    assert (action.actor, action.template_id) == ("mon_1", "a1")
    assert "Goblin" in client.prompts[0]


def test_dm_monster_action_can_be_told_it_has_already_barked():
    client = ScriptedClient('{"action": "a1", "params": {}, "speech": "Kill them!"}')
    action = dm(client).monster_action("VIEW", templates(), "mon_1", "Goblin", speak=False)
    assert action.speech is None
    assert '"speech": null' in client.prompts[0]


def test_dm_adjudication_defaults_are_safe():
    client = ScriptedClient('{"resolution": "start_combat", "encounter": null}')
    out = dm(client).adjudicate("VIEW", "sneak past")
    assert out["resolution"] == "narrative"  # no monsters => cannot start combat

    client = ScriptedClient('{"resolution": "skill_check", "skill": "Stealth", "dc": "13", "actor": "pc_2"}')
    out = dm(client).adjudicate("VIEW", "sneak past")
    assert out == {
        "resolution": "skill_check",
        "skill": "Stealth",
        "dc": 13,
        "actor": "pc_2",
        "narration": "",
        "encounter": None,
    }

    client = ScriptedClient("total garbage")
    assert dm(client).adjudicate("VIEW", "x")["resolution"] == "narrative"


def test_dm_scene_options_always_returns_options():
    assert len(dm(ScriptedClient("garbage")).scene_options("v")) >= 3
    opts = dm(ScriptedClient('{"options": ["Look", "Leave", "Listen", "Loot", "Extra"]}')).scene_options("v")
    assert len(opts) == 4


def test_dm_note_is_injected_into_the_next_prompt_only():
    client = ScriptedClient('{"narration": "a"}', '{"narration": "b"}')
    d = dm(client)
    d.pending_note = "the goblins are cowards"
    d.narrate("v", events(2))
    d.narrate("v", events(2))
    assert "DM NOTE FROM TABLE: the goblins are cowards" in client.prompts[0]
    assert "DM NOTE" not in client.prompts[1]


# --- summarizer ------------------------------------------------------------


def test_summarize_uses_cheap_model_and_clamps():
    client = ScriptedClient('{"summary": "%s"}' % " ".join(["w"] * 400))
    led = Ledger()
    out = summarize(client, "claude-haiku-4-5-20251001", led, "before", events(4))
    assert len(out.split()) <= 151
    assert led.by_role["summarizer"]["calls"] == 1
    assert "RESPONSE_SHAPE: summary" in client.prompts[0]


def test_summarize_names_the_speaker_of_a_dialogue_event():
    """The summary is fed back to every agent; anonymous lines misattribute plans."""
    client = ScriptedClient('{"summary": "ok"}')
    line = eng.Event(1, 1, "dialogue", "pc_1", "I take the left fork.", {"speaker": "Thorin"})
    summarize(client, "m", Ledger(), "", [line])
    assert "Thorin: I take the left fork." in client.prompts[0]


def test_summarize_keeps_previous_on_garbage():
    assert summarize(ScriptedClient(""), "m", Ledger(), "previous text", events(2)) == "previous text"


def test_agents_work_against_the_mock_client():
    state = make_state()
    led = Ledger()
    a = PlayerAgent(MockLLMClient(seed=2), "m", state.combatants["pc_1"].sheet, led, engine=eng)
    for _ in range(15):
        action = a.choose_action(player_view(state, "pc_1", events(3), ""), templates())
        assert action.template_id in {"a1", "a2", "a3"}
    assert led.total_usd > 0


# --- sampling temperature --------------------------------------------------


def test_players_sample_at_the_configured_temperature():
    c = ScriptedClient('{"action": "a1", "params": {}, "speech": "For the hold."}')
    a = PlayerAgent(c, "m", make_state().combatants["pc_1"].sheet, Ledger(), engine=eng, temperature=0.4)
    a.choose_action("v", templates())
    assert c.temperatures == [0.4]


def test_players_run_hot_by_default():
    c = ScriptedClient('{"action": "a1", "params": {}}', '{"speech": "Aye."}')
    a = agent(c)
    a.choose_action("v", templates())
    a.speak("v", "What now?")
    assert c.temperatures == [player.DEFAULT_TEMPERATURE] * 2
    assert player.DEFAULT_TEMPERATURE == 1.0  # the top of the Anthropic range


@pytest.mark.parametrize(
    "given,expect",
    [(2.5, 1.0), (1.0, 1.0), (0.0, 0.0), (-3, 0.0), ("0.5", 0.5), (None, 1.0), ("hot", 1.0), (float("nan"), 1.0)],
)
def test_temperature_is_clamped_not_rejected(given, expect):
    """A silly number in a scenario file costs variety; it must never 400 mid-game."""
    assert player.clamp_temperature(given) == expect


# --- the cached prefix -----------------------------------------------------
#
# The `cache_control` marker on the player's system block did nothing for the
# first sixteen live games: the block was about 1,900 tokens and the Haiku
# minimum cacheable prefix is 4,096, so 823 player calls read nothing from
# cache. `agents.reference` is what carries it past the minimum, and these two
# tests are the whole guarantee — one that it is long enough, one that it does
# not move.


def _example_sheets():
    """Every seat of every shipped scenario, built by the real engine.

    The real `build_character` and not the fake one on purpose: what has to
    clear the cache minimum is the block a live game sends, and that is built
    from a real sheet with real spells, features and default equipment on it.
    """
    from engine.characters import build_character
    from engine.dice import RNG

    for path in sorted(pathlib.Path("examples").glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        party = raw.get("party") or (raw.get("scenario") or {}).get("party") or []
        for spec in party:
            yield path.name, build_character(dict(spec), RNG(1))


def test_every_example_seat_clears_the_cache_minimum():
    from agents.player import CACHE_MIN_TOKENS, CHARS_PER_TOKEN

    floor = 16000
    assert floor >= CACHE_MIN_TOKENS * CHARS_PER_TOKEN  # 14,336 plus headroom
    for scenario, sheet in _example_sheets():
        a = PlayerAgent(MockLLMClient(seed=1), "m", sheet, Ledger())
        size = len(a.system_blocks[0]["text"])
        assert size >= floor, f"{scenario} {sheet.name}: {size} chars is not cacheable"


def test_the_reference_is_the_seats_own_and_not_boilerplate():
    """Two seats of the same party get different blocks; the same seat, the same."""
    sheets = {name: sheet for _, sheet in _example_sheets() for name in [sheet.name]}
    cleric = sheets["Deacon Orla Vance"]
    fighter = sheets["Hob Tanner"]
    text = {
        s.name: PlayerAgent(MockLLMClient(seed=1), "m", s, Ledger()).system_blocks[0]["text"]
        for s in (cleric, fighter)
    }
    assert "Sacred Flame" in text["Deacon Orla Vance"]
    assert "Sacred Flame" not in text["Hob Tanner"]
    assert "Second Wind" in text["Hob Tanner"]
    # Conditions and the SRD action list are every seat's, and are the bulk.
    for t in text.values():
        assert "COMBAT ACTIONS (SRD)" in t and "Unconscious:" in t


def test_two_consecutive_choices_send_a_byte_identical_system_block():
    """What the cache is keyed on, including the suffix the client appends.

    `AnthropicClient.complete` adds `JSON_ONLY_SUFFIX` to the last system block
    on every `json_only` call, so the thing that must not move is the block
    *with* the suffix on it, not the block this agent hands over.
    """
    from llm.client import JSON_ONLY_SUFFIX

    class Recording(MockLLMClient):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.systems = []

        def complete(self, **kw):
            self.systems.append(json.loads(json.dumps(kw["system"])))
            return super().complete(**kw)

    client = Recording(seed=3)
    a = agent(client)
    a.choose_action("view one", templates())
    a.choose_action("a completely different view", templates())
    assert len(client.systems) == 2
    assert client.systems[0] == client.systems[1]
    sent = [
        blocks[-1]["text"] + JSON_ONLY_SUFFIX for blocks in client.systems
    ]
    assert sent[0] == sent[1]


# --- what the engine refused -----------------------------------------------


def test_a_rejected_action_is_put_back_to_the_player():
    client = ScriptedClient('{"action": "a3", "params": {}}')
    agent(client).choose_action(
        "v", templates(), rejected="(3,4) is occupied by Goblin 1"
    )
    prompt = client.prompts[0]
    assert prompt.startswith("The engine rejected your last action:")
    assert "(3,4) is occupied by Goblin 1" in prompt
    assert "Choose again from the list" in prompt
    # ... and the list is still there, after it.
    assert "[a1]" in prompt and "RESPONSE_SHAPE: player_action" in prompt


def test_a_rejected_action_is_put_back_to_the_dm():
    client = ScriptedClient('{"action": "a3", "params": {}}')
    dm(client).monster_action(
        "v", templates(), "mon_1", "Goblin 1", rejected="path leaves the grid"
    )
    prompt = client.prompts[0]
    assert prompt.startswith("The engine rejected your last action:")
    assert "path leaves the grid" in prompt
    assert "RESPONSE_SHAPE: dm_monster_action" in prompt


def test_nothing_is_prepended_when_nothing_was_rejected():
    client = ScriptedClient('{"action": "a3", "params": {}}')
    agent(client).choose_action("v", templates())
    assert not client.prompts[0].startswith("The engine rejected")


def test_the_rejection_never_touches_the_cached_system_block():
    client = ScriptedClient('{"action": "a3"}', '{"action": "a3"}')
    a = agent(client)
    a.choose_action("v", templates())
    a.choose_action("v", templates(), rejected="out of range")
    assert client.systems[0] == client.systems[1]


# --- what the narrator is shown --------------------------------------------


def _ev(kind, text, **data):
    return eng.Event(1, 1, kind, "pc_1", text, data)


def test_the_dm_is_shown_the_engines_wordless_actions():
    """Dodge, Dash, Disengage, Action Surge and a drunk potion are `system`."""
    from agents.dm import _event_lines

    lines = _event_lines(
        [
            _ev("system", "Thorin takes the Dodge action.", dodge=True),
            _ev("system", "Thorin surges: one more action this turn!", action_surge=True),
            _ev("system", "Thorin drinks a Potion of healing.", item="Potion of healing"),
            _ev("error", "Vessa's action was rejected: (9,9) is a wall"),
        ]
    )
    assert "Dodge action" in lines
    assert "surges" in lines
    assert "drinks a Potion of healing" in lines
    assert "(9,9) is a wall" in lines


def test_the_dm_is_not_shown_the_option_list_the_seed_line_or_the_round_cap():
    from agents.dm import _event_lines

    lines = _event_lines(
        [
            _ev("system", "The party considers: Search; Press on", options=["Search", "Press on"]),
            _ev("system", "The table is set — seed 42", game_id="abc", seed=42),
            _ev("system", "Combat hits the 20-round cap; the fight breaks off.", round_cap=20),
            _ev("system", "Thorin takes the Dodge action.", dodge=True),
        ]
    )
    assert "considers" not in lines
    assert "seed" not in lines
    assert "cap" not in lines
    assert "Dodge action" in lines


# --- suggested squares, and what they are for ------------------------------


def test_render_actions_prints_the_label_beside_each_square():
    tpl = eng.ActionTemplate(
        "a2",
        "move",
        "Move up to your speed",
        {"suggested": [[3, 4], [9, 1]], "labels": ["adjacent to Goblin 1", "away from all enemies"]},
        ["path"],
        "movement",
    )
    text = render_actions([tpl])
    first, second = text.splitlines()
    assert first.endswith("suggested=[[3, 4], [9, 1]]")
    assert "[3, 4] = adjacent to Goblin 1" in second
    assert "[9, 1] = away from all enemies" in second


def test_the_labels_line_does_not_hide_the_squares_from_the_mock():
    """`MockLLMClient` reads `suggested=` with a line-anchored regex.

    Printing the labels after it on the same line would take every mock move's
    destination away — silently, as an empty path the engine then refuses.
    """
    from llm.client import _SUGGESTED_RE

    tpl = eng.ActionTemplate(
        "a2", "move", "Move", {"suggested": [[3, 4]], "labels": ["adjacent to Goblin 1"]},
        ["path"], "movement",
    )
    text = render_actions([tpl])
    assert _SUGGESTED_RE.search(text.splitlines()[0])


def test_a_template_without_labels_renders_exactly_as_before():
    text = render_actions(templates())
    assert len(text.splitlines()) == 3
    assert "where" not in text


# --- coordinates and surprise ----------------------------------------------


def test_the_player_view_says_where_everyone_is():
    state = make_state()
    view = player_view(state, "pc_1", events(), "")
    row = next(l for l in view.splitlines() if l.startswith("pc_1 "))
    assert "(you)" in row
    x, y = state.combatants["pc_1"].position
    assert f"({x},{y})" in row
    mon = next(l for l in view.splitlines() if l.startswith("mon_1 "))
    mx, my = state.combatants["mon_1"].position
    assert f"({mx},{my})" in mon and "ft" in mon


def test_a_surprised_creature_is_marked_in_both_views():
    state = make_state()
    state.combatants["pc_1"].surprised = True
    pview = player_view(state, "pc_1", [], "")
    assert "YOU ARE SURPRISED" in pview
    assert "surprised" in next(l for l in pview.splitlines() if l.startswith("pc_1 "))
    dview = dm_view(state, [], "")
    assert "surprised" in next(l for l in dview.splitlines() if l.startswith("pc_1 |"))


def test_surprise_is_read_from_a_flag_or_a_condition_too():
    from agents.views import surprised

    state = make_state()
    plain = state.combatants["pc_2"]
    assert not surprised(plain)
    plain.conditions.append(eng.Condition("surprised"))
    assert surprised(plain)
    other = state.combatants["pc_3"]
    other.flags = {"surprised": True}
    assert surprised(other)


def test_the_digest_stops_promising_that_a_path_is_filtered():
    """The old sentence was true of every option except the one it mattered for.

    `move` takes a free-form `path` the engine validates only on `apply`, and
    a digest that says everything was pre-filtered is why a character walked
    into a wall and lost the turn.
    """
    from engine.srd import rules_digest as engine_digest

    text = " ".join(engine_digest().split())
    assert "the engine already filtered by range" not in text
    assert "THE PARAMETERS ARE NOT FILTERED" in text
    assert "difficult terrain costs two feet of movement for every one" in text
    assert "A suggested destination is always reachable" in text
