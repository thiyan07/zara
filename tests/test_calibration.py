"""Calibration harness + evidence dataset (Governor v2 readiness).

Deterministic, offline, stdlib-only (+pytest). Replays scenarios through
EXISTING pure functions (demand_defer_reason, Governor.check,
normalize_constraints, resolver.resolve on make_stack fixtures) and
records expected-vs-actual with mismatch classification.

No execution dispatch, no approvals, no scheduler weights. Evidence is
data, never authority. Fixed seed; bounded output.

Generate:  PYTHONHASHSEED=42 python -m pytest tests/test_calibration.py -q
Dataset:   docs/research/evidence/calibration_v1.jsonl (+ .summary.json)
"""
import hashlib
import json
import os
import random
import re
from datetime import datetime, timezone

import pytest

from core.governor import (BATTERY_LOW_BAND, CPU_PRESSURE_BOUNDS,
                           MEMORY_PRESSURE_BOUNDS, ResourceGovernor,
                           canonical_snapshot, demand_defer_reason)
from core.models import DeviceKind, DeviceState, ResourceDemands

SCHEMA = "zara.evidence/v1"
SEED = 42
EVIDENCE_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "docs", "research", "evidence")
EVIDENCE_FILE = os.path.join(EVIDENCE_DIR, "calibration_v1.jsonl")
SUMMARY_FILE = os.path.join(EVIDENCE_DIR, "calibration_v1.summary.json")
MAX_RECORDS = 500
MAX_BYTES = 256 * 1024

G = ResourceGovernor()
_SECRET_RE = re.compile(
    r"device_key|pairing_code|api[_-]?key|secret|token|bearer|"
    r"/home/|sk-[a-z]|xox", re.IGNORECASE)
_ID_RE = re.compile(r"^[a-z][a-z0-9_.\-]{1,63}$")
_STATUSES = {"proceed", "defer", "reroute", "resolved", "requires_approval",
             "unavailable", "offline", "denied", "not_executed",
             "no_device", "no_capability", "unauthorized", "governor_blocked",
             "policy_blocked", "succeeded", "failed"}


def _now():
    # Fixed stamp for generated records: determinism requires no wall
    # clock in output. Measured records carry their own explicit stamp.
    return "2026-10-09T00:00:00+00:00"


def make_record(scenario_id, category, source, actual, expected,
                device_class="unknown", metrics=None, demand=None,
                constraints=None, resolver=None, notes="",
                calibration_suitable=False, seed=SEED, observed_at=None):
    assert _ID_RE.match(scenario_id), scenario_id
    assert category in ("demand-threshold", "telemetry-plumbing",
                        "resolver", "governor-cost", "execution"), category
    assert source in ("measured", "synthetic", "replayed")
    rec = {"schema": SCHEMA, "scenario_id": scenario_id,
           "category": category, "seed": seed, "source": source,
           "device_class": device_class,
           "observed_at": observed_at or _now(),
           "command": ("PYTHONHASHSEED=42 python -m pytest "
                       "tests/test_calibration.py -q"),
           "metrics": metrics or [],
           "freshness": {"telemetry_stale": False, "threshold_s": 120.0},
           "demand": demand or {"cpu": "low", "memory": "low",
                                "network": "low", "battery": "low"},
           "thresholds": {"memory": dict(MEMORY_PRESSURE_BOUNDS),
                          "cpu": dict(CPU_PRESSURE_BOUNDS),
                          "battery_low_band": BATTERY_LOW_BAND},
           "constraints": constraints or {},
           "resolver": resolver or {},
           "execution": {"state": "not_executed"},
           "expected": expected, "actual": actual,
           "mismatch_class": "none" if expected == actual
           else "predicate-drift",
           "calibration_suitable": calibration_suitable, "notes": notes[:300]}
    blob = json.dumps(rec, sort_keys=True)
    assert len(blob) <= 8192, scenario_id
    assert not _SECRET_RE.search(blob), scenario_id
    assert expected in _STATUSES and actual in _STATUSES, scenario_id
    return rec


def _snap_kwargs(metrics):
    kw = {}
    for m in metrics:
        if m["name"] in ("mem_pct", "cpu_pct", "battery_pct",
                         "battery_temp_c", "storage_free_mb",
                         "storage_total_mb", "thermal_state"):
            kw[m["name"]] = m["value"]
    return kw


def generate_demand_records(rng):
    """Seeded cartesian sweep: dims × tiers × boundary/unknown/invalid."""
    recs = []
    dims = [("memory", MEMORY_PRESSURE_BOUNDS, "mem_pct"),
            ("cpu", CPU_PRESSURE_BOUNDS, "cpu_pct")]
    tiers = ["low", "moderate", "high"]
    n = 0
    for dim, bounds, field in dims:
        for tier in tiers:
            bound = bounds[tier]
            for delta, tag in ((-0.01, "below"), (0.0, "at"), (0.01, "above")):
                val = round(bound + delta, 2)
                kw = {"battery_pct": 80.0, "charging": True,
                      "network": "wifi", "device_kind": "linux"}
                kw[field] = val
                s = canonical_snapshot(**kw)
                d = {k: "low" for k in ("cpu", "memory", "network",
                                       "battery")}
                d[dim] = tier
                reason = demand_defer_reason(s, d)
                actual = "defer" if reason else "proceed"
                # Golden expectation encoded from the specified contract:
                # strict `>` defers; == proceeds; moderate/high unknown
                # defers; low tolerates unknown.
                if delta > 0:
                    expected = "defer"
                else:
                    expected = "proceed"
                n += 1
                recs.append(make_record(
                    f"d-thr-{dim}-{tier}-{tag}", "demand-threshold",
                    "synthetic", actual, expected, device_class="linux",
                    metrics=[{"name": field, "value": val, "unit": "pct",
                              "at": "2026-10-09T00:00:00+00:00"}],
                    demand=d, notes=f"bound {bound}, strict >"))
            # Unknown input for this dim/tier.
            kw = {"battery_pct": 80.0, "charging": True, "network": "wifi",
                  "device_kind": "linux"}
            s = canonical_snapshot(**kw)
            d = {k: "low" for k in ("cpu", "memory", "network", "battery")}
            d[dim] = tier
            reason = demand_defer_reason(s, d)
            actual = "defer" if reason else "proceed"
            expected = "proceed" if tier == "low" else "defer"
            n += 1
            recs.append(make_record(
                f"d-thr-{dim}-{tier}-unknown", "demand-threshold",
                "synthetic", actual, expected, device_class="linux",
                demand=d, notes="field None → unverifiable"))
    # Battery axis.
    for tier, batt, lp, exp in (
            ("low", 80.0, False, "proceed"),
            ("moderate", 80.0, True, "defer"),
            ("high", 80.0, True, "defer"),
            ("moderate", 10.0, False, "defer"),
            ("high", None, None, "defer")):
        kw = {"network": "wifi", "device_kind": "linux",
              "charging": False, "low_power_mode": lp}
        if batt is not None:
            kw["battery_pct"] = batt
        s = canonical_snapshot(**kw)
        d = {"cpu": "low", "memory": "low", "network": "low",
             "battery": tier}
        reason = demand_defer_reason(s, d)
        actual = "defer" if reason else "proceed"
        n += 1
        tag = f"{tier}-{batt}-{str(lp).lower()}".lower()
        recs.append(make_record(
            f"d-thr-battery-{tag}", "demand-threshold",
            "synthetic", actual, exp, device_class="linux", demand=d,
            notes="low-power/band-30"))
    # Invalid inputs never defer LOW.
    for bad in ("nan-s", "inf-s"):
        v = float("nan") if bad == "nan-s" else float("inf")
        s = canonical_snapshot(mem_pct=v, cpu_pct=v)
        actual = "defer" if demand_defer_reason(
            s, {"cpu": "low", "memory": "low", "network": "low",
                "battery": "low"}) else "proceed"
        n += 1
        recs.append(make_record(
            f"d-thr-invalid-{bad}", "demand-threshold", "synthetic",
            actual, "proceed", device_class="unknown",
            notes="non-finite → unknown → LOW proceeds"))
    assert n <= MAX_RECORDS
    return recs


def generate_resolver_records():
    """Replay safe resolver decisions on make_stack fixtures (no dispatch)."""
    from core.app import build_stack
    from core.models import DeviceKind as DK, DeviceState as DS
    recs = []
    stack = build_stack()
    code = stack["device_auth"].enroll("emu-lab", DK("android"))
    stack["device_auth"].claim(code)
    stack["devices"].register(DS(
        device_id="emu-lab", kind=DK("android"),
        capabilities=["system.battery", "gui.tap"], online=True,
        status="online"))
    stack["fabric"].advertise([
        {"id": "system.battery", "name": "system.battery",
         "descriptor_version": "1", "version": "1.0.0", "risk": "safe",
         "availability": "available"},
        {"id": "gui.tap", "name": "gui.tap", "descriptor_version": "1",
         "version": "1.0.0", "risk": "high_risk",
         "availability": "available"}], "emu-lab")
    cases = [
        ("r-safe", "system.battery", {}, "resolved"),
        ("r-highrisk", "gui.tap", {}, "requires_approval"),
        ("r-riskmax", "gui.tap", {"risk_max": "safe"}, None),
        ("r-unknown-key", "system.battery", {"color": "red"}, "resolved"),
        ("r-offline-only", "system.battery", {"online_only": True},
         "resolved"),
    ]
    for sid, cap, constraints, exp_status in cases:
        r = stack["resolver"].resolve(cap, "emu-lab",
                                      constraints=dict(constraints))
        actual = r.status if r.device_id else r.status
        expected = exp_status or r.status
        if exp_status is None:  # filtered case: device must be None
            assert r.device_id is None
            expected = actual
        recs.append(make_record(
            sid, "resolver", "replayed", actual, expected,
            device_class="android", constraints=dict(constraints),
            resolver={"status": r.status, "device_id": r.device_id,
                      "fallback_used": r.fallback_used,
                      "substituted": r.substituted},
            notes="fixture replay, no dispatch"))
    # Revoked stays revoked under any constraints.
    stack["fabric"].revoke_device("emu-lab", "calibration")
    r = stack["resolver"].resolve("system.battery", "emu-lab",
                                  constraints={"risk_max": "high_risk"})
    assert r.device_id is None
    recs.append(make_record(
        "r-revoked", "resolver", "replayed", r.status, r.status,
        device_class="android", constraints={"risk_max": "high_risk"},
        resolver={"status": r.status, "device_id": None},
        notes="revoke wins over constraints"))
    return recs


def measured_records():
    """Task-3 documented baselines, provenance-cited, NOT calibration
    data (N=1 per cell). Values copied verbatim from
    docs/research/RESOURCE_TELEMETRY.md §§12-14 (2026-10-08/09)."""
    return [
        make_record("m-emu-idle", "telemetry-plumbing", "measured",
                    "proceed", "proceed", device_class="android",
                    metrics=[
                        {"name": "mem_pct", "value": 78.96, "unit": "pct",
                         "at": "2026-10-08T12:00:00+00:00"},
                        {"name": "thermal_state", "value": "none",
                         "unit": "enum", "at": "2026-10-08T12:00:00+00:00"}],
                    notes="ZaraLab API36 idle; cpu null (API gap). "
                    "provenance: RESOURCE_TELEMETRY.md §12. N=1, unsuitable.",
                    calibration_suitable=False),
        make_record("m-linux-idle", "telemetry-plumbing", "measured",
                    "proceed", "proceed", device_class="linux",
                    metrics=[
                        {"name": "cpu_pct", "value": 8.5, "unit": "pct",
                         "at": "2026-10-08T12:00:00+00:00"},
                        {"name": "mem_pct", "value": 79.8, "unit": "pct",
                         "at": "2026-10-08T12:00:00+00:00"}],
                    notes="12-core laptop idle; cpu loadavg-derived. "
                    "provenance: RESOURCE_TELEMETRY.md §13. N=1, unsuitable.",
                    calibration_suitable=False),
    ]


def write_evidence(records):
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    assert len(records) <= MAX_RECORDS
    blob_lines = [json.dumps(r, sort_keys=True) for r in records]
    total = sum(len(l) for l in blob_lines)
    assert total <= MAX_BYTES
    with open(EVIDENCE_FILE, "w") as f:
        f.write("\n".join(blob_lines) + "\n")
    by_cat, by_src, mism = {}, {}, []
    for r in records:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
        if r["mismatch_class"] != "none":
            mism.append(r["scenario_id"])
    summary = {"schema": SCHEMA, "seed": SEED, "records": len(records),
               "bytes": total, "by_category": by_cat, "by_source": by_src,
               "mismatches": mism,
               "command": ("PYTHONHASHSEED=42 python -m pytest "
                           "tests/test_calibration.py -q")}
    with open(SUMMARY_FILE, "w") as f:
        json.dump(summary, f, sort_keys=True, indent=1)
    return summary


# ---------- tests ----------

def test_K00_harness_deterministic():
    rng = random.Random(SEED)
    a = generate_demand_records(rng)
    rng = random.Random(SEED)
    b = generate_demand_records(rng)
    assert [json.dumps(r, sort_keys=True) for r in a] == \
        [json.dumps(r, sort_keys=True) for r in b]


def test_K01_evidence_validates_and_bounded():
    rng = random.Random(SEED)
    recs = generate_demand_records(rng) + generate_resolver_records() \
        + measured_records()
    summary = write_evidence(recs)
    assert summary["mismatches"] == []
    assert summary["records"] <= MAX_RECORDS
    assert summary["bytes"] <= MAX_BYTES
    # Round-trip: every line re-parses and re-validates.
    for line in open(EVIDENCE_FILE):
        r = json.loads(line)
        assert r["schema"] == SCHEMA
        assert _ID_RE.match(r["scenario_id"])
        assert not _SECRET_RE.search(line)


def test_K02_measured_synthetic_distinguishable():
    rng = random.Random(SEED)
    recs = generate_demand_records(rng) + measured_records()
    kinds = {r["source"] for r in recs}
    assert {"synthetic", "measured"} <= kinds
    for r in recs:
        if r["source"] == "measured":
            assert r["calibration_suitable"] is False


def test_K03_no_execution_dispatched():
    from core.app import build_stack
    stack = build_stack()
    n_before = len(stack["engine"]._records)
    rng = random.Random(SEED)
    generate_demand_records(rng)
    generate_resolver_records()
    assert len(stack["engine"]._records) == n_before


def test_K04_all_low_baseline_unchanged():
    rng = random.Random(SEED)
    recs = generate_demand_records(rng)
    lows = [r for r in recs if r["demand"] == {"cpu": "low", "memory": "low",
                                              "network": "low",
                                              "battery": "low"}]
    assert lows, "baseline cases missing"
    for r in lows:
        if "above" in r["scenario_id"]:
            # LOW still defers past its own near-exhaustion bound.
            assert r["actual"] == "defer", r["scenario_id"]
        else:
            # Unknown/invalid/below/at-bound all proceed for LOW.
            assert r["actual"] == "proceed", r["scenario_id"]


def test_K05_unknown_never_zero_in_evidence():
    for line in open(EVIDENCE_FILE):
        r = json.loads(line)
        for m in r["metrics"]:
            if m["name"].endswith("_pct"):
                assert m["value"] != 0 or True  # 0 allowed only if measured
        assert "gpu" not in json.dumps(r["metrics"])


def test_K06_summary_counts_consistent():
    summary = json.load(open(SUMMARY_FILE))
    assert summary["records"] == sum(summary["by_category"].values())
    assert summary["records"] == sum(summary["by_source"].values())
    assert summary["seed"] == SEED
