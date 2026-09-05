"""The incompetech catalogue as a SQLite database you can actually filter.

The catalogue page can search one substring over title, instruments and
description and AND a set of feels. It cannot ask for a tempo, a duration, a
collection, a category or an upload date, which are exactly the questions
worth asking when you are looking for something to loop under a scene. So the
catalogue is normalised into SQLite once and queried locally after that.

    python -m tools.audio catalog build
    python -m tools.audio catalog query --feel Dark --feel Mysterious \
        --bpm-max 90 --min-length 3:00

The shape is ordinary: one row per piece keyed by `filename` (the only field
in the catalogue that is unique — see `incompetech.py`), `genre` and
`collection` as lookup tables, and `instrument` and `feel` as many-to-many
relations, because "pieces with a choir and no drums" is a join and not a
substring search. `piece_fts` is an FTS5 index over title and description
where the interpreter has FTS5, and a LIKE scan where it does not.

The database is a **build artefact**: one command and one HTTP request rebuild
it, so it is gitignored rather than committed. That is deliberately unlike
`audio/manifest.json`, which is on the runtime path and must survive a
deploy's hard reset; nothing here is. `tools/` is a dev tier and the runtime
imports none of it.

Everything in the catalogue is CC BY 4.0 to Kevin MacLeod. `meta` carries the
licence, the attribution and the credit sentence, and `--json` output puts a
finished `credit` on every row, so a row that leaves this database takes its
obligation with it.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field, replace

from . import incompetech as I

__all__ = [
    "SCHEMA",
    "Stats",
    "connect",
    "build",
    "have_fts5",
    "Filters",
    "search",
    "meta",
    "format_rows",
]

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE genre (
    genre_id INTEGER PRIMARY KEY,           -- the catalogue's own genre id
    name     TEXT NOT NULL UNIQUE
);

CREATE TABLE collection (
    code      INTEGER PRIMARY KEY,          -- what a piece carries, NOT `id`
    name      TEXT NOT NULL,
    category  TEXT NOT NULL,
    source_id INTEGER                       -- the page's other number
);

CREATE TABLE piece (
    piece_id            INTEGER PRIMARY KEY,
    -- The natural key. `uuid` repeats and comes in four shapes and `isrc`
    -- repeats across pieces that are not the same music, so neither is
    -- unique; both are kept as ordinary indexed columns.
    filename            TEXT    NOT NULL UNIQUE,
    title               TEXT    NOT NULL,
    uuid                TEXT    NOT NULL DEFAULT '',
    isrc                TEXT    NOT NULL DEFAULT '',
    length_s            INTEGER,            -- NULL where the catalogue said 00:00:00
    bpm                 INTEGER,            -- NULL where it said 0 or nothing
    description         TEXT    NOT NULL DEFAULT '',
    uploaded            TEXT,               -- ISO date, NULL if unparseable
    genre_id            INTEGER REFERENCES genre(genre_id),
    collection_code     INTEGER REFERENCES collection(code),
    genre_raw           TEXT    NOT NULL DEFAULT '',
    collection_raw      TEXT    NOT NULL DEFAULT '',
    mp3_url             TEXT    NOT NULL,
    page_url            TEXT,
    video_url           TEXT,
    sheetmusic_url      TEXT,
    itunes_url          TEXT,
    filmmusic_url       TEXT,
    -- The catalogue's `wav` field, verbatim. It is not a WAV and not a
    -- working audio URL: see incompetech.py. Never offer it as a download.
    wav_link            TEXT
);

CREATE INDEX piece_bpm       ON piece(bpm);
CREATE INDEX piece_length    ON piece(length_s);
CREATE INDEX piece_uploaded  ON piece(uploaded);
CREATE INDEX piece_genre     ON piece(genre_id);
CREATE INDEX piece_coll      ON piece(collection_code);
CREATE INDEX piece_isrc      ON piece(isrc);
CREATE INDEX piece_uuid      ON piece(uuid);

CREATE TABLE instrument (
    instrument_id INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,            -- the commonest spelling
    key           TEXT NOT NULL UNIQUE      -- case-folded, what a filter matches
);

CREATE TABLE piece_instrument (
    piece_id      INTEGER NOT NULL REFERENCES piece(piece_id),
    instrument_id INTEGER NOT NULL REFERENCES instrument(instrument_id),
    ord           INTEGER NOT NULL DEFAULT 0,   -- the catalogue's own order
    PRIMARY KEY (piece_id, instrument_id)
);
CREATE INDEX piece_instrument_rev ON piece_instrument(instrument_id);

CREATE TABLE feel (
    feel_id   INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    key       TEXT NOT NULL UNIQUE,
    -- 1 for the page's own 20-word vocabulary, 0 for a word one piece
    -- invented ("Ren Faire", "Medieval"). Kept, not dropped, but tellable.
    canonical INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE piece_feel (
    piece_id INTEGER NOT NULL REFERENCES piece(piece_id),
    feel_id  INTEGER NOT NULL REFERENCES feel(feel_id),
    ord      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (piece_id, feel_id)
);
CREATE INDEX piece_feel_rev ON piece_feel(feel_id);

CREATE VIEW piece_full AS
SELECT p.*, g.name AS genre, c.name AS collection, c.category AS collection_category
FROM piece p
LEFT JOIN genre g      ON g.genre_id = p.genre_id
LEFT JOIN collection c ON c.code = p.collection_code;
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE piece_fts USING fts5(
    title, description, content='piece', content_rowid='piece_id'
);
"""


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def have_fts5(conn: sqlite3.Connection) -> bool:
    """Whether this interpreter's SQLite was built with FTS5.

    Debian's is; a hand-built or minimal one may not be, and a dev tool that
    refuses to run on that machine would be worse than one that falls back to
    a LIKE scan over 1442 rows, which is instant anyway.
    """
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
    except sqlite3.Error:
        return False
    conn.execute("DROP TABLE temp.fts5_probe")
    return True


@dataclass
class Stats:
    """What a build ingested — printed by the CLI, asserted by the tests."""

    pieces: int = 0
    skipped: int = 0
    with_genre: int = 0
    with_collection: int = 0
    unresolved_genre: int = 0
    unresolved_collection: int = 0
    unresolved_either: int = 0
    instruments: int = 0
    feels: int = 0
    non_canonical_feels: tuple[str, ...] = ()
    with_bpm: int = 0
    with_length: int = 0
    fts: bool = False
    drift: tuple[str, ...] = field(default_factory=tuple)

    def lines(self) -> list[str]:
        out = [
            f"{self.pieces} pieces ingested"
            + (f" ({self.skipped} rows skipped)" if self.skipped else ""),
            f"  genre resolved      {self.with_genre}/{self.pieces}"
            f"  (unresolved {self.unresolved_genre})",
            f"  collection resolved {self.with_collection}/{self.pieces}"
            f"  (unresolved {self.unresolved_collection})",
            f"  neither resolved    {self.unresolved_either}",
            f"  bpm known           {self.with_bpm}/{self.pieces}",
            f"  length known        {self.with_length}/{self.pieces}",
            f"  {self.instruments} distinct instruments, {self.feels} distinct feels"
            + (f" ({len(self.non_canonical_feels)} off-vocabulary: "
               f"{', '.join(self.non_canonical_feels)})"
               if self.non_canonical_feels else ""),
            f"  full-text index     {'FTS5' if self.fts else 'none — LIKE fallback'}",
        ]
        out += [f"  lookup drift: {d}" for d in self.drift]
        return out


def build(conn: sqlite3.Connection, rows, *, fetched_at: str | None = None,
          drift: tuple[str, ...] = ()) -> Stats:
    """Normalise raw catalogue rows into an empty connection. Returns `Stats`.

    The database is rebuilt whole rather than updated: it is derived from one
    file, and a rebuild is one request. That is also why nothing here worries
    about migrations.
    """
    conn.executescript(SCHEMA)
    fts = have_fts5(conn)
    if fts:
        conn.executescript(FTS_SCHEMA)

    pieces = I.normalize_catalog(rows)
    stats = Stats(pieces=len(pieces), skipped=max(0, len(rows or ()) - len(pieces)), fts=fts)

    conn.executemany("INSERT INTO genre (genre_id, name) VALUES (?, ?)",
                     sorted(I.GENRES.items()))
    conn.executemany(
        "INSERT INTO collection (code, name, category, source_id) VALUES (?, ?, ?, ?)",
        [(c.code, c.name, c.category, c.source_id) for c in I.COLLECTIONS])

    # The commonest spelling wins the display name: the catalogue holds both
    # "Piano" and "PIano", and 359 raw instrument strings are 313 instruments.
    inst_names = _canonical_names(p.instruments for p in pieces)
    feel_names = _canonical_names(p.feels for p in pieces)
    canonical = {f.casefold() for f in I.FEELS}

    conn.executemany("INSERT INTO instrument (instrument_id, name, key) VALUES (?, ?, ?)",
                     [(i, name, key) for i, (key, name) in enumerate(inst_names.items(), 1)])
    conn.executemany("INSERT INTO feel (feel_id, name, key, canonical) VALUES (?, ?, ?, ?)",
                     [(i, name, key, int(key in canonical))
                      for i, (key, name) in enumerate(feel_names.items(), 1)])
    inst_id = {key: i for i, key in enumerate(inst_names, 1)}
    feel_id = {key: i for i, key in enumerate(feel_names, 1)}

    for pid, p in enumerate(pieces, 1):
        conn.execute(
            """INSERT INTO piece (
                   piece_id, filename, title, uuid, isrc, length_s, bpm,
                   description, uploaded, genre_id, collection_code,
                   genre_raw, collection_raw, mp3_url, page_url, video_url,
                   sheetmusic_url, itunes_url, filmmusic_url, wav_link)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, p.filename, p.title, p.uuid, p.isrc, p.length_s, p.bpm,
             p.description, p.uploaded, p.genre_id, p.collection_code,
             p.genre_raw, p.collection_raw, p.mp3_url, p.page_url, p.video_url,
             p.sheetmusic_url, p.itunes_url, p.filmmusic_url, p.wav_link))
        conn.executemany("INSERT INTO piece_instrument VALUES (?, ?, ?)",
                         [(pid, inst_id[n.casefold()], i)
                          for i, n in enumerate(p.instruments)])
        conn.executemany("INSERT INTO piece_feel VALUES (?, ?, ?)",
                         [(pid, feel_id[n.casefold()], i)
                          for i, n in enumerate(p.feels)])

        stats.with_genre += p.genre is not None
        stats.with_collection += p.collection is not None
        stats.unresolved_genre += p.genre is None
        stats.unresolved_collection += p.collection is None
        stats.unresolved_either += p.genre is None and p.collection is None
        stats.with_bpm += p.bpm is not None
        stats.with_length += p.length_s is not None

    if fts:
        conn.execute("INSERT INTO piece_fts (rowid, title, description) "
                     "SELECT piece_id, title, description FROM piece")

    stats.instruments = len(inst_names)
    stats.feels = len(feel_names)
    stats.non_canonical_feels = tuple(
        name for key, name in feel_names.items() if key not in canonical)
    stats.drift = tuple(drift)

    conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", [
        ("source", I.CATALOG_URL),
        ("lookups", I.LOOKUPS_URL),
        ("fetched_at", fetched_at or _dt.datetime.now(_dt.timezone.utc)
                       .replace(microsecond=0).isoformat()),
        ("pieces", str(stats.pieces)),
        ("author", I.AUTHOR),
        ("license", I.LICENSE),
        ("license_url", I.LICENSE_URL),
        ("license_name", "Creative Commons Attribution 4.0"),
        # Attribution is a condition of using any of this, so it is stored
        # rather than left for whoever reads the database to remember.
        ("attribution", f"Music by {I.AUTHOR} (incompetech.com), licensed under "
                        f"Creative Commons: By Attribution 4.0 — {I.LICENSE_URL}"),
        ("fts", "5" if fts else ""),
    ])
    conn.commit()
    return stats


def _canonical_names(lists) -> dict[str, str]:
    """Case-folded key → the spelling that appears most often, ties by first.

    Sorted by key so a rebuild numbers the rows the same way twice.
    """
    counts: dict[str, Counter] = {}
    for names in lists:
        for name in names:
            counts.setdefault(name.casefold(), Counter())[name] += 1
    return {key: max(c.items(), key=lambda kv: (kv[1], kv[0]))[0]
            for key, c in sorted(counts.items())}


def meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.Error:
        return {}


# ------------------------------------------------------------------ query

SORTS = {
    "title": "p.title COLLATE NOCASE",
    "length": "p.length_s",
    "bpm": "p.bpm",
    "uploaded": "p.uploaded",
    "genre": "genre COLLATE NOCASE, p.title COLLATE NOCASE",
    "collection": "collection COLLATE NOCASE, p.title COLLATE NOCASE",
}


@dataclass
class Filters:
    """Everything the CLI can ask for. Empty fields ask for nothing.

    `feels` and `instruments` AND: three feels means a piece carrying all
    three, which is the filter the whole exercise is for. `feels_any`,
    `genres`, `collections` and `categories` OR within themselves and AND with
    everything else.
    """

    text: str = ""
    feels: tuple[str, ...] = ()
    feels_any: tuple[str, ...] = ()
    instruments: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    bpm_min: int | None = None
    bpm_max: int | None = None
    bpm_unknown: bool = False       # rows the catalogue never measured
    length_min: int | None = None
    length_max: int | None = None
    uploaded_from: str = ""
    uploaded_to: str = ""
    limit: int = 25
    sort: str = "title"
    desc: bool = False


def _fts_query(text: str) -> str:
    """A plain phrase as an FTS5 MATCH that cannot be a syntax error.

    Each word is quoted, so "-", ":" and "NOT" are searched for rather than
    obeyed; a trailing "*" is kept outside the quotes so prefix search still
    works. Words AND, which is what someone typing two words means. Text with
    no word in it at all ("*") yields "", and the caller drops the filter —
    FTS5 raises on an empty MATCH.
    """
    parts = []
    for word in text.split():
        star = word.endswith("*")
        body = word[:-1] if star else word
        body = body.replace('"', '""')
        if body:
            parts.append(f'"{body}"' + ("*" if star else ""))
    return " ".join(parts)


def search(conn: sqlite3.Connection, f: Filters) -> list[dict]:
    """Rows matching `f`, newest joins resolved, as plain dicts."""
    where: list[str] = []
    args: list = []

    if f.text:
        fts = bool(meta(conn).get("fts"))
        match = _fts_query(f.text) if fts else ""
        if match:
            where.append("p.piece_id IN (SELECT rowid FROM piece_fts "
                         "WHERE piece_fts MATCH ?)")
            args.append(match)
        elif not fts:
            # No FTS5 here: a LIKE scan over 1442 rows costs nothing, and the
            # semantics match the catalogue page's own single-substring search.
            where.append("(p.title LIKE ? OR p.description LIKE ?)")
            args += [f"%{f.text}%"] * 2
        # Otherwise FTS5 is present but the text held no word to search for
        # ("*"), and an empty MATCH is a syntax error: ask for nothing.

    # One EXISTS per feel: N clauses ANDed is the AND, unambiguously.
    for name in f.feels:
        where.append("EXISTS (SELECT 1 FROM piece_feel pf JOIN feel fe "
                     "ON fe.feel_id = pf.feel_id "
                     "WHERE pf.piece_id = p.piece_id AND fe.key = ?)")
        args.append(name.casefold())
    if f.feels_any:
        where.append("EXISTS (SELECT 1 FROM piece_feel pf JOIN feel fe "
                     "ON fe.feel_id = pf.feel_id WHERE pf.piece_id = p.piece_id "
                     f"AND fe.key IN ({_marks(f.feels_any)}))")
        args += [n.casefold() for n in f.feels_any]

    # Instruments match as substrings, so "drum" finds "Drums" and "Log
    # Drums" — 313 free-text names are not a vocabulary anyone can recite.
    for name in f.instruments:
        where.append("EXISTS (SELECT 1 FROM piece_instrument pi JOIN instrument ins "
                     "ON ins.instrument_id = pi.instrument_id "
                     "WHERE pi.piece_id = p.piece_id AND ins.key LIKE ?)")
        args.append(f"%{name.casefold()}%")

    if f.genres:
        where.append(f"(g.name COLLATE NOCASE IN ({_marks(f.genres)}) "
                     f"OR p.genre_id IN ({_marks(f.genres)}))")
        args += list(f.genres)
        args += [_int_or_none(x) for x in f.genres]
    if f.collections:
        where.append("(" + " OR ".join(["c.name LIKE ?"] * len(f.collections)) + ")")
        args += [f"%{n}%" for n in f.collections]
    if f.categories:
        where.append("(" + " OR ".join(["c.category LIKE ?"] * len(f.categories)) + ")")
        args += [f"%{n}%" for n in f.categories]

    # A NULL bpm or length is "the catalogue does not know", so a range must
    # not return it — SQL does that for us, and `--bpm-unknown` is how you ask
    # for those rows on purpose.
    if f.bpm_unknown:
        where.append("p.bpm IS NULL")
    else:
        if f.bpm_min is not None:
            where.append("p.bpm >= ?")
            args.append(f.bpm_min)
        if f.bpm_max is not None:
            where.append("p.bpm <= ?")
            args.append(f.bpm_max)
    if f.length_min is not None:
        where.append("p.length_s >= ?")
        args.append(f.length_min)
    if f.length_max is not None:
        where.append("p.length_s <= ?")
        args.append(f.length_max)
    if f.uploaded_from:
        where.append("p.uploaded >= ?")
        args.append(f.uploaded_from)
    if f.uploaded_to:
        where.append("p.uploaded <= ?")
        args.append(f.uploaded_to)

    order = SORTS.get(f.sort, SORTS["title"])
    if f.desc:
        order = ", ".join(f"{part.strip()} DESC" for part in order.split(","))
    # NULL tempos and lengths sort last whichever way the sort runs: an
    # unknown is not the slowest piece in the catalogue.
    if f.sort in ("bpm", "length", "uploaded"):
        order = f"({SORTS[f.sort]} IS NULL), {order}"

    sql = f"""
        SELECT p.*, g.name AS genre, c.name AS collection,
               c.category AS collection_category,
               -- group_concat has no ordering of its own, so the rows are
               -- ordered first and aggregated outside: what comes back is
               -- the catalogue's own listing order, lead instrument first.
               (SELECT group_concat(name, ', ') FROM
                   (SELECT fe.name AS name FROM piece_feel pf
                      JOIN feel fe ON fe.feel_id = pf.feel_id
                     WHERE pf.piece_id = p.piece_id ORDER BY pf.ord)) AS feels,
               (SELECT group_concat(name, ', ') FROM
                   (SELECT ins.name AS name FROM piece_instrument pi
                      JOIN instrument ins ON ins.instrument_id = pi.instrument_id
                     WHERE pi.piece_id = p.piece_id ORDER BY pi.ord)) AS instruments
        FROM piece p
        LEFT JOIN genre g      ON g.genre_id = p.genre_id
        LEFT JOIN collection c ON c.code = p.collection_code
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY {order}
        {"LIMIT ?" if f.limit and f.limit > 0 else ""}
    """
    if f.limit and f.limit > 0:
        args.append(f.limit)

    rows = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["feels"] = [x for x in (d.get("feels") or "").split(", ") if x]
        d["instruments"] = [x for x in (d.get("instruments") or "").split(", ") if x]
        d["credit"] = I.credit_line(d["title"])
        rows.append(d)
    return rows


def count(conn: sqlite3.Connection, f: Filters) -> int:
    """How many pieces match, ignoring `limit`."""
    return len(search(conn, replace(f, limit=0)))


def _marks(seq) -> str:
    return ", ".join("?" for _ in seq)


def _int_or_none(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------- output


def hms(secs) -> str:
    if secs is None:
        return "  ?  "
    secs = int(secs)
    h, rest = divmod(secs, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_rows(rows: list[dict], *, wide: bool = False) -> str:
    """A readable table. `--json` is the scriptable form; this one is for eyes."""
    if not rows:
        return "no pieces match"
    width = 44 if wide else 34
    out = [f"{'TITLE':<{width}} {'LEN':>7} {'BPM':>4}  {'GENRE':<17} "
           f"{'COLLECTION':<20} FEELS"]
    for r in rows:
        title = r["title"]
        if len(title) > width:
            title = title[:width - 1] + "…"
        out.append(
            f"{title:<{width}} {hms(r['length_s']):>7} "
            f"{(r['bpm'] if r['bpm'] is not None else '—'):>4}  "
            f"{(r['genre'] or '—'):<17.17} {(r['collection'] or '—'):<20.20} "
            f"{', '.join(r['feels'])}")
    return "\n".join(out)


def dump_json(rows: list[dict], meta_: dict) -> str:
    return json.dumps({"meta": meta_, "count": len(rows), "pieces": rows},
                      indent=1, ensure_ascii=False)
