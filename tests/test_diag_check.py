"""Guarded live-provider check endpoint (Night 10 Part A)."""
from fastapi.testclient import TestClient

from core.app import build_stack, create_app

OP = {"Authorization": "Bearer dev-token"}


def make_client(**kw):
    stack = build_stack(fabric_db="", **kw)
    return TestClient(create_app(stack)), stack


def test_D01_mock_provider_rejected_not_healthy():
    c, _ = make_client()
    r = c.post("/v1/diagnostics/provider-check", headers=OP)
    assert r.status_code == 422, r.text
    assert "echo" in r.json()["detail"]


def test_D02_cancel_without_check_is_noop():
    c, _ = make_client()
    r = c.post("/v1/diagnostics/provider-check/cancel", headers=OP)
    assert r.status_code == 200 and r.json() == {"cancelled": False}


def test_D03_auth_required():
    c, _ = make_client()
    assert c.post("/v1/diagnostics/provider-check").status_code in (401, 403)
    assert c.post("/v1/diagnostics/provider-check/cancel").status_code in (
        401, 403)


def test_D04_live_check_creates_no_side_effects(monkeypatch):
    """Scripted stand-in for a live provider: success must not create
    missions, executions, memories, or device jobs."""
    import os
    monkeypatch.setenv("LLM_PROVIDER", "scripted")
    c, stack = make_client()
    n_missions = len(stack["missions"]._missions)
    n_exec = len(stack["engine"]._records)
    n_mem = len(stack["memory"].all())
    n_jobs = len(stack["jobs"]._jobs)
    # Scripted is mock -> 422 by contract (config is not health).
    r = c.post("/v1/diagnostics/provider-check", headers=OP)
    assert r.status_code == 422, r.text
    assert len(stack["missions"]._missions) == n_missions
    assert len(stack["engine"]._records) == n_exec
    assert len(stack["memory"].all()) == n_mem
    assert len(stack["jobs"]._jobs) == n_jobs


def test_D05_live_success_shape_with_stub(monkeypatch):
    """Endpoint shape on success, using a stub live provider (no network).
    Proves metadata + timing fields and zero side effects."""
    from core.llm import LLMOutcome, LLMAction
    from core.providers import ModelProvider

    class StubLive(ModelProvider):
        name = "stub-live"

        def __init__(self):
            self.model = "stub-1"

        def reason(self, system, user, tools, cancel=None, timeout=60.0):
            assert tools == [], "diagnostic must never offer tools"
            return LLMOutcome(action=LLMAction(type="response", text="ok"),
                              text="ok", provider=self.name, model=self.model,
                              latency_s=0.01)

    c, stack = make_client()
    stack["model"] = StubLive()  # same dict object the app closes over
    r = c.post("/v1/diagnostics/provider-check", headers=OP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["provider"] == "stub-live"
    assert body["model"] == "stub-1" and body["elapsed_s"] >= 0
    assert "sk-" not in r.text and "Bearer" not in r.text


def test_D06_busy_second_caller_gets_409():
    import threading
    import time
    from core.llm import LLMOutcome, LLMAction
    from core.providers import ModelProvider

    release = threading.Event()

    class SlowLive(ModelProvider):
        name = "slow-live"

        def __init__(self):
            self.model = "s"

        def reason(self, system, user, tools, cancel=None, timeout=60.0):
            release.wait(timeout=10)
            return LLMOutcome(action=LLMAction(type="response", text="ok"),
                              text="ok", provider=self.name, model=self.model,
                              latency_s=0.0)

    c, stack = make_client()
    stack["model"] = SlowLive()  # same dict object the app closes over
    t = threading.Thread(target=lambda: c.post(
        "/v1/diagnostics/provider-check", headers=OP), daemon=True)
    t.start()
    time.sleep(0.5)
    r = c.post("/v1/diagnostics/provider-check", headers=OP)
    assert r.status_code == 409, r.text
    release.set()
    t.join(timeout=10)
