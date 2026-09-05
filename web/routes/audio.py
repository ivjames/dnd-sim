"""The score: the picked audio pack, and the cue rules that route events to it.

`tools/audio` searches the openly-licensed libraries, and `fetch` writes
`audio/manifest.json` beside the files it downloaded — each cue's playback
knobs and its `match` rule, copied out of `cues.py`, so that routing an event
to a sound needs the manifest and nothing else (AUDIO.md). Until this module
existed nothing served either half, so the pack was committed and unreachable:
`/audio/assets/...` was a 404 and no page had ever read a manifest. These two
routes are what `web/static/cues.js` reads.

Three rules worth stating, because each is a decision rather than a detail:

- **The manifest is the allowlist.** The asset route serves a file only if some
  cue names it. The same directory holds `config.json` — the picker's output,
  carrying source URLs and per-source ids — and `CREDITS.md`, and neither is
  part of what a player needs.
- **Clips are immutable and the digest is the cache-buster**, exactly as the
  Polly route does it: the bytes under a given name change only when someone
  re-fetches the pack, and `?v=<digest>` moves when the manifest does. Without
  it a re-picked bed would go on playing out of every spectator's cache.
- **The credit sentence is read, not written.** Most of this pack is CC BY, so
  attribution is a licence condition the moment a listener can hear it — and
  the wording each source requires is decided by `tools/audio` and recorded in
  the manifest as `credit_text`. Neither this module nor the browser rebuilds
  it: `tools/` is a dev tool that stays off the runtime path, and the way to
  keep it there is for the generated file to carry the finished sentence.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any

from flask import Blueprint, current_app, jsonify, send_from_directory

bp = Blueprint("audio", __name__)

#: Where the pack lives, unless `DND_AUDIO_DIR` says otherwise. `tools.audio`
#: writes to ./audio by default and the whole pack is tracked, so this is the
#: checkout's own directory rather than anything a deploy has to create.
DEFAULT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "audio",
)

#: a year: the URL carries the pack's digest, so these bytes never change
IMMUTABLE = "public, max-age=31536000, immutable"

#: What a cue's entry hands the player. The manifest also records `sha256`,
#: `bytes`, `source_url` and the raw `credit` block, which are how the pack is
#: verified and rebuilt — not how it is played.
CUE_FIELDS = (
    "group", "match", "loop", "gain_db", "duration",
    "fade_in_ms", "fade_out_ms", "trim_start_s", "trim_end_s", "when",
)

_lock = threading.Lock()
_cached: dict[str, Any] = {}


def _dir() -> str:
    return current_app.config.get("DND_AUDIO_DIR") or DEFAULT_DIR


def _stamp(path: str):
    """(mtime, size), or None where there is no manifest to read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _load(root: str) -> dict:
    """The parsed pack for `root`, cached until its manifest changes on disk.

    Re-picking the audio rewrites the manifest under a running process, and a
    deploy restarts that process anyway — so the cheap check is the honest one:
    stat the file, reread it only when it has moved.
    """
    path = os.path.join(root, "manifest.json")
    stamp = _stamp(path)
    with _lock:
        if _cached.get("root") == root and _cached.get("stamp") == stamp:
            return _cached["pack"]
    pack = _read(root, path, stamp)
    with _lock:
        _cached.update({"root": root, "stamp": stamp, "pack": pack})
    return pack


def _read(root: str, path: str, stamp) -> dict:
    if stamp is None:
        return {"payload": {"available": False, "reason": "no audio pack on this server"},
                "files": frozenset()}
    try:
        raw = open(path, "rb").read()
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        current_app.logger.warning("unreadable audio manifest at %s: %s", path, exc)
        return {"payload": {"available": False, "reason": f"unreadable manifest: {exc}"},
                "files": frozenset()}

    digest = hashlib.sha256(raw).hexdigest()[:12]
    cues: list[dict] = []
    files: set[str] = set()
    # A LIST, where the manifest on disk keys by cue id. Order is the tie-break
    # when two cues in a group match one event equally well (`cues.js`), and on
    # disk it is cue-table order (`fetch.plan`) — but a JSON object's order is
    # not something to lean on across a serializer that may sort keys, or a
    # client that may not preserve them. Ordering that matters travels as an
    # array.
    for cue_id, entry in (doc.get("cues") or {}).items():
        rel = str(entry.get("file") or "").strip()
        if not rel or not _plays(root, rel):
            continue
        out = {k: entry.get(k) for k in CUE_FIELDS}
        out["id"] = cue_id
        out["file"] = rel
        out["credit"] = str(entry.get("credit_text") or "")
        if not out["credit"]:
            # A pack fetched before the manifest carried the sentence. The
            # audio still plays; nothing on the page can attribute it, which
            # for a CC BY file is a licence problem rather than a cosmetic one.
            current_app.logger.warning(
                "audio cue %s has no credit_text; re-run `python -m tools.audio fetch`", cue_id
            )
        cues.append(out)
        files.add(rel)

    payload = {
        "available": bool(cues),
        "version": doc.get("version"),
        "generated": doc.get("generated"),
        "digest": digest,
        "base": "/audio/",
        "cues": cues,
    }
    if not cues:
        payload["reason"] = "the audio pack names no playable file"
    return {"payload": payload, "files": frozenset(files)}


def _plays(root: str, rel: str) -> bool:
    """A relative path inside `root` that is really there.

    A manifest is a tracked file rather than user input, but it is also the
    thing this route trusts to name what may be served — so the traversal check
    is here rather than left to the one `send_from_directory` does later.
    """
    if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
        return False
    return os.path.isfile(os.path.join(root, rel))


@bp.get("/api/audio")
def manifest():
    """The pack, or why there is none. Never an error: a server with no audio
    is a page with no score, which is what every page was until now."""
    return jsonify(_load(_dir())["payload"])


@bp.get("/audio/<path:name>")
def asset(name: str):
    pack = _load(_dir())
    if name not in pack["files"]:
        return jsonify({"error": "no such cue file"}), 404
    resp = send_from_directory(_dir(), name, conditional=True)
    resp.headers["Cache-Control"] = IMMUTABLE
    return resp
