"""Run every cue's search terms against the available sources and write the
candidate set the picker screen loads.

Output is one JSON file, `candidates.json`:

    {"version": 1, "generated": "...", "sources": [...], "skipped": [...],
     "cues": {"<cue id>": {"cue": {...}, "candidates": [{...}, ...]}}}

Nothing is filtered on quality — that is what the ears are for. What *is*
filtered: licenses outside the permissive set (a candidate you cannot ship is
noise in the list), duplicates within a cue, and anything a source returns
without a playable URL.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import cues as C
from .sources import PERMISSIVE, Candidate, Source, build_sources

__all__ = ["harvest", "select_cues", "write_candidates", "SOURCE_GROUPS", "MIN_INTERVAL"]

# Which source is worth asking for which kind of cue. Jamendo is full tracks,
# so it never answers a two-second sting; the Archive's short-file metadata is
# too unreliable to be worth the request. incompetech carries a Stings genre
# alongside the beds, but it is a music catalogue and never a door creak.
SOURCE_GROUPS = {
    "freesound": ("music", "ambience", "sting", "swell", "sfx"),
    "jamendo": ("music",),
    "incompetech": ("music", "ambience", "sting", "swell"),
    "archive": ("music", "ambience"),
}

# Seconds between calls, per source. Freesound allows 60/minute and answers
# a 429 when pushed; the others are unmetered but not free to us either.
# incompetech is zero because it fetches its catalogue once and then searches
# in memory — the per-query cost is nothing.
MIN_INTERVAL = {"freesound": 1.1, "jamendo": 0.3, "incompetech": 0.0, "archive": 0.3}


def select_cues(*, groups: tuple[str, ...] = (), ids: tuple[str, ...] = (),
                required_only: bool = False) -> list[C.Cue]:
    out = list(C.CUES)
    if groups:
        out = [c for c in out if c.group in groups]
    if ids:
        want = set(ids)
        out = [c for c in out if c.id in want]
        missing = want - {c.id for c in out}
        if missing:
            raise SystemExit(f"unknown cue id(s): {', '.join(sorted(missing))}")
    if required_only:
        out = [c for c in out if c.required]
    return out


def harvest(cues: list[C.Cue], sources: list[Source], *, per_query: int = 8,
            licenses: tuple[str, ...] = PERMISSIVE, log=print,
            sleep=time.sleep) -> dict:
    last: dict[str, float] = {}
    per_cue: dict[str, list[Candidate]] = {}
    errors: list[str] = []

    for cue in cues:
        found: dict[str, Candidate] = {}
        for src in sources:
            if cue.group not in SOURCE_GROUPS.get(src.name, ()):
                continue
            for query in cue.queries:
                gap = MIN_INTERVAL.get(src.name, 0.5)
                waited = time.monotonic() - last.get(src.name, 0.0)
                if waited < gap:
                    sleep(gap - waited)
                last[src.name] = time.monotonic()
                try:
                    hits = src.search(query, dur=cue.dur, limit=per_query)
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    errors.append(f"{cue.id} / {src.name} / {query!r}: {exc}")
                    continue
                for cand in hits:
                    if cand.license not in licenses:
                        continue
                    found.setdefault(cand.key, cand)
        per_cue[cue.id] = list(found.values())
        log(f"  {cue.id:<28} {len(found):>3} candidates")

    return {
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [s.name for s in sources],
        "licenses": list(licenses),
        "errors": errors,
        "cues": {
            cue.id: {
                "cue": cue.to_dict(),
                "candidates": [c.to_dict() for c in per_cue.get(cue.id, [])],
            }
            for cue in cues
        },
    }


def write_candidates(doc: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    return path


def merge(old: dict | None, new: dict) -> dict:
    """Fold a fresh harvest into an earlier one, keeping cues not re-run.

    Re-running one cue should not throw away the other forty-nine, and a
    re-run of the same cue should replace it rather than pile duplicates in.
    """
    if not old:
        return new
    merged = dict(old)
    merged.update({k: v for k, v in new.items() if k != "cues"})
    cues = dict(old.get("cues") or {})
    cues.update(new.get("cues") or {})
    merged["cues"] = cues
    return merged


def run(*, out: Path, groups=(), ids=(), required_only=False, per_query=8,
        only_sources=None, env=None, log=print) -> dict:
    chosen = select_cues(groups=groups, ids=ids, required_only=required_only)
    with httpx.Client(timeout=30.0, follow_redirects=True,
                      headers={"User-Agent": "dnd-sim-audio-sourcing/1"}) as client:
        live, skipped = build_sources(client, env=env, only=only_sources)
        for note in skipped:
            log(f"skipped {note}")
        if not live:
            raise SystemExit("no usable sources; see AUDIO.md for the two free keys")
        log(f"harvesting {len(chosen)} cues from {', '.join(s.name for s in live)}")
        doc = harvest(chosen, live, per_query=per_query, log=log)
    doc["skipped"] = skipped
    existing = None
    if out.exists():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
    doc = merge(existing, doc)
    write_candidates(doc, out)
    total = sum(len(v["candidates"]) for v in doc["cues"].values())
    log(f"wrote {out} — {len(doc['cues'])} cues, {total} candidates")
    if doc.get("errors"):
        log(f"{len(doc['errors'])} search errors (see the file's `errors` list)")
    return doc
