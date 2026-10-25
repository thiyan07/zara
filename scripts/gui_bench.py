#!/usr/bin/env python
"""GUI benchmark + capability report for Zara's automation paths.

Establishes REAL numbers for the surfaces Zara already owns, and states
honestly what is pending hardware. No privileged operations, no packages:
everything here is bounded reads + fixture parsing.

Probes (each reports hardware=real|fixture|pending honestly):

  adb_inventory     adb devices -l through Zara's bounded adapter
                    (real adb binary; a device is not required)
  android_a11y      paired-app accessibility snapshot — PENDING without a
                    paired phone; the contract is fixture-verified in
                    tests (structural-id + STALE_SNAPSHOT refusal)
  adb_dump          uiautomator dump (compressed-first) — real only with
                    an attached device; otherwise reported pending
  fixture_ui_parse  CPU cost of parsing a 400-node UI XML through
                    device.adb_linux.ui_nodes (clearly labeled fixture)
  atspi_tree        full bounded AT-SPI tree walk on this desktop (real,
                    read-only)
  atspi_node        single-node re-read — the freshness re-check latency
                    paid before every desktop action (real)
  x11_window_list   wmctrl -l (works under Xwayland; real evidence for
                    the X11-only claim under Wayland)
  x11_window_active xdotool getactivewindow (records whether Xwayland
                    serves the active window)
  injector          ydotool/wtype presence + /dev/uinput permissions
                    (read-only stat — nothing is installed or enabled)

Usage:
    python3 scripts/gui_bench.py                # human report
    python3 scripts/gui_bench.py --json out.json
Exit code is always 0: a benchmark failure is DATA, not an exception.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

P95_TOL_MS = 0            # percentile interpolation off by default
_RUN_CAP = 20             # hard sample cap per probe (bounded benches)
BATCH_READS = 8           # multi-op benchmark: reads per batch
_TimeoutError = subprocess.TimeoutExpired


def summarize(latencies_ms: list, failures: int = 0, timeouts: int = 0,
              extra: dict = None) -> dict:
    """Pure stats block: sample count, median, p95, extremes, failure
    counts. Empty/honest for zero samples — never fabricated."""
    lat = sorted(float(v) for v in latencies_ms)
    out = {"n": len(lat), "failures": int(failures), "timeouts":
           int(timeouts), "median_ms": None, "p95_ms": None,
           "min_ms": None, "max_ms": None}
    if lat:
        # nearest-rank p95 (interpolation off) — reproducible
        out["median_ms"] = round(statistics.median(lat), 2)
        rank = max(1, int(round(0.95 * len(lat))))
        out["p95_ms"] = round(lat[min(rank - 1, len(lat) - 1)], 2)
        out["min_ms"] = round(lat[0], 2)
        out["max_ms"] = round(lat[-1], 2)
    if extra:
        out.update(extra)
    return out


def _timed(fn, *a, **k):
    """One timed call: returns (elapsed_ms, ok, error, was_timeout)."""
    t0 = time.monotonic()
    try:
        fn(*a, **k)
        return (time.monotonic() - t0) * 1000.0, True, None, False
    except _TimeoutError:
        return (time.monotonic() - t0) * 1000.0, False, "timeout", True
    except Exception as e:  # noqa: BLE001 — a failure is data here
        return (time.monotonic() - t0) * 1000.0, False, str(e)[:200], False


def _reps(n, cap=_RUN_CAP):
    return max(1, min(int(n), cap))


# ------------------------------------------------------------------
# capability report
# ------------------------------------------------------------------

def capabilities() -> dict:
    """Session type + which backends exist NOW (read-only). This is the
    routing evidence: what may act where, and what is honestly missing."""
    xdg = os.environ.get("XDG_SESSION_TYPE", "").lower()
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    x11 = bool(os.environ.get("DISPLAY"))
    session = ("wayland" if wayland else
               "x11" if x11 else xdg or "headless")
    wmctrl, xdotool = shutil.which("wmctrl"), shutil.which("xdotool")
    ydotool, wtype = shutil.which("ydotool"), shutil.which("wtype")
    uinput = Path("/dev/uinput")
    uinput_info = None
    try:
        st = os.stat(uinput)
        uinput_info = {"mode": oct(st.st_mode & 0o777),
                       "owner_uid": st.st_uid}
    except OSError:
        pass
    adb = shutil.which("adb") or str(
        _REPO.parent / "Android" / "Sdk" / "platform-tools" / "adb") \
        if Path("/home/thiyan/Android/Sdk/platform-tools/adb").exists() \
        else shutil.which("adb")
    return {
        "session": session,
        "xserver_display": bool(x11),
        "x11_tools": {"wmctrl": bool(wmctrl), "xdotool": bool(xdotool)},
        "wayland_injector": {"ydotool": bool(ydotool),
                             "wtype": bool(wtype),
                             "uinput": uinput_info,
                             "usable": bool(ydotool or wtype)},
        "adb": {"present": bool(adb), "path": adb},
    }


# ------------------------------------------------------------------
# probes — real hardware/desktop paths
# ------------------------------------------------------------------

def probe_adb_inventory(reps: int) -> dict:
    from device import adb_linux
    lat, fails, timeouts, detail = [], 0, 0, None
    for _ in range(_reps(reps)):
        # one timed call that both measures AND keeps the result
        t0 = time.monotonic()
        try:
            r = adb_linux.list_devices()
            lat.append((time.monotonic() - t0) * 1000.0)
            detail = {"devices": r.get("devices", []),
                      "device_attached": any(
                          d.get("state") == "device"
                          for d in r.get("devices", []))}
        except _TimeoutError:
            timeouts += 1
        except Exception as e:  # noqa: BLE001
            fails += 1
            detail = {"error": str(e)[:200]}
    stats = summarize(lat, fails, timeouts)
    if stats["n"] == 0:
        stats["hardware"] = "pending"
        stats["reason"] = ((detail or {}).get("error")
                           or "no successful sample (failures/timeouts only)")
    elif detail:
        stats["hardware"] = "real"
        stats["device_attached"] = detail.get("device_attached", False)
    return {"name": "adb_inventory", "stats": stats}


def probe_adb_dump(reps: int) -> dict:
    from device import adb_linux
    inv = adb_linux.list_devices()
    attached = [d for d in inv.get("devices", [])
                if d.get("state") == "device"]
    if not attached:
        return {"name": "adb_dump", "stats": {
            "n": 0, "failures": 0, "timeouts": 0, "median_ms": None,
            "p95_ms": None, "min_ms": None, "max_ms": None,
            "hardware": "pending", "reason":
            "no attached Android device — real dump latency pending"}}
    lat, fails, timeouts, last = [], 0, 0, None
    for _ in range(_reps(reps)):
        ms, ok, err, to = _timed(adb_linux.ui_dump)
        if ok:
            lat.append(ms)
            last = True
        elif to:
            timeouts += 1
        else:
            fails += 1
            last = err
    stats = summarize(lat, fails, timeouts)
    stats["hardware"] = "real" if stats["n"] else "pending"
    if last:
        stats["detail"] = ("ok" if last is True else str(last)[:120])
    return {"name": "adb_dump", "stats": stats}


def probe_fixture_ui_parse(reps: int, nodes: int = 400) -> dict:
    """Fixture CPU benchmark: parse a synthetic 400-node UI XML through
    the production parser. This measures PARSER throughput only —
    explicitly NOT device latency."""
    from device import adb_linux
    rows = []
    for i in range(nodes):
        rows.append(f'<node index="{i}" text="Item {i}" '
                    f'content-desc="row {i}" clickable="true"'
                    f' class="android.widget.TextView"'
                    f' bounds="[0,{i * 40}][1080,{i * 40 + 40}]"/>')
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<hierarchy><node text="" class="android.widget.FrameLayout"'
           ' content-desc="" clickable="false"'
           ' bounds="[0,0][1080,2400]"/>' + "".join(rows) + "</hierarchy>")
    lat, fails, timeouts = [], 0, 0
    n_items = None
    for _ in range(_reps(reps)):
        t0 = time.monotonic()
        try:
            items = adb_linux.ui_nodes(xml)
            n_items = len(items)
            lat.append((time.monotonic() - t0) * 1000.0)
        except Exception:  # noqa: BLE001
            fails += 1
    stats = summarize(lat, fails, timeouts,
                      {"nodes": n_items,
                       "hardware": "fixture",
                       "reason": "parser CPU cost, not device latency"})
    return {"name": "fixture_ui_parse", "stats": stats}


def probe_atspi_tree(reps: int, max_nodes: int = 150) -> dict:
    from device.linux import tools_linux as tl
    lat, fails, timeouts = [], 0, 0
    focused = None
    for _ in range(_reps(reps)):
        ms, ok, err, to = _timed(tl._atspi_call,
                                 {"op": "tree", "app_filter": "",
                                  "max_nodes": max_nodes,
                                  "max_depth": 4,
                                  "include_text": False})
        if ok:
            lat.append(ms)
            focused = None  # content is untrusted; don't persist nodes
        elif to:
            timeouts += 1
        else:
            fails += 1
    stats = summarize(lat, fails, timeouts)
    a11y_ok = stats["n"] > 0
    stats["hardware"] = "real" if a11y_ok else "pending"
    if not a11y_ok:
        stats["reason"] = ("AT-SPI tree walk failed on this desktop — "
                           "a11y bus unreachable or no dbus binding")
    return {"name": "atspi_tree", "stats": stats}


def probe_atspi_node(reps: int) -> dict:
    """Single-node re-read: exactly the freshness re-check
    (`_resolve_node`) pays before EVERY desktop action — so this number
    is the per-action floor of snapshot-bound clicking/typing."""
    from device.linux import tools_linux as tl
    try:
        tree = tl._atspi_call({"op": "tree", "app_filter": "",
                               "max_nodes": 60, "max_depth": 2,
                               "include_text": False})
        first = (tree.get("apps") or [{}])[0].get("nodes") or []
        target = next((n for n in first
                       if n.get("bus") and n.get("path")), None)
        if target is None:
            return {"name": "atspi_node", "stats": {
                "n": 0, "failures": 1, "timeouts": 0,
                "hardware": "pending",
                "reason": "no addressable node to re-read"}}
        req = {"op": "node", "bus": target["bus"],
               "path": target["path"]}
    except Exception as e:  # noqa: BLE001
        return {"name": "atspi_node", "stats": {
            "n": 0, "failures": 1, "timeouts": 0, "hardware": "pending",
            "reason": str(e)[:160]}}
    lat, fails, timeouts = [], 0, 0
    for _ in range(_reps(reps)):
        ms, ok, err, to = _timed(tl._atspi_call, req)
        if ok:
            lat.append(ms)
        elif to:
            timeouts += 1
        else:
            fails += 1
    stats = summarize(lat, fails, timeouts)
    stats["hardware"] = "real" if stats["n"] else "pending"
    return {"name": "atspi_node", "stats": stats}


def _run_probe_real(argv: list, timeout_s: float):
    r = subprocess.run(argv, capture_output=True, text=True,
                       timeout=timeout_s)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "").strip()[:160])
    return r.stdout


def probe_atspi_apps(reps: int) -> dict:
    """Cost of the fast registry app-list read (`op "apps"`): the op
    the Wayland launch-verification path pays per poll. Read-only."""
    from device.linux import tools_linux as tl
    lat, fails, tos = [], 0, 0
    app_names = None
    err = None
    req = {"op": "apps", "max_apps": 40}

    def _once():
        nonlocal app_names
        r = tl._atspi_call(dict(req))
        if isinstance(r, dict) and isinstance(r.get("apps"), list) \
                and app_names is None:
            app_names = [str(n)[:60] for n in r["apps"]]
        return r

    for _ in range(_reps(reps)):
        ms, ok, e, was_to = _timed(_once)
        if ok:
            lat.append(ms)
        elif was_to:
            tos += 1
        else:
            fails += 1
            err = str(e)[:160] if e else err
    stats = summarize(lat, fails, tos)
    if stats["n"] == 0:
        stats["hardware"] = "pending"
        stats["reason"] = (err or "no successful sample (a11y bus "
                                  "unreachable or timeouts only)")
    else:
        stats["hardware"] = "real"
        stats["a11y_apps_seen"] = len(app_names or [])
    return {"name": "atspi_apps", "stats": stats}


def probe_x11_window_list(reps: int, timeout_s: float = 5.0) -> dict:
    if not shutil.which("wmctrl"):
        return {"name": "x11_window_list", "stats": {
            "n": 0, "failures": 1, "timeouts": 0, "hardware": "pending",
            "reason": "wmctrl not installed (honest absence)"}}
    lat, fails, timeouts = [], 0, 0
    windows_preview = None
    for _ in range(_reps(reps)):
        ms, ok, err, to = _timed(_run_probe_real,
                                 ["wmctrl", "-l"], timeout_s)
        if ok:
            lat.append(ms)
        elif to:
            timeouts += 1
        else:
            fails += 1
    stats = summarize(lat, fails, timeouts)
    stats["hardware"] = "real" if stats["n"] else "pending"
    if not stats["n"] and (fails or timeouts):
        stats["reason"] = ("wmctrl present but the call failed — "
                           "typical on Wayland when no X server is "
                           "reachable from this context")
    return {"name": "x11_window_list", "stats": stats}


def probe_x11_window_active(reps: int, timeout_s: float = 5.0) -> dict:
    if not shutil.which("xdotool"):
        return {"name": "x11_window_active", "stats": {
            "n": 0, "failures": 1, "timeouts": 0, "hardware": "pending",
            "reason": "xdotool not installed (honest absence)"}}
    lat, fails, timeouts = [], 0, 0
    for _ in range(_reps(reps)):
        ms, ok, err, to = _timed(_run_probe_real,
                                 ["xdotool", "getactivewindow"],
                                 timeout_s)
        if ok:
            lat.append(ms)
        elif to:
            timeouts += 1
        else:
            fails += 1
    stats = summarize(lat, fails, timeouts)
    stats["hardware"] = "real" if stats["n"] else "pending"
    if not stats["n"]:
        stats["reason"] = ("xdotool present but the call failed — on "
                           "Wayland the focused window belongs to the "
                           "compositor, not X (AT-SPI inspect reports "
                           "it instead)")
    return {"name": "x11_window_active", "stats": stats}


def probe_atspi_multi(reps: int) -> dict:
    """Quantify the cost of ONE spawn per op: 8 × node re-read issued
    as standalone spawns vs the same reads through the single-spawn
    read-batch op (multi). Real desktop reads (read-only)."""
    from device.linux import tools_linux as tl
    lat_singles, lat_batch = [], []
    n_fails = n_timeo = 0
    try:
        tree = tl._atspi_call({"op": "tree", "app_filter": "",
                               "max_nodes": 60, "max_depth": 2,
                               "include_text": False})
        first = (tree.get("apps") or [{}])[0].get("nodes") or []
        target = next((n for n in first
                       if n.get("bus") and n.get("path")), None)
        if target is None:
            return {"name": "atspi_multi", "stats": {
                "n": 0, "failures": 1, "timeouts": 0,
                "hardware": "pending",
                "reason": "no addressable node to re-read"}}
        req_one = {"op": "node", "bus": target["bus"],
                   "path": target["path"]}
    except Exception as e:  # noqa: BLE001
        return {"name": "atspi_multi", "stats": {
            "n": 0, "failures": 1, "timeouts": 0, "hardware": "pending",
            "reason": str(e)[:160]}}
    for _ in range(_reps(reps)):
        for _ in range(8):
            ms1, ok1, _e1, to1 = _timed(tl._atspi_call, dict(req_one))
            if ok1:
                lat_singles.append(ms1)
            elif to1:
                n_timeo += 1
            else:
                n_fails += 1
        batch_reqs = [dict(req_one) for _ in range(8)]
        ms2, ok2, _e2, to2 = _timed(
            tl._atspi_call, {"op": "multi", "ops": batch_reqs})
        if ok2:
            lat_batch.append(ms2)
        elif to2:
            n_timeo += 1
        else:
            n_fails += 1
    s_stats, b_stats = summarize(lat_singles), summarize(lat_batch)
    top = summarize(lat_batch, n_fails, n_timeo)
    top["hardware"] = "real" if top["n"] else "pending"
    top["mode"] = "8-node-reads: 8 spawns vs 1 batch"
    top["singles"] = s_stats
    top["batch"] = b_stats
    if (s_stats["median_ms"] is not None
            and (b_stats["median_ms"] or 0) > 0):
        # per-request efficiency: 8 reads in ~1 spawn vs 8 spawns
        top["batch_speedup_vs_singles"] = round(
            (BATCH_READS * s_stats["median_ms"]) / b_stats["median_ms"], 2)
    return {"name": "atspi_multi", "stats": top}


PROBES = {
    "capabilities": capabilities,
    "adb_inventory": probe_adb_inventory,
    "adb_dump": probe_adb_dump,
    "fixture_ui_parse": lambda reps=50: probe_fixture_ui_parse(reps),
    "atspi_tree": lambda reps=5: probe_atspi_tree(reps),
    "atspi_node": lambda reps=10: probe_atspi_node(reps),
    "atspi_multi": lambda reps=4: probe_atspi_multi(reps),
    "atspi_apps": lambda reps=10: probe_atspi_apps(reps),
    "x11_window_list": lambda reps=5: probe_x11_window_list(reps),
    "x11_window_active": lambda reps=5: probe_x11_window_active(reps),
}


def run_bench(reps: int = 5, only: list = None) -> dict:
    """Run the selected probes; assemble the honest report dict. Always
    succeeds structurally — failures live inside stats."""
    from device import adb_linux  # noqa: F401 — adapter presence check
    names = list(PROBES)
    if only:
        names = [n for n in names if n in only]
    report = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime()),
        "capabilities": capabilities(),
        "results": [],
    }
    for name in names:
        if name == "capabilities":
            continue
        try:
            res = PROBES[name](reps)
        except Exception as e:  # noqa: BLE001 — a bench failure is data
            res = {"name": name, "stats": {
                "n": 0, "failures": 1, "timeouts": 0, "hardware":
                "pending", "reason": str(e)[:200]}}
        report["results"].append(res)
    # phone-paired path is always reported honestly when no device exists
    report["results"].append({
        "name": "android_a11y_paired_snapshot",
        "stats": {"n": 0, "failures": 0, "timeouts": 0,
                  "hardware": "pending",
                  "reason": "paired-device protocol latency requires a "
                            "real phone with the Zara app connected; the "
                            "snapshot/freshness contract is "
                            "fixture-verified in tests"
                            " (expected_snapshot_id + "
                            "STALE_SNAPSHOT refusal)"}})
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--json", default=None)
    ap.add_argument("--only", default=None,
                    help="comma-separated probe names")
    a = ap.parse_args()
    report = run_bench(reps=a.reps, only=(a.only.split(",")
                                          if a.only else None))
    lines = []
    for r in report["results"]:
        s = r["stats"]
        tag = s.get("hardware", "?")
        base = f"{r['name']:<28} {tag:<9} n={s['n']}"
        if s["n"]:
            base += (f" median={s['median_ms']}ms p95={s['p95_ms']}ms "
                     f"min={s['min_ms']} max={s['max_ms']}")
        if s["failures"] or s["timeouts"]:
            base += f" failures={s['failures']} timeouts={s['timeouts']}"
        if isinstance(s.get("reason"), str):
            base += f" | {s['reason'][:130]}"
        base += (f" | devices={len(s.get('devices', []))}"
                 if s.get("devices") is not None else "")
        lines.append(base)
    cap = report["capabilities"]
    lines.insert(0, "session=%s wmctrl=%s xdotool=%s ydotool=%s adb=%s"
                 % (cap["session"], int(cap["x11_tools"]["wmctrl"]),
                    int(cap["x11_tools"]["xdotool"]),
                    int(cap["wayland_injector"]["ydotool"]),
                    int(cap["adb"]["present"])))
    print("\n".join(lines))
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
