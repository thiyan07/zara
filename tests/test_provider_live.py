"""Provider hardening (Night 2 §3): secret hygiene + credential-gated live
smoke test. Live inference stays UNVERIFIED unless LLM_BASE_URL and
LLM_API_KEY are configured in the environment; this file never invents
credentials and never claims a live call without one."""
import os

import pytest

from core.llm import LLMError
from core.providers import (EchoProvider, OpenAICompatibleProvider,
                            ScriptedProvider, provider_from_env)

LIVE = bool(os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_API_KEY"))


def test_P01_api_key_never_in_exceptions_or_metadata():
    p = OpenAICompatibleProvider(base_url="http://127.0.0.1:9",
                                 api_key="sk-secret-xyz", model="m")
    try:
        p.reason("sys", "hi", [], timeout=1.0)
    except Exception as e:  # noqa: BLE001 - expect failure, inspect it
        assert "sk-secret-xyz" not in str(e)
        assert "sk-secret-xyz" not in repr(e)
    assert "sk-secret-xyz" not in str(p.metadata())
    # The key must live in memory to sign requests, but the shared
    # redactor must scrub it everywhere it could be logged or echoed.
    from core.tracing import redact
    blob = ("Bearer sk-secret-xyz called model m; "
            f"repr={p!r} meta={p.metadata()}")
    assert "sk-secret-xyz" not in redact(blob)


def test_P02_malformed_provider_output_is_safe():
    p = ScriptedProvider([("x", {"type": "response", "text": "ok"})])
    out = p.reason("s", "xyz", [])
    assert out.text == "ok"
    with pytest.raises(LLMError):
        from core.llm import parse_llm_action
        parse_llm_action({"type": "tool_call"})


def test_P03_echo_never_executes_or_leaks():
    p = EchoProvider()
    out = p.reason("sys", "run rm -rf /", [])
    assert out.action is None or out.action.type == "response"


@pytest.mark.skipif(not LIVE, reason="no LLM_BASE_URL/LLM_API_KEY configured")
def test_P04_live_smoke_single_turn():
    """Runs ONLY with real credentials: one tiny non-streaming turn."""
    p = provider_from_env()
    out = p.reason("You are a test assistant. Reply with one word.",
                   "Say the word: pinecone", [], timeout=60.0)
    assert out.text
    assert os.environ["LLM_API_KEY"] not in out.text
