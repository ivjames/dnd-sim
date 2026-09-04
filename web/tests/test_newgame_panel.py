"""The new-game panel's fields exist in both files that have to agree on them.

`web/static/app.js` reads and writes the panel by element id; `index.html`
defines them. Nothing else checks that the two agree, and a typo'd id fails
silently in the browser — `$()` returns null and the field is simply ignored,
so a game starts with a default the operator did not choose.
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
