"""Cue routing in `web/static/cues.js`, checked against `tools/audio/cues.py`.

The rules that decide which sound an event fires are written twice: in Python,
where the picker and the fetcher read them, and in JavaScript, where the page
plays them. That is a deliberate duplication — the browser cannot import the
Python — and it is only safe while the two agree, so this drives both over the
same events and compares the answers rather than checking either alone.

The events are generated FROM the cue table: one that satisfies each rule, one
that satisfies all but one constraint of it, and one that offers `1` where the
rule says `true`. A new cue therefore arrives with its own cases already
written, which is the point.
"""

from __future__ import annotations

import json
import os

import pytest

from tools.audio import cues as C

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CUES_JS = os.path.join(ROOT, "web", "static", "cues.js")
MANIFEST = os.path.join(ROOT, "audio", "manifest.json")

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None, reason="node not installed"
)


def _run(body: str, arg: object = None) -> object:
    """`run_js`, but with `S` bound to cues.js rather than speech.js."""
    import json as _json
    import subprocess

    script = (
        "const S = require(%s);\n"
        "const IN = JSON.parse(process.argv[1] || 'null');\n"
        "const out = (function () {\n%s\n})();\n"
        "process.stdout.write(JSON.stringify(out === undefined ? null : out));\n"
    ) % (_json.dumps(CUES_JS), body)
    proc = subprocess.run(
        ["node", "-e", script, _json.dumps(arg)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return _json.loads(proc.stdout)


def _set(data: dict, path: str, value) -> None:
    cur = data
    parts = path.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _event(match: dict, drop: str | None = None, swap_bools: bool = False) -> dict:
    """An event built to satisfy `match`, minus `drop`, optionally with its
    booleans written as 1/0 — which is what `event_matches` refuses."""
    data: dict = {}
    for path, want in (match.get("data") or {}).items():
        if path == drop:
            continue
        if swap_bools and isinstance(want, bool):
            want = 1 if want else 0
        _set(data, path, want)
    return {"seq": 1, "round": 1, "kind": match["kind"], "actor": None, "text": "", "data": data}


def _cases() -> list[dict]:
    out: list[dict] = []
    for cue in C.CUES:
        if not cue.match:
            continue
        out.append(_event(cue.match))
        for path in (cue.match.get("data") or {}):
            out.append(_event(cue.match, drop=path))
        if any(isinstance(v, bool) for v in (cue.match.get("data") or {}).values()):
            out.append(_event(cue.match, swap_bools=True))
    # Every kind the engine can emit, carrying nothing: the bare case.
    from engine.events import EVENT_KINDS  # noqa: PLC0415

    for kind in sorted(EVENT_KINDS):
        out.append({"seq": 1, "round": 1, "kind": kind, "actor": None, "text": "", "data": {}})
    return out


def _table() -> list[dict]:
    """The cue table as the page sees it: id, group and rule, in table order."""
    return [{"id": c.id, "group": c.group, "match": c.match} for c in C.CUES]


def test_the_two_cue_tables_route_every_event_the_same_way():
    events = _cases()
    js = _run(
        "return IN.events.map(function (ev) {"
        "  return S.cuesForEvent(ev, IN.cues).map(function (c) { return c.id; });"
        "});",
        {"events": events, "cues": _table()},
    )
    py = [[c.id for c in C.cues_for_event(ev)] for ev in events]
    for ev, a, b in zip(events, js, py):
        assert a == b, f"{ev['kind']} {ev['data']}: cues.js says {a}, cues.py says {b}"
    # A test that fired nothing would pass vacuously.
    assert any(py), "no event in the generated set fires a cue"


def test_one_cue_per_group_and_the_most_specific_wins():
    """A crit is a hit; the sting that names both beats the one that names one."""
    cues = _table()
    ev = {"kind": "attack", "data": {"hit": True, "crit": True}}
    got = _run("return S.cuesForEvent(IN.ev, IN.cues).map(function (c) { return c.group; });",
               {"ev": ev, "cues": cues})
    assert len(got) == len(set(got)), f"more than one cue in a group: {got}"
    sting = _run("var c = S.cueForEvent(IN.ev, 'sting', IN.cues); return c && c.id;",
                 {"ev": ev, "cues": cues})
    assert sting == "sting_crit"
    assert sting == C.cue_for_event(ev, "sting").id


def test_a_rule_that_says_true_is_not_satisfied_by_one():
    cues = _table()
    got = _run("return S.cuesForEvent(IN.ev, IN.cues).map(function (c) { return c.id; });",
               {"ev": {"kind": "attack", "data": {"hit": 1, "crit": 1}}, "cues": cues})
    assert got == []
    assert C.cues_for_event({"kind": "attack", "data": {"hit": 1, "crit": 1}}) == []


def test_beds_are_the_looping_layers_and_nothing_else_is():
    groups = _run("return S.GROUPS.map(function (g) { return [g, !!S.BEDS[g]]; });")
    assert [g for g, _ in groups] == list(C.GROUPS)
    assert [g for g, bed in groups if bed] == ["music", "ambience"]


def test_the_committed_pack_routes_through_the_same_rules():
    """Not a copy of the table: what `tools.audio fetch` actually wrote."""
    with open(MANIFEST, encoding="utf-8") as fh:
        doc = json.load(fh)
    ids = _run("return S.fromManifest(IN).map(function (c) { return c.id; });", doc)
    assert ids == list(doc["cues"]), "fromManifest lost the manifest's order"
    fired = _run(
        "var cues = S.fromManifest(IN); "
        "return S.cuesForEvent({kind: 'combat_start', data: {}}, cues)"
        "  .map(function (c) { return c.id; });",
        doc,
    )
    # Whatever the pack holds, the answer is what the table says for the same
    # subset — including the empty answer, if nobody has picked these yet.
    subset = tuple(C.CUES_BY_ID[i] for i in doc["cues"] if i in C.CUES_BY_ID)
    assert fired == [c.id for c in C.cues_for_event({"kind": "combat_start", "data": {}}, subset)]


def test_gain_is_read_as_decibels():
    assert _run("return [S.gainOf(0), S.gainOf(-6), S.gainOf('x')];") == \
        pytest.approx([1.0, 0.5011872336272722, 1.0])


def test_a_cue_with_no_end_trim_plays_to_the_end():
    """The bug this function exists for.

    `Number(null)` is 0, not NaN, so the obvious inline version reads "no end
    trim" — which is what the pack records for nearly every cue — as "trimmed
    to nothing". Beds then loop at position zero and stings are cut a fraction
    of a second in: silence that looks like a broken pack and is not one.
    """
    got = _run(
        "return IN.map(function (c) { var t = S.trimOf(c);"
        "  return [t.from, isFinite(t.to) ? t.to : null]; });",
        [
            {"trim_start_s": 0, "trim_end_s": None},   # the whole committed pack
            {},                                         # a manifest without the keys
            {"trim_start_s": 2, "trim_end_s": 9},       # a real trim
            {"trim_start_s": 5, "trim_end_s": 5},       # nobody picks an empty cue
            {"trim_start_s": -3, "trim_end_s": "8"},    # junk in, sense out
        ],
    )
    assert got == [[0, None], [0, None], [2, 9], [5, None], [0, 8]]


def test_the_committed_pack_is_untrimmed(pack=MANIFEST):
    """Which is why the bug above was silent until something played it."""
    with open(pack, encoding="utf-8") as fh:
        cues = json.load(fh)["cues"].values()
    assert all(c["trim_start_s"] == 0 and c["trim_end_s"] is None for c in cues)


def test_the_asset_url_carries_the_packs_digest():
    url = _run("return S.assetUrl('/audio/', {file: 'assets/music/a.mp3'}, 'abc123');")
    assert url == "/audio/assets/music/a.mp3?v=abc123"
