"""Token/cost accounting. Contract: CONTRACTS.md §2."""

from __future__ import annotations

from .client import LLMResponse

__all__ = ["PRICES", "Ledger", "price_for"]

# $ per million tokens: (input, output)
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25
_DEFAULT_PRICE = (2.0, 10.0)


def price_for(model: str) -> tuple[float, float]:
    if model in PRICES:
        return PRICES[model]
    for known, price in PRICES.items():
        if model.startswith(known) or known.startswith(model):
            return price
    return _DEFAULT_PRICE


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
            + resp.cache_read_tokens * pin * CACHE_READ_MULT
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
