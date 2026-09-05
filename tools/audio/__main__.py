"""`python -m tools.audio <command>` — source, pick, fetch.

    harvest   search the libraries for every cue and write candidates.json,
              then bake the picker page
    picker    rebuild the page from an existing candidates.json
    fetch     download a picked config into assets/ + manifest.json + CREDITS.md,
              and normalise what it downloaded where ffmpeg is installed
    normalize re-run that levelling over an already-fetched directory, cutting
              anything longer than its cue's window down to it
    verify    re-hash a fetched directory against its manifest
    cues      print the cue table (--json for the machine-readable form)
    catalog   build and query a local database of incompetech's catalogue —
              `catalog build`, then `catalog query` with the filters the
              catalogue's own page does not offer (tempo, duration, date)

Everything writes under `audio/` unless told otherwise. What lands there is
tracked apart from the build artefacts — `candidates.json`, `picker.html` and
the catalogue database — see AUDIO.md, which also carries the licence rules and
the levelling profiles.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import cues as C

DEFAULT_OUT = Path("audio")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help="working directory (default: audio/)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.audio", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("harvest", help="search the libraries for candidates")
    _add_common(h)
    h.add_argument("--group", action="append", default=[], choices=list(C.GROUPS),
                   help="limit to a cue group (repeatable)")
    h.add_argument("--cues", default="", help="comma-separated cue ids")
    h.add_argument("--required", action="store_true", help="only the required cues")
    h.add_argument("--per-query", type=int, default=8, help="results per search term (default 8)")
    h.add_argument("--source", action="append", default=[],
                   help="limit to a source: freesound, jamendo, incompetech, archive (repeatable)")
    h.add_argument("--no-picker", action="store_true", help="do not rebuild picker.html")

    p = sub.add_parser("picker", help="rebuild picker.html from candidates.json")
    _add_common(p)

    f = sub.add_parser("fetch", help="download a picked configuration")
    _add_common(f)
    f.add_argument("--config", type=Path, default=None,
                   help="config JSON (default: <out>/config.json, or - for stdin)")
    f.add_argument("--allow", default="", help="extra licence codes to accept, comma-separated")
    f.add_argument("--force", action="store_true", help="re-download files already present")
    f.add_argument("--dry-run", action="store_true", help="validate and list, download nothing")
    f.add_argument("--no-normalize", action="store_true",
                   help="keep the files exactly as downloaded (default is to level them)")

    n = sub.add_parser("normalize", help="level and re-encode what a manifest names")
    _add_common(n)
    n.add_argument("--force", action="store_true",
                   help="redo files already carrying the current profile")
    n.add_argument("--no-trim", action="store_true",
                   help="keep a file longer than its cue's window, and warn instead")

    v = sub.add_parser("verify", help="check a fetched directory against its manifest")
    _add_common(v)

    c = sub.add_parser("cues", help="print the cue table")
    c.add_argument("--json", action="store_true")

    _add_catalog(sub)

    args = ap.parse_args(argv)

    if args.cmd == "cues":
        return _cmd_cues(args)
    if args.cmd == "harvest":
        return _cmd_harvest(args)
    if args.cmd == "picker":
        return _cmd_picker(args)
    if args.cmd == "fetch":
        return _cmd_fetch(args)
    if args.cmd == "normalize":
        return _cmd_normalize(args)
    if args.cmd == "verify":
        return _cmd_verify(args)
    if args.cmd == "catalog":
        return _cmd_catalog(args)
    return 2


DEFAULT_DB = DEFAULT_OUT / "incompetech.sqlite3"


def _duration(text: str) -> int:
    """A duration as seconds, written "210", "3:30" or "1:02:03".

    A bad one is a `ValueError` with the accepted forms in it, because the
    caller turns that into a message; unhandled it is a traceback over a typo.
    """
    total = 0
    for part in str(text).split(":"):
        try:
            total = total * 60 + int(part or 0)
        except ValueError:
            raise ValueError(f"{text!r} is not a duration; write it as 210, "
                             "3:30 or 1:02:03") from None
    return total


def _add_catalog(sub) -> None:
    """`catalog build` and `catalog query` — see tools/audio/catalog.py."""
    cat = sub.add_parser("catalog", help="build and query the incompetech catalogue database")
    cs = cat.add_subparsers(dest="sub", required=True)

    b = cs.add_parser("build", help="fetch the catalogue and write the database")
    b.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help=f"database path (default: {DEFAULT_DB})")
    b.add_argument("--from-file", type=Path, default=None,
                   help="read pieces.json from disk instead of fetching it")
    b.add_argument("--no-check-lookups", action="store_true",
                   help="skip the second request that checks the genre and "
                        "collection tables against the catalogue page")

    q = cs.add_parser("query", help="filter the catalogue")
    q.add_argument("--db", type=Path, default=DEFAULT_DB)
    q.add_argument("--text", "-t", default="", help="full-text over title + description")
    q.add_argument("--feel", action="append", default=[],
                   help="require this feel (repeatable — they AND)")
    q.add_argument("--feel-any", action="append", default=[],
                   help="require any one of these feels (repeatable — they OR)")
    q.add_argument("--instrument", action="append", default=[],
                   help="require this instrument, matched as a substring (repeatable — they AND)")
    q.add_argument("--genre", action="append", default=[], help="genre name or id (OR)")
    q.add_argument("--collection", action="append", default=[], help="collection name (OR)")
    q.add_argument("--category", action="append", default=[],
                   help="collection category, e.g. 'Film Scoring Moods' (OR)")
    q.add_argument("--bpm-min", type=int, default=None)
    q.add_argument("--bpm-max", type=int, default=None)
    q.add_argument("--bpm-unknown", action="store_true",
                   help="only the 246 pieces whose tempo the catalogue never measured")
    q.add_argument("--min-length", default=None, help="e.g. 180 or 3:00")
    q.add_argument("--max-length", default=None)
    q.add_argument("--since", default="", help="uploaded on or after (YYYY-MM-DD)")
    q.add_argument("--until", default="", help="uploaded on or before (YYYY-MM-DD)")
    q.add_argument("--limit", type=int, default=25, help="0 for no limit")
    q.add_argument("--sort", default="title", choices=sorted(_sorts()),
                   help="default: title")
    q.add_argument("--desc", action="store_true")
    q.add_argument("--json", action="store_true", help="machine-readable, with URLs and credit")


def _sorts():
    from .catalog import SORTS
    return SORTS


def _cmd_catalog(args) -> int:
    if args.sub == "build":
        return _cmd_catalog_build(args)
    return _cmd_catalog_query(args)


def _cmd_catalog_build(args) -> int:
    from . import catalog as CAT
    from . import incompetech as I

    drift: tuple[str, ...] = ()
    if args.from_file:
        rows = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    else:
        import httpx
        with httpx.Client(timeout=60.0, follow_redirects=True,
                          headers={"User-Agent": "dnd-sim-audio-sourcing/1"}) as client:
            rows = I.fetch_catalog(client)
            if not args.no_check_lookups:
                try:
                    drift = tuple(I.lookup_drift(I.fetch_lookups(client)))
                except Exception as exc:            # noqa: BLE001 — advisory only
                    drift = (f"could not check the lookup tables: {exc}",)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.db.exists():
        args.db.unlink()        # the database is derived; a build rebuilds it
    conn = CAT.connect(args.db)
    try:
        stats = CAT.build(conn, rows)
    finally:
        conn.close()

    print(f"wrote {args.db}")
    for line in stats.lines():
        print(line)
    if drift:
        print("lookup tables have drifted from the catalogue page:", file=sys.stderr)
        for note in drift:
            print(f"  {note}", file=sys.stderr)
        print("  update tools/audio/incompetech.py by hand", file=sys.stderr)
    return 0


def _cmd_catalog_query(args) -> int:
    from . import catalog as CAT

    if not args.db.exists():
        print(f"no database at {args.db}; run `python -m tools.audio catalog build` first",
              file=sys.stderr)
        return 2
    try:
        f = CAT.Filters(
            text=args.text,
            feels=tuple(args.feel),
            feels_any=tuple(args.feel_any),
            instruments=tuple(args.instrument),
            genres=tuple(args.genre),
            collections=tuple(args.collection),
            categories=tuple(args.category),
            bpm_min=args.bpm_min,
            bpm_max=args.bpm_max,
            bpm_unknown=args.bpm_unknown,
            length_min=_duration(args.min_length) if args.min_length else None,
            length_max=_duration(args.max_length) if args.max_length else None,
            uploaded_from=args.since,
            uploaded_to=args.until,
            limit=args.limit,
            sort=args.sort,
            desc=args.desc,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    conn = CAT.connect(args.db)
    try:
        rows = CAT.search(conn, f)
        total = CAT.count(conn, f)
        info = CAT.meta(conn)
    except ValueError as exc:       # a filter that cannot mean anything
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if args.json:
        print(CAT.dump_json(rows, info))
        return 0
    print(CAT.format_rows(rows))
    shown = f"{len(rows)} of {total}" if total != len(rows) else str(total)
    print(f"\n{shown} pieces — {info.get('attribution', '')}")
    return 0


def _cmd_cues(args) -> int:
    if args.json:
        print(json.dumps([c.to_dict() for c in C.CUES], indent=1))
        return 0
    group = None
    for cue in C.CUES:
        if cue.group != group:
            group = cue.group
            print(f"\n{group.upper()}")
        flag = "*" if cue.required else " "
        print(f" {flag} {cue.id:<26} {cue.label:<24} {cue.when}")
    print(f"\n{len(C.CUES)} cues, {len(C.required_cues())} required (*)")
    return 0


def _cmd_harvest(args) -> int:
    from . import harvest as H
    doc = H.run(
        out=args.out / "candidates.json",
        groups=tuple(args.group),
        ids=tuple(x.strip() for x in args.cues.split(",") if x.strip()),
        required_only=args.required,
        per_query=args.per_query,
        only_sources=tuple(args.source) or None,
    )
    if not args.no_picker:
        _cmd_picker(args)
    return 0 if doc else 1


def _cmd_picker(args) -> int:
    from . import build as B
    out = B.build(args.out / "candidates.json", args.out / "picker.html")
    print(f"wrote {out} — open it in a browser")
    return 0


def _cmd_fetch(args) -> int:
    import httpx

    from . import fetch as F
    from .sources import PERMISSIVE

    path = args.config or (args.out / "config.json")
    if str(path) == "-":
        doc = json.load(sys.stdin)
    elif not Path(path).exists():
        print(f"no config at {path}; save the picker's output there first", file=sys.stderr)
        return 2
    else:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))

    allow = PERMISSIVE + tuple(x.strip() for x in args.allow.split(",") if x.strip())
    problems = F.validate_config(doc, allow=allow)
    hard = [p for p in problems if not p.startswith("unassigned required cues")]
    for p in problems:
        print(("error: " if p in hard else "note:  ") + p, file=sys.stderr)
    if hard:
        return 1
    if args.dry_run:
        for cue_id, a in F.plan(doc):
            print(f"  {cue_id:<28} {a.get('license'):<6} {a.get('download_url') or a.get('preview_url')}")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60.0, follow_redirects=True,
                      headers={"User-Agent": "dnd-sim-audio-sourcing/1"}) as client:
        manifest = F.fetch_all(doc, args.out, client=client, force=args.force)
    F.write_manifest(manifest, args.out)

    if not args.no_normalize:
        from . import normalize as N
        if N.have_ffmpeg():
            manifest = N.normalize_manifest(args.out)
        else:
            print("ffmpeg not on PATH — files kept as downloaded, so levels will "
                  "not match between cues (see AUDIO.md)", file=sys.stderr)
    F.write_credits(manifest, args.out)

    got = len(manifest["cues"])
    want = len(F.plan(doc))
    print(f"{got}/{want} cues fetched into {args.out}; manifest.json and CREDITS.md written")
    return 0 if got == want else 1


def _cmd_normalize(args) -> int:
    from . import normalize as N
    if not N.have_ffmpeg():
        print("ffmpeg and ffprobe are not on PATH; nothing to do", file=sys.stderr)
        return 2
    N.normalize_manifest(args.out, force=args.force, trim=not args.no_trim)
    return 0


def _cmd_verify(args) -> int:
    from . import fetch as F
    problems = F.verify(args.out)
    for p in problems:
        print("error: " + p, file=sys.stderr)
    if not problems:
        print(f"{args.out}: every file present and unchanged")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
