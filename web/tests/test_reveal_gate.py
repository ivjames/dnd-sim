"""The reveal gate: the page shows a turn as the narrator reads it, not as it
arrives.

Two halves, tested the two ways this repo tests JavaScript. The rule about
*which* waiting events may go on screen in a beat is pure, lives in
`speech.js`, and is driven through node with real event shapes. The wiring
that puts that rule between the stream and the DOM is in `app.js`, which needs
a browser, so it is asserted at source level like `test_app_js_hold.py` — the
failure being guarded against is an edit that quietly puts an event back on
screen the moment it arrives.
"""

from __future__ import annotations

import os
import re

from web.tests.test_speech_js import run_js  # node harness; skips where node is absent

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(ROOT, "web", "static", "app.js")


def read() -> str:
    with open(APP_JS, "r", encoding="utf-8") as fh:
        return fh.read()


#: One resolved turn, in the order the orchestrator emits it: what happened,
#: and only then the paragraph saying what it looked like.
TURN = [
    {"seq": 1, "kind": "turn_start", "actor": "pc_1", "text": "Thorin's turn.", "data": {}},
    {"seq": 2, "kind": "move", "actor": "pc_1", "text": "Thorin moves 15 feet.", "data": {"ft": 15}},
    {"seq": 3, "kind": "attack", "actor": "pc_1", "text": "Thorin attacks Goblin 2 with a greataxe", "data": {"hit": True}},
    {"seq": 4, "kind": "damage", "actor": "pc_1", "text": "Goblin 2 takes 9 slashing (7 -> 0)", "data": {"amount": 9}},
    {"seq": 5, "kind": "narration", "actor": "dm", "text": "The axe finds the collarbone.", "data": {}},
]


# -- the rule ----------------------------------------------------------------

def test_a_muted_turn_is_revealed_whole_when_its_narration_begins():
    """The case the gate exists for.

    With mechanics muted nothing in the turn is spoken until the paragraph, so
    releasing any of it earlier would drop a goblin's hit points to zero in
    silence. All five go up together, in the beat the narration starts.
    """
    n = run_js("return S.revealRun(IN, {enabled: true, muteMechanics: true});", TURN)
    assert n == len(TURN)


def test_with_mechanics_spoken_each_line_waits_for_its_own_turn():
    """Every line is read, so every line is its own beat and the run is one."""
    settings = {"enabled": True, "muteMechanics": False}
    n = run_js("return S.revealRun(IN.q, IN.s);", {"q": TURN, "s": settings})
    assert n == 1


def test_a_turn_with_no_narration_yet_reveals_nothing():
    """Held, not dropped: the paragraph is the next thing the DM writes, and
    the run goes up when it arrives. Nothing here has words of its own."""
    n = run_js("return S.revealRun(IN, {enabled: true, muteMechanics: true});", TURN[:4])
    assert n == 0


def test_the_gate_is_open_when_nothing_is_being_read():
    """Voice off is the page this always was: everything, as it arrives."""
    assert run_js("return S.revealRun(IN, {enabled: false});", TURN) == 0
    # ... which app.js reads as "no line is coming", and it is `revealGated`
    # that lets the queue through — see the source assertions below.


def test_a_kind_nobody_speaks_never_holds_a_run_open():
    """`turn_end` and `roll` are spoken by nothing (see speech.js), so a queue
    of them alone must not look like a beat waiting to start."""
    quiet = [{"seq": 1, "kind": "turn_end", "text": "", "data": {}},
             {"seq": 2, "kind": "roll", "text": "1d20 -> 14", "data": {}}]
    assert run_js("return S.revealRun(IN, {enabled: true});", quiet) == 0


# -- the wiring --------------------------------------------------------------

def test_the_stream_hands_events_to_the_queue_and_not_to_the_dom():
    js = read()
    on_message = re.search(r"function onMessage\(e\) \{(.+?)\n  \}", js, re.S)
    assert on_message, "app.js no longer has onMessage"
    assert "ingest(ev)" in on_message.group(1)
    assert "renderEvent" not in on_message.group(1)
    assert "forEach(ingest)" in js, "the history load no longer goes through the queue"

    # `renderEvent` is what puts an event on screen, so the queue's own release
    # has to be its only caller — anything else is a path around the gate.
    # Its declaration is not a call, and neither is a mention in a comment.
    code = re.sub(r"//[^\n]*", "", js)
    calls = re.findall(r"(?<!function )renderEvent\(", code)
    assert len(calls) == 1, f"renderEvent is called from {len(calls)} places, not just revealOne"
    one = re.search(r"function revealOne\(\) \{(.+?)\n  \}", js, re.S)
    assert one and "renderEvent(ev)" in one.group(1)


def test_a_reconnect_resumes_from_what_was_received_not_from_what_was_shown():
    """The queue holds events the transcript has not reached, and they are
    still events this page has. Resuming from the visible edge would ask the
    server to send them again."""
    js = read()
    m = re.search(r"/stream\?after=' \+ (S\.\w+)", js)
    assert m and m.group(1) == "S.gotSeq"


def test_the_board_is_asked_for_as_of_the_last_line_shown():
    js = read()
    fn = re.search(r"function refreshSnapshot\(\) \{(.+?)\n  \}", js, re.S)
    assert fn, "app.js no longer has refreshSnapshot"
    body = fn.group(1)
    assert "revealGated()" in body and "at_seq=" in body and "S.lastSeq" in body


def test_the_narrator_being_free_is_what_lets_a_run_through():
    """Mid-line, paused, backgrounded, or still working through what is already
    on screen: in every one of those the queue stays put."""
    js = read()
    fn = re.search(r"function revealPump\(\) \{(.+?)\n  \}", js, re.S)
    assert fn, "app.js no longer has revealPump"
    body = fn.group(1)
    assert "V.current || !voiceArmed() || V.cursor < S.events.length" in body
    assert "S.revealing" in body, "revealing an event pumps the narrator; the guard is required"


def test_the_end_banner_waits_for_the_narrator_too():
    """The stream's `end` frame arrives while there may be minutes of
    transcript left to read; "session finished" is the end of the story."""
    js = read()
    end = re.search(r"es\.addEventListener\('end', function \(e\) \{(.+?)\n    \}\);", js, re.S)
    assert end, "app.js no longer handles the stream's end frame"
    assert "S.endBanner =" in end.group(1)
    assert "appendNode" not in end.group(1)


def test_turning_the_voice_off_lets_everything_go_at_once():
    """With nothing keeping step there is nothing to hold back, and a page
    stuck mid-queue with the voice off would just look broken."""
    js = read()
    fn = re.search(r"function voiceDisable\(\) \{(.+?)\n  \}", js, re.S)
    assert fn and "revealAll()" in fn.group(1)
