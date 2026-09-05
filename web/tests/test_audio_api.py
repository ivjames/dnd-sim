"""The score endpoints: what the pack serves, and what it refuses to.

`tools/audio` writes `audio/manifest.json` and the files beside it; these two
routes are the only way a browser sees either. The rules being pinned here are
the ones a future edit could quietly lose: the manifest is the allowlist, the
order the cues travel in is the tie-break the player depends on, and a server
with no pack answers rather than errors.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from tools.audio import cues as C
from web.app import create_app
from web.auth import ENV_VAR as WRITE_TOKEN_ENV

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PACK = os.path.join(ROOT, "audio")


def app_with(db_file, audio_dir):
    app = create_app(db_path=db_file, config={"DND_TTS": None, WRITE_TOKEN_ENV: "t",
                                              "DND_AUDIO_DIR": audio_dir})
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def pack_client(db_file):
    """The committed pack, served as it will be in production."""
    app = app_with(db_file, PACK)
    yield app.test_client()
    app.config["DND_REGISTRY"].shutdown()


# -- the probe ---------------------------------------------------------------

def test_the_committed_pack_is_served(pack_client):
    body = pack_client.get("/api/audio").get_json()
    assert body["available"] is True
    assert body["base"] == "/audio/"
    assert body["digest"] and len(body["digest"]) == 12
    assert body["cues"], "the pack in the repo has no playable cue"


def test_every_cue_carries_what_the_player_needs(pack_client):
    for cue in pack_client.get("/api/audio").get_json()["cues"]:
        assert cue["id"] in C.CUES_BY_ID
        assert cue["file"] and cue["group"] in C.GROUPS
        assert isinstance(cue["loop"], bool)
        assert cue["gain_db"] is not None
        # `match` may be null: those cues exist and are picked, but nothing in
        # the event stream fires them (AUDIO.md). The player is told so rather
        # than left to guess from a missing key.
        assert "match" in cue


def test_the_cues_travel_in_cue_table_order(pack_client):
    """The player breaks a tie on this order, so it is not decoration.

    A JSON object's key order survives neither a serializer that sorts keys nor
    every client that parses one, which is why this is a list.
    """
    body = pack_client.get("/api/audio").get_json()
    assert isinstance(body["cues"], list)
    got = [c["id"] for c in body["cues"]]
    assert got == [c.id for c in C.CUES if c.id in set(got)]


def test_every_cue_reaches_the_page_with_its_credit(pack_client):
    """CC BY is a condition of playing it, and the sentence is the manifest's:
    `tools/audio` writes `credit_text`, and neither this route nor the browser
    rebuilds it."""
    cues = pack_client.get("/api/audio").get_json()["cues"]
    assert all(c["credit"] for c in cues)
    assert any("creativecommons.org" in c["credit"] for c in cues)


def test_a_pack_with_no_credit_still_plays_and_says_nothing_it_cannot(db_file, tmp_path, caplog):
    """A manifest from before `credit_text` existed. The audio is not withheld
    — the files are the same files — but the page is handed an empty credit
    rather than an invented one, and the log says to re-fetch."""
    doc = json.loads(open(os.path.join(PACK, "manifest.json"), encoding="utf-8").read())
    one = next(iter(doc["cues"]))
    doc["cues"] = {one: {k: v for k, v in doc["cues"][one].items() if k != "credit_text"}}
    os.makedirs(tmp_path / os.path.dirname(doc["cues"][one]["file"]), exist_ok=True)
    shutil.copy(os.path.join(PACK, doc["cues"][one]["file"]), tmp_path / doc["cues"][one]["file"])
    (tmp_path / "manifest.json").write_text(json.dumps(doc), encoding="utf-8")
    app = app_with(db_file, str(tmp_path))
    try:
        body = app.test_client().get("/api/audio").get_json()
        assert body["available"] is True
        assert body["cues"][0]["credit"] == ""
    finally:
        app.config["DND_REGISTRY"].shutdown()


def test_the_pack_directory_comes_from_the_environment(db_file, tmp_path, monkeypatch):
    """`DND_AUDIO_DIR` is documented, so it has to be read.

    Flask copies no environment variable into `app.config` by itself, so an
    operator setting this would have gone on being served the checkout's own
    pack. `create_app` snapshots it, as it does the write token, and a test
    still overrides it by passing `config=`.
    """
    monkeypatch.setenv("DND_AUDIO_DIR", str(tmp_path / "elsewhere"))
    app = create_app(db_path=db_file, config={"DND_TTS": None, WRITE_TOKEN_ENV: "t"})
    try:
        assert app.config["DND_AUDIO_DIR"] == str(tmp_path / "elsewhere")
        body = app.test_client().get("/api/audio").get_json()
        assert body["available"] is False, "it served the checkout's pack, not the one named"
    finally:
        app.config["DND_REGISTRY"].shutdown()


def test_an_unset_pack_directory_is_the_checkouts_own(db_file, monkeypatch):
    monkeypatch.delenv("DND_AUDIO_DIR", raising=False)
    app = create_app(db_path=db_file, config={"DND_TTS": None, WRITE_TOKEN_ENV: "t"})
    try:
        assert app.test_client().get("/api/audio").get_json()["available"] is True
    finally:
        app.config["DND_REGISTRY"].shutdown()


def test_a_server_with_no_pack_says_so_rather_than_erroring(db_file, tmp_path):
    app = app_with(db_file, str(tmp_path / "nothing"))
    try:
        body = app.test_client().get("/api/audio").get_json()
        assert body["available"] is False and body["reason"]
    finally:
        app.config["DND_REGISTRY"].shutdown()


def test_a_manifest_that_will_not_parse_is_not_a_500(db_file, tmp_path):
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    app = app_with(db_file, str(tmp_path))
    try:
        rv = app.test_client().get("/api/audio")
        assert rv.status_code == 200 and rv.get_json()["available"] is False
    finally:
        app.config["DND_REGISTRY"].shutdown()


def test_a_cue_whose_file_is_missing_is_dropped_rather_than_offered(db_file, tmp_path):
    """Half a pack still plays. A cue pointing at nothing would be a 404 in the
    middle of a fight, so it never reaches the page at all."""
    doc = json.loads(open(os.path.join(PACK, "manifest.json"), encoding="utf-8").read())
    first = next(iter(doc["cues"]))
    os.makedirs(tmp_path / "assets" / doc["cues"][first]["group"], exist_ok=True)
    shutil.copy(os.path.join(PACK, doc["cues"][first]["file"]), tmp_path / doc["cues"][first]["file"])
    (tmp_path / "manifest.json").write_text(json.dumps(doc), encoding="utf-8")
    app = app_with(db_file, str(tmp_path))
    try:
        body = app.test_client().get("/api/audio").get_json()
        assert [c["id"] for c in body["cues"]] == [first]
    finally:
        app.config["DND_REGISTRY"].shutdown()


# -- the files ---------------------------------------------------------------

def test_a_cue_file_is_served_immutable(pack_client):
    cue = pack_client.get("/api/audio").get_json()["cues"][0]
    rv = pack_client.get("/audio/" + cue["file"])
    assert rv.status_code == 200
    assert rv.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert rv.headers["Content-Type"].startswith("audio/")
    rv.close()


def test_the_manifest_is_the_allowlist(pack_client):
    """The pack directory also holds the picker's config, with its source URLs
    and per-source ids, and the credits file. Neither is part of playing it."""
    for path in ("/audio/config.json", "/audio/CREDITS.md", "/audio/manifest.json"):
        assert pack_client.get(path).status_code == 404, path


def test_no_path_escapes_the_pack(pack_client):
    for path in ("/audio/../web/app.py", "/audio/assets/../../web/app.py", "/audio//etc/passwd"):
        assert pack_client.get(path).status_code in (301, 308, 404), path
