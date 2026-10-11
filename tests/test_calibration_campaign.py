"""Calibration campaign validation (Task 8): v2 dataset integrity.

Validates docs/research/evidence/calibration_v2.jsonl + summary:
schema, provenance, no-dup IDs, timestamp spacing, bounds, secrets,
category counts, v1 byte-stability, determinism rules. No workloads here;
collection lives in scripts/calibration_campaign.py (+ Android reads).
"""
import json
import os
import re

import pytest

EVIDIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "docs", "research", "evidence")
V2 = os.path.join(EVIDIR, "calibration_v2.jsonl")
V2SUM = os.path.join(EVIDIR, "calibration_v2.summary.json")
V1 = os.path.join(EVIDIR, "calibration_v1.jsonl")
_SECRET_RE = re.compile(
    r"device_key|pairing_code|api[_-]?key|secret|token|bearer|/home/|"
    r"sk-[a-z]|xox|AKIA[0-9A-Z]{16}|BEGIN .*PRIVATE KEY",
    re.IGNORECASE)
_ID_RE = re.compile(r"^[a-z][a-z0-9_.\-]{1,63}$")
_STATUSES = {"proceed", "defer", "reroute", "resolved", "requires_approval",
             "unavailable", "offline", "denied", "not_executed",
             "no_device", "no_capability", "unauthorized", "governor_blocked",
             "policy_blocked", "succeeded", "failed"}


def load_v2():
    rows = []
    with open(V2) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def test_M01_v2_exists_bounded():
    assert os.path.exists(V2), "campaign dataset missing"
    size = os.path.getsize(V2)
    assert 0 < size <= 256 * 1024, size
    rows = load_v2()
    assert len(rows) <= 500 and len(rows) > 0


def test_M02_schema_and_required_fields():
    for r in load_v2():
        assert r["schema"] == "zara.evidence/v1"
        assert _ID_RE.match(r["scenario_id"]), r["scenario_id"]
        assert r["source"] in ("measured", "synthetic", "replayed")
        assert r["category"] in ("demand-threshold", "telemetry-plumbing",
                                 "resolver", "governor-cost", "execution")
        assert r["expected"] in _STATUSES and r["actual"] in _STATUSES
        assert isinstance(r["observed_at"], str) and len(r["observed_at"]) > 0
        assert isinstance(r.get("notes", ""), str)
        assert len(json.dumps(r, sort_keys=True)) <= 8192


def test_M03_no_duplicate_ids():
    ids = [r["scenario_id"] for r in load_v2()]
    assert len(ids) == len(set(ids))


def test_M04_measured_have_real_timestamps_spaced():
    from datetime import datetime
    seen = {}
    for r in load_v2():
        if r["source"] != "measured":
            continue
        ts = datetime.fromisoformat(r["observed_at"])
        assert ts.tzinfo is not None, r["scenario_id"]
        for m in r.get("metrics", []):
            mt = datetime.fromisoformat(m["at"])
            assert mt.tzinfo is not None
        key = (r.get("device_class"), r.get("workload", {}).get("class"))
        if key in seen:
            # Distinct windows: no two measured records share a timestamp.
            assert r["observed_at"] != seen[key], r["scenario_id"]
        seen[key] = r["observed_at"]


def test_M05_metric_bounds():
    for r in load_v2():
        for m in r.get("metrics", []):
            v = m.get("value")
            if v is None:
                continue
            # Enum-unit metrics (thermal_state) carry strings by design;
            # numeric bounds apply only to numeric units. Unknown is None,
            # never zero (M10); strings are never range-checked as numbers.
            if m.get("unit") == "enum":
                assert isinstance(v, str) and v, (r["scenario_id"], m)
                continue
            assert isinstance(v, (int, float)) and not isinstance(v, bool)
            assert v == v and abs(v) != float("inf"), (r["scenario_id"], m)
            if m["name"].endswith("_pct"):
                assert 0.0 <= v <= 100.0, (r["scenario_id"], m)
            if m["name"].startswith("storage_"):
                assert 0 <= v <= 10**7, (r["scenario_id"], m)


def test_M06_no_secrets_no_gpu_fabrication():
    for r in load_v2():
        blob = json.dumps(r, sort_keys=True)
        assert not _SECRET_RE.search(blob), r["scenario_id"]
        names = [m["name"] for m in r.get("metrics", [])]
        assert "gpu_pct" not in names, r["scenario_id"]


def test_M07_stratum_counts_reported():
    summary = json.load(open(V2SUM))
    rows = load_v2()
    assert summary["records"] == len(rows)
    assert summary["records"] == sum(summary["by_source"].values())
    assert summary["records"] == sum(summary["by_category"].values())
    by_cell = summary.get("by_cell", {})
    assert isinstance(by_cell, dict) and len(by_cell) > 0
    assert sum(by_cell.values()) == sum(
        1 for r in rows if r["source"] == "measured")


def test_M08_v1_byte_stable():
    import hashlib
    digest = hashlib.sha256(open(V1, "rb").read()).hexdigest()
    # Pinned full SHA-256 of calibration_v1.jsonl. NOTE (Night 6): the
    # previous pin was the file's MD5 (32 hex) compared against a SHA-256
    # (64 hex), which could never pass; the file itself is byte-stable
    # (md5 5498ad82... recorded across five shifts). Intent unchanged:
    # any byte drift fails loudly here.
    assert digest == \
        "33cfe4a0c07cd7310afbcf59aa1626d40ab855191912403ec249e303c8df8db5", \
        "v1 evidence must not change"


def test_M09_synthetic_not_counted_measured():
    rows = load_v2()
    measured = [r for r in rows if r["source"] == "measured"]
    assert len(measured) > 0
    for r in measured:
        assert r["calibration_suitable"] in (True, False)
        # Measured unsuitable records must say why.
        if r["calibration_suitable"] is False:
            assert len(r.get("notes", "")) > 0


def test_M10_unknown_never_zero_in_dataset():
    for r in load_v2():
        for m in r.get("metrics", []):
            # Absent metrics are omitted, never 0-as-unknown: spot-check
            # that no record claims a platform-gap metric with 0.
            if m["name"] == "cpu_pct" and r.get("device_class") == "android":
                assert m["value"] is None or m["value"] > 0
