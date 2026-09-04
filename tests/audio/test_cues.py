"""The cue table, and the routing it promises.

The point of these is that a cue's `match` is a claim about events the engine
really emits. Hand-written event dicts prove the matcher; the mock game at the
bottom proves the claims.
"""

from __future__ import annotations

import pytest

from engine.events import EVENT_KINDS
from tools.audio import cues as C


def test_every_event_kind_is_either_scored_or_waived():
    """A new event kind should force a decision, not slip in silently."""
    scored = {c.match["kind"] for c in C.CUES if c.match}
    undecided = EVENT_KINDS - scored - C.UNSCORED_EVENT_KINDS
    assert not undecided, (
        f"event kinds with neither a cue nor a waiver: {sorted(undecided)} — "
        "add a cue in tools/audio/cues.py or list it in UNSCORED_EVENT_KINDS"
    )


def test_no_cue_matches_an_event_kind_the_engine_cannot_emit():
    bogus = {c.id: c.match["kind"] for c in C.CUES if c.match and c.match["kind"] not in EVENT_KINDS}
    assert not bogus, bogus


def test_waived_kinds_are_not_also_scored():
    both = {c.id for c in C.CUES if c.match and c.match["kind"] in C.UNSCORED_EVENT_KINDS}
    assert not both


@pytest.mark.parametrize("cue", C.CUES, ids=lambda c: c.id)
def test_cue_is_well_formed(cue):
    assert cue.group in C.GROUPS
    assert cue.queries, "a cue with no search terms harvests nothing"
    assert 0 < cue.dur[0] < cue.dur[1]
    assert -60 <= cue.gain_db <= 6
    assert cue.id.replace("_", "").isalnum()
    if cue.match:
        assert set(cue.match) <= {"kind", "data"}


def test_ids_are_unique_and_grouped_together():
    ids = [c.id for c in C.CUES]
    assert len(ids) == len(set(ids))
    order = [c.group for c in C.CUES]
    assert order == sorted(order, key=lambda g: order.index(g)), "groups are interleaved"


def ev(kind, **data):
    return {"seq": 1, "round": 1, "kind": kind, "actor": "pc_1", "text": "", "data": data}


def test_match_needs_the_kind_and_every_constraint():
    crit = C.cue("sting_crit").match
    assert C.event_matches(crit, ev("attack", hit=True, crit=True))
    assert not C.event_matches(crit, ev("attack", hit=True, crit=False))
    assert not C.event_matches(crit, ev("damage", hit=True, crit=True))
    assert not C.event_matches(crit, ev("attack", hit=True))
    assert not C.event_matches(None, ev("attack", hit=True, crit=True))


def test_one_is_not_true():
    """`1 == True` in Python; a cue means the boolean."""
    assert not C.event_matches(C.cue("sting_crit").match, ev("attack", hit=1, crit=1))


def test_dotted_paths_reach_into_the_roll():
    fumble = C.cue("sting_fumble").match
    assert C.event_matches(fumble, ev("attack", hit=False, roll={"natural": 1, "total": 3}))
    assert not C.event_matches(fumble, ev("attack", hit=False, roll={"natural": 7}))
    assert not C.event_matches(fumble, ev("attack", hit=False))
    assert not C.event_matches(fumble, ev("attack", hit=False, roll="1"))


def fired(event):
    return [c.id for c in C.cues_for_event(event)]


def test_an_event_lights_one_cue_per_layer():
    """A crit is a sting *and* a hit; initiative is a bed change *and* a sting."""
    assert fired(ev("attack", hit=True, crit=True)) == ["sting_crit", "sfx_attack_hit"]
    assert fired(ev("combat_start")) == ["music_combat", "sting_combat_start"]
    assert fired(ev("narration")) == []


def test_the_most_specific_cue_wins_inside_a_layer():
    assert C.cue_for_event(ev("attack", hit=True, crit=True), "sfx").id == "sfx_attack_hit"
    assert C.cue_for_event(ev("attack", hit=True, crit=False), "sting") is None
    assert C.cue_for_event(ev("attack", hit=False, roll={"natural": 1}), "sting").id == "sting_fumble"
    assert C.cue_for_event(ev("attack", hit=False, roll={"natural": 12}), "sfx").id == "sfx_attack_miss"
    assert C.cue_for_event(ev("damage", damage_type="fire", amount=8), "sfx").id == "sfx_dmg_fire"
    assert C.cue_for_event(ev("death_save", success=False), "sting").id == "sting_death_save_fail"


def test_asking_for_a_group_that_does_not_exist_is_a_mistake():
    with pytest.raises(ValueError):
        C.cue_for_event(ev("combat_start"), "stings")


def test_required_cues_cover_the_moments_a_table_would_notice():
    required = {c.id for c in C.required_cues()}
    assert {"music_combat", "sting_combat_start", "sting_crit", "sting_dead",
            "sfx_attack_hit", "sfx_dice", "amb_dungeon_stone"} <= required


def test_cue_dicts_survive_a_json_round_trip():
    import json
    doc = json.loads(json.dumps([c.to_dict() for c in C.CUES]))
    assert {d["id"] for d in doc} == {c.id for c in C.CUES}
    assert doc[0]["dur"] == list(C.CUES[0].dur)


def test_the_cues_fire_on_a_real_mock_game():
    """Route a whole seeded game through the table.

    Slower than the rest of this file, and the only test that proves the match
    rules are written against the events the engine actually emits.
    """
    llm = pytest.importorskip("llm.client")
    from orchestrator.bus import EventBus
    from orchestrator.config import GameConfig
    from orchestrator.game import Game, default_engine

    cfg = GameConfig.load("examples/goblin_ambush.json")
    cfg.tempo_ms = 0
    cfg.mock = True
    cfg.max_rounds_per_combat = 8
    cfg.scenario["max_scenes"] = 1
    cfg.scenario["beats_per_scene"] = 1
    bus = EventBus()
    Game(cfg, llm.MockLLMClient(seed=cfg.seed), bus, engine=default_engine()).run()

    history = [e.to_dict() for e in bus.history()]
    assert history
    lit: dict[str, int] = {}
    for e in history:
        for cue in C.cues_for_event(e):
            lit[cue.id] = lit.get(cue.id, 0) + 1

    # Anything the engine emitted that is neither scored nor waived would have
    # slipped past the first test only by never being emitted at all.
    seen = {e["kind"] for e in history}
    assert seen <= (C.UNSCORED_EVENT_KINDS | {c.match["kind"] for c in C.CUES if c.match})
    assert {"music_combat", "sting_combat_start", "sfx_dice"} <= set(lit), sorted(lit)
    assert any(k.startswith("sfx_dmg_") for k in lit), sorted(lit)
