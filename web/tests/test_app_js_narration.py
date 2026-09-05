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


def narration_branch(js: str) -> str:
    """The transcript's own rendering of a `narration` event."""
    m = re.search(r"ev\.kind === 'narration'\) \{(.+?)\n    \} else if", js, re.S)
    assert m, "the transcript no longer has a narration branch"
    return m.group(1)


def test_a_narration_closes_the_turn_mechanics_group():
    """So a held-back knockout lands below the paragraph, not back above it.

    The orchestrator holds a turn's `down`/`dead` until its narration has been
    said (`Game._emit_turn`), so the reveal arrives *after* this paragraph.
    The turn's mechanics group is a node already appended above it, and the
    mechanics branch files any line into `S.groupBody` while one is open — so
    an open group would put the reveal back inside it, above the prose that is
    supposed to land first, and send the playhead scrolling up to it.
    """
    branch = narration_branch(read())
    assert "S.group = null" in branch
    assert "S.groupBody = null" in branch


def test_the_reader_takes_a_voice_per_chunk():
    js = read()
    assert "Speech.segmentsFor(" in js, "the reader no longer asks for the voiced parts"
    # One key per line would put the narrator's name clip in the monster's voice.
    assert "cur.vkey" not in js
    # Every clip is asked for under a chunk's own key; nothing passes a
    # line-wide one, which is what would put the name in the monster's voice.
    # `role.key` is the voice lab previewing one seat: a seat key like the rest,
    # and it goes through ttsUrl at all because a preview in a different voice
    # from the narration would be a tester that lies.
    for call in re.findall(r"ttsUrl\(([^,]+),", js):
        assert call.strip() in ("key", "c.key", "ahead.key", "chunkKey(cur)",
                                "role.key"), call


def test_a_line_in_two_voices_settles_on_one_engine_before_it_starts():
    """The narrator's half and the speaker's half never split across engines.

    A cached name clip costs nothing and plays; the words behind it can still
    be refused — a game running out of budget mid-scene is exactly that case —
    and the line would arrive as Polly then the device. `cur.local` cannot
    prevent it: it is set by a failure that has already happened. So a line
    with more than one voice left in it fetches all of its clips first and
    hands the whole line to the browser if any is refused.
    """
    js = read().split("// ---- the reader ----")[-1]
    m = re.search(r"function voiceStartLine\(cur\) \{(.+?)\n  \}", js, re.S)
    assert m, "the line no longer starts through voiceStartLine"
    body = m.group(1)
    assert "voicesLeft(cur) < 2" in body            # only multi-voice lines wait
    assert "Promise.all(" in body                   # all of them, before any of them
    assert "voiceServerFailed(cur, err, token)" in body   # a refusal takes the line
    assert "cur.local = true" in body               # over the cap: never asked, still whole
    assert re.search(r"voiceSavePos\(\);\n      voiceStartLine\(cur\);", js), \
        "the pump no longer starts lines through it"


def test_the_line_in_flight_is_only_read_for_fields_it_carries():
    """Every `cur.x` / `V.current.x` the reader reads is one something sets.

    `cur` is a plain object built in one place and then read from a dozen —
    the speaking paths, the fallback, the prefetch, the status panel — so a
    field dropped from the constructor fails silently as `undefined` rather
    than loudly. That happened once already: the voice moved onto the chunks,
    `cur.phrase` went with it, and the narration panel went blank for every
    line while every test still passed.
    """
    # From the reader down: `cur` is a common local name and an initiative
    # entry higher up in the file is a different object entirely.
    js = read().split("// ---- the reader ----")[-1]
    assert js, "the reader section is no longer marked"
    literal = re.search(r"var cur = \{(.+?)\};", js, re.S)
    assert literal, "the reader no longer builds a `cur`"
    carried = set(re.findall(r"(\w+):", literal.group(1)))
    carried |= set(re.findall(r"cur\.(\w+) =", js))       # set later: token, local, ...
    read_back = set(re.findall(r"(?:cur|V\.current)\.(\w+)", js)) - {"classList"}
    assert read_back <= carried, f"read but never set: {sorted(read_back - carried)}"


def test_the_panel_does_not_reprint_the_line_it_is_reading():
    """The narration panel says *that* a line is being read, never the words.

    It used to print the line itself, and the line the playhead was parked on
    as well — which, once the transcript began revealing a line only as the
    narrator started it, made this panel the one place on the page that showed
    words before they were spoken. The transcript is where the words go; this
    panel says what the transcript cannot, which is that something is being
    read and in whose voice.
    """
    js = read()
    now = re.search(r"function voiceNowText\(\) \{(.+?)\n  \}", js, re.S)
    assert now, "app.js no longer has voiceNowText"
    body = now.group(1)
    assert "read by ' + voiceReaderName(" in body
    # Nothing in here may reach for the words of a line, spoken or queued.
    for forbidden in ("lineText", "phraseFor", "voicePhrase", "chunkText", ".text)"):
        assert forbidden not in body, f"the panel is printing the line again ({forbidden})"
    assert "V.current.phrase" not in js


def test_the_kind_label_in_that_panel_cannot_be_squeezed_into_a_column():
    """It was, on the deployed build, and it is a stylesheet fault not a text one.

    `.vt-now` is a flex row; the tag is a span beside an anonymous text item.
    With the line printed there the text took the row and the span — shrinkable
    by default, `min-width: auto` — came out one character wide, so
    "NARRATION" ran down the panel a letter to a line and stood the transport
    on its end. Dropping the text fixes the symptom; these two declarations are
    what stop anything else in that row ever doing it again.
    """
    with open(os.path.join(ROOT, "web", "static", "style.css"), "r", encoding="utf-8") as fh:
        css = fh.read()
    rule = re.search(r"\.vt-now \.vt-tag \{(.+?)\}", css, re.S)
    assert rule, "style.css no longer styles the narration panel's tag"
    assert "flex: 0 0 auto" in rule.group(1)
    assert "white-space: nowrap" in rule.group(1)
    # ... and the row itself cannot grow into a column, whatever it is given.
    now = re.search(r"\.vt-now \{(.+?)\}", css, re.S)
    assert now and "max-height" in now.group(1) and "overflow: hidden" in now.group(1)


# -- the theme toggle --------------------------------------------------------

def test_the_theme_cycle_does_not_ask_storage_what_it_is_showing():
    """A browser may refuse `localStorage` — Safari with cookies blocked, an
    embedded WebView, a full quota. `themeApply` swallows the write, which is
    right: the theme still applies, it just does not stick. But a cycle that
    read the value back from storage on the next click would be told "auto"
    every time and step to "light" for ever: a button that visibly works once
    and is then inert, and `dark` unreachable. It also mattered with storage
    working — two tabs stepped from each other's last write rather than from
    what each was showing.

    So the click reads the variable this page set, and storage is where the
    choice is kept between visits rather than between clicks.
    """
    js = read()
    click = re.search(r"\$\('btn-theme'\)\.addEventListener\('click', function \(\) \{(.+?)\}\);", js, re.S)
    assert click, "app.js no longer wires the theme button"
    assert "themeNow" in click.group(1)
    assert "themeStored" not in click.group(1) and "localStorage" not in click.group(1)

    # And the same for the system-theme listener, which asks the same question.
    mq = re.search(r"mq\.addEventListener\('change', function \(\) \{(.+?)\}\);", js, re.S)
    assert mq and "themeNow" in mq.group(1)

    # `themeApply` is the only writer, so the variable cannot drift from the
    # attribute on <html> that it sets in the same breath. (Its declaration is
    # not a write; anything else that assigns it is.)
    writes = [m for m in re.findall(r"[ \t]*(var )?themeNow = ", js) if not m]
    assert len(writes) == 1, "something other than themeApply assigns themeNow"
