"""The two themes have to stay a pair, and the palette has to stay legible.

`web/static/style.css` carries one set of custom properties in three blocks:
the dark values on `:root`, and the light values twice — once under
`prefers-color-scheme: light` for a visitor who has not chosen, once under
`:root[data-theme="light"]` for one who has. Nothing in a browser complains
when those two drift apart or when a token is added to one block and not the
others; the page simply falls back to the dark value on a light ground, which
is exactly the sort of thing nobody notices until a screenshot.

The contrast table below is the design commitment made when the themes were
written, in the only form that survives an edit: **every** run of text clears
WCAG AAA (7:1) against every surface it can land on, with the tiers spaced by
ratio — body text at 12:1, the secondary tier at 9.5:1, the quietest metadata
at 7:1 — so the hierarchy is carried by measured steps rather than by letting
the bottom step fall through the floor. Non-text carriers of meaning clear
4.5:1, half again the 3:1 the guideline asks for, and the two terrain fills on
the map clear 3:1 against the ground they sit on. A colour nudged "just a
little" that drops one of these fails here rather than in front of a reader.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSS = os.path.join(ROOT, "web", "static", "style.css")
APP_JS = os.path.join(ROOT, "web", "static", "app.js")

# Tokens that are not colours and so are only ever declared once, on :root.
NON_COLOUR = {"--radius", "--quad-h", "--mono", "--serif", "--sans"}


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def block(css: str, selector: str, start: int = 0) -> Dict[str, str]:
    """The declarations of the first `selector { ... }` at or after `start`."""
    at = css.index(selector, start)
    open_brace = css.index("{", at)
    depth, i = 1, open_brace + 1
    while depth:
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
        i += 1
    body = css[open_brace + 1 : i - 1]
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--[a-z-]+)\s*:\s*([^;]+);", body)}


def themes() -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    css = read(CSS)
    dark = block(css, ":root {")
    media = css.index("@media (prefers-color-scheme: light)")
    light_auto = block(css, ':root:not([data-theme="dark"])', media)
    light_chosen = block(css, ':root[data-theme="light"]')
    return dark, light_auto, light_chosen


def srgb(value: str) -> Tuple[float, float, float]:
    h = value.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def luminance(value: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in srgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# Every surface a run of text can land on. A colour is checked against all of
# them, not against the one it happens to be used on today: the card that moves
# from the sunken well onto a panel next month must not take a drop with it.
SURFACES = [
    "--bg", "--bg-panel", "--bg-sunken", "--bg-raise",
    "--bg-active", "--bg-speaking", "--bg-scene", "--glow",
]

# Where a side colour prints: panels, the card wells, and the two highlights.
CARDS = ["--bg-panel", "--bg-sunken", "--bg-active", "--bg-speaking"]

# (token, surfaces it is used on, floor). Everything that is text clears AAA;
# the three tiers are spaced so the hierarchy survives without anyone dropping
# to the floor. 4.5 is the non-text floor, set half again above the 3:1 the
# guideline asks for.
CONTRACT: List[Tuple[str, List[str], float]] = [
    ("--ink", SURFACES, 12.0),
    ("--ink-dim", SURFACES, 9.5),
    ("--ink-faint", SURFACES, 7.0),
    ("--accent", SURFACES, 8.0),
    ("--party", CARDS, 8.0),
    ("--enemy", CARDS, 8.0),
    ("--neutral", CARDS, 8.0),
    ("--good", CARDS, 8.0),
    ("--danger", SURFACES, 8.0),
    ("--line-strong", SURFACES, 4.5),
    ("--accent-line", SURFACES, 4.5),
    ("--hp-good", ["--bg-raise"], 4.5),
    ("--hp-hurt", ["--bg-raise"], 4.5),
    ("--hp-bad", ["--bg-raise"], 4.5),
    # The map's terrain is a graphical object that carries meaning, so it is
    # held to 1.4.11 against the ground it is painted on — and against the
    # panel, because the legend prints the same two swatches there and the
    # legend is how you learn what the board is showing.
    ("--wall", ["--bg-sunken", "--bg-panel"], 3.0),
    ("--difficult", ["--bg-sunken", "--bg-panel"], 3.0),
    # Bars: the fill against its own track, which is the pair a reader
    # actually sees rather than the fill against the page behind it.
    ("--accent-fill", ["--bg-sunken"], 3.0),
    ("--danger-fill", ["--bg-sunken"], 3.0),
]

# Text printed on a filled block, where the fill is the background.
ON_FILL: List[Tuple[str, str, float]] = [
    ("--on-accent", "--accent-fill", 8.0),
    ("--on-good", "--good-fill", 8.0),
    ("--on-danger", "--danger-fill", 8.0),
]


def test_the_light_theme_is_declared_identically_in_both_of_its_blocks():
    _, light_auto, light_chosen = themes()
    assert light_auto == light_chosen, (
        "the system-preference and the chosen light theme have drifted: "
        + repr({k: (light_auto.get(k), light_chosen.get(k)) for k in set(light_auto) ^ set(light_chosen)
                or [k for k in light_auto if light_auto[k] != light_chosen.get(k)]})
    )


def test_every_colour_token_exists_in_both_themes():
    dark, light, _ = themes()
    missing = sorted((set(dark) - NON_COLOUR) - set(light))
    assert not missing, f"defined dark-only, so the light page falls back to a dark value: {missing}"
    extra = sorted(set(light) - set(dark))
    assert not extra, f"defined light-only, so the dark page has no value at all: {extra}"


def test_every_var_reference_in_the_stylesheet_is_defined():
    css = read(CSS)
    dark, _, _ = themes()
    used = set(re.findall(r"var\((--[a-z-]+)", css))
    assert used <= set(dark), f"used but never declared: {sorted(used - set(dark))}"


def test_the_map_reads_tokens_the_stylesheet_actually_declares():
    """Every token `drawGrid` asks the theme for is one the theme declares.

    This catches a rename, which is the failure that leaves the map painting
    its hard-coded fallbacks on both themes while everything around it moves.
    It does not, and cannot from here, prove the map has no literals left in
    it — that is what a screenshot in each theme is for.
    """
    dark, _, _ = themes()
    read_by_js = set(re.findall(r"tok\('(--[a-z-]+)'", read(APP_JS)))
    assert read_by_js, "the canvas no longer reads the theme"
    assert read_by_js <= set(dark), f"read by app.js, absent from the stylesheet: {sorted(read_by_js - set(dark))}"


def test_both_palettes_clear_the_contrast_floors_they_were_built_to():
    dark, light, _ = themes()
    failures = []
    for name, palette in (("dark", dark), ("light", light)):
        for token, surfaces, floor in CONTRACT:
            for surface in surfaces:
                ratio = contrast(palette[token], palette[surface])
                if ratio + 1e-9 < floor:
                    failures.append(f"{name}: {token} on {surface} is {ratio:.2f}, needs {floor}")
        for ink, fill, floor in ON_FILL:
            ratio = contrast(palette[ink], palette[fill])
            if ratio + 1e-9 < floor:
                failures.append(f"{name}: {ink} on {fill} is {ratio:.2f}, needs {floor}")
    assert not failures, "\n".join(failures)
