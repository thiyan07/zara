"""Pin: reasoning-model plan-glitch salvage + blocking budget (2026-10-22).

Live failure with nvidia/nemotron-3-ultra-550b-a55b on the REAL stack:
the plan once arrived as '{"type":"{"type":"response",…}' — a truncated
first attempt the model restarted mid-string. One malformed sample
failed the whole turn ("unreadable plan"). _salvage_action absorbs this
class ONLY by accepting a fully strict-parsed action object — it can
never widen execution.
"""
import pytest

from core.llm import LLMError
from core.providers import OpenAICompatibleProvider, _salvage_action


def _provider_with(content):
    p = OpenAICompatibleProvider("http://stub.local/v1", api_key="k",
                                 model="stub")
    p._post = lambda payload, timeout: {  # type: ignore[method-assign]
        "choices": [{"message": {"content": content}}],
        "usage": {}}  # type: ignore[misc]
    return p


def test_glitched_prefix_plan_is_salvaged():
    raw = '{"type":"{"type":"response","text":"Hello! How can I help you today?"}'
    a = _salvage_action(raw)
    assert a is not None and a.type == "response"
    assert a.text == "Hello! How can I help you today?"


def test_reason_survives_the_glitched_sample():
    out = _provider_with(
        '{"type":"{"type":"response","text":"fine"}').reason("s", "u", [])
    assert out.text == "fine"


def test_garbage_still_raises_malformed():
    with pytest.raises(LLMError):
        _provider_with("totally not json").reason("s", "u", [])
    with pytest.raises(LLMError):
        _provider_with('{"type":"tool_call"').reason("s", "u", [])
    assert _salvage_action("[]") is None
    assert _salvage_action("") is None


def test_blocking_budget_suits_reasoning_models():
    """nemotron-3-ultra burn tokens on the scratchpad before the plan;
    512 truncated it mid-object (live). The blocking payload must carry
    a 1024-token budget like the streaming path."""
    p = _provider_with('{"type":"response","text":"x"}')
    captured = {}
    orig = p._post

    def cap(payload, timeout):
        captured.update(payload)
        return orig(payload, timeout)

    p._post = cap  # type: ignore[method-assign]
    p.reason("s", "u", [])
    assert captured.get("max_tokens") == 1024
