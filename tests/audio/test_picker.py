"""The picker page: what gets baked into it, and whether it holds together.

The page is a browser thing with no test harness of its own, so this checks
the parts that break silently — a lost placeholder, an id the script reaches
for that the markup does not have, JSON that ends the `<script>` early — and
hands the script itself to `node --check` where node exists.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from tools.audio import build as B
from tools.audio import cues as C

HTML = B.TEMPLATE.read_text(encoding="utf-8")
SCRIPT = re.search(r'<script>\n(.*)</script>', HTML, re.S).group(1)


def test_the_template_keeps_both_placeholders():
    assert HTML.count(B.CANDIDATES_TOKEN) == 1
    assert HTML.count(B.CUES_TOKEN) == 1


def test_render_bakes_the_cue_table_in_even_with_no_candidates():
    page = B.render(None)
    assert B.CANDIDATES_TOKEN not in page and B.CUES_TOKEN not in page
    assert "const DATA = null;" in page
    assert '"sting_crit"' in page
    for cue in C.CUES:
        assert f'"{cue.id}"' in page


def test_render_bakes_candidates_in():
    doc = {"version": 1, "generated": "2026-09-04T00:00:00+00:00", "sources": ["freesound"],
           "cues": {"sfx_dice": {"cue": C.cue("sfx_dice").to_dict(), "candidates": [
               {"key": "freesound:1", "title": "d20 on oak", "preview_url": "https://x.invalid/1.mp3"}]}}}
    page = B.render(doc)
    assert "d20 on oak" in page
    assert "2026-09-04T00:00:00+00:00" in page


def test_a_candidate_cannot_end_the_script_early():
    """A title containing `</script>` would otherwise close the tag."""
    doc = {"cues": {"sfx_dice": {"cue": C.cue("sfx_dice").to_dict(),
                                 "candidates": [{"title": "</script><script>alert(1)</script>"}]}}}
    page = B.render(doc)
    body = page.split("<script>")[-1]
    assert "</script>" not in body.split("</script>\n</body>")[0]
    assert "<\\/script>" in page


def test_a_broken_template_is_a_loud_failure():
    with pytest.raises(RuntimeError, match="placeholder"):
        B.render(None, template="<html>no placeholders here</html>")


def test_build_writes_next_to_the_candidates(tmp_path):
    cand = tmp_path / "candidates.json"
    cand.write_text(json.dumps({"cues": {}}))
    out = B.build(cand, tmp_path / "picker.html")
    assert out.exists() and "const DATA = " in out.read_text()


def test_build_without_a_candidates_file_still_writes_a_usable_page(tmp_path):
    out = B.build(tmp_path / "missing.json", tmp_path / "picker.html")
    assert "const DATA = null;" in out.read_text()


def test_every_id_the_script_touches_exists_in_the_markup():
    markup = HTML.split("<script>")[0]
    ids = set(re.findall(r'\bid="([^"]+)"', markup))
    # ids the script creates itself, inside templates it renders
    rendered = set(re.findall(r'\bid="([^"]+)"', SCRIPT))
    wanted = set(re.findall(r'\$\("([^"]+)"\)', SCRIPT))
    dynamic = {w for w in wanted if any(w.startswith(r) for r in rendered)}
    missing = wanted - ids - rendered - dynamic
    assert not missing, f"the script reaches for ids that are not in the page: {sorted(missing)}"


def test_the_keyboard_map_and_the_footer_agree():
    for key in ("j", "k", "space", "enter", "x", "n", "p"):
        assert f"<kbd>{key}</kbd>" in HTML


def test_the_page_declares_the_permissive_licences_the_fetcher_enforces():
    from tools.audio.sources import PERMISSIVE
    listed = re.search(r'const PERMISSIVE = \[(.*?)\];', SCRIPT, re.S).group(1)
    assert {x.strip().strip('"') for x in listed.split(",")} == set(PERMISSIVE), (
        "picker.html and sources.PERMISSIVE disagree about what is shippable"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_script_parses():
    page = B.render({"cues": {}})
    script = re.search(r'<script>\n(.*)</script>', page, re.S).group(1)
    proc = subprocess.run(["node", "--check", "-"], input=script, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
