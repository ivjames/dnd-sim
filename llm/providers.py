"""Provider table: which platform serves a model id, and how to talk to it.

Contract: CONTRACTS.md §2, amendment 2026-09-03 (multi-provider routing).

One row per platform. Anthropic keeps the native SDK (`AnthropicClient`);
every other row is served by ONE OpenAI-compatible chat-completions adapter
(`OpenAICompatClient`) parameterised by base URL + key. Routing is by model-id
prefix — `provider_for("grok-4.3")` → the xai row — so a seat's platform is
decided by the model string alone and no config field names a provider.

Two rows are *hosts* rather than platforms: SiliconFlow and DeepInfra serve
other people's models under namespaced ids (`deepseek-ai/DeepSeek-V3.2`,
`Qwen/Qwen3-32B`) that say nothing about which host is meant — and a bare
`deepseek-` prefix already routes to DeepSeek's own API. So those rows carry
no prefixes and are reached only by the explicit form `provider:model`
(`deepinfra:Qwen/Qwen3-32B`), which overrides prefix routing for any row and
is the seat id everywhere: config, preflight, snapshot, price rows. Only the
part after the colon goes on the wire (`split_model` / `wire_model`).

Every URL / model id / price below was read from the cited page on
2026-09-03. Where a page could not be read or did not say, the row says so
rather than guessing; those gaps are listed in the CONTRACTS amendment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "Provider",
    "PROVIDERS",
    "HOSTS",
    "provider_for",
    "provider_named",
    "split_model",
    "wire_model",
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
    # lower-cased wire-model prefixes on which this row's json_mode does NOT
    # hold (a host that fronts many models can support it unevenly)
    json_mode_except: tuple[str, ...] = ()

    def accepts_json_mode(self, wire: str) -> bool:
        w = (wire or "").lower()
        return self.json_mode and not any(w.startswith(x) for x in self.json_mode_except)


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
    Provider(
        # SiliconFlow's INTERNATIONAL platform. SiliconFlow runs two platforms
        # with separate consoles, keys and base URLs, and each doc site names
        # only its own: docs.siliconflow.com/en (keys minted on
        # cloud.siliconflow.com) posts to https://api.siliconflow.com/v1;
        # docs.siliconflow.cn/en (keys from cloud.siliconflow.cn/account/ak)
        # posts to https://api.siliconflow.cn/v1. A non-China user signs up
        # on the .com console, so the .com host is wired here; a .cn key
        # would need the .cn URL and will not authenticate against this one.
        # https://docs.siliconflow.com/en/api-reference/chat-completions/chat-completions
        #   servers: https://api.siliconflow.com/v1, POST /chat/completions;
        #   `max_tokens`; roles user|assistant|system; `enable_thinking`
        #   (bool, default true) on the Qwen3 series (8B/14B/32B/30B-A3B/
        #   235B-A22B) and the DeepSeek-V3.1 / V3.2 variants; the usage object
        #   is prompt/completion/total_tokens only — no cache-hit field, so
        #   cache reads are never counted here (nor discounted).
        # https://docs.siliconflow.com/en/userguide/guides/json-mode —
        #   response_format {"type": "json_object"} is supported by "online
        #   models, except for the DeepSeek R1 series and V3 models"; hence
        #   json_mode_except, which keeps it off the DeepSeek V3/R1 ids.
        # Prices: https://www.siliconflow.com/models
        name="siliconflow",
        key_env="SILICONFLOW_API_KEY",
        base_url="https://api.siliconflow.com/v1",
        prefixes=(),  # a host: reached only by the explicit `siliconflow:<id>` form
        dialect="openai_compat",
        json_mode=True,
        json_mode_except=("deepseek-ai/deepseek-r1", "deepseek-ai/deepseek-v3"),
        max_tokens_field="max_tokens",
        docs="https://docs.siliconflow.com/en/api-reference/chat-completions/chat-completions",
    ),
    Provider(
        # https://docs.deepinfra.com/chat/overview — POST
        #   https://api.deepinfra.com/v1/openai/chat/completions, header
        #   `Authorization: Bearer $DEEPINFRA_TOKEN`, `max_tokens`, roles
        #   system|user|assistant; parameters listed: model, messages,
        #   max_tokens, stream, temperature, top_p, stop, n, presence_penalty,
        #   frequency_penalty, response_format, tools, tool_choice,
        #   service_tier, fail_fast, reasoning_effort.
        # https://docs.deepinfra.com/chat/structured-outputs — response_format
        #   json_object and json_schema; "Always prompt the model to produce
        #   JSON" (JSON_ONLY_SUFFIX does).
        # https://docs.deepinfra.com/chat/reasoning — reasoning_effort
        #   none|low|medium|high, "high" the default for reasoning models,
        #   "none" disables chain-of-thought; listed for the DeepSeek-V4
        #   family, GLM-5.2, Kimi-K3 and Ling-3.0-flash — NOT for
        #   DeepSeek-V3.2 or Qwen3, so no COMPAT_RULES row sends it to those
        #   (an undocumented field can 400; sending nothing cannot).
        # Prices: https://deepinfra.com/pricing (the per-model pages under
        #   deepinfra.com/<id> agree). Cached-input rates are listed there;
        #   which usage field reports cached tokens is not on the pages read,
        #   so the adapter's OpenAI-shaped `prompt_tokens_details.cached_tokens`
        #   mapping is the only path that would ever count them.
        name="deepinfra",
        key_env="DEEPINFRA_API_KEY",
        base_url="https://api.deepinfra.com/v1/openai",
        prefixes=(),  # a host: reached only by the explicit `deepinfra:<id>` form
        dialect="openai_compat",
        json_mode=True,
        max_tokens_field="max_tokens",
        docs="https://docs.deepinfra.com/chat/overview",
    ),
)

_BY_NAME: dict[str, Provider] = {p.name: p for p in PROVIDERS}

# Rows with no prefixes: hosts that serve namespaced ids and are reached only
# through the explicit `provider:model` form.
HOSTS: tuple[Provider, ...] = tuple(p for p in PROVIDERS if not p.prefixes)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def split_model(model: str) -> tuple[str | None, str]:
    """`"deepinfra:Qwen/Qwen3-32B"` → ("deepinfra", "Qwen/Qwen3-32B"); a bare id → (None, id).

    The explicit form is `<provider name>:<model id>`. The name is returned
    lower-cased and is NOT checked against the table here — `provider_for`
    answers None for an unknown one and the router names it in its error.
    """
    m = (model or "").strip()
    head, sep, tail = m.partition(":")
    head, tail = head.strip().lower(), tail.strip()
    if sep and tail and _NAME_RE.match(head):
        return head, tail
    return None, m


def wire_model(model: str) -> str:
    """The id to put in the request body: the part after `provider:`, else the id itself."""
    return split_model(model)[1]


def provider_named(name: str) -> Provider | None:
    return _BY_NAME.get((name or "").strip().lower())


def provider_for(model: str) -> Provider | None:
    """The row `model` routes to, or None when nothing routes it.

    The explicit `provider:model` form wins outright (an unknown provider name
    → None, never a prefix fallback on the remainder); a bare id routes by
    prefix. A namespaced bare id (`Qwen/Qwen3-32B`, `deepseek-ai/DeepSeek-V3.2`)
    routes nowhere — never by prefix, and the hosts have none — and the
    router says which form to use.
    """
    name, wire = split_model(model)
    if name is not None:
        return _BY_NAME.get(name)
    if "/" in wire:
        # `deepseek-ai/DeepSeek-V3.2` starts with `deepseek-` but is a hosted
        # id, not DeepSeek's API; a namespaced bare id routes nowhere.
        return None
    m = wire.lower()
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
#   siliconflow  `enable_thinking` defaults to true on the Qwen3 series and
#            the DeepSeek-V3.1/V3.2 variants (chat-completions reference), so
#            those rows send `enable_thinking: false`. Keyed on the full
#            `siliconflow:<id>` seat id: a host rule must never leak onto the
#            same model served elsewhere.
#   deepinfra  nothing: its reasoning page lists `reasoning_effort` for the
#            DeepSeek-V4 family and a few others, not for DeepSeek-V3.2 or
#            Qwen3, so those seats send plain sampling fields.
#
# Matching: the full seat id (lower-cased) first, so `provider:`-keyed rows
# hit; then, only when the explicit provider is the one the wire id's prefix
# would have routed to anyway (`deepseek:deepseek-v4-flash`), the bare wire
# id — so DeepSeek's own `thinking` field never reaches `deepinfra:deepseek-ai/…`.
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
    ("siliconflow:qwen/qwen3-", True, {"enable_thinking": False}),
    ("siliconflow:deepseek-ai/deepseek-v3.1", True, {"enable_thinking": False}),
    ("siliconflow:deepseek-ai/deepseek-v3.2", True, {"enable_thinking": False}),
)
_DEFAULT_COMPAT_RULE: tuple[bool, dict[str, Any]] = (True, {})


def compat_params_for(model: str, *, temperature: float | None) -> dict[str, Any]:
    """Extra chat-completions body fields for `model` (fresh dicts, never shared)."""
    sampling, extra = _DEFAULT_COMPAT_RULE
    name, wire = split_model(model)
    # Match on the normalised spelling (lower-cased provider, stripped id), the
    # same key pricing uses, so `siliconflow: Qwen/Qwen3-32B` finds its rule.
    candidates = [f"{name}:{wire}".lower() if name is not None else (model or "").strip().lower()]
    if name is not None:
        p = _BY_NAME.get(name)
        if p is not None and any(wire.lower().startswith(pre) for pre in p.prefixes):
            candidates.append(wire.lower())
    for m in candidates:
        hit = next(((s, f) for prefix, s, f in COMPAT_RULES if m.startswith(prefix)), None)
        if hit is not None:
            sampling, extra = hit
            break
    params: dict[str, Any] = {}
    if sampling and temperature is not None:
        params["temperature"] = temperature
    for k, v in extra.items():
        params[k] = dict(v) if isinstance(v, dict) else v
    return params
