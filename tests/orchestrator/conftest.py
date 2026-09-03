import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.config import GameConfig  # noqa: E402

from . import fake_engine  # noqa: E402


@pytest.fixture
def engine():
    return fake_engine


@pytest.fixture
def cfg() -> GameConfig:
    """The goblin ambush, trimmed for speed: instant tempo, tiny scene count."""
    raw = json.loads((ROOT / "examples" / "goblin_ambush.json").read_text())
    raw["tempo_ms"] = 0
    raw["mock"] = True
    raw["scenario"]["max_scenes"] = 1
    raw["scenario"]["beats_per_scene"] = 1
    raw["max_rounds_per_combat"] = 6
    return GameConfig.from_dict(raw)
