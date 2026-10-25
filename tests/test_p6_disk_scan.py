"""P5 feature-hunt ⑰ (2026-10-24) — "where did my disk go".

dust/gdu/dua idea (interface only, stdlib re-implementation in
core/storage_scan.scan_dir_usage + agent wrapper storage.scan +
core intent routing): a SHALLOW per-directory overview of the home
disk, honestly bounded.

Contract pinned here:
- subtree sizes aggregate (apparent size st_size), level-1 children
  ranked by bytes; bytes inside nested subdirs roll up to every
  ancestor bucket;
- symlinks are never followed and never counted (neither their
  target's bytes);
- hard links are counted once while the dedup window (200k inodes)
  holds, and the window is stated in the semantics line;
- unreadable directories are skipped AND counted (fail-open honest
  counts, bounded sample list);
- a time/entry budget stops the walk with partial=True and the
  detail says the sizes are FLOORS, never totals;
- the agent wrapper keeps the house scope (_fs_path: allowed roots
  only, sensitive paths refused, default = home);
- the chat matcher routes the BREAKDOWN ask to storage.scan and
  leaves the health asks ("how much disk space", "disk usage") on
  system.health; absent capability falls through honestly;
- the deterministic formatter renders the top list + honesty lines
  (never a bare "Done").
"""
import os
import stat
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

from core.storage_scan import scan_dir_usage  # noqa: E402


def _mk(root, rel, size):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"x" * size)
    return p


@pytest.fixture()
def tree(tmp_path):
    a = _mk(str(tmp_path), "a/one.bin", 3000)
    _mk(str(tmp_path), "a/two.bin", 1000)
    _mk(str(tmp_path), "a/sub/three.txt", 400)
    _mk(str(tmp_path), "b/four.bin", 2000)
    _mk(str(tmp_path), "root_loose.bin", 600)
    return str(tmp_path), a


def test_subtree_sizes_roll_up_and_rank(tree):
    root, _ = tree
    out = scan_dir_usage(root, top_n=5, budget_s=10.0)
    by = {r["path"]: r["bytes"] for r in out["items"]}
    # top-level children rank first; ancestors carry the subtree total
    assert by["a"] == 3000 + 1000 + 400
    assert by["b"] == 2000
    assert out["root_files_bytes"] == 600
    assert out["root_files_count"] == 1
    assert out["total_bytes"] == 3000 + 1000 + 400 + 2000 + 600
    ranked = [r["path"] for r in out["items"][:2]]
    assert ranked == ["a", "b"]          # 4400 > 2000
    assert not out["budget"]["partial"]


def test_symlink_target_never_counted(tree, tmp_path_factory):
    root, _ = tree
    outside = tmp_path_factory.mktemp("outside")
    with open(os.path.join(outside, "big"), "wb") as f:
        f.write(b"y" * 50_000)
    os.symlink(os.path.join(outside, "big"),
               os.path.join(root, "hook"))
    out = scan_dir_usage(root, budget_s=10.0)
    assert out["total_bytes"] == 7000     # 50k target bytes excluded
    assert out["symlinks_skipped"] == 1


def test_hardlink_counted_once(tree):
    root, a_file = tree
    os.link(a_file, os.path.join(root, "a", "dup.bin"))
    out = scan_dir_usage(root, budget_s=10.0)
    by = {r["path"]: r["bytes"] for r in out["items"]}
    assert by["a"] == 4400                # not 7400


def test_unreadable_dir_skipped_and_counted(tree):
    root, _ = tree
    d = os.path.join(root, "locked")
    os.makedirs(d)
    os.chmod(d, 0)                        # unreadable (non-root)
    try:
        out = scan_dir_usage(root, budget_s=10.0)
        assert out["unreadable_count"] == 1
        assert any(p.endswith("locked") for p in out["unreadable"])
    finally:
        os.chmod(d, 0o700)


def test_budget_stops_with_floor_semantics(tree):
    root, _ = tree
    out = scan_dir_usage(root, top_n=2, budget_s=1e-9)
    assert out["budget"]["partial"] is True
    assert "floors" in out["detail"]
    assert out["budget"]["reason"] == "time budget"
    assert out["entries_seen"] >= 0


def test_entry_cap_partial(tree):
    root, _ = tree
    out = scan_dir_usage(root, max_entries=2, budget_s=10.0)
    assert out["budget"]["partial"] is True
    assert out["budget"]["reason"] == "entry cap"
    assert out["entries_seen"] <= 2


def test_bad_root_raises():
    with pytest.raises(ValueError):
        scan_dir_usage("/nonexistent-zara-test", budget_s=1.0)


def test_wrapper_scope_and_defaults(monkeypatch):
    from device.linux import tools_linux as tl
    # explicit allowed path (tmp root is allowed)
    with tempfile.TemporaryDirectory() as d:
        _mk(d, "big/f.bin", 5000)
        out = tl.storage_scan({"path": d, "top_n": 3}, {})
        assert out["items"][0]["path"] == "big"
        assert out["items"][0]["bytes"] == 5000
        assert "top 1 under" in out["detail"]
    # sensitive path refused through the house scope
    with pytest.raises(PermissionError):
        tl.storage_scan({"path": "~/.ssh"}, {})
    # outside allowed roots refused
    with pytest.raises(PermissionError):
        tl.storage_scan({"path": "/usr/share"}, {})


def test_registry_def_includes_bounded_scan():
    from core.device_tools import DEVICE_TOOL_DEFS
    matches = [d for d in DEVICE_TOOL_DEFS if d.name == "storage.scan"]
    assert len(matches) == 1
    d = matches[0]
    assert d.risk.value == "safe"
    assert d.permission.value == "public"
    assert "storage.read" in d.required_capabilities
    assert d.supported_devices == ["linux"]
    props = d.input_schema.get("properties", {})
    assert "path" in props and "budget_seconds" in props


def _propose(parser, utterance, caps):
    from core.intent import ParserContext
    ctx = ParserContext(known_capabilities=caps)
    return parser.parse(utterance, ctx)


def _local_parser():
    from core.intent import LocalIntentParser
    return LocalIntentParser()


CAPSWithMany = {"storage.scan", "system.health", "system.battery"}


def test_matcher_breakdown_phrases_route_to_scan():
    p = _local_parser()
    for ask in ("what's using my disk space?",
                "where did my disk go",
                "where did all my disk space go",
                "what is taking up all my space",
                "what's eating my disk",
                "disk usage breakdown",
                "show biggest folders on my disk"):
        out = _propose(p, ask, CAPSWithMany)
        assert out.capability == "storage.scan", (ask, out)
        assert not out.needs_clarification


def test_health_asks_stay_on_health():
    p = _local_parser()
    for ask in ("how much disk space do I have",
                "disk usage",
                "disk space",
                "check my battery"):
        out = _propose(p, ask, CAPSWithMany)
        assert out.capability != "storage.scan", ask


def test_absent_capability_falls_through():
    p = _local_parser()
    out = _propose(p, "what's using my disk space",
                   {"system.health"})
    assert out.capability != "storage.scan"


def test_formatter_deterministic_top_list():
    from core.intent import IntentTurnService, IntentProposal
    proposal = IntentProposal(
        request_id="t", utterance="what's using my disk",
        capability="storage.scan", parameters={}, confidence=0.8,
        needs_clarification=False, source="local")
    msg = IntentTurnService._success_message(proposal, {"result": {
        "root": "/home/me", "items": [
            {"path": "projects", "bytes": 80 * 10 ** 9,
             "files": 1203},
            {"path": "downloads", "bytes": 45 * 10 ** 9,
             "files": 87}],
        "root_files_bytes": 4.2 * 10 ** 6,
        "root_files_count": 132,
        "total_bytes": 0, "entries_seen": 55123,
        "unreadable_count": 2,
        "budget": {"budget_s": 4.0, "partial": True,
                   "reason": "time budget"},
        "detail": "partial scan"}})
    assert msg.startswith("Disk usage under /home/me "
                          "(apparent size):")
    assert "80.0 GB projects (1203 files)" in msg
    assert "45.0 GB downloads (87 files)" in msg
    assert "PARTIAL" in msg and "floors" in msg
    assert "2 path(s) could not be read" in msg


def test_formatter_clean_scan_has_no_partial_line():
    from core.intent import IntentTurnService, IntentProposal
    proposal = IntentProposal(
        request_id="t", utterance="disk breakdown",
        capability="storage.scan", parameters={}, confidence=0.8,
        needs_clarification=False, source="local")
    msg = IntentTurnService._success_message(proposal, {"result": {
        "root": "/home/me", "items": [
            {"path": "a", "bytes": 100, "files": 1}],
        "root_files_bytes": 0, "root_files_count": 0,
        "entries_seen": 1, "unreadable_count": 0,
        "budget": {"budget_s": 4.0, "partial": False,
                   "reason": ""}}})
    assert "PARTIAL" not in msg
    assert "100 B a (1 files)" in msg
