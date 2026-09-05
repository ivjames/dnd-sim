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


def test_changing_a_tune_drops_the_clips_already_fetched():
    """They are the old voice. Kept, the page would go on playing what the
    listener has just changed away from until the cache rolled over."""
    js = read(APP_JS)
    m = re.search(r"function vlSetTune\(key, patch\) \{(.+?)\n  \}", js, re.S)
    assert m, "vlSetTune has gone"
    assert "clipForget()" in m.group(1)
    assert "voiceSaveSettings()" in m.group(1)


def test_the_lab_has_a_way_in_and_a_way_out():
    html = read(INDEX)
    assert 'id="voice-lab"' in html and 'id="voicelab"' in html
    assert 'id="vl-rows"' in html and 'id="vl-close"' in html
