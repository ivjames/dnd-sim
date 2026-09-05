"""LLM clients: a thin Anthropic wrapper and a deterministic mock.

Contract: CONTRACTS.md §2.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

__all__ = [
    "LLMResponse",
    "LLMClient",
    "AnthropicClient",
    "MockLLMClient",
    "LLMError",
    "DM_MODEL",
    "PLAYER_MODEL",
    "SUMMARY_MODEL",
    "MODEL_RULES",
    "request_params_for",
]


class LLMError(RuntimeError):
    """Raised when the provider fails after all retries."""


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    model: str
    stop_reason: str

    def to_dict(self) -> dict:
        return asdict(self)


class LLMClient(Protocol):
    def complete(
        self,
        *,
        model: str,
        system: str | list[dict],
        messages: list[dict],
        max_tokens: int,
        temperature: float = 0.7,
        json_only: bool = False,
    ) -> LLMResponse: ...


# --- model defaults from env (CONTRACTS §2) -------------------------------

DM_MODEL = os.environ.get("DND_DM_MODEL", "claude-sonnet-5")
PLAYER_MODEL = os.environ.get("DND_PLAYER_MODEL", "claude-haiku-4-5-20251001")
SUMMARY_MODEL = os.environ.get("DND_SUMMARY_MODEL", PLAYER_MODEL)

#: Shortest system prefix Anthropic will cache, in tokens, by model id or
#: prefix (longest prefix wins). Below it the `cache_control` marker is
#: silently inert — no error, no cache entry — which is how 823 player calls
#: on Haiku 4.5 read nothing from cache across the first sixteen live games:
#: the block was ~1,900 tokens against a 4,096 floor. Read on 2026-09-05
#: from the prompt-caching reference; not a price, so it lives here rather
#: than in cost.py. Non-Anthropic ids fall through to the default.
CACHE_MIN_PREFIX_TOKENS: dict[str, int] = {
    "claude-haiku-4-5": 4096,
    "claude-sonnet-5": 1024,
    "claude-opus-5": 512,
    "claude-fable-5": 512,
}
DEFAULT_CACHE_MIN_PREFIX_TOKENS = 1024


def cache_min_prefix_tokens(model: str) -> int:
    """Tokens a system prefix needs before the provider caches it."""
    hits = [k for k in CACHE_MIN_PREFIX_TOKENS if (model or "").startswith(k)]
    if not hits:
        return DEFAULT_CACHE_MIN_PREFIX_TOKENS
    return CACHE_MIN_PREFIX_TOKENS[max(hits, key=len)]


# --- per-model request parameters (CONTRACTS §2, amendment 2026-09-03) ----
#
# Which sampling/thinking fields a model accepts is a property of the model
# family, and getting it wrong is a hard 400, not a degraded answer. The rule
# lives here — in the client, once — so the agents keep passing `temperature`
# and a model swap via DND_*_MODEL is one row in this table.
#
# Each row: (model-id prefix, forward sampling params?, `thinking` to send or
# None to omit the field). First prefix match wins; no match → _DEFAULT_RULE.
#
#   sampling=False  the API rejects temperature/top_p/top_k (400) — drop them.
#   thinking=disabled  these models run ADAPTIVE thinking when the field is
#                   absent, and thinking tokens count against `max_tokens`.
#                   Our per-call caps are 200–600 tokens of JSON/narration, so
#                   an unasked-for think would eat the whole budget and return
#                   a truncated (or empty) reply. Disabling keeps the caps —
#                   and the frugality requirement — meaningful.
#   thinking=None   Haiku 4.5 / Sonnet 4.6 and older: thinking is off unless
#                   explicitly enabled; sending {"type": "disabled"} to Haiku
#                   4.5 is an error, so the field is simply left out.
#
# claude-fable-*: thinking is always on and BOTH "disabled" and budget_tokens
# are rejected, so the only valid request omits the field — which means the
# tight max_tokens caps here would not survive a Fable run. Nobody uses Fable
# in this project (ten times the Sonnet price); the row exists so a stray
# DND_DM_MODEL=claude-fable-5 fails on max_tokens, not on a 400.
MODEL_RULES: tuple[tuple[str, bool, dict | None], ...] = (
    ("claude-fable", False, None),
    ("claude-sonnet-5", False, {"type": "disabled"}),
    ("claude-opus-5", False, {"type": "disabled"}),
    ("claude-opus-4-7", False, {"type": "disabled"}),
    ("claude-opus-4-8", False, {"type": "disabled"}),
)
_DEFAULT_RULE: tuple[bool, dict | None] = (True, None)


def request_params_for(model: str, *, temperature: float | None) -> dict[str, Any]:
    """Extra `messages.create` kwargs for `model`: sampling and `thinking`.

    Returns only the keys that should be on the wire — a missing key is the
    point (an absent `thinking` on Haiku, an absent `temperature` on Sonnet 5),
    so callers splat the result rather than reading fixed fields from it.
    """
    sampling, thinking = _DEFAULT_RULE
    for prefix, allow_sampling, thinking_field in MODEL_RULES:
        if model.startswith(prefix):
            sampling, thinking = allow_sampling, thinking_field
            break
    params: dict[str, Any] = {}
    if sampling and temperature is not None:
        params["temperature"] = temperature
    if thinking is not None:
        params["thinking"] = dict(thinking)
    return params


JSON_ONLY_SUFFIX = (
    "\n\nOutput rules: reply with a single raw JSON object and nothing else. "
    "No prose, no markdown fences, no explanation before or after."
)


def _as_blocks(system: str | list[dict]) -> list[dict]:
    """Normalize `system` to Anthropic content blocks.

    A plain string becomes ONE cached block. A list is passed through, but the
    first block gets `cache_control: ephemeral` if none is marked — the stable
    prefix (role rules + SRD digest) is what we want cached.
    """
    if isinstance(system, str):
        blocks = [{"type": "text", "text": system}]
    else:
        blocks = [dict(b) for b in system]
    if not blocks:
        return blocks
    if not any("cache_control" in b for b in blocks):
        blocks[0]["cache_control"] = {"type": "ephemeral"}
    return blocks


class AnthropicClient:
    """LLMClient backed by the `anthropic` SDK.

    The SDK is imported lazily so the rest of the package (and the whole test
    suite) works without the dependency installed.
    """

    MAX_ATTEMPTS = 3

    def __init__(
        self,
        api_key: str | None = None,
        *,
        sdk: Any = None,
        max_attempts: int | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._sleep = sleep
        if max_attempts is not None:
            self.MAX_ATTEMPTS = max_attempts
        if sdk is not None:
            self._client = sdk
            return
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - env dependent
            raise LLMError(
                "the `anthropic` package is required for live runs "
                "(pip install anthropic), or use --mock"
            ) from exc
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=key)

    # -- retry policy ------------------------------------------------------

    @staticmethod
    def _status_of(exc: Exception) -> int | None:
        st = getattr(exc, "status_code", None)
        if st is None:
            resp = getattr(exc, "response", None)
            st = getattr(resp, "status_code", None)
        try:
            return int(st) if st is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _retryable(cls, exc: Exception) -> bool:
        st = cls._status_of(exc)
        if st is None:
            # connection/timeout style errors from the SDK
            name = type(exc).__name__.lower()
            return "connection" in name or "timeout" in name or "apierror" in name
        return st == 429 or st >= 500

    def complete(
        self,
        *,
        model: str,
        system: str | list[dict],
        messages: list[dict],
        max_tokens: int,
        temperature: float = 0.7,
        json_only: bool = False,
    ) -> LLMResponse:
        blocks = _as_blocks(system)
        if json_only and blocks:
            blocks = blocks[:-1] + [
                {**blocks[-1], "text": blocks[-1].get("text", "") + JSON_ONLY_SUFFIX}
            ]
        params = request_params_for(model, temperature=temperature)
        last: Exception | None = None
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                raw = self._client.messages.create(
                    model=model,
                    system=blocks,
                    messages=messages,
                    max_tokens=max_tokens,
                    **params,
                )
            except Exception as exc:  # noqa: BLE001 - SDK exception hierarchy varies
                last = exc
                if not self._retryable(exc) or attempt == self.MAX_ATTEMPTS - 1:
                    raise LLMError(f"anthropic call failed: {exc}") from exc
                self._sleep(min(8.0, 0.5 * (2**attempt)))
                continue
            return self._to_response(raw, model)
        raise LLMError(f"anthropic call failed: {last}")  # pragma: no cover

    @staticmethod
    def _to_response(raw: Any, model: str) -> LLMResponse:
        parts: list[str] = []
        for block in getattr(raw, "content", []) or []:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text:
                parts.append(text)
        usage = getattr(raw, "usage", None)

        def u(name: str) -> int:
            val = getattr(usage, name, None)
            if val is None and isinstance(usage, dict):
                val = usage.get(name)
            return int(val or 0)

        return LLMResponse(
            text="".join(parts),
            input_tokens=u("input_tokens"),
            output_tokens=u("output_tokens"),
            cache_read_tokens=u("cache_read_input_tokens"),
            cache_write_tokens=u("cache_creation_input_tokens"),
            model=getattr(raw, "model", model) or model,
            stop_reason=getattr(raw, "stop_reason", "") or "",
        )


# --------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------

_ACTION_RE = re.compile(r"^\s*\[(a\d+)\]\s*(.*)$", re.MULTILINE)
_SHAPE_RE = re.compile(r"RESPONSE_SHAPE:\s*([a-z_]+)")
_SUGGESTED_RE = re.compile(r"suggested=(\[.*?\])\s*$", re.MULTILINE)
_COORD_RE = re.compile(r"\(?\s*(-?\d+)\s*,\s*(-?\d+)\s*\)?")

#: Skills the mock DM asks for, in the engine's own spelling.
_SKILLS = ["Perception", "Stealth", "Investigation", "Survival", "Persuasion"]

_SPEECH = [
    "Stand fast — I have this one!",
    "For the light, and for coin!",
    "Watch the flank, watch the flank!",
    "You picked the wrong hallway.",
    "Steady. Breathe. Strike.",
    "That's going to leave a mark.",
    "Cover me, I'm going in.",
    "By the gods, hold the line!",
]

_NARRATION = [
    "Steel rings on steel as the skirmish tightens; dust drifts through the lantern light.",
    "The blow lands hard, and the echo of it rolls down the corridor.",
    "Boots scrape on stone. Somewhere behind the noise, something else is listening.",
    "The clash breaks apart for a heartbeat, then closes again like a fist.",
]

_SCENE = [
    "The passage opens ahead, cold and quiet, and the party's torchlight finds nothing friendly.",
    "A low wind moves through the ruin, carrying old smoke and older grudges.",
]


class MockLLMClient:
    """Deterministic stand-in for the API.

    Reads `RESPONSE_SHAPE:` from the prompt, harvests `[aN]` action ids, and
    always emits syntactically valid JSON (or plain prose for narration
    shapes). Never malformed.
    """

    def __init__(self, seed: int = 0) -> None:
        self.seed = seed
        self._rng = random.Random(seed)
        self.calls = 0
        #: system prefixes this client has already been sent, by model —
        #: the mock's stand-in for the provider's prompt cache.
        self._cached: set[tuple[str, str]] = set()

    @staticmethod
    def _system_text(system: str | list[dict]) -> str:
        if isinstance(system, str):
            return system
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in system or []
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _prompt_text(system: str | list[dict], messages: list[dict]) -> str:
        parts: list[str] = []
        if isinstance(system, str):
            parts.append(system)
        else:
            for b in system or []:
                parts.append(b.get("text", "") if isinstance(b, dict) else str(b))
        for m in messages or []:
            content = m.get("content", "")
            if isinstance(content, str):
                parts.append(content)
            else:
                for b in content:
                    parts.append(b.get("text", "") if isinstance(b, dict) else str(b))
        return "\n".join(parts)

    @staticmethod
    def _actions(prompt: str) -> list[tuple[str, str]]:
        return [(m.group(1), m.group(2)) for m in _ACTION_RE.finditer(prompt)]

    def _pick_action(self, prompt: str) -> tuple[str, str]:
        actions = self._actions(prompt)
        if not actions:
            return "a1", ""
        weights = []
        for _aid, label in actions:
            low = label.lower()
            if low.startswith("end turn") or "end_turn" in low:
                weights.append(1)
            elif "attack" in low or "cast" in low:
                weights.append(12)
            elif "move" in low or "dash" in low:
                weights.append(4)
            else:
                weights.append(3)
        return self._rng.choices(actions, weights=weights, k=1)[0]

    def _params_for(self, label: str) -> dict:
        """Supply the params a template's `needs=[...]` asks for.

        `render_actions` appends `needs=[...]` and, for moves, a
        `suggested=[(x,y), ...]` list; we pick one deterministically so the
        engine gets a usable destination rather than an empty path.
        """
        params: dict[str, Any] = {}
        if "needs=" not in label:
            return params
        needs_part = label.split("needs=", 1)[1]
        dest: Any = None
        m = _SUGGESTED_RE.search(label)
        if m:
            coords = _COORD_RE.findall(m.group(1))
            if coords:
                x, y = self._rng.choice(coords)
                dest = [int(x), int(y)]
        if "'path'" in needs_part or '"path"' in needs_part:
            params["path"] = [dest] if dest else []
        if "'point'" in needs_part or '"point"' in needs_part:
            params["point"] = dest
        if "'targets'" in needs_part or '"targets"' in needs_part:
            params["targets"] = []
        return params

    def _payload(self, shape: str, prompt: str) -> str:
        if shape in ("player_action", "dm_monster_action"):
            aid, label = self._pick_action(prompt)
            # The prompt pins "speech" to null once the actor has had its line
            # this turn; a live model obeys that, so the mock does too.
            speech = None if '"speech": null' in prompt else self._rng.choice(_SPEECH)
            return json.dumps(
                {
                    "action": aid,
                    "params": self._params_for(label),
                    "speech": speech,
                    "reasoning": "best available option",
                }
            )
        if shape == "summary":
            return json.dumps(
                {
                    "summary": "The party pressed on; blows were traded and the "
                    "situation remains unresolved."
                }
            )
        if shape == "dm_adjudication":
            # One ruling in three asks for a check, so a seeded mock game
            # walks the skill-check path — and, through the orchestrator,
            # the surprise it can decide — rather than only narrating.
            if self._rng.randrange(3) == 0:
                return json.dumps(
                    {
                        "resolution": "skill_check",
                        "skill": self._rng.choice(_SKILLS),
                        "dc": 10 + 2 * self._rng.randrange(4),
                        "actor": None,
                        "narration": self._rng.choice(_SCENE),
                        "encounter": None,
                    }
                )
            return json.dumps(
                {
                    "resolution": "narrative",
                    "skill": None,
                    "dc": None,
                    "actor": None,
                    "narration": self._rng.choice(_SCENE),
                    "encounter": None,
                }
            )
        if shape == "scene_options":
            return json.dumps(
                {
                    "options": [
                        "Search the room carefully",
                        "Press on down the corridor",
                        "Listen at the far door",
                    ]
                }
            )
        if shape == "player_speech":
            return json.dumps({"speech": self._rng.choice(_SPEECH)})
        if shape == "scene_choice":
            return json.dumps(
                {"choice": self._rng.randrange(3), "speech": self._rng.choice(_SPEECH)}
            )
        if shape == "dm_narration":
            return json.dumps({"narration": self._rng.choice(_NARRATION)})
        # unknown shape: still valid JSON
        return json.dumps({"text": self._rng.choice(_NARRATION)})

    def complete(
        self,
        *,
        model: str,
        system: str | list[dict],
        messages: list[dict],
        max_tokens: int,
        temperature: float = 0.7,
        json_only: bool = False,
    ) -> LLMResponse:
        self.calls += 1
        prompt = self._prompt_text(system, messages)
        m = _SHAPE_RE.search(prompt)
        shape = m.group(1) if m else "dm_narration"
        text = self._payload(shape, prompt)
        # cheap deterministic token estimate: ~4 chars/token
        in_tok = max(1, len(prompt) // 4)
        # Account for the cache the way the provider does, so a mock run's
        # total is a usable stand-in for a live one when a budget is sized
        # from it: a system prefix at or above the model's minimum is written
        # once (at the write multiple) and read on every later call at the
        # read multiple; a shorter one is billed as plain input every time,
        # which is exactly what happens live — the marker is silently inert.
        sys_tok = len(self._system_text(system)) // 4
        cache_read = cache_write = 0
        if sys_tok >= cache_min_prefix_tokens(model):
            key = (model, self._system_text(system))
            if key in self._cached:
                cache_read = sys_tok
            else:
                self._cached.add(key)
                cache_write = sys_tok
            in_tok = max(1, in_tok - sys_tok)
        return LLMResponse(
            text=text,
            input_tokens=in_tok,
            output_tokens=max(1, len(text) // 4),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            model=model,
            stop_reason="end_turn",
        )
