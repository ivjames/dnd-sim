"""Two things about `web/static/app.js` that no other test would catch.

The name of whoever is speaking has to stay **on the screen** while it comes
off the **soundtrack**: the transcript builds that line itself, from
`data.speaker`, and shares no string with `speech.js` — so a change to what is
spoken must not be able to take the visible name with it.

And the voice is now per chunk rather than per line, because an attributed line
of dialogue changes speaker partway through (the narrator names the monster,
then the monster talks). Source-level assertions, like
`test_newgame_panel.py`: the reader needs a DOM and an audio element, and the
failure being guarded against is an edit that quietly reverts the shape.
"""

from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(ROOT, "web", "static", "app.js")


def read() -> str:
    with open(APP_JS, "r", encoding="utf-8") as fh:
        return fh.read()


def dialogue_branch(js: str) -> str:
    """The transcript's own rendering of a `dialogue` event."""
    m = re.search(r"ev\.kind === 'dialogue'\) \{(.+?)\n    \} else if", js, re.S)
    assert m, "the transcript no longer has a dialogue branch"
    return m.group(1)


def test_the_transcript_still_prints_the_speaker_name():
    branch = dialogue_branch(read())
    assert "ev.data && ev.data.speaker" in branch          # the name, from the event
    assert "'speaker'" in branch                           # into its own span
    assert "+ ':'" in branch                               # still punctuated on screen
    # Whatever it prints is built here, not borrowed from the spoken phrase.
    assert "phraseFor" not in branch and "segmentsFor" not in branch


def test_the_reader_takes_a_voice_per_chunk():
    js = read()
    assert "Speech.segmentsFor(" in js, "the reader no longer asks for the voiced parts"
    # One key per line would put the narrator's name clip in the monster's voice.
    assert "cur.vkey" not in js
    for call in re.findall(r"ttsUrl\(([^,]+),", js):
        assert call.strip() in ("key", "ahead.key", "chunkKey(cur)"), call
