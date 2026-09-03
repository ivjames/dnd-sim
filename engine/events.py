"""Engine events — the only channel through which resolved mechanics reach
the UI and the LLM layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Event", "EventFactory", "EVENT_KINDS"]

EVENT_KINDS = {
    "combat_start",
    "round_start",
    "turn_start",
    "turn_end",
    "roll",
    "attack",
    "damage",
    "heal",
    "save",
    "condition_add",
    "condition_remove",
    "move",
    "spell_cast",
    "concentration_broken",
    "death_save",
    "down",
    "dead",
    "stable",
    "combat_end",
    "narration",
    "dialogue",
    "dm_note",
    "scene",
    "skill_check",
    "system",
    "cost",
    "error",
}


@dataclass
class Event:
    seq: int
    round: int
    kind: str
    actor: str | None
    text: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "round": self.round,
            "kind": self.kind,
            "actor": self.actor,
            "text": self.text,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            seq=int(d["seq"]),
            round=int(d.get("round", 0)),
            kind=d["kind"],
            actor=d.get("actor"),
            text=d.get("text", ""),
            data=dict(d.get("data", {})),
        )

    def __str__(self) -> str:
        return self.text


class EventFactory:
    """Allocates monotonic sequence numbers off a GameState."""

    def __init__(self, state):
        self.state = state
        self.events: list[Event] = []

    def emit(self, kind: str, text: str, actor: str | None = None, **data) -> Event:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        self.state.event_seq += 1
        ev = Event(
            seq=self.state.event_seq,
            round=self.state.round,
            kind=kind,
            actor=actor,
            text=text,
            data=data,
        )
        self.events.append(ev)
        return ev
