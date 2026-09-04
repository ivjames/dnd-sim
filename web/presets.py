"""Preset configs for the new-game panel.

Presets are read from ``examples/*.json`` in the repo root (owned by the
orchestrator builder). If that directory is empty or missing we fall back to a
built-in scenario so the UI is never dead on arrival.
"""

from __future__ import annotations

import json
import os
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def examples_dir() -> str:
    return os.environ.get("DND_SIM_EXAMPLES", os.path.join(REPO_ROOT, "examples"))


BUILTIN: list[dict[str, Any]] = [
    {
        "name": "Goblin Ambush (built-in)",
        "description": "Level 3 party of four ambushed on the Triboar trail. One combat.",
        "config": {
            "seed": 42,
            "setting": "The Sword Coast: damp pine forest along a rutted trade road at dusk.",
            "tone": "classic heroic",
            "budget_usd": 1.0,
            "tempo_ms": 800,
            "max_rounds_per_combat": 20,
            "party": [
                {
                    "id": "pc_1",
                    "name": "Thorin Barrelheart",
                    "race": "Dwarf (Hill)",
                    "klass": "Fighter",
                    "level": 3,
                    "abilities": "standard_array",
                    "equipment": "default",
                    "persona": "Gruff dwarven veteran. Speaks in short sentences. Guards the squishy ones.",
                },
                {
                    "id": "pc_2",
                    "name": "Wren Quickfingers",
                    "race": "Halfling (Lightfoot)",
                    "klass": "Rogue",
                    "level": 3,
                    "abilities": "standard_array",
                    "equipment": "default",
                    "persona": "Cheerful halfling thief. Loves flanking, hates being noticed.",
                },
                {
                    "id": "pc_3",
                    "name": "Sister Alenne",
                    "race": "Human",
                    "klass": "Cleric",
                    "level": 3,
                    "abilities": "standard_array",
                    "equipment": "default",
                    "spells": "default",
                    "pronouns": "she/her",
                    "persona": "Stern priestess of the dawn. Heals grudgingly, lectures freely.",
                },
                {
                    "id": "pc_4",
                    "name": "Mirelle Ashquill",
                    "race": "Elf (High)",
                    "klass": "Wizard",
                    "level": 3,
                    "abilities": "standard_array",
                    "equipment": "default",
                    "spells": "default",
                    "pronouns": "she/her",
                    "persona": "Precise elven scholar. Narrates her own spellcasting like a lecture.",
                },
            ],
            "scenario": {
                "opening": "The party rounds a bend in the trail and finds a toppled cart, "
                "still-warm ashes, and no bodies.",
                "max_scenes": 2,
                "encounters": [
                    {
                        "trigger": "scene_1",
                        "monsters": [{"name": "Goblin", "count": 4}],
                        "grid": {
                            "width": 12,
                            "height": 10,
                            "party_start": [[1, 4], [1, 5], [2, 4], [2, 5]],
                            "enemy_start": [[10, 3], [10, 6], [9, 2], [9, 7]],
                            "difficult": [[5, 4], [5, 5], [6, 4]],
                            "walls": [[6, 7], [6, 8], [6, 9]],
                        },
                    }
                ],
            },
        },
    }
]


def _title_for(path: str, cfg: dict[str, Any]) -> str:
    for key in ("name", "title"):
        if isinstance(cfg.get(key), str) and cfg[key].strip():
            return cfg[key].strip()
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.replace("_", " ").replace("-", " ").title()


def _description_for(cfg: dict[str, Any]) -> str:
    for key in ("description", "summary"):
        if isinstance(cfg.get(key), str) and cfg[key].strip():
            return cfg[key].strip()
    scenario = cfg.get("scenario") or {}
    opening = scenario.get("opening") if isinstance(scenario, dict) else None
    if isinstance(opening, str) and opening.strip():
        return opening.strip()[:200]
    setting = cfg.get("setting")
    return setting.strip()[:200] if isinstance(setting, str) else ""


def load_presets() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    directory = examples_dir()
    if os.path.isdir(directory):
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(directory, fname)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(cfg, dict):
                continue
            out.append(
                {
                    "name": _title_for(path, cfg),
                    "description": _description_for(cfg),
                    "file": fname,
                    "config": cfg,
                }
            )
    if not out:
        out = [dict(p) for p in BUILTIN]
    return out


def title_from_config(cfg: dict[str, Any]) -> str:
    for key in ("name", "title"):
        if isinstance(cfg.get(key), str) and cfg[key].strip():
            return cfg[key].strip()[:80]
    setting = cfg.get("setting")
    if isinstance(setting, str) and setting.strip():
        return setting.strip()[:80]
    return "Untitled game"
