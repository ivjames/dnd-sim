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
