"""incompetech's catalogue: the lookup tables, and the normaliser over them.

Kevin MacLeod publishes the whole catalogue as one JSON file — 1400-odd
pieces, one request, no key — which is why `sources.py` searches it in memory
rather than crawling anything. What that file does *not* carry is what its own
numbers mean: `genre` and `collection` are bare ids whose tables live only as
inline JavaScript in the catalogue page. They are transcribed below, and this
module is the one place in the repo that holds them.

The raw rows are thin and dirty, and every one of these is real:

  * `genre` is a numeric id; `collection` is a numeric **code**, which is *not*
    the collection's `id` — see `COLLECTIONS`.
  * `length` is "hh:mm:ss", and "00:00:00" on three rows means "unknown";
    `bpm` is a string, sometimes null and 238 times "0", which likewise means
    "not measured" rather than a tempo. Both become NULL — see `parse_bpm`.
  * `instruments` and `feel` are comma-separated free text, with the same
    instrument spelled "Piano" and "PIano".
  * `\r\n` runs appear inside title, instruments and description, 505 rows
    carry leading or trailing whitespace, and a few `video` and `itunes`
    values begin with a space *before* the URL.
  * `null` and `""` are both used for absent, inconsistently per field.
  * `feel` has a 20-word vocabulary that one piece ignores.
  * `uuid` repeats twice and comes in four shapes (a real UUID, an ISRC, a
    bare number, a truncated fragment); `isrc` repeats six times, twice on
    pieces that are not the same music. `filename` is the only unique field.
  * `wav` holds no WAV: 160 of its 268 values point at a Downloads page that
    404s and the rest at a filmmusic.io page that has moved. It is kept
    verbatim as `wav_link` and is not an audio URL.

`normalize_piece` deals with all of it and returns a `Piece`; `catalog.py`
stores those in SQLite. Nothing here reads the environment or writes a file,
and nothing on the runtime path imports it — `tools/` is a dev tier.

Everything in the catalogue is CC BY 4.0 to Kevin MacLeod. The credit is a
licence condition rather than a courtesy, so `credit_line` lives here and
`fetch.py` uses it when it writes CREDITS.md.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

__all__ = [
    "CATALOG_URL",
    "LOOKUPS_URL",
    "MP3_BASE",
    "DETAIL_BASE",
    "AUTHOR",
    "LICENSE",
    "LICENSE_URL",
    "GENRES",
    "Collection",
    "COLLECTIONS",
    "COLLECTIONS_BY_CODE",
    "FEELS",
    "Piece",
    "credit_line",
    "genre_name",
    "collection_for",
    "clean",
    "parse_length",
    "parse_bpm",
    "parse_date",
    "split_multi",
    "mp3_url",
    "sheetmusic_url",
    "detail_url",
    "normalize_piece",
    "normalize_catalog",
    "fetch_catalog",
    "fetch_lookups",
    "parse_lookups",
    "lookup_drift",
]

CATALOG_URL = "https://incompetech.com/music/royalty-free/pieces.json"
# The catalogue page: the lookup tables are inline JS in it and nowhere else.
LOOKUPS_URL = "https://incompetech.com/music/royalty-free/music.html"
MP3_BASE = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/"
# The per-piece page is one page that looks its subject up by ISRC client-side.
DETAIL_BASE = "https://incompetech.com/music/royalty-free/index.html?isrc="

AUTHOR = "Kevin MacLeod"
LICENSE = "by"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"


def credit_line(title: str, license_url: str = LICENSE_URL) -> str:
    """incompetech's own house wording, as its licence page generates it."""
    return (f'"{title}" Kevin MacLeod (incompetech.com) — Licensed under '
            f"Creative Commons: By Attribution 4.0 — {license_url}")


# ---------------------------------------------------------------- lookups
# Transcribed from the `genres`, `collections` and `feelsList` arrays in
# LOOKUPS_URL (2026-09-05). `build --check-lookups` re-fetches that page and
# reports anything that has moved, which is the only thing that can catch
# these going stale.

GENRES: dict[int, str] = {
    2: "African",
    3: "Blues",
    4: "Classical",
    5: "Contemporary",
    6: "Disco",
    7: "Electronica",
    8: "Funk",
    9: "Holiday",
    10: "Horror",
    11: "Jazz",
    12: "Latin",
    13: "Modern",
    14: "Musical",
    15: "Polka",
    16: "Pop",
    18: "Reggae",
    19: "Rock",
    20: "Silent Film Score",
    21: "Ska",
    22: "Soundtrack",
    23: "Stings",
    24: "Unclassifiable",
    25: "World",
    26: "Urban",
}


@dataclass(frozen=True)
class Collection:
    """One collection, keyed by the number a piece actually carries.

    A collection entry has both an `id` and a `code` and they differ for all
    but a handful of rows. The `collection` field on a piece is the **code**:
    the catalogue page's own `getCollectionName()` matches on `code`, and
    resolving by `id` instead silently mislabels 133 of the 1442 pieces and
    leaves four codes unmatched. `source_id` keeps the other number only so a
    re-transcription can be diffed against the page.
    """

    code: int
    name: str
    category: str
    source_id: int


COLLECTIONS: tuple[Collection, ...] = (
    Collection(1, "African", "World", 9),
    Collection(2, "Bright Piano", "Lighter Faire", 33),
    Collection(3, "Celtic and Folk", "World", 13),
    Collection(4, "Christmas", "Everything Else", 43),
    Collection(5, "Danse Macabre", "Everything Else", 44),
    Collection(6, "Dark World", "Everything Else", 45),
    Collection(7, "Disco and Lounge", "Electronic and Rock", 4),
    Collection(8, "Famous Classics", "Everything Else", 46),
    Collection(9, "Mad Pianist", "Everything Else", 47),
    Collection(10, "Far East Inspired", "World", 17),
    Collection(11, "Funk and Blues", "Electronic and Rock", 8),
    Collection(12, "Hard Electronic", "Electronic and Rock", 1),
    Collection(13, "Heartfelt Melodies", "Lighter Faire", 39),
    Collection(14, "Jazz", "Everything Else", 16),
    Collection(15, "Latin Sounds", "World", 15),
    Collection(16, "Elegant Piano", "Lighter Faire", 38),
    Collection(17, "Light", "Lighter Faire", 37),
    Collection(18, "Medium Electronic", "Electronic and Rock", 2),
    Collection(19, "Native American", "World", 14),
    Collection(20, "Thoughtful", "Lighter Faire", 40),
    Collection(21, "Oddities", "Everything Else", 48),
    Collection(22, "Polka", "World", 12),
    Collection(23, "Reggae and Ska", "World", 11),
    Collection(24, "Rock Classic", "Electronic and Rock", 5),
    Collection(25, "Rock Medium", "Electronic and Rock", 6),
    Collection(26, "Rock Harder", "Electronic and Rock", 7),
    Collection(27, "Serenity", "Lighter Faire", 41),
    Collection(28, "Touching Moments", "Lighter Faire", 42),
    Collection(29, "Video Classica", "Electronic and Rock", 3),
    Collection(30, "Wonders of Other Worlds", "Everything Else", 49),
    Collection(31, "Silent Film - Bright", "Film Scoring Moods", 18),
    Collection(32, "Silent Film - Dark", "Film Scoring Moods", 19),
    Collection(33, "Action", "Film Scoring Moods", 20),
    Collection(34, "Comedic", "Film Scoring Moods", 21),
    Collection(35, "Darkness and Unease", "Film Scoring Moods", 22),
    Collection(36, "Gloom and Sadness", "Film Scoring Moods", 23),
    Collection(37, "Horror Soundscapes", "Film Scoring Moods", 24),
    Collection(38, "Horror Themes", "Film Scoring Moods", 25),
    Collection(39, "Misc", "Film Scoring Moods", 26),
    Collection(40, "Mystery", "Film Scoring Moods", 27),
    Collection(41, "Noire", "Film Scoring Moods", 28),
    Collection(42, "Transitions", "Film Scoring Moods", 29),
    Collection(43, "Aspiring", "Film Scoring Moods", 30),
    Collection(44, "Tension", "Film Scoring Moods", 31),
    Collection(45, "Wonder", "Film Scoring Moods", 32),
    Collection(46, "Middle East Inspired", "World", 10),
    Collection(47, "Polynesian", "World", 54),
    Collection(49, "Brazilian", "World", 56),
    Collection(50, "Western European", "World", 52),
    Collection(999, "not in a collection", "not in a collection", 999),
)

COLLECTIONS_BY_CODE: dict[int, Collection] = {c.code: c for c in COLLECTIONS}

# The `feel` vocabulary the catalogue page offers as filter buttons. It is the
# canonical set, not the observed one: a piece may carry a word that is not
# here (one does), and that word is kept rather than dropped.
FEELS: tuple[str, ...] = (
    "Action", "Aggressive", "Bouncy", "Bright", "Calm", "Calming", "Dark",
    "Driving", "Eerie", "Epic", "Grooving", "Humorous", "Intense",
    "Mysterious", "Mystical", "Relaxed", "Somber", "Suspenseful",
    "Unnerving", "Uplifting",
)


def genre_name(raw) -> str | None:
    """Name for a genre id, or None where the catalogue means nothing by it."""
    try:
        return GENRES.get(int(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def collection_for(raw) -> Collection | None:
    """Collection for the **code** a piece carries. See `Collection`."""
    try:
        return COLLECTIONS_BY_CODE.get(int(str(raw).strip()))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- normalising

_WS = re.compile(r"\s+")


def clean(v) -> str:
    """One field of catalogue text as a single stripped line.

    Absent is `""`, whether it arrived as null or as an empty string, and the
    `\r\n` runs inside titles, instrument lists and descriptions collapse to
    single spaces rather than surviving into a database column.
    """
    if v is None:
        return ""
    return _WS.sub(" ", str(v)).strip()


def _clean_or_none(v) -> str | None:
    """Same, but absent is None — for the URL columns, which are nullable."""
    return clean(v) or None


def parse_length(v) -> int | None:
    """"hh:mm:ss" (or "mm:ss", or a bare count) as whole seconds.

    "00:00:00" — three rows, one of them titled "Easy Lemon (30 second)" — is
    missing data rather than a zero-length track, so it lands as None with the
    other unknowns instead of at the bottom of every duration sort.
    """
    s = clean(v)
    if not s:
        return None
    total = 0
    for part in s.split(":"):
        try:
            total = total * 60 + int(part or 0)
        except ValueError:
            return None
    return total or None


def parse_bpm(v) -> int | None:
    """Tempo as an integer, or None where the catalogue does not know one.

    238 rows say "0" and eight say null. Zero beats per minute is not a tempo
    — it is the same "no idea" the nulls are — so both land as None rather
    than as a number that would sort to the front of every tempo filter.

    Anything else the field can hold is an unknown too, and that includes the
    two strings `float` accepts and `int` then refuses: "inf" and "1e400" parse
    and overflow, which is an `OverflowError` rather than the `ValueError` the
    word "fast" raises. A parser whose job is surviving dirty input must not be
    the thing that fails a build.
    """
    s = clean(v)
    if not s:
        return None
    try:
        n = int(float(s))
    except (ValueError, OverflowError):
        return None
    return n if n > 0 else None


def parse_date(v) -> str | None:
    """The upload date if it is an ISO one, so date ranges compare as text."""
    s = clean(v)
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def split_multi(v) -> list[str]:
    """A comma-separated free-text field as a de-duplicated list.

    Order is the catalogue's; duplicates within one piece (four have them) are
    dropped case-insensitively, keeping the first spelling.
    """
    out: list[str] = []
    seen: set[str] = set()
    for part in clean(v).split(","):
        name = clean(part)
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def mp3_url(filename: str) -> str:
    """The working MP3 URL for a filename.

    One row ships its filename already percent-encoded ("Joey%27s Formal
    Waltz.mp3"), so the name is decoded before it is encoded — quoting it
    blind would double-escape that apostrophe.
    """
    return MP3_BASE + quote(unquote(clean(filename)))


SHEETMUSIC_BASE = "https://incompetech.com/music/royalty-free/sheetmusic/"


def sheetmusic_url(v) -> str | None:
    """The 41 populated `sheetmusic` values, as URLs.

    Thirty-nine are a bare PDF filename wanting the sheetmusic directory in
    front of them; two are already absolute, and on `http://`, which is
    upgraded rather than followed.
    """
    s = clean(v)
    if not s:
        return None
    if s.startswith("http://"):
        return "https://" + s[len("http://"):]
    if s.startswith("https://"):
        return s
    return SHEETMUSIC_BASE + quote(unquote(s))


def detail_url(isrc: str) -> str | None:
    """The catalogue's per-piece page, which looks a piece up by ISRC.

    Six ISRCs are shared by two pieces each, so for those the page shows
    whichever the catalogue lists first. There is no better key: the page
    offers no other way in. It is a static shell that resolves the ISRC in the
    browser, so it answers 200 for an ISRC that does not exist — constructing
    the URL is safe, and fetching it validates nothing.
    """
    code = clean(isrc)
    return DETAIL_BASE + quote(code) if code else None


@dataclass(frozen=True)
class Piece:
    """One catalogue row, cleaned. `catalog.py` stores exactly this."""

    filename: str
    title: str
    uuid: str
    isrc: str
    length_s: int | None
    bpm: int | None
    description: str
    uploaded: str | None
    genre_id: int | None
    genre: str | None
    genre_raw: str
    collection_code: int | None
    collection: str | None
    collection_category: str | None
    collection_raw: str
    mp3_url: str
    page_url: str | None
    # Not a URL to anything that plays: see the module docstring on `wav`.
    wav_link: str | None
    video_url: str | None
    sheetmusic_url: str | None
    itunes_url: str | None
    filmmusic_url: str | None
    instruments: tuple[str, ...] = ()
    feels: tuple[str, ...] = ()


def normalize_piece(raw: dict) -> Piece | None:
    """One raw catalogue row as a `Piece`, or None if it has no filename.

    The filename is the only field that is both present and unique across the
    catalogue — `uuid` repeats twice and `isrc` six times — so it is the key,
    and a row without one has no MP3 either and is not a piece.
    """
    filename = clean(raw.get("filename"))
    if not filename:
        return None
    gid = raw.get("genre")
    coll = collection_for(raw.get("collection"))
    isrc = clean(raw.get("isrc"))
    return Piece(
        filename=filename,
        title=clean(raw.get("title")) or filename.rsplit(".", 1)[0],
        uuid=clean(raw.get("uuid")),
        isrc=isrc,
        length_s=parse_length(raw.get("length")),
        bpm=parse_bpm(raw.get("bpm")),
        description=clean(raw.get("description")),
        uploaded=parse_date(raw.get("uploaded")),
        genre_id=int(clean(gid)) if clean(gid).isdigit() and genre_name(gid) else None,
        genre=genre_name(gid),
        genre_raw=clean(gid),
        collection_code=coll.code if coll else None,
        collection=coll.name if coll else None,
        collection_category=coll.category if coll else None,
        collection_raw=clean(raw.get("collection")),
        mp3_url=mp3_url(filename),
        page_url=detail_url(isrc),
        wav_link=_clean_or_none(raw.get("wav")),
        video_url=_clean_or_none(raw.get("video")),
        sheetmusic_url=sheetmusic_url(raw.get("sheetmusic")),
        itunes_url=_clean_or_none(raw.get("itunes")),
        filmmusic_url=_clean_or_none(raw.get("filmmusicURL")),
        instruments=tuple(split_multi(raw.get("instruments"))),
        feels=tuple(split_multi(raw.get("feel"))),
    )


def normalize_catalog(rows) -> list[Piece]:
    """Every row that is a piece, in catalogue order, first spelling wins.

    Two rows sharing a filename would be one piece listed twice; there are
    none today, and if there ever are, the later one is dropped rather than
    silently overwriting the earlier.
    """
    out: list[Piece] = []
    seen: set[str] = set()
    for raw in rows or ():
        if not isinstance(raw, dict):
            continue
        piece = normalize_piece(raw)
        if piece is None or piece.filename in seen:
            continue
        seen.add(piece.filename)
        out.append(piece)
    return out


# ------------------------------------------------------------------ fetch


def fetch_catalog(client) -> list[dict]:
    """The raw catalogue. One request, no key, ~960 KB."""
    r = client.get(CATALOG_URL)
    r.raise_for_status()
    doc = r.json()
    if not isinstance(doc, list):
        raise RuntimeError(f"{CATALOG_URL}: expected a list of pieces")
    return doc


_ARRAY = r"const {name} = (\[.*?\]);"


def parse_lookups(html: str) -> dict:
    """The three lookup arrays out of the catalogue page's inline JavaScript.

    They are JSON in every way that matters, so they are parsed as JSON rather
    than by a second, worse parser. A missing or changed array yields None for
    that table instead of an exception: this is drift-checking, and a page
    rewrite should report rather than crash.
    """
    out: dict = {"genres": None, "collections": None, "feels": None}
    for key, name in (("genres", "genres"), ("collections", "collections"),
                      ("feels", "feelsList")):
        m = re.search(_ARRAY.format(name=name), html, re.S)
        if not m:
            continue
        try:
            out[key] = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
    return out


def fetch_lookups(client) -> dict:
    r = client.get(LOOKUPS_URL)
    r.raise_for_status()
    return parse_lookups(r.text)


def lookup_drift(parsed: dict) -> list[str]:
    """What the page now says that the tables above do not, as English.

    Empty means the transcription is still right. Anything else is a line to
    put in this module by hand — these tables are checked in on purpose, so
    that a catalogue fetch is one request and a build works offline.
    """
    notes: list[str] = []

    genres = parsed.get("genres")
    if genres is None:
        notes.append("genres: the page no longer carries a `genres` array")
    else:
        live = {int(g["id"]): str(g["genre"]) for g in genres}
        for gid, name in sorted(live.items()):
            if GENRES.get(gid) != name:
                notes.append(f"genre {gid}: page says {name!r}, table says "
                             f"{GENRES.get(gid)!r}")
        for gid in sorted(set(GENRES) - set(live)):
            notes.append(f"genre {gid} ({GENRES[gid]!r}) is gone from the page")

    collections = parsed.get("collections")
    if collections is None:
        notes.append("collections: the page no longer carries a `collections` array")
    else:
        live = {int(c["code"]): (str(c["collection_name"]),
                                 str(c["collection_category"]))
                for c in collections}
        for code, (name, cat) in sorted(live.items()):
            have = COLLECTIONS_BY_CODE.get(code)
            if have is None:
                notes.append(f"collection code {code} ({name!r}) is new")
            elif (have.name, have.category) != (name, cat):
                notes.append(f"collection code {code}: page says {name!r} / "
                             f"{cat!r}, table says {have.name!r} / {have.category!r}")
        for code in sorted(set(COLLECTIONS_BY_CODE) - set(live)):
            notes.append(f"collection code {code} "
                         f"({COLLECTIONS_BY_CODE[code].name!r}) is gone from the page")

    feels = parsed.get("feels")
    if feels is None:
        notes.append("feels: the page no longer carries a `feelsList` array")
    elif tuple(str(f) for f in feels) != FEELS:
        notes.append(f"feels: page says {list(feels)}, table says {list(FEELS)}")

    return notes
