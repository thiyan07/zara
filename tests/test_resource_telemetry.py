"""Resource telemetry tests: snapshot, heartbeat, demand, security.

Proves: real device telemetry flows heartbeat → DeviceState → snapshot →
observation; validation is fail-closed to unknown (never zero-as-healthy,
never authority); demand vectors are bounded description only; old
heartbeats keep working; authorization/approval/targets/terminals are
untouched by telemetry values.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.app import build_stack, create_app
from core.devices import DeviceManager
from core.governor import ResourceGovernor, ResourceSnapshot
from core.models import (DeviceKind, DeviceState, ExecutionState,
                         ResourceDemands, ResourceTier, ToolDefinition)
from core.protocol import HeartbeatPayload

OP = {"Authorization": "Bearer dev-token"}
INSPECT = "gui.screen.inspect"
TAP = "gui.tap"
BAT = "system.battery"
VICTIM = "dev.zara.lab.privtest"


def make_stack(fabric_db=""):
    return build_stack(fabric_db=fabric_db)


def enroll_claim(stack, device_id, kind="linux"):
    code = stack["device_auth"].enroll(device_id, DeviceKind(kind))
    return stack["device_auth"].claim(code)


def register(stack, device_id, kind="linux", caps=None):
    stack["devices"].register(DeviceState(
        device_id=device_id, kind=DeviceKind(kind),
        capabilities=caps or [], online=True, status="online"))


def paired_android(stack, dev="emu-lab"):
    enroll_claim(stack, dev, "android")
    register(stack, dev, "android", [INSPECT, TAP, BAT])
    stack["fabric"].advertise([
        {"id": INSPECT, "name": INSPECT, "descriptor_version": "1",
         "version": "1.0.0", "risk": "confirm", "availability": "available"},
        {"id": TAP, "name": TAP, "descriptor_version": "1",
         "version": "1.0.0", "risk": "high_risk",
         "availability": "available"},
        {"id": BAT, "name": BAT, "descriptor_version": "1",
         "version": "1.0.0", "risk": "safe", "availability": "available"},
    ], dev)
    return dev


# ---------- snapshot ----------

def test_R_valid_cpu_ram_snapshot_proceeds():
    g = ResourceGovernor()
    s = ResourceSnapshot(battery_pct=80.0, charging=True, network="wifi",
                         device_kind="android", cpu_pct=35.0, mem_pct=60.0,
                         low_power_mode=False)
    assert g.check(s, 0.1, "safe").action == "proceed"


def test_R_unknown_telemetry_never_defers_nor_boosts():
    g = ResourceGovernor()
    s = ResourceSnapshot()  # everything unknown
    assert g.check(s, 0.1, "safe").action == "proceed"
    assert g.check(s, 0.9, "high_risk").action in ("proceed", "reroute")


def test_R_low_power_mode_defers_costly_work_only():
    g = ResourceGovernor()
    s = ResourceSnapshot(low_power_mode=True)
    assert g.check(s, 0.5, "safe").action == "defer"
    assert g.check(s, 0.1, "safe").action == "proceed"
    assert g.check(ResourceSnapshot(low_power_mode=None),
                   0.9, "safe").action in ("proceed", "reroute")


def test_R_nonfinite_battery_treated_unknown():
    g = ResourceGovernor()
    for bad in (float("nan"), float("inf"), -5.0, 150.0, "high", None):
        s = ResourceSnapshot(battery_pct=bad, charging=False)
        # Never crashes; never defers on an unreadable reading.
        assert g.check(s, 0.1, "safe").action == "proceed"
    s = ResourceSnapshot(battery_pct=5.0, charging=False)
    assert g.check(s, 0.5, "safe").action == "defer"


# ---------- heartbeat validation ----------

def test_R_heartbeat_accepts_valid_telemetry():
    h = HeartbeatPayload(battery_pct=80.0, charging=True, network="wifi",
                         cpu_pct=35.0, mem_pct=60.0, low_power_mode=False,
                         storage_free_mb=22950, storage_total_mb=126454,
                         battery_temp_c=28.5, thermal_state="light")
    assert h.cpu_pct == 35.0 and h.thermal_state == "light"


def test_R_heartbeat_rejects_out_of_bounds():
    for kw in ({"cpu_pct": -1}, {"cpu_pct": 101}, {"mem_pct": 101},
               {"storage_free_mb": -1}, {"battery_temp_c": 200}):
        with pytest.raises(ValidationError):
            HeartbeatPayload(**kw)


def test_R_heartbeat_rejects_nan_inf_wrong_types():
    for kw in ({"cpu_pct": float("nan")}, {"mem_pct": float("inf")},
               {"cpu_pct": "high"}, {"mem_pct": [50]}):
        with pytest.raises(ValidationError):
            HeartbeatPayload(**kw)


def test_R_heartbeat_thermal_length_capped():
    with pytest.raises(ValidationError):
        HeartbeatPayload(thermal_state="x" * 17)
    assert HeartbeatPayload(thermal_state="moderate").thermal_state == \
        "moderate"


def test_R_heartbeat_old_body_still_validates():
    h = HeartbeatPayload(battery_pct=50.0)
    assert h.cpu_pct is None and h.mem_pct is None
    assert h.low_power_mode is None and h.online is True


def test_R_heartbeat_stores_and_stamps_telemetry():
    m = DeviceManager()
    m.register(DeviceState(device_id="d1"))
    d = m.heartbeat("d1", battery_pct=80.0, cpu_pct=35.0, mem_pct=60.0,
                     low_power_mode=False)
    assert (d.cpu_pct, d.mem_pct, d.low_power_mode) == (35.0, 60.0, False)
    assert d.last_telemetry_at is not None
    assert m.telemetry_stale("d1", threshold_s=120.0) is False


def test_R_heartbeat_without_telemetry_preserves_and_no_stamp():
    m = DeviceManager()
    m.register(DeviceState(device_id="d1"))
    m.heartbeat("d1", cpu_pct=35.0)
    d = m.heartbeat("d1", battery_pct=80.0)
    assert d.cpu_pct == 35.0  # not nulled by telemetry-less heartbeat
    assert m.telemetry_stale("d1") is False  # stamp persists
    m2 = DeviceManager()
    m2.register(DeviceState(device_id="d2"))
    assert m2.telemetry_stale("d2") is True  # never reported → stale


def test_R_missing_telemetry_is_none_not_zero():
    d = DeviceState(device_id="d1")
    assert d.cpu_pct is None and d.mem_pct is None
    assert d.low_power_mode is None and d.storage_free_mb is None
    assert d.last_telemetry_at is None


def test_R_heartbeat_restores_online_after_stale_graceful_disconnect():
    """A dying agent's disconnect can land after its successor's register
    (both run during an agent restart). Heartbeats must then restore the
    presence status — before the fix, status stayed 'offline' forever
    while online=True, and every approval re-check failed closed
    (observed live 2026-10-25)."""
    m = DeviceManager()
    d = m.register(DeviceState(device_id="d1", status="online",
                               online=True))
    assert d.status == "online"
    m.mark_offline("d1")  # the late graceful disconnect
    assert m.get("d1").status == "offline" and \
        m.get("d1").online is False
    d = m.heartbeat("d1")  # successor agent keeps heartbeating
    assert d.status == "online" and d.online is True
    from core.fabric import derive_presence
    assert derive_presence(d.status, d.online, d.last_seen, 120.0) \
        not in ("offline", "unknown", "stale")
    # already-online heartbeats must not spam the event stream
    before = d.status
    m.heartbeat("d1")
    assert m.get("d1").status == before


def test_R_network_normalized_not_invented():
    m = DeviceManager()
    m.register(DeviceState(device_id="d1"))
    d = m.heartbeat("d1", network="  WiFi  ")
    assert d.network == "wifi"
    d = m.heartbeat("d1", network="x" * 100)
    assert len(d.network) <= 32


def test_R_poll_advances_presence_not_telemetry():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0)
    before = stack["devices"].get(dev).last_telemetry_at
    stack["devices"].heartbeat(dev)  # bare poll-equivalent
    after = stack["devices"].get(dev)
    assert after.cpu_pct == 35.0
    assert after.last_telemetry_at == before


def test_R_snapshot_carries_telemetry_as_observation():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0, mem_pct=60.0,
                               low_power_mode=False)
    from core.governor import ResourceSnapshot as RS
    snap = stack["fabric"]._snapshot_for(dev)
    assert isinstance(snap, RS)
    assert (snap.cpu_pct, snap.mem_pct, snap.low_power_mode) == \
        (35.0, 60.0, False)


def test_R_http_heartbeat_roundtrip():
    stack = make_stack()
    c = TestClient(create_app(stack))
    dev = paired_android(stack)
    key = stack["device_auth"].claim(
        stack["device_auth"].enroll("hb-dev", DeviceKind("android")))
    # Enroll+claim a second device properly via agent endpoints instead.
    r = c.post("/v1/agent/enroll", json={"device_id": "hb2", "kind": "android"},
               headers=OP)
    code = r.json()["pairing_code"]
    r = c.post("/v1/agent/claim", json={"pairing_code": code})
    h = {"X-Device-Id": "hb2", "X-Device-Key": r.json()["device_key"]}
    r = c.post("/v1/agent/register",
               json={"capabilities": [], "kind": "android",
                     "software_version": "t"}, headers=h)
    assert r.status_code == 200, r.text
    r = c.post("/v1/agent/heartbeat",
               json={"battery_pct": 80, "charging": True, "network": "wifi",
                     "cpu_pct": 35, "mem_pct": 60, "low_power_mode": False,
                     "storage_free_mb": 22950, "thermal_state": "light"},
               headers=h)
    assert r.status_code == 200, r.text
    d = stack["devices"].get("hb2")
    assert (d.cpu_pct, d.mem_pct, d.low_power_mode) == (35.0, 60.0, False)
    assert d.thermal_state == "light"
    # Malformed → 422, stored values untouched.
    r = c.post("/v1/agent/heartbeat", json={"cpu_pct": 101}, headers=h)
    assert r.status_code == 422
    assert stack["devices"].get("hb2").cpu_pct == 35.0
    # Legacy body still works.
    r = c.post("/v1/agent/heartbeat", json={"battery_pct": 50}, headers=h)
    assert r.status_code == 200
    assert stack["devices"].get("hb2").cpu_pct == 35.0


# ---------- demand vectors ----------

def test_R_demand_defaults_all_low():
    t = ToolDefinition(name="x.t", description="d", input_schema={},
                       output_schema={}, required_capabilities=[])
    assert t.resource_demands.model_dump() == {
        "cpu": "low", "memory": "low", "network": "low", "battery": "low"}


def test_R_demand_valid_vector_roundtrip():
    t = ToolDefinition(name="x.t", description="d", input_schema={},
                       output_schema={}, required_capabilities=[],
                       resource_demands={"cpu": "high", "network": "moderate"})
    assert t.resource_demands.cpu == ResourceTier.HIGH
    assert t.resource_demands.memory == ResourceTier.LOW
    assert t.model_dump()["resource_demands"]["cpu"] == "high"


def test_R_demand_unknown_dimension_rejected():
    with pytest.raises(ValidationError):
        ToolDefinition(name="x.t", description="d", input_schema={},
                       output_schema={}, required_capabilities=[],
                       resource_demands={"gpu_quantum": "high"})


def test_R_demand_invalid_enum_rejected():
    with pytest.raises(ValidationError):
        ToolDefinition(name="x.t", description="d", input_schema={},
                       output_schema={}, required_capabilities=[],
                       resource_demands={"cpu": "extreme"})


def test_R_demand_registry_tools_all_valid():
    from core.device_tools import DEVICE_TOOL_DEFS
    for d in DEVICE_TOOL_DEFS:
        assert isinstance(d.resource_demands, ResourceDemands)
        assert d.resource_demands.model_dump() == {
            "cpu": "low", "memory": "low", "network": "low",
            "battery": "low"}


# ---------- security: telemetry never authorizes ----------

def test_R_fake_healthy_telemetry_still_parks_high_risk():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=0.0, mem_pct=0.0,
                               charging=True, battery_pct=100.0,
                               network="wifi")
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    from core.models import ExecutionState as ES
    assert rec.state == ES.WAITING_FOR_PERMISSION
    assert stack["jobs"].poll(dev) is None


def test_R_sick_telemetry_never_denies_submit():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=100.0, mem_pct=100.0)
    rec = stack["engine"].submit_async(BAT, {}, dev)
    from core.models import ExecutionState as ES
    assert rec.state == ES.RUNNING
    stack["engine"].cancel(rec.id)


def test_R_telemetry_absent_from_audit_secrets():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0, network="wifi")
    blob = json.dumps(stack["audit"].query(200))
    assert "zara-dev-" not in blob and "device_key" not in blob
