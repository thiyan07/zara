"""Stage 1 contract tests: tools, policy, execution, events, missions,
devices, memory, context, governor, audit, notifications, scheduler, API."""
import time
import pytest
from fastapi.testclient import TestClient

from core import tools as T
from core import policy as P
from core import execution as E
from core import events as EV
from core import missions as M
from core import devices as D
from core import memory as MEM
from core import context as C
from core import governor as G
from core import audit as A
from core import notifications as N
from core import scheduler as S
from core.models import (ToolDefinition, PermissionLevel, RiskLevel, DeviceState,
                         DeviceKind, MemoryItem, MissionState, ExecutionState)
from core.app import create_app, build_stack


def safe_tool(name="t.safe"):
    return ToolDefinition(name=name, description="safe test tool",
                          input_schema={"type": "object", "required": ["x"],
                                        "properties": {"x": {"type": "string"}}},
                          permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
                          timeout_s=5.0)


def test_tool_register_call_and_schema():
    r = T.ToolRegistry()
    r.register(safe_tool(), lambda i, c: {"got": i["x"]})
    assert r.call("t.safe", {"x": "hi"}) == {"got": "hi"}
    with pytest.raises(ValueError):
        r.call("t.safe", {})  # missing required
    with pytest.raises(ValueError):
        r.register(safe_tool(), lambda i, c: {})  # duplicate


def test_tool_timeout():
    import time as _t
    r = T.ToolRegistry()
    d = safe_tool("t.slow")
    d.timeout_s = 0.05
    r.register(d, lambda i, c: (_t.sleep(2), {"got": 1})[1])
    with pytest.raises(TimeoutError):
        r.call("t.slow", {"x": "y"})


def test_policy_safe_confirm_highrisk_and_deny_patterns():
    p = P.PolicyEngine()
    assert p.decide("u", "tool:a", risk=RiskLevel.SAFE).allow
    dec = p.decide("u", "tool:b", risk=RiskLevel.CONFIRM)
    assert not dec.allow and dec.requires_approval
    gid = p.grant("u", "tool:b", ttl_s=60)
    assert gid
    dec2 = p.decide("u", "tool:b", risk=RiskLevel.CONFIRM)
    assert dec2.allow  # scoped grant consumed
    hr = p.decide("u", "tool:c", risk=RiskLevel.HIGH_RISK)
    assert not hr.allow and hr.requires_approval
    deny = p.decide("u", "shell", resource="run",
                    context={"command": "rm -rf /"})
    assert not deny.allow  # deny-pattern holds even if labelled safe


def test_execution_requires_policy_and_verifies():
    r = T.ToolRegistry()
    pol = P.PolicyEngine()
    eng = E.ExecutionEngine(r, pol)
    r.register(safe_tool(), lambda i, c: {"got": i["x"]})
    rec = eng.submit("t.safe", {"x": "ok"})
    assert rec.state == ExecutionState.SUCCEEDED and rec.verified
    # confirm-risk tool waits for approval, then runs after approve
    d = safe_tool("t.send")
    d.risk = RiskLevel.CONFIRM
    r.register(d, lambda i, c: {"got": i["x"]})
    rec2 = eng.submit("t.send", {"x": "msg"})
    assert rec2.state == ExecutionState.WAITING_FOR_PERMISSION
    rec2 = eng.approve(rec2.id)
    assert rec2.state == ExecutionState.SUCCEEDED
    # high-risk can never auto-run
    d3 = safe_tool("t.wipe")
    d3.risk = RiskLevel.HIGH_RISK
    r.register(d3, lambda i, c: {"got": i["x"]})
    rec3 = eng.submit("t.wipe", {"x": "z"})
    assert rec3.state == ExecutionState.WAITING_FOR_PERMISSION


def test_execution_retry_and_failure():
    r = T.ToolRegistry()
    pol = P.PolicyEngine()
    eng = E.ExecutionEngine(r, pol)
    d = safe_tool("t.flaky")
    d.failure_behavior = "retry"
    calls = {"n": 0}

    def flaky(i, c):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return {"ok": True}

    r.register(d, flaky)
    rec = eng.submit("t.flaky", {"x": "1"}, max_retries=3)
    assert rec.state == ExecutionState.SUCCEEDED and rec.attempt == 3


def test_events_pubsub_and_history():
    bus = EV.EventBus()
    seen = []
    bus.subscribe("battery_changed", lambda e: seen.append(e))
    bus.publish("battery_changed", source="test", payload={"battery_pct": 42})
    assert seen and seen[0].payload["battery_pct"] == 42
    assert bus.history("battery_changed")


def test_mission_lifecycle_checkpoint_restore():
    me = M.MissionEngine()
    m = me.create("do thing", [{"tool": "t.safe", "inputs": {"x": "1"}}])
    me.transition(m.id, MissionState.PLANNING)
    me.transition(m.id, MissionState.EXECUTING)
    me.checkpoint(m.id)
    me.transition(m.id, MissionState.VERIFYING)
    me.transition(m.id, MissionState.COMPLETED)
    with pytest.raises(M.IllegalTransition):
        me.transition(m.id, MissionState.EXECUTING)  # terminal immutable
    restored = me.restore(m.id)
    assert restored.checkpoints


def test_device_registry_and_routing():
    dm = D.DeviceManager()
    dm.register(DeviceState(device_id="phone", kind=DeviceKind.ANDROID,
                            capabilities=["notify"], battery_pct=80))
    dm.register(DeviceState(device_id="laptop", kind=DeviceKind.LINUX,
                            capabilities=["notify", "shell"]))
    dm.register(DeviceState(device_id="cloud", kind=DeviceKind.CLOUD,
                            capabilities=["notify", "shell", "heavy"]))
    sel = dm.select(["shell"])
    assert sel.device_id in ("laptop", "cloud")  # prefers non-android
    assert sel.device_id == "cloud"
    dm.heartbeat("phone", battery_pct=10)
    assert dm.get("phone").battery_pct == 10


def test_memory_policy_refuses_secrets_and_recalls():
    ms = MEM.MemoryStore()
    with pytest.raises(ValueError):
        ms.remember(MemoryItem(text="my api_key is XYZ", importance=0.9))
    trivial = ms.remember(MemoryItem(text="ok", importance=0.1))
    assert trivial is None  # below keep threshold
    kept = ms.remember(MemoryItem(text="user prefers dark mode", category="preferences",
                                  importance=0.8))
    assert kept is not None
    hits = ms.recall("dark mode preference", top_k=3)
    assert hits and hits[0][0].id == kept.id


def test_context_prompt_minimal_and_capped():
    cm = C.ContextManager()
    cm.add_turn("user", "hello")
    cm.update_world({"place": "home"})
    b = cm.bundle()
    prompt = cm.build_prompt(b, "greet")
    assert "greet" in prompt and len(prompt) <= C.MAX_PROMPT_CHARS


def test_governor_battery_offline_reroute():
    g = G.ResourceGovernor()
    low = G.ResourceSnapshot(battery_pct=10, charging=False, device_kind="android")
    assert g.check(low, 0.5).action == "defer"
    assert g.check(low, 0.05).action == "proceed"  # light reads still fine
    off = G.ResourceSnapshot(network="offline")
    assert g.check(off, 0.9).action == "defer"
    heavy = G.ResourceSnapshot(battery_pct=90, charging=True, device_kind="android")
    assert g.check(heavy, 0.9).action == "reroute"


def test_audit_and_notifications():
    a = A.AuditLog()
    a.record("test", "exec", "t.safe", "ok")
    assert a.query()[0]["action"] == "exec"
    n = N.NotificationManager()
    m = n.create("hi", device_id="phone")
    assert n.pending("phone") and not n.pending("laptop")
    n.mark_delivered(m.id)
    assert not n.pending("phone")


def test_scheduler_once_and_cancel():
    s = S.Scheduler()
    jid = s.schedule_once("remind", 0.05)
    time.sleep(0.15)
    assert any(j["id"] == jid for j in s.fired())
    jid2 = s.schedule_once("later", 5)
    assert s.cancel(jid2)


def test_api_smoke():
    app = create_app(build_stack())
    c = TestClient(app)
    assert c.get("/v1/health").json()["ok"]
    r = c.post("/v1/chat", json={"text": "hello"})
    assert r.status_code == 401  # auth gate enforced
    h = {"Authorization": "Bearer dev-token"}
    assert c.post("/v1/chat", json={"text": "hello"}, headers=h).status_code == 200
    ex = c.post("/v1/exec", json={"tool": "system.info", "inputs": {}},
                headers=h).json()
    assert ex["status"] == "succeeded", ex
    assert c.get("/v1/tools", headers=h).status_code == 200
    assert c.get("/v1/audit", headers=h).status_code == 200
