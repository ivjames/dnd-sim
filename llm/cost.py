"""Token/cost accounting. Contract: CONTRACTS.md §2 (+ amendment 2026-09-03)."""

from __future__ import annotations

from .client import LLMResponse

__all__ = ["PRICES", "CACHE_READ_PRICES", "Ledger", "price_for", "has_price", "cache_read_price_for"]

# $ per million tokens: (input, output). Keys are model ids or id prefixes;
# lookup is exact first, then the LONGEST prefix that ends on an id boundary
# ("-", ".", or end of string), so "gpt-5.4-nano" never falls through to the
# "gpt-5" row. Every non-Anthropic price was read on 2026-09-03 from the page
# named above its block; sub-200k-context / standard-tier rates throughout.
PRICES: dict[str, tuple[float, float]] = {
    # anthropic (unchanged)
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    # openai — https://developers.openai.com/api/docs/pricing
    "gpt-6-astra": (10.0, 50.0),
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.2, 1.2),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.5-pro": (30.0, 180.0),
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.4-nano": (0.2, 1.25),
    "gpt-5.4-pro": (30.0, 180.0),
    "gpt-5.2": (1.75, 14.0),
    "gpt-5.2-pro": (21.0, 168.0),
    "gpt-5.1": (1.25, 10.0),
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.4),
    "gpt-5-pro": (15.0, 120.0),
    # xai — https://docs.x.ai/docs/models (<200k-token prompts)
    "grok-4.6": (2.0, 6.0),
    "grok-4.5": (2.0, 6.0),
    "grok-4.3": (1.25, 2.5),
    "grok-4.20-0309-reasoning": (1.25, 2.5),
    "grok-4.20-0309-non-reasoning": (1.25, 2.5),
    "grok-4.20-multi-agent-0309": (1.25, 2.5),
    "grok-build-0.1": (1.0, 2.0),
    # mistral — https://mistral.ai/pricing/api
    "mistral-medium-latest": (1.5, 7.5),
    "mistral-medium-3-5": (1.5, 7.5),
    "mistral-small-latest": (0.15, 0.6),
    "mistral-large-latest": (0.5, 1.5),
    "codestral-latest": (0.3, 0.9),
    "ministral-3b-latest": (0.1, 0.1),
    "ministral-8b-latest": (0.15, 0.15),
    "ministral-14b-latest": (0.2, 0.2),
    # gemini — https://ai.google.dev/gemini-api/docs/pricing (paid tier, text).
    # 3.8/3.7/3.6 Flash are promotional through 2026-12-31 and double on
    # 2027-01-01 ($1.50 / $7.50) — re-read the page then.
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash": (1.5, 9.0),
    "gemini-3.5-flash-lite": (0.3, 2.5),
    "gemini-3.1-flash-lite": (0.25, 1.5),
    "gemini-3.1-pro-preview": (2.0, 12.0),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.3, 2.5),
    "gemini-2.5-flash-lite": (0.1, 0.4),
    # deepseek — https://api-docs.deepseek.com/quick_start/pricing/ ; PEAK
    # rates (off-peak is half, 01:00–04:00 and 06:00–10:00 UTC weekdays) so
    # the budget stop errs on the side of stopping.
    "deepseek-v4-flash": (0.44, 1.32),
    "deepseek-v4-pro": (1.32, 3.96),
    # hosts — keyed by the seat id as written, `provider:<host's model id>`
    # (explicit form only; these rows have no prefixes). Case matters: the
    # part after the colon is the host's id verbatim.
    # siliconflow (international platform, api.siliconflow.com) —
    # https://www.siliconflow.com/models . The usage object on this host has
    # no cache-hit field, so cached input is billed as input here (errs toward
    # stopping) and there is no CACHE_READ_PRICES row. Llama-3.3-70B-Instruct
    # is not listed on the international platform: no row.
    "siliconflow:deepseek-ai/DeepSeek-V3.2": (0.27, 0.42),
    "siliconflow:deepseek-ai/DeepSeek-V3": (0.25, 1.0),
    "siliconflow:Qwen/Qwen3-32B": (0.14, 0.57),
    "siliconflow:Qwen/Qwen3-14B": (0.07, 0.28),
    # deepinfra — https://deepinfra.com/pricing (standard tier; the model
    # pages deepinfra.com/deepseek-ai/DeepSeek-V3.2, deepinfra.com/Qwen/Qwen3-32B
    # and deepinfra.com/meta-llama/Llama-3.3-70B-Instruct-Turbo say the same).
    # The non-Turbo meta-llama/Llama-3.3-70B-Instruct page is a 404 today, so
    # only the -Turbo id is priced.
    "deepinfra:deepseek-ai/DeepSeek-V3.2": (0.26, 0.38),
    "deepinfra:Qwen/Qwen3-32B": (0.08, 0.28),
    "deepinfra:meta-llama/Llama-3.3-70B-Instruct-Turbo": (0.10, 0.32),
}

CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25
_DEFAULT_PRICE = (2.0, 10.0)

# $ per million CACHED input tokens where the provider's discount is not the
# 0.1× that CACHE_READ_MULT assumes (OpenAI and Gemini are exactly 0.1×, so
# they need no row). Same lookup rule as PRICES.
CACHE_READ_PRICES: dict[str, float] = {
    # xai — https://docs.x.ai/docs/models
    "grok-4.6": 0.5,
    "grok-4.5": 0.3,
    "grok-4.3": 0.2,
    "grok-4.20-0309-reasoning": 0.2,
    "grok-4.20-0309-non-reasoning": 0.2,
    "grok-4.20-multi-agent-0309": 0.2,
    "grok-build-0.1": 0.2,
    # deepseek — cache-hit input, peak
    "deepseek-v4-flash": 0.014,
    "deepseek-v4-pro": 0.044,
    # deepinfra — https://deepinfra.com/pricing "$0.26 / $0.13 cached"; only
    # ever applied if the host reports prompt_tokens_details.cached_tokens
    "deepinfra:deepseek-ai/DeepSeek-V3.2": 0.13,
}


def _lookup_raw(table: dict, model: str):
    """Exact key, else the longest prefix key ending on an id boundary."""
    if model in table:
        return table[model]
    best: str | None = None
    for known in table:
        if model.startswith(known) and (
            len(model) == len(known) or model[len(known)] in "-.:@"
        ):
            if best is None or len(known) > len(best):
                best = known
    return table[best] if best is not None else None


def _lookup(table: dict, model: str):
    """`_lookup_raw`, then — for the explicit `provider:model` form on a
    provider that its bare id would route to anyway (`openai:gpt-5.4-nano`,
    `deepseek:deepseek-v4-flash`) — the bare id's row. Host-qualified keys
    (`deepinfra:...`, `siliconflow:...`) never fall back: the same model id
    costs different money on different hosts, so those rows are keyed in full.
    """
    from .providers import provider_for, provider_named, split_model  # noqa: PLC0415

    name, wire = split_model(model)
    if name is None or wire == model:
        return _lookup_raw(table, model)
    # The explicit form matches a row exactly or not at all: boundary-prefix
    # matching exists for dated Anthropic ids (`claude-haiku-4-5-20251001` →
    # `claude-haiku-4-5`), and on a host key it would let `DeepSeek-V3` answer
    # for `DeepSeek-V3.1`, a model with no verified rate.
    if model in table:
        return table[model]
    prov = provider_named(name)
    # Only when the bare id would route to this very provider: `anthropic:gpt-5.4-nano`
    # names a provider that will reject the id, so it must not borrow OpenAI's rate.
    if prov is None or not prov.prefixes or provider_for(wire) is not prov:
        return None
    return _lookup_raw(table, wire)


def has_price(model: str) -> bool:
    """True when `model` hits a PRICES row (exactly or by boundary prefix)."""
    return _lookup(PRICES, model or "") is not None


def price_for(model: str) -> tuple[float, float]:
    hit = _lookup(PRICES, model)
    if hit is not None:
        return hit
    # legacy leniency: a dated key answering for its undated model id
    for known, price in PRICES.items():
        if known.startswith(model):
            return price
    return _DEFAULT_PRICE


def cache_read_price_for(model: str) -> float:
    """$/MTok for cache-read input: the provider's rate, else 0.1× input."""
    hit = _lookup(CACHE_READ_PRICES, model)
    if hit is not None:
        return hit
    return price_for(model)[0] * CACHE_READ_MULT


class Ledger:
    """Running USD/token totals, overall and per role."""

    def __init__(self) -> None:
        self.total_usd: float = 0.0
        self.by_role: dict[str, dict] = {}

    def add(self, role: str, resp: LLMResponse) -> float:
        pin, pout = price_for(resp.model)
        usd = (
            resp.input_tokens * pin
            + resp.output_tokens * pout
            + resp.cache_read_tokens * cache_read_price_for(resp.model)
            + resp.cache_write_tokens * pin * CACHE_WRITE_MULT
        ) / 1_000_000.0
        row = self.by_role.setdefault(
            role,
            {"calls": 0, "in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "usd": 0.0},
        )
        row["calls"] += 1
        row["in"] += resp.input_tokens
        row["out"] += resp.output_tokens
        row["cache_read"] += resp.cache_read_tokens
        row["cache_write"] += resp.cache_write_tokens
        row["usd"] = round(row["usd"] + usd, 8)
        self.total_usd = round(self.total_usd + usd, 8)
        return usd

    def to_dict(self) -> dict:
        return {
            "total_usd": round(self.total_usd, 6),
            "by_role": {k: dict(v) for k, v in self.by_role.items()},
            "calls": sum(v["calls"] for v in self.by_role.values()),
            "input_tokens": sum(v["in"] for v in self.by_role.values()),
            "output_tokens": sum(v["out"] for v in self.by_role.values()),
        }
