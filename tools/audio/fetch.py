"""Turn a picked configuration into files on disk, a manifest, and credits.

Input is whatever the picker's "Copy configuration" produced. Output is a
directory that a player only has to be pointed at:

    assets/<group>/<cue id>.<ext>   the audio
    manifest.json                   cue → file + playback knobs + match rule + credit
    CREDITS.md                      attribution for every licence that wants it

The manifest carries each cue's `match` rule copied out of `cues.py`, so
routing an event to a sound needs the manifest and nothing else.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import cues as C
from . import incompetech
from .sources import LICENSE_NAMES, PERMISSIVE

__all__ = ["validate_config", "plan", "fetch_all", "write_manifest", "write_credits",
           "credit_line", "verify", "MAX_BYTES", "EXT_BY_TYPE"]

MAX_BYTES = 40 * 1024 * 1024

EXT_BY_TYPE = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/aac": ".m4a",
    "audio/webm": ".webm",
}

KNOBS = {
    "gain_db": 0.0,
    "loop": False,
    "fade_in_ms": 0,
    "fade_out_ms": 0,
    "trim_start_s": 0.0,
    "trim_end_s": None,
}


def validate_config(doc: dict, *, allow: tuple[str, ...] = PERMISSIVE) -> list[str]:
    """Every problem with a config, as sentences. Empty means it is usable."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["config is not a JSON object"]
    assignments = doc.get("assignments")
    if not isinstance(assignments, dict) or not assignments:
        return ["config has no `assignments`"]
    for cue_id, a in assignments.items():
        where = f"{cue_id}"
        if cue_id not in C.CUES_BY_ID:
            problems.append(f"{where}: not a cue in cues.py")
            continue
        if not isinstance(a, dict):
            problems.append(f"{where}: assignment is not an object")
            continue
        if not (a.get("download_url") or a.get("preview_url")):
            problems.append(f"{where}: no download_url or preview_url")
        lic = a.get("license") or "unknown"
        if lic not in allow:
            problems.append(
                f"{where}: licence {lic!r} ({LICENSE_NAMES.get(lic, 'unrecognised')}) "
                f"is outside the allowed set {', '.join(allow)}"
            )
        for knob, default in KNOBS.items():
            v = a.get(knob, default)
            if knob == "loop":
                if not isinstance(v, bool):
                    problems.append(f"{where}: loop must be true or false")
            elif v is not None and not isinstance(v, (int, float)):
                problems.append(f"{where}: {knob} must be a number")
    missing = [c.id for c in C.required_cues() if c.id not in assignments]
    if missing:
        problems.append("unassigned required cues: " + ", ".join(missing))
    return problems


def _ext_for(url: str, content_type: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in EXT_BY_TYPE:
        return EXT_BY_TYPE[ct]
    tail = url.split("?")[0].rsplit(".", 1)
    if len(tail) == 2 and 2 <= len(tail[1]) <= 5:
        return "." + tail[1].lower()
    return ".mp3"


def plan(doc: dict) -> list[tuple[str, dict]]:
    """Assignments in cue-table order, so output is stable across runs."""
    assignments = doc.get("assignments") or {}
    return [(c.id, assignments[c.id]) for c in C.CUES if c.id in assignments]


def _download(client: httpx.Client, url: str, dest_stem: Path, *,
              max_bytes: int = MAX_BYTES) -> tuple[Path, str, int]:
    with client.stream("GET", url) as r:
        r.raise_for_status()
        ext = _ext_for(url, r.headers.get("content-type"))
        dest = dest_stem.with_suffix(ext)
        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in r.iter_bytes(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(f"over {max_bytes} bytes")
                digest.update(chunk)
                fh.write(chunk)
        tmp.replace(dest)
    return dest, digest.hexdigest(), size


def fetch_all(doc: dict, out: Path, *, client: httpx.Client, force: bool = False,
              log=print) -> dict:
    """Download every assignment; return the manifest (not yet written)."""
    manifest_path = out / "manifest.json"
    previous = {}
    if manifest_path.exists() and not force:
        try:
            previous = (json.loads(manifest_path.read_text(encoding="utf-8")) or {}).get("cues") or {}
        except json.JSONDecodeError:
            previous = {}

    cues_out: dict[str, dict] = {}
    for cue_id, a in plan(doc):
        cue = C.CUES_BY_ID[cue_id]
        url = a.get("download_url") or a.get("preview_url")
        prev = previous.get(cue_id)
        if prev and prev.get("source_url") == url and (out / prev["file"]).exists():
            log(f"  {cue_id:<28} kept {prev['file']}")
            entry = dict(prev)
        else:
            stem = out / "assets" / cue.group / cue_id
            for old in stem.parent.glob(f"{cue_id}.*"):
                old.unlink()
            try:
                dest, sha, size = _download(client, url, stem)
            except (httpx.HTTPError, RuntimeError) as exc:
                log(f"  {cue_id:<28} FAILED {exc}")
                continue
            entry = {
                "file": str(dest.relative_to(out)),
                "bytes": size,
                "sha256": sha,
                "source_url": url,
            }
            log(f"  {cue_id:<28} {entry['file']} ({size // 1024} kB)")
        entry.update({
            "group": cue.group,
            "when": cue.when,
            "match": cue.match,
            "duration": a.get("duration"),
            **{k: a.get(k, v) for k, v in KNOBS.items()},
            "credit": {
                "source": a.get("source"),
                "source_id": a.get("source_id"),
                "title": a.get("title"),
                "author": a.get("author"),
                "license": a.get("license"),
                "license_url": a.get("license_url"),
                "page_url": a.get("page_url"),
            },
        })
        # The finished sentence, beside the parts it is made of. A player has
        # to show this — most of the pack is CC BY and attribution is a
        # condition of playing it at all — and the wording each source requires
        # is decided here, so it is written here rather than re-derived by
        # whatever ends up doing the showing. It is also what keeps the audio
        # tool off the runtime path: the page reads a manifest, not this
        # module.
        entry["credit_text"] = credit_line(entry["credit"])
        cues_out[cue_id] = entry

    return {
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cues": cues_out,
    }


def write_manifest(manifest: dict, out: Path) -> Path:
    path = out / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return path


def credit_line(credit: dict) -> str:
    """The sentence to paste into the game, not a description of one.

    Attribution is only done when a listener can read it, so the file has to
    carry text that goes straight onto the credits panel. Where a source
    publishes its own required wording, use that wording.
    """
    title = credit.get("title") or "untitled"
    author = credit.get("author") or "unknown"
    lic = credit.get("license") or "unknown"
    lic_url = credit.get("license_url") or {
        "by": "https://creativecommons.org/licenses/by/4.0/",
        "by-sa": "https://creativecommons.org/licenses/by-sa/4.0/",
    }.get(lic, "")
    if credit.get("source") == "incompetech":
        # incompetech's own house form, worded once in `incompetech.py`.
        return incompetech.credit_line(title, lic_url)
    name = {"by": "CC BY", "by-sa": "CC BY-SA"}.get(lic, lic.upper())
    where = f" via {credit['source']}" if credit.get("source") else ""
    return f'"{title}" by {author}{where} — {name}' + (f" — {lic_url}" if lic_url else "")


def write_credits(manifest: dict, out: Path) -> Path:
    """Attribution, grouped by licence. CC0 is listed too — it costs a line
    and it is the only record of where a file came from."""
    by_license: dict[str, list[dict]] = {}
    for cue_id, entry in manifest.get("cues", {}).items():
        cr = dict(entry.get("credit") or {})
        cr["cue"] = cue_id
        by_license.setdefault(cr.get("license") or "unknown", []).append(cr)

    lines = [
        "# Audio credits",
        "",
        "Generated by `python -m tools.audio fetch` — do not edit by hand.",
        "",
    ]
    needs_credit = sorted(c for c in by_license if c not in ("cc0", "pd"))
    if needs_credit:
        lines += [
            "Everything under a CC BY or CC BY-SA heading **must** be credited "
            "wherever the audio is played. Keep this file next to the assets.",
            "",
            "## Paste this into the credits panel",
            "",
            "```",
        ]
        seen: set[str] = set()
        for lic in needs_credit:
            for cr in sorted(by_license[lic], key=lambda c: c["cue"]):
                line = credit_line(cr)
                if line not in seen:      # one line per track, not per cue
                    seen.add(line)
                    lines.append(line)
        lines += ["```", ""]
    for lic in sorted(by_license):
        lines.append(f"## {LICENSE_NAMES.get(lic, lic)}")
        lines.append("")
        for cr in sorted(by_license[lic], key=lambda c: c["cue"]):
            title = cr.get("title") or "untitled"
            author = cr.get("author") or "unknown"
            page = cr.get("page_url") or ""
            src = cr.get("source") or "?"
            link = f" — <{page}>" if page else ""
            lines.append(f"- `{cr['cue']}` — *{title}* by {author} ({src}){link}")
        lines.append("")
    path = out / "CREDITS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def verify(out: Path) -> list[str]:
    """Problems with a fetched directory: missing files, changed bytes."""
    path = out / "manifest.json"
    if not path.exists():
        return [f"no manifest at {path}"]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    for cue_id, entry in (manifest.get("cues") or {}).items():
        f = out / entry.get("file", "")
        if not f.exists():
            problems.append(f"{cue_id}: {entry.get('file')} is missing")
            continue
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        if entry.get("sha256") and digest != entry["sha256"]:
            problems.append(f"{cue_id}: {entry['file']} does not match its recorded sha256")
    return problems
