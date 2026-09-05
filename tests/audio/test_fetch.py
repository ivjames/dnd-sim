"""Turning a picked config into files, a manifest and credits."""

from __future__ import annotations

import json

import httpx
import pytest

from tools.audio import cues as C
from tools.audio import fetch as F


def assignment(**over):
    a = {
        "key": "freesound:1", "source": "freesound", "source_id": "1",
        "title": "Sword hit", "author": "someone",
        "license": "cc0", "license_url": "", "page_url": "https://freesound.org/s/1/",
        "preview_url": "https://cdn.invalid/1-hq.mp3",
        "download_url": "https://cdn.invalid/1-hq.mp3",
        "duration": 1.4, "gain_db": -6.0, "loop": False,
        "fade_in_ms": 0, "fade_out_ms": 120, "trim_start_s": 0.0, "trim_end_s": None,
    }
    a.update(over)
    return a


def config(**assignments):
    return {"version": 1, "assignments": assignments}


def full_config():
    """Every required cue assigned — the shape validate_config is happy with."""
    return config(**{c.id: assignment() for c in C.required_cues()})


def audio_client(body=b"ID3fake-audio-bytes", content_type="audio/mpeg", calls=None):
    def handler(request):
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, content=body, headers={"content-type": content_type})
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- validation

def test_a_complete_config_validates():
    assert F.validate_config(full_config()) == []


def test_unassigned_required_cues_are_reported():
    problems = F.validate_config(config(sting_crit=assignment()))
    assert any(p.startswith("unassigned required cues") for p in problems)


def test_a_cue_that_is_not_in_the_table_is_rejected():
    problems = F.validate_config(config(sting_kazoo=assignment()))
    assert any("not a cue in cues.py" in p for p in problems)


def test_non_commercial_is_refused_unless_allowed():
    doc = config(sting_crit=assignment(license="by-nc"))
    assert any("by-nc" in p and "outside the allowed set" in p for p in F.validate_config(doc))
    assert not any("outside the allowed set" in p
                   for p in F.validate_config(doc, allow=F.PERMISSIVE + ("by-nc",)))


def test_an_assignment_with_no_url_is_rejected():
    doc = config(sting_crit=assignment(download_url="", preview_url=""))
    assert any("no download_url" in p for p in F.validate_config(doc))


def test_knobs_must_be_numbers_and_loop_must_be_a_boolean():
    problems = F.validate_config(config(sting_crit=assignment(gain_db="loud", loop="yes")))
    assert any("gain_db must be a number" in p for p in problems)
    assert any("loop must be true or false" in p for p in problems)


def test_an_empty_config_says_so():
    assert F.validate_config({}) == ["config has no `assignments`"]
    assert F.validate_config([]) == ["config is not a JSON object"]


# ------------------------------------------------------------------ fetching

def test_fetch_writes_the_file_the_manifest_and_the_credits(tmp_path):
    doc = config(sting_crit=assignment(),
                 music_combat=assignment(license="by", title="Battle", author="Composer",
                                         source="jamendo", loop=True, gain_db=-15.0,
                                         download_url="https://cdn.invalid/battle.mp3"))
    with audio_client() as client:
        manifest = F.fetch_all(doc, tmp_path, client=client, log=lambda *_: None)
    F.write_manifest(manifest, tmp_path)
    F.write_credits(manifest, tmp_path)

    entry = manifest["cues"]["sting_crit"]
    assert entry["file"] == "assets/sting/sting_crit.mp3"
    assert (tmp_path / entry["file"]).read_bytes().startswith(b"ID3")
    assert entry["bytes"] == len(b"ID3fake-audio-bytes")
    assert len(entry["sha256"]) == 64
    # the knobs from the picker and the routing rule from the cue table
    assert entry["gain_db"] == -6.0 and entry["fade_out_ms"] == 120
    assert entry["match"] == C.cue("sting_crit").match
    assert entry["when"] == C.cue("sting_crit").when
    assert entry["credit"]["page_url"].endswith("/s/1/")

    assert (tmp_path / "manifest.json").exists()
    credits = (tmp_path / "CREDITS.md").read_text()
    assert "Battle" in credits and "Composer" in credits
    assert "must** be credited" in credits


def test_the_manifest_carries_the_finished_credit_sentence(tmp_path):
    """The player has to show this, and must not have to rebuild it.

    `credit_line` decides the wording each source requires; a page that
    re-derived it would be the same licence rule in two places, and the one in
    JavaScript would be the untested one — so the generated file carries the
    sentence and the runtime path never imports this module.
    """
    doc = config(music_combat=assignment(license="by", title="Battle", author="Composer",
                                         source="incompetech",
                                         download_url="https://cdn.invalid/battle.mp3"))
    with audio_client() as client:
        manifest = F.fetch_all(doc, tmp_path, client=client, log=lambda *_: None)
    entry = manifest["cues"]["music_combat"]
    assert entry["credit_text"] == F.credit_line(entry["credit"])
    assert "Battle" in entry["credit_text"] and "creativecommons.org" in entry["credit_text"]


def test_files_are_named_by_cue_and_grouped_by_layer(tmp_path):
    doc = config(**{c: assignment() for c in ("amb_camp_fire", "sfx_dice", "music_combat")})
    with audio_client() as client:
        manifest = F.fetch_all(doc, tmp_path, client=client, log=lambda *_: None)
    files = sorted(e["file"] for e in manifest["cues"].values())
    assert files == ["assets/ambience/amb_camp_fire.mp3",
                     "assets/music/music_combat.mp3",
                     "assets/sfx/sfx_dice.mp3"]


@pytest.mark.parametrize("ctype,url,ext", [
    ("audio/ogg", "https://x.invalid/a", ".ogg"),
    ("audio/x-wav", "https://x.invalid/a", ".wav"),
    ("application/octet-stream", "https://x.invalid/a.flac", ".flac"),
    ("application/octet-stream", "https://x.invalid/download/track/1/mp32/", ".mp3"),
    ("audio/mpeg", "https://x.invalid/a.wav?token=1", ".mp3"),
])
def test_the_extension_comes_from_the_type_then_the_url(ctype, url, ext):
    assert F._ext_for(url, ctype) == ext


def test_a_second_run_keeps_what_it_already_has(tmp_path):
    calls = []
    doc = config(sting_crit=assignment())
    with audio_client(calls=calls) as client:
        F.write_manifest(F.fetch_all(doc, tmp_path, client=client, log=lambda *_: None), tmp_path)
        assert len(calls) == 1
        F.fetch_all(doc, tmp_path, client=client, log=lambda *_: None)
        assert len(calls) == 1, "unchanged assignment should not be downloaded again"
        F.fetch_all(doc, tmp_path, client=client, force=True, log=lambda *_: None)
        assert len(calls) == 2, "--force should fetch it again"


def test_a_new_url_replaces_the_old_file_rather_than_piling_up(tmp_path):
    with audio_client(content_type="audio/ogg") as client:
        F.write_manifest(F.fetch_all(config(sting_crit=assignment()), tmp_path,
                                     client=client, log=lambda *_: None), tmp_path)
    with audio_client(content_type="audio/mpeg") as client:
        manifest = F.fetch_all(config(sting_crit=assignment(download_url="https://cdn.invalid/2.mp3")),
                               tmp_path, client=client, log=lambda *_: None)
    assert manifest["cues"]["sting_crit"]["file"].endswith(".mp3")
    assert sorted(p.name for p in (tmp_path / "assets" / "sting").iterdir()) == ["sting_crit.mp3"]


def test_an_oversized_file_is_refused_and_leaves_nothing_behind(tmp_path):
    big = b"x" * 2048
    with audio_client(body=big) as client:
        with pytest.raises(RuntimeError, match="over 1024 bytes"):
            F._download(client, "https://cdn.invalid/big.mp3", tmp_path / "big", max_bytes=1024)
    assert list(tmp_path.iterdir()) == []


def test_a_download_that_fails_does_not_stop_the_others(tmp_path):
    def handler(request):
        if "bad" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=b"ok", headers={"content-type": "audio/mpeg"})

    doc = config(sting_crit=assignment(download_url="https://cdn.invalid/bad.mp3"),
                 sfx_dice=assignment())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        manifest = F.fetch_all(doc, tmp_path, client=client, log=lambda *_: None)
    assert set(manifest["cues"]) == {"sfx_dice"}


def test_the_preview_url_is_used_when_there_is_no_download_url(tmp_path):
    calls = []
    doc = config(sfx_dice=assignment(download_url="", preview_url="https://cdn.invalid/p.mp3"))
    with audio_client(calls=calls) as client:
        F.fetch_all(doc, tmp_path, client=client, log=lambda *_: None)
    assert calls == ["https://cdn.invalid/p.mp3"]


def test_the_manifest_is_ordered_like_the_cue_table(tmp_path):
    doc = config(sfx_dice=assignment(), music_combat=assignment(), sting_crit=assignment())
    with audio_client() as client:
        manifest = F.fetch_all(doc, tmp_path, client=client, log=lambda *_: None)
    assert list(manifest["cues"]) == ["music_combat", "sting_crit", "sfx_dice"]


# -------------------------------------------------------------------- verify

def test_verify_notices_a_missing_or_edited_file(tmp_path):
    with audio_client() as client:
        F.write_manifest(F.fetch_all(config(sfx_dice=assignment()), tmp_path,
                                     client=client, log=lambda *_: None), tmp_path)
    assert F.verify(tmp_path) == []

    target = tmp_path / "assets" / "sfx" / "sfx_dice.mp3"
    target.write_bytes(b"tampered")
    assert any("does not match" in p for p in F.verify(tmp_path))

    target.unlink()
    assert any("is missing" in p for p in F.verify(tmp_path))
    assert F.verify(tmp_path / "nowhere") == [f"no manifest at {tmp_path / 'nowhere' / 'manifest.json'}"]


def test_credits_separate_what_needs_crediting_from_what_does_not(tmp_path):
    doc = config(sfx_dice=assignment(license="cc0", title="Dice"),
                 music_combat=assignment(license="by-sa", title="Fight", author="Someone"))
    with audio_client() as client:
        manifest = F.fetch_all(doc, tmp_path, client=client, log=lambda *_: None)
    text = F.write_credits(manifest, tmp_path).read_text()
    assert "## CC0 (public domain)" in text
    assert "## CC BY-SA (credit + share-alike)" in text
    assert "`music_combat` — *Fight* by Someone" in text


def test_credits_stay_quiet_when_nothing_needs_crediting(tmp_path):
    with audio_client() as client:
        manifest = F.fetch_all(config(sfx_dice=assignment()), tmp_path,
                               client=client, log=lambda *_: None)
    text = F.write_credits(manifest, tmp_path).read_text()
    assert "must** be credited" not in text
    assert "`sfx_dice`" in text


def test_a_config_survives_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(full_config()))
    assert F.validate_config(json.loads(path.read_text())) == []


# ------------------------------------------------------------- credit lines

def test_the_credit_line_is_the_sentence_you_paste():
    assert F.credit_line({
        "title": "Crypt Deep", "author": "Kevin MacLeod", "source": "incompetech",
        "license": "by", "license_url": "https://creativecommons.org/licenses/by/4.0/",
    }) == ('"Crypt Deep" Kevin MacLeod (incompetech.com) — Licensed under '
           "Creative Commons: By Attribution 4.0 — https://creativecommons.org/licenses/by/4.0/")

    assert F.credit_line({
        "title": "Cave Loop", "author": "Truman", "source": "archive",
        "license": "by-sa", "license_url": "http://creativecommons.org/licenses/by-sa/3.0/",
    }) == ('"Cave Loop" by Truman via archive — CC BY-SA — '
           "http://creativecommons.org/licenses/by-sa/3.0/")


def test_a_credit_line_survives_a_missing_licence_url():
    line = F.credit_line({"title": "T", "author": "A", "source": "freesound", "license": "by"})
    assert line.endswith("https://creativecommons.org/licenses/by/4.0/")


def test_credits_carry_a_paste_block_for_what_needs_crediting(tmp_path):
    doc = config(sfx_dice=assignment(license="cc0", title="Dice"),
                 music_combat=assignment(license="by", title="Fight", author="Kevin MacLeod",
                                         source="incompetech"),
                 music_explore=assignment(license="by", title="Fight", author="Kevin MacLeod",
                                          source="incompetech"))
    with audio_client() as client:
        manifest = F.fetch_all(doc, tmp_path, client=client, log=lambda *_: None)
    text = F.write_credits(manifest, tmp_path).read_text()

    block = text.split("```")[1]
    assert '"Fight" Kevin MacLeod (incompetech.com)' in block
    assert block.count("Fight") == 1, "one line per track, not per cue that uses it"
    assert "Dice" not in block, "CC0 needs no credit line"
    assert "`sfx_dice`" in text, "but it is still recorded below"


def test_no_paste_block_when_everything_is_public_domain(tmp_path):
    with audio_client() as client:
        manifest = F.fetch_all(config(sfx_dice=assignment()), tmp_path,
                               client=client, log=lambda *_: None)
    assert "```" not in F.write_credits(manifest, tmp_path).read_text()
