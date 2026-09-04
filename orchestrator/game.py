"""The game loop: scenes → encounters → turns → summaries → epilogue.

CONTRACTS.md §4. The engine is injected (see Amendments) so this loop can be
exercised without the engine package present.
"""

from __future__ import annotations

import re
import threading
import time
import traceback
import uuid
from types import SimpleNamespace
from typing import Any, Callable

from agents.common import AgentOutputError
from agents.dm import DMAgent
from agents.player import PlayerAgent
from agents.summarizer import summarize
from agents.views import dm_view, party_summary, player_view

from llm.client import LLMClient
from llm.cost import Ledger

from .bus import EventBus
from .config import GameConfig

__all__ = ["Game", "default_engine"]

SUMMARY_EVERY = 15  # events
MAX_ACTIONS_PER_TURN = 8
DIALOGUE_MEMORY = 8  # spoken lines kept for the repetition guard
SELF_REPEAT = 0.5  # word overlap at which a speaker is repeating itself
ECHO_REPEAT = 0.7  # ... at which one character is parroting another
FUZZY_MIN_WORDS = 4  # below this, only identical content counts as a repeat
MAX_TURNS_PER_COMBAT = 400
DEFAULT_BEATS_PER_SCENE = 2
RECENT_EVENTS = 12


class _Stopped(Exception):
    """Internal: cooperative stop requested."""


class _BudgetExceeded(Exception):
    """Internal: USD budget spent."""


def default_engine() -> Any:
    """Flatten the engine package into one namespace of §1 names.

    Amendment: `Game(engine=...)` takes a single module-like object exposing
    the engine's public names (RNG, GameState, Grid, Combatant, Event,
    build_character, monster_to_combatant, legal_actions, apply, start_combat,
    advance_turn, combat_over, skill_check, IllegalAction). This adapter builds
    that namespace from the real package.
    """
    ns = SimpleNamespace()
    for modname in ("dice", "state", "events", "characters", "actions", "srd"):
        try:
            mod = __import__(f"engine.{modname}", fromlist=["*"])
        except Exception:  # noqa: BLE001 - partially built engine is tolerated
            continue
        for name in dir(mod):
            if not name.startswith("_"):
                setattr(ns, name, getattr(mod, name))
    if not hasattr(ns, "apply"):
        raise ImportError(
            "engine package is not importable; pass engine=... or install it"
        )
    return ns


class Game:
    """One simulated game. Runs on its own thread or blocking via `run()`."""

    def __init__(
        self,
        cfg: GameConfig,
        client: LLMClient,
        bus: EventBus,
        on_event: Callable[[Any], None] | None = None,
        engine: Any = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.bus = bus
        self.on_event = on_event
        self.engine = engine if engine is not None else default_engine()
        self.id = uuid.uuid4().hex[:12]
        self.status = "created"
        self.ledger = Ledger()
        self.summary = ""
        self.state: Any = None
        self.error: str | None = None
        self.outcome: str = "unresolved"

        self._seq = 0
        self._events_since_summary = 0
        self._unsummarized: list[Any] = []
        self._lock = threading.Lock()
        self._resume = threading.Event()
        self._resume.set()
        self._stop = threading.Event()
        self._notes: list[str] = []
        self._spoken: list[tuple[str, frozenset, bool]] = []  # guard: actor, words, negates
        self._thread: threading.Thread | None = None
        self.dm: DMAgent | None = None
        self.players: dict[str, PlayerAgent] = {}
        self.seat_models: dict[str, str] = {}  # combatant id -> model serving it

    # ------------------------------------------------------------------
    # controls
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name=f"game-{self.id}", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if self.status == "running":
            self.status = "paused"
        self._resume.clear()

    def resume(self) -> None:
        if self.status == "paused":
            self.status = "running"
        self._resume.set()

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()  # release a paused loop so it can notice the stop

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def inject_dm_note(self, text: str) -> None:
        text = " ".join(str(text or "").split())
        if not text:
            return
        with self._lock:
            self._notes.append(text)
        self._publish(self._event("dm_note", f"DM NOTE FROM TABLE: {text}", data={"text": text}))

    def _pop_notes(self) -> str | None:
        with self._lock:
            if not self._notes:
                return None
            notes = self._notes
            self._notes = []
        return " | ".join(notes)

    def _gate(self) -> None:
        """Honour pause/stop. Called between events and before LLM calls."""
        if self._stop.is_set():
            raise _Stopped()
        if not self._resume.is_set():
            self._resume.wait()
        if self._stop.is_set():
            raise _Stopped()

    def _check_budget(self) -> None:
        if self.ledger.total_usd >= self.cfg.budget_usd:
            raise _BudgetExceeded()

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    def _event(self, kind: str, text: str, actor: str | None = None, data: dict | None = None) -> Any:
        try:
            cls = getattr(self.engine, "Event", None)
        except Exception:  # noqa: BLE001 - a broken engine must still be reportable
            cls = None
        rnd = getattr(self.state, "round", 0) if self.state is not None else 0
        with self._lock:
            self._seq += 1
            seq = self._seq
        payload = {
            "seq": seq,
            "round": rnd,
            "kind": kind,
            "actor": actor,
            "text": text,
            "data": data or {},
        }
        if cls is None:
            return SimpleNamespace(**payload)
        try:
            return cls(**payload)
        except TypeError:  # pragma: no cover - engine dataclass drift
            return SimpleNamespace(**payload)

    def _publish(self, ev: Any) -> None:
        self.bus.publish(ev)
        if self.on_event is not None:
            try:
                self.on_event(ev)
            except Exception:  # noqa: BLE001 - a bad sink must not kill the game
                pass

    def _emit(self, ev: Any) -> None:
        """Renumber, publish, pace, and feed the summarizer."""
        with self._lock:
            self._seq += 1
            seq = self._seq
        try:
            ev.seq = seq
        except Exception:  # noqa: BLE001 - frozen dataclass; leave its own seq
            pass
        self._publish(ev)
        self._unsummarized.append(ev)
        self._events_since_summary += 1
        if self.cfg.tempo_ms:
            time.sleep(self.cfg.tempo_ms / 1000.0)
        self._gate()

    def _emit_new(self, kind: str, text: str, actor: str | None = None, data: dict | None = None) -> None:
        ev = self._event(kind, text, actor, data)
        # _event already claimed a seq; _emit claims another, so reuse this one.
        self._publish(ev)
        self._unsummarized.append(ev)
        self._events_since_summary += 1
        if self.cfg.tempo_ms:
            time.sleep(self.cfg.tempo_ms / 1000.0)
        self._gate()

    def _say(self, actor_id: str, name: str, speech: str | None) -> bool:
        """Emit one line of dialogue unless it repeats what was just said.

        Characters left to themselves restate their last line every time they
        are asked, so a line close enough to a recent one — the same speaker
        rephrasing itself, or another character parroting it back — is dropped
        rather than printed — unless it contradicts that line, which is a new
        contribution however alike the wording. Returns whether it was emitted.

        Overlap is only trusted on lines of `FUZZY_MIN_WORDS` content words or
        more. Below that a single word is most of the line, so "heal me" and
        "heal him" or "I go left" and "I go right" score as high as a genuine
        repeat does; short lines are therefore suppressed only when their
        content is identical, which is what a repeated bark actually is.
        """
        text = " ".join(str(speech or "").split())
        if not text:
            return False
        words, negated = _line_key(text)
        if not words:  # nothing to compare (punctuation, an emoji); let it through
            self._emit_new("dialogue", text, actor=actor_id, data={"speaker": name})
            return True
        for prev_actor, prev_words, prev_negated in self._spoken:
            if negated != prev_negated:
                continue  # one of the two contradicts the other; not a repeat
            if words == prev_words:
                return False  # the same thing said again, at any length
            if min(len(words), len(prev_words)) < FUZZY_MIN_WORDS:
                continue  # too short to judge by overlap: see _say's docstring
            limit = SELF_REPEAT if prev_actor == actor_id else ECHO_REPEAT
            if _overlap(words, prev_words) >= limit:
                return False
        self._spoken.append((actor_id, words, negated))
        del self._spoken[:-DIALOGUE_MEMORY]
        self._emit_new("dialogue", text, actor=actor_id, data={"speaker": name})
        return True

    def _emit_all(self, events: list) -> None:
        for ev in events or []:
            self._emit(ev)

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    def _setup(self) -> None:
        eng = self.engine
        self.rng = eng.RNG(self.cfg.seed)
        combatants: dict[str, Any] = {}
        starts = self._party_starts()
        for i, spec in enumerate(self.cfg.party):
            sheet = eng.build_character(dict(spec), self.rng)
            pos = starts[i] if i < len(starts) else (1, 1 + i)
            combatants[sheet.id] = self._pc_combatant(sheet, tuple(pos))
            self.seat_models[sheet.id] = self.cfg.player_model_for(spec)
        scene = {
            "title": self.cfg.title,
            "description": self.cfg.opening,
            "objectives": (self.cfg.scenario or {}).get("objectives", []),
            "location": (self.cfg.scenario or {}).get("location", ""),
        }
        self.state = self._new_state(combatants, scene, self._grid(None))

        self.dm = DMAgent(
            self.client,
            self.cfg.dm_model,
            self.ledger,
            self.cfg.setting,
            self.cfg.tone,
            engine=self.engine,
        )
        for cid, c in combatants.items():
            self.players[cid] = PlayerAgent(
                self.client,
                self.seat_models.get(cid, self.cfg.player_model),
                c.sheet,
                self.ledger,
                engine=self.engine,
            )

    def _party_starts(self) -> list[tuple[int, int]]:
        for enc in self.cfg.encounters():
            grid = enc.get("grid") or {}
            if grid.get("party_start"):
                return [tuple(p) for p in grid["party_start"]]
        return [(1, 2 + i) for i in range(len(self.cfg.party))]

    def _pc_combatant(self, sheet: Any, position: tuple[int, int]) -> Any:
        eng = self.engine
        for name in ("pc_to_combatant", "sheet_to_combatant", "combatant_from_sheet"):
            fn = getattr(eng, name, None)
            if fn is not None:
                c = fn(sheet, position=position) if _accepts(fn, "position") else fn(sheet)
                try:
                    c.position = tuple(position)
                except Exception:  # noqa: BLE001
                    pass
                return c
        # Contract fallback: build the dataclass from the sheet directly.
        cls = eng.Combatant
        resources = {"spell_slots": dict(getattr(sheet, "spell_slots", {}) or {})}
        starting = getattr(eng, "starting_resources", None)
        if starting is not None:
            try:
                resources = starting(sheet)
            except Exception:  # noqa: BLE001 - fall back to bare slots
                pass
        return _construct(
            cls,
            id=sheet.id,
            name=sheet.name,
            side="party",
            kind="pc",
            sheet=sheet,
            stat_block=None,
            hp=sheet.max_hp,
            max_hp=sheet.max_hp,
            temp_hp=0,
            ac=sheet.ac,
            speed=sheet.speed,
            abilities=dict(sheet.abilities),
            save_profs=list(sheet.saves),
            skill_profs=list(sheet.skills),
            proficiency=sheet.proficiency,
            position=tuple(position),
            size="M",
            conditions=[],
            concentration=None,
            death_saves={"success": 0, "failure": 0},
            stable=False,
            dead=False,
            resources=resources,
            turn={
                "action": False,
                "bonus": False,
                "reaction": False,
                "movement_left": sheet.speed,
                "attacks_left": 1,
                "free_object": False,
            },
            inventory=list(getattr(sheet, "weapons", []) or []),
        )

    def _grid(self, spec: dict | None) -> Any:
        eng = self.engine
        spec = spec or {}
        return _construct(
            eng.Grid,
            width=int(spec.get("width", 12)),
            height=int(spec.get("height", 10)),
            difficult={tuple(p) for p in spec.get("difficult", [])},
            walls={tuple(p) for p in spec.get("walls", [])},
            cover={tuple(k): v for k, v in (spec.get("cover") or {}).items()}
            if isinstance(spec.get("cover"), dict)
            else {},
        )

    def _new_state(self, combatants: dict, scene: dict, grid: Any) -> Any:
        eng = self.engine
        if hasattr(eng, "new_state"):
            return eng.new_state(
                seed=self.cfg.seed, combatants=combatants, scene=scene, grid=grid, rng=self.rng
            )
        return _construct(
            eng.GameState,
            seed=self.cfg.seed,
            rng=self.rng.state(),
            mode="exploration",
            round=0,
            turn_index=0,
            combatants=combatants,
            initiative=[],
            grid=grid,
            scene=scene,
            event_seq=0,
        )

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def run(self) -> None:
        self.status = "running"
        try:
            self._setup()
            self._emit_new(
                "system",
                f"The table is set — seed {self.cfg.seed}"
                + (" (mock LLM)" if self.cfg.mock else ""),
                data={"game_id": self.id, "seed": self.cfg.seed, "mock": self.cfg.mock},
            )
            self._run_scenes()
            self._finish_epilogue()
            self.status = "finished"
        except _Stopped:
            self.status = "stopped"
            self._safe_emit("system", "Game stopped by the table.")
        except _BudgetExceeded:
            self.status = "budget_exceeded"
            self._safe_emit(
                "cost",
                f"Budget of ${self.cfg.budget_usd:.2f} exhausted "
                f"(${self.ledger.total_usd:.4f} spent) — stopping.",
                data=self.ledger.to_dict(),
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure as an event
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc()[-1500:]
            self._safe_emit("error", self.error, data={"traceback": tb})
        finally:
            self._safe_emit(
                "cost",
                f"Total spend ${self.ledger.total_usd:.4f} over "
                f"{sum(v['calls'] for v in self.ledger.by_role.values())} model calls.",
                data=self.ledger.to_dict(),
            )
            self.bus.close()

    def _safe_emit(self, kind: str, text: str, data: dict | None = None) -> None:
        """Emit without pacing or gating — used on the way out."""
        try:
            self._publish(self._event(kind, text, data=data))
        except Exception:  # noqa: BLE001
            pass

    # -- scene loop --------------------------------------------------------

    def _run_scenes(self) -> None:
        scenes = (self.cfg.scenario or {}).get("scenes") or []
        for i in range(1, self.cfg.max_scenes + 1):
            key = f"scene_{i}"
            spec = scenes[i - 1] if i - 1 < len(scenes) else {}
            self.state.scene = {
                "title": spec.get("title") or self.cfg.title,
                "description": spec.get("description") or (self.cfg.opening if i == 1 else ""),
                "objectives": spec.get("objectives", []) or (self.cfg.scenario or {}).get("objectives", []),
                "location": spec.get("location", ""),
            }
            self._emit_new(
                "scene",
                f"Scene {i}: {self.state.scene['title']}",
                data={"scene": self.state.scene, "index": i},
            )
            self._gate()
            self._check_budget()
            narration = self._dm_call(
                self.dm.open_scene, self.state.scene, party_summary(self.state)
            )
            if narration:
                self._emit_new("narration", narration, actor="dm")

            self._exploration(key)
            if self._party_down():
                break
            if self._stop.is_set():
                raise _Stopped()

    def _exploration(self, scene_key: str) -> None:
        beats = int((self.cfg.scenario or {}).get("beats_per_scene", DEFAULT_BEATS_PER_SCENE))
        for _ in range(max(0, beats)):
            self._gate()
            self._check_budget()
            view = dm_view(self.state, self._recent(), self.summary)
            options = self._dm_call(self.dm.scene_options, view)
            self._emit_new(
                "system", "The party considers: " + "; ".join(options), data={"options": options}
            )
            votes: dict[int, int] = {}
            said: list[str] = []
            for cid, agent in self.players.items():
                if not self._conscious(cid):
                    continue
                self._gate()
                self._check_budget()
                pview = player_view(self.state, cid, self._recent(), self.summary)
                try:
                    reply = agent.choose_scene_action(pview, options, said)
                except AgentOutputError as exc:
                    self._emit_new("error", f"{agent.name} said nothing useful: {exc}", actor=cid)
                    continue
                votes[reply["choice"]] = votes.get(reply["choice"], 0) + 1
                if self._say(cid, agent.name, reply.get("speech")):
                    said.append(f"{agent.name}: {reply['speech']}")
            if not options:
                return
            choice = max(votes.items(), key=lambda kv: (kv[1], -kv[0]))[0] if votes else 0
            request = options[min(choice, len(options) - 1)]

            self._gate()
            self._check_budget()
            view = dm_view(self.state, self._recent(), self.summary)
            ruling = self._dm_call(self.dm.adjudicate, view, request)
            if ruling.get("narration"):
                self._emit_new("narration", ruling["narration"], actor="dm")

            if ruling["resolution"] == "skill_check":
                self._resolve_skill_check(ruling)
            elif ruling["resolution"] == "start_combat":
                self._run_combat(ruling.get("encounter") or {}, None)
                return
            self._maybe_summarize()

        encounter = self.cfg.encounter_for(scene_key)
        if encounter:
            self._run_combat(encounter, encounter.get("grid"))

    def _resolve_skill_check(self, ruling: dict) -> None:
        actor = ruling.get("actor")
        if actor not in self.state.combatants:
            candidates = [cid for cid in self.players if self._conscious(cid)]
            actor = candidates[0] if candidates else None
        if actor is None:
            return
        try:
            self.state, events = self.engine.skill_check(
                self.state, actor, ruling.get("skill") or "Perception", int(ruling.get("dc") or 10)
            )
        except Exception as exc:  # noqa: BLE001 - engine refused; narrate instead
            self._emit_new("error", f"skill check failed: {exc}", actor=actor)
            return
        self._emit_all(events)

    # -- combat ------------------------------------------------------------

    def _run_combat(self, encounter: dict, grid_spec: dict | None) -> None:
        eng = self.engine
        grid_spec = grid_spec or encounter.get("grid") or {}
        if grid_spec:
            self.state.grid = self._grid(grid_spec)
        self._place_party(grid_spec)
        self._spawn_monsters(encounter, grid_spec)
        if not any(c.side == "enemy" and not c.dead for c in self.state.combatants.values()):
            return

        self.state.mode = "combat"
        self.state, events = eng.start_combat(self.state, self.rng.state())
        self._emit_all(events)

        turns = 0
        while turns < MAX_TURNS_PER_COMBAT:
            turns += 1
            self._gate()
            if eng.combat_over(self.state) is not None:
                break
            if self.state.round > self.cfg.max_rounds_per_combat:
                self._emit_new(
                    "system",
                    f"Combat hits the {self.cfg.max_rounds_per_combat}-round cap; the fight breaks off.",
                )
                break
            actor_id = self.state.active_id()
            if actor_id is None:
                self.state, events = eng.advance_turn(self.state)
                self._emit_all(events)
                continue
            turn_events = self._run_turn(actor_id)
            if turn_events:
                self._narrate(turn_events)
            self.state, events = eng.advance_turn(self.state)
            self._emit_all(events)
            self._maybe_summarize()

        winner = eng.combat_over(self.state)
        self.outcome = {
            "party": "the party won the fight",
            "enemy": "the party was defeated",
        }.get(winner or "", "the fight ended inconclusively")
        self._emit_new(
            "combat_end",
            f"Combat ends: {self.outcome}.",
            data={"winner": winner},
        )
        self.state.mode = "exploration"

    def _place_party(self, grid_spec: dict | None) -> None:
        starts = [tuple(p) for p in (grid_spec or {}).get("party_start", [])]
        if not starts:
            return
        for i, cid in enumerate(list(self.players)):
            if i < len(starts) and cid in self.state.combatants:
                self.state.combatants[cid].position = starts[i]

    def _spawn_monsters(self, encounter: dict, grid_spec: dict | None) -> None:
        eng = self.engine
        spots = [tuple(p) for p in (grid_spec or {}).get("enemy_start", [])]
        idx = 0
        n_existing = sum(1 for c in self.state.combatants.values() if c.side == "enemy")
        for entry in encounter.get("monsters", []) or []:
            name = entry.get("name")
            if not name:
                continue
            count = int(entry.get("count", 1))
            for ordinal in range(1, count + 1):
                idx += 1
                cid = f"mon_{n_existing + idx}"
                try:
                    mon = eng.monster_to_combatant(name, cid, self.rng)
                except Exception as exc:  # noqa: BLE001 - unknown monster name
                    self._emit_new("error", f"cannot spawn '{name}': {exc}")
                    continue
                if idx - 1 < len(spots):
                    mon.position = spots[idx - 1]
                if count > 1 and getattr(mon, "name", None):
                    mon.name = f"{mon.name} {ordinal}"
                self.state.combatants[cid] = mon
        if idx:
            names = ", ".join(
                c.name for c in self.state.combatants.values() if c.side == "enemy" and not c.dead
            )
            self._emit_new("system", f"Enemies appear: {names}", data={"encounter": encounter})

    def _run_turn(self, actor_id: str) -> list:
        """Let one combatant act until it ends its turn. Returns its events."""
        eng = self.engine
        collected: list = []
        spoke = False
        for _ in range(MAX_ACTIONS_PER_TURN):
            self._gate()
            self._check_budget()
            actor = self.state.combatants.get(actor_id)
            if actor is None or actor.dead or actor.hp <= 0:
                break
            try:
                templates = eng.legal_actions(self.state, actor_id)
            except Exception as exc:  # noqa: BLE001
                self._emit_new("error", f"legal_actions failed for {actor_id}: {exc}")
                break
            if not templates:
                break
            action = self._choose(actor_id, actor, templates, speak=not spoke)
            if action is None:
                break
            if self._say(actor_id, actor.name, getattr(action, "speech", None)):
                spoke = True
            try:
                self.state, events = eng.apply(self.state, action)
            except Exception as exc:  # noqa: BLE001 - IllegalAction and friends
                self._emit_new(
                    "error", f"{actor.name}'s action was rejected: {exc}", actor=actor_id
                )
                action = self._end_turn_action(actor_id, templates)
                if action is None:
                    break
                try:
                    self.state, events = eng.apply(self.state, action)
                except Exception as exc2:  # noqa: BLE001
                    self._emit_new("error", f"end_turn failed: {exc2}", actor=actor_id)
                    break
            self._emit_all(events)
            collected.extend(events)
            if self._is_end_turn(templates, action):
                break
        return collected

    def _choose(self, actor_id: str, actor: Any, templates: list, *, speak: bool = True) -> Any:
        """Ask the right agent; on agent failure fall back to end_turn."""
        try:
            if getattr(actor, "kind", "pc") == "pc" and actor_id in self.players:
                agent = self.players[actor_id]
                view = player_view(self.state, actor_id, self._recent(), self.summary)
                return agent.choose_action(view, templates, speak=speak)
            view = dm_view(self.state, self._recent(), self.summary)
            self.dm.pending_note = self._pop_notes()
            return self.dm.monster_action(
                view, templates, actor_id, getattr(actor, "name", None), speak=speak
            )
        except AgentOutputError as exc:
            self._emit_new("error", f"{actor.name} hesitated ({exc}); ending turn.", actor=actor_id)
            return self._end_turn_action(actor_id, templates)

    def _end_turn_action(self, actor_id: str, templates: list) -> Any:
        for t in templates:
            if getattr(t, "type", None) == "end_turn":
                cls = getattr(self.engine, "Action", None)
                if cls is None:  # pragma: no cover
                    from agents.common import action_class

                    cls = action_class(self.engine)
                return cls(actor=actor_id, template_id=t.id, params={}, speech=None)
        return None

    @staticmethod
    def _is_end_turn(templates: list, action: Any) -> bool:
        for t in templates:
            if getattr(t, "id", None) == getattr(action, "template_id", None):
                return getattr(t, "type", "") == "end_turn"
        return True

    def _narrate(self, events: list) -> None:
        if not events:
            return
        if not any(getattr(e, "kind", "") in ("attack", "damage", "spell_cast", "heal", "move", "save", "down", "dead") for e in events):
            return
        self._gate()
        self._check_budget()
        view = dm_view(self.state, events, self.summary)
        text = self._dm_call(self.dm.narrate, view, events)
        if text:
            self._emit_new("narration", text, actor="dm")

    # -- memory ------------------------------------------------------------

    def _recent(self, n: int = RECENT_EVENTS) -> list:
        return self.bus.history()[-n:]

    def _maybe_summarize(self) -> None:
        if self._events_since_summary < SUMMARY_EVERY:
            return
        events = self._unsummarized
        self._unsummarized = []
        self._events_since_summary = 0
        self._gate()
        self._check_budget()
        try:
            self.summary = summarize(
                self.client, self.cfg.summary_model, self.ledger, self.summary, events
            )
        except Exception as exc:  # noqa: BLE001 - a summary is never worth dying for
            self._emit_new("error", f"summary failed: {exc}")

    # -- helpers -----------------------------------------------------------

    def _dm_call(self, fn: Callable, *args: Any) -> Any:
        self.dm.pending_note = self._pop_notes()
        return fn(*args)

    def _conscious(self, cid: str) -> bool:
        c = self.state.combatants.get(cid)
        return c is not None and not c.dead and c.hp > 0

    def _party_down(self) -> bool:
        return not any(self._conscious(cid) for cid in self.players)

    def _finish_epilogue(self) -> None:
        if self._party_down():
            self.outcome = "the party fell"
        self._gate()
        try:
            self._check_budget()
        except _BudgetExceeded:
            return
        view = dm_view(self.state, self._recent(), self.summary)
        text = self._dm_call(self.dm.epilogue, view, self.outcome)
        if text:
            self._emit_new("narration", text, actor="dm")

    def snapshot(self) -> dict:
        state = None
        if self.state is not None:
            try:
                state = self.state.to_dict()
            except Exception:  # noqa: BLE001 - snapshot must never raise
                state = None
        return {
            "id": self.id,
            "state": state,
            "summary": self.summary,
            "ledger": self.ledger.to_dict(),
            "status": self.status,
            "round": getattr(self.state, "round", 0) if self.state is not None else 0,
            "outcome": self.outcome,
            "error": self.error,
            "models": {
                "dm": self.cfg.dm_model,
                "summary": self.cfg.summary_model,
                "players": dict(self.seat_models),
            },
        }


# Apostrophes are stripped before this is consulted, so the contracted forms
# appear here as "dont", "cant" and so on. "cannot" is the one that has to be
# spelled out separately: it is a single word, not a contraction.
_NEGATIONS = frozenset(
    {
        "no", "not", "nor", "never", "none", "neither", "nothing", "nobody",
        "nowhere", "cannot", "cant", "dont", "doesnt", "didnt", "wont",
        "wouldnt", "shouldnt", "couldnt", "isnt", "arent", "wasnt", "werent",
        "havent", "hasnt", "hadnt", "mustnt", "shant", "neednt", "aint",
    }
)


# Function words: they carry the grammar rather than the point, and two lines
# that differ only in these are the same line. Everything else is content —
# pronouns and short nouns included, since "heal me" and "heal him" differ by
# nothing else. Negations are never listed here.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "so", "yet",
        "of", "to", "in", "into", "on", "onto", "upon", "at", "by", "for",
        "from", "with", "as",
        "is", "are", "was", "were", "be", "been", "being", "am",
        "do", "does", "did", "has", "have", "had",
        "will", "shall", "would", "should", "can", "could", "may", "might",
        "must",
    }
)


def _line_key(text: str) -> tuple[frozenset, bool]:
    """What a repetition is judged on: content words, and whether the line negates.

    Function words are dropped, so "Sir Zombie, your desecration ends here" and
    "Sir Zombie, your desecration ends NOW" still read as one line. Length is
    deliberately not the test: "heal me" and "heal him", or "attack the goblin"
    and "attack the orc", differ only in a word of two or three letters and are
    different instructions. Negation is handled separately, by flag rather than
    by word, because "open the door" and "do not open the door" overlap too far
    to be told apart by words at all. Apostrophes go first, so "don't" reduces
    to "dont" rather than to two fragments. If a line is nothing but function
    words, fall back to all of them so it still compares equal to itself.

    Words are Unicode: an ASCII-only pattern reduces every line of a Cyrillic
    or CJK game to the empty set, which then compares equal to every other
    such line. A line that yields no words at all (pure punctuation) is handled
    by `_say`, which never suppresses one.
    """
    flat = text.lower().replace("'", "").replace("\u2019", "")
    words = re.findall(r"\w+", flat, re.UNICODE)
    negated = any(w in _NEGATIONS for w in words)
    return frozenset([w for w in words if w not in _STOPWORDS] or words), negated


def _overlap(a: frozenset, b: frozenset) -> float:
    """Jaccard similarity of two word sets; 0.0 if either is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _construct(cls: Any, **kwargs: Any) -> Any:
    """Instantiate a dataclass, dropping keys it does not declare.

    Keeps the orchestrator working if the engine's dataclasses grow or shrink
    fields relative to the contract.
    """
    try:
        import dataclasses

        names = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in kwargs.items() if k in names}
    except TypeError:  # pragma: no cover - not a dataclass
        pass
    return cls(**kwargs)


def _accepts(fn: Callable, name: str) -> bool:
    try:
        import inspect

        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False
