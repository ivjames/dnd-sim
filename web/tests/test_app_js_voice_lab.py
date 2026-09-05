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
    for param in ("accent", "voice", "rate", "pitch", "volume"):
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
    and the seat would keep the shift for good."""
    js = read(APP_JS)
    m = re.search(r"resetBtn\.addEventListener\('click', function \(\) \{(.+?)\n    \}\);", js, re.S)
    assert m, "the auto button has gone"
    body = m.group(1)
    for field in (
        "accent", "voice", "rate", "pitch", "volume", "drc",
        "size", "growl", "cave", "ring", "tremolo", "muffle", "crush",
    ):
        assert field + ": null" in body, field


def test_the_treatment_travels_in_the_clip_url_too():
    js = read(APP_JS)
    q = re.search(r"function tuneQuery\(key\) \{(.+?)\n  \}", js, re.S)
    assert q and "'size', 'growl', 'cave'" in q.group(1)


def tune_query() -> str:
    js = read(APP_JS)
    m = re.search(r"function tuneQuery\(key\) \{(.+?)\n  \}", js, re.S)
    assert m, "tuneQuery has gone"
    return m.group(1)


def vl_row() -> str:
    js = read(APP_JS)
    m = re.search(r"\n  function vlRow\(role\) \{(.+?)\n  \}\n", js, re.S)
    assert m, "vlRow has gone"
    return m.group(1)


def test_the_compressor_travels_as_a_value_and_not_as_a_flag():
    """`drc` has three states and a checkbox has two, which is the whole trap.

    Unset means "leave whatever the cast decided alone"; `drc=0` means a
    listener switching off a compressor the cast turned on. Sent as a bare
    presence flag, the second would be indistinguishable from the first and a
    seat could never be un-compressed.
    """
    body = tune_query()
    assert "t.drc !== undefined && t.drc !== null" in body
    assert "'&drc=' + (t.drc ? '1' : '0')" in body

    # And the toggle stores the false rather than deleting the field, or the
    # three states collapse back into two on the way in.
    row = vl_row()
    assert re.search(r"vlToggle\(\s*'even out the loud and quiet'", row), (
        "the drc toggle is gone, or is labelled with the acronym again"
    )
    assert "vlSetTune(role.key, { drc: on })" in row


def test_the_creature_effects_travel_too():
    """The four that make it not a person. They ride with the three that were
    here first, in one list, because the server ignores every one of them on a
    seat that is not a monster — so none of them needs a test for the seat."""
    body = tune_query()
    for field in ("ring", "tremolo", "muffle", "crush"):
        assert "'" + field + "'" in body, field


def test_the_accent_is_not_the_voice_pickers_peer():
    """On the server a named voice wins outright and the accent is not even
    read. Offered side by side as two live selects they would read as filters
    that combine, and a listener would set an accent, hear no change, and
    conclude the lab is broken. So the accent goes dead while a voice is named
    — the same treatment as a slider the engine ignores — and a line under the
    two says which one is deciding."""
    row = vl_row()
    assert "el('select', 'vl-accent')" in row
    # Built from what the engine reports it can serve, not from a list baked in
    # here that would go stale the moment the pool changed.
    assert "spec.accents" in row
    # The accent is a language code to the server and an accent name to the
    # listener; the option carries both, each on the right side.
    assert "o.value = a.language" in row
    assert "a.accent" in row

    assert "accentSel.disabled = !canAccent || named" in row
    assert "accentSel.classList.add('vl-dead')" in row
    assert "precNote.textContent" in row
    assert "a named voice wins" in row
    # Dead, not cleared: setting the voice back to auto has to bring the
    # listener's accent back rather than having quietly thrown it away.
    assert "vlSetTune(role.key, { accent: null })" not in row


def test_volume_and_drc_are_offered_only_where_the_engine_honours_them():
    """Read from the roster's `ssml` list, exactly as `pitch` already is: Polly
    accepts `<prosody rate>` on neural and drops the rest, so which controls
    are real is the server's answer and never a guess from the engine's name."""
    row = vl_row()
    assert "var ssml = (spec && spec.ssml) || []" in row
    for flag, field in (("canPitch", "pitch"), ("canVolume", "volume"), ("canDrc", "drc")):
        assert "var %s = ssml.indexOf('%s') >= 0;" % (flag, field) in row, field

    # Shown dead rather than hidden, so the listener sees the control exists
    # and why it is not theirs — and disabled, so it cannot be moved into a
    # stored override the server would then drop on the floor.
    for guard in ("if (!canPitch) {", "if (!canVolume) {", "if (!canDrc) {"):
        assert guard in row, guard
    assert row.count("classList.add('vl-dead')") >= 4   # pitch, volume, drc, accent
    assert "vol.querySelector('input').disabled = true" in row
    assert "drc.querySelector('input').disabled = true" in row

    # The decibel range is the server's, not a pair of numbers picked here.
    assert "limits.volume" in row


def test_the_creature_effects_are_offered_on_monster_seats_only():
    """They are made out of the audio after Polly hands it over, and the server
    applies them to nothing else. A "broken" slider on the DM's row would move
    and change nothing, which is the failure the dead-control treatment exists
    to avoid — except here the control should not be there at all."""
    row = vl_row()
    marker = "if (Speech.isMonsterKey(role.key) && fxSpec"
    assert marker in row, "the monster-only branch has moved"
    before, after = row.split(marker, 1)
    for field in ("size", "growl", "cave", "ring", "tremolo", "muffle", "crush"):
        assert "fxSpec." + field in after, field
        assert "fxSpec." + field not in before, field

    # Named for what they sound like. "ring" is a modulator to `tts/dsp.py` and
    # nothing at all to somebody listening.
    for label in ("'unearthly'", "'wobble'", "'muffled'", "'broken'"):
        assert "label: " + label in after, label
    # Each with a hint saying what it does to the voice, since the label alone
    # cannot carry it.
    assert after.count("hint:") == 7

    # A roster from a build that predates one of these carries no bounds for
    # it, and a slider with no range throws on min.
    assert "if (!c.spec) return;" in after
