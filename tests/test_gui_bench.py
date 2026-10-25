"""GUI benchmark harness + batched-read op tests.

All tests here are fixture-driven: no adb, no a11y bus, no desktop
needed. The benchmark script itself reports hardware=real/fixture/pending
honestly on the live desktop; these tests prove the HARNESS logic
(stats math, failure containment, honest absence, batch mechanics).
"""
import subprocess
import sys
import types

import pytest

import scripts.gui_bench as gb


# ------------------------------------------------------------------
# summarize — the stats contract
# ------------------------------------------------------------------

def test_summarize_odd_and_even_counts():
    odd = gb.summarize([30.0, 10.0, 20.0])
    assert odd["n"] == 3 and odd["median_ms"] == 20.0
    assert odd["min_ms"] == 10.0 and odd["max_ms"] == 30.0
    assert odd["p95_ms"] == 30.0          # nearest-rank: ceil(.95*3)=3
    even = gb.summarize([10.0, 20.0, 30.0, 40.0])
    assert even["median_ms"] == 25.0
    assert even["p95_ms"] == 40.0         # rank 4 of 4


def test_summarize_empty_is_honest_zero_like():
    out = gb.summarize([], failures=2, timeouts=1)
    assert out["n"] == 0 and out["failures"] == 2 and out["timeouts"] == 1
    assert out["median_ms"] is None and out["p95_ms"] is None


def test_summarize_carries_extra_and_returns_bad_never_raises():
    out = gb.summarize([1.0, 2.0], extra={"hardware": "real"})
    assert out["hardware"] == "real"


# ------------------------------------------------------------------
# fixture parse probe — real CPU timing, clearly labeled
# ------------------------------------------------------------------

def test_fixture_parse_probe_measures_parser():
    out = gb.probe_fixture_ui_parse(reps=5, nodes=80)
    s = out["stats"]
    assert s["n"] == 5 and s["failures"] == 0
    assert s["hardware"] == "fixture"
    assert "not device latency" in s["reason"]
    assert s["nodes"] == 81  # 80 rows + root frame
    assert s["median_ms"] < 1000  # parser is microseconds-scale


# ------------------------------------------------------------------
# honest absence: adb / atspi / wmctrl missing → pending, not crash
# ------------------------------------------------------------------

def test_atspi_batch_children_must_be_readonly(monkeypatch):
    from device.linux import tools_linux as tl
    spawned = []

    def fake_call(req, timeout=None):
        spawned.append(req)
        # _atspi_call's contract: returns the UNWRAPPED result of the op
        return {"results": [{"ok": True, "result": {}}
                            for _ in req["ops"]]}

    monkeypatch.setattr(tl, "_atspi_call", fake_call)
    # happy path splits per-entry results
    out = tl._atspi_batch([{"op": "node", "bus": ":1", "path": "/x"},
                           {"op": "get_text", "bus": ":1", "path": "/x"}])
    assert len(spawned) == 1 and spawned[0]["op"] == "multi"
    assert out == [{"ok": True, "result": {}}, {"ok": True, "result": {}}]
    spawned.clear()
    with pytest.raises(RuntimeError, match="read-only"):
        tl._atspi_batch([{"op": "do_action", "bus": ":1", "path": "/x"}])
    with pytest.raises(RuntimeError, match="read-only"):
        tl._atspi_batch([{"op": "set_text", "bus": ":1", "path": "/x"}])
    assert spawned == []                # never reached the helper
    with pytest.raises(RuntimeError, match="too many"):
        tl._atspi_batch([{"op": "node"}] * 9)
    with pytest.raises(RuntimeError, match="non-empty"):
        tl._atspi_batch([])


def test_atspi_batch_shape_mismatch_is_honest(monkeypatch):
    from device.linux import tools_linux as tl
    monkeypatch.setattr(tl, "_atspi_call",
                        lambda req, timeout=None:
                        {"ok": True, "result": {"results": []}})
    with pytest.raises(RuntimeError, match="shape mismatch"):
        tl._atspi_batch([{"op": "node", "bus": ":1", "path": "/x"}])


# ------------------------------------------------------------------
# atspi multi probe — batch vs singles mechanics with a faked bridge
# ------------------------------------------------------------------

def test_adb_inventory_pending_when_helper_times_out(monkeypatch):
    """Inventory calls that can never complete (timeout) are recorded as
    honest pending samples, not exceptions."""
    from device import adb_linux

    def dead():
        raise subprocess.TimeoutExpired(cmd="adb", timeout=10.0)

    monkeypatch.setattr(adb_linux, "list_devices", dead)
    out = gb.probe_adb_inventory(reps=2)
    s = out["stats"]
    assert s["hardware"] == "pending" and s["timeouts"] == 2
    assert s["reason"]


def test_probe_atspi_multi_counts_spawns_and_reports_speedup(monkeypatch):
    from device.linux import tools_linux as tl
    spawns = []

    def fake(req, timeout=None):
        spawns.append(req.get("op"))
        if req.get("op") == "tree":
            return {"apps": [{"app": "x", "bus": ":1", "nodes": [
                {"bus": ":1", "path": "/n1", "role": "button",
                 "role_num": 1, "name": "B"}]}],
                "focused": None, "skipped": 0, "truncated": False,
                "apps_seen": 1}
        if req.get("op") == "node":
            return {"ok": True}
        # multi envelope: one per child
        assert len(req["ops"]) == gb.BATCH_READS
        return {"results": [{"ok": True, "result": {"ok": True,
                                                    "role": "button"}}
                            for _ in req["ops"]]}

    monkeypatch.setattr(tl, "_atspi_call", fake)
    out = gb.probe_atspi_multi(reps=2)
    s = out["stats"]
    assert s["n"] == 2 and s["hardware"] == "real"
    assert spawns.count("tree") == 1
    assert spawns.count("node") == 2 * gb.BATCH_READS
    assert spawns.count("multi") == 2
    assert s["singles"]["n"] == 2 * gb.BATCH_READS
    assert "batch" in s and "singles" in s


def test_probe_atspi_multi_honest_when_no_addressable_node(monkeypatch):
    from device.linux import tools_linux as tl
    monkeypatch.setattr(tl, "_atspi_call",
                        lambda req, timeout=None:
                        {"apps": [], "focused": None, "skipped": 0,
                         "truncated": False, "apps_seen": 0})
    out = gb.probe_atspi_multi(reps=1)
    assert out["stats"]["hardware"] == "pending"
    assert "no addressable node" in out["stats"]["reason"]


def test_probe_atspi_multi_reports_failures_as_data(monkeypatch):
    from device.linux import tools_linux as tl
    def fake(req, timeout=None):
        if req.get("op") == "multi":
            raise RuntimeError("a11y backend bad output")
        return {"apps": [{"app": "x", "bus": ":1", "nodes": [
            {"bus": ":1", "path": "/n1", "role": "button",
             "role_num": 1, "name": "B"}]}],
            "focused": None, "skipped": 0, "truncated": False,
            "apps_seen": 1}
    monkeypatch.setattr(tl, "_atspi_call", fake)
    out = gb.probe_atspi_multi(reps=1)
    s = out["stats"]
    assert s["failures"] == 1 and s["hardware"] == "pending"


# ------------------------------------------------------------------
# run_bench containment — a raising probe becomes a pending entry
# ------------------------------------------------------------------

def test_run_bench_contains_raising_probes(monkeypatch):
    monkeypatch.setitem(gb.PROBES, "adb_inventory", lambda reps: 1 / 0)
    report = gb.run_bench(reps=1, only=["adb_inventory"])
    entry = [r for r in report["results"] if r["name"] == "adb_inventory"][0]
    assert entry["stats"]["hardware"] == "pending"
    assert "division" in entry["stats"]["reason"]


def test_run_bench_includes_paired_phone_honesty_entry():
    report = gb.run_bench(reps=1, only=[])
    names = [r["name"] for r in report["results"]]
    assert "android_a11y_paired_snapshot" in names
    pending = report["results"][-1]["stats"]
    assert pending["hardware"] == "pending"


# ------------------------------------------------------------------
# the probe's multi op itself (dbus stubbed — no a11y bus needed)
# ------------------------------------------------------------------

def _import_probe_with_dbus_stub():
    already = "dbus" in sys.modules
    if not already:
        stub = types.ModuleType("dbus")
        sys.modules["dbus"] = stub
    import importlib
    import device.linux.atspi_probe as ap
    importlib.reload(ap)
    return ap


def test_op_multi_read_only_gating_and_caps():
    ap = _import_probe_with_dbus_stub()
    calls = []
    ap.READ_DISPATCH = {
        "node": lambda bus, req: calls.append(req) or {"role": "button"},
        "tree": lambda bus, req: calls.append(req) or {"apps": []},
    }
    bus = object()
    out = ap.op_multi(bus, {"ops": [
        {"op": "node", "bus": ":1", "path": "/a"},
        {"op": "do_action", "bus": ":1", "path": "/a", "action": "click"},
        "not-a-dict",
        {"op": "tree", "app_filter": "", "max_nodes": 5},
    ]})
    results = out["results"]
    assert results[0] == {"ok": True, "result": {"role": "button"}}
    assert results[1] == {"ok": False,
                          "error": "unsupported child op (refused)"}
    assert results[2] == {"ok": False,
                          "error": "unsupported child op (refused)"}
    assert results[3] == {"ok": True, "result": {"apps": []}}
    # only the read-only children reached the bus
    assert [r["op"] for r in calls] == ["node", "tree"]


def test_op_multi_child_handler_failure_is_per_entry():
    ap = _import_probe_with_dbus_stub()

    def boom(bus, req):
        raise RuntimeError("node unreadable")

    ap.READ_DISPATCH = {"node": boom}
    out = ap.op_multi(object(), {"ops": [{"op": "node"}]})
    assert out["results"][0]["ok"] is False
    assert "node unreadable" in out["results"][0]["error"]


def test_op_multi_cap_and_shape():
    ap = _import_probe_with_dbus_stub()
    out = ap.op_multi(object(), {"ops": [{"op": "node"}] * 9})
    assert "too many ops" in out["error"]
    out2 = ap.op_multi(object(), {"ops": []})
    assert "non-empty" in out2["error"]
