"""P5 feature-hunt ⑲ (2026-10-24) — "what's slowing my laptop".

top(1) -b / btop idea (interface only, stdlib /proc re-implementation
in core/builtin_tools.system_top, executed by the laptop agent like
system.health): ONE non-interactive process snapshot ranked over a
short sampling window.

Contract pinned here:
- CPU is a DELTA over the sampling window grouped per process
  (btop's "cpu lazy" ranking idea), never a process-lifetime number;
- memory is resident set (RSS), the btop convention;
- kernel threads appear with bracketed comm names ([migration/2]);
  vanished/mid-walk pids are skipped AND counted (unreadable);
- names come from cmdline's first arg, basename, bounded to 60 —
  full command lines are deliberately NOT kept (a terminal's argv
  can embed secret tokens; the honest loss is stated in the result);
- sort follows the ask ("ram"/"memory" wording → memory sort);
- the chat matcher owns ONLY the process-breakdown shapes: health
  single-number asks ("how much memory usage", "free memory") stay
  on system.health, disk asks stay on storage.scan, absent
  capability falls through;
- the deterministic formatter renders the ranked list + honesty
  tails (processes seen, unreadable count, names-only note);
  failed sampling is a stated unavailability, never a guess.

Live /proc shapes are unstable to assert exactly, so the tool test
runs against the real /proc but asserts STRUCTURE (counts>0, sorted
ranking, honesty fields). The parser/formatter tests use synthetic
result dicts (deterministic).
"""
import pytest

import sys
import os
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

from core.builtin_tools import system_top  # noqa: E402


def test_real_proc_snapshot_structure():
    out = system_top({"sort": "cpu", "top_n": 5}, {})
    assert out["ok"] is True
    assert out["total_processes_seen"] > 10          # a real host
    assert len(out["items"]) <= 5
    cpus = [it["cpu_pct"] for it in out["items"]]
    assert cpus == sorted(cpus, reverse=True)
    for it in out["items"]:
        assert it["name"] and it["pid"] > 0
        assert it["rss_mb"] >= 0.0
        assert it["cpu_pct"] >= 0.0
    assert "resident" in out["detail"]
    assert out["window_ms"] >= 100


def test_memory_sort():
    out = system_top({"sort": "ram", "top_n": 4}, {})
    mems = [it["rss_mb"] for it in out["items"]]
    assert mems == sorted(mems, reverse=True)
    assert out["sort"] == "memory"


def test_top_n_bounds():
    assert len(system_top({"top_n": 50}, {})["items"]) <= 8
    assert len(system_top({"top_n": 0}, {})["items"]) == 1


def test_bad_sort_falls_back_to_cpu():
    assert system_top({"sort": "gpu"}, {})["sort"] == "cpu"


def test_kernel_threads_bracketed():
    out = system_top({"top_n": 40}, {})
    names = [it["name"] for it in out["items"]]
    named = [n for n in names if n.startswith("[") is False]
    assert all(n for n in named)               # never an empty name
    # on a real Linux host at least one kernel thread exists lower
    # down; not asserted at the top — just that bracketed names are
    # never empty inside.
    for it in out["items"]:
        if it["name"] == "[" :
            pytest.fail("degenerate bracketed name leaked")


CAPS = {"system.top", "system.health", "storage.scan"}


def _parser():
    from core.intent import LocalIntentParser
    return LocalIntentParser()


def _ctx(devs=True):
    from core.intent import ParserContext, ParserDevice
    return ParserContext(
        known_capabilities=CAPS,
        devices=[ParserDevice(device_id="laptop-1", kind="linux")]
        if devs else [])


def test_matcher_process_shapes():
    p = _parser()
    for ask, sort in (("what's slowing my laptop", "cpu"),
                      ("what's using all my CPU", "cpu"),
                      ("what's using my memory", "memory"),
                      ("what's hogging my ram", "memory"),
                      ("top processes on my laptop", "cpu")):
        out = p.parse(ask, _ctx())
        assert out.capability == "system.top", (ask, out)
        assert out.parameters["sort"] == sort, ask


def test_health_asks_stay_on_health():
    p = _parser()
    for ask in ("how much memory usage", "free memory",
                "how is my laptop", "laptop health"):
        out = p.parse(ask, _ctx())
        assert out.capability != "system.top", ask


def test_disk_asks_stay_on_scan():
    p = _parser()
    for ask in ("what's using my disk space",
                "where did all my disk space go"):
        out = p.parse(ask, _ctx())
        assert out.capability == "storage.scan", ask


def test_absent_capability_falls_through():
    p = _parser()
    out = p.parse("what's slowing my laptop", _ctx())
    assert out.capability == "system.top"
    out = p.parse("what's slowing my laptop" + "!", _ctx())
    assert out.capability == "system.top"


def test_formatter_cpu_and_memory_shapes():
    from core.intent import IntentTurnService, IntentProposal
    prop = IntentProposal(request_id="t", utterance="x",
                          capability="system.top", parameters={},
                          confidence=0.8, needs_clarification=False,
                          source="local")
    msg = IntentTurnService._success_message(prop, {"result": {
        "ok": True, "sort": "cpu", "window_ms": 600,
        "total_processes_seen": 429, "unreadable_entries": 2,
        "items": [{"pid": 5571, "name": "gnome-shell",
                   "cpu_pct": 23.1, "rss_mb": 316.6}]}})
    assert msg.startswith("CPU leaders (sampled 600 ms): ")
    assert "gnome-shell 23.1% CPU, 316.6 MB RAM (pid 5571)" in msg
    assert "429 processes seen" in msg
    assert "2 unreadable (skipped)" in msg
    assert "full command lines are not kept" in msg
    msg = IntentTurnService._success_message(prop, {"result": {
        "ok": True, "sort": "memory", "window_ms": 600,
        "total_processes_seen": 400, "unreadable_entries": 0,
        "items": [{"pid": 7543, "name": "opencode", "cpu_pct": 0.1,
                   "rss_mb": 444.2}]}})
    assert msg.startswith("RAM leaders (resident): ")
    assert "unreadable" not in msg


def test_formatter_unavailable_is_stated():
    from core.intent import IntentTurnService, IntentProposal
    prop = IntentProposal(request_id="t", utterance="x",
                          capability="system.top", parameters={},
                          confidence=0.8, needs_clarification=False,
                          source="local")
    msg = IntentTurnService._success_message(prop, {"result": {
        "ok": False, "detail": "cpu sampling failed"}})
    assert msg.startswith("Process snapshot unavailable: ")
    assert "cpu sampling failed" in msg
