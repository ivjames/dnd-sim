"""Voice casting in `web/static/speech.js`, exercised through node.

The module is deliberately dependency-free so `node` can run it directly (see
README, "Spoken narration"); this drives it from pytest so the rules that
decide *who* speaks in *which* voice are checked with the rest of the suite,
and checks the "can it speak" predicate against the real SRD stat blocks
rather than against invented strings. Skipped where there is no node.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPEECH_JS = os.path.join(ROOT, "web", "static", "speech.js")
MONSTERS = os.path.join(ROOT, "engine", "data", "monsters.json")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run_js(body: str, arg: object = None) -> object:
    """Run `body` with `S` bound to the module and `IN` to `arg`; return its JSON."""
    script = (
        "const S = require(%s);\n"
        "const IN = JSON.parse(process.argv[1] || 'null');\n"
        "const out = (function () {\n%s\n})();\n"
        "process.stdout.write(JSON.stringify(out === undefined ? null : out));\n"
    ) % (json.dumps(SPEECH_JS), body)
    proc = subprocess.run(
        ["node", "-e", script, json.dumps(arg)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# A device that ships both kinds, as macOS does. `default` marks the DM's voice.
VOICES = [
    {"name": "Samantha", "lang": "en-US", "default": True},
    {"name": "Alex", "lang": "en-US"},
    {"name": "Daniel", "lang": "en-GB"},
    {"name": "Fiona", "lang": "en-GB"},
    {"name": "Karen", "lang": "en-AU"},
    {"name": "Albert", "lang": "en-US"},
    {"name": "Bubbles", "lang": "en-US"},
    {"name": "Trinoids", "lang": "en-US"},
    {"name": "Zarvox (English (US))", "lang": "en-US"},
    {"name": "Amelie", "lang": "fr-CA"},
]
NOVELTY_NAMES = {"Albert", "Bubbles", "Trinoids", "Zarvox (English (US))"}
PLAIN = [v for v in VOICES if v["name"] not in NOVELTY_NAMES]


def test_can_speak_matches_the_srd_stat_blocks():
    """Every distinct `languages` string in engine/data/monsters.json."""
    langs = sorted({m.get("languages") for m in json.load(open(MONSTERS, encoding="utf-8"))})
    got = dict(zip(langs, run_js("return IN.map(S.canSpeakLanguages);", langs)))

    mute = [s for s, ok in got.items() if not ok]
    assert set(mute) == {
        "—",
        "understands all it knew in life but can't speak",
        "understands the languages it knew in life but can't speak",
    }
    assert got["Common, Goblin"] and got["any one language (usually Common)"]
    assert got["the languages it knew in life"]          # a ghost talks
    # Nothing at the table, and nothing recorded: no voice guessed either way.
    assert run_js("return [S.canSpeakLanguages(null), S.canSpeakLanguages(''),"
                  " S.canSpeakLanguages('\\u2014 (telepathy 60 ft.)'),"
                  " S.canSpeakLanguages(\"understands Common but doesn't speak it\")];") \
        == [False, False, False, False]


def test_speaking_monsters_from_a_snapshot():
    combatants = {
        "pc_thorin": {"id": "pc_thorin", "kind": "pc", "side": "party"},
        "goblin_1": {"id": "goblin_1", "kind": "monster", "side": "enemy",
                     "stat_block": {"languages": "Common, Goblin"}},
        "wolf_1": {"id": "wolf_1", "kind": "monster", "side": "enemy",
                   "stat_block": {"languages": "—"}},
        "zombie_1": {"id": "zombie_1", "kind": "monster", "side": "enemy",
                     "stat_block": {"languages": "understands all it knew in life "
                                                 "but can't speak"}},
        "smith": {"id": "smith", "kind": "monster", "side": "neutral"},   # no stat block
    }
    assert run_js("return S.speakingMonsters(IN);", combatants) == {"goblin_1": True}


def test_only_a_speaking_monster_is_dealt_a_novelty_voice():
    party = {"pc_thorin": True}
    monsters = {"goblin_1": True, "ogre_1": True}
    keys = run_js(
        "const ev = (actor, kind) => ({kind: kind || 'dialogue', actor: actor});\n"
        "return [S.voiceKeyFor(ev('pc_thorin'), IN.party, IN.monsters),\n"
        "        S.voiceKeyFor(ev('goblin_1'), IN.party, IN.monsters),\n"
        "        S.voiceKeyFor(ev('wolf_1'), IN.party, IN.monsters),\n"
        "        S.voiceKeyFor(ev('goblin_1', 'narration'), IN.party, IN.monsters),\n"
        "        S.voiceKeyFor(ev('goblin_1'), IN.party, null)];",
        {"party": party, "monsters": monsters},
    )
    assert keys == ["pc_thorin", "monster:goblin_1", "npc", "dm", "npc"]

    profiles = run_js(
        "return IN.keys.map(k => S.voiceProfileFor(k, IN.voices, 'en-US'));",
        {"keys": ["dm", "pc_thorin", "npc", "monster:goblin_1", "monster:ogre_1"],
         "voices": VOICES},
    )
    named = [(p["voice"] or {}).get("name") for p in profiles]
    dm, pc, npc, goblin, ogre = named
    assert dm == "Samantha"                                   # the flagged default
    assert {dm, pc, npc} <= {v["name"] for v in PLAIN}         # never a novelty voice
    assert {goblin, ogre} <= NOVELTY_NAMES
    # A four-voice novelty pool cannot give every monster its own, so the
    # spread that tells them apart is pitch and rate — never the same seat twice.
    assert profiles[3] != profiles[4]
    for p in profiles[3:]:
        assert 0.7 <= p["pitch"] <= 1.3 and 0.9 <= p["rate"] <= 1.1

    # Same id, same voice, every load — and the same pitch and rate with it.
    again = run_js("return S.voiceProfileFor('monster:goblin_1', IN, 'en-US');", VOICES)
    assert again == profiles[3] and again["key"] == "monster:goblin_1"


def test_without_a_novelty_voice_a_monster_sounds_like_any_npc():
    """Windows, Android, most of Linux: the pre-existing shared NPC voice."""
    got = run_js(
        "return [S.voiceProfileFor('monster:goblin_1', IN, 'en-US'),\n"
        "        S.voiceProfileFor('npc', IN, 'en-US')];",
        PLAIN,
    )
    monster, npc = got
    assert monster["voice"] == npc["voice"] and monster["pitch"] == npc["pitch"]
    assert monster["key"] == "monster:goblin_1"          # the profile still names its seat

    # A novelty voice in the wrong language is no better than none: an English
    # Zarvox reading French is the failure the language filter exists to avoid.
    fr = run_js(
        "return [(S.voiceProfileFor('monster:goblin_1', IN, 'fr-CA').voice || {}).name,\n"
        "        (S.voiceProfileFor('npc', IN, 'fr-CA').voice || {}).name];",
        VOICES,
    )
    assert fr == ["Amelie", "Amelie"]


# One line each for the three kinds of speaker, as the page would replay them.
PARTY_IDS = {"pc_thorin": True}
SPEAKING = {"goblin_1": True}
LINES = {
    "pc": {"kind": "dialogue", "actor": "pc_thorin", "text": "Not today.",
           "data": {"speaker": "Thorin"}},
    "npc": {"kind": "dialogue", "actor": "npc_marta", "text": "Rooms are two silver.",
            "data": {"speaker": "Elder Marta"}},
    "monster": {"kind": "dialogue", "actor": "goblin_1", "text": "I'll gut you.",
                "data": {"speaker": "Goblin Sneak"}},
}


def speech_of(ev: dict) -> list:
    """Every clip the page would ask for, in order: [[voice key, words], ...]."""
    return run_js(
        "return S.segmentsFor(IN.ev, {}, IN.party, IN.monsters)\n"
        "        .map(s => [s.key, s.text]);",
        {"ev": ev, "party": PARTY_IDS, "monsters": SPEAKING},
    )


def test_a_speaker_name_is_never_inside_the_spoken_line():
    """What the speaker's own voice says is only ever what they said.

    The name used to be prepended to the phrase, so it was billed, distorted
    with the monster's voice, and read as a label by the colon in the middle of
    it. `phraseFor` is now the words alone for every speaker.
    """
    words = run_js(
        "return ['pc', 'npc', 'monster'].map(k => S.phraseFor(IN.lines[k], {}, IN.party));",
        {"lines": LINES, "party": PARTY_IDS},
    )
    assert words == ["Not today.", "Rooms are two silver.", "I'll gut you."]


def test_the_narrator_names_the_speaker_and_a_pc_needs_no_naming():
    """Two clips for a monster or an NPC, one for a PC.

    A PC's own voice has always been their attribution. A monster's timbre and
    the one shared NPC voice are not, so the narrator says who is speaking —
    in the DM's voice, as its own clip, and never in the speaker's.
    """
    assert speech_of(LINES["monster"]) == [
        ["dm", "Goblin Sneak."],
        ["monster:goblin_1", "I'll gut you."],
    ]
    assert speech_of(LINES["npc"]) == [
        ["dm", "Elder Marta."],
        ["npc", "Rooms are two silver."],
    ]
    assert speech_of(LINES["pc"]) == [["pc_thorin", "Not today."]]

    # Nothing else in the transcript is attributed: narration is one clip in
    # the DM's voice, as it always was.
    assert speech_of({"kind": "narration", "text": "The torch gutters."}) == [
        ["dm", "The torch gutters."]
    ]


def test_a_replayed_line_that_carries_its_own_prefix_is_not_read_twice():
    """History from before the speaker moved into `data` (CONTRACTS, 2026-09-04).

    `splitSpeaker` takes the name off such a line, so the narrator announces it
    exactly once and the monster says the rest — not "Goblin 2. Goblin 2: ...".
    """
    assert speech_of({"kind": "dialogue", "actor": "goblin_1",
                      "text": "Goblin 2: You die now."}) == [
        ["dm", "Goblin 2."],
        ["monster:goblin_1", "You die now."],
    ]


def test_an_unnamed_or_silent_speaker_is_not_announced():
    """No name to say, or nothing said: one clip, or none."""
    assert speech_of({"kind": "dialogue", "actor": "goblin_1", "text": "Grah!"}) == [
        ["monster:goblin_1", "Grah!"]
    ]
    assert speech_of({"kind": "dialogue", "actor": "goblin_1", "text": "   ",
                      "data": {"speaker": "Goblin Sneak"}}) == []
