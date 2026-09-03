"""PlayerAgent — one Haiku instance per party member. CONTRACTS.md §3."""

from __future__ import annotations

from typing import Any

from llm.client import LLMClient
from llm.cost import Ledger

from .common import (
    AgentOutputError,
    action_class,
    clamp_words,
    extract_json,
    render,
    rules_digest,
    speech_fields,
)
from .views import render_actions

__all__ = ["PlayerAgent", "AgentOutputError"]

SPEECH_WORDS = 20
SCENE_SPEECH_WORDS = 25
REASONING_WORDS = 25
MAX_TOKENS_ACTION = 200
MAX_TOKENS_SPEECH = 160


def _sheet_summary(sheet: Any) -> str:
    """One line of who this character is, for the cached system block."""
    if sheet is None:
        return "unknown adventurer"
    abil = getattr(sheet, "abilities", {}) or {}
    abil_str = " ".join(f"{k} {v}" for k, v in abil.items())
    bits = [
        f"{getattr(sheet, 'name', '?')}, level {getattr(sheet, 'level', 1)} "
        f"{getattr(sheet, 'race', '?')} {getattr(sheet, 'klass', '?')}",
        f"AC {getattr(sheet, 'ac', 10)}, {getattr(sheet, 'max_hp', 1)} max HP, "
        f"speed {getattr(sheet, 'speed', 30)} ft",
        abil_str,
    ]
    weapons = getattr(sheet, "weapons", None)
    if weapons:
        bits.append("Weapons: " + ", ".join(weapons))
    spells = getattr(sheet, "spells_known", None)
    if spells:
        bits.append("Spells: " + ", ".join(spells))
    features = getattr(sheet, "features", None)
    if features:
        bits.append("Features: " + ", ".join(features))
    return ". ".join(b for b in bits if b)


class PlayerAgent:
    """Turns a compact view + legal actions into a validated engine Action."""

    def __init__(
        self,
        client: LLMClient,
        model: str,
        sheet: Any,
        ledger: Ledger,
        *,
        engine: Any = None,
    ) -> None:
        self.client = client
        self.model = model
        self.sheet = sheet
        self.ledger = ledger
        self.engine = engine
        self.actor_id = getattr(sheet, "id", "pc")
        self.name = getattr(sheet, "name", self.actor_id)
        self.role = f"player:{self.actor_id}"
        self._system = render(
            "player_system.txt",
            sheet_line=_sheet_summary(sheet),
            persona=getattr(sheet, "persona", "") or "a capable adventurer",
            rules_digest=rules_digest(),
        )

    # -- prompt plumbing ---------------------------------------------------

    @property
    def system_blocks(self) -> list[dict]:
        """Stable, cacheable system prefix (persona + rules + role rules)."""
        return [
            {
                "type": "text",
                "text": self._system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def _call(self, user: str, max_tokens: int, temperature: float = 0.8) -> str:
        resp = self.client.complete(
            model=self.model,
            system=self.system_blocks,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=temperature,
            json_only=True,
        )
        self.ledger.add(self.role, resp)
        return resp.text

    # -- combat ------------------------------------------------------------

    def choose_action(self, view: str, templates: list, *, speak: bool = True) -> Any:
        """Pick one legal action. One retry on bad output, then AgentOutputError.

        `speak=False` tells the character it has already had its line this turn;
        the reply then carries no speech at all.
        """
        if not templates:
            raise AgentOutputError("no legal actions offered")
        by_id = {getattr(t, "id", None) or t.get("id"): t for t in templates}
        user = render(
            "player_action.txt",
            view=view,
            actions=render_actions(templates),
            **speech_fields(speak, SPEECH_WORDS),
        )
        error: str | None = None
        for attempt in range(2):
            prompt = user if error is None else f"{user}\n\nYOUR LAST REPLY WAS REJECTED: {error}\nTry again. JSON only."
            text = self._call(prompt, MAX_TOKENS_ACTION)
            try:
                return self._parse_action(text, by_id, speak=speak)
            except AgentOutputError as exc:
                error = str(exc)
                if attempt == 1:
                    raise AgentOutputError(
                        f"{self.name} failed to choose a legal action: {error}"
                    ) from exc
        raise AgentOutputError("unreachable")  # pragma: no cover

    def _parse_action(self, text: str, by_id: dict, *, speak: bool = True) -> Any:
        obj = extract_json(text)
        aid = obj.get("action") or obj.get("action_id") or obj.get("id")
        if not isinstance(aid, str):
            raise AgentOutputError("missing string field 'action'")
        aid = aid.strip().strip("[]")
        if aid not in by_id:
            raise AgentOutputError(
                f"'{aid}' is not one of the offered ids: {', '.join(by_id)}"
            )
        template = by_id[aid]
        params = obj.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise AgentOutputError("'params' must be a JSON object")
        needs = getattr(template, "needs", None)
        if needs is None and isinstance(template, dict):
            needs = template.get("needs")
        for key in needs or []:
            if key not in params or params[key] in (None, ""):
                raise AgentOutputError(f"action {aid} needs a '{key}' value in params")
        cls = action_class(self.engine)
        return cls(
            actor=self.actor_id,
            template_id=aid,
            params=params,
            speech=clamp_words(obj.get("speech"), SPEECH_WORDS) if speak else None,
        )

    # -- non-combat --------------------------------------------------------

    def speak(self, view: str, prompt: str) -> str:
        """A short in-character line for social/exploration beats (≤60 words)."""
        user = render("player_speech.txt", view=view, prompt=prompt)
        text = self._call(user, MAX_TOKENS_SPEECH)
        try:
            obj = extract_json(text)
        except AgentOutputError:
            return clamp_words(text, 60) or ""
        return clamp_words(obj.get("speech") or obj.get("text"), 60) or ""

    def choose_scene_action(
        self, view: str, options: list[str], said: list[str] | None = None
    ) -> dict:
        """{"choice": idx, "speech": str} — index is always in range.

        `said` is what the party has already argued this beat, so a character
        who agrees can vote without restating the point in its own words.
        """
        rendered = "\n".join(f"{i}. {o}" for i, o in enumerate(options))
        user = render(
            "player_scene_choice.txt",
            view=view,
            options=rendered,
            said="\n".join(f"- {line}" for line in said or []) or "(nobody has spoken yet)",
        )
        text = self._call(user, MAX_TOKENS_SPEECH)
        try:
            obj = extract_json(text)
        except AgentOutputError:
            return {"choice": 0, "speech": clamp_words(text, SCENE_SPEECH_WORDS) or ""}
        try:
            choice = int(obj.get("choice", 0))
        except (TypeError, ValueError):
            choice = 0
        if not 0 <= choice < max(1, len(options)):
            choice = 0
        return {
            "choice": choice,
            "speech": clamp_words(obj.get("speech"), SCENE_SPEECH_WORDS) or "",
        }
