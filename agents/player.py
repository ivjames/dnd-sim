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
    rejection_preamble,
    render,
    rules_digest,
    speech_fields,
)
from .reference import seat_reference
from .views import pronouns_of_sheet, render_actions

__all__ = ["PlayerAgent", "AgentOutputError", "DEFAULT_TEMPERATURE", "clamp_temperature"]

#: The shortest system prefix Anthropic will cache for the Haiku models these
#: seats run on. Below it the `cache_control` marker is accepted and silently
#: does nothing — which is what it did for the first sixteen live games: 823
#: player calls, not one cache read, on a block of about 1,900 tokens.
#: `agents.reference` exists to carry the block past this, with SRD text the
#: character can actually use rather than filler. Held by
#: `tests/orchestrator/test_agents.py::test_every_example_seat_clears_the_cache_minimum`.
CACHE_MIN_TOKENS = 4096
#: Tokens are counted server-side; 3.5 characters per token is the pessimistic
#: end of the usual English range, so a block that clears the bound by this
#: measure clears it in fact.
CHARS_PER_TOKEN = 3.5

# Players sample hot on purpose. At 0.8 a Haiku seat converges: the same
# character reaches for the same opening line and the same attack every round,
# and `Game._say` then swallows the repeat, so the character goes quiet rather
# than saying something new. 1.0 is the top of the Anthropic range, not a
# tuned optimum — it is simply as much variance as the API will give.
DEFAULT_TEMPERATURE = 1.0
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 1.0

SPEECH_WORDS = 20
SCENE_SPEECH_WORDS = 25
FREE_SPEECH_WORDS = 35   # a social beat; spoken aloud, so kept short
REASONING_WORDS = 25
MAX_TOKENS_ACTION = 200
MAX_TOKENS_SPEECH = 160


def clamp_temperature(value: Any, default: float = DEFAULT_TEMPERATURE) -> float:
    """A sampling temperature the wire will accept, or `default` if unreadable.

    The ceiling is 1.0 because that is Anthropic's maximum and every default
    seat here is an Anthropic model; an OpenAI-compatible host would take 2.0,
    but a config that only works on some seats is worse than one that works on
    all of them. Clamping rather than raising is deliberate: a bad number in a
    scenario file should cost variety, not kill the game mid-scene.
    """
    try:
        t = float(value)
    except (TypeError, ValueError):
        return default
    if t != t:  # NaN
        return default
    return min(TEMPERATURE_MAX, max(TEMPERATURE_MIN, t))


def _sheet_summary(sheet: Any) -> str:
    """One line of who this character is, for the cached system block."""
    if sheet is None:
        return "unknown adventurer"
    abil = getattr(sheet, "abilities", {}) or {}
    abil_str = " ".join(f"{k} {v}" for k, v in abil.items())
    bits = [
        f"{getattr(sheet, 'name', '?')} ({pronouns_of_sheet(sheet)}), "
        f"level {getattr(sheet, 'level', 1)} "
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
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> None:
        self.client = client
        self.model = model
        self.sheet = sheet
        self.ledger = ledger
        self.engine = engine
        self.temperature = clamp_temperature(temperature)
        self.actor_id = getattr(sheet, "id", "pc")
        self.name = getattr(sheet, "name", self.actor_id)
        self.role = f"player:{self.actor_id}"
        # The seat's SRD reference is appended, not prepended: everything above
        # it is what the older prompt said and where the tests look for it, and
        # a cached prefix is only stable if new material goes on the end.
        self._system = (
            render(
                "player_system.txt",
                sheet_line=_sheet_summary(sheet),
                persona=getattr(sheet, "persona", "") or "a capable adventurer",
                rules_digest=rules_digest(),
            ).rstrip()
            + "\n\n"
            + seat_reference(sheet)
        )

    # -- prompt plumbing ---------------------------------------------------

    @property
    def system_blocks(self) -> list[dict]:
        """Stable, cacheable system prefix (persona + rules + role rules + SRD).

        One block, marked `ephemeral`, and byte-identical on every call of a
        game — `seat_reference` reads the sheet and nothing else, no round, no
        clock — because a prefix that changes by one character is a prefix that
        is never read from cache.
        """
        return [
            {
                "type": "text",
                "text": self._system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def _call(self, user: str, max_tokens: int, temperature: float | None = None) -> str:
        resp = self.client.complete(
            model=self.model,
            system=self.system_blocks,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=self.temperature if temperature is None else temperature,
            json_only=True,
        )
        self.ledger.add(self.role, resp)
        return resp.text

    # -- combat ------------------------------------------------------------

    def choose_action(
        self,
        view: str,
        templates: list,
        *,
        speak: bool = True,
        rejected: str | None = None,
    ) -> Any:
        """Pick one legal action. One retry on bad output, then AgentOutputError.

        `speak=False` tells the character it has already had its line this turn;
        the reply then carries no speech at all.

        `rejected` is the engine's own complaint about the action this seat
        chose a moment ago — an occupied square, a wall, a target out of range.
        It goes in the user turn rather than the system block so the cached
        prefix stays byte-identical, and it is worth the second call: the
        engine's message names the thing that was wrong, and a seat told that
        much usually picks a legal action next. Without it the turn was simply
        thrown away.
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
        user = rejection_preamble(rejected) + user
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
        """A short in-character line for social/exploration beats (≤35 words)."""
        user = render("player_speech.txt", view=view, prompt=prompt)
        text = self._call(user, MAX_TOKENS_SPEECH)
        try:
            obj = extract_json(text)
        except AgentOutputError:
            return clamp_words(text, FREE_SPEECH_WORDS) or ""
        return clamp_words(obj.get("speech") or obj.get("text"), FREE_SPEECH_WORDS) or ""

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
