"""Shared agent plumbing: prompt loading, tolerant JSON parsing, rules digest.

Amendment (see CONTRACTS.md): this helper module is not named in §3 but holds
machinery all three agent modules need.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

__all__ = [
    "AgentOutputError",
    "load_prompt",
    "render",
    "extract_json",
    "rules_digest",
    "clamp_words",
    "action_class",
    "speech_fields",
    "event_text",
    "rejection_preamble",
]

_PROMPT_DIR = Path(__file__).parent / "prompts"

# Short standalone digest used when engine.srd is unavailable (parallel builds,
# unit tests). The engine's own digest wins whenever it imports.
_FALLBACK_DIGEST = """\
D&D 5e combat, as this engine runs it:
- Turn order by initiative (1d20+DEX). Each turn: one action, one bonus action, one reaction, and movement up to your speed. Difficult terrain costs double.
- Attack: 1d20 + ability mod + proficiency vs target AC. Natural 20 always hits and doubles the damage dice; natural 1 always misses.
- Advantage rolls twice and keeps the higher; disadvantage keeps the lower. They cancel out. Ranged attacks have disadvantage while an enemy is within 5 ft.
- Cover: half cover +2 AC and +2 DEX saves, three-quarters cover +5.
- Saving throws: 1d20 + ability mod (+ proficiency if proficient) vs the effect's DC. Spell save DC = 8 + proficiency + casting ability mod.
- Spells cost a slot of the level used; upcasting a lower-level spell in a higher slot scales it. Concentration holds one spell at a time; taking damage forces a CON save at DC 10 or half the damage taken, whichever is higher, and casting another concentration spell drops the first.
- Conditions in play: blinded, charmed, deafened, frightened, grappled, incapacitated, invisible, paralyzed, petrified, poisoned, prone, restrained, stunned, unconscious, and exhaustion levels. Attacks against a paralyzed or unconscious creature have advantage and crit from within 5 ft.
- Leaving an enemy's reach on foot provokes an opportunity attack unless you Disengage. Dodge imposes disadvantage on attacks against you.
- At 0 HP a creature falls unconscious and rolls death saves each turn: three successes stabilize, three failures kill; a natural 20 restores 1 HP, a natural 1 counts as two failures. Any healing above 0 wakes it. Damage taken while down counts as a failure; a critical hit counts as two.
- Healing never exceeds maximum HP. Temporary HP does not stack and absorbs damage first.
"""


class AgentOutputError(RuntimeError):
    """The model's output could not be turned into a valid decision."""


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Read a prompt template from agents/prompts (cached; templates are static)."""
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def render(name: str, **kwargs: object) -> str:
    return load_prompt(name).format(**kwargs)


@lru_cache(maxsize=1)
def rules_digest() -> str:
    """SRD combat digest for the cached system prefix.

    Uses the engine's canonical digest when the engine is importable, so both
    layers stay in sync; falls back to a built-in string otherwise.
    """
    try:
        from engine import srd  # noqa: PLC0415

        text = srd.rules_digest()
        if isinstance(text, str) and text.strip():
            return text.strip()
    except Exception:  # noqa: BLE001 - engine may not exist yet or may be partial
        pass
    return _FALLBACK_DIGEST.strip()


# --- tolerant JSON ---------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _balanced_object(text: str) -> str | None:
    """Return the first brace-balanced {...} substring, ignoring braces in strings."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model reply. Raises AgentOutputError."""
    if not text or not text.strip():
        raise AgentOutputError("empty model output")
    candidates: list[str] = []
    stripped = text.strip()
    fence = _FENCE_RE.search(stripped)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(stripped)
    balanced = _balanced_object(stripped)
    if balanced:
        candidates.append(balanced)
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    raise AgentOutputError(f"no JSON object in output: {text[:200]!r}")


@dataclass
class _FallbackAction:
    """Stand-in for engine.actions.Action when the engine is not importable."""

    actor: str
    template_id: str
    params: dict
    speech: str | None = None


def action_class(engine: object | None = None):
    """Resolve the Action dataclass to construct: injected engine wins."""
    if engine is not None and hasattr(engine, "Action"):
        return engine.Action
    try:
        from engine.actions import Action  # noqa: PLC0415

        return Action
    except Exception:  # noqa: BLE001 - engine may not exist yet
        return _FallbackAction


def event_text(ev: object) -> str:
    """One line of an event for a prompt, with the speaker on dialogue.

    Dialogue events carry the bare spoken line in `text` and the name in
    `data["speaker"]` (the UI renders the two separately), so attribution has
    to be put back for a prompt.
    """
    get = ev.get if isinstance(ev, dict) else lambda k, d=None: getattr(ev, k, d)
    text = str(get("text", "") or "").strip()
    if not text or (get("kind", "") or "") != "dialogue":
        return text
    data = get("data", None) or {}
    speaker = data.get("speaker") if isinstance(data, dict) else None
    return f"{speaker}: {text}" if speaker else text


_SPEECH_RULE_ON = (
    'SPEECH: at most one short line, and only when it adds something the table has '
    'not heard. Silence is normal — on most turns set "speech" to null. Never say '
    "again, in other words, a line you have already said."
)
_SPEECH_RULE_OFF = (
    'SPEECH: you have already spoken this turn. Set "speech" to null and just act.'
)


#: How the engine's refusal of an action is put back to the agent that chose
#: it. One wording for both seats — a player and a monster are told the same
#: thing because the engine tells them the same thing — and it opens the user
#: turn rather than the system block, so the cached prefix does not move.
_REJECTION = (
    "The engine rejected your last action: {message}. Choose again from the "
    "list. Something else in it will work; the same choice will not."
)


def rejection_preamble(message: str | None) -> str:
    """The lead-in for a re-ask after `IllegalAction`, or "" if there was none."""
    text = " ".join(str(message or "").split())
    if not text:
        return ""
    return _REJECTION.format(message=text) + "\n\n"


def speech_fields(speak: bool, words: int) -> dict[str, str]:
    """The `{speech_shape}`/`{speech_rule}` pair an action prompt expects.

    `speak=False` is how the orchestrator says "you have had your line this
    turn" — one combatant's turn can span several actions (extra attacks,
    Action Surge, a move), and a line apiece turns a turn into a monologue.
    """
    if not speak:
        return {"speech_shape": "null", "speech_rule": _SPEECH_RULE_OFF}
    return {
        "speech_shape": f'"<in-character, {words} words max, or null>"',
        "speech_rule": _SPEECH_RULE_ON,
    }


def clamp_words(text: str | None, limit: int) -> str | None:
    """Trim free text to `limit` words (belt and braces; the prompt asks too)."""
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    text = " ".join(text.split())
    if not text:
        return None
    words = text.split(" ")
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]).rstrip(",;:") + "…"
