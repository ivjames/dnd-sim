"""The new-game panel's fields exist in both files that have to agree on them.

`web/static/app.js` reads and writes the panel by element id; `index.html`
defines them. Nothing else checks that the two agree, and a typo'd id fails
silently in the browser — `$()` returns null and the field is simply ignored,
so a game starts with a default the operator did not choose.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(ROOT, "web", "static", "app.js")
INDEX = os.path.join(ROOT, "web", "static", "index.html")


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_every_new_game_id_app_js_touches_is_defined_in_the_page():
    js, html = read(APP_JS), read(INDEX)
    ids = set(re.findall(r"""\$\('(ng-[a-z0-9-]+)'\)""", js))
    assert "ng-temp" in ids, "the improv field is no longer read from the panel"
    defined = set(re.findall(r'''id="(ng-[a-z0-9-]+)"''', html))
    assert ids <= defined, f"ids used but not defined: {sorted(ids - defined)}"


def test_the_panel_sends_a_player_temperature_in_range():
    js = read(APP_JS)
    assert "cfg.player_temperature" in js
    # clamped client-side as well as in GameConfig.from_dict
    assert re.search(r"Math\.min\(1,\s*Math\.max\(0,\s*num\(\$\('ng-temp'\)", js)


def test_every_element_app_js_reaches_for_is_defined_in_the_page():
    """The `ng-` check above, widened: `$()` returns null for an id the page
    does not define and the line then throws, which for the write-access gate
    would leave the controls in whatever state the last render left them."""
    js, html = read(APP_JS), read(INDEX)
    used = set(re.findall(r"""\$\('([a-zA-Z0-9_-]+)'\)""", js))
    defined = set(re.findall(r'''id="([a-zA-Z0-9_-]+)"''', html))
    assert used <= defined, f"ids used but not defined: {sorted(used - defined)}"


def test_the_panel_offers_a_voice_age_per_seat():
    """The control that answers "why is Father Bexley a child?".

    Casting reads `age` off the party member; without somewhere to set it, the
    only way to say a character is a child is to edit the scenario's JSON.
    """
    js, html = read(APP_JS), read(INDEX)
    assert 'id="ng-party"' in html and "$('ng-party')" in js
    # Rendered from the chosen preset's own party, and read back into the
    # config that is submitted — a control that is only rendered is a decoration.
    assert "renderPartySeats(cfg.party)" in js
    assert "applyPartySeats(cfg.party)" in js
    # Rows are built per seat and keyed by index, which is the same order the
    # submitted `cfg.party` is in (it is a deep copy of the preset's).
    assert "'ng-age-' + i" in js


def test_the_panel_offers_pronouns_per_seat():
    """The other trait that narrows who a seat can be dealt.

    A character's pronouns are a fact its persona already carries, which is why
    this row can be asked at all where a gender picker could not: the panel is
    reading back what the character says about itself, not choosing for it.
    """
    js = read(APP_JS)
    assert "'ng-pronouns-' + i" in js
    m = re.search(r"var PRONOUN_CHOICES = \[([^\]]*)\]", js)
    assert m, "the panel no longer offers a pronoun set"
    assert [s.strip().strip("'") for s in m.group(1).split(",")] == \
        ["she/her", "he/him", "they/them"]


def test_every_pronoun_the_panel_offers_is_one_the_server_reads():
    """The panel's list and `gender_for_pronouns` have to agree, or the row
    shows an answer the casting does not act on."""
    from tts.voices import gender_for_pronouns      # noqa: PLC0415

    js = read(APP_JS)
    offered = re.findall(r"'([a-z]+/[a-z]+)'", re.search(
        r"var PRONOUN_CHOICES = \[([^\]]*)\]", js).group(1))
    assert {said: gender_for_pronouns(said) for said in offered} == {
        "she/her": "female", "he/him": "male", "they/them": "",
    }


def test_a_config_that_states_something_else_keeps_it_verbatim():
    """The three offered are not a taxonomy of who a character may be. A
    scenario that states a set the panel does not list gets that set as its own
    option, so opening the panel and touching nothing cannot round a character
    off to a neighbour on submit."""
    js = read(APP_JS)
    m = re.search(r"function pronounOptions\((.+?)\n  \}", js, re.S)
    assert m, "the panel no longer keeps a config's own pronouns"
    body = m.group(1)
    assert "opts.push(s)" in body          # unlisted: added rather than dropped
    assert "opts[at] = s" in body          # listed but spelled differently: theirs wins


def test_choosing_adult_or_unstated_states_nothing_at_all():
    """An unstated age already casts as an adult and unstated pronouns already
    cast from the whole pool, so the panel writes only the answers that change
    something. Stating either back into every scenario's config would be
    writing a fact about a character that nobody chose."""
    js = read(APP_JS)
    m = re.search(r"function applyPartySeats\((.+?)\n  \}", js, re.S)
    assert m, "the panel no longer writes its answers back"
    body = m.group(1)
    assert "member.age = 'child'" in body
    assert "delete member.age" in body
    assert "member.pronouns = pro.value" in body
    assert "delete member.pronouns" in body
    # Stated pronouns drop a legacy `gender`, which the server ignores anyway
    # once pronouns are present; leaving both would leave the config arguing
    # with itself.
    assert "delete member.gender" in body


#: Strings the two implementations of "is this a child" have to agree on. The
#: interesting ones are where `Number()` and Python's `float()` part company —
#: JavaScript reads `0x`/`0b`/`0o` literals that `float()` rejects, `float()`
#: reads the underscores that `Number()` rejects — because either disagreement
#: is a select that shows one thing while the server casts another, and
#: submitting the panel then rewrites the character's age to match the wrong
#: one. Reported by review on #25 and the reason `NUMERIC_AGE` exists.
AGE_CORPUS = [
    "child", "kid", "boy", "girl", "adult", "elder", "elderly", "old", "grown-up",
    "", "   ", "Child", " KID ", "ancient-ish", "old enough", "twelve",
    "0xA", "0xa", "0b1010", "0o14", "1_0", "1_2.5", "inf", "Infinity", "-inf", "nan",
    "0", "-3", "+12", "12", "12.", "12.0", "12.5", ".5", "13", "1e1", "1e3",
    "1e-400", "1e999", "4000", " 9 ", "9.999", "1e400",
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_panel_and_the_server_read_an_age_the_same_way():
    """Run both implementations over one corpus rather than trusting the shapes.

    `app.js` is an IIFE that exports nothing, so the block is lifted out of the
    source and evaluated — which also means a rename or a reorder fails here
    loudly instead of quietly stopping the comparison.
    """
    from tts.voices import normalize_age      # noqa: PLC0415

    js = read(APP_JS)
    m = re.search(r"(var CHILD_AGES = .*?\n  \})", js, re.S)
    assert m, "the panel's age reader is no longer where this test can find it"
    script = (
        m.group(1)
        + "\nprocess.stdout.write(JSON.stringify(JSON.parse(process.argv[1]).map(isChildAge)));\n"
    )
    proc = subprocess.run(
        ["node", "-e", script, json.dumps(AGE_CORPUS)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    panel = json.loads(proc.stdout)
    server = [normalize_age(said) == "child" for said in AGE_CORPUS]
    disagree = [said for said, a, b in zip(AGE_CORPUS, panel, server) if a != b]
    assert not disagree, f"panel and server disagree on: {disagree}"
    # And the corpus is doing work: it has to contain both answers.
    assert any(server) and not all(server)


def test_the_panels_idea_of_a_child_is_the_servers():
    """`isChildAge` in app.js decides which way the select starts; `voices.py`
    decides the casting. Disagreeing means a scenario that says `"age": 9` is
    shown as an adult and then cast as a child, or the reverse — a control that
    lies about the state it is showing."""
    from tts.voices import AGES, CHILD_MAX_AGE      # noqa: PLC0415

    js = read(APP_JS)
    m = re.search(r"var CHILD_AGES = \{([^}]*)\}", js)
    assert m, "the panel no longer knows which words mean a child"
    words = set(re.findall(r"([a-z-]+):", m.group(1)))
    assert words == {word for word, meaning in AGES.items() if meaning == "child"}

    cap = re.search(r"var CHILD_MAX_AGE = (\d+);", js)
    assert cap and int(cap.group(1)) == CHILD_MAX_AGE


def test_the_write_controls_are_hidden_behind_the_gate():
    """Anonymous spectating is the product, so the page must not show controls
    that can only 401 — and `#ctl-note` must stay outside the hidden wrapper,
    because load failures are reported there to every spectator."""
    js, html = read(APP_JS), read(INDEX)
    for element in ("btn-new", "btn-unlock", "write-controls", "write-locked"):
        assert 'id="%s"' % element in html, element
        assert "$('%s')" % element in js, element
    # the wrapper holds the buttons and the note form, and nothing else
    wrapper = re.search(
        r'<div id="write-controls" hidden>(.*?)\n      </div>', html, re.S
    )
    assert wrapper, "the write-controls wrapper is gone"
    assert 'id="btn-pause"' in wrapper.group(1)
    assert 'id="note-form"' in wrapper.group(1)
    assert 'id="ctl-note"' not in wrapper.group(1)


def test_the_token_is_not_written_into_the_page():
    """It belongs in `.env` on the droplet and in the visitor's own browser."""
    html = read(INDEX)
    assert "DND_WRITE_TOKEN" in html, "the panel should name the variable to set"
    assert not re.search(r'value="[^"]*"[^>]*id="ul-token"', html)
    assert re.search(r'id="ul-token"[^>]*type="password"', html)
