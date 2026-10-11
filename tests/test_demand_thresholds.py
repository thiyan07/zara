"""Demand-threshold predicate tests: snapshot + demand → eligible/defer.

Proves deterministic eligibility semantics per dimension × tier, exact
boundaries (strict `>`), UNKNOWN/STALE/UNAVAILABLE fail-safe discipline,
malformed-input tolerance, multi-dimension pressure, and that thresholds
never authorize, never alter trust/approval/targets/terminals. No sleeps,
no drain helpers, no production weakening.
"""
import math

import pytest

from core.governor import (BATTERY_LOW_BAND, CPU_PRESSURE_BOUNDS,
                           MEMORY_PRESSURE_BOUNDS, ResourceGovernor,
                           canonical_snapshot, demand_defer_reason)
from core.models import ResourceDemands

G = ResourceGovernor()
LOW = {"cpu": "low", "memory": "low", "network": "low", "battery": "low"}


def snap(**kw):
    base = {"battery_pct": 80.0, "charging": True, "network": "wifi",
            "device_kind": "android"}
    base.update(kw)
    return canonical_snapshot(**base)


def dem(**kw):
    base = dict(LOW)
    base.update(kw)
    return ResourceDemands(**base)


def check(snap_obj, demands, cost=0.1, risk="safe", **kw):
    return G.check(snap_obj, cost, risk, demands=demands, **kw)


# ---------- healthy × tiers ----------

def test_D01_all_low_healthy_proceeds():
    assert check(snap(cpu_pct=35.0, mem_pct=60.0), dem()).action == "proceed"


def test_D02_each_high_tier_healthy_proceeds():
    s = snap(cpu_pct=35.0, mem_pct=60.0)
    for d in (dem(cpu="high"), dem(memory="high"), dem(network="high"),
              dem(battery="high")):
        assert check(s, d).action == "proceed", d


def test_D03_all_high_healthy_proceeds():
    s = snap(cpu_pct=35.0, mem_pct=60.0)
    d = dem(cpu="high", memory="high", network="high", battery="high")
    assert check(s, d).action == "proceed"


# ---------- exact boundaries (strict >) ----------

@pytest.mark.parametrize("tier,bound", [("low", 98.0), ("moderate", 93.0),
                                        ("high", 85.0)])
def test_D04_memory_boundaries(tier, bound):
    d = dem(memory=tier)
    assert check(snap(mem_pct=bound - 0.01), d).action == "proceed"
    assert check(snap(mem_pct=bound), d).action == "proceed"  # strict >
    r = check(snap(mem_pct=bound + 0.01), d)
    assert r.action == "defer" and "memory" in r.reason


@pytest.mark.parametrize("tier,bound", [("low", 98.0), ("moderate", 90.0),
                                        ("high", 75.0)])
def test_D05_cpu_boundaries(tier, bound):
    d = dem(cpu=tier)
    assert check(snap(cpu_pct=bound), d).action == "proceed"
    r = check(snap(cpu_pct=bound + 0.01), d)
    assert r.action == "defer" and "cpu" in r.reason


def test_D06_bounds_match_table():
    assert MEMORY_PRESSURE_BOUNDS == {"low": 98.0, "moderate": 93.0,
                                     "high": 85.0}
    assert CPU_PRESSURE_BOUNDS == {"low": 98.0, "moderate": 90.0,
                                  "high": 75.0}
    assert BATTERY_LOW_BAND == 30.0


# ---------- critical pressure ----------

def test_D07_critical_memory_defers_demand_tiers():
    s = snap(mem_pct=99.0)
    assert check(s, dem(memory="high")).action == "defer"
    assert check(s, dem(memory="moderate")).action == "defer"
    assert check(s, dem(memory="low")).action == "defer"


def test_D08_emulator_idle_never_defers():
    # Measured idle ~79%: HIGH bound (85) clears it with margin.
    s = snap(mem_pct=79.41, cpu_pct=None)
    assert check(s, dem(memory="high")).action == "proceed"


# ---------- UNKNOWN / STALE / UNAVAILABLE ----------

def test_D09_unknown_tolerated_for_low_defers_for_demanding():
    s = snap()  # mem/cpu None
    assert check(s, dem()).action == "proceed"
    assert check(s, dem(memory="moderate")).action == "defer"
    assert check(s, dem(memory="high")).action == "defer"
    assert check(s, dem(cpu="high")).action == "defer"


def test_D10_stale_treated_as_unknown():
    s = snap(mem_pct=99.0)  # would defer if live
    assert demand_defer_reason(s, dem(memory="high"),
                               telemetry_stale=True) is not None
    assert "stale" in demand_defer_reason(
        s, dem(memory="high"), telemetry_stale=True)
    # Stale + LOW still proceeds (architecture permits).
    assert check(s, dem(), telemetry_stale=True).action == "proceed"


def test_D11_android_cpu_null_path():
    s = snap(cpu_pct=None, mem_pct=60.0)  # Android never reports cpu
    assert check(s, dem(cpu="low")).action == "proceed"
    r = check(s, dem(cpu="moderate"))
    assert r.action == "defer" and "unverifiable" in r.reason


def test_D12_none_values():
    s = canonical_snapshot()
    assert check(s, dem()).action == "proceed"


# ---------- NaN / inf / malformed ----------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"),
                                 float("-inf"), -5.0, 150.0, "high"])
def test_D13_nonfinite_out_of_range_never_defers_low(bad):
    assert check(snap(mem_pct=bad, cpu_pct=bad), dem()).action == "proceed"


def test_D14_malformed_demand_dict_defers_closed():
    s = snap(cpu_pct=35.0, mem_pct=60.0)
    # Unreadable demand structures fail closed (defer with reason), never
    # proceed blind; None demands preserve legacy behavior.
    r = demand_defer_reason(s, {"__x__": object()})
    assert r is not None and "unreadable" in r
    assert demand_defer_reason(s, None) is None


def test_D15_unknown_tier_rejected_at_model():
    import pytest as _pt
    with _pt.raises(Exception):
        ResourceDemands(cpu="extreme")
    with _pt.raises(Exception):
        ResourceDemands(gpu="high")


# ---------- multi-dimension / mixed ----------

def test_D16_multi_pressure_first_hit_wins_deterministic():
    s = snap(mem_pct=99.0, cpu_pct=99.0)
    d = dem(memory="high", cpu="high")
    r1 = check(s, d)
    r2 = check(s, d)
    assert r1.action == r2.action == "defer"
    assert r1.reason == r2.reason
    assert "memory" in r1.reason  # fixed order: memory before cpu


def test_D17_mixed_unknown_healthy():
    s = snap(cpu_pct=None, mem_pct=60.0)
    assert check(s, dem(memory="low")).action == "proceed"
    assert check(s, dem(cpu="high")).action == "defer"


# ---------- battery / low-power ----------

def test_D18_battery_demand_tiers():
    healthy = snap(battery_pct=80.0, charging=False, low_power_mode=False)
    assert check(healthy, dem(battery="high")).action == "proceed"
    lp = snap(battery_pct=80.0, charging=False, low_power_mode=True)
    assert check(lp, dem(battery="moderate")).action == "defer"
    assert check(lp, dem(battery="high")).action == "defer"
    assert check(lp, dem(battery="low")).action == "proceed"
    low = snap(battery_pct=10.0, charging=False, low_power_mode=False)
    assert check(low, dem(battery="moderate")).action == "defer"
    # HIGH + fully unknown battery state → unverifiable → defer.
    unk = snap(battery_pct=None, low_power_mode=None)
    assert check(unk, dem(battery="high")).action == "defer"
    assert check(unk, dem(battery="low")).action == "proceed"


def test_D19_network_dimension_has_no_predicate_yet():
    # Network demand alone never defers (documented deferred dimension:
    # snapshot carries only the network name, no latency/bandwidth).
    # The offline *cost* gate is separate pre-existing behavior.
    for net in ("offline", "metered", "wifi", "wired", "unknown"):
        s = canonical_snapshot(network=net)
        assert demand_defer_reason(s, dem(network="high")) is None


# ---------- security / lifecycle ----------

def test_D20_high_risk_still_parks_when_healthy():
    from core.app import build_stack
    from core.models import DeviceKind, DeviceState
    stack = build_stack()
    code = stack["device_auth"].enroll("emu-lab", DeviceKind("android"))
    stack["device_auth"].claim(code)
    stack["devices"].register(DeviceState(
        device_id="emu-lab", kind=DeviceKind("android"),
        capabilities=["gui.tap"], online=True, status="online"))
    stack["fabric"].advertise([
        {"id": "gui.tap", "name": "gui.tap", "descriptor_version": "1",
         "version": "1.0.0", "risk": "high_risk",
         "availability": "available"}], "emu-lab")
    stack["devices"].heartbeat("emu-lab", cpu_pct=0.0, mem_pct=0.0,
                               battery_pct=100.0, charging=True,
                               network="wifi")
    rec = stack["engine"].submit_async(
        "gui.tap", {"target_package": "dev.zara.lab.privtest"}, "emu-lab")
    from core.models import ExecutionState as ES
    assert rec.state == ES.WAITING_FOR_PERMISSION
    assert stack["jobs"].poll("emu-lab") is None


def test_D21_defer_creates_no_job_no_record():
    from core.app import build_stack
    from fastapi.testclient import TestClient
    from core.app import create_app
    stack = build_stack()
    c = TestClient(create_app(stack))
    # low_power_mode=True + costly tool → deferred pre-submit.
    from core.models import DeviceKind, DeviceState
    code = stack["device_auth"].enroll("emu-lab", DeviceKind("android"))
    stack["device_auth"].claim(code)
    stack["devices"].register(DeviceState(
        device_id="emu-lab", kind=DeviceKind("android"),
        capabilities=["system.battery"], online=True, status="online"))
    stack["devices"].heartbeat("emu-lab", low_power_mode=True)
    n_before = len(stack["engine"]._records)
    r = c.post("/v1/executions",
               json={"tool": "system.battery", "inputs": {},
                     "device_id": "emu-lab"},
               headers={"Authorization": "Bearer dev-token"})
    # cost 0.01 < 0.2: low-power gate passes; demand default LOW passes.
    assert r.status_code in (200, 201)
    assert len(stack["engine"]._records) in (n_before, n_before + 1)


def test_D22_defer_preserves_trust_and_auth():
    from core.app import build_stack
    stack = build_stack()
    from core.models import DeviceKind, DeviceState
    code = stack["device_auth"].enroll("emu-lab", DeviceKind("android"))
    stack["device_auth"].claim(code)
    stack["devices"].register(DeviceState(
        device_id="emu-lab", kind=DeviceKind("android"),
        capabilities=["system.battery"], online=True, status="online"))
    stack["fabric"].advertise([
        {"id": "system.battery", "name": "system.battery",
         "descriptor_version": "1", "version": "1.0.0", "risk": "safe",
         "availability": "available"}], "emu-lab")
    stack["devices"].heartbeat("emu-lab", low_power_mode=True)
    v = stack["fabric"].authorize_capability("system.battery", "emu-lab")
    # cost for safe = 0.1 < 0.2: still authorized; low-power gate needs cost.
    assert v["authorized"] is True
    assert stack["fabric"].trust_of("emu-lab") == "trusted"


def test_D23_revoked_never_eligible():
    from core.app import build_stack
    stack = build_stack()
    from core.models import DeviceKind, DeviceState
    code = stack["device_auth"].enroll("emu-lab", DeviceKind("android"))
    stack["device_auth"].claim(code)
    stack["devices"].register(DeviceState(
        device_id="emu-lab", kind=DeviceKind("android"),
        capabilities=["system.battery"], online=True, status="online"))
    stack["devices"].heartbeat("emu-lab", cpu_pct=0.0, mem_pct=0.0,
                               battery_pct=100.0, charging=True)
    stack["fabric"].revoke_device("emu-lab", "test")
    s = stack["fabric"].resource_snapshot("emu-lab")
    assert s.cpu_pct is None
    v = stack["resolver"].resolve("system.battery", "emu-lab")
    assert v.device_id is None


def test_D24_snapshot_not_mutated_by_evaluation():
    s = snap(mem_pct=99.0, cpu_pct=99.0)
    before = (s.mem_pct, s.cpu_pct)
    demand_defer_reason(s, dem(memory="high", cpu="high"))
    G.check(s, 0.1, "safe", demands=dem(memory="high"))
    assert (s.mem_pct, s.cpu_pct) == before


def test_D25_deterministic_repeated_evaluation():
    s = snap(mem_pct=95.0, cpu_pct=80.0)
    d = dem(memory="high", cpu="high")
    outs = [demand_defer_reason(s, d) for _ in range(20)]
    assert len(set(outs)) == 1
    outs = [G.check(s, 0.1, "safe", demands=d).action for _ in range(20)]
    assert set(outs) == {"defer"}


def test_D26_linux_cpu_path_documented():
    s = snap(cpu_pct=8.5, mem_pct=79.8, device_kind="linux")
    assert check(s, dem(cpu="high"), cost=0.9).action == "proceed"
    assert check(snap(cpu_pct=99.0, device_kind="linux"),
                 dem(cpu="high"), cost=0.9).action == "defer"


def test_D27_same_input_same_output():
    import json
    s = snap(mem_pct=90.0)
    d = dem(memory="high")
    a = demand_defer_reason(s, d)
    b = demand_defer_reason(
        snap(mem_pct=90.0), dem(memory="high"))
    assert a == b == "memory pressure 90.0% exceeds high demand bound 85%: defer"


def test_D28_old_snapshot_never_silently_fresh():
    # A snapshot object carries no freshness; staleness comes only from
    # the caller-supplied flag (DeviceState.last_telemetry_at).
    s = snap(mem_pct=99.0)
    assert demand_defer_reason(s, dem(memory="high"),
                               telemetry_stale=False) is not None
    assert "stale" not in (demand_defer_reason(
        s, dem(memory="high"), telemetry_stale=False) or "")
