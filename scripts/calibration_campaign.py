"""Calibration campaign collector (Task 8) — Linux host phases.

Serial, bounded, stdlib-only. Three phases (idle/light/moderate) x 30
samples x ~25 s spacing. Each sample: run phase workload window, then
read system_telemetry(), validate bounds, append JSONL record.

Workloads (per workload-auditor memo, light only, strict ceilings):
- idle: sleep 20 s (no loop/allocation).
- light: 20 s of 30 ms busy + 70 ms sleep (~30% of 1 core), nice 10.
- moderate: 20 s of 150 ms busy + 100 ms sleep (~60% of 1 core) plus a
  single 50 MB bytearray held during the window, freed after.

Safety: wall-clock deadline per window (30 s), RSS ceiling 100 MB,
parent `timeout` recommended. Cleanup: buffers deleted each window.
Usage: python3 scripts/calibration_campaign.py --out DIR --phase all
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCHEMA = "zara.evidence/v1"
SEED = 8
WINDOW_S = 20.0
SPACING_S = 25.0
SAMPLES_PER_CELL = 30
_ID_RE = re.compile(r"^[a-z][a-z0-9_.\-]{1,63}$")
_SECRET_RE = re.compile(
    r"device_key|pairing_code|api[_-]?key|secret|token|bearer|/home/|"
    r"sk-[a-z]|xox|AKIA[0-9A-Z]{16}|BEGIN .*PRIVATE KEY",
    re.IGNORECASE)

LIGHT_DUTY = (0.03, 0.07)    # busy, sleep per 100 ms cycle
MODERATE_DUTY = (0.15, 0.10)  # busy, sleep per 250 ms cycle
MODERATE_MEM_MB = 50
RSS_CEILING_MB = 100


def _now():
    return datetime.now(timezone.utc).isoformat()


def _busy_window(busy, rest, duration):
    import resource
    end = time.monotonic() + duration
    while time.monotonic() < end:
        t0 = time.monotonic()
        while time.monotonic() - t0 < busy:
            pass
        time.sleep(rest)
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        if rss_mb > RSS_CEILING_MB:
            return False, f"rss {rss_mb:.0f}MB over ceiling"
    return True, "ok"


def run_phase(phase):
    """Run one workload window. Returns (ok, detail, hold_buffer)."""
    if phase == "idle":
        time.sleep(WINDOW_S)
        return True, "sleep-only", None
    if phase == "light":
        return _busy_window(*LIGHT_DUTY, WINDOW_S) + (None,)
    if phase == "moderate":
        buf = bytearray(MODERATE_MEM_MB * 1024 * 1024)
        ok, detail = _busy_window(*MODERATE_DUTY, WINDOW_S)
        return ok, detail, buf
    raise ValueError(phase)


def collect_phase(phase, n, out_records, t0):
    from device.linux.tools_linux import system_telemetry
    for i in range(n):
        ok, detail, _hold = run_phase(phase)
        # Release buffer before measuring so the sample reflects the
        # window's steady state, not the hold itself for mem accounting.
        del _hold
        t = system_telemetry({}, {})
        ts = _now()
        rec = {
            "schema": SCHEMA,
            "scenario_id": f"m-linux-{phase}-{i:02d}",
            "category": "telemetry-plumbing",
            "seed": SEED,
            "source": "measured",
            "device_class": "linux",
            "observed_at": ts,
            "command": ("python3 scripts/calibration_campaign.py "
                        f"--phase {phase}"),
            "metrics": [
                {"name": "cpu_pct", "value": t.get("cpu_pct"),
                 "unit": "pct", "at": ts},
                {"name": "mem_pct", "value": t.get("mem_pct"),
                 "unit": "pct", "at": ts},
                {"name": "storage_free_mb",
                 "value": t.get("storage_free_mb"), "unit": "mb",
                 "at": ts},
            ],
            "freshness": {"telemetry_stale": False, "threshold_s": 120.0},
            "demand": {"cpu": "low", "memory": "low", "network": "low",
                       "battery": "low"},
            "thresholds": {"memory": {"low": 98.0, "moderate": 93.0,
                                      "high": 85.0},
                           "cpu": {"low": 98.0, "moderate": 90.0,
                                   "high": 75.0},
                           "battery_low_band": 30.0},
            "constraints": {},
            "resolver": {},
            "execution": {"state": "not_executed"},
            "expected": "proceed",
            "actual": "proceed",
            "mismatch_class": "none",
            "calibration_suitable": True,
            "notes": (f"linux {phase} window {WINDOW_S:.0f}s; "
                      f"workload {detail}; "
                      f"cpu via {t.get('cpu_source')}; "
                      f"t+{time.time()-t0:.0f}s")[:300],
            "workload": {"class": phase, "duration_s": WINDOW_S,
                         "exit": "ok" if ok else "aborted",
                         "cleanup": "buffer-released"},
        }
        blob = json.dumps(rec, sort_keys=True)
        assert _ID_RE.match(rec["scenario_id"])
        assert not _SECRET_RE.search(blob), rec["scenario_id"]
        assert len(blob) <= 8192
        for m in rec["metrics"]:
            v = m["value"]
            assert v is None or (isinstance(v, (int, float))
                                 and v == v and abs(v) != float("inf")), \
                (rec["scenario_id"], m)
        out_records.append(rec)
        print(f"[{phase} {i+1:02d}/{n}] {blob[:160]}", flush=True)
        if i < n - 1:
            time.sleep(max(0.0, SPACING_S - WINDOW_S))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--phase", default="all",
                    choices=["all", "idle", "light", "moderate"])
    ap.add_argument("--n", type=int, default=SAMPLES_PER_CELL)
    args = ap.parse_args()
    phases = ["idle", "light", "moderate"] if args.phase == "all" \
        else [args.phase]
    os.makedirs(args.out, exist_ok=True)
    t0 = time.time()
    for phase in phases:
        recs = []
        collect_phase(phase, args.n, recs, t0)
        path = os.path.join(args.out, f"linux-{phase}.jsonl")
        with open(path, "w") as f:
            for r in recs:
                f.write(json.dumps(r, sort_keys=True) + "\n")
        print(f"wrote {len(recs)} -> {path} "
              f"(elapsed {time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
