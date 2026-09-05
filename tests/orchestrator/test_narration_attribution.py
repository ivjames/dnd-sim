"""Who did what, and what they are called while doing it.

The DM's per-turn narration was mechanically faithful and repeatedly wrong
about attribution: it acted for player characters on a monster's turn, gave the
actor's own blow to its target, and re-invented a monster's gender between
rounds. None of that is checkable by reading the prose — a mock client returns
canned text — but the inputs that made it possible are, and this file pins
those: the turn's actor reaches the prompt, every creature carries one settled
set of pronouns, and both stay put across identical runs.
"""

import json
from pathlib import Path

import pytest

from agents.dm import DMAgent
from agents.views import DEFAULT_PRONOUNS, PRONOUNS, dm_view, pronouns_for
from llm.client import LLMResponse, MockLLMClient
from llm.cost import Ledger
from orchestrator.bus import EventBus
from orchestrator.config import GameConfig
from orchestrator.game import Game

from . import fake_engine as eng
from .test_agents import ScriptedClient, make_state

ROOT = Path(__file__).resolve().parents[2]


# --- whose turn is it ------------------------------------------------------


def test_dm_view_names_whose_turn_it_is():
    state = make_state()
    view = dm_view(state, [], "")
    actor = state.combatants[state.active_id()]
    assert f"TURN: {actor.id} {actor.name}" in view


def test_dm_view_has_no_turn_line_when_nobody_is_up():
    state = make_state()
    state.initiative = []
    assert "TURN:" not in dm_view(state, [], "")


def test_dm_view_survives_a_state_that_cannot_say_whose_turn_it_is():
    """The views are duck-typed against the engine; a fake without `active_id`
    must still render rather than raise."""

    class Bare:
        combatants = {}

    assert "TURN:" not in dm_view(Bare(), [], "")


def test_the_actor_reaches_the_prompt_even_when_another_creature_leads_the_events():
    """The regression this whole change exists for.

    Roughly half a turn's event lists in a real game name more than one
    creature, and a turn that opens with an opportunity attack *on* the actor
    reads, to anyone inferring the actor from the first line, as the attacker's
    turn. That is how a bandit's reaction on Rooke's turn became Rooke acting
    on the bandit captain's.
    """
    state = make_state()
    actor = state.combatants[state.active_id()]
    events = [
        eng.Event(1, 2, "attack", "mon_1",
                  f"Goblin 1 makes an opportunity attack on {actor.name} with Scimitar: "
                  "1d20+3 → 14 vs AC 19, miss", {}),
        eng.Event(2, 2, "move", actor.id, f"{actor.name} moves from (1,4) to (1,7) (30 ft).", {}),
    ]
    client = ScriptedClient('{"narration": "Steel scrapes past."}')
    DMAgent(client, "m", Ledger(), "a grim frontier", "dark", engine=eng).narrate(
        dm_view(state, [], ""), events
    )
    prompt = client.prompts[0]
    turn_line = f"TURN: {actor.id} {actor.name}"
    assert turn_line in prompt
    # And it is stated before the events, not buried after them.
    assert prompt.index(turn_line) < prompt.index("THE ENGINE JUST RESOLVED")
    assert "give no other creature a decision" in prompt


def test_the_narration_view_does_not_reprint_the_events_it_is_about():
    """`_narrate` used to pass the turn's events as the view's `recent`, so
    every line was printed twice in one prompt. The events block is the
    authoritative copy; the view carries the state around it."""
    state = make_state()
    line = "Thorin swings at Goblin 1: hit for 6"
    events = [eng.Event(1, 2, "attack", "pc_1", line, {})]
    client = ScriptedClient('{"narration": "x"}')
    DMAgent(client, "m", Ledger(), "s", "t", engine=eng).narrate(dm_view(state, [], ""), events)
    assert client.prompts[0].count(line) == 1


# --- pronouns --------------------------------------------------------------


@pytest.mark.parametrize(
    "stated, expected",
    [("female", "she/her"), ("Female", "she/her"), ("f", "she/her"), ("woman", "she/her"),
     ("male", "he/him"), ("M", "he/him"), ("man", "he/him"),
     ("", DEFAULT_PRONOUNS), ("  ", DEFAULT_PRONOUNS), ("nonbinary", DEFAULT_PRONOUNS),
     (None, DEFAULT_PRONOUNS)],
)
def test_a_party_member_whose_spec_states_only_the_older_gender(stated, expected):
    """`gender` is the spelling a party spec used to state. Still read, and
    still read through `PRONOUNS`, because a stranger's config and a game
    persisted before `pronouns` existed both still carry it."""
    sheet = eng.build_character(
        {"id": "pc_1", "name": "Rooke", "klass": "Fighter", "level": 3, "gender": stated}, eng.RNG(1)
    )
    assert pronouns_for(eng.Combatant(id="pc_1", name="Rooke", side="party", kind="pc",
                                      sheet=sheet)) == expected


@pytest.mark.parametrize(
    "stated, expected",
    [("she/her", "she/her"), ("he/him", "he/him"), ("they/them", "they/them"),
     ("xe/xem", "xe/xem"), ("He/Him", "He/Him"), ("  she/her  ", "she/her"),
     ("", DEFAULT_PRONOUNS), ("   ", DEFAULT_PRONOUNS), (None, DEFAULT_PRONOUNS)],
)
def test_stated_pronouns_are_narrated_as_written(stated, expected):
    """The key a party spec states now, and the column asks for exactly it —
    so there is nothing to infer and nothing to round off. A set this codebase
    has never heard of reaches the DM intact; only silence becomes the default.
    """
    sheet = eng.build_character(
        {"id": "pc_1", "name": "Rooke", "klass": "Fighter", "level": 3, "pronouns": stated},
        eng.RNG(1),
    )
    assert pronouns_for(eng.Combatant(id="pc_1", name="Rooke", side="party", kind="pc",
                                      sheet=sheet)) == expected


def test_stated_pronouns_beat_a_legacy_gender_here_too():
    """`web/routes/tts.py` casts the voice off `pronouns` where both are
    stated. Narrating off the other one would put the DM and the voice at odds
    in exactly the config that took the trouble to say so."""
    sheet = eng.build_character(
        {"id": "pc_1", "name": "Rooke", "klass": "Fighter", "level": 3,
         "gender": "female", "pronouns": "they/them"}, eng.RNG(1),
    )
    assert pronouns_for(eng.Combatant(id="pc_1", name="Rooke", side="party", kind="pc",
                                      sheet=sheet)) == "they/them"


def test_a_monster_states_nothing_so_it_is_they():
    """An SRD stat block records a size and a type and no gender. Nothing is
    inferred from the name, and nothing is dealt from the dice — a monster
    whose pronoun came out of the RNG is a monster whose pronoun can drift."""
    mon = eng.monster_to_combatant("Goblin", "mon_1", eng.RNG(1))
    assert pronouns_for(mon) == DEFAULT_PRONOUNS == "they/them"


def test_the_pronoun_spellings_are_the_ones_tts_already_casts_on():
    """One authored answer on a party spec should not cast a voice one way and
    narrate the character the other. `agents/` cannot import `tts/` — wrong
    direction in the layering — so the tables are pinned against each other
    here instead."""
    from tts.voices import GENDERS, gender_for_pronouns, normalize_gender

    assert set(PRONOUNS) == set(GENDERS)
    for spelling, gender in GENDERS.items():
        assert PRONOUNS[spelling] == {"female": "she/her", "male": "he/him"}[gender]
        # And the legacy read is a round trip: narrating a `gender` through
        # this table lands on pronouns the casting reads back as the same pool.
        assert gender_for_pronouns(PRONOUNS[spelling]) == normalize_gender(spelling)

    # The other direction is the one that matters now, because `pronouns` is
    # what a spec states: what the DM is told and what the voice is dealt from
    # come off the SAME string, so the two cannot disagree about a character.
    for said in ("she/her", "he/him", "they/them", "xe/xem", "she/they"):
        sheet = eng.build_character(
            {"id": "pc_1", "name": "Rooke", "klass": "Fighter", "level": 3, "pronouns": said},
            eng.RNG(1),
        )
        narrated = pronouns_for(eng.Combatant(id="pc_1", name="Rooke", side="party",
                                              kind="pc", sheet=sheet))
        assert narrated == said
        assert gender_for_pronouns(narrated) == gender_for_pronouns(said)


def test_the_view_gives_every_combatant_pronouns_and_a_class():
    """Both columns exist for the same reason: what is not in the view is guessed.

    A narrator without the class column called the party's wizard "the downed
    cleric" while the cleric was standing over him.
    """
    state = make_state()
    view = dm_view(state, [], "")
    assert "COMBATANTS (id | name | pronouns | side | class | HP | AC | pos | conditions)" in view
    for cid, c in state.combatants.items():
        row = next(l for l in view.splitlines() if l.startswith(f"{cid} |"))
        assert f"| {pronouns_for(c)} |" in row
        sheet = getattr(c, "sheet", None)
        if sheet is not None:
            assert f"| {sheet.klass} {sheet.level} |" in row


# --- across a whole game ---------------------------------------------------


@pytest.fixture
def tollhouse() -> GameConfig:
    raw = json.loads((ROOT / "examples" / "tollhouse.json").read_text())
    raw["tempo_ms"] = 0
    raw["mock"] = True
    return GameConfig.from_dict(raw)


def _narration_prompts(cfg: GameConfig, seed: int) -> list[str]:
    """Every user prompt `DMAgent.narrate` built over one full mock game.

    Matched on the events header rather than the RESPONSE_SHAPE, which
    `dm_open_scene` and `dm_epilogue` also answer to.
    """
    cfg = GameConfig.from_dict({**cfg.to_dict(), "seed": seed})
    captured: list[str] = []

    class Watching(MockLLMClient):
        def complete(self, **kw):
            msg = kw["messages"][0]["content"]
            if "THE ENGINE JUST RESOLVED THESE EVENTS" in msg:
                captured.append(msg)
            return super().complete(**kw)

    game = Game(cfg, Watching(seed=seed), EventBus())
    game.run()
    assert game.status == "finished", game.error
    assert captured, "the game produced no narration prompts"
    return captured


def test_a_creature_keeps_one_set_of_pronouns_for_the_whole_game(tollhouse):
    """The Bandit Captain was "her" in rounds 1-2 and "him" from round 5. The
    prompt could not have said otherwise, because it never said anything."""
    seen: dict[str, set[str]] = {}
    for prompt in _narration_prompts(tollhouse, 23):
        for line in prompt.splitlines():
            parts = [p.strip() for p in line.split(" | ")]
            if len(parts) < 8 or not parts[0].startswith(("pc_", "mon_")):
                continue
            seen.setdefault(f"{parts[0]} {parts[1]}", set()).add(parts[2])
    assert seen, "no combatant rows found in the narration prompts"
    drifted = {k: v for k, v in seen.items() if len(v) != 1}
    assert not drifted, f"pronouns changed mid-game: {drifted}"
    assert {v for k, v in seen.items() if k.startswith("mon_") for v in v} == {"they/them"}
    assert seen["pc_1 Captain Isolde Rooke"] == {"she/her"}
    assert seen["pc_3 Father Bexley Crane"] == {"he/him"}


def test_every_narration_prompt_names_the_turn_it_describes(tollhouse):
    for prompt in _narration_prompts(tollhouse, 23):
        turn = [l for l in prompt.splitlines() if l.startswith("TURN: ")]
        assert len(turn) == 1, prompt
        assert turn[0].split(" ", 2)[1].startswith(("pc_", "mon_"))


def test_the_narration_prompts_are_identical_across_two_runs(tollhouse):
    """Nothing added here reads the clock, the global `random`, or a dict
    iteration order the game does not own."""
    assert _narration_prompts(tollhouse, 23) == _narration_prompts(tollhouse, 23)
