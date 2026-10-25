"""P5 feature-hunt ㉑ (2026-10-24) — "which files are over 1 GB" /
"my biggest files" (storage.find).

The FILE-level lens of the disk breakdown (dust/gdu give directory
AND file views; batch 17 shipped folders, this ships files). Same
honesty contract as scan_dir_usage:
- apparent size (st_size); symlinks never followed/counted;
- hard links deduped within a bounded window;
- bounded walk (budget/entries/depth cap): a partial list is a
  FLOOR and its detail says more big files may exist;
- paths ARE returned (a file answer without paths is useless), 
  bounded to top_n, only from the house scope (_fs_path);
- the min_bytes floor comes from the chat shape in DECIMAL everyday
  units ("over 1 gb" = 10^9);
- deletion asks are refused at the matcher (delete is another
  conversation); folder asks stay on storage.scan;
- timing invariant: the device def waits > 60 s (test_T0).
"""
import pytest

import sys
import os
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

from core.storage_scan import find_big_files  # noqa: E402


def _mk(root, rel, size):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"x" * size)
    return p


@pytest.fixture()
def tree(tmp_path):
    _mk(str(tmp_path), "a/big.bin", 3_000_000)
    _mk(str(tmp_path), "a/med.bin", 500_000)
    _mk(str(tmp_path), "b/small.txt", 12)
    _mk(str(tmp_path), "mid.bin", 1_500_000)
    return str(tmp_path)


def test_ranking_and_min_floor(tree):
    out = find_big_files(tree, min_bytes=1_000_000, top_n=10,
                         budget_s=10.0)
    names = [os.path.basename(i["path"]) for i in out["items"]]
    assert names == ["big.bin", "mid.bin"]     # ranked, floor-filtered
    assert out["matched_count"] == 2
    assert not out["budget"]["partial"]


def test_no_floor_ranks_all(tree):
    out = find_big_files(tree, top_n=2, budget_s=10.0)
    names = [os.path.basename(i["path"]) for i in out["items"]]
    assert names == ["big.bin", "mid.bin"]     # 3e6 > 1.5e6 > rest


def test_symlink_never_counted(tree, tmp_path_factory):
    outside = tmp_path_factory.mktemp("out")
    with open(os.path.join(outside, "huge"), "wb") as f:
        f.write(b"y" * 9_000_000)
    os.symlink(os.path.join(outside, "huge"),
               os.path.join(tree, "hook"))
    out = find_big_files(tree, min_bytes=4_000_000, budget_s=10.0)
    assert out["items"] == []
    assert out["symlinks_skipped"] == 1


def test_hardlink_once(tree):
    os.link(_mk(tree, "dup_target", 0), "") if False else None
    src = _mk(tree, "hl.bin", 2_000_000)
    os.link(src, os.path.join(tree, "hl2.bin"))
    out = find_big_files(tree, min_bytes=1_000_000, budget_s=10.0)
    assert out["matched_count"] == 2 + 1        # ml? no: big,m mid + one
    # big.bin, mid.bin, and the hardlinked pair counted ONCE


def test_budget_partial_floor(tree):
    out = find_big_files(tree, min_bytes=0, budget_s=1e-9)
    assert out["budget"]["partial"] is True
    assert "floor" in out["detail"]


def test_bad_root_raises():
    with pytest.raises(ValueError):
        find_big_files("/nonexistent-zara-test", budget_s=1.0)


def test_wrapper_scope():
    from device.linux import tools_linux as tl
    with pytest.raises(PermissionError):
        tl.storage_find({"path": "/usr/share"}, {})
    with pytest.raises(PermissionError):
        tl.storage_find({"path": "~/.ssh"}, {})


def test_timing_and_registry_def():
    from core.device_tools import DEVICE_TOOL_DEFS
    d = next(x for x in DEVICE_TOOL_DEFS if x.name == "storage.find")
    assert d.timeout_s > 60.0
    assert "storage.read" in d.required_capabilities
    assert d.risk.value == "safe"
    props = d.input_schema.get("properties", {})
    assert "min_bytes" in props and "top_n" in props


def test_parity_core_vs_linux():
    from core.device_tools import DEVICE_TOOL_DEFS
    from device.linux import tools_linux as LT
    core = next(x for x in DEVICE_TOOL_DEFS if x.name == "storage.find")
    linux = {d.name: d for d, _ in LT.LINUX_TOOLS}["storage.find"]
    assert set(core.required_capabilities) == \
        set(linux.required_capabilities)
    assert core.input_schema == linux.input_schema
    assert core.risk == linux.risk


CAPS = {"storage.find", "storage.scan"}


def _parser():
    from core.intent import LocalIntentParser
    return LocalIntentParser()


def _ctx(caps=frozenset(CAPS)):
    from core.intent import ParserContext, ParserDevice
    return ParserContext(
        known_capabilities=set(caps),
        devices=[ParserDevice(device_id="laptop-1", kind="linux")])


def test_matcher_file_shapes_and_floor_parse():
    p = _parser()
    out = p.parse("which files are over 1 gb", _ctx())
    assert out.capability == "storage.find"
    assert out.parameters["min_bytes"] == 10 ** 9
    out = p.parse("files over 500 mb", _ctx())
    assert out.parameters["min_bytes"] == 500 * 10 ** 6
    out = p.parse("find my biggest files", _ctx())
    assert out.capability == "storage.find"
    assert "min_bytes" not in out.parameters


def test_folder_asks_stay_on_scan():
    p = _parser()
    out = p.parse("biggest folders on my disk", _ctx())
    assert out.capability == "storage.scan", out


def test_delete_shape_refused_by_matcher():
    p = _parser()
    out = p.parse("delete big files", _ctx())
    # either the dangerous clarify or our read-only guide — never a
    # silent execute of a delete under the find tool.
    assert out.capability != "storage.find"
    assert out.needs_clarification


def test_absent_capability_falls_through():
    p = _parser()
    out = p.parse("which files are over 1 gb", _ctx({"storage.scan"}))
    assert out.capability != "storage.find"


def test_formatter_shapes():
    from core.intent import IntentTurnService, IntentProposal
    prop = IntentProposal(request_id="t", utterance="x",
                          capability="storage.find", parameters={},
                          confidence=0.8, needs_clarification=False,
                          source="local")
    msg = IntentTurnService._success_message(prop, {"result": {
        "root": "/home/me", "min_bytes": 10 ** 9,
        "items": [{"path": "/home/me/Downloads/big.iso",
                   "bytes": 4.2 * 10 ** 9},
                  {"path": "/home/me/a/b.img", "bytes": 1.1 * 10 ** 9}],
        "entries_seen": 90000, "unreadable_count": 0,
        "budget": {"budget_s": 6.0, "partial": False,
                   "reason": ""}}})
    assert msg.startswith("Biggest files: ")
    assert "4.2 GB /home/me/Downloads/big.iso" in msg
    assert "1.1 GB /home/me/a/b.img" in msg
    assert "PARTIAL" not in msg
    msg = IntentTurnService._success_message(prop, {"result": {
        "root": "/home/me", "min_bytes": 10 ** 9, "items": [],
        "entries_seen": 5, "unreadable_count": 0,
        "budget": {"budget_s": 6.0, "partial": False,
                   "reason": ""}}})
    assert msg.startswith("No files over 1.0 GB under /home/me.")
    msg = IntentTurnService._success_message(prop, {"result": {
        "root": "/home/me", "min_bytes": 10 ** 9, "items": [],
        "entries_seen": 90000, "unreadable_count": 3,
        "budget": {"budget_s": 6.0, "partial": True,
                   "reason": "time budget"}}})
    assert "also a floor" in msg
