"""DMAgent — the Sonnet dungeon master. CONTRACTS.md §3."""

from __future__ import annotations

import json
from typing import Any

from llm.client import LLMClient
from llm.cost import Ledger

from .common import (
    AgentOutputError,
    action_class,
    clamp_words,
    event_text,
    extract_json,
    render,
    rules_digest,
    speech_fields,
)
from .views import render_actions

__all__ = ["DMAgent"]

MAX_TOKENS_SCENE = 600
MAX_TOKENS_NARRATE = 300
MAX_TOKENS_ACTION = 220
MAX_TOKENS_ADJUDICATE = 400
MAX_TOKENS_OPTIONS = 200

NARRATION_WORDS = 120
SCENE_WORDS = 150
SPEECH_WORDS = 20

_EVENT_KINDS_FOR_NARRATION = {
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
    "combat_start",
    "combat_end",
    "skill_check",
    "dialogue",
}


def _event_lines(events: list) -> str:
    lines = []
    for ev in events or []:
        kind = getattr(ev, "kind", "") or (ev.get("kind") if isinstance(ev, dict) else "")
        text = event_text(ev)
        if not text or kind not in _EVENT_KINDS_FOR_NARRATION:
            continue
        lines.append(f"- {text}")
    return "\n".join(lines[-20:]) or "- (nothing mechanical happened)"


class DMAgent:
    def __init__(
        self,
        client: LLMClient,
        model: str,
        ledger: Ledger,
        setting: str,
        tone: str,
        *,
        engine: Any = None,
    ) -> None:
        self.client = client
        self.model = model
        self.ledger = ledger
        self.setting = setting
        self.tone = tone
        self.engine = engine
        self.role = "dm"
        self._system = render(
            "dm_system.txt",
            setting=setting,
            tone=tone,
            rules_digest=rules_digest(),
        )
        # Set by the orchestrator; consumed by the very next call.
        self.pending_note: str | None = None

    @property
    def system_blocks(self) -> list[dict]:
        return [
            {
                "type": "text",
                "text": self._system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    # -- note plumbing -----------------------------------------------------

    def take_note(self) -> str:
        """Consume a pending table note, formatted for injection into a prompt."""
        note = self.pending_note
        self.pending_note = None
        if not note:
            return ""
        return f"\nDM NOTE FROM TABLE: {note}\nHonour this note in what you write next.\n"

    def _call(
        self,
        user: str,
        max_tokens: int,
        temperature: float = 0.8,
        json_only: bool = True,
    ) -> str:
        resp = self.client.complete(
            model=self.model,
            system=self.system_blocks,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=temperature,
            json_only=json_only,
        )
        self.ledger.add(self.role, resp)
        return resp.text

    @staticmethod
    def _prose(text: str, key: str, limit: int) -> str:
        try:
            obj = extract_json(text)
        except AgentOutputError:
            return clamp_words(text, limit) or ""
        val = obj.get(key) or obj.get("text") or obj.get("narration") or ""
        return clamp_words(val, limit) or ""

    # -- narration ---------------------------------------------------------

    def open_scene(self, scene: dict, party_summary: str) -> str:
        user = render(
            "dm_open_scene.txt",
            scene=json.dumps(scene, ensure_ascii=False)[:1200],
            party_summary=party_summary[:1200],
            dm_note=self.take_note(),
        )
        return self._prose(self._call(user, MAX_TOKENS_SCENE), "narration", SCENE_WORDS)

    def narrate(self, view: str, events: list) -> str:
        user = render(
            "dm_narrate.txt",
            view=view,
            events=_event_lines(events),
            dm_note=self.take_note(),
        )
        return self._prose(
            self._call(user, MAX_TOKENS_NARRATE), "narration", NARRATION_WORDS
        )

    def epilogue(self, view: str, outcome: str) -> str:
        user = render(
            "dm_epilogue.txt", view=view, outcome=outcome, dm_note=self.take_note()
        )
        return self._prose(self._call(user, MAX_TOKENS_SCENE), "narration", SCENE_WORDS)

    # -- monsters ----------------------------------------------------------

    def monster_action(
        self,
        view: str,
        templates: list,
        monster_id: str,
        monster_name: str | None = None,
        *,
        speak: bool = True,
    ) -> Any:
        if not templates:
            raise AgentOutputError("no legal actions offered")
        by_id = {getattr(t, "id", None) or t.get("id"): t for t in templates}
        name = monster_name or monster_id
        user = render(
            "dm_monster_action.txt",
            view=view,
            monster_id=monster_id,
            monster_name=name,
            actions=render_actions(templates),
            **speech_fields(speak, SPEECH_WORDS),
        )
        error: str | None = None
        for attempt in range(2):
            prompt = (
                user
                if error is None
                else f"{user}\n\nYOUR LAST REPLY WAS REJECTED: {error}\nTry again. JSON only."
            )
            text = self._call(prompt, MAX_TOKENS_ACTION)
            try:
                return self._parse_action(text, by_id, monster_id, speak=speak)
            except AgentOutputError as exc:
                error = str(exc)
                if attempt == 1:
                    raise AgentOutputError(
                        f"DM failed to act for {monster_id}: {error}"
                    ) from exc
        raise AgentOutputError("unreachable")  # pragma: no cover

    def _parse_action(self, text: str, by_id: dict, actor: str, *, speak: bool = True) -> Any:
        obj = extract_json(text)
        aid = obj.get("action") or obj.get("action_id") or obj.get("id")
        if not isinstance(aid, str):
            raise AgentOutputError("missing string field 'action'")
        aid = aid.strip().strip("[]")
        if aid not in by_id:
            raise AgentOutputError(
                f"'{aid}' is not one of the offered ids: {', '.join(by_id)}"
            )
        params = obj.get("params") or {}
        if not isinstance(params, dict):
            raise AgentOutputError("'params' must be a JSON object")
        template = by_id[aid]
        needs = getattr(template, "needs", None)
        if needs is None and isinstance(template, dict):
            needs = template.get("needs")
        for key in needs or []:
            if key not in params or params[key] in (None, ""):
                raise AgentOutputError(f"action {aid} needs a '{key}' value in params")
        cls = action_class(self.engine)
        return cls(
            actor=actor,
            template_id=aid,
            params=params,
            speech=clamp_words(obj.get("speech"), SPEECH_WORDS) if speak else None,
        )

    # -- non-combat --------------------------------------------------------

    def adjudicate(self, view: str, request: str) -> dict:
        user = render(
            "dm_adjudicate.txt",
            view=view,
            request=request,
            dm_note=self.take_note(),
        )
        text = self._call(user, MAX_TOKENS_ADJUDICATE)
        try:
            obj = extract_json(text)
        except AgentOutputError:
            obj = {}
        resolution = obj.get("resolution")
        if resolution not in ("skill_check", "narrative", "start_combat"):
            resolution = "narrative"
        dc = obj.get("dc")
        try:
            dc = int(dc) if dc is not None else None
        except (TypeError, ValueError):
            dc = None
        if resolution == "skill_check" and (dc is None or not obj.get("skill")):
            resolution = "narrative"
        encounter = obj.get("encounter")
        if not isinstance(encounter, dict):
            encounter = None
        if resolution == "start_combat" and not (encounter or {}).get("monsters"):
            resolution = "narrative"
            encounter = None
        return {
            "resolution": resolution,
            "skill": obj.get("skill"),
            "dc": dc,
            "actor": obj.get("actor"),
            "narration": clamp_words(obj.get("narration"), NARRATION_WORDS) or "",
            "encounter": encounter,
        }

    def scene_options(self, view: str) -> list[str]:
        user = render("dm_scene_options.txt", view=view, dm_note=self.take_note())
        text = self._call(user, MAX_TOKENS_OPTIONS)
        try:
            obj = extract_json(text)
        except AgentOutputError:
            obj = {}
        options = obj.get("options")
        cleaned = [
            " ".join(str(o).split())
            for o in (options or [])
            if isinstance(o, (str, int, float)) and str(o).strip()
        ][:4]
        if not cleaned:
            cleaned = ["Press on", "Search the area", "Hold position and listen"]
        return cleaned
