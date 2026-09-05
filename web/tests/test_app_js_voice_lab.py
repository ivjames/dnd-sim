"""The voice lab's two load-bearing edges in `web/static/app.js`.

Source-level assertions, like `test_app_js_narration.py` and
`test_newgame_panel.py`: the reader needs a DOM, an <audio> element and a
server, and what is guarded here is a shape an edit could quietly revert.

Both are about the same thing — that what the lab plays is what the narration
will play. A tune that reached the preview but not the narration would be a
tester that lies, which is worse than no tester.
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
