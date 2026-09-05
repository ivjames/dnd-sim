"""The voice lab's load-bearing edges in `web/static/app.js`.

Source-level assertions, like `test_app_js_narration.py` and
`test_newgame_panel.py`: the reader needs a DOM, an <audio> element and a
server, and what is guarded here is a shape an edit could quietly revert.

The first of them are about one thing — that what the lab plays is what the
narration will play. A tune that reached the preview but not the narration
would be a tester that lies, which is worse than no tester.

The rest are about the second: that the bench is built out of what the server
serves rather than out of a list kept here. The treatment has seventeen knobs
and had three, and the only reason that cost no edit to this page is that no
part of it names one.
"""

from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(ROOT, "web", "static", "app.js")
INDEX = os.path.join(ROOT, "web", "static", "index.html")


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_every_clip_url_carries_the_seats_tune():
    """`ttsUrl` is the one place a clip is asked for — the preview and the
    narration both go through it, which is what makes them the same clip."""
    js = read(APP_JS)
    m = re.search(r"function ttsUrl\(key, text\) \{(.+?)\n  \}", js, re.S)
    assert m, "ttsUrl has moved or changed shape"
    assert "tuneQuery(key)" in m.group(1)
    # And the tune is the stored one, keyed by seat, not something recomputed.
    q = re.search(r"function tuneQuery\(key\) \{(.+?)\n  \}", js, re.S)
    assert q, "tuneQuery has gone"
    body = q.group(1)
    assert "V.settings.tunes" in body
    for param in ("voice", "rate", "pitch"):
        assert "'&" + param + "='" in body


def test_changing_a_tune_drops_that_seats_clips_and_only_those():
    """They are the old voice. Kept, the page would go on playing what the
    listener has just changed away from until the cache rolled over.

    One seat, not the lot: the narrator may be mid-line, and `clipForget()` —
    which is for a game switch — would revoke the object URL of the clip
    playing and of the line prefetched behind it.
    """
    js = read(APP_JS)
    m = re.search(r"function vlSetTune\(key, patch\) \{(.+?)\n  \}", js, re.S)
    assert m, "vlSetTune has gone"
    body = m.group(1)
    assert "clipForgetSeat(key)" in body
    assert "clipForget()" not in body
    assert "voiceSaveSettings()" in body

    seat = re.search(r"function clipForgetSeat\(key\) \{(.+?)\n  \}", js, re.S)
    assert seat, "clipForgetSeat has gone"
    # Matched on the seat the URL names, and never the clip in the element.
    assert "'key=' + encodeURIComponent(key)" in seat.group(1)
    assert "V.audio && V.audio.src" in seat.group(1)


def test_the_lab_does_not_let_the_narrator_talk_over_the_samples():
    """And resumes only what it stopped: a spectator who paused first and then
    opened the lab did not ask for the narration to start again."""
    js = read(APP_JS)
    o = re.search(r"function vlOpen\(\) \{(.+?)\n  \}", js, re.S)
    c = re.search(r"function vlClose\(\) \{(.+?)\n  \}", js, re.S)
    assert o and c, "vlOpen/vlClose have gone"
    assert "VL.wasPlaying = V.playing" in o.group(1)
    assert "voicePausePlayback()" in o.group(1)
    assert "VL.wasPlaying && !V.playing" in c.group(1)
    assert "voicePlay()" in c.group(1)


def test_the_lab_has_a_way_in_and_a_way_out():
    html = read(INDEX)
    assert 'id="voice-lab"' in html and 'id="voicelab"' in html
    assert 'id="vl-rows"' in html and 'id="vl-close"' in html


def test_auto_clears_every_field_the_row_can_set():
    """A row reading "auto" while still carrying a size shift would be a lie,
    and the seat would keep the shift for good.

    Read off what the seat is holding rather than off a list of the fields
    there are: the treatment has seventeen knobs, a saved tune outlives the
    build that wrote it, and a list here would be a fourth place to add the
    eighteenth.
    """
    js = read(APP_JS)
    m = re.search(r"resetBtn\.addEventListener\('click', function \(\) \{(.+?)\n    \}\);", js, re.S)
    assert m, "the auto button has gone"
    body = m.group(1)
    for field in ("voice", "rate", "pitch"):
        assert field + ": null" in body, field
    # Everything else the seat holds, whatever it is called.
    assert "Object.keys(held).forEach" in body
    assert "patch[f] = null" in body
    assert "vlSetTune(role.key, patch)" in body


def test_the_treatment_travels_in_the_clip_url_too():
    """Every knob, by name, and not from a list of them: `tuneQuery` sends
    whatever the seat holds that is not one of the three SSML fields, so a knob
    added to `tts/dsp.py` reaches the server without this page learning it."""
    js = read(APP_JS)
    q = re.search(r"function tuneQuery\(key\) \{(.+?)\n  \}", js, re.S)
    assert q, "tuneQuery has gone"
    body = q.group(1)
    assert "Object.keys(t).sort().forEach" in body, "one tune has to be one URL"
    assert "f === 'voice' || f === 'rate' || f === 'pitch'" in body
    assert "encodeURIComponent(f)" in body
    # And no hand-written roll-call of the knobs, which is what this replaced.
    assert "'size', 'growl', 'cave'" not in body


def test_the_bench_is_built_from_what_the_server_serves():
    """The sliders, their bounds, their names and their hints all come from
    `/api/tts/voices` (`_fx_spec`), which reads them off `dsp.FIELDS`. A page
    with its own copy of any of that is a second place a knob has to be added,
    and the field table exists to stop exactly that."""
    js = read(APP_JS)
    m = re.search(r"var fxSpec = \(VL\.roster && VL\.roster\.fx\) \|\| null;(.+?)\n    \}\n",
                  js, re.S)
    assert m, "the fx block has gone"
    body = m.group(1)
    # The guard stands: a monster seat, a roster that has answered, and a
    # deployment where the treatment is switched on at all.
    assert "Speech.isMonsterKey(role.key) && fxSpec && fxSpec.available" in body
    # Driven off the server's order, because `jsonify` sorts an object's keys.
    assert "(fxSpec.order || []).forEach" in body
    assert "c.label || f" in body and "c.min, c.max" in body
    assert "c.hint" in body
    # Nothing hand-written about any individual knob.
    for spelled in ("fxSpec.size", "fxSpec.growl", "fxSpec.cave", "'growl'", "'cave'"):
        assert spelled not in body, spelled


def test_the_three_the_casting_deals_stay_in_front_of_the_listener():
    """Seventeen sliders in one row is a bench nobody can read. The three the
    casting actually deals (`DEALT_FX`, and the server says which) describe
    every monster in the game; the other fourteen fold away behind a summary
    until somebody is deciding by ear what a creature should sound like."""
    js = read(APP_JS)
    m = re.search(r"var fxSpec = \(VL\.roster && VL\.roster\.fx\) \|\| null;(.+?)\n    \}\n",
                  js, re.S)
    assert m, "the fx block has gone"
    body = m.group(1)
    assert "if (c.dealt)" in body, "the split is the server's `dealt` flag"
    assert "el('details', 'vl-more')" in body and "el('summary'" in body
    # A stored tune must never be a slider the listener cannot find.
    assert "more.open = true" in body
