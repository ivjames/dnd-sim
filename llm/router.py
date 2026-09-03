"""RouterClient: one `LLMClient` that fans out to a provider per model id.

The orchestrator holds one client per game; with this one, every seat (DM,
each player, summarizer) can name a model on any platform in `PROVIDERS` and
the call lands on the right adapter. Per-provider clients are built lazily,
so a game seated entirely on Anthropic never touches httpx and a game with
no Anthropic seat never imports the SDK. Contract: CONTRACTS.md §2,
amendment 2026-09-03.

`preflight(models)` is the fail-fast check run at game creation in live mode:
every seat's model must route to a provider, that provider's key must be in
the environment, and the model must have a price row (the budget stop is
blind otherwise) unless `DND_ALLOW_UNPRICED=1`.

A seat id may be the explicit `provider:model` form (`deepinfra:Qwen/Qwen3-32B`),
which is the only way to reach a host row and overrides prefix routing for
the rest. The router hands the compat adapter the full id (it strips the
prefix itself and keeps the full id on the response for pricing) and the
native Anthropic SDK the bare id, then stamps the full id back onto the
response so the ledger sees the seat as configured.
"""

from __future__ import annotations

import os
from dataclasses import replace
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .client import AnthropicClient, LLMError, LLMResponse
from .cost import has_price
from .providers import HOSTS, PROVIDERS, Provider, provider_for, provider_named, split_model

__all__ = ["RouterClient", "allow_unpriced"]

ClientFactory = Callable[[Provider, str], Any]


def allow_unpriced(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("DND_ALLOW_UNPRICED", "")).strip().lower() in ("1", "true", "yes", "on")


def _default_factory(provider: Provider, key: str) -> Any:
    if provider.dialect == "anthropic":
        return AnthropicClient(api_key=key)
    from .compat import OpenAICompatClient  # noqa: PLC0415

    return OpenAICompatClient(provider, key)


class RouterClient:
    """Implements LLMClient by delegating to a per-provider client."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        clients: Mapping[str, Any] | None = None,
        factory: ClientFactory | None = None,
    ) -> None:
        self._env = os.environ if env is None else env
        self._clients: dict[str, Any] = dict(clients or {})
        self._factory = factory or _default_factory

    # -- routing -----------------------------------------------------------

    @staticmethod
    def provider_for(model: str) -> Provider:
        name, wire = split_model(model)
        hosts = ", ".join(h.name for h in HOSTS)
        if name is not None:
            p = provider_named(name)
            if p is None:
                names = ", ".join(prov.name for prov in PROVIDERS)
                raise LLMError(
                    f"unknown provider {name!r} in model {model!r}; the provider:model "
                    f"form takes one of: {names}"
                )
            return p
        p = provider_for(model)
        if p is not None:
            return p
        if "/" in wire:
            examples = " or ".join(f"{h.name}:{wire}" for h in HOSTS)
            raise LLMError(
                f"model {model!r} is a namespaced id that does not say which host serves "
                f"it; use the provider:model form — {examples} (hosts: {hosts})"
            )
        known = ", ".join(pre for prov in PROVIDERS for pre in prov.prefixes)
        raise LLMError(
            f"no provider routes model {model!r}; known model-id prefixes: {known}; "
            f"a model on a host needs the provider:model form (hosts: {hosts})"
        )

    def key_for(self, provider: Provider) -> str:
        key = self._env.get(provider.key_env)
        if not key:
            raise LLMError(
                f"{provider.key_env} is not set — needed to seat a {provider.name} model"
            )
        return key

    def client_for(self, model: str) -> Any:
        p = self.provider_for(model)
        client = self._clients.get(p.name)
        if client is None:
            client = self._factory(p, self.key_for(p))
            self._clients[p.name] = client
        return client

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
        p = self.provider_for(model)
        name, wire = split_model(model)
        # The native SDK knows nothing of the prefix; the compat adapter strips
        # it itself and needs the full id to key the response for pricing.
        send = wire if p.dialect == "anthropic" else model
        resp = self.client_for(model).complete(
            model=send,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            json_only=json_only,
        )
        if name is not None and resp.model != model:
            resp = replace(resp, model=model)
        return resp

    # -- fail-fast at game creation ----------------------------------------

    def preflight(
        self,
        seats: Mapping[str, str] | Iterable[str],
        *,
        allow_unpriced: bool | None = None,
    ) -> None:
        """Raise one LLMError listing everything wrong with these seats."""
        if isinstance(seats, Mapping):
            pairs = list(seats.items())
        else:
            pairs = [(str(m), str(m)) for m in seats]
        if allow_unpriced is None:
            allow_unpriced = globals()["allow_unpriced"](self._env)
        problems: list[str] = []
        seen: set[str] = set()
        for seat, model in pairs:
            model = str(model or "")
            if not model or model in seen:
                continue
            seen.add(model)
            try:
                p = self.provider_for(model)
            except LLMError as exc:
                problems.append(f"{seat}: {exc}")
                continue
            if p.name not in self._clients:
                try:
                    self.key_for(p)
                except LLMError as exc:
                    problems.append(f"{seat}: {exc}")
                    continue
            if not allow_unpriced and not has_price(model):
                problems.append(
                    f"{seat}: no price row for model {model!r} in llm/cost.py PRICES — the "
                    "budget stop would be blind; add a row, or set DND_ALLOW_UNPRICED=1 "
                    "to run it at the default price"
                )
        if problems:
            raise LLMError("cannot seat this game:\n  " + "\n  ".join(problems))
