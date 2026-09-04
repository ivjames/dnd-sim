"""Search adapters, driven through httpx's MockTransport.

No network: each test answers the request the adapter makes with a payload
shaped like the real one, and checks both what was asked for and what came
back. The payloads are trimmed copies of real responses — the field names are
the contract, so they are spelled exactly as the APIs spell them.
"""

from __future__ import annotations

import httpx
import pytest

from tools.audio import sources as S


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.invalid")


@pytest.mark.parametrize("raw,code", [
    ("Creative Commons 0", "cc0"),
    ("http://creativecommons.org/publicdomain/zero/1.0/", "cc0"),
    ("Attribution", "by"),
    ("Attribution NonCommercial 4.0", "by-nc"),
    ("Attribution ShareAlike", "by-sa"),
    ("Attribution Noncommercial Sharealike", "by-nc-sa"),
    ("http://creativecommons.org/licenses/by-nc-nd/3.0/", "by-nc-nd"),
    ("https://creativecommons.org/licenses/by/4.0/", "by"),
    ("Sampling+", "sampling+"),
    ("", "unknown"),
    (None, "unknown"),
    ("all rights reserved", "unknown"),
])
def test_licences_normalise(raw, code):
    assert S.normalize_license(raw) == code


def test_non_commercial_never_reads_as_permissive():
    for raw in ("Attribution NonCommercial", "http://creativecommons.org/licenses/by-nc-sa/3.0/"):
        assert S.normalize_license(raw) not in S.PERMISSIVE


FREESOUND_PAGE = {
    "count": 2,
    "results": [
        {
            "id": 316847, "name": "sword-hit.wav", "username": "someone",
            "license": "http://creativecommons.org/publicdomain/zero/1.0/",
            "url": "https://freesound.org/people/someone/sounds/316847/",
            "duration": 1.4, "filesize": 250000, "type": "wav",
            "tags": ["sword", "hit", "metal"],
            "previews": {
                "preview-hq-mp3": "https://cdn.freesound.org/previews/316/316847_1-hq.mp3",
                "preview-lq-ogg": "https://cdn.freesound.org/previews/316/316847_1-lq.ogg",
            },
        },
        {   # no previews at all — unplayable in a browser, so it is dropped
            "id": 999, "name": "broken", "username": "nobody",
            "license": "Attribution", "url": "", "duration": 2.0, "previews": {},
        },
    ],
}


def test_freesound_asks_the_right_question_and_normalises_the_answer():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=FREESOUND_PAGE)

    with client_for(handler) as c:
        got = S.FreesoundSource(c, "KEY").search("sword hit", dur=(0.2, 4.0), limit=10)

    assert seen["auth"] == "Token KEY"
    assert "freesound.org/apiv2/search/text/" in seen["url"]
    assert "duration%3A%5B0.2+TO+4%5D" in seen["url"] or "duration:[0.2 TO 4]" in seen["url"]
    assert "license" in seen["url"]

    assert len(got) == 1, "the preview-less result should be dropped"
    cand = got[0]
    assert (cand.source, cand.source_id, cand.license) == ("freesound", "316847", "cc0")
    assert cand.preview_url.endswith("-hq.mp3")
    assert cand.download_url == cand.preview_url
    assert cand.duration == 1.4
    assert cand.key == "freesound:316847"
    assert cand.extra["preview"] is True


def test_freesound_falls_back_to_the_low_quality_preview():
    page = {"results": [dict(FREESOUND_PAGE["results"][0],
                             previews={"preview-lq-mp3": "https://cdn.freesound.org/x-lq.mp3"})]}
    with client_for(lambda r: httpx.Response(200, json=page)) as c:
        got = S.FreesoundSource(c, "KEY").search("x", dur=(0, 9), limit=3)
    assert got[0].preview_url.endswith("-lq.mp3")


JAMENDO_OK = {
    "headers": {"status": "success", "results_count": 1},
    "results": [{
        "id": "1886179", "name": "Dungeon Crawl", "artist_name": "Someone",
        "album_name": "Quests", "duration": 184,
        "license_ccurl": "http://creativecommons.org/licenses/by/3.0/",
        "shareurl": "https://www.jamendo.com/track/1886179",
        "audio": "https://prod-1.storage.jamendo.com/?trackid=1886179&format=mp31",
        "audiodownload": "https://prod-1.storage.jamendo.com/download/track/1886179/mp32/",
        "musicinfo": {"tags": {"genres": ["soundtrack"], "instruments": ["strings"], "vartags": ["dark"]}},
    }],
}


def test_jamendo_normalises_and_prefers_the_download_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=JAMENDO_OK)

    with client_for(handler) as c:
        got = S.JamendoSource(c, "CID").search("dungeon crawl", dur=(60, 300), limit=5)

    assert "client_id=CID" in seen["url"]
    assert "durationbetween=60_300" in seen["url"]
    cand = got[0]
    assert cand.license == "by"
    assert cand.download_url.endswith("/mp32/")
    assert cand.preview_url.startswith("https://prod-1.storage.jamendo.com/?")
    assert "soundtrack" in cand.tags and "dark" in cand.tags


def test_jamendo_streams_when_download_is_not_offered():
    payload = {"headers": {"status": "success"},
               "results": [dict(JAMENDO_OK["results"][0], audiodownload=None)]}
    with client_for(lambda r: httpx.Response(200, json=payload)) as c:
        got = S.JamendoSource(c, "CID").search("x", dur=(1, 9), limit=1)
    assert got[0].download_url == got[0].preview_url


def test_jamendo_says_so_when_the_credential_is_wrong():
    payload = {"headers": {"status": "failed", "code": 5,
                           "error_message": "Your credential is not authorized."},
               "results": []}
    with client_for(lambda r: httpx.Response(200, json=payload)) as c:
        with pytest.raises(RuntimeError, match="not authorized"):
            S.JamendoSource(c, "bad").search("x", dur=(1, 9), limit=1)


ARCHIVE_SEARCH = {"response": {"numFound": 2, "docs": [
    {"identifier": "good_item", "title": "Cave Ambience",
     "creator": ["A Person"], "licenseurl": ["http://creativecommons.org/licenses/by/4.0/"]},
    {"identifier": "unlicensed", "title": "No licence here"},
]}}

ARCHIVE_META = {"files": [
    {"name": "cover.jpg", "format": "JPEG"},
    {"name": "too short.mp3", "format": "VBR MP3", "length": "0:03", "size": "100"},
    {"name": "cave loop.mp3", "format": "VBR MP3", "length": "3:33.40", "size": "5242880"},
]}


def test_archive_drops_unlicensed_items_and_picks_a_playable_file():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "advancedsearch" in request.url.path:
            return httpx.Response(200, json=ARCHIVE_SEARCH)
        return httpx.Response(200, json=ARCHIVE_META)

    with client_for(handler) as c:
        got = S.ArchiveSource(c).search("cave ambience", dur=(20, 900), limit=5)

    # One search, and metadata fetched only for the item that had a licence.
    assert len([u for u in calls if "metadata" in u]) == 1
    assert len(got) == 1
    cand = got[0]
    assert cand.license == "by"
    assert cand.duration == pytest.approx(213.4)
    assert cand.preview_url == "https://archive.org/download/good_item/cave%20loop.mp3"
    assert cand.page_url == "https://archive.org/details/good_item"
    assert cand.author == "A Person"


def test_archive_survives_an_item_whose_metadata_fails():
    def handler(request):
        if "advancedsearch" in request.url.path:
            return httpx.Response(200, json=ARCHIVE_SEARCH)
        return httpx.Response(503)

    with client_for(handler) as c:
        assert S.ArchiveSource(c).search("x", dur=(1, 999), limit=5) == []


@pytest.mark.parametrize("raw,secs", [
    ("213.4", 213.4), ("3:33.40", 213.4), ("1:02:03", 3723.0), ("", None), (None, None), ("abc", None),
])
def test_archive_lengths_parse(raw, secs):
    assert S._seconds(raw) == (pytest.approx(secs) if secs is not None else None)


def test_build_sources_skips_what_it_has_no_key_for():
    with client_for(lambda r: httpx.Response(200, json={})) as c:
        live, skipped = S.build_sources(c, env={})
        assert [s.name for s in live] == ["incompetech", "archive"], "the keyless two"
        assert any("FREESOUND_API_KEY" in s for s in skipped)
        assert any("JAMENDO_CLIENT_ID" in s for s in skipped)

        live, _ = S.build_sources(c, env={"FREESOUND_API_KEY": "k", "JAMENDO_CLIENT_ID": "c"})
        assert [s.name for s in live] == ["freesound", "jamendo", "incompetech", "archive"]

        live, _ = S.build_sources(c, env={"FREESOUND_API_KEY": "k"}, only=("freesound",))
        assert [s.name for s in live] == ["freesound"]


INCOMPETECH_CATALOG = [
    {"uuid": "1", "title": "Crypt of the Necrodancer", "filename": "Crypt Deep.mp3",
     "length": "00:03:20", "instruments": "Strings, Choir", "genre": "10", "bpm": "70",
     "description": "Slow dread under a stone ceiling.", "feel": "Dark, Eerie",
     "isrc": "USUAN1100001"},
    {"uuid": "2", "title": "Bright Wedding", "filename": "Bright Wedding.mp3",
     "length": "00:02:10", "instruments": "Ukulele", "genre": "22", "bpm": "120",
     "description": "Cheerful nonsense.", "feel": "Bright, Bouncy", "isrc": "USUAN1100002"},
    {"uuid": "3", "title": "Discovery Hit", "filename": "Discovery Hit.mp3",
     "length": "00:00:06", "instruments": "Brass", "genre": "23", "bpm": "0",
     "description": "A short dark hit.", "feel": "Epic, Intense", "isrc": "USUAN1100003"},
]


def incompetech_client(calls=None):
    def handler(request):
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, json=INCOMPETECH_CATALOG)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_incompetech_ranks_by_the_words_that_matter():
    with incompetech_client() as c:
        got = S.IncompetechSource(c).search("dark dread music loop", dur=(45, 420), limit=10)
    assert [x.title for x in got] == ["Crypt of the Necrodancer"], "the ukulele is not dark"
    cand = got[0]
    assert cand.license == "by"
    assert cand.author == "Kevin MacLeod"
    assert cand.duration == 200
    assert cand.page_url.endswith("isrc=USUAN1100001")
    assert cand.download_url == (
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Crypt%20Deep.mp3")
    assert "Dark" in cand.tags and "Horror" in cand.tags


def test_incompetech_honours_the_cue_duration_window():
    """A six-second sting must not surface for a music bed, or the reverse."""
    with incompetech_client() as c:
        src = S.IncompetechSource(c)
        beds = [x.title for x in src.search("dark epic", dur=(45, 420), limit=10)]
        stings = [x.title for x in src.search("dark epic", dur=(0.4, 8), limit=10)]
    assert "Discovery Hit" not in beds
    assert stings == ["Discovery Hit"]


def test_incompetech_ignores_words_that_match_the_whole_catalogue():
    with incompetech_client() as c:
        assert S.IncompetechSource(c).search("music loop track", dur=(0, 999), limit=10) == []


def test_incompetech_fetches_its_catalogue_once():
    calls = []
    with incompetech_client(calls) as c:
        src = S.IncompetechSource(c)
        src.search("dark", dur=(0, 999), limit=5)
        src.search("bright", dur=(0, 999), limit=5)
    assert len(calls) == 1, "the catalogue is fetched per run, not per query"


def test_incompetech_needs_no_credential():
    with incompetech_client() as c:
        live, _ = S.build_sources(c, env={})
        assert "incompetech" in [s.name for s in live]


def test_incompetech_strips_the_stray_newlines_in_the_catalogue():
    """Real rows carry trailing newlines; one in a filename breaks the URL."""
    row = dict(INCOMPETECH_CATALOG[0], title="Mesmerizing Galaxy\n",
               filename="Mesmerizing Galaxy.mp3\n", isrc="USUAN1100009\n")
    with client_for(lambda r: httpx.Response(200, json=[row])) as c:
        cand = S.IncompetechSource(c).search("dark", dur=(0, 999), limit=1)[0]
    assert cand.title == "Mesmerizing Galaxy"
    assert cand.download_url.endswith("Mesmerizing%20Galaxy.mp3")
    assert cand.page_url.endswith("isrc=USUAN1100009")


def test_archive_asks_for_field_recordings_when_the_cue_wants_ambience():
    """A crypt wants the sound of a crypt, not a composition about one."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "advancedsearch" in request.url.path:
            return httpx.Response(200, json=ARCHIVE_SEARCH)
        return httpx.Response(200, json=ARCHIVE_META)

    with client_for(handler) as c:
        src = S.ArchiveSource(c)
        src.search("crypt ambience", dur=(20, 900), limit=3, group="ambience")
        src.search("battle music", dur=(45, 420), limit=3, group="music")

    ambience, music = seen[0], [u for u in seen if "advancedsearch" in u][1]
    assert "field+recording" in ambience or "field recording" in ambience
    assert "aporee" in ambience
    assert "aporee" not in music, "a music bed is a composition; do not narrow it to recordings"


def test_group_is_optional_for_the_sources_that_have_one_kind_of_thing():
    with client_for(lambda r: httpx.Response(200, json=FREESOUND_PAGE)) as c:
        assert S.FreesoundSource(c, "K").search("x", dur=(0, 9), limit=1, group="ambience")
    with incompetech_client() as c:
        assert S.IncompetechSource(c).search("dark", dur=(0, 999), limit=1, group="music")


def test_archive_drops_the_words_that_match_the_whole_corpus():
    terms = S.ArchiveSource._terms("cave ambience water drips")
    assert "ambience" not in terms
    assert set(terms.split(" OR ")) == {"cave", "water", "drips"}
    # something has to be searched for, even when every word is generic
    assert S.ArchiveSource._terms("ambience loop") == "ambience loop"


ARCHIVE_MANY_FORMATS = {"files": [
    {"name": "rec.ogg", "format": "Ogg Vorbis", "length": "120", "size": "900000"},
    {"name": "rec.mp3", "format": "VBR MP3", "length": "120", "size": "1900000"},
    {"name": "rec_64.mp3", "format": "64Kbps MP3", "length": "120", "size": "900000"},
]}


def test_one_recording_in_three_encodings_is_one_candidate():
    def handler(request):
        if "advancedsearch" in request.url.path:
            return httpx.Response(200, json=ARCHIVE_SEARCH)
        return httpx.Response(200, json=ARCHIVE_MANY_FORMATS)

    with client_for(handler) as c:
        got = S.ArchiveSource(c).search("cave", dur=(20, 900), limit=9, group="ambience")

    assert len(got) == 1, "the same field recording twice is a wasted audition"
    assert got[0].preview_url.endswith("rec.mp3"), "and it should be the best encoding"
    assert got[0].title == "Cave Ambience", "the item's title beats a filename"
