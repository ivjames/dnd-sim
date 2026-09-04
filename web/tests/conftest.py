"""Fakes so the web layer can be tested without engine/llm/orchestrator.

The fakes implement exactly the slice of CONTRACTS.md 2/4 that web touches:
``EventBus.subscribe/unsubscribe/publish/history``, ``Event`` fields, and
``Game.id/status/start/pause/resume/stop/hold/release/release_all/inject_dm_note/snapshot/ledger``.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from web.app import create_app  # noqa: E402
from web.auth import ENV_VAR as WRITE_TOKEN_ENV, HEADER as WRITE_HEADER  # noqa: E402

#: The write token the fixtures configure. Every fixture client presents it, so
#: existing tests exercise the routes rather than the gate; a test that wants an
#: anonymous caller builds a plain `app.test_client()` (see test_auth.py).
WRITE_TOKEN = "test-write-token"
#: The WSGI environ key for `WRITE_HEADER`, which is how a test client carries
#: a header on every request it makes.
WRITE_ENVIRON_KEY = "HTTP_" + WRITE_HEADER.upper().replace("-", "_")


def write_client(app):
    """A test client that presents the write token on every request."""
    client = app.test_client()
    client.environ_base[WRITE_ENVIRON_KEY] = WRITE_TOKEN
    return client


@dataclass
class FakeEvent:
    seq: int
    round: int
    kind: str
    actor: str | None
    text: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class FakeBus:
    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._history: list[FakeEvent] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, ev: Any) -> None:
        with self._lock:
            if ev is not None:
                self._history.append(ev)
            subs = list(self._subs)
        for q in subs:
            q.put(ev)

    def history(self) -> list[FakeEvent]:
        with self._lock:
            return list(self._history)


class FakeGame:
    """Emits a scripted script of events: two synchronously at start (so the
    SSE test always has something to replay), the rest from a thread."""

    SCRIPT = [
        ("scene", "The cart still smoulders."),
        ("combat_start", "Combat begins."),
        ("turn_start", "Thorin's turn."),
        ("attack", "Thorin attacks Goblin 2: 1d20+5 -> 17 vs AC 15, hit"),
        ("damage", "Goblin 2 takes 9 slashing (7 -> 0)"),
        ("narration", "The dwarf's axe finds the goblin's collarbone."),
        ("combat_end", "The party stands."),
    ]

    def __init__(self, config: dict, on_event: Callable[[Any], None], bus: FakeBus) -> None:
        self.id = "g_" + uuid.uuid4().hex[:10]
        self.config = config
        self.status = "created"
        self.bus = bus
        self.on_event = on_event
        self.ledger = {"total_usd": 0.0123, "by_role": {"dm": {"calls": 1, "in": 100, "out": 50, "usd": 0.0123}}}
        self.notes: list[str] = []
        self._seq = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self.held = 0.0
        self.hold_clients: dict[str, float] = {}
        self._holds: dict[str, float] = {}
        self.step_delay = float(os.environ.get("FAKE_STEP_DELAY", "0.02"))

    # -- helpers
    def _emit(self, kind: str, text: str) -> None:
        ev = FakeEvent(seq=self._seq, round=1, kind=kind, actor="pc_1", text=text, data={})
        self._seq += 1
        self.on_event(ev)
        self.bus.publish(ev)

    # -- Game interface
    def start(self) -> None:
        self.status = "running"
        for kind, text in self.SCRIPT[:2]:
            self._emit(kind, text)

        def run() -> None:
            for kind, text in self.SCRIPT[2:]:
                if self._stop.is_set():
                    break
                while self._paused.is_set() and not self._stop.is_set():
                    time.sleep(0.01)
                time.sleep(self.step_delay)
                self._emit(kind, text)
            if self.status == "running":
                self.status = "finished"

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def run(self) -> None:  # pragma: no cover - parity with contract
        self.start()
        if self._thread:
            self._thread.join()

    def pause(self) -> None:
        self._paused.set()
        self.status = "paused"

    def resume(self) -> None:
        self._paused.clear()
        self.status = "running"

    def stop(self) -> None:
        self._stop.set()
        self._paused.clear()
        self.release_all()
        self.status = "stopped"

    def hold(self, seconds: float = 0.0, client: str = "") -> float:
        secs = max(0.0, min(float(seconds), 30.0))
        self.held = secs
        self.hold_clients[str(client or "")] = secs
        now = time.monotonic()
        if secs:
            self._holds[str(client or "")] = now + secs
        else:
            self._holds.pop(str(client or ""), None)
        return secs

    def release(self, client: str = "") -> None:
        self.hold(0.0, client)

    def release_all(self) -> None:
        self._holds.clear()

    def hold_remaining(self) -> float:
        now = time.monotonic()
        return max([d - now for d in self._holds.values()] + [0.0])

    def inject_dm_note(self, text: str) -> None:
        self.notes.append(text)
        self._emit("dm_note", text)

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "holding": self.hold_remaining() > 0,
            "round": 1,
            "summary": "The party ambushed on the trail.",
            "ledger": self.ledger,
            "state": {
                "round": 1,
                "turn_index": 0,
                "mode": "combat",
                "initiative": [["pc_1", 19], ["mon_1", 12]],
                "grid": {"width": 6, "height": 5, "difficult": [[2, 2]], "walls": [], "cover": {}},
                "combatants": {
                    "pc_1": {
                        "id": "pc_1", "name": "Thorin", "side": "party", "kind": "pc",
                        "hp": 24, "max_hp": 30, "temp_hp": 0, "ac": 18,
                        "position": [1, 2], "conditions": [], "dead": False,
                        "resources": {"spell_slots": {}, "second_wind": 1},
                        "death_saves": {"success": 0, "failure": 0},
                    },
                    "mon_1": {
                        "id": "mon_1", "name": "Goblin 2", "side": "enemy", "kind": "monster",
                        "hp": 0, "max_hp": 7, "temp_hp": 0, "ac": 15,
                        "position": [4, 2], "conditions": [{"name": "prone", "duration": None}],
                        "dead": True, "resources": {},
                        "death_saves": {"success": 0, "failure": 0},
                    },
                },
            },
        }


def fake_factory(config: dict, on_event: Callable[[Any], None]):
    bus = FakeBus()
    return FakeGame(config, on_event, bus), bus


@pytest.fixture()
def db_file(tmp_path):
    return str(tmp_path / "test.sqlite3")


class FakeTTS:
    """The slice of `tts.PollyTTS` that `web/routes/tts.py` uses.

    Records what it was asked for, so a test can tell a cache hit from a
    synthesis — which is the difference between a clip that costs money and one
    that does not.
    """

    def __init__(self, *, up: bool = True, fail: str = "", price: float = 4.0) -> None:
        self.engine = "standard"
        self.monster_engine = "standard"
        self.language = "en-US"
        self.max_chars = 40
        self.price_per_million = price
        self.up = up
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.clips: dict[str, bytes] = {}
        self._gates: dict[str, threading.Lock] = {}
        self._gate_lock = threading.Lock()

    def available(self) -> bool:
        return self.up

    def config_id(self) -> str:
        return "fake-config"

    def rate_for(self, engine: str) -> float:
        from tts.client import PRICE_USD_PER_MILLION_CHARS  # noqa: PLC0415

        if str(engine or self.engine) == self.engine:
            return self.price_per_million
        return PRICE_USD_PER_MILLION_CHARS.get(str(engine), self.price_per_million)

    def price_of(self, chars: int, engine: str = "") -> float:
        return max(0, int(chars)) * self.rate_for(engine or self.engine) / 1_000_000.0

    def engine_for(self, key: str) -> str:
        from tts.voices import is_monster_key  # noqa: PLC0415

        return self.monster_engine if is_monster_key(key) else self.engine

    def cast(self, key: str, gender: str = ""):
        from tts.voices import STANDARD_ENGLISH, cast_for  # noqa: PLC0415

        return cast_for(key, STANDARD_ENGLISH, "Brian", gender, self.engine_for(key))

    def cache_key_for(self, key: str, text: str, gender: str = ""):
        from tts.cache import cache_key  # noqa: PLC0415

        cast = self.cast(key, gender)
        return cast, cache_key(self.engine, cast.cache_key(), text)

    def cached(self, ckey: str):
        return self.clips.get(ckey)

    def voices(self, engine: str = ""):
        from tts.voices import STANDARD_ENGLISH  # noqa: PLC0415

        return STANDARD_ENGLISH if self.up else ()

    @contextmanager
    def exclusive(self, ckey: str):
        """A real gate, because the route's correctness depends on it being one.

        A no-op stub here would let identical requests through side by side and
        quietly pass tests the real service would fail.
        """
        with self._gate_lock:
            gate = self._gates.setdefault(ckey, threading.Lock())
        with gate:
            yield

    def render(self, key: str, text: str, gender: str = ""):
        return self.synthesize(key, text, gender)

    def synthesize(self, key: str, text: str, gender: str = ""):
        from tts.client import TTSError, TTSResult  # noqa: PLC0415

        self.calls.append((key, text))
        if self.fail:
            raise TTSError(self.fail)
        cast, ckey = self.cache_key_for(key, text, gender)
        if ckey in self.clips:
            return TTSResult(self.clips[ckey], cast, 0, 0.0, True, ckey)
        audio = b"\xff\xfb" + text.encode("utf-8")
        self.clips[ckey] = audio
        # At the seat's own engine's rate, as `PollyTTS.render` does.
        usd = self.price_of(len(text), cast.engine)
        return TTSResult(audio, cast, len(text), usd, False, ckey)


@pytest.fixture()
def tts():
    return FakeTTS()


@pytest.fixture()
def app(db_file):
    # Server voices off unless a test asks for them: `create_app` would
    # otherwise build a real Polly service from whatever AWS variables happen
    # to be in the environment running the suite.
    app = create_app(
        game_factory=fake_factory,
        db_path=db_file,
        config={"DND_TTS": None, WRITE_TOKEN_ENV: WRITE_TOKEN},
    )
    app.config["TESTING"] = True
    yield app
    app.config["DND_REGISTRY"].shutdown()


@pytest.fixture()
def tts_app(db_file, tts):
    app = create_app(
        game_factory=fake_factory,
        db_path=db_file,
        config={"DND_TTS": tts, WRITE_TOKEN_ENV: WRITE_TOKEN},
    )
    app.config["TESTING"] = True
    yield app
    app.config["DND_REGISTRY"].shutdown()


@pytest.fixture()
def tts_client(tts_app):
    return write_client(tts_app)


@pytest.fixture()
def client(app):
    return write_client(app)


@pytest.fixture()
def sample_config():
    return {
        "seed": 7,
        "setting": "A damp pine forest.",
        "tone": "grim",
        "budget_usd": 0.5,
        "tempo_ms": 0,
        "party": [{"id": "pc_1", "name": "Thorin", "race": "Dwarf (Hill)", "klass": "Fighter", "level": 3}],
        "scenario": {"opening": "Ambush.", "encounters": [], "max_scenes": 1},
    }
