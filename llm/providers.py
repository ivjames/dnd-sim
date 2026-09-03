"""Provider table: which platform serves a model id, and how to talk to it.

Contract: CONTRACTS.md §2, amendment 2026-09-03 (multi-provider routing).

One row per platform. Anthropic keeps the native SDK (`AnthropicClient`);
every other row is served by ONE OpenAI-compatible chat-completions adapter
(`OpenAICompatClient`) parameterised by base URL + key. Routing is by model-id
prefix — `provider_for("grok-4.3")` → the xai row — so a seat's platform is
decided by the model string alone and no config field names a provider.

Every URL / model id / price below was read from the cited page on
2026-09-03. Where a page could not be read or did not say, the row says so
rather than guessing; those gaps are listed in the CONTRACTS amendment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "Provider",
    "PROVIDERS",
    "provider_for",
    "COMPAT_RULES",
    "compat_params_for",
    "key_env_for",
]


@dataclass(frozen=True)
class Provider:
    name: str
    key_env: str
    base_url: str | None
    prefixes: tuple[str, ...]
    dialect: str  # "anthropic" (native SDK) | "openai_compat" (chat/completions)
    json_mode: bool  # accepts response_format={"type": "json_object"}
    max_tokens_field: str  # "max_tokens" | "max_completion_tokens"
    docs: str  # the page the row was read from


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        name="anthropic",
        key_env="ANTHROPIC_API_KEY",
        base_url=None,  # native SDK, not the compat adapter
        prefixes=("claude-",),
        dialect="anthropic",
        json_mode=False,  # no response_format; JSON is asked for in the system text
        max_tokens_field="max_tokens",
        docs="llm/client.py (AnthropicClient; per-model rules in MODEL_RULES)",
    ),
    Provider(
        # https://developers.openai.com/api/docs/api-reference/chat/create
        #   POST /chat/completions; `max_tokens` is "deprecated in favor of
        #   max_completion_tokens"; response_format {"type": "json_object"}
        #   supported; role "system" accepted; usage.prompt_tokens_details.
        #   cached_tokens. Prices: https://developers.openai.com/api/docs/pricing
        name="openai",
        key_env="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1",
        prefixes=("gpt-", "chatgpt-", "o1", "o3", "o4"),
        dialect="openai_compat",
        json_mode=True,
        max_tokens_field="max_completion_tokens",
        docs="https://developers.openai.com/api/docs/api-reference/chat/create",
    ),
    Provider(
        # https://docs.x.ai/docs/overview — base URL https://api.x.ai/v1, key
        #   $XAI_API_KEY, OpenAI client interface.
        # https://docs.x.ai/docs/guides/structured-outputs — response_format
        #   "also accepts json_object for any well-formed JSON".
        # Prices + ids: https://docs.x.ai/docs/models
        name="xai",
        key_env="XAI_API_KEY",
        base_url="https://api.x.ai/v1",
        prefixes=("grok-",),
        dialect="openai_compat",
        json_mode=True,
        max_tokens_field="max_tokens",
        docs="https://docs.x.ai/docs/models",
    ),
    Provider(
        # https://docs.mistral.ai/api/ — POST https://api.mistral.ai/v1/chat/
        #   completions; max_tokens, temperature, response_format json_object
        #   ("guarantees the message ... is in JSON"), system role.
        # Prices + ids: https://mistral.ai/pricing/api
        name="mistral",
        key_env="MISTRAL_API_KEY",
        base_url="https://api.mistral.ai/v1",
        prefixes=(
            "mistral-",
            "ministral-",
            "magistral-",
            "codestral-",
            "open-mistral",
            "open-mixtral",
            "pixtral-",
        ),
        dialect="openai_compat",
        json_mode=True,
        max_tokens_field="max_tokens",
        docs="https://docs.mistral.ai/api/",
    ),
    Provider(
        # https://ai.google.dev/gemini-api/docs/openai — base_url
        #   https://generativelanguage.googleapis.com/v1beta/openai/, key
        #   GEMINI_API_KEY as Bearer, system role + temperature + max_tokens in
        #   the examples, `reasoning_effort` mapped to thinking level. The page
        #   shows structured output only through the SDK's `.parse()` with a
        #   schema; `response_format: {"type": "json_object"}` appears nowhere
        #   on it, so json_mode is OFF here — the JSON instruction in the
        #   system text plus the agents' tolerant parser carry Gemini seats.
        # Prices + ids: https://ai.google.dev/gemini-api/docs/pricing
        name="gemini",
        key_env="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        prefixes=("gemini-",),
        dialect="openai_compat",
        json_mode=False,  # unconfirmed on the compat layer; see above
        max_tokens_field="max_tokens",
        docs="https://ai.google.dev/gemini-api/docs/openai",
    ),
    Provider(
        # https://api-docs.deepseek.com/ — "uses an API format compatible with
        #   OpenAI"; the page's curl posts to https://api.deepseek.com/chat/
        #   completions (no /v1 segment appears on the page as read today).
        # https://api-docs.deepseek.com/api/create-chat-completion — max_tokens,
        #   temperature, response_format json_object, system role; usage carries
        #   prompt_cache_hit_tokens / prompt_cache_miss_tokens.
        # https://api-docs.deepseek.com/guides/json_mode — json_object needs the
        #   word "json" in the prompt (JSON_ONLY_SUFFIX has it).
        # https://api-docs.deepseek.com/guides/thinking_mode — thinking is ON
        #   by default; {"thinking": {"type": "disabled"}} turns it off.
        # Prices: https://api-docs.deepseek.com/quick_start/pricing/
        name="deepseek",
        key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        prefixes=("deepseek-",),
        dialect="openai_compat",
        json_mode=True,
        max_tokens_field="max_tokens",
        docs="https://api-docs.deepseek.com/",
    ),
)


def provider_for(model: str) -> Provider | None:
    """The row whose prefix matches `model`, or None when nothing routes it."""
    m = (model or "").strip().lower()
    for p in PROVIDERS:
        if any(m.startswith(pre) for pre in p.prefixes):
            return p
    return None


def key_env_for(model: str) -> str | None:
    p = provider_for(model)
    return p.key_env if p else None


# --- per-model request parameters on the compat dialect --------------------
#
# The same problem MODEL_RULES solves for Anthropic: reasoning models spend
# hidden tokens against the per-call cap, and some reject sampling fields.
# Each row: (model-id prefix, forward `temperature`?, extra top-level body
# fields). First match wins; no match → (True, {}).
#
#   openai   reasoning tokens "are billed as output tokens" and the cap covers
#            reasoning + reply (developers.openai.com/api/docs/guides/reasoning),
#            so effort is turned down: `minimal` for gpt-5/-mini/-nano/-pro and
#            gpt-6 (gpt-6-astra returns 400 on `none`), `none` for gpt-5.1+.
#            `temperature` is dropped on the whole gpt-5/gpt-6 family: the
#            reference says parameter support "can differ ... particularly for
#            newer reasoning models" without listing which — omitting it can
#            never 400, sending it can.
#   xai      grok-4.6 / grok-4.5 / grok-4.20-multi-agent are reasoning models
#            with reasoning_effort low|medium|high (default high) — send low.
#            Temperature is not listed as unsupported, so it is kept.
#   gemini   reasoning_effort "none" disables thinking on 2.5 models but
#            "cannot be turned off for Gemini 2.5 Pro or 3 models", where
#            `minimal` is the lowest accepted level.
#   deepseek thinking is on by default; {"thinking": {"type": "disabled"}}
#            (top-level in the JSON body — the SDK's extra_body) turns it off,
#            and non-thinking mode accepts temperature.
COMPAT_RULES: tuple[tuple[str, bool, dict[str, Any]], ...] = (
    ("gpt-5-", False, {"reasoning_effort": "minimal"}),
    ("gpt-5.", False, {"reasoning_effort": "none"}),
    ("gpt-5", False, {"reasoning_effort": "minimal"}),
    ("gpt-6", False, {"reasoning_effort": "minimal"}),
    ("grok-4.6", True, {"reasoning_effort": "low"}),
    ("grok-4.5", True, {"reasoning_effort": "low"}),
    ("grok-4.20-multi-agent", True, {"reasoning_effort": "low"}),
    ("gemini-2.5-flash", True, {"reasoning_effort": "none"}),
    ("gemini-", True, {"reasoning_effort": "minimal"}),
    ("deepseek-", True, {"thinking": {"type": "disabled"}}),
)
_DEFAULT_COMPAT_RULE: tuple[bool, dict[str, Any]] = (True, {})


def compat_params_for(model: str, *, temperature: float | None) -> dict[str, Any]:
    """Extra chat-completions body fields for `model` (fresh dicts, never shared)."""
    sampling, extra = _DEFAULT_COMPAT_RULE
    m = (model or "").lower()
    for prefix, allow_sampling, fields in COMPAT_RULES:
        if m.startswith(prefix):
            sampling, extra = allow_sampling, fields
            break
    params: dict[str, Any] = {}
    if sampling and temperature is not None:
        params["temperature"] = temperature
    for k, v in extra.items():
        params[k] = dict(v) if isinstance(v, dict) else v
    return params
