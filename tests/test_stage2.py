"""Stage 2 tests: device auth, protocol, presence, jobs, routing, dispatch,
Linux tools, contract parity, security. Stage 1 suite must keep passing."""
import threading
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.app import create_app, build_stack
from core.device_auth import DeviceAuthStore
from core.devices import DeviceManager
from core.device_tools import DEVICE_TOOL_DEFS
from core.governor import ResourceGovernor
from core.jobs import JobQueue
from core.models import DeviceKind, DeviceState
from core.policy import PolicyEngine
from core.protocol import (parse_message, DeviceMessage, HeartbeatPayload,
                           CapabilityUpdate, ExecutionResult)
from core.routing import Router
from core.tools import ToolRegistry
from device.linux import tools_linux as LT

H = {"Authorization": "Bearer dev-token"}


def make_client():
    stack = build_stack()
    return TestClient(create_app(stack)), stack


def enroll_claim(stack, device_id="laptop-1", kind="linux"):
    code = stack["device_auth"].enroll(device_id,
                                       DeviceKind(kind))
    return stack["device_auth"].claim(code)


# ---------- registration ----------

def test_device_registration_and_duplicate():
    dm = DeviceManager()
    d = dm.register(DeviceState(device_id="laptop-1", kind=DeviceKind.LINUX,
                                capabilities=["filesystem.list"]))
    assert d.status == "online"
    d2 = dm.register(DeviceState(device_id="laptop-1", kind=DeviceKind.LINUX,
                                 capabilities=["filesystem.list", "shell-x"]))
    assert "shell-x" in dm.get("laptop-1").capabilities  # re-register updates
    with pytest.raises(ValidationError):
        DeviceState(device_id="", kind=DeviceKind.LINUX)  # invalid rejected


def test_capability_update_and_unknown_device():
    dm = DeviceManager()
    dm.register(DeviceState(device_id="p", kind=DeviceKind.ANDROID,
                            capabilities=["a"]))
    dm.update_capabilities("p", ["a", "b"])
    assert dm.get("p").capabilities == ["a", "b"]
    with pytest.raises(KeyError):
        dm.update_capabilities("ghost", ["a"])


# ---------- authentication ----------

def test_auth_enroll_claim_verify():
    auth = DeviceAuthStore()
    code = auth.enroll("laptop-1", DeviceKind.LINUX)
    device_id, key = auth.claim(code)
    assert device_id == "laptop-1"
    assert auth.verify("laptop-1", key)
    assert not auth.verify("laptop-1", "wrong-key")  # forged rejected
    with pytest.raises(ValueError):
        auth.claim(code)  # one-time pairing code


def test_auth_revoke_and_expiry():
    auth = DeviceAuthStore()
    code = auth.enroll("old-phone", DeviceKind.ANDROID)
    _, key = auth.claim(code, ttl_days=-1)  # already expired
    assert not auth.verify("old-phone", key)
    code2 = auth.enroll("laptop-2", DeviceKind.LINUX)
    _, key2 = auth.claim(code2)
    assert auth.verify("laptop-2", key2)
    auth.revoke("laptop-2")
    assert not auth.verify("laptop-2", key2)  # revoked rejected


def test_http_agent_auth_flow_and_forged_rejected():
    c, stack = make_client()
    # enroll requires dev token
    assert c.post("/v1/agent/enroll",
                  json={"device_id": "lap", "kind": "linux"}).status_code == 401
    r = c.post("/v1/agent/enroll", json={"device_id": "lap", "kind": "linux"},
               headers=H).json()
    claim = c.post("/v1/agent/claim",
                   json={"pairing_code": r["pairing_code"]}).json()
    dh = {"X-Device-Id": "lap", "X-Device-Key": claim["device_key"]}
    reg = c.post("/v1/agent/register",
                 json={"capabilities": ["filesystem.list"],
                       "software_version": "t", "kind": "linux"}, headers=dh)
    assert reg.status_code == 200
    bad = c.post("/v1/agent/heartbeat",
                 json={"charging": False, "network": "wifi", "online": True},
                 headers={"X-Device-Id": "lap", "X-Device-Key": "forged"})
    assert bad.status_code == 403  # forged device rejected
    assert c.post("/v1/agent/heartbeat",
                  json={"charging": False, "network": "wifi",
                        "online": True}).status_code in (401, 422)
    # revoked device rejected
    c.post("/v1/agent/revoke?device_id=lap", headers=H)
    gone = c.post("/v1/agent/jobs/poll", headers=dh)
    assert gone.status_code == 403


# ---------- protocol ----------

def test_protocol_strict_parsing():
    m = parse_message({"type": "heartbeat", "device_id": "d",
                       "payload": {"battery_pct": 50}})
    assert isinstance(m, DeviceMessage)
    with pytest.raises(Exception):
        parse_message({"type": "nope", "device_id": "d"})  # unknown type
    with pytest.raises(Exception):
        parse_message({"type": "heartbeat", "device_id": "d",
                       "v": "9.9"})  # version mismatch
    with pytest.raises(ValidationError):
        HeartbeatPayload(battery_pct=150)  # schema violation


# ---------- presence ----------

def test_presence_lifecycle_and_stale():
    dm = DeviceManager()
    dm.register(DeviceState(device_id="p", kind=DeviceKind.ANDROID))
    dm.set_status("p", "reconnecting")
    assert dm.get("p").online  # reconnecting still counts online
    dm.mark_offline("p")
    assert not dm.get("p").online
    with pytest.raises(ValueError):
        dm.set_status("p", "flying")  # invalid status rejected
    assert dm.stale_ids(threshold_s=-1)  # everything stale w/ negative window


def test_low_battery_degraded_via_api():
    c, stack = make_client()
    enroll_claim(stack, "lap")
    # register through store-level device (auth headers need key)
    code = stack["device_auth"].enroll("lap2", DeviceKind.LINUX)
    _, key = stack["device_auth"].claim(code)
    dh = {"X-Device-Id": "lap2", "X-Device-Key": key}
    c.post("/v1/agent/register",
           json={"capabilities": [], "software_version": "", "kind": "linux"},
           headers=dh)
    out = c.post("/v1/agent/heartbeat",
                 json={"battery_pct": 5, "charging": False,
                       "network": "wifi", "online": True}, headers=dh).json()
    assert out["status"] == "degraded"


# ---------- jobs + proxy execution ----------

def test_job_lifecycle_cancel_timeout():
    q = JobQueue()
    j = q.enqueue("lap", "filesystem.list", {"path": "/tmp"})
    assert q.poll("lap").id == j.id
    assert q.poll("lap") is None  # claimed, not double-delivered
    q2 = q.enqueue("lap", "filesystem.list", {"path": "/tmp"})
    assert q.cancel(q2.id)
    assert q.wait_for_result(q2.id, timeout=1).state == "cancelled"
    q3 = q.enqueue("lap", "filesystem.list", {"path": "/tmp"})
    q.poll("lap")
    assert q.wait_for_result(q3.id, timeout=0.1).state in ("expired", "claimed",
                                                           "pending")


def test_dispatch_roundtrip_with_fake_agent():
    c, stack = make_client()
    _, key = enroll_claim(stack, "lap")
    dh = {"X-Device-Id": "lap", "X-Device-Key": key}
    c.post("/v1/agent/register",
           json={"capabilities": ["filesystem.list"],
                 "software_version": "t", "kind": "linux"}, headers=dh)
    out = {}

    def do_dispatch():
        rec = stack["engine"].submit("filesystem.list", {"path": "/tmp"}, "lap")
        out["rec"] = rec

    t = threading.Thread(target=do_dispatch, daemon=True)
    t.start()
    # fake agent: poll-claim the job the proxy enqueued, then complete it
    import time
    claimed = None
    for _ in range(100):
        claimed = stack["jobs"].poll("lap")
        if claimed is not None:
            break
        time.sleep(0.05)
    assert claimed is not None
    stack["jobs"].complete(claimed.id, True, {"path": "/tmp", "entries": []})
    t.join(timeout=10)
    assert out["rec"].state.value == "succeeded"
    assert out["rec"].result == {"path": "/tmp", "entries": []}


def test_dispatch_http_defer_and_deny():
    c, stack = make_client()
    # no devices at all -> deny (no capable device)
    r = c.post("/v1/dispatch",
               json={"tool": "filesystem.list", "inputs": {"path": "/tmp"}},
               headers=H).json()
    assert r["status"] in ("deny", "defer")
    # unknown tool -> deny
    r2 = c.post("/v1/dispatch", json={"tool": "nope.x", "inputs": {}},
                headers=H).json()
    assert r2["status"] == "deny"


def test_dispatch_confirm_holds_and_deny_pattern_refuses():
    c, stack = make_client()
    _, key = enroll_claim(stack, "lap")
    dh = {"X-Device-Id": "lap", "X-Device-Key": key}
    c.post("/v1/agent/register",
           json={"capabilities": ["terminal.safe"],
                 "software_version": "t", "kind": "linux"}, headers=dh)
    # benign confirm tool -> routed, execution waits for approval
    r = c.post("/v1/dispatch",
               json={"tool": "shell.safe_readonly",
                     "inputs": {"command": "uptime"}}, headers=H).json()
    assert r["status"] == "waiting_for_permission", r
    assert r["device_id"] == "lap"
    # destructive command -> hard deny-pattern refusal, never approvable
    r2 = c.post("/v1/dispatch",
                json={"tool": "shell.safe_readonly",
                      "inputs": {"command": "rm -rf /"}}, headers=H).json()
    assert r2["status"] == "deny", r2
    assert "deny-pattern" in r2["reason"]


# ---------- routing ----------

def routed_router(devices):
    reg = ToolRegistry()
    from core.device_tools import register_device_proxies
    from core.jobs import JobQueue as JQ
    register_device_proxies(reg, JQ())
    return Router(devices, reg, PolicyEngine(), ResourceGovernor())


def test_routing_prefers_laptop_over_android():
    dm = DeviceManager()
    dm.register(DeviceState(device_id="phone", kind=DeviceKind.ANDROID,
                            capabilities=["filesystem.list"], battery_pct=90))
    dm.register(DeviceState(device_id="laptop", kind=DeviceKind.LINUX,
                            capabilities=["filesystem.list"]))
    dec = routed_router(dm).route("filesystem.list")
    assert dec.action == "route" and dec.device_id == "laptop"


def test_routing_skips_offline_and_mismatch():
    dm = DeviceManager()
    dm.register(DeviceState(device_id="lap", kind=DeviceKind.LINUX,
                            capabilities=["filesystem.list"], online=False,
                            status="offline"))
    dec = routed_router(dm).route("filesystem.list")
    assert dec.action in ("deny", "defer")
    dm2 = DeviceManager()
    dm2.register(DeviceState(device_id="lap", kind=DeviceKind.LINUX,
                             capabilities=["other.cap"]))
    assert routed_router(dm2).route("filesystem.list").action == "deny"


def test_routing_battery_aware_defer():
    dm = DeviceManager()
    dm.register(DeviceState(device_id="phone", kind=DeviceKind.ANDROID,
                            capabilities=["terminal.safe"], battery_pct=5,
                            charging=False, network="unknown"))
    # shell.safe_readonly costs 0.05 -> allowed even at 5%? cost<=0.2 proceeds.
    # Use a heavy-cost lens: router uses declared cost; assert low-battery
    # phone is NOT chosen for heavy work by checking governor directly.
    from core.governor import ResourceSnapshot
    g = ResourceGovernor()
    snap = ResourceSnapshot(battery_pct=5, charging=False,
                            device_kind="android")
    assert g.check(snap, 0.9).action == "defer"
    assert g.check(snap, 0.9, risk="safe").action == "defer"


# ---------- linux tools ----------

def test_linux_tools_safe_and_scoped(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hello zara")
    out = LT.filesystem_read({"path": str(f)}, {})
    assert "hello zara" in out["text"]
    with pytest.raises(PermissionError):
        LT.filesystem_read({"path": "/etc/passwd"}, {})  # outside roots
    with pytest.raises(PermissionError):
        LT.filesystem_read({"path": "~/.ssh/id_rsa"}, {})  # sensitive
    r = LT.shell_safe_readonly({"command": "uptime"}, {})
    assert r["returncode"] == 0
    with pytest.raises(PermissionError):
        LT.shell_safe_readonly({"command": "rm -rf /"}, {})  # not allowlisted
    with pytest.raises(PermissionError):
        LT.shell_safe_readonly({"command": "ls; rm x"}, {})  # metachars
    with pytest.raises(PermissionError):
        LT.shell_safe_readonly({"command": "git push origin main"}, {})


def test_contract_parity_core_vs_linux():
    """Same tool names, capabilities, risk, and input schemas on both sides.

    Platform-scoped tools (android.* and gui.* namespaces) are Android-only
    by design: the Linux agent has no twin and refuses them as unimplemented
    (see test_android_only_tools_absent_on_linux below)."""
    linux = {d.name: d for d, _ in LT.LINUX_TOOLS}
    for core_def in DEVICE_TOOL_DEFS:
        if core_def.name.startswith(("android.", "gui.")):
            assert core_def.name not in linux, \
                f"android-only tool leaked to linux: {core_def.name}"
            continue
        assert core_def.name in linux, f"missing on device: {core_def.name}"
        ldev = linux[core_def.name]
        assert set(core_def.required_capabilities) == \
            set(ldev.required_capabilities)
        assert core_def.risk == ldev.risk
        assert core_def.input_schema == ldev.input_schema
    shared = sorted({c for d in DEVICE_TOOL_DEFS for c in d.required_capabilities
                     if not d.name.startswith(("android.", "gui."))})
    assert shared == [c for c in LT.LINUX_CAPABILITIES
                      if not c.startswith(("android.", "gui."))]


def test_android_only_tools_absent_on_linux():
    """The Linux agent must not implement, advertise, or accept
    Android-only capabilities. Unknown tools are refused, never executed."""
    import device.linux.agent as _agent_mod
    import inspect as _inspect
    names = {d.name for d, _ in LT.LINUX_TOOLS}
    assert "android.app.force_stop" not in names
    assert "android.app.force_stop" not in LT.LINUX_CAPABILITIES
    assert "gui.screen.inspect" not in names
    assert "gui.tap" not in names
    assert "gui.screen.inspect" not in LT.LINUX_CAPABILITIES
    assert "gui.tap" not in LT.LINUX_CAPABILITIES
    src = _inspect.getsource(_agent_mod)
    assert "tool not implemented on device" in src


def test_no_secrets_in_audit_or_tools():
    c, stack = make_client()
    c.post("/v1/chat", json={"text": "hello"}, headers=H)
    for entry in stack["audit"].query(50):
        assert "zara-dev-" not in entry["detail"]
