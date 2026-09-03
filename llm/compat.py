"""OpenAI-compatible chat-completions client over httpx.

Serves every non-Anthropic row of `llm.providers.PROVIDERS` (OpenAI, xAI,
Mistral, Gemini's compat endpoint, DeepSeek) with one adapter: POST
`<base_url>/chat/completions`, system + user messages, the provider's
max-tokens field, `temperature` where the model accepts it, and
`response_format={"type": "json_object"}` only when the provider supports it
AND the call asked for JSON. Usage is mapped onto `LLMResponse` so `Ledger`
works unchanged. Contract: CONTRACTS.md §2, amendment 2026-09-03.

Retry policy mirrors `AnthropicClient`: 429 / 5xx / timeouts / transport
errors retry with backoff (3 attempts); any other 4xx raises `LLMError` at
once. The API key travels only in the Authorization header and is never put
in an error message or log line.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .client import JSON_ONLY_SUFFIX, LLMError, LLMResponse
from .providers import Provider, compat_params_for

__all__ = ["OpenAICompatClient"]


def _system_text(system: str | list[dict]) -> str:
    """Flatten the Anthropic-shaped system (str or content blocks) to one string.

    `cache_control` markers are dropped: prompt caching is not carried across
    to the compat dialect (providers cache — or not — on their own terms).
    """
    if isinstance(system, str):
        return system
    parts: list[str] = []
    for b in system or []:
        text = b.get("text", "") if isinstance(b, dict) else str(b)
        if text:
            parts.append(str(text))
    return "\n\n".join(parts)


def _message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for b in content or []:
        text = b.get("text", "") if isinstance(b, dict) else str(b)
        if text:
            parts.append(str(text))
    return "\n".join(parts)


class OpenAICompatClient:
    """LLMClient for one `Provider` row of the openai_compat dialect."""

    MAX_ATTEMPTS = 3

    def __init__(
        self,
        provider: Provider,
        api_key: str | None = None,
        *,
        transport: Any = None,
        timeout: float = 60.0,
        max_attempts: int | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        if provider.dialect != "openai_compat" or not provider.base_url:
            raise LLMError(f"provider {provider.name!r} is not served by the compat adapter")
        key = api_key or os.environ.get(provider.key_env)
        if not key:
            raise LLMError(f"{provider.key_env} is not set (needed for the {provider.name} seat)")
        try:
            import httpx  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - env dependent
            raise LLMError("the `httpx` package is required for live runs, or use --mock") from exc
        self.provider = provider
        self._sleep = sleep
        if max_attempts is not None:
            self.MAX_ATTEMPTS = max_attempts
        self._httpx = httpx
        self._url = provider.base_url.rstrip("/") + "/chat/completions"
        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    # -- request shape -----------------------------------------------------

    def build_body(
        self,
        *,
        model: str,
        system: str | list[dict],
        messages: list[dict],
        max_tokens: int,
        temperature: float | None = 0.7,
        json_only: bool = False,
    ) -> dict[str, Any]:
        """The exact JSON body `complete` posts. Public so tests/docs can show it."""
        sys_text = _system_text(system)
        if json_only:
            sys_text = sys_text + JSON_ONLY_SUFFIX
        msgs: list[dict[str, str]] = []
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
        for m in messages or []:
            role = m.get("role", "user")
            msgs.append({"role": role, "content": _message_content(m.get("content", ""))})
        body: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            self.provider.max_tokens_field: int(max_tokens),
        }
        body.update(compat_params_for(model, temperature=temperature))
        if json_only and self.provider.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    # -- retry policy ------------------------------------------------------

    @staticmethod
    def _retryable_status(status: int) -> bool:
        return status == 429 or status >= 500

    def _retryable_exc(self, exc: Exception) -> bool:
        hx = self._httpx
        return isinstance(exc, (hx.TimeoutException, hx.TransportError))

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
        body = self.build_body(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            json_only=json_only,
        )
        name = self.provider.name
        last: str = ""
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                resp = self._http.post(self._url, json=body)
            except Exception as exc:  # noqa: BLE001 - httpx hierarchy
                last = f"{type(exc).__name__}: {exc}"
                if not self._retryable_exc(exc) or attempt == self.MAX_ATTEMPTS - 1:
                    raise LLMError(f"{name} call failed: {last}") from exc
                self._sleep(min(8.0, 0.5 * (2**attempt)))
                continue
            status = resp.status_code
            if status >= 400:
                last = f"HTTP {status}: {_snippet(resp)}"
                if not self._retryable_status(status) or attempt == self.MAX_ATTEMPTS - 1:
                    raise LLMError(f"{name} call failed: {last}")
                self._sleep(min(8.0, 0.5 * (2**attempt)))
                continue
            try:
                raw = resp.json()
            except ValueError as exc:
                raise LLMError(f"{name} returned non-JSON: {_snippet(resp)}") from exc
            return self._to_response(raw, model)
        raise LLMError(f"{name} call failed: {last}")  # pragma: no cover

    # -- response mapping --------------------------------------------------

    @staticmethod
    def _to_response(raw: Any, model: str) -> LLMResponse:
        if not isinstance(raw, dict):
            raise LLMError("chat completion is not an object")
        choices = raw.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        text = _message_content(message.get("content") or "")
        usage = raw.get("usage") or {}

        def u(*path: str) -> int:
            cur: Any = usage
            for key in path:
                if not isinstance(cur, dict):
                    return 0
                cur = cur.get(key)
            try:
                return int(cur or 0)
            except (TypeError, ValueError):
                return 0

        prompt = u("prompt_tokens")
        # OpenAI/xAI/Gemini/Mistral: prompt_tokens INCLUDES cached tokens and
        # prompt_tokens_details.cached_tokens says how many; DeepSeek splits
        # the prompt into prompt_cache_hit_tokens + prompt_cache_miss_tokens.
        cached = u("prompt_tokens_details", "cached_tokens") or u("prompt_cache_hit_tokens")
        cached = min(cached, prompt)
        return LLMResponse(
            text=text,
            input_tokens=prompt - cached,
            output_tokens=u("completion_tokens"),  # includes reasoning tokens
            cache_read_tokens=cached,
            cache_write_tokens=0,  # no provider on this dialect reports writes
            model=str(raw.get("model") or model),
            stop_reason=str(choice.get("finish_reason") or ""),
        )


def _snippet(resp: Any, limit: int = 300) -> str:
    try:
        text = resp.text
    except Exception:  # noqa: BLE001
        return ""
    text = " ".join(str(text).split())
    try:
        obj = json.loads(text)
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict) and err.get("message"):
            text = str(err["message"])
        elif isinstance(err, str):
            text = err
    except ValueError:
        pass
    return text[:limit]
