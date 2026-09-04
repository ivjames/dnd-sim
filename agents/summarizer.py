"""Rolling summary, run on the cheap model. CONTRACTS.md §3."""

from __future__ import annotations

from typing import Any

from llm.client import LLMClient
from llm.cost import Ledger

from .common import (
    AgentOutputError,
    clamp_words,
    event_text,
    extract_json,
    load_prompt,
    render,
)

__all__ = ["summarize", "SUMMARY_WORDS"]

SUMMARY_WORDS = 150
MAX_TOKENS = 300
_SKIP_KINDS = {"roll", "system", "cost", "turn_start", "turn_end"}


def _event_lines(events: list) -> str:
    lines = []
    for ev in events or []:
        kind = getattr(ev, "kind", "") or (ev.get("kind") if isinstance(ev, dict) else "")
        text = event_text(ev)
        if not text or kind in _SKIP_KINDS:
            continue
        lines.append(f"- {text}")
    return "\n".join(lines[-60:]) or "- (nothing notable)"


def summarize(
    client: LLMClient,
    model: str,
    ledger: Ledger,
    previous_summary: str,
    events: list,
    *,
    role: str = "summarizer",
) -> str:
    """Merge `events` into `previous_summary`; ≤150 words.

    Returns the previous summary unchanged if the model output is unusable —
    a bad summary is worse than a stale one.
    """
    user = render(
        "summarize.txt",
        previous_summary=(previous_summary or "(none yet)")[:2000],
        events=_event_lines(events),
    )
    system: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": load_prompt("summarizer_system.txt"),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    resp = client.complete(
        model=model,
        system=system,
        messages=[{"role": "user", "content": user}],
        max_tokens=MAX_TOKENS,
        temperature=0.3,
        json_only=True,
    )
    ledger.add(role, resp)
    try:
        obj = extract_json(resp.text)
    except AgentOutputError:
        return clamp_words(resp.text, SUMMARY_WORDS) or previous_summary
    text = clamp_words(obj.get("summary") or obj.get("text"), SUMMARY_WORDS)
    return text or previous_summary
