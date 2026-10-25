"""P5 feature-hunt ㉓ (2026-10-24) — "what grew on my disk"
(storage.growth).

The growth lens of the storage family: scan now, diff against the
last saved assistant-internal baseline for the same root, then save
this scan as the new baseline.

Contract pinned here:
- growth_diff is a PURE name-keyed delta (grown/shrunk/new/gone);
  equal-byte buckets are UNCHANGED and skipped (the earlier chain
  fell through to "gone" — exact fixtures pin it);
- a diff over a PARTIAL scan is REFUSED (floors delta to noise) —
  the ValueError names the fix (raise the budget);
- no baseline means an honest first run: rows empty +
  first_baseline=True + "ask again later" wording, never a fake
  comparison against nothing;
- the ledger is keyed-replace per (root, day) so at-least-once
  redelivery cannot stack duplicate baselines, and is bounded to
  the newest 8 days per root;
- the persisted payload is AGGREGATE level-1 sizes only (no file
  paths, no content) — assistant machinery, deliberately outside
  the approval-gate (same rule that lets the core's own state
  databases write);
- the device def waits > 60 s (timing invariant) and matches the
  agent def (parity);
- the matcher requires BOTH disk wording and growth wording, so
  the folder rundown and file finder keep their own turns.
"""
import json

import pytest

import sys
import os
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

from core.storage_scan import growth_diff  # noqa: E402


def _mk(seeds):
    return [{"path": n, "bytes": b, "files": 1, "dirs": 0}
            for n, b in seeds]


def test_diff_states_and_skip_unchanged():
    prev = _mk([("a", 100), ("b", 200), ("c", 300), ("d", 50)])
    cur = _mk([("a", 400), ("b", 200), ("c", 100)])
    out = growth_diff({"items": prev, "ts": "t0"},
                      {"items": cur})
    rows = {(r["bucket"], r["state"]): r["delta_bytes"]
            for r in out["rows"]}
    # b unchanged skips; d vanished -> a GONE row, not silence
    assert set(n for n, _ in rows) == {"a", "c", "d"}
    assert rows[("a", "grown")] == 300
    assert rows[("c", "shrunk")] == -200
    assert rows[("d", "gone")] == -50
    out2 = growth_diff(
        {"items": _mk([("x", 5)]), "ts": "t1"},
        {"items": _mk([("y", 9), ("x", 5)])})
    states = {(r["bucket"], r["state"]) for r in out2["rows"]}
    assert states == {("y", "new")}                  # x unchanged skips
    out3 = growth_diff({"items": _mk([("gone", 7)]), "ts": "t2"},
                       {"items": []})
    assert out3["rows"][0]["state"] == "gone"


def test_partial_refusal(tmp_path, monkeypatch):
    from core.storage_scan import storage_growth
    monkeypatch.setenv("ZARA_STORAGE_LEDGER_DB",
                       str(tmp_path / "ledger.db"))
    called = {}

    def fake_scan(root, top_n=10, budget_s=None, max_entries=None):
        called["budget"] = budget_s
        called["max_entries"] = max_entries
        return {"root": root, "items": [{"path": "a", "bytes": 1,
                                         "files": 1, "dirs": 0}],
                "budget": {"budget_s": budget_s, "partial": True,
                           "reason": "time budget"}}
    import core.storage_scan as ss
    monkeypatch.setattr(ss, "scan_dir_usage", fake_scan)
    with pytest.raises(ValueError) as e:
        storage_growth("/home/x", budget_s=3.0)
    assert "FULL scans" in str(e.value)
    assert called["budget"] == 3.0
    assert called["max_entries"] == 2_000_000   # growth-sized cap


def test_first_run_saves_and_second_diffs(tmp_path, monkeypatch):
    import core.storage_scan as ss
    monkeypatch.setenv("ZARA_STORAGE_LEDGER_DB",
                       str(tmp_path / "ledger.db"))
    seeds = {"val": _mk([("a", 100), ("b", 50)])}
    seq = {"n": 0}

    def fake_scan(root, top_n=10, budget_s=None, max_entries=None):
        i = seq["n"]
        seq["n"] += 1
        if i == 0:
            items = seeds["val"]
        elif i == 1:
            items = _mk([("a", 300), ("b", 50), ("c", 10)])
        else:
            items = _mk([("a", 300), ("b", 50), ("c", 10)])
        return {"root": root, "items": items,
                "budget": {"budget_s": budget_s, "partial": False,
                           "reason": ""}}
    monkeypatch.setattr(ss, "scan_dir_usage", fake_scan)
    out1 = ss.storage_growth("/home/x", budget_s=5.0)
    assert out1["first_baseline"] is True and out1["rows"] == []
    out2 = ss.storage_growth("/home/x", budget_s=5.0)
    assert out2["first_baseline"] is False
    states = [(r["bucket"], r["state"], r["delta_bytes"])
              for r in out2["rows"]]
    assert ("a", "grown", 200) in states
    assert ("c", "new", 10) in states
    assert all(b != "gone" or delta != 0 for b, _, delta in states)
    out3 = ss.storage_growth("/home/x", budget_s=5.0)
    assert out3["rows"] == []                        # no change


def test_ledger_redelivery_and_bound(tmp_path):
    from core.storage_scan import StorageLedger
    led = StorageLedger(str(tmp_path / "ledger.db"))
    led.save("/r", "2026-10-24T10:00:00", [{"path": "a", "bytes": 1}])
    led.save("/r", "2026-10-24T18:00:00", [{"path": "a", "bytes": 2}])
    latest = led.latest("/r")
    assert latest["ts"] == "2026-10-24T18:00:00"     # keyed replace
    assert latest["items"][0]["bytes"] == 2
    rows = led._conn.execute(
        "SELECT COUNT(*) FROM storage_baseline WHERE root='/r'"
    ).fetchone()[0]
    assert rows == 1                                 # no redelivery stack
    for i in range(12):
        led.save("/r", f"2026-09-{i + 1:02d}T00:00:00",
                 [{"path": "a", "bytes": i}])
    rows = led._conn.execute(
        "SELECT COUNT(*) FROM storage_baseline WHERE root='/r'"
    ).fetchone()[0]
    assert rows <= 8                                 # bounded by design


CAPS = {"storage.growth", "storage.scan", "storage.find",
        "system.top"}


def _parser():
    from core.intent import LocalIntentParser
    return LocalIntentParser()


def _ctx():
    from core.intent import ParserContext, ParserDevice
    return ParserContext(
        known_capabilities=CAPS,
        devices=[ParserDevice(device_id="laptop-1", kind="linux")])


def test_matcher_and_separation():
    p = _parser()
    for ask in ("what grew on my disk",
                "what changed on my disk since last week",
                "disk growth check"):
        out = p.parse(ask, _ctx())
        assert out.capability == "storage.growth", ask
    assert p.parse("where did my disk go", _ctx()).capability \
        == "storage.scan"
    assert p.parse("which files are over 1 gb", _ctx()).capability \
        == "storage.find"
    # no growth wording -> rundown (its own alias) wins, not growth
    out = p.parse("disk cleanup", _ctx())
    assert out.capability != "storage.growth"


def test_registry_and_parity():
    from core.device_tools import DEVICE_TOOL_DEFS
    from device.linux import tools_linux as LT
    core = next(x for x in DEVICE_TOOL_DEFS if x.name == "storage.growth")
    assert core.timeout_s > 60.0
    assert "storage.read" in core.required_capabilities
    assert core.risk.value == "safe"
    linux = {d.name: d for d, _ in LT.LINUX_TOOLS}["storage.growth"]
    assert set(core.required_capabilities) == \
        set(linux.required_capabilities)
    assert core.input_schema == linux.input_schema
    assert core.risk == linux.risk


def _fmt(result):
    from core.intent import IntentTurnService, IntentProposal
    prop = IntentProposal(request_id="t", utterance="x",
                          capability="storage.growth", parameters={},
                          confidence=0.8, needs_clarification=False,
                          source="local")
    return IntentTurnService._success_message(
        prop, {"result": result, "stage": "succeeded"})


def test_formatter_first_run_and_diff():
    msg = _fmt({"root": "/home/me", "first_baseline": True,
                "current": [{"path": "a", "bytes": 1}],
                "rows": [], "now": "t"})
    assert msg.startswith("No earlier baseline for /home/me")
    assert "ask again later" in msg.lower()
    msg = _fmt({"root": "/home/me", "first_baseline": False,
                "prev_ts": "2026-10-20T09:00:00+05:30",
                "rows": [{"bucket": "downloads", "state": "grown",
                          "delta_bytes": 12_300_000_000},
                         {"bucket": "cache", "state": "shrunk",
                          "delta_bytes": -900_000_000}],
                "now": "t"})
    assert "Disk growth since 2026-10-20T09:00:00+05:30" in msg
    assert "+12.3 GB downloads (grown)" in msg
    assert "-900.0 MB cache (shrunk)" in msg
    assert "non-partial" in msg
    msg = _fmt({"root": "/home/me", "first_baseline": False,
                "prev_ts": "t0", "rows": [], "now": "t"})
    assert "nothing changed" in msg
