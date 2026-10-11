"""Safe provider-failure behavior: construction ok, first call fails SAFE.

No network, no credentials — everything is stubbed/mocked. A canary key
(CANARY) stands in for a real secret; every raised message, event payload,
and reply is scanned for it.
"""
import json
import threading
import types

import pytest

from core.app import build_stack
from core.conversation import ConversationLoop
from core.llm import LLMError, LLMErrorKind, classify_http_error
from core.memory_policy import MemoryService
from core.missions import MissionState
from core.models import PermissionLevel, RiskLevel, ToolDefinition
from core.providers import (ModelProvider, OpenAICompatibleProvider,
                            provider_from_env)

CANARY = "sk-canary-9f8e7d6c5b4a"

SAFE_KINDS = {
    LLMErrorKind.TIMEOUT, LLMErrorKind.NETWORK, LLMErrorKind.MALFORMED,
    LLMErrorKind.RATE_LIMITED, LLMErrorKind.AUTH,
    LLMErrorKind.CONTEXT_TOO_LARGE, LLMErrorKind.UNAVAILABLE,
    LLMErrorKind.CANCELLED,
}


def assert_no_canary(*blobs):
    for b in blobs:
        s = b if isinstance(b, str) else json.dumps(b, default=str)
        assert CANARY not in s, f"canary leaked in: {s[:200]}"


def make_loop(provider, **kw):
    stack = build_stack()
    stack["registry"].register(
        ToolDefinition(name="test.ping", description="test ping tool",
                       input_schema={"type": "object", "properties": {}},
                       permission=PermissionLevel.PUBLIC,
                       risk=RiskLevel.SAFE),
        lambda i, c: {"pong": True})
    loop = ConversationLoop(provider=provider, ctx=stack["ctx"],
                            memory=MemoryService(stack["memory"]),
                            registry=stack["registry"], policy=stack["policy"],
                            engine=stack["engine"], router=stack["router"],
                            devices=stack["devices"], governor=stack["gov"],
                            missions=stack["missions"], audit=stack["audit"],
                            bus=stack["bus"], sessions=stack["sessions"],
                            tracer=stack["tracer"], config=kw.get("config"))
    return stack, loop


def run_turn(loop):
    """handle_text with event capture; returns (result, events)."""
    events = []

    def emit(name, data):
        events.append({"event": name, "data": data})

    out = loop.handle_text("please do the thing", emit=emit)
    return out, events


def stub_post(p, monkeypatch, fake_post):
    """Patch _post AND force the streaming path through the stubbed
    reason() (handle_text uses stream_reason whenever emit is given;
    the base-class fallback preserves the parsed action)."""
    monkeypatch.setattr(p, "_post", fake_post)
    monkeypatch.setattr(p, "stream_reason",
                        types.MethodType(ModelProvider.stream_reason, p))


def count_submits(stack):
    calls = []
    orig = stack["engine"].submit

    def wrapper(*a, **k):
        calls.append((a, k))
        return orig(*a, **k)

    stack["engine"].submit = wrapper
    return calls


def ok_payload(text="fine"):
    return {"choices": [{"message": {"content":
            json.dumps({"type": "response", "text": text})}}],
            "usage": {}}


# ---------- 1. empty base_url ----------

def test_empty_base_url_constructs_but_first_call_fails_safe(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", CANARY)
    monkeypatch.setenv("LLM_MODEL", "m")
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    p = provider_from_env()  # construction must not raise
    assert isinstance(p, OpenAICompatibleProvider)
    with pytest.raises(LLMError) as ei:
        p.reason("sys", "hello", [], timeout=2.0)
    # FIXED: misconfiguration surfaces as LLMError (safe turn error),
    # never a raw ValueError escaping to the caller.
    assert ei.value.kind == LLMErrorKind.NETWORK
    assert_no_canary(str(ei.value), repr(ei.value))


def test_empty_base_url_turn_errors_with_nothing_executed(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", CANARY)
    monkeypatch.setenv("LLM_MODEL", "m")
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    stack, loop = make_loop(provider_from_env())
    calls = count_submits(stack)
    out, events = run_turn(loop)  # emit path: _reason_stream wraps the
    # raw ValueError into LLMError(NETWORK) -> safe error turn.
    assert out.status == "error"
    assert "nothing was executed" in out.reply.lower()
    assert calls == []
    assert dict(stack["engine"]._records) == {}
    assert_no_canary(out.reply, events)


def test_empty_base_url_blocking_path_crashes_raw(monkeypatch):
    """CONTRACT BUG (core/providers.py:133 + core/conversation.py:328):
    without emit, handle_text takes the blocking _reason path, where the
    raw ValueError from Request() propagates uncaught (only LLMError is
    handled) and crashes the turn instead of yielding status=error."""
    monkeypatch.setenv("LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", CANARY)
    monkeypatch.setenv("LLM_MODEL", "m")
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    stack, loop = make_loop(provider_from_env())
    calls = count_submits(stack)
    # FIXED: misconfiguration surfaces as a safe turn error, never a crash.
    out = loop.handle_text("please do the thing")  # no emit: blocking path
    assert out.status == "error"
    assert calls == []
    assert dict(stack["engine"]._records) == {}


# ---------- 2. auth failure ----------

def test_auth_failure_maps_and_mentions_config(monkeypatch):
    # _post surfaces HTTP errors via classify_http_error: 401/403 -> AUTH.
    assert classify_http_error(401, "x").kind == LLMErrorKind.AUTH
    assert classify_http_error(403, "x").kind == LLMErrorKind.AUTH
    p = OpenAICompatibleProvider(base_url="http://127.0.0.1:9",
                                 api_key=CANARY, model="m")

    def fake_post(payload, timeout):
        raise classify_http_error(401, "unauthorized: bad key")

    stub_post(p, monkeypatch, fake_post)
    stack, loop = make_loop(p)
    calls = count_submits(stack)
    out, events = run_turn(loop)
    assert out.status == "error"
    lowered = out.reply.lower()
    assert "authentication" in lowered or "configuration" in lowered
    assert calls == []  # engine untouched: no executions created
    assert dict(stack["engine"]._records) == {}
    assert_no_canary(out.reply, events)


# ---------- 3. timeout / network / rate-limit / malformed mapping ----------

def test_timeout_maps_to_safe_reply(monkeypatch):
    p = OpenAICompatibleProvider(base_url="http://127.0.0.1:9",
                                 api_key=CANARY, model="m")
    post_calls = []

    def fake_post(payload, timeout):
        post_calls.append(1)
        raise LLMError(LLMErrorKind.TIMEOUT, "provider timed out")

    stub_post(p, monkeypatch, fake_post)
    stack, loop = make_loop(p)
    calls = count_submits(stack)
    out, events = run_turn(loop)
    assert out.status == "error"
    assert "timed out" in out.reply.lower()
    assert "nothing" in out.reply.lower()  # nothing executed
    assert len(post_calls) == 1  # TIMEOUT is not retried
    assert calls == []
    assert_no_canary(out.reply, events)


def test_network_error_retries_then_maps_safe(monkeypatch):
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    p = OpenAICompatibleProvider(base_url="http://127.0.0.1:9",
                                 api_key=CANARY, model="m")
    post_calls = []

    def fake_post(payload, timeout):
        post_calls.append(1)
        raise LLMError(LLMErrorKind.NETWORK, "provider network: down")

    stub_post(p, monkeypatch, fake_post)
    stack, loop = make_loop(p)
    calls = count_submits(stack)
    out, events = run_turn(loop)
    assert out.status == "error"
    assert "couldn't reach" in out.reply.lower()
    assert "nothing was executed" in out.reply.lower()
    assert len(post_calls) == OpenAICompatibleProvider.MAX_RETRIES + 1
    assert calls == []
    assert_no_canary(out.reply, events)


def test_rate_limit_then_success_recovers(monkeypatch):
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    p = OpenAICompatibleProvider(base_url="http://127.0.0.1:9",
                                 api_key=CANARY, model="m")
    post_calls = []
    responses = [LLMError(LLMErrorKind.RATE_LIMITED, "rate limited"),
                 ok_payload("recovered after throttle")]

    def fake_post(payload, timeout):
        post_calls.append(1)
        nxt = responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    stub_post(p, monkeypatch, fake_post)
    stack, loop = make_loop(p)
    out, events = run_turn(loop)
    assert out.status == "responded"
    assert "recovered after throttle" in out.reply
    assert len(post_calls) == 2  # one retry, then success
    assert_no_canary(out.reply, events)


def test_malformed_raised_maps_to_discard_reply(monkeypatch):
    p = OpenAICompatibleProvider(base_url="http://127.0.0.1:9",
                                 api_key=CANARY, model="m")
    post_calls = []

    def fake_post(payload, timeout):
        post_calls.append(1)
        raise LLMError(LLMErrorKind.MALFORMED, "malformed LLM output: nope")

    stub_post(p, monkeypatch, fake_post)
    stack, loop = make_loop(p)
    calls = count_submits(stack)
    out, events = run_turn(loop)
    assert out.status == "error"
    assert "unreadable" in out.reply.lower()
    assert "nothing executed" in out.reply.lower()
    assert len(post_calls) == 1  # MALFORMED never retried, never dispatched
    assert calls == []
    assert_no_canary(out.reply, events)


# ---------- 3b. payload-shape contract bugs (reported, unfixed) ----------
# reason() only catches LLMError. A 200 with a bad body therefore escapes
# as a RAW exception instead of LLMError(MALFORMED), and handle_text (which
# catches only LLMError) lets it crash the turn. See CONTRACT-BUGS below.

def test_malformed_json_body_maps_malformed(monkeypatch):
    # Faithful stub at the HTTP layer: 200 with a non-JSON body.
    import urllib.request

    class BadJSON:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{not valid json"

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: BadJSON())
    p = OpenAICompatibleProvider(base_url="http://127.0.0.1:9",
                                 api_key=CANARY, model="m")
    with pytest.raises(LLMError) as ei:
        p.reason("s", "hi", [], timeout=2.0)
    assert ei.value.kind == LLMErrorKind.MALFORMED  # FIXED, was raw
    assert_no_canary(str(ei.value), repr(ei.value))


def test_empty_choices_maps_malformed(monkeypatch):
    p = OpenAICompatibleProvider(base_url="http://127.0.0.1:9",
                                 api_key=CANARY, model="m")
    monkeypatch.setattr(p, "_post", lambda payload, timeout: {"choices": []})
    with pytest.raises(LLMError) as ei:
        p.reason("s", "hi", [], timeout=2.0)
    assert ei.value.kind == LLMErrorKind.MALFORMED  # FIXED, was IndexError
    assert_no_canary(str(ei.value), repr(ei.value))


def test_missing_message_content_maps_malformed(monkeypatch):
    p = OpenAICompatibleProvider(base_url="http://127.0.0.1:9",
                                 api_key=CANARY, model="m")
    monkeypatch.setattr(p, "_post",
                        lambda payload, timeout:
                        {"choices": [{"message": {}}]})
    with pytest.raises(LLMError) as ei:
        p.reason("s", "hi", [], timeout=2.0)
    assert ei.value.kind == LLMErrorKind.MALFORMED  # FIXED, was KeyError
    assert_no_canary(str(ei.value), repr(ei.value))


# ---------- 4. no retry of a failing tool action ----------

def test_failing_tool_is_submitted_exactly_once():
    from core.providers import ScriptedProvider
    stack = build_stack()
    stack["registry"].register(
        ToolDefinition(name="test.boom", description="always fails",
                       input_schema={"type": "object", "properties": {}},
                       permission=PermissionLevel.PUBLIC,
                       risk=RiskLevel.SAFE),
        lambda i, c: 1 / 0)
    # Empty needle matches every prompt: the provider keeps proposing the
    # same failing tool; the loop must NOT resubmit it (seen-call guard).
    provider = ScriptedProvider(
        [("", {"type": "tool_call", "tool": "test.boom",
               "arguments": {}, "reason": "try it"})])
    loop = ConversationLoop(provider=provider, ctx=stack["ctx"],
                            memory=MemoryService(stack["memory"]),
                            registry=stack["registry"], policy=stack["policy"],
                            engine=stack["engine"], router=stack["router"],
                            devices=stack["devices"], governor=stack["gov"],
                            missions=stack["missions"], audit=stack["audit"],
                            bus=stack["bus"], sessions=stack["sessions"],
                            tracer=stack["tracer"])
    calls = count_submits(stack)
    events = []

    def emit(name, data):
        events.append({"event": name, "data": data})

    out = loop.handle_text("boom go", emit=emit)
    boom_submits = [c for c in calls if c[0] and c[0][0] == "test.boom"]
    assert len(boom_submits) == 1  # single engine submission, no auto-resubmit
    assert "succeed" not in out.reply.lower()
    assert "verified" not in out.reply.lower()
    assert_no_canary(out.reply, events)


# ---------- 5. cancellation during provider wait ----------

def test_cancel_during_provider_wait_aborts_without_success():
    from core.providers import ModelProvider

    entered = threading.Event()

    class BlockingProvider(ModelProvider):
        name = "blocking"

        def reason(self, system, user, tools, cancel=None, timeout=60.0):
            import time as _t
            entered.set()
            start = _t.time()
            while True:
                if cancel is not None and cancel.is_set():
                    raise LLMError(LLMErrorKind.CANCELLED,
                                   "cancelled during provider wait")
                if _t.time() - start > 10:
                    raise LLMError(LLMErrorKind.TIMEOUT, "stuck")
                _t.sleep(0.01)

    stack, loop = make_loop(BlockingProvider())
    calls = count_submits(stack)
    out_box = {}
    tid = "cancel-wait-1"
    t = threading.Thread(target=lambda: out_box.update(
        reply=loop.handle_text("slow please", turn_id=tid)),
        daemon=True)
    t.start()
    assert entered.wait(timeout=10)
    assert loop.turns.cancel(tid) is True
    t.join(timeout=15)
    assert not t.is_alive()
    out = out_box["reply"]
    assert out.status == "cancelled"
    assert "cancell" in out.reply.lower()
    assert "nothing further will run" in out.reply.lower()
    assert calls == []  # nothing executed while waiting
    assert dict(stack["engine"]._records) == {}
    mission = stack["missions"].get(out.mission_id)
    assert mission.state == MissionState.CANCELLED  # no success recorded
    assert_no_canary(out.reply)


# ---------- 6. canary sweep across every path above ----------
# Each test already calls assert_no_canary on its raised messages, event
# payloads, and replies. This test pins the fixture itself: the canary must
# look like a real secret (sk- prefix) so absence assertions are meaningful.

def test_canary_fixture_is_secret_shaped():
    assert CANARY.startswith("sk-")


def test_chat_endpoint_maps_provider_failure_to_safe_502(monkeypatch):
    from fastapi.testclient import TestClient
    from core.app import build_stack, create_app
    from core.llm import LLMError, LLMErrorKind
    stack = build_stack()
    c = TestClient(create_app(stack))
    OP = {"Authorization": "Bearer dev-token"}

    def boom(prompt, **kw):
        raise LLMError(LLMErrorKind.UNAVAILABLE, "down")

    monkeypatch.setattr(stack["model"], "complete", boom)
    r = c.post("/v1/chat", json={"text": "hi"}, headers=OP)
    assert r.status_code == 502, r.text
    assert "unavailable" in r.json()["detail"]
