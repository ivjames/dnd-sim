"""GameConfig. CONTRACTS.md §4."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from llm.client import DM_MODEL, PLAYER_MODEL, SUMMARY_MODEL

__all__ = ["GameConfig"]


@dataclass
class GameConfig:
    seed: int = 0
    setting: str = "A weathered frontier of the Sword Coast"
    tone: str = "classic heroic"
    party: list[dict] = field(default_factory=list)
    scenario: dict = field(default_factory=dict)
    dm_model: str = DM_MODEL
    player_model: str = PLAYER_MODEL
    summary_model: str = SUMMARY_MODEL
    max_rounds_per_combat: int = 20
    budget_usd: float = 1.00
    tempo_ms: int = 800
    mock: bool = False
    title: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "GameConfig":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in (d or {}).items() if k in known}
        cfg = cls(**kwargs)
        cfg.seed = int(cfg.seed)
        cfg.budget_usd = float(cfg.budget_usd)
        cfg.tempo_ms = int(cfg.tempo_ms)
        cfg.max_rounds_per_combat = int(cfg.max_rounds_per_combat)
        cfg.mock = bool(cfg.mock)
        if not cfg.title:
            cfg.title = (cfg.scenario or {}).get("title", "") or "Untitled adventure"
        return cfg

    @classmethod
    def load(cls, path: str | Path) -> "GameConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict:
        return asdict(self)

    # -- convenience -------------------------------------------------------

    @property
    def max_scenes(self) -> int:
        return int((self.scenario or {}).get("max_scenes", 1) or 1)

    @property
    def opening(self) -> str:
        return (self.scenario or {}).get("opening", "") or ""

    def encounters(self) -> list[dict]:
        return list((self.scenario or {}).get("encounters", []) or [])

    def encounter_for(self, trigger: str) -> dict | None:
        for enc in self.encounters():
            if enc.get("trigger") == trigger:
                return enc
        return None
