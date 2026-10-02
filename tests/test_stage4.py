"""Stage 4 tests: hardened integration. Prior stages must keep passing.

Status key: IMPLEMENTED / UNIT TESTED / INTEGRATION TESTED /
PHYSICALLY TESTED / DEPLOYED / DEFERRED / BLOCKED.
"""
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from core.app import create_app, build_stack
from core.llm import LLMError, LLMErrorKind
from core.providers import OpenAICompatibleProvider

H = {"Authorization": "Bearer dev-token"}


class StubOpenAI(BaseHTTPRequestHandler):
    """Deterministic local OpenAI-compatible endpoint (real HTTP, no mocks
    for transport). Behavior controlled via server.plan list."""
    plan: list = []
    requests: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        StubOpenAI.requests.append(
            {"path": self.path, "payload": payload,
             "auth": self.headers.get("Authorization", "")})
        action, status = StubOpenAI.plan.pop(0) if StubOpenAI.plan else (
            {"type": "response", "text": "stub ok"}, 200)
        if isinstance(action, dict) and action.get("_stream"):
            chunks = action["_stream"]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for ch in chunks:
                self.wfile.write(
                    f"data: {json.dumps({'choices': [{'delta': {'content': ch}}]})}\n".encode())
            self.wfile.write(b"data: [DONE]\n")
            return
        if status != 200:
            self._send(status, {"error": action})
            return
        content = action if isinstance(action, str) else json.dumps(action)
        self._send(200, {"choices": [{"message": {"content": content}}],
                         "usage": {"prompt_tokens": 5,
                                   "completion_tokens": 7}})


@pytest.fixture()
def stub():
    StubOpenAI.plan = []
    StubOpenAI.requests = []
    server = HTTPServer(("127.0.0.1", 0), StubOpenAI)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_openai_real_http_structured(stub):
    StubOpenAI.plan = [({"type": "tool_call", "tool": "system.battery",
                         "arguments": {}}, 200)]
    p = OpenAICompatibleProvider(stub, api_key="k", model="m")
    out = p.reason("sys", "battery?", [{"name": "system.battery"}])
    assert out.action is not None and out.action.type == "tool_call"
    assert out.usage.get("completion_tokens") == 7
    assert StubOpenAI.requests[0]["payload"]["model"] == "m"


def test_openai_real_http_retry_then_ok(stub):
    StubOpenAI.plan = [("boom", 500),
                       ({"type": "response", "text": "recovered"}, 200)]
    p = OpenAICompatibleProvider(stub, api_key="k", model="m")
    out = p.reason("s", "u", [])
    assert out.text == "recovered"
    assert len(StubOpenAI.requests) == 2  # bounded retry, then success


def test_openai_real_http_stream(stub):
    StubOpenAI.plan = [({"_stream": ["hel", "lo"]}, 200)]
    p = OpenAICompatibleProvider(stub, api_key="k", model="m")
    assert "".join(p.stream_reason("s", "u", [])) == "hello"


def test_openai_auth_failure_no_retry(stub):
    StubOpenAI.plan = [("no", 401)]
    p = OpenAICompatibleProvider(stub, api_key="bad", model="m")
    with pytest.raises(LLMError) as e:
        p.reason("s", "u", [])
    assert e.value.kind == LLMErrorKind.AUTH
    assert len(StubOpenAI.requests) == 1  # auth fails fast, no retry loop


SECRET = "sk-test-SECRETKEY123"


def test_api_key_never_leaks(stub):
    StubOpenAI.plan = [("denied", 401)]
    p = OpenAICompatibleProvider(stub, api_key=SECRET, model="m")
    try:
        p.reason("s", "u", [])
        raise AssertionError("should have raised")
    except LLMError as e:
        assert SECRET not in str(e)
    assert p.metadata().get("api_key", None) is None
    # key travels only as the Authorization header, never in URL/body/logs
    for r in StubOpenAI.requests:
        assert SECRET not in r["path"]
        assert SECRET not in json.dumps(r["payload"])
    assert SECRET not in json.dumps(p.metadata())


# ---------- websocket realtime ----------

def test_ws_rejects_unauthenticated():
    from starlette.testclient import TestClient as TC
    from starlette.websockets import WebSocketDisconnect
    c = TC(create_app(build_stack()))
    with c.websocket_connect("/v1/stream") as ws:
        try:
            ws.receive_json()
        except WebSocketDisconnect as e:
            assert e.code == 4401
            return
        raise AssertionError("should have closed")


def test_ws_live_event_replay_and_no_exec():
    c = TestClient(create_app(build_stack()))
    stack_app = c.app.state.stack
    with c.websocket_connect("/v1/stream?token=dev-token") as ws:
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
        stack_app["bus"].publish("battery_changed", source="test",
                                 payload={"battery_pct": 42})
        msg = ws.receive_json()
        assert msg["type"] == "battery_changed"
        first_id = msg["id"]
        # invalid/action frames are refused, never executed
        ws.send_json({"type": "exec", "tool": "system.info"})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert stack_app["audit"].query(50) is not None
    # reconnect with ?since= replays missed events
    stack_app["bus"].publish("network_changed", source="test2", payload={})
    with c.websocket_connect(
            f"/v1/stream?token=dev-token&since={first_id}") as ws2:
        ws2.receive_json()  # welcome
        replayed = ws2.receive_json()
        assert replayed["type"] == "network_changed"


def test_ws_device_auth_and_forged_rejected():
    from core.models import DeviceKind
    stack = build_stack()
    code = stack["device_auth"].enroll("ws-phone", DeviceKind.ANDROID)
    _, key = stack["device_auth"].claim(code)
    c = TestClient(create_app(stack))
    with c.websocket_connect(
            f"/v1/stream?device_id=ws-phone&device_key={key}") as ws:
        assert ws.receive_json()["identity"] == "device:ws-phone"
    stack["device_auth"].revoke("ws-phone")
    from starlette.websockets import WebSocketDisconnect
    with c.websocket_connect(
            f"/v1/stream?device_id=ws-phone&device_key={key}") as ws:
        try:
            ws.receive_json()
        except WebSocketDisconnect as e:
            assert e.code == 4401
            return
        raise AssertionError("revoked device should be refused")


# ---------- auth hardening: rotation, replay, idempotency, rate limits ----------

def test_credential_rotation_kills_old_key():
    from core.device_auth import DeviceAuthStore
    from core.models import DeviceKind
    auth = DeviceAuthStore()
    _, key = auth.claim(auth.enroll("d1", DeviceKind.LINUX))
    assert auth.verify("d1", key)
    new_key = auth.rotate("d1", key)
    assert auth.verify("d1", new_key)
    assert not auth.verify("d1", key)  # old key dead
    with pytest.raises(ValueError):
        auth.rotate("d1", "wrong-key")  # rotation needs current key


def test_pairing_replay_and_job_idempotency():
    from core.device_auth import DeviceAuthStore
    from core.jobs import JobQueue
    from core.models import DeviceKind
    auth = DeviceAuthStore()
    code = auth.enroll("d2", DeviceKind.LINUX)
    auth.claim(code)
    with pytest.raises(ValueError):
        auth.claim(code)  # replayed pairing code fails
    q = JobQueue()
    j = q.enqueue("d2", "filesystem.list", {"path": "/tmp"})
    q.poll("d2")
    first = q.complete(j.id, True, {"entries": []})
    again = q.complete(j.id, False, {"entries": ["evil"]})
    assert again.state == "done" and again.result == {"entries": []}


def test_rate_limiting_middleware():
    c = TestClient(create_app(build_stack()))
    statuses = [c.post("/v1/agent/enroll",
                       json={"device_id": f"rl-{i}", "kind": "linux"},
                       headers=H).status_code for i in range(35)]
    assert 429 in statuses  # sensitive endpoint bounded at 30/min
    assert c.get("/v1/health").status_code == 200  # health exempt


def test_ratelimiter_unit():
    from core.ratelimit import RateLimiter
    rl = RateLimiter(2, window_s=60)
    assert rl.check("k")[0] and rl.check("k")[0]
    allowed, retry = rl.check("k")
    assert not allowed and retry > 0
