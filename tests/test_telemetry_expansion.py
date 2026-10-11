"""Telemetry expansion tests: storage_total, stale-null, validation
hardening, Linux true-CPU/battery-temp, out-of-order, isolation.

All ingestion through real paths (devices.heartbeat / HTTP heartbeat /
resource_snapshot). No drains, no sleeps (staleness via threshold_s=0.0).
"""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.app import build_stack, create_app
from core.models import DeviceKind, DeviceState
from core.protocol import HeartbeatPayload

OP = {"Authorization": "Bearer dev-token"}
INSPECT = "gui.screen.inspect"
TAP = "gui.tap"
BAT = "system.battery"


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


# ---------- storage_total plumbing ----------

def test_X01_storage_total_reaches_snapshot():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, storage_free_mb=8000,
                               storage_total_mb=16000)
    s = stack["fabric"].resource_snapshot(dev)
    assert (s.storage_free_mb, s.storage_total_mb) == (8000, 16000)
    v = stack["fabric"].device_view(dev).as_dict()
    assert (v["storage_free_mb"], v["storage_total_mb"]) == (8000, 16000)


def test_X02_storage_bounds_capped():
    with pytest.raises(ValidationError):
        HeartbeatPayload(storage_free_mb=10**7 + 1)
    with pytest.raises(ValidationError):
        HeartbeatPayload(storage_total_mb=-1)


def test_X03_free_gt_total_drops_both():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, storage_free_mb=9000,
                               storage_total_mb=8000)
    d = stack["devices"].get(dev)
    assert d.storage_free_mb is None and d.storage_total_mb is None


# ---------- stale-null at build ----------

def test_X04_stale_snapshot_nulls_telemetry():
    from datetime import timedelta
    from core.models import utcnow
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0, mem_pct=60.0,
                               low_power_mode=True, battery_pct=80.0)
    # Age the telemetry store past the threshold (no sleeps).
    d = stack["devices"].get(dev)
    d.last_telemetry_at = utcnow() - timedelta(seconds=300)
    assert stack["devices"].telemetry_stale(dev) is True
    assert stack["devices"].telemetry_stale(dev, threshold_s=0.0) is True
    s = stack["fabric"].resource_snapshot(dev)
    assert s.cpu_pct is None and s.mem_pct is None
    assert s.low_power_mode is None
    # Battery keeps legacy presence-adjacent behavior (not nulled).
    assert s.battery_pct == 80.0


def test_X05_fresh_snapshot_keeps_telemetry():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0, low_power_mode=True)
    s = stack["fabric"].resource_snapshot(dev)
    assert (s.cpu_pct, s.low_power_mode) == (35.0, True)


# ---------- second-layer clamp (non-HTTP writers) ----------

def test_X06_direct_heartbeat_clamps():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=101.0, mem_pct=-5.0,
                               storage_free_mb=10**9,
                               thermal_state="quantum")
    d = stack["devices"].get(dev)
    assert d.cpu_pct is None and d.mem_pct is None
    assert d.storage_free_mb is None and d.thermal_state is None


def test_X07_direct_heartbeat_accepts_valid():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0, thermal_state="light")
    d = stack["devices"].get(dev)
    assert (d.cpu_pct, d.thermal_state) == (35.0, "light")


def test_X08_bool_is_not_a_number():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=True, mem_pct=False)
    d = stack["devices"].get(dev)
    assert d.cpu_pct is None and d.mem_pct is None


# ---------- thermal enum ----------

def test_X09_thermal_known_states_pass_unknown_dropped():
    stack = make_stack()
    dev = paired_android(stack)
    for known in ("none", "light", "moderate", "severe", "critical",
                  "emergency", "shutdown", "unknown"):
        stack["devices"].heartbeat(dev, thermal_state=known)
        assert stack["devices"].get(dev).thermal_state == known
    # Unknown strings are dropped (prior value kept), never stored.
    stack["devices"].heartbeat(dev, thermal_state="lukewarm")
    assert stack["devices"].get(dev).thermal_state == "unknown"


def test_X10_thermal_http_length_still_capped():
    with pytest.raises(ValidationError):
        HeartbeatPayload(thermal_state="x" * 17)


# ---------- out-of-order / isolation ----------

def test_X11_last_write_wins_documented():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=90.0)
    stack["devices"].heartbeat(dev, cpu_pct=20.0)
    assert stack["fabric"].resource_snapshot(dev).cpu_pct == 20.0
    stack["devices"].heartbeat(dev, cpu_pct=90.0)
    assert stack["fabric"].resource_snapshot(dev).cpu_pct == 90.0


def test_X12_snapshot_copy_isolated():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, cpu_pct=35.0, storage_free_mb=1000,
                               thermal_state="light")
    s1 = stack["fabric"].resource_snapshot(dev)
    stack["devices"].heartbeat(dev, cpu_pct=90.0, storage_free_mb=2000,
                               thermal_state="severe")
    s2 = stack["fabric"].resource_snapshot(dev)
    assert (s1.cpu_pct, s1.storage_free_mb, s1.thermal_state) == \
        (35.0, 1000, "light")
    assert (s2.cpu_pct, s2.storage_free_mb, s2.thermal_state) == \
        (90.0, 2000, "severe")


# ---------- Linux helpers ----------

def test_X13_linux_telemetry_shape():
    from device.linux.tools_linux import system_telemetry
    t = system_telemetry({}, {})
    assert 0.0 <= t["cpu_pct"] <= 100.0
    assert t.get("cpu_source") in ("proc_stat", "loadavg_approx")
    assert 0 <= t["mem_avail_mb"] <= t["mem_total_mb"]
    assert 0.0 <= t["mem_pct"] <= 100.0
    assert 0 <= t["storage_free_mb"] <= t["storage_total_mb"]
    assert "thermal_state" not in t  # no portable source: omitted
    assert "battery_temp_c" not in t or isinstance(
        t["battery_temp_c"], float)


def test_X14_linux_no_new_dependency():
    import subprocess
    out = subprocess.run(["grep", "-rn", "import psutil",
                          "device/linux/"], capture_output=True, text=True)
    assert out.stdout.strip() == ""


# ---------- demand compatibility ----------

def test_X15_storage_fields_never_gate():
    from core.governor import ResourceGovernor, canonical_snapshot
    s = canonical_snapshot(storage_free_mb=1, storage_total_mb=10**7,
                           battery_temp_c=99.0, thermal_state="critical")
    assert ResourceGovernor().check(s, 0.9, "high_risk").action in (
        "proceed", "reroute")


def test_X16_stale_low_power_no_longer_defers():
    # Stale low-power reads as unknown at build → no gate on that axis
    # (fail-safe direction documented; presence-STALE still denies at
    # authorize for genuinely stale devices).
    from datetime import timedelta
    from core.models import utcnow
    from core.governor import ResourceGovernor, canonical_snapshot
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, low_power_mode=True)
    stack["devices"].get(dev).last_telemetry_at = \
        utcnow() - timedelta(seconds=300)
    assert stack["devices"].telemetry_stale(dev) is True
    assert stack["devices"].telemetry_stale(dev, threshold_s=0.0) is True
    s = stack["fabric"].resource_snapshot(dev)
    assert s.low_power_mode is None
    assert ResourceGovernor().check(s, 0.5, "safe").action == "proceed"


# ---------- HTTP ----------

def test_X17_http_roundtrip_new_fields():
    stack = make_stack()
    c = TestClient(create_app(stack))
    r = c.post("/v1/agent/enroll", json={"device_id": "hb3", "kind": "linux"},
               headers=OP)
    code = r.json()["pairing_code"]
    r = c.post("/v1/agent/claim", json={"pairing_code": code})
    h = {"X-Device-Id": "hb3", "X-Device-Key": r.json()["device_key"]}
    r = c.post("/v1/agent/register",
               json={"capabilities": [], "kind": "linux",
                     "software_version": "t"}, headers=h)
    assert r.status_code == 200, r.text
    r = c.post("/v1/agent/heartbeat",
               json={"storage_free_mb": 8000, "storage_total_mb": 16000,
                     "thermal_state": "light", "unknown_future": 1},
               headers=h)
    assert r.status_code == 200, r.text
    d = stack["devices"].get("hb3")
    assert (d.storage_free_mb, d.storage_total_mb) == (8000, 16000)
    r = c.post("/v1/agent/heartbeat",
               json={"storage_free_mb": 10**7 + 1}, headers=h)
    assert r.status_code == 422
    assert stack["devices"].get("hb3").storage_free_mb == 8000
