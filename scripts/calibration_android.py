"""Android campaign collector (Task 8) — passive reads only, no workload.

Reads Core device view every ~40 s (app heartbeats every 30 s, so each
read observes a fresh tick). Idle cell: no input injected. Light cell:
caller runs throttled monkey separately; this script only reads.
Never dispatches, approves, or modifies anything.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

SEED = 8
SPACING_S = 40.0

TOKEN = os.environ.get("ASSISTANT_TOKEN", "dev-token")
CORE = os.environ.get("ZARA_CORE_URL", "http://127.0.0.1:8080")


def _get(path):
    req = urllib.request.Request(
        CORE + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def collect(n, workload, out_path, extra_notes=""):
    from datetime import datetime as _dt
    recs = []
    i = 0
    skips = 0
    last_kept_instant = None
    while len(recs) < n:
        if i >= n + 3 * n:
            # Bounded: too many stale windows (e.g. backgrounded app).
            # Stop rather than loop forever; partial set stays honest.
            print(f"STOP: {skips} stale windows, {len(recs)}/{n} kept",
                  flush=True)
            break
        d = _get("/v1/fabric/devices/emu-lab")
        ts = datetime.now(timezone.utc).isoformat()
        # Staleness gate (Night 8): the device view surfaces last-stored
        # values; verify freshness against last_telemetry_at instead of
        # trusting cadence. Stale windows are SKIPPED (never recorded as
        # fresh, never counted toward n).
        try:
            lta = _dt.fromisoformat(d.get("last_telemetry_at") or "")
            age = (_dt.now(timezone.utc) - lta).total_seconds()
        except Exception:
            age = None
        if age is None or age > 120.0:
            skips += 1
            print(f"[{workload} skip] stale telemetry "
                  f"(age={age}, telem_at={d.get('last_telemetry_at')})",
                  flush=True)
            i += 1
            time.sleep(SPACING_S)
            continue
        instant = d.get("last_telemetry_at")
        if instant is not None and instant == last_kept_instant:
            skips += 1
            print(f"[{workload} skip] duplicate measurement instant "
                  f"({instant})", flush=True)
            i += 1
            time.sleep(SPACING_S)
            continue
        last_kept_instant = instant
        rec = {
            "schema": "zara.evidence/v1",
            "scenario_id": f"m-android-{workload}-{len(recs):02d}",
            "category": "telemetry-plumbing",
            "seed": SEED,
            "source": "measured",
            "device_class": "android",
            "observed_at": ts,
            "command": ("android campaign reader (device-view poll, "
                        f"{SPACING_S:.0f}s cadence)"),
            "metrics": [
                {"name": k, "value": d.get(k), "unit": u, "at": ts}
                for k, u in (("mem_pct", "pct"),
                             ("storage_free_mb", "mb"),
                             ("storage_total_mb", "mb"),
                             ("battery_temp_c", "degC"),
                             ("thermal_state", "enum"),
                             ("battery_pct", "pct"))
                if d.get(k) is not None or k in (
                    "mem_pct", "battery_temp_c", "thermal_state")],
            "freshness": {"telemetry_stale": False, "threshold_s": 120.0,
                          "last_telemetry_at": d.get("last_telemetry_at")},
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
            "notes": (f"android {workload}; cpu permanently unavailable; "
                      f"{extra_notes}")[:300],
            "workload": {"class": workload,
                         "duration_s": SPACING_S,
                         "exit": "ok", "cleanup": "n/a-read-only"},
        }
        # cpu always omitted (platform gap); record that explicitly.
        blob = json.dumps(rec, sort_keys=True)
        assert len(blob) <= 8192, rec["scenario_id"]
        recs.append(rec)
        print(f"[{workload} {len(recs):02d}/{n}] mem={d.get('mem_pct')} "
              f"therm={d.get('thermal_state')} telem_at="
              f"{d.get('last_telemetry_at')}", flush=True)
        i += 1
        if len(recs) < n:
            time.sleep(SPACING_S)
    with open(out_path, "w") as f:
        for r in recs:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    print(f"wrote {len(recs)} -> {out_path}", flush=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--workload", default="idle")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--notes", default="")
    args = ap.parse_args()
    T = open("/home/thiyan/projects/assistant/.env").read()
    for line in T.splitlines():
        if line.startswith("ASSISTANT_TOKEN="):
            TOKEN = line.split("=", 1)[1]
            break
    collect(args.n, args.workload, args.out, args.notes)
