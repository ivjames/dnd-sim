"""Harvesting: which source is asked for what, what gets thrown away, and
what happens to an earlier run's work.
"""

from __future__ import annotations

import json

from tools.audio import cues as C
from tools.audio import harvest as H
from tools.audio.sources import Candidate, Source


class FakeSource(Source):
    """Records every query it is asked, and answers with what it was given."""

    def __init__(self, name, answers=None, boom=None):
        self.name = name
        self.asked = []
        self.answers = answers or {}
        self.boom = boom

    def search(self, query, *, dur, limit, group=""):
        self.asked.append((query, dur, limit))
        if self.boom:
            raise self.boom
        return list(self.answers.get(query, []))


def cand(key, license="cc0", source="fake"):
    return Candidate(source=source, source_id=key, title=key, author="a",
                     license=license, license_url="", page_url="",
                     preview_url=f"https://x.invalid/{key}.mp3",
                     download_url=f"https://x.invalid/{key}.mp3", duration=2.0)


def no_sleep(_seconds):
    return None


def test_a_source_is_only_asked_for_the_groups_it_can_answer():
    fs = FakeSource("freesound")
    jam = FakeSource("jamendo")
    music = C.cue("music_combat")
    sting = C.cue("sting_crit")

    H.harvest([music, sting], [fs, jam], log=lambda *_: None, sleep=no_sleep)

    assert len(fs.asked) == len(music.queries) + len(sting.queries)
    assert len(jam.asked) == len(music.queries), "jamendo has no two-second stings"


def test_the_search_runs_inside_the_cue_duration_window():
    fs = FakeSource("freesound")
    cue = C.cue("sting_crit")
    H.harvest([cue], [fs], log=lambda *_: None, sleep=no_sleep)
    assert {d for _, d, _ in fs.asked} == {cue.dur}


def test_duplicates_across_queries_collapse_and_non_permissive_is_dropped():
    cue = C.cue("sting_crit")
    q1, q2, q3 = cue.queries
    fs = FakeSource("freesound", {
        q1: [cand("a"), cand("b")],
        q2: [cand("a"), cand("nc", license="by-nc")],
        q3: [cand("c", license="by-sa")],
    })
    doc = H.harvest([cue], [fs], log=lambda *_: None, sleep=no_sleep)
    keys = {c["source_id"] for c in doc["cues"][cue.id]["candidates"]}
    assert keys == {"a", "b", "c"}


def test_a_failing_source_is_recorded_rather_than_fatal():
    cue = C.cue("sting_crit")
    ok = FakeSource("freesound", {q: [cand("a")] for q in cue.queries})
    bad = FakeSource("freesound", boom=RuntimeError("429 slow down"))
    doc = H.harvest([cue], [ok, bad], log=lambda *_: None, sleep=no_sleep)
    assert len(doc["errors"]) == len(cue.queries)
    assert "429 slow down" in doc["errors"][0]
    assert doc["cues"][cue.id]["candidates"], "the working source still contributed"


def test_the_document_carries_the_cue_it_searched_for():
    cue = C.cue("amb_camp_fire")
    doc = H.harvest([cue], [FakeSource("freesound")], log=lambda *_: None, sleep=no_sleep)
    assert doc["cues"][cue.id]["cue"]["label"] == cue.label
    assert doc["version"] == 1 and doc["generated"]


def test_selecting_cues():
    assert {c.id for c in H.select_cues(groups=("swell",))} == {c.id for c in C.cues_in("swell")}
    assert [c.id for c in H.select_cues(ids=("sfx_dice",))] == ["sfx_dice"]
    assert all(c.required for c in H.select_cues(required_only=True))
    try:
        H.select_cues(ids=("nope",))
    except SystemExit as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("an unknown cue id should stop the run")


def test_rerunning_one_cue_keeps_the_rest(tmp_path):
    old = {"version": 1, "generated": "then", "cues": {
        "sfx_dice": {"cue": {"id": "sfx_dice"}, "candidates": [{"key": "keep"}]},
        "sting_crit": {"cue": {"id": "sting_crit"}, "candidates": [{"key": "stale"}]},
    }}
    new = {"version": 1, "generated": "now", "cues": {
        "sting_crit": {"cue": {"id": "sting_crit"}, "candidates": [{"key": "fresh"}]},
    }}
    merged = H.merge(old, new)
    assert merged["generated"] == "now"
    assert merged["cues"]["sfx_dice"]["candidates"] == [{"key": "keep"}]
    assert merged["cues"]["sting_crit"]["candidates"] == [{"key": "fresh"}]
    assert H.merge(None, new) == new


def test_written_candidates_are_json(tmp_path):
    doc = H.harvest([C.cue("sfx_dice")], [FakeSource("freesound", {
        "dice roll table wood": [cand("d1")]})], log=lambda *_: None, sleep=no_sleep)
    path = H.write_candidates(doc, tmp_path / "audio" / "candidates.json")
    assert json.loads(path.read_text())["cues"]["sfx_dice"]["candidates"][0]["source_id"] == "d1"


def test_every_source_is_rate_limited():
    from tools.audio.sources import SOURCES
    assert {s.name for s in SOURCES} <= set(H.MIN_INTERVAL)
    assert {s.name for s in SOURCES} <= set(H.SOURCE_GROUPS)
    for groups in H.SOURCE_GROUPS.values():
        assert set(groups) <= set(C.GROUPS)


def test_a_music_catalogue_is_never_asked_for_ambience():
    """incompetech is compositions. Ambience is a recording of a place."""
    assert "ambience" not in H.SOURCE_GROUPS["incompetech"]
    assert "ambience" in H.SOURCE_GROUPS["freesound"]
    assert "ambience" in H.SOURCE_GROUPS["archive"]


def test_the_cue_group_reaches_the_source():
    class Recorder(FakeSource):
        def __init__(self):
            super().__init__("archive")
            self.groups = []

        def search(self, query, *, dur, limit, group=""):
            self.groups.append(group)
            return []

    rec = Recorder()
    H.harvest([C.cue("amb_crypt_undead")], [rec], log=lambda *_: None, sleep=no_sleep)
    assert set(rec.groups) == {"ambience"}
