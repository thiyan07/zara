"""Telemetry ingestion contract gaps: full-field store/retrieve, partial
merge pin, stale-null scope, unknown-never-zero, invalid-type 422, revoked
403.

Complements (not duplicates) tests/test_resource_telemetry.py and
tests/test_telemetry_expansion.py:
- R_/X_ suites cover: bounds/NaN rejection at payload level, storage_total
  plumbing, free>total drop, stale null of cpu/mem/low_power, direct-clamp
  layer, thermal enum, last-write-wins, snapshot isolation, HTTP roundtrips.
- This file covers ONLY the six ingestion-contract gaps below. No sleeps;
  staleness via backdated last_telemetry_at / threshold_s=0.0.
"""
from datetime import timedelta

from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.models import DeviceKind, DeviceState, utcnow

OP = {"Authorization": "Bearer dev-token"}


def make_stack(fabric_db=""):
    return build_stack(fabric_db=fabric_db)


def enroll_claim(stack, device_id, kind="android"):
    code = stack["device_auth"].enroll(device_id, DeviceKind(kind))
    return stack["device_auth"].claim(code)


def register(stack, device_id, kind="android", caps=None):
    stack["devices"].register(DeviceState(
        device_id=device_id, kind=DeviceKind(kind),
        capabilities=caps or [], online=True, status="online"))


def paired_android(stack, dev="emu-lab"):
    enroll_claim(stack, dev, "android")
    register(stack, dev, "android", ["system.battery"])
    return dev


def http_pair(stack, device_id, kind="android"):
    """Enroll/claim/register a device over HTTP; return device headers."""
    c = TestClient(create_app(stack))
    r = c.post("/v1/agent/enroll",
               json={"device_id": device_id, "kind": kind}, headers=OP)
    assert r.status_code == 200, r.text
    r = c.post("/v1/agent/claim", json={"pairing_code": r.json()["pairing_code"]})
    assert r.status_code == 200, r.text
    h = {"X-Device-Id": device_id, "X-Device-Key": r.json()["device_key"]}
    r = c.post("/v1/agent/register",
               json={"capabilities": [], "kind": kind,
                     "software_version": "t"}, headers=h)
    assert r.status_code == 200, r.text
    return c, h


FULL = {"battery_pct": 77.0, "charging": True, "network": "wifi",
        "cpu_pct": 33.5, "mem_pct": 61.0, "low_power_mode": False,
        "storage_free_mb": 9000, "storage_total_mb": 32000,
        "battery_temp_c": 30.5, "thermal_state": "light"}


def test_I01_full_heartbeat_stores_all_fields_retrievably():
    stack = make_stack()
    c, h = http_pair(stack, "hb-full")
    r = c.post("/v1/agent/heartbeat", json=dict(FULL), headers=h)
    assert r.status_code == 200, r.text
    v = stack["fabric"].device_view("hb-full").as_dict()
    for k, want in FULL.items():
        assert v[k] == want, (k, v[k])
    s = stack["fabric"].resource_snapshot("hb-full")
    assert (s.cpu_pct, s.mem_pct, s.low_power_mode) == (33.5, 61.0, False)
    assert (s.storage_free_mb, s.storage_total_mb) == (9000, 32000)
    assert (s.battery_temp_c, s.thermal_state) == (30.5, "light")
    assert (s.battery_pct, s.charging, s.network) == (77.0, True, "wifi")


def test_I02_partial_heartbeat_merges_keeps_prior_fields():
    # ACTUAL (pinned): telemetry fields merge — omitted fields KEEP their
    # prior stored values (never nulled); only a valid new value overwrites.
    # FLAG if surprising: a mem-only heartbeat refreshes last_telemetry_at
    # (whole-store freshness), so a single live axis marks all axes fresh.
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, **FULL)
    before = stack["devices"].get(dev).last_telemetry_at
    assert before is not None
    d = stack["devices"].heartbeat(dev, mem_pct=22.0)
    assert d.mem_pct == 22.0
    assert d.cpu_pct == 33.5  # kept, not nulled
    assert (d.storage_free_mb, d.storage_total_mb) == (9000, 32000)
    assert (d.battery_temp_c, d.thermal_state) == (30.5, "light")
    assert d.low_power_mode is False
    assert d.last_telemetry_at is not None and d.last_telemetry_at >= before


def test_I03_stale_snapshot_nulls_telemetry_keeps_presence_adjacent():
    # X04 pins stale-null of cpu/mem/low_power + battery kept; this pins the
    # REST of the scope: storage/temp/thermal nulled, charging/network kept,
    # presence stays ONLINE (presence owns its own freshness via last_seen).
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].heartbeat(dev, **FULL)
    stack["devices"].get(dev).last_telemetry_at = \
        utcnow() - timedelta(seconds=300)
    assert stack["devices"].telemetry_stale(dev) is True
    assert stack["devices"].telemetry_stale(dev, threshold_s=0.0) is True
    s = stack["fabric"].resource_snapshot(dev)
    assert s.cpu_pct is None and s.mem_pct is None
    assert s.low_power_mode is None
    assert s.storage_free_mb is None and s.storage_total_mb is None
    assert s.battery_temp_c is None and s.thermal_state is None
    assert s.battery_pct == 77.0 and s.charging is True
    assert s.network == "wifi"
    assert stack["fabric"].presence_of(dev) == "online"


def test_I04_fresh_device_telemetry_is_none_not_zero():
    stack = make_stack()
    dev = paired_android(stack, "fresh-unknown")
    v = stack["fabric"].device_view(dev).as_dict()
    assert v["mem_pct"] is None and v["mem_pct"] != 0
    assert v["cpu_pct"] is None and v["battery_temp_c"] is None
    assert v["storage_free_mb"] is None and v["thermal_state"] is None
    s = stack["fabric"].resource_snapshot(dev)
    assert s.mem_pct is None and s.cpu_pct is None
    assert s.storage_free_mb is None and s.thermal_state is None


def test_I05_invalid_types_rejected_stored_values_intact():
    stack = make_stack()
    c, h = http_pair(stack, "hb-badtype")
    r = c.post("/v1/agent/heartbeat", json=dict(FULL), headers=h)
    assert r.status_code == 200, r.text
    for bad in ({"mem_pct": "high"},          # wrong type
                {"battery_temp_c": "nan"},    # NaN-as-string: must 422
                {"battery_temp_c": "hot"},    # non-numeric string
                {"thermal_state": "x" * 17}):  # length cap
        r = c.post("/v1/agent/heartbeat", json=bad, headers=h)
        assert r.status_code == 422, (bad, r.status_code, r.text)
    d = stack["devices"].get("hb-badtype")
    assert (d.mem_pct, d.battery_temp_c, d.thermal_state) == \
        (61.0, 30.5, "light")


def test_I06_revoked_heartbeat_403_stored_telemetry_untouched():
    # Auth-level revoke: credential dies, DeviceManager record (and its
    # telemetry) stays. Heartbeat must fail closed 403 without touching it.
    stack = make_stack()
    c, h = http_pair(stack, "hb-revoked")
    r = c.post("/v1/agent/heartbeat", json=dict(FULL), headers=h)
    assert r.status_code == 200, r.text
    stack["device_auth"].revoke("hb-revoked")
    r = c.post("/v1/agent/heartbeat", json={"mem_pct": 10.0}, headers=h)
    assert r.status_code == 403, (r.status_code, r.text)
    d = stack["devices"].get("hb-revoked")
    assert (d.mem_pct, d.cpu_pct, d.battery_temp_c) == (61.0, 33.5, 30.5)
    assert (d.storage_free_mb, d.thermal_state) == (9000, "light")
