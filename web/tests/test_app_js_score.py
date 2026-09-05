"""The score's wiring in `app.js`, asserted at source level.

The routing is pure and tested through node (`test_cues_js.py`); the mixer
needs a browser, so this does what `test_app_js_hold.py` and
`test_reveal_gate.py` do and pins the handful of decisions that an edit could
undo without any test noticing. Each one below cost something to get right.
"""

from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(ROOT, "web", "static", "app.js")
INDEX = os.path.join(ROOT, "web", "static", "index.html")


def read(path=APP_JS) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def fn(js: str, name: str) -> str:
    m = re.search(r"\n  function " + re.escape(name) + r"\([^)]*\) \{(.+?)\n  \}\n", js, re.S)
    assert m, f"{name} is gone from app.js"
    return m.group(1)


def test_the_page_loads_the_cue_table_before_it_loads_the_player():
    html = read(INDEX)
    assert html.index("/static/cues.js") < html.index("/static/app.js")


def test_cues_fire_from_the_reveal_and_from_nowhere_else():
    """`renderEvent` is the beat a line lands in — with the narrator running,
    the beat it is read in. Firing on arrival instead would play a fight's
    stings while its first sentence was still being spoken."""
    js = read()
    calls = re.findall(r"(?<!function )scoreOnEvent\(", js)
    assert len(calls) == 1, f"scoreOnEvent is called from {len(calls)} places, not just renderEvent"
    assert "scoreOnEvent(ev)" in fn(js, "renderEvent")


def test_the_history_replay_is_silent():
    """A page opened mid-game renders hundreds of events in one pass."""
    body = fn(read(), "scoreOnEvent")
    assert "V.loading" in body and "scoreArmed()" in body
    assert body.index("return") < body.index("cuesForEvent"), "it routes before it refuses"


def test_nothing_plays_without_a_gesture_or_in_a_background_tab():
    body = fn(read(), "scoreArmed")
    for guard in ("A.settings.enabled", "A.unlocked", "pageHidden()"):
        assert guard in body, guard


def test_the_bed_ducks_under_whatever_is_being_read():
    """Either engine: `V.current` is set for a Polly clip and an utterance."""
    js = read()
    assert "V.current" in fn(js, "scoreDuckEval")
    assert "scoreDuckEval();" in fn(js, "voiceRenderControls"), \
        "the duck is no longer evaluated on a voice transition"


def test_the_same_bed_firing_again_does_not_restart_it():
    body = fn(read(), "scoreFire")
    assert re.search(r"A\.bed\.cue\.id === cue\.id\)\s*return", body), \
        "a second combat_start would start the music over"


def test_switching_games_silences_the_score_before_taking_the_new_id():
    js = read()
    sel = re.search(r"function selectGame\(id\) \{(.+?)\n    S\.gameId = id;", js, re.S)
    assert sel and "scoreReset()" in sel.group(1)
    assert "scoreCatchUp()" in js, "a game joined in progress would have no bed"


def test_releasing_an_element_takes_its_handlers_off_first():
    """Emptying `src` fails the load, which fires `error`, which re-enters this
    — a loop that took the tab down rather than the sound."""
    body = fn(read(), "scoreDrop")
    assert "play.dropped" in body
    assert body.index("play.detach()") < body.index("removeAttribute('src')")


def test_the_credits_are_rendered_for_the_whole_pack():
    """CC BY is a condition of playing it. A credit that flashes past during a
    fight is not attribution anybody can read."""
    body = fn(read(), "scoreRenderCredits")
    assert "A.cues.forEach" in body and "c.credit" in body
    assert 'id="score-credits"' in read(INDEX)
