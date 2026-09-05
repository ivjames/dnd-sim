"""The incompetech catalogue database: normalising it, and querying it.

No network. The fixture below is a handful of real catalogue rows with every
kind of dirt the real 1442 carry — embedded `\\r\\n`, leading whitespace in
front of a URL, null-versus-empty-string, a tempo of "0" that means "not
measured", a length of "00:00:00" that means "unknown", a feel outside the
page's vocabulary, an already-percent-encoded filename, a repeated ISRC and a
repeated UUID, and the same instrument spelled two ways.

The one thing worth stating twice: `collection` is a **code**, and code 12 is
Hard Electronic while *id* 12 is Polka. A join written the wrong way round
still returns a name for every row, which is why it is tested rather than
eyeballed.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.audio import catalog as CAT
from tools.audio import incompetech as I

# A row as the catalogue really ships one. Anything not overridden is clean.
CLEAN = {
    "uuid": "00000000-0000-0000-0000-000000000000",
    "title": "Untitled", "filename": "Untitled.mp3", "length": "00:02:00",
    "instruments": "Piano", "genre": "22", "bpm": "100", "description": "",
    "feel": "Calm", "uploaded": "2015-05-05", "isrc": "USUAN1500001",
    "collection": "999", "sheetmusic": None, "video": None, "itunes": "",
    "wav": None, "filmmusicURL": None,
}


def row(**kw) -> dict:
    return {**CLEAN, **kw}


CATALOG = [
    # Dirty in every field that can be dirty, and in collection code 12.
    row(uuid="61123837", title="Dungeon Descent\r\n\r\n",
        filename="Dungeon Descent.mp3", length="00:03:20",
        instruments="Strings, Choir\r\n\r\n, Strings", genre="10", bpm="70",
        description="  Slow dread under a stone ceiling.\r\n",
        feel="Dark, Eerie, Mysterious", uploaded="2020-01-02",
        isrc="USUAN2000001", collection="12",
        sheetmusic="Dungeon Descent.pdf", video=" http://youtu.be/abc",
        itunes="", wav="https://incompetech.com/music/royalty-free/Downloads/x.html",
        filmmusicURL=""),
    # The one off-vocabulary feel in the catalogue, on the one piece with it.
    row(uuid="61123838", title="The Britons", filename="The Britons.mp3",
        length="00:05:07", instruments="Lute, Recorder", genre="22", bpm="180",
        description="Tavern music like from the olden days.",
        feel="Ren Faire, Medieval", uploaded="2026-06-29", isrc="USUAN2600004",
        collection="999"),
    # bpm "0" and bpm null both mean "not measured", not a tempo.
    row(title="Corncob", filename="Corncob.mp3", length="00:00:00", bpm="0",
        feel="Bouncy, Humorous", genre="24", collection="21",
        uploaded="2009-03-01", isrc="USUAN0900001"),
    row(title="Discovery Hit", filename="Discovery Hit.mp3", length="00:00:06",
        bpm=None, genre="23", feel="Epic, Intense", collection="42",
        instruments="Brass", uploaded="2011-01-01", isrc="USUAN1100003"),
    # A tempo that is not a number at all.
    row(title="Broken Tempo", filename="Broken Tempo.mp3", bpm="fast",
        feel="Dark", genre="10", collection="35", uploaded="2018-08-08",
        isrc="USUAN1800001"),
    # Already percent-encoded on the way in; an absolute sheetmusic URL.
    row(title="Joey's Formal Waltz", filename="Joey%27s Formal Waltz.mp3",
        length="00:04:00", bpm="90", feel="Bright", genre="4",
        collection="16", uploaded="2012-02-02", isrc="USUAN1200090",
        sheetmusic="http://incompetech.com/music/royalty-free/sheetmusic/Deuces.pdf",
        instruments="PIano"),
    # Two pieces, one ISRC, one of them sharing a UUID with the first row.
    row(uuid="USUAN1900054", title="A Very Brady Special",
        filename="A Very Brady Special.mp3", isrc="USUAN1900054",
        feel="Humorous", genre="13", collection="39", bpm="112",
        instruments="Piano, Drums", uploaded="2019-04-04"),
    row(uuid="USUAN1900054", title="Royal Coupling", filename="Royal Coupling.mp3",
        isrc="USUAN1900054", feel="Uplifting", genre="13", collection="39",
        bpm="112", instruments="piano", uploaded="2019-04-05"),
    # Not a piece: no filename, so no MP3 and nothing to key on.
    row(title="Ghost row", filename=""),
]


@pytest.fixture
def db():
    conn = CAT.connect(":memory:")
    CAT.build(conn, CATALOG, fetched_at="2026-09-05T00:00:00+00:00")
    yield conn
    conn.close()


# ------------------------------------------------------------- lookup tables


def test_collection_resolves_by_code_and_not_by_id():
    """The gotcha. `getCollectionName()` on the catalogue page matches `code`."""
    twelve = I.collection_for("12")
    assert (twelve.name, twelve.category) == ("Hard Electronic", "Electronic and Rock")
    by_id = [c for c in I.COLLECTIONS if c.source_id == 12]
    assert [c.name for c in by_id] == ["Polka"], "id 12 is a different collection"
    assert twelve.source_id == 1


def test_the_lookup_tables_are_the_shape_the_catalogue_publishes():
    assert len(I.GENRES) == 24
    assert len(I.COLLECTIONS) == 50 == len(I.COLLECTIONS_BY_CODE)
    assert len(I.FEELS) == 20
    assert I.genre_name("10") == I.genre_name(10) == "Horror"
    assert I.genre_name("") is None and I.genre_name(None) is None
    assert I.genre_name("17") is None, "17 is not a genre; it is a hole in the list"


def test_sources_shares_this_one_transcription():
    """There is one copy of someone else's numbering in this repo, not two."""
    from tools.audio import sources as S
    assert not hasattr(S.IncompetechSource, "GENRES")
    assert S.IncompetechSource.CATALOG == I.CATALOG_URL
    assert S.IncompetechSource.FILES == I.MP3_BASE


LOOKUP_JS = """
const genres = [ { "id": 10, "genre": "Horror" } ];
const collections = [
  {"id":"1","collection_name":"Hard Electronic","collection_category":"Electronic and Rock","code":"12"}
];
const feelsList = ["Dark"];
"""


def test_lookup_drift_reads_the_page_and_reports_what_moved():
    parsed = I.parse_lookups(LOOKUP_JS)
    assert parsed["collections"][0]["code"] == "12"
    notes = I.lookup_drift(parsed)
    # This snippet is a subset, so everything else reads as "gone from the page".
    assert any("gone from the page" in n for n in notes)
    assert not any(n.startswith(("genre 10:", "collection code 12:")) for n in notes), \
        "what the snippet does carry agrees with the table"

    moved = I.parse_lookups(LOOKUP_JS.replace('"Horror"', '"Terror"'))
    assert any("genre 10: page says 'Terror'" in n for n in I.lookup_drift(moved))


def test_a_page_without_the_arrays_reports_instead_of_raising():
    notes = I.lookup_drift(I.parse_lookups("<html>nothing here</html>"))
    assert len(notes) == 3 and all("no longer carries" in n for n in notes)


# --------------------------------------------------------------- normalising


def test_the_dirt_comes_out_in_the_wash():
    p = I.normalize_piece(CATALOG[0])
    assert p.title == "Dungeon Descent", "no \\r\\n survives into a column"
    assert p.description == "Slow dread under a stone ceiling."
    assert p.instruments == ("Strings", "Choir"), "deduplicated, newlines gone"
    assert p.feels == ("Dark", "Eerie", "Mysterious")
    assert p.length_s == 200
    assert p.bpm == 70
    assert p.genre == "Horror" and p.genre_id == 10
    assert (p.collection, p.collection_category) == ("Hard Electronic",
                                                     "Electronic and Rock")
    assert p.video_url == "http://youtu.be/abc", "the leading space is stripped"
    assert p.sheetmusic_url == (
        "https://incompetech.com/music/royalty-free/sheetmusic/Dungeon%20Descent.pdf")
    assert p.mp3_url == (
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Dungeon%20Descent.mp3")
    assert p.page_url.endswith("index.html?isrc=USUAN2000001")


def test_null_and_empty_string_both_mean_absent():
    p = I.normalize_piece(CATALOG[0])          # itunes "", filmmusicURL ""
    q = I.normalize_piece(CATALOG[1])          # both null
    assert p.itunes_url is None and p.filmmusic_url is None
    assert q.itunes_url is None and q.filmmusic_url is None
    assert q.wav_link is None
    assert p.wav_link, "the junk `wav` value is kept verbatim, just not as audio"


@pytest.mark.parametrize("raw,secs", [
    ("00:03:20", 200), ("00:00:06", 6), ("1:02:03", 3723), ("3:30", 210),
    ("00:00:00", None), ("", None), (None, None), ("nope", None),
])
def test_lengths_parse_and_zero_means_unknown(raw, secs):
    assert I.parse_length(raw) == secs


@pytest.mark.parametrize("raw,bpm", [
    ("70", 70), (" 128 ", 128), ("0", None), (None, None), ("", None),
    ("fast", None), ("120.0", 120),
])
def test_tempos_parse_and_zero_means_not_measured(raw, bpm):
    assert I.parse_bpm(raw) == bpm


def test_the_already_encoded_filename_is_not_encoded_twice():
    p = I.normalize_piece(CATALOG[5])
    assert p.mp3_url.endswith("Joey%27s%20Formal%20Waltz.mp3")
    assert "%2527" not in p.mp3_url
    assert p.sheetmusic_url.startswith("https://"), "http:// is upgraded, not followed"


def test_a_row_without_a_filename_is_not_a_piece():
    assert I.normalize_piece(CATALOG[-1]) is None
    assert len(I.normalize_catalog(CATALOG)) == len(CATALOG) - 1


def test_split_multi_keeps_order_and_drops_repeats():
    assert I.split_multi("Strings, Choir\r\n, strings") == ["Strings", "Choir"]
    assert I.split_multi(None) == []
    assert I.split_multi(" , ,") == []


def test_the_credit_is_incompetechs_own_wording():
    line = I.credit_line("Dungeon Descent")
    assert line.startswith('"Dungeon Descent" Kevin MacLeod (incompetech.com)')
    assert line.endswith("https://creativecommons.org/licenses/by/4.0/")


# --------------------------------------------------------------------- build


def test_build_ingests_every_piece_and_resolves_every_id(db):
    n = db.execute("SELECT count(*) FROM piece").fetchone()[0]
    assert n == len(CATALOG) - 1
    assert db.execute("SELECT count(*) FROM piece WHERE genre_id IS NULL").fetchone()[0] == 0
    assert db.execute(
        "SELECT count(*) FROM piece WHERE collection_code IS NULL").fetchone()[0] == 0


def test_build_reports_what_it_did():
    conn = CAT.connect(":memory:")
    stats = CAT.build(conn, CATALOG)
    assert (stats.pieces, stats.skipped) == (len(CATALOG) - 1, 1)
    assert stats.unresolved_genre == stats.unresolved_collection == 0
    assert stats.with_bpm == stats.pieces - 3, "0, null and 'fast' are all unknown"
    assert stats.with_length == stats.pieces - 1, "00:00:00 is unknown"
    assert set(stats.non_canonical_feels) == {"Medieval", "Ren Faire"}
    assert "\n".join(stats.lines()).count("unresolved 0") == 2
    conn.close()


def test_neither_isrc_nor_uuid_is_unique_and_no_row_is_dropped(db):
    got = db.execute("SELECT title FROM piece WHERE isrc = 'USUAN1900054' "
                     "ORDER BY title").fetchall()
    assert [r["title"] for r in got] == ["A Very Brady Special", "Royal Coupling"]
    dupe_uuid = db.execute(
        "SELECT count(*) FROM piece WHERE uuid = 'USUAN1900054'").fetchone()[0]
    assert dupe_uuid == 2
    assert db.execute("SELECT count(DISTINCT filename) FROM piece").fetchone()[0] == \
        db.execute("SELECT count(*) FROM piece").fetchone()[0]


def test_the_same_instrument_spelled_three_ways_is_one_instrument(db):
    rows = db.execute("SELECT name, key FROM instrument WHERE key = 'piano'").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "Piano", "the commonest spelling wins, not 'PIano'"
    n = db.execute("SELECT count(*) FROM piece_instrument pi JOIN instrument i "
                   "ON i.instrument_id = pi.instrument_id "
                   "WHERE i.key = 'piano'").fetchone()[0]
    assert n == 5, "Piano, PIano and piano are the same filter"


def test_off_vocabulary_feels_are_kept_and_flagged(db):
    rows = {r["name"]: r["canonical"] for r in db.execute("SELECT name, canonical FROM feel")}
    assert rows["Ren Faire"] == 0 and rows["Medieval"] == 0
    assert rows["Dark"] == 1 and rows["Humorous"] == 1


def test_the_licence_is_stored_with_the_data(db):
    m = CAT.meta(db)
    assert m["license"] == "by"
    assert m["author"] == "Kevin MacLeod"
    assert "creativecommons.org/licenses/by/4.0" in m["attribution"]
    assert m["fetched_at"] == "2026-09-05T00:00:00+00:00"
    assert m["source"] == I.CATALOG_URL


def test_every_row_carries_its_credit_out_of_the_database(db):
    for r in CAT.search(db, CAT.Filters(limit=0)):
        assert r["credit"] == I.credit_line(r["title"])


# --------------------------------------------------------------------- query


def titles(db, **kw) -> list[str]:
    return [r["title"] for r in CAT.search(db, CAT.Filters(limit=0, **kw))]


def test_feels_and_rather_than_or(db):
    """The whole point of the relation: three feels means all three."""
    assert titles(db, feels=("Dark",)) == ["Broken Tempo", "Dungeon Descent"]
    assert titles(db, feels=("Dark", "Eerie")) == ["Dungeon Descent"]
    assert titles(db, feels=("Dark", "Eerie", "Mysterious")) == ["Dungeon Descent"]
    assert titles(db, feels=("Dark", "Bouncy")) == [], "no piece carries both"
    # ... and the OR form is a separate flag, not the same one spelled twice.
    assert titles(db, feels_any=("Dark", "Bouncy")) == [
        "Broken Tempo", "Corncob", "Dungeon Descent"]


def test_feels_match_regardless_of_case(db):
    assert titles(db, feels=("dark", "EERIE")) == ["Dungeon Descent"]


def test_instruments_and_and_match_as_substrings(db):
    assert titles(db, instruments=("choir",)) == ["Dungeon Descent"]
    assert titles(db, instruments=("strings", "choir")) == ["Dungeon Descent"]
    assert titles(db, instruments=("piano", "drums")) == ["A Very Brady Special"]
    assert titles(db, instruments=("lute",)) == ["The Britons"]


def test_a_tempo_range_never_returns_an_unmeasured_tempo(db):
    """238 real rows say bpm 0. Treated as a number they head every slow query."""
    assert "Corncob" not in titles(db, bpm_max=90)
    assert "Broken Tempo" not in titles(db, bpm_max=90)
    assert titles(db, bpm_max=90) == ["Dungeon Descent", "Joey's Formal Waltz"]
    assert titles(db, bpm_min=112, bpm_max=180) == [
        "A Very Brady Special", "Royal Coupling", "The Britons"]
    # and they can still be asked for on purpose
    assert titles(db, bpm_unknown=True) == ["Broken Tempo", "Corncob", "Discovery Hit"]


def test_a_duration_range_never_returns_an_unknown_duration(db):
    assert titles(db, length_min=180) == ["Dungeon Descent", "Joey's Formal Waltz",
                                          "The Britons"]
    assert titles(db, length_max=10) == ["Discovery Hit"]
    assert "Corncob" not in titles(db, length_min=0), "00:00:00 is not a duration"


def test_the_axes_the_website_does_not_have(db):
    assert titles(db, categories=("Electronic and Rock",)) == ["Dungeon Descent"]
    assert titles(db, collections=("Hard Electronic",)) == ["Dungeon Descent"]
    assert titles(db, genres=("Horror",)) == ["Broken Tempo", "Dungeon Descent"]
    assert titles(db, genres=("10",)) == ["Broken Tempo", "Dungeon Descent"]
    assert titles(db, uploaded_from="2019-01-01", uploaded_to="2019-12-31") == [
        "A Very Brady Special", "Royal Coupling"]


def test_filters_combine(db):
    got = titles(db, feels=("Dark",), genres=("Horror",), bpm_max=90,
                 length_min=120, categories=("Electronic and Rock",))
    assert got == ["Dungeon Descent"]


def test_full_text_searches_title_and_description(db):
    assert titles(db, text="dread") == ["Dungeon Descent"], "found in the description"
    assert titles(db, text="britons") == ["The Britons"]
    assert titles(db, text="tavern olden") == ["The Britons"], "words AND"
    assert titles(db, text="tavern dread") == [], "and they really do AND"


def test_full_text_survives_punctuation_that_fts_treats_as_syntax(db):
    for text in ("stone -ceiling", 'a "quote', "NOT", "AND OR", "*", "x:y"):
        CAT.search(db, CAT.Filters(text=text))       # must not raise


def test_text_search_falls_back_where_the_interpreter_has_no_fts5(db):
    """Same question, LIKE instead of MATCH, for a SQLite built without FTS5."""
    db.execute("UPDATE meta SET value = '' WHERE key = 'fts'")
    assert titles(db, text="dread") == ["Dungeon Descent"]
    assert titles(db, text="Britons") == ["The Britons"]


def test_a_sqlite_without_fts5_still_builds(monkeypatch):
    monkeypatch.setattr(CAT, "have_fts5", lambda conn: False)
    conn = CAT.connect(":memory:")
    stats = CAT.build(conn, CATALOG)
    assert stats.fts is False
    assert not conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'piece_fts'").fetchall()
    assert [r["title"] for r in CAT.search(conn, CAT.Filters(text="dread"))] == \
        ["Dungeon Descent"]
    conn.close()


def test_sorting_puts_the_unknowns_last_both_ways(db):
    up = [r["bpm"] for r in CAT.search(db, CAT.Filters(sort="bpm", limit=0))]
    down = [r["bpm"] for r in CAT.search(db, CAT.Filters(sort="bpm", desc=True, limit=0))]
    assert up[:2] == [70, 90] and up[-3:] == [None, None, None]
    assert down[0] == 180 and down[-3:] == [None, None, None]


def test_limit_limits_and_count_does_not(db):
    f = CAT.Filters(limit=2)
    assert len(CAT.search(db, f)) == 2
    assert CAT.count(db, f) == len(CATALOG) - 1


def test_rows_come_back_with_their_lists_and_urls(db):
    r = CAT.search(db, CAT.Filters(text="dread"))[0]
    assert r["feels"] == ["Dark", "Eerie", "Mysterious"], "catalogue order, not alphabetical"
    assert r["instruments"] == ["Strings", "Choir"]
    assert r["mp3_url"].endswith("Dungeon%20Descent.mp3")
    assert r["collection_category"] == "Electronic and Rock"


def test_the_table_output_is_readable_and_says_when_nothing_matched(db):
    text = CAT.format_rows(CAT.search(db, CAT.Filters(feels=("Dark", "Eerie"))))
    assert "Dungeon Descent" in text and "Hard Electronic" in text
    assert "TITLE" in text.splitlines()[0]
    assert CAT.format_rows([]) == "no pieces match"


def test_hms_reads_as_a_duration():
    assert (CAT.hms(200), CAT.hms(3723), CAT.hms(6), CAT.hms(None)) == \
        ("3:20", "1:02:03", "0:06", "  ?  ")


def test_the_view_joins_the_same_way_the_query_does(db):
    r = db.execute("SELECT * FROM piece_full WHERE title = 'Dungeon Descent'").fetchone()
    assert (r["genre"], r["collection"]) == ("Horror", "Hard Electronic")


def test_the_schema_says_no_to_a_dangling_reference(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO piece_feel VALUES (1, 9999, 0)")
