"""Multi-provider routing: provider table, OpenAI-compat adapter, router, seats.

No network: the adapter is exercised through httpx.MockTransport, the router
through injected fakes, and the game through the deterministic mock.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from llm.client import JSON_ONLY_SUFFIX, LLMError, LLMResponse, MockLLMClient
from llm.compat import OpenAICompatClient
from llm.cost import PRICES, Ledger, cache_read_price_for, has_price, price_for
from llm.providers import HOSTS, PROVIDERS, compat_params_for, provider_for, split_model, wire_model
from llm.router import RouterClient
from orchestrator.bus import EventBus
from orchestrator.config import GameConfig
from orchestrator.game import Game

from . import fake_engine as eng

COMPAT = [p for p in PROVIDERS if p.dialect == "openai_compat"]
BY_NAME = {p.name: p for p in PROVIDERS}

# one representative, priced model per compat provider: prefix-routed for the
# platforms, the explicit `provider:model` form for the two hosts
SAMPLE_MODEL = {
    "openai": "gpt-5.4-nano",
    "xai": "grok-4.3",
    "mistral": "mistral-small-latest",
    "gemini": "gemini-2.5-flash",
    "deepseek": "deepseek-v4-flash",
    "siliconflow": "siliconflow:Qwen/Qwen3-32B",
    "deepinfra": "deepinfra:meta-llama/Llama-3.3-70B-Instruct-Turbo",
}
PREFIX_ROUTED = {k: v for k, v in SAMPLE_MODEL.items() if ":" not in v}

SYSTEM = [{"type": "text", "text": "You are Thorin.", "cache_control": {"type": "ephemeral"}}]
USER = [{"role": "user", "content": "[a1] Attack\n[a2] End turn\nRESPONSE_SHAPE: player_action"}]


def _ok(model, content='{"action": "a1"}', usage=None):
    usage = usage or {"prompt_tokens": 100, "completion_tokens": 20}
    return {
        "id": "x",
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": usage,
    }


class Server:
    """A scripted upstream: a list of (status, body) replies, records requests."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.replies.pop(0) if self.replies else (200, _ok("m"))
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body)

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def _client(provider, server, key="sk-test-secret"):
    return OpenAICompatClient(
        provider, key, transport=httpx.MockTransport(server), sleep=lambda _s: None
    )


# --- provider table --------------------------------------------------------


def test_every_provider_has_a_key_env_and_unique_prefixes():
    seen = {}
    for p in PROVIDERS:
        assert p.key_env.endswith("_API_KEY")
        for pre in p.prefixes:
            assert pre not in seen, f"{pre!r} claimed by {seen.get(pre)} and {p.name}"
            seen[pre] = p.name
        if p.dialect == "openai_compat":
            assert p.base_url and p.base_url.startswith("https://")
            assert not p.base_url.endswith("/")


@pytest.mark.parametrize(
    "model, name",
    [
        ("claude-sonnet-5", "anthropic"),
        ("claude-haiku-4-5-20251001", "anthropic"),
        ("gpt-5.4-nano", "openai"),
        ("grok-4.3", "xai"),
        ("mistral-small-latest", "mistral"),
        ("ministral-8b-latest", "mistral"),
        ("gemini-2.5-flash", "gemini"),
        ("deepseek-v4-flash", "deepseek"),
        ("GPT-5", "openai"),
    ],
)
def test_provider_for_routes_by_prefix(model, name):
    assert provider_for(model).name == name


def test_provider_for_unknown_prefix_is_none():
    assert provider_for("llama-3") is None
    assert provider_for("") is None


# --- explicit provider:model form and the two hosts --------------------------


def test_the_hosts_are_siliconflow_and_deepinfra_with_no_prefixes():
    assert [h.name for h in HOSTS] == ["siliconflow", "deepinfra"]
    sf, di = BY_NAME["siliconflow"], BY_NAME["deepinfra"]
    assert sf.prefixes == () and di.prefixes == ()
    assert sf.key_env == "SILICONFLOW_API_KEY" and di.key_env == "DEEPINFRA_API_KEY"
    assert sf.base_url == "https://api.siliconflow.com/v1"  # international, not .cn
    assert di.base_url == "https://api.deepinfra.com/v1/openai"
    assert sf.max_tokens_field == di.max_tokens_field == "max_tokens"
    assert sf.json_mode and di.json_mode
    # SiliconFlow's json-mode page excludes the DeepSeek R1/V3 ids
    assert not sf.accepts_json_mode("deepseek-ai/DeepSeek-V3.2")
    assert not sf.accepts_json_mode("deepseek-ai/DeepSeek-R1")
    assert sf.accepts_json_mode("Qwen/Qwen3-32B")
    assert di.accepts_json_mode("deepseek-ai/DeepSeek-V3.2")


@pytest.mark.parametrize(
    "model, expect",
    [
        ("deepinfra:Qwen/Qwen3-32B", ("deepinfra", "Qwen/Qwen3-32B")),
        ("siliconflow:deepseek-ai/DeepSeek-V3.2", ("siliconflow", "deepseek-ai/DeepSeek-V3.2")),
        ("DeepInfra: Qwen/Qwen3-32B ", ("deepinfra", "Qwen/Qwen3-32B")),
        ("deepseek:deepseek-v4-flash", ("deepseek", "deepseek-v4-flash")),
        ("anthropic:claude-sonnet-5", ("anthropic", "claude-sonnet-5")),
        ("nope:gpt-5", ("nope", "gpt-5")),  # parsed; provider_for says None
        ("Qwen/Qwen3-32B", (None, "Qwen/Qwen3-32B")),
        ("gpt-5.4-nano", (None, "gpt-5.4-nano")),
        ("", (None, "")),
        ("deepinfra:", (None, "deepinfra:")),
    ],
)
def test_split_model_parses_the_explicit_form(model, expect):
    assert split_model(model) == expect
    assert wire_model(model) == expect[1]


@pytest.mark.parametrize(
    "model, name",
    [
        ("siliconflow:Qwen/Qwen3-32B", "siliconflow"),
        ("deepinfra:meta-llama/Llama-3.3-70B-Instruct-Turbo", "deepinfra"),
        # explicit wins over the prefix: deepseek-ai/... on a host is NOT DeepSeek's API
        ("siliconflow:deepseek-ai/DeepSeek-V3.2", "siliconflow"),
        ("deepinfra:deepseek-ai/DeepSeek-V3.2", "deepinfra"),
        # and the explicit form works for every prefix-routed row too
        ("deepseek:deepseek-v4-flash", "deepseek"),
        ("openai:gpt-5.4-nano", "openai"),
        ("anthropic:claude-sonnet-5", "anthropic"),
    ],
)
def test_provider_for_explicit_form(model, name):
    assert provider_for(model).name == name


def test_provider_for_namespaced_or_unknown_provider_is_none():
    assert provider_for("Qwen/Qwen3-32B") is None  # no host claims a prefix
    assert provider_for("meta-llama/Llama-3.3-70B-Instruct-Turbo") is None
    assert provider_for("deepseek-ai/DeepSeek-V3.2") is None  # NOT DeepSeek's API by prefix
    assert provider_for("nope:gpt-5") is None  # never falls back to the prefix


@pytest.mark.parametrize("model, name", [(m, n) for n, m in PREFIX_ROUTED.items()] + [("claude-sonnet-5", "anthropic")])
def test_existing_prefix_routing_is_unchanged(model, name):
    assert split_model(model) == (None, model)
    assert wire_model(model) == model
    assert provider_for(model).name == name


# --- request shape per provider -------------------------------------------


@pytest.mark.parametrize("provider", COMPAT, ids=[p.name for p in COMPAT])
def test_request_shape_for_a_player_call(provider):
    model = SAMPLE_MODEL[provider.name]
    server = Server((200, _ok(model)))
    resp = _client(provider, server).complete(
        model=model, system=SYSTEM, messages=USER, max_tokens=200, temperature=0.8, json_only=True
    )
    req = server.requests[0]
    assert str(req.url) == provider.base_url + "/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test-secret"
    body = json.loads(req.content)
    assert body["model"] == wire_model(model)  # never the `provider:` prefix
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"].startswith("You are Thorin.")
    assert body["messages"][0]["content"].endswith(JSON_ONLY_SUFFIX)
    assert "cache_control" not in json.dumps(body)
    assert body["messages"][1] == {"role": "user", "content": USER[0]["content"]}
    assert body[provider.max_tokens_field] == 200
    other = "max_tokens" if provider.max_tokens_field != "max_tokens" else "max_completion_tokens"
    assert other not in body
    if provider.json_mode:
        assert body["response_format"] == {"type": "json_object"}
    else:
        assert "response_format" not in body
    assert resp.text == '{"action": "a1"}'
    assert resp.stop_reason == "stop"


@pytest.mark.parametrize(
    "model, extra, json_mode",
    [
        ("siliconflow:Qwen/Qwen3-32B", {"enable_thinking": False}, True),
        ("siliconflow:deepseek-ai/DeepSeek-V3.2", {"enable_thinking": False}, False),
        ("deepinfra:deepseek-ai/DeepSeek-V3.2", {}, True),
        ("deepinfra:Qwen/Qwen3-32B", {}, True),
        ("deepinfra:meta-llama/Llama-3.3-70B-Instruct-Turbo", {}, True),
    ],
)
def test_request_body_per_host(model, extra, json_mode):
    """The exact body a player call posts to each host, prefix stripped."""
    provider = BY_NAME[split_model(model)[0]]
    server = Server((200, _ok(wire_model(model))))
    resp = _client(provider, server).complete(
        model=model, system=SYSTEM, messages=USER, max_tokens=200, temperature=0.8, json_only=True
    )
    req = server.requests[0]
    assert str(req.url) == provider.base_url + "/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test-secret"
    body = json.loads(req.content)
    expected = {
        "model": wire_model(model),
        "messages": [
            {"role": "system", "content": "You are Thorin." + JSON_ONLY_SUFFIX},
            {"role": "user", "content": USER[0]["content"]},
        ],
        "max_tokens": 200,
        "temperature": 0.8,
        **extra,
    }
    if json_mode:
        expected["response_format"] = {"type": "json_object"}
    assert body == expected
    assert ":" not in body["model"]
    for stray in ("thinking", "reasoning_effort", "max_completion_tokens"):
        assert stray not in body
    # the response keeps the full seat id, which is the price-row key
    assert resp.model == model
    assert has_price(resp.model)


def test_compat_response_keeps_the_explicit_id_but_the_servers_bare_one():
    p = BY_NAME["deepinfra"]
    server = Server((200, _ok("deepseek-ai/DeepSeek-V3.2-0102")), (200, _ok("gpt-5.4-nano-2026")))
    c = _client(p, server)
    a = c.complete(model="deepinfra:deepseek-ai/DeepSeek-V3.2", system="s", messages=USER, max_tokens=5)
    assert a.model == "deepinfra:deepseek-ai/DeepSeek-V3.2"
    b = c.complete(model="gpt-5.4-nano", system="s", messages=USER, max_tokens=5)
    assert b.model == "gpt-5.4-nano-2026"  # bare ids keep the server's string, as before


def test_missing_host_keys_are_named():
    for name, var in (("siliconflow", "SILICONFLOW_API_KEY"), ("deepinfra", "DEEPINFRA_API_KEY")):
        with pytest.raises(LLMError) as ei:
            OpenAICompatClient(BY_NAME[name], "")
        assert var in str(ei.value)
        r = RouterClient(env={k: "k" for k in ALL_KEYS if k != var})
        with pytest.raises(LLMError) as ei:
            r.complete(model=SAMPLE_MODEL[name], system="s", messages=USER, max_tokens=5)
        assert var in str(ei.value) and name in str(ei.value)


def test_json_response_format_only_when_asked():
    p = BY_NAME["openai"]
    server = Server((200, _ok("gpt-5.4-nano")), (200, _ok("gpt-5.4-nano")))
    c = _client(p, server)
    c.complete(model="gpt-5.4-nano", system="s", messages=USER, max_tokens=50, json_only=False)
    c.complete(model="gpt-5.4-nano", system="s", messages=USER, max_tokens=50, json_only=True)
    plain, jsonish = server.bodies()
    assert "response_format" not in plain
    assert not plain["messages"][0]["content"].endswith(JSON_ONLY_SUFFIX)
    assert jsonish["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "model, expect",
    [
        # openai: reasoning family drops temperature, turns effort down
        ("gpt-5", {"reasoning_effort": "minimal"}),
        ("gpt-5-nano", {"reasoning_effort": "minimal"}),
        ("gpt-5.4-nano", {"reasoning_effort": "none"}),
        ("gpt-5.5", {"reasoning_effort": "none"}),
        ("gpt-6-astra", {"reasoning_effort": "minimal"}),
        # xai: reasoning models get low effort, temperature kept
        ("grok-4.6", {"temperature": 0.8, "reasoning_effort": "low"}),
        ("grok-4.3", {"temperature": 0.8}),
        # gemini: 2.5 flash can switch thinking off, the rest go to minimal
        ("gemini-2.5-flash", {"temperature": 0.8, "reasoning_effort": "none"}),
        ("gemini-2.5-flash-lite", {"temperature": 0.8, "reasoning_effort": "none"}),
        ("gemini-2.5-pro", {"temperature": 0.8, "reasoning_effort": "minimal"}),
        ("gemini-3.8-flash", {"temperature": 0.8, "reasoning_effort": "minimal"}),
        # deepseek: thinking is on by default -> off; temperature kept
        ("deepseek-v4-flash", {"temperature": 0.8, "thinking": {"type": "disabled"}}),
        # mistral and unknowns: plain sampling
        ("mistral-small-latest", {"temperature": 0.8}),
        ("something-else", {"temperature": 0.8}),
    ],
)
def test_compat_params_table(model, expect):
    assert compat_params_for(model, temperature=0.8) == expect


@pytest.mark.parametrize(
    "model, expect",
    [
        # siliconflow: enable_thinking defaults to true on Qwen3 and DeepSeek-V3.1/V3.2
        ("siliconflow:Qwen/Qwen3-32B", {"temperature": 0.8, "enable_thinking": False}),
        ("siliconflow:Qwen/Qwen3-14B", {"temperature": 0.8, "enable_thinking": False}),
        ("siliconflow:deepseek-ai/DeepSeek-V3.2", {"temperature": 0.8, "enable_thinking": False}),
        ("siliconflow:deepseek-ai/DeepSeek-V3", {"temperature": 0.8}),
        ("siliconflow:Qwen/Qwen3.6-27B", {"temperature": 0.8}),  # not on the enable_thinking list
        # deepinfra: no documented thinking control for these ids -> plain sampling
        ("deepinfra:Qwen/Qwen3-32B", {"temperature": 0.8}),
        ("deepinfra:meta-llama/Llama-3.3-70B-Instruct-Turbo", {"temperature": 0.8}),
        # DeepSeek's own `thinking` field must not leak onto a host serving deepseek-ai/...
        ("deepinfra:deepseek-ai/DeepSeek-V3.2", {"temperature": 0.8}),
        # ...but the explicit form of a prefix-routed row keeps that row's rule
        ("deepseek:deepseek-v4-flash", {"temperature": 0.8, "thinking": {"type": "disabled"}}),
        ("openai:gpt-5.4-nano", {"reasoning_effort": "none"}),
    ],
)
def test_compat_params_for_the_explicit_form(model, expect):
    assert compat_params_for(model, temperature=0.8) == expect


def test_compat_params_returns_fresh_dicts():
    a = compat_params_for("deepseek-v4-flash", temperature=0.5)
    a["thinking"]["type"] = "mutated"
    assert compat_params_for("deepseek-v4-flash", temperature=0.5)["thinking"] == {"type": "disabled"}


# --- usage mapping ---------------------------------------------------------


def test_usage_maps_cached_tokens_out_of_prompt_tokens():
    p = BY_NAME["openai"]
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 30},
    }
    server = Server((200, _ok("gpt-5.4-nano", usage=usage)))
    resp = _client(p, server).complete(model="gpt-5.4-nano", system="s", messages=USER, max_tokens=50)
    assert (resp.input_tokens, resp.cache_read_tokens, resp.output_tokens) == (70, 30, 20)
    assert resp.cache_write_tokens == 0
    assert resp.model == "gpt-5.4-nano"
    # and the ledger prices it at the model's rows (0.1x for cached input)
    led = Ledger()
    usd = led.add("player:pc_1", resp)
    pin, pout = PRICES["gpt-5.4-nano"]
    assert usd == pytest.approx((70 * pin + 20 * pout + 30 * pin * 0.1) / 1e6)


def test_usage_maps_deepseek_cache_hit_miss():
    p = BY_NAME["deepseek"]
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_cache_hit_tokens": 40,
        "prompt_cache_miss_tokens": 60,
    }
    server = Server((200, _ok("deepseek-v4-flash", usage=usage)))
    resp = _client(p, server).complete(model="deepseek-v4-flash", system="s", messages=USER, max_tokens=50)
    assert (resp.input_tokens, resp.cache_read_tokens) == (60, 40)


def test_missing_usage_and_null_content_are_zero_and_empty():
    p = BY_NAME["mistral"]
    body = {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}
    server = Server((200, body))
    resp = _client(p, server).complete(model="mistral-small-latest", system="s", messages=USER, max_tokens=5)
    assert resp.text == ""
    assert resp.input_tokens == resp.output_tokens == 0
    assert resp.stop_reason == "length"


# --- retry policy ----------------------------------------------------------


def test_retries_429_then_succeeds():
    p = BY_NAME["xai"]
    server = Server((429, {"error": "slow down"}), (200, _ok("grok-4.3")))
    resp = _client(p, server).complete(model="grok-4.3", system="s", messages=USER, max_tokens=50)
    assert len(server.requests) == 2
    assert resp.text == '{"action": "a1"}'


def test_retries_timeouts_then_succeeds():
    p = BY_NAME["gemini"]
    server = Server(httpx.ReadTimeout("slow"), (200, _ok("gemini-2.5-flash")))
    resp = _client(p, server).complete(model="gemini-2.5-flash", system="s", messages=USER, max_tokens=50)
    assert len(server.requests) == 2 and resp.text


def test_gives_up_after_three_5xx():
    p = BY_NAME["mistral"]
    server = Server((500, {}), (502, {}), (503, {}), (200, _ok("m")))
    with pytest.raises(LLMError) as ei:
        _client(p, server).complete(model="mistral-small-latest", system="s", messages=USER, max_tokens=50)
    assert len(server.requests) == 3
    assert "mistral" in str(ei.value) and "503" in str(ei.value)


def test_400_raises_at_once_and_never_leaks_the_key():
    p = BY_NAME["openai"]
    server = Server((400, {"error": {"message": "Unsupported parameter: temperature"}}))
    with pytest.raises(LLMError) as ei:
        _client(p, server, key="sk-live-DO-NOT-LEAK").complete(
            model="gpt-5.4-nano", system="s", messages=USER, max_tokens=50
        )
    assert len(server.requests) == 1
    msg = str(ei.value)
    assert "Unsupported parameter" in msg and "400" in msg
    assert "DO-NOT-LEAK" not in msg


def test_compat_client_requires_a_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(LLMError) as ei:
        OpenAICompatClient(BY_NAME["xai"])
    assert "XAI_API_KEY" in str(ei.value)


def test_compat_client_refuses_the_anthropic_row():
    with pytest.raises(LLMError):
        OpenAICompatClient(BY_NAME["anthropic"], "k")


# --- router ----------------------------------------------------------------


class Recorder:
    def __init__(self, name):
        self.name = name
        self.models = []

    def complete(self, *, model, system, messages, max_tokens, temperature=0.7, json_only=False):
        self.models.append(model)
        return LLMResponse("{}", 1, 1, 0, 0, model, "end_turn")


def _router(env=None, **clients):
    return RouterClient(env=env or {}, clients=clients)


def test_router_routes_each_model_to_its_provider():
    a, x, d = Recorder("anthropic"), Recorder("xai"), Recorder("deepseek")
    r = _router(anthropic=a, xai=x, deepseek=d)
    for m in ("claude-sonnet-5", "grok-4.3", "deepseek-v4-flash", "claude-haiku-4-5-20251001"):
        r.complete(model=m, system="s", messages=USER, max_tokens=5)
    assert a.models == ["claude-sonnet-5", "claude-haiku-4-5-20251001"]
    assert x.models == ["grok-4.3"] and d.models == ["deepseek-v4-flash"]


def test_router_unknown_prefix_names_the_known_ones():
    with pytest.raises(LLMError) as ei:
        _router().complete(model="llama-3", system="s", messages=USER, max_tokens=5)
    assert "llama-3" in str(ei.value) and "grok-" in str(ei.value)
    assert "provider:model" in str(ei.value)


def test_router_routes_the_explicit_form_and_strips_it_only_for_the_sdk():
    a, sf, di, ds = Recorder("anthropic"), Recorder("siliconflow"), Recorder("deepinfra"), Recorder("deepseek")
    r = _router(anthropic=a, siliconflow=sf, deepinfra=di, deepseek=ds)
    out = {}
    for m in (
        "anthropic:claude-sonnet-5",
        "siliconflow:deepseek-ai/DeepSeek-V3.2",  # NOT DeepSeek's API
        "deepinfra:Qwen/Qwen3-32B",
        "deepseek:deepseek-v4-flash",
        "deepseek-v4-flash",
    ):
        out[m] = r.complete(model=m, system="s", messages=USER, max_tokens=5)
    assert a.models == ["claude-sonnet-5"]  # the native SDK never sees the prefix
    assert sf.models == ["siliconflow:deepseek-ai/DeepSeek-V3.2"]  # compat clients strip it themselves
    assert di.models == ["deepinfra:Qwen/Qwen3-32B"]
    assert ds.models == ["deepseek:deepseek-v4-flash", "deepseek-v4-flash"]
    # every response carries the seat id as configured, so the ledger keys on it
    for m, resp in out.items():
        assert resp.model == m


def test_router_unknown_provider_name_is_named_not_prefix_routed():
    with pytest.raises(LLMError) as ei:
        _router().complete(model="nope:gpt-5.4-nano", system="s", messages=USER, max_tokens=5)
    msg = str(ei.value)
    assert "unknown provider 'nope'" in msg and "deepinfra" in msg and "openai" in msg


def test_router_builds_host_clients_with_their_own_keys():
    built = []

    def factory(provider, key):
        built.append((provider.name, key))
        return Recorder(provider.name)

    r = RouterClient(env={"SILICONFLOW_API_KEY": "sk-sf", "DEEPINFRA_API_KEY": "sk-di"}, factory=factory)
    r.complete(model="deepinfra:Qwen/Qwen3-32B", system="s", messages=USER, max_tokens=5)
    r.complete(model="siliconflow:Qwen/Qwen3-32B", system="s", messages=USER, max_tokens=5)
    r.complete(model="deepinfra:deepseek-ai/DeepSeek-V3.2", system="s", messages=USER, max_tokens=5)
    assert built == [("deepinfra", "sk-di"), ("siliconflow", "sk-sf")]
    for name in ("siliconflow", "deepinfra"):
        from llm.router import _default_factory

        c = _default_factory(BY_NAME[name], "k")
        assert isinstance(c, OpenAICompatClient) and c.provider.name == name


def test_router_missing_key_names_the_env_var():
    r = RouterClient(env={"ANTHROPIC_API_KEY": "k"})
    with pytest.raises(LLMError) as ei:
        r.complete(model="mistral-small-latest", system="s", messages=USER, max_tokens=5)
    assert "MISTRAL_API_KEY" in str(ei.value) and "mistral" in str(ei.value)


def test_router_builds_clients_lazily_with_the_right_key():
    built = []

    def factory(provider, key):
        built.append((provider.name, key))
        return Recorder(provider.name)

    r = RouterClient(env={"OPENAI_API_KEY": "sk-o", "GEMINI_API_KEY": "sk-g"}, factory=factory)
    assert built == []
    r.complete(model="gpt-5.4-nano", system="s", messages=USER, max_tokens=5)
    r.complete(model="gpt-5-nano", system="s", messages=USER, max_tokens=5)
    r.complete(model="gemini-2.5-flash", system="s", messages=USER, max_tokens=5)
    assert built == [("openai", "sk-o"), ("gemini", "sk-g")]


def test_default_factory_uses_the_sdk_for_anthropic_and_httpx_for_the_rest():
    from llm.router import _default_factory

    c = _default_factory(BY_NAME["xai"], "k")
    assert isinstance(c, OpenAICompatClient) and c.provider.name == "xai"
    # anthropic path builds the SDK client without a network call
    a = _default_factory(BY_NAME["anthropic"], "sk-ant-test")
    assert type(a).__name__ == "AnthropicClient"


# --- preflight / fail-fast --------------------------------------------------

ALL_KEYS = {p.key_env: "k" for p in PROVIDERS}


def test_preflight_passes_priced_seats_with_keys():
    RouterClient(env=ALL_KEYS).preflight(
        {"dm": "claude-sonnet-5", "summary": "claude-haiku-4-5-20251001", "player:pc_1": "grok-4.3"}
    )


def test_preflight_missing_key_names_seat_and_var():
    env = dict(ALL_KEYS)
    del env["GEMINI_API_KEY"]
    with pytest.raises(LLMError) as ei:
        RouterClient(env=env).preflight({"player:pc_2": "gemini-2.5-flash"})
    assert "player:pc_2" in str(ei.value) and "GEMINI_API_KEY" in str(ei.value)


def test_preflight_unpriced_model_fails_unless_allowed():
    env = dict(ALL_KEYS)
    with pytest.raises(LLMError) as ei:
        RouterClient(env=env).preflight({"player:pc_3": "grok-99-experimental"})
    assert "no price row" in str(ei.value) and "DND_ALLOW_UNPRICED" in str(ei.value)
    RouterClient(env={**env, "DND_ALLOW_UNPRICED": "1"}).preflight({"player:pc_3": "grok-99-experimental"})
    RouterClient(env=env).preflight({"player:pc_3": "grok-99-experimental"}, allow_unpriced=True)


def test_all_keys_includes_both_host_vars():
    assert {"SILICONFLOW_API_KEY", "DEEPINFRA_API_KEY"} <= set(ALL_KEYS)


def test_preflight_passes_host_seats_with_keys_and_prices():
    RouterClient(env=ALL_KEYS).preflight(
        {
            "dm": "claude-sonnet-5",
            "player:pc_1": "siliconflow:Qwen/Qwen3-32B",
            "player:pc_2": "deepinfra:meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "player:pc_3": "deepinfra:deepseek-ai/DeepSeek-V3.2",
            "player:pc_4": "siliconflow:deepseek-ai/DeepSeek-V3",
        }
    )


def test_preflight_namespaced_id_without_a_provider_says_which_form_and_hosts():
    with pytest.raises(LLMError) as ei:
        RouterClient(env=ALL_KEYS).preflight({"player:pc_1": "Qwen/Qwen3-32B"})
    msg = str(ei.value)
    assert "player:pc_1" in msg and "'Qwen/Qwen3-32B'" in msg
    assert "provider:model" in msg
    assert "siliconflow:Qwen/Qwen3-32B" in msg and "deepinfra:Qwen/Qwen3-32B" in msg
    assert "hosts: siliconflow, deepinfra" in msg
    # a bare deepseek-ai/... id must not silently become DeepSeek's own API
    with pytest.raises(LLMError) as ei:
        RouterClient(env=ALL_KEYS).preflight({"player:pc_2": "deepseek-ai/DeepSeek-V3.2"})
    assert "deepinfra:deepseek-ai/DeepSeek-V3.2" in str(ei.value)


def test_preflight_missing_host_keys_names_both_vars_at_once():
    env = {k: "k" for k in ALL_KEYS if k not in ("SILICONFLOW_API_KEY", "DEEPINFRA_API_KEY")}
    with pytest.raises(LLMError) as ei:
        RouterClient(env=env).preflight(
            {"player:pc_1": "siliconflow:Qwen/Qwen3-32B", "player:pc_2": "deepinfra:Qwen/Qwen3-32B"}
        )
    msg = str(ei.value)
    assert "player:pc_1" in msg and "SILICONFLOW_API_KEY" in msg
    assert "player:pc_2" in msg and "DEEPINFRA_API_KEY" in msg


def test_preflight_unpriced_host_model_is_refused_by_its_full_key():
    with pytest.raises(LLMError) as ei:
        RouterClient(env=ALL_KEYS).preflight({"player:pc_1": "deepinfra:meta-llama/Llama-3.3-70B-Instruct"})
    assert "no price row" in str(ei.value) and "deepinfra:meta-llama/Llama-3.3-70B-Instruct" in str(ei.value)
    # case matters: the part after the colon is the host's id verbatim
    with pytest.raises(LLMError):
        RouterClient(env=ALL_KEYS).preflight({"player:pc_1": "deepinfra:qwen/qwen3-32b"})


def test_preflight_reports_every_problem_at_once():
    with pytest.raises(LLMError) as ei:
        RouterClient(env={}).preflight({"dm": "claude-sonnet-5", "player:pc_1": "llama-3"})
    msg = str(ei.value)
    assert "ANTHROPIC_API_KEY" in msg and "llama-3" in msg


# --- prices ----------------------------------------------------------------


def test_price_lookup_prefers_the_longest_boundary_prefix():
    assert price_for("gpt-5.4-nano") == PRICES["gpt-5.4-nano"]
    assert price_for("gpt-5.4-nano-2026-01-01") == PRICES["gpt-5.4-nano"]
    assert price_for("gpt-5") == PRICES["gpt-5"]
    assert price_for("claude-sonnet-5-20260301") == PRICES["claude-sonnet-5"]
    assert has_price("grok-4.3") and has_price("grok-4.3-fast")
    assert not has_price("grok-43") and not has_price("gpt-55") and not has_price("nope")


def test_cache_read_price_uses_provider_rate_when_it_is_not_a_tenth():
    assert cache_read_price_for("grok-4.6") == 0.5
    assert cache_read_price_for("gpt-5.4-nano") == pytest.approx(PRICES["gpt-5.4-nano"][0] * 0.1)
    assert cache_read_price_for("claude-sonnet-5") == pytest.approx(0.2)


def test_price_lookup_by_the_full_provider_model_key():
    assert has_price("deepinfra:Qwen/Qwen3-32B") and has_price("siliconflow:Qwen/Qwen3-32B")
    assert price_for("deepinfra:Qwen/Qwen3-32B") == (0.08, 0.28)
    assert price_for("siliconflow:Qwen/Qwen3-32B") == (0.14, 0.57)
    assert price_for("deepinfra:deepseek-ai/DeepSeek-V3.2") == (0.26, 0.38)
    assert price_for("siliconflow:deepseek-ai/DeepSeek-V3.2") == (0.27, 0.42)
    assert price_for("deepinfra:meta-llama/Llama-3.3-70B-Instruct-Turbo") == (0.10, 0.32)
    # the bare host id is not a key: it is the seat id that is priced
    assert not has_price("Qwen/Qwen3-32B") and not has_price("deepseek-ai/DeepSeek-V3.2")
    # the host key never bleeds into DeepSeek's own rows or vice versa
    assert price_for("deepinfra:deepseek-ai/DeepSeek-V3.2") != price_for("deepseek-v4-flash")
    # a dated/suffixed host id still prices by the boundary-prefix rule
    # A dated or suffixed host variant has no verified rate of its own and must
    # NOT inherit the base row (Codex round 3): host keys match exactly.
    assert not has_price("deepinfra:Qwen/Qwen3-32B-2026")
    # V3.2 is its own row, longer than the V3 row it also prefix-matches
    assert price_for("siliconflow:deepseek-ai/DeepSeek-V3") == (0.25, 1.0)


def test_cache_read_price_for_hosts():
    assert cache_read_price_for("deepinfra:deepseek-ai/DeepSeek-V3.2") == 0.13
    # siliconflow reports no cache-hit usage; its rate is the default 0.1x
    assert cache_read_price_for("siliconflow:deepseek-ai/DeepSeek-V3.2") == pytest.approx(0.027)


def test_ledger_prices_a_host_seat_by_its_full_key():
    p = BY_NAME["deepinfra"]
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 30}}
    server = Server((200, _ok("deepseek-ai/DeepSeek-V3.2", usage=usage)))
    resp = _client(p, server).complete(
        model="deepinfra:deepseek-ai/DeepSeek-V3.2", system="s", messages=USER, max_tokens=50
    )
    assert (resp.input_tokens, resp.cache_read_tokens, resp.output_tokens) == (70, 30, 20)
    usd = Ledger().add("player:pc_1", resp)
    assert usd == pytest.approx((70 * 0.26 + 20 * 0.38 + 30 * 0.13) / 1e6)


def test_every_sample_model_and_every_default_is_priced():
    for m in SAMPLE_MODEL.values():
        assert has_price(m), m
    assert has_price("claude-sonnet-5") and has_price("claude-haiku-4-5-20251001")


# --- per-seat models through the game ---------------------------------------


class SeatRecorder:
    """The deterministic mock, plus a record of which model each call named."""

    def __init__(self, seed):
        self.inner = MockLLMClient(seed=seed)
        self.models = []

    def complete(self, **kw):
        self.models.append(kw["model"])
        return self.inner.complete(**kw)


def _cfg_with_seat(cfg, pid="pc_2", model="grok-4.3"):
    raw = cfg.to_dict()
    for spec in raw["party"]:
        if spec["id"] == pid:
            spec["model"] = model
    return GameConfig.from_dict(raw)


def test_config_seat_models(cfg):
    seats = cfg.seat_models()
    assert seats["dm"] == cfg.dm_model and seats["summary"] == cfg.summary_model
    assert seats["player:pc_2"] == cfg.player_model
    c2 = _cfg_with_seat(cfg)
    assert c2.seat_models()["player:pc_2"] == "grok-4.3"
    assert c2.seat_models()["player:pc_1"] == cfg.player_model
    assert c2.player_model_for({"id": "pc_2", "model": "grok-4.3"}) == "grok-4.3"
    assert c2.player_model_for({"id": "pc_9"}) == cfg.player_model


def test_per_seat_model_reaches_the_player_agent_and_snapshot(cfg):
    c2 = _cfg_with_seat(cfg)
    client = SeatRecorder(cfg.seed)
    game = Game(c2, client, EventBus(), engine=eng)
    game.run()
    assert game.status == "finished"
    assert game.players["pc_2"].model == "grok-4.3"
    assert game.players["pc_1"].model == cfg.player_model
    assert "grok-4.3" in client.models and cfg.player_model in client.models
    assert set(client.models) == {cfg.dm_model, cfg.player_model, "grok-4.3"} | {cfg.summary_model}
    snap = game.snapshot()
    assert snap["models"]["players"]["pc_2"] == "grok-4.3"
    assert snap["models"]["players"]["pc_1"] == cfg.player_model
    assert snap["models"]["dm"] == cfg.dm_model
    # the ledger keeps the seat as its own row, priced at the seat's model
    assert game.ledger.by_role["player:pc_2"]["calls"] > 0


def test_host_seat_reaches_the_snapshot_and_ledger_in_full_form(cfg):
    model = "deepinfra:Qwen/Qwen3-32B"
    c2 = _cfg_with_seat(cfg, model=model)
    assert c2.seat_models()["player:pc_2"] == model
    client = SeatRecorder(cfg.seed)
    game = Game(c2, client, EventBus(), engine=eng)
    game.run()
    assert game.status == "finished"
    assert game.players["pc_2"].model == model
    assert model in client.models
    snap = game.snapshot()
    assert snap["models"]["players"]["pc_2"] == model  # the seat id as configured, prefix and all
    row = game.ledger.by_role["player:pc_2"]
    assert row["calls"] > 0 and row["usd"] > 0


def test_seat_override_does_not_change_the_mock_transcript(cfg):
    """Same seed, same play — only the `cost` lines move, because pc_2 is now
    priced at grok-4.3's rate rather than Haiku's (the ledger doing its job)."""

    def events_for(c):
        out = []
        game = Game(c, MockLLMClient(seed=c.seed), EventBus(), on_event=lambda e: out.append((e.kind, e.text)), engine=eng)
        game.run()
        return out

    plain, seated = events_for(cfg), events_for(_cfg_with_seat(cfg))
    assert len(plain) == len(seated) > 50
    assert [e for e in plain if e[0] != "cost"] == [e for e in seated if e[0] != "cost"]
    assert [e for e in plain if e[0] == "cost"] != [e for e in seated if e[0] == "cost"]


# --- factory / cli fail-fast --------------------------------------------------


def _live_config(**extra):
    raw = json.loads(open("examples/goblin_ambush.json").read())
    raw["tempo_ms"] = 0
    raw.update(extra)
    return raw


def test_factory_live_mode_fails_fast_on_unpriced_seat(monkeypatch):
    from web.factory import default_game_factory

    for var, val in ALL_KEYS.items():
        monkeypatch.setenv(var, val)
    monkeypatch.delenv("DND_SIM_MOCK", raising=False)
    monkeypatch.delenv("DND_ALLOW_UNPRICED", raising=False)
    raw = _live_config()
    raw["party"][0]["model"] = "gpt-unpriced-9"
    with pytest.raises(RuntimeError) as ei:
        default_game_factory(raw, lambda _e: None)
    assert "player:pc_1" in str(ei.value) and "no price row" in str(ei.value)
    monkeypatch.setenv("DND_ALLOW_UNPRICED", "1")
    game, bus = default_game_factory(raw, lambda _e: None)  # builds; never runs
    assert isinstance(game.client, RouterClient)
    assert game.cfg.player_model_for(raw["party"][0]) == "gpt-unpriced-9"


def test_factory_live_mode_fails_fast_on_missing_key(monkeypatch):
    from web.factory import default_game_factory

    for var in ALL_KEYS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("DND_SIM_MOCK", raising=False)
    raw = _live_config()
    raw["party"][1]["model"] = "mistral-small-latest"
    with pytest.raises(RuntimeError) as ei:
        default_game_factory(raw, lambda _e: None)
    assert "MISTRAL_API_KEY" in str(ei.value) and "DND_SIM_MOCK=1" in str(ei.value)


def test_factory_live_mode_names_the_host_key_and_the_form(monkeypatch):
    from web.factory import default_game_factory

    for var, val in ALL_KEYS.items():
        monkeypatch.setenv(var, val)
    monkeypatch.delenv("DND_SIM_MOCK", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY")
    raw = _live_config()
    raw["party"][0]["model"] = "siliconflow:Qwen/Qwen3-32B"
    raw["party"][1]["model"] = "Qwen/Qwen3-32B"
    with pytest.raises(RuntimeError) as ei:
        default_game_factory(raw, lambda _e: None)
    msg = str(ei.value)
    assert "player:pc_1" in msg and "SILICONFLOW_API_KEY" in msg
    assert "player:pc_2" in msg and "provider:model" in msg and "deepinfra:Qwen/Qwen3-32B" in msg
    monkeypatch.setenv("SILICONFLOW_API_KEY", "k")
    raw["party"][1]["model"] = "deepinfra:Qwen/Qwen3-32B"
    game, _bus = default_game_factory(raw, lambda _e: None)  # builds; never runs
    assert isinstance(game.client, RouterClient)
    assert game.cfg.seat_models()["player:pc_2"] == "deepinfra:Qwen/Qwen3-32B"


def test_factory_mock_mode_ignores_keys_and_prices(monkeypatch):
    from web.factory import default_game_factory

    for var in ALL_KEYS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DND_SIM_MOCK", "1")
    raw = _live_config()
    raw["party"][0]["model"] = "gpt-unpriced-9"
    game, _bus = default_game_factory(raw, lambda _e: None)
    assert isinstance(game.client, MockLLMClient)


def test_cli_live_mode_exits_2_with_the_missing_var(tmp_path, monkeypatch, capsys):
    from orchestrator import cli

    for var in ALL_KEYS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("DND_SIM_MOCK", raising=False)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(_live_config()))
    code = cli.main(["--config", str(path), "--live", "--tempo", "0"])
    assert code == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_cli_mock_run_is_byte_identical_across_runs_and_seat_overrides(tmp_path, monkeypatch, capsys):
    from orchestrator import cli

    raw = _live_config()
    raw["scenario"]["max_scenes"] = 1
    raw["scenario"]["beats_per_scene"] = 0
    raw["max_rounds_per_combat"] = 3
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps(raw))
    raw["party"][2]["model"] = "deepseek-v4-flash"
    seated = tmp_path / "seated.json"
    seated.write_text(json.dumps(raw))

    def run(path):
        code = cli.main(["--config", str(path), "--mock", "--seed", "42", "--tempo", "0", "--json"])
        assert code == 0
        # the first event carries a fresh uuid game_id; everything else must match
        return re.sub(r'"game_id": "[0-9a-f]+"', '"game_id": "X"', capsys.readouterr().out)

    a, b, c = run(plain), run(plain), run(seated)
    assert a.count("\n") > 5
    assert a == b  # byte-identical across runs

    def sans_cost(out):
        return [ln for ln in out.splitlines() if '"kind": "cost"' not in ln]

    # a seat override changes only the ledger lines (pc_3 priced as deepseek)
    assert sans_cost(a) == sans_cost(c)
    assert len(a.splitlines()) == len(c.splitlines())


# -- explicit provider:model on a prefix-routed provider prices as the bare id --


@pytest.mark.parametrize(
    "explicit,bare",
    [
        ("openai:gpt-5.4-nano", "gpt-5.4-nano"),
        ("deepseek:deepseek-v4-flash", "deepseek-v4-flash"),
        ("anthropic:claude-sonnet-5", "claude-sonnet-5"),
    ],
)
def test_explicit_form_on_prefix_provider_prices_like_bare_id(explicit, bare):
    from llm.cost import cache_read_price_for, has_price, price_for

    assert has_price(bare)
    assert has_price(explicit)
    assert price_for(explicit) == price_for(bare)
    assert cache_read_price_for(explicit) == cache_read_price_for(bare)


def test_host_qualified_keys_do_not_fall_back_to_bare_rows():
    from llm.cost import _DEFAULT_PRICE, has_price, price_for

    # Priced only under its host key: the bare id must not answer for it.
    assert has_price("deepinfra:deepseek-ai/DeepSeek-V3.2")
    assert not has_price("deepseek-ai/DeepSeek-V3.2")
    # A host id with no row stays unpriced even though DeepSeek's own API
    # prices a similarly named model.
    assert not has_price("deepinfra:deepseek-v4-flash")
    assert price_for("deepinfra:deepseek-v4-flash") == _DEFAULT_PRICE


def test_unknown_provider_prefix_never_prices():
    from llm.cost import has_price

    assert not has_price("nosuch:gpt-5.4-nano")


@pytest.mark.parametrize(
    "mismatched",
    ["anthropic:gpt-5.4-nano", "openai:deepseek-v4-flash", "deepseek:claude-sonnet-5"],
)
def test_explicit_provider_that_does_not_route_the_bare_id_is_unpriced(mismatched):
    from llm.cost import _DEFAULT_PRICE, has_price, price_for

    assert not has_price(mismatched)
    assert price_for(mismatched) == _DEFAULT_PRICE


@pytest.mark.parametrize(
    "unpriced_variant",
    [
        "siliconflow:deepseek-ai/DeepSeek-V3.1",
        "siliconflow:deepseek-ai/DeepSeek-V3-0324",
        "deepinfra:deepseek-ai/DeepSeek-V3.2-Exp",
        "deepinfra:Qwen/Qwen3-32B-Instruct",
    ],
)
def test_host_qualified_keys_match_exactly_never_by_prefix(unpriced_variant):
    from llm.cost import _DEFAULT_PRICE, has_price, price_for

    assert not has_price(unpriced_variant)
    assert price_for(unpriced_variant) == _DEFAULT_PRICE


def test_explicit_prefix_provider_still_honours_dated_ids():
    from llm.cost import has_price, price_for

    # Dated Anthropic ids keep boundary matching through the bare-id fallback.
    assert has_price("anthropic:claude-haiku-4-5-20251001")
    assert price_for("anthropic:claude-haiku-4-5-20251001") == price_for("claude-haiku-4-5-20251001")


def test_unpriced_host_id_that_prefixes_a_priced_one_gets_the_default_rate():
    from llm.cost import _DEFAULT_PRICE, cache_read_price_for, has_price, price_for

    m = "deepinfra:meta-llama/Llama-3.3-70B-Instruct"  # only the -Turbo id has a row
    assert not has_price(m)
    assert price_for(m) == _DEFAULT_PRICE
    assert cache_read_price_for(m) == _DEFAULT_PRICE[0] * 0.1


def test_bare_undated_id_still_borrows_its_dated_row():
    from llm.cost import price_for

    assert price_for("claude-haiku-4-5") == price_for("claude-haiku-4-5-20251001")


@pytest.mark.parametrize(
    "spelling",
    ["DeepInfra:Qwen/Qwen3-32B", "deepinfra: Qwen/Qwen3-32B ", " DEEPINFRA :Qwen/Qwen3-32B"],
)
def test_host_price_lookup_uses_the_normalised_key(spelling):
    from llm.cost import has_price, price_for

    assert has_price(spelling)
    assert price_for(spelling) == price_for("deepinfra:Qwen/Qwen3-32B")


def test_host_price_lookup_keeps_case_after_the_colon():
    from llm.cost import has_price

    assert not has_price("deepinfra:qwen/qwen3-32b")


@pytest.mark.parametrize(
    "spelling",
    ["siliconflow: Qwen/Qwen3-32B", "siliconflow :Qwen/Qwen3-32B", "SiliconFlow:Qwen/Qwen3-32B "],
)
def test_compat_rules_match_the_normalised_host_spelling(spelling):
    from llm.providers import compat_params_for

    assert compat_params_for(spelling, temperature=0.8) == compat_params_for(
        "siliconflow:Qwen/Qwen3-32B", temperature=0.8
    )
    assert compat_params_for(spelling, temperature=0.8)["enable_thinking"] is False
