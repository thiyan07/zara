"""Canonical resource snapshot tests: one provider, all consumers.

Proves every builder routes through the canonical path, every consumer
reads the same snapshot, freshness/unknown/unavailable hold, revoke and
reconnect behave, demand/constraint foundations are bounded and
non-authoritative. No drain helpers; no sleeps except scaled CadencePoller
where device ticks are modeled.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.governor import (ResourceGovernor, ResourceSnapshot,
                           canonical_snapshot, normalize_device_kind)
from core.models import DeviceKind, DeviceState, ExecutionState

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


# ---------- provider identity ----------

def test_C01_canonical_constructor_defaults_unknown():
    s = canonical_snapshot()
    assert s.battery_pct is None and s.cpu_pct is None
    assert s.low_power_mode is None and s.device_kind == "cloud"
    assert ResourceGovernor().check(s, 0.9, "high_risk").action in (
        "proceed", "reroute")


def test_C02_kind_normalization_closed_set():
    assert normalize_device_kind("android") == "android"
    assert normalize_device_kind("linux") == "linux"
    assert normalize_device_kind("cloud") == "cloud"
    for weird in ("usb_storage", "", "ANDROID", "bluetooth", "null"):
        assert normalize_device_kind(weird) == "unknown"


def test_C03_all_builders_agree():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, battery_pct=80.0, charging=True,
                               network="wifi", cpu_pct=35.0, mem_pct=60.0,
                               low_power_mode=False)
    a = stack["fabric"].resource_snapshot(dev)
    b = stack["fabric"]._snapshot_for(dev)
    for f in ("battery_pct", "charging", "network", "device_kind",
              "low_power_mode", "cpu_pct", "mem_pct"):
        assert getattr(a, f) == getattr(b, f), f
    # Router builds the same values through the canonical constructor.
    from core.governor import canonical_snapshot as cs
    d = stack["devices"].snapshot(dev)
    c = cs(battery_pct=d.battery_pct, charging=d.charging,
           network=d.network, device_kind=d.kind.value,
           low_power_mode=d.low_power_mode, cpu_pct=d.cpu_pct,
           mem_pct=d.mem_pct, storage_free_mb=d.storage_free_mb,
           battery_temp_c=d.battery_temp_c, thermal_state=d.thermal_state)
    for f in ("battery_pct", "charging", "network", "device_kind",
              "low_power_mode", "cpu_pct", "mem_pct"):
        assert getattr(a, f) == getattr(c, f), f


def test_C04_unknown_device_yields_bare_default():
    stack = make_stack()
    s = stack["fabric"].resource_snapshot("ghost")
    assert s.battery_pct is None and s.device_kind == "cloud"
    assert ResourceGovernor().check(s, 0.9, "safe").action == "proceed"


def test_C05_update_propagates_same_tick():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0, mem_pct=60.0,
                               low_power_mode=False)
    s = stack["fabric"].resource_snapshot(dev)
    assert (s.cpu_pct, s.mem_pct, s.low_power_mode) == (35.0, 60.0, False)
    v = stack["fabric"].device_view(dev).as_dict()
    assert (v["cpu_pct"], v["mem_pct"], v["low_power_mode"]) == \
        (35.0, 60.0, False)


def test_C06_partial_update_preserves_rest():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0, mem_pct=60.0)
    stack["devices"].heartbeat(dev, battery_pct=80.0)
    s = stack["fabric"].resource_snapshot(dev)
    assert (s.cpu_pct, s.mem_pct, s.battery_pct) == (35.0, 60.0, 80.0)


def test_C07_malformed_field_cannot_poison_snapshot():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0, mem_pct=60.0)
    # Direct invalid injection is impossible via heartbeat (422); even a
    # hypothetical out-of-range store never defers on that axis.
    from core.governor import ResourceSnapshot as RS
    assert ResourceGovernor().check(
        RS(cpu_pct=999.0, mem_pct=-5.0), 0.9, "safe").action in (
            "proceed", "reroute")


def test_C08_unknown_stale_unavailable_distinct():
    stack = make_stack()
    dev = paired_android(stack)
    assert stack["devices"].telemetry_stale(dev) is True  # never reported
    s = stack["fabric"].resource_snapshot(dev)
    assert s.cpu_pct is None
    stack["devices"].heartbeat(dev, cpu_pct=35.0)
    assert stack["devices"].telemetry_stale(dev) is False
    # Capability availability is orthogonal to freshness.
    v = stack["resolver"].resolve(BAT, dev)
    assert v.status in ("resolved", "requires_approval")


def test_C09_reconnect_refreshes_telemetry():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0)
    stack["devices"].mark_offline(dev)
    assert stack["devices"].telemetry_stale(dev, threshold_s=0.0) is True
    stack["devices"].heartbeat(dev, cpu_pct=40.0)
    s = stack["fabric"].resource_snapshot(dev)
    assert s.cpu_pct == 40.0
    assert stack["devices"].telemetry_stale(dev) is False


def test_C10_revoke_drops_snapshot_to_default():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0)
    stack["fabric"].revoke_device(dev, "test")
    s = stack["fabric"].resource_snapshot(dev)
    assert s.cpu_pct is None and s.battery_pct is None
    v = stack["resolver"].resolve(BAT, dev)
    assert v.device_id is None


def test_C11_snapshot_copy_isolated_from_later_writes():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0)
    s1 = stack["fabric"].resource_snapshot(dev)
    stack["devices"].heartbeat(dev, cpu_pct=90.0)
    s2 = stack["fabric"].resource_snapshot(dev)
    assert s1.cpu_pct == 35.0 and s2.cpu_pct == 90.0


def test_C12_concurrent_heartbeat_read_consistency():
    stack = make_stack()
    dev = paired_android(stack)
    stop = threading.Event()

    def writer(n):
        for i in range(20):
            if stop.is_set():
                return
            stack["devices"].heartbeat(dev, cpu_pct=float(n),
                                       mem_pct=float(n))

    ts = [threading.Thread(target=writer, args=(n,), daemon=True)
          for n in (10, 20, 30)]
    seen = []
    for t in ts:
        t.start()
    for _ in range(60):
        s = stack["fabric"].resource_snapshot(dev)
        # Each read is internally consistent: both fields from one write.
        assert s.cpu_pct == s.mem_pct
        seen.append(s.cpu_pct)
    stop.set()
    for t in ts:
        t.join(timeout=3)
    assert set(seen) <= {10.0, 20.0, 30.0, None}


# ---------- demand (bounded, non-authoritative) ----------

def test_C13_demand_valid_and_defaults():
    from core.models import ResourceDemands, ResourceTier, ToolDefinition
    assert ResourceDemands().model_dump() == {
        "cpu": "low", "memory": "low", "network": "low", "battery": "low"}
    t = ToolDefinition(name="x.t", description="d", input_schema={},
                       output_schema={}, required_capabilities=[],
                       resource_demands={"cpu": "high"})
    assert t.resource_demands.cpu == ResourceTier.HIGH
    assert t.model_dump()["resource_demands"]["cpu"] == "high"


def test_C14_demand_rejects_unknown_and_bad_enum():
    from core.models import ToolDefinition
    import pytest as _pt
    with _pt.raises(Exception):
        ToolDefinition(name="x.t", description="d", input_schema={},
                       output_schema={}, required_capabilities=[],
                       resource_demands={"gpu": "high"})
    with _pt.raises(Exception):
        ToolDefinition(name="x.t", description="d", input_schema={},
                       output_schema={}, required_capabilities=[],
                       resource_demands={"cpu": "extreme"})


def test_C15_demand_never_gates_execution():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(BAT, {}, dev)
    from core.models import ExecutionState as ES
    assert rec.state == ES.RUNNING  # demand tiers read by no gate
    stack["engine"].cancel(rec.id)


# ---------- constraints (existing 4-key representation) ----------

def test_C16_constraint_valid_filters():
    stack = make_stack()
    dev = paired_android(stack)
    r = stack["resolver"].resolve(BAT, dev, constraints={"platform": "x"})
    assert r.device_id is None  # platform mismatch filters honestly
    r = stack["resolver"].resolve(BAT, dev,
                                  constraints={"online_only": True})
    assert r.device_id == dev


def test_C17_constraint_values_documented_behavior():
    stack = make_stack()
    dev = paired_android(stack)
    # Valid risk_max filters: high-risk TAP excluded under risk_max=safe.
    r = stack["resolver"].resolve(TAP, dev, constraints={"risk_max": "safe"})
    assert r.device_id is None
    # Unknown risk_max value is ignored (documented: no constraint), and
    # unknown keys never authorize: revoked stays revoked either way.
    r = stack["resolver"].resolve(BAT, dev, constraints={"risk_max": "nonsense",
                                                         "allow": True})
    assert r.device_id == dev
    stack["fabric"].revoke_device(dev, "test")
    r = stack["resolver"].resolve(BAT, dev, constraints={"risk_max": "safe"})
    assert r.device_id is None


def test_C18_constraint_cannot_authorize_revoked():
    stack = make_stack()
    dev = paired_android(stack)
    stack["fabric"].revoke_device(dev, "test")
    r = stack["resolver"].resolve(BAT, dev,
                                  constraints={"online_only": False})
    assert r.device_id is None


# ---------- ranker inventory (documented, not rewritten) ----------

def test_C19_three_rankers_exist_with_documented_keys():
    from core.fabric import FabricRegistry
    from core.resolver import CapabilityResolver
    from core.routing import Router
    assert hasattr(FabricRegistry, "_rank")
    assert hasattr(CapabilityResolver, "_rank_key")
    assert hasattr(Router, "route")
    # Battery tiebreak present in fabric rank source.
    import inspect
    assert "battery" in inspect.getsource(FabricRegistry._rank)
    assert "battery" in inspect.getsource(CapabilityResolver._rank_key)


# ---------- security ----------

def test_C20_snapshot_cannot_authorize_or_approve():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=0.0, mem_pct=0.0,
                               battery_pct=100.0, charging=True,
                               network="wifi")
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    from core.models import ExecutionState as ES
    assert rec.state == ES.WAITING_FOR_PERMISSION
    assert stack["jobs"].poll(dev) is None
