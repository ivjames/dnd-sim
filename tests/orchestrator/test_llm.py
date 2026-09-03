"""MockLLMClient, tolerant parsing, cost ledger, and the Anthropic retry policy."""

import json

import pytest

from agents.common import AgentOutputError, extract_json
from llm.client import (
    MODEL_RULES,
    AnthropicClient,
    LLMError,
    LLMResponse,
    MockLLMClient,
    request_params_for,
)
from llm.cost import PRICES, Ledger

SHAPES = [
    "player_action",
    "dm_monster_action",
    "dm_narration",
    "dm_adjudication",
    "summary",
    "scene_options",
    "scene_choice",
    "player_speech",
]

ACTION_BLOCK = """\
[a1] Attack Goblin 2 with Longsword (+5, 1d8+3)
[a2] Move up to your speed  needs=['path'] suggested=[(3, 4), (5, 6)]
[a3] End turn
"""


def _call(client, prompt, model="claude-haiku-4-5-20251001"):
    return client.complete(
        model=model,
        system=[{"type": "text", "text": "system"}],
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        json_only=True,
    )


@pytest.mark.parametrize("shape", SHAPES)
def test_mock_returns_valid_json_for_every_shape(shape):
    client = MockLLMClient(seed=3)
    for _ in range(20):
        resp = _call(client, f"{ACTION_BLOCK}\nRESPONSE_SHAPE: {shape}")
        obj = json.loads(resp.text)  # must parse without any tolerance
        assert isinstance(obj, dict)
        assert resp.input_tokens > 0 and resp.output_tokens > 0


def test_mock_picks_an_offered_action_id():
    client = MockLLMClient(seed=11)
    seen = set()
    for _ in range(40):
        obj = json.loads(_call(client, f"{ACTION_BLOCK}\nRESPONSE_SHAPE: player_action").text)
        assert obj["action"] in {"a1", "a2", "a3"}
        seen.add(obj["action"])
        assert len(obj["speech"].split()) <= 40
    # weighted toward acting, not ending the turn
    assert "a1" in seen


def test_mock_fills_needed_params_from_suggestions():
    client = MockLLMClient(seed=5)
    for _ in range(30):
        obj = json.loads(_call(client, f"{ACTION_BLOCK}\nRESPONSE_SHAPE: player_action").text)
        if obj["action"] == "a2":
            assert obj["params"]["path"], "move must carry a destination"
            return
    pytest.fail("never chose the move action in 30 draws")


def test_mock_is_seeded():
    a = [_call(MockLLMClient(seed=9), f"{ACTION_BLOCK}\nRESPONSE_SHAPE: player_action").text for _ in range(1)]
    c1, c2 = MockLLMClient(seed=9), MockLLMClient(seed=9)
    out1 = [_call(c1, f"{ACTION_BLOCK}\nRESPONSE_SHAPE: player_action").text for _ in range(10)]
    out2 = [_call(c2, f"{ACTION_BLOCK}\nRESPONSE_SHAPE: player_action").text for _ in range(10)]
    assert out1 == out2
    assert a[0] == out1[0]


def test_extract_json_is_tolerant():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('sure! {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}
    assert extract_json('{"speech": "he said \\"no\\" }"}')["speech"] == 'he said "no" }'
    with pytest.raises(AgentOutputError):
        extract_json("no json here")
    with pytest.raises(AgentOutputError):
        extract_json("")


# --- ledger ---------------------------------------------------------------


def _resp(model, tin=1000, tout=100, cr=0, cw=0):
    return LLMResponse("x", tin, tout, cr, cw, model, "end_turn")


def test_ledger_prices_and_rollup():
    led = Ledger()
    usd = led.add("dm", _resp("claude-sonnet-5", 1_000_000, 1_000_000))
    assert usd == pytest.approx(sum(PRICES["claude-sonnet-5"]))
    led.add("player:pc_1", _resp("claude-haiku-4-5-20251001", 1_000_000, 0))
    assert led.by_role["player:pc_1"]["usd"] == pytest.approx(1.0)
    assert led.total_usd == pytest.approx(13.0)
    d = led.to_dict()
    assert d["calls"] == 2 and d["by_role"]["dm"]["calls"] == 1


def test_ledger_cache_multipliers():
    led = Ledger()
    usd = led.add("dm", _resp("claude-sonnet-5", 0, 0, cr=1_000_000, cw=1_000_000))
    assert usd == pytest.approx(2.0 * 0.1 + 2.0 * 1.25)


# --- anthropic client -----------------------------------------------------


class _Boom(Exception):
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.status_code = status


class _FakeSDK:
    """Stands in for anthropic.Anthropic: fails N times, then succeeds."""

    def __init__(self, failures=0, status=429):
        self.failures = failures
        self.status = status
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) <= self.failures:
            raise _Boom(self.status)
        return type(
            "R",
            (),
            {
                "content": [type("B", (), {"text": '{"ok": true}'})()],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 2,
                },
                "model": kw["model"],
                "stop_reason": "end_turn",
            },
        )()


def _client(sdk):
    return AnthropicClient(sdk=sdk, sleep=lambda _s: None)


def test_anthropic_caches_the_stable_system_block_and_adds_json_instruction():
    sdk = _FakeSDK()
    _client(sdk).complete(
        model="claude-sonnet-5",
        system="stable rules",
        messages=[{"role": "user", "content": "go"}],
        max_tokens=50,
        json_only=True,
    )
    blocks = sdk.calls[0]["system"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "single raw JSON object" in blocks[-1]["text"]


def test_anthropic_retries_429_then_succeeds():
    sdk = _FakeSDK(failures=2)
    resp = _client(sdk).complete(
        model="claude-sonnet-5", system="s", messages=[], max_tokens=10
    )
    assert len(sdk.calls) == 3
    assert resp.text == '{"ok": true}'
    assert (resp.cache_read_tokens, resp.cache_write_tokens) == (3, 2)


def test_anthropic_gives_up_after_three_attempts():
    sdk = _FakeSDK(failures=99, status=500)
    with pytest.raises(LLMError):
        _client(sdk).complete(model="claude-sonnet-5", system="s", messages=[], max_tokens=10)
    assert len(sdk.calls) == 3


def test_anthropic_does_not_retry_client_errors():
    sdk = _FakeSDK(failures=99, status=400)
    with pytest.raises(LLMError):
        _client(sdk).complete(model="claude-sonnet-5", system="s", messages=[], max_tokens=10)
    assert len(sdk.calls) == 1


# --- per-model request parameters ----------------------------------------


def _kwargs(model, temperature=0.8):
    sdk = _FakeSDK()
    _client(sdk).complete(
        model=model,
        system="stable rules",
        messages=[{"role": "user", "content": "go"}],
        max_tokens=50,
        temperature=temperature,
    )
    return sdk.calls[0]


def test_sonnet_5_gets_no_sampling_params_and_thinking_disabled():
    kw = _kwargs("claude-sonnet-5")
    assert "temperature" not in kw
    assert "top_p" not in kw and "top_k" not in kw
    assert kw["thinking"] == {"type": "disabled"}
    # max_tokens is still the tight per-call cap; nothing else is added
    assert kw["max_tokens"] == 50
    assert set(kw) == {"model", "system", "messages", "max_tokens", "thinking"}
    # cache_control placement is unchanged by the rule
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_haiku_4_5_keeps_temperature_and_sends_no_thinking_field():
    kw = _kwargs("claude-haiku-4-5-20251001", temperature=0.3)
    assert kw["temperature"] == 0.3
    assert "thinking" not in kw
    assert set(kw) == {"model", "system", "messages", "max_tokens", "temperature"}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize(
    "model, expect",
    [
        ("claude-sonnet-5", {"thinking": {"type": "disabled"}}),
        ("claude-opus-5", {"thinking": {"type": "disabled"}}),
        ("claude-opus-4-7", {"thinking": {"type": "disabled"}}),
        ("claude-opus-4-8", {"thinking": {"type": "disabled"}}),
        # Fable: thinking always on, "disabled" rejected -> omit the field too
        ("claude-fable-5", {}),
        ("claude-fable-5-1", {}),
        # everything else forwards temperature and leaves thinking out
        ("claude-haiku-4-5-20251001", {"temperature": 0.8}),
        ("claude-haiku-4-5", {"temperature": 0.8}),
        ("claude-sonnet-4-6", {"temperature": 0.8}),
        ("claude-opus-4-6", {"temperature": 0.8}),
        ("some-future-model", {"temperature": 0.8}),
    ],
)
def test_request_params_table(model, expect):
    assert request_params_for(model, temperature=0.8) == expect


def test_request_params_returns_a_fresh_thinking_dict():
    a = request_params_for("claude-sonnet-5", temperature=0.8)
    a["thinking"]["type"] = "mutated"
    assert request_params_for("claude-sonnet-5", temperature=0.8)["thinking"] == {"type": "disabled"}


def test_model_rules_prefixes_are_unambiguous():
    # every rule must be reachable: no earlier prefix may shadow a later one
    prefixes = [p for p, _s, _t in MODEL_RULES]
    for i, later in enumerate(prefixes):
        for earlier in prefixes[:i]:
            assert not later.startswith(earlier), f"{earlier!r} shadows {later!r}"


def test_mock_client_ignores_the_model_rule():
    # the mock is model-agnostic: same seed, same output whichever model is named
    a = _call(MockLLMClient(seed=4), f"{ACTION_BLOCK}\nRESPONSE_SHAPE: player_action", model="claude-sonnet-5")
    b = _call(MockLLMClient(seed=4), f"{ACTION_BLOCK}\nRESPONSE_SHAPE: player_action", model="claude-fable-5")
    assert a.text == b.text
