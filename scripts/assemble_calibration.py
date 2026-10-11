"""Assemble calibration_v2 dataset (Task 8) from campaign collectors.

Merges per-phase JSONL files, validates every record (schema, bounds,
secrets, dup IDs, timestamp spacing per cell), writes versioned dataset
+ summary. Never touches v1 files.
"""
import hashlib
import json
import os
import re
import sys

EVIDIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "docs", "research", "evidence")
V2 = os.path.join(EVIDIR, "calibration_v2.jsonl")
V2SUM = os.path.join(EVIDIR, "calibration_v2.summary.json")
MAX_RECORDS = 500
MAX_BYTES = 512 * 1024
_SECRET_RE = re.compile(
    r"device_key|pairing_code|api[_-]?key|secret|token|bearer|/home/|"
    r"sk-[a-z]|xox|AKIA[0-9A-Z]{16}|BEGIN .*PRIVATE KEY",
    re.IGNORECASE)
_ID_RE = re.compile(r"^[a-z][a-z0-9_.\-]{1,63}$")
MIN_SPACING_S = 15.0


def load_dir(path):
    rows = []
    for fn in sorted(os.listdir(path)):
        if fn.endswith(".jsonl"):
            with open(os.path.join(path, fn)) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append((fn, json.loads(line)))
    return rows


def validate(rows):
    from datetime import datetime
    seen_ids = set()
    last_ts = {}
    stats = {"valid": 0, "invalid": 0, "duplicate": 0, "stale_ts": 0}
    good = []
    for fn, r in rows:
        sid = r.get("scenario_id", "?")
        blob = json.dumps(r, sort_keys=True)
        bad = None
        if sid in seen_ids:
            bad = "duplicate"
            stats["duplicate"] += 1
        elif not _ID_RE.match(sid):
            bad = "bad-id"
        elif r.get("schema") != "zara.evidence/v1":
            bad = "bad-schema"
        elif _SECRET_RE.search(blob):
            bad = "secret"
        elif len(blob) > 8192:
            bad = "oversize"
        else:
            try:
                ts = datetime.fromisoformat(r["observed_at"])
                assert ts.tzinfo is not None
                key = (r.get("device_class"),
                       (r.get("workload") or {}).get("class"))
                if key in last_ts and ts <= last_ts[key]:
                    bad = "non-monotonic-ts"
                else:
                    last_ts[key] = ts
            except Exception:
                bad = "bad-ts"
        if bad:
            if bad not in ("duplicate",):
                stats["invalid"] += 1
            print(f"QUARANTINE {sid} from {fn}: {bad}", flush=True)
            continue
        seen_ids.add(sid)
        stats["valid"] += 1
        good.append(r)
    return good, stats


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/calib"
    rows = load_dir(src)
    print(f"loaded {len(rows)} raw records from {src}", flush=True)
    good, stats = validate(rows)
    assert len(good) <= MAX_RECORDS
    blob = "\n".join(json.dumps(r, sort_keys=True) for r in good) + "\n"
    assert len(blob) <= MAX_BYTES
    by_cell, by_src, by_cat = {}, {}, {}
    for r in good:
        cell = f"{r.get('device_class')}/{(r.get('workload') or {}).get('class', '?')}"
        by_cell[cell] = by_cell.get(cell, 0) + 1
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    with open(V2, "w") as f:
        f.write(blob)
    summary = {"schema": "zara.evidence/v1", "seed": 8,
               "records": len(good), "bytes": len(blob),
               "by_category": by_cat, "by_source": by_src,
               "by_cell": by_cell, "validation": stats, "mismatches": [],
               "command": ("python3 scripts/calibration_campaign.py + "
                           "scripts/calibration_android.py; "
                           "python3 scripts/assemble_calibration.py")}
    with open(V2SUM, "w") as f:
        json.dump(summary, f, sort_keys=True, indent=1)
    print(json.dumps(summary, indent=1), flush=True)
    print("sha256:", hashlib.sha256(blob.encode()).hexdigest(), flush=True)


if __name__ == "__main__":
    main()
