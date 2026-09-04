"""Bake a candidates file into the picker page.

The picker is a tracked template with two placeholders; building it means
substituting JSON for them and writing the result next to the candidates. The
output is one self-contained file that opens off the filesystem — no server, no
CORS, nothing to install. Audio still streams from the source, so the machine
doing the picking needs the network.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import cues as C

__all__ = ["TEMPLATE", "CANDIDATES_TOKEN", "CUES_TOKEN", "render", "build"]

TEMPLATE = Path(__file__).with_name("picker.html")
CANDIDATES_TOKEN = "/*__CANDIDATES__*/null"
CUES_TOKEN = "/*__CUES__*/null"


def _blob(value) -> str:
    """JSON safe to drop inside a <script>: no `</script>`, no U+2028/9."""
    return (json.dumps(value, ensure_ascii=False)
            .replace("</", "<\\/")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def render(candidates: dict | None, template: str | None = None) -> str:
    html = TEMPLATE.read_text(encoding="utf-8") if template is None else template
    for token in (CANDIDATES_TOKEN, CUES_TOKEN):
        if token not in html:
            raise RuntimeError(f"picker template lost its {token} placeholder")
    html = html.replace(CANDIDATES_TOKEN, _blob(candidates))
    html = html.replace(CUES_TOKEN, _blob([c.to_dict() for c in C.CUES]))
    return html


def build(candidates_path: Path, out: Path) -> Path:
    doc = None
    if candidates_path.exists():
        doc = json.loads(candidates_path.read_text(encoding="utf-8"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(doc), encoding="utf-8")
    return out
