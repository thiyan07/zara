"""Tests for Stage 20 universal connected-device awareness."""
from __future__ import annotations

import os
import threading

import pytest

from core.discovery import PeripheralRegistry, stable_id
from core.storage_scan import scan_tree


def _item(cls="usb_storage", durable="vid=0781 pid=5583 sn=ABC123",
          label="SanDisk 64GB", mount="/media/thiyan/USB", fs="vfat",
          caps=None):
    return {"device_class": cls, "label": label,
            "identity": {"durable": durable}, "connection": "usb",
            "capabilities": caps or {"read": True, "write": True},
            "mounts": [{"mountpoint": mount, "fs": fs}]}


# ---------------- registry ----------------

def test_connect_disconnect_lifecycle_and_stability():
    reg = PeripheralRegistry()
    r = reg.update_host_snapshot("laptop", [_item()])
    assert r["connected"] == [stable_id("usb_storage",
                                        {"durable": "vid=0781 pid=5583 sn=ABC123"})]
    # unchanged re-snapshot emits nothing new
    r2 = reg.update_host_snapshot("laptop", [_item()])
    assert r2["connected"] == [] and r2["unchanged"] == 1
    # disconnect
    r3 = reg.update_host_snapshot("laptop", [])
    assert len(r3["disconnected"]) == 1
    evs = [e["type"] for e in reg.events()]
    assert evs == ["peripheral_connected", "peripheral_disconnected"]
    pid = reg.list()[0].pid
    dev = reg.get(pid)
    assert dev.state == "removed" and dev.removed_at is not None
    # replug: same durable identity -> same pid, reconnect event
    r4 = reg.update_host_snapshot("laptop", [_item()])
    assert r4["connected"] == [pid]
    dev = reg.get(pid)
    assert dev.state == "present" and dev.removed_at is None


def test_transient_path_change_keeps_identity():
    """Same device, different USB bus/mountpoint -> same pid, no dup."""
    reg = PeripheralRegistry()
    reg.update_host_snapshot("laptop", [_item(mount="/media/thiyan/USBA",
                                              durable="sn=ABC123")])
    snap = [_item(mount="/media/thiyan/USBX", durable="sn=ABC123")]
    r = reg.update_host_snapshot("laptop", snap)
    assert r["connected"] == [] and r["disconnected"] == []
    assert len(reg.list()) == 1


def test_different_serial_same_model_is_a_different_device():
    a = _item(durable="sn=AAA")
    b = _item(durable="sn=BBB")
    assert stable_id("usb_storage", {"durable": "sn=AAA"}) != \
        stable_id("usb_storage", {"durable": "sn=BBB"})
    reg = PeripheralRegistry()
    r = reg.update_host_snapshot("laptop", [a, b])
    assert len(r["connected"]) == 2  # both appear, one event each


def test_detection_never_grants_trust():
    reg = PeripheralRegistry()
    reg.update_host_snapshot("laptop", [_item()])
    for d in reg.list():
        assert d.trusted is False


def test_privacy_conscious_event_payloads():
    emitted = []
    reg = PeripheralRegistry(on_event=lambda t, p, b: emitted.append(b))
    reg.update_host_snapshot("laptop", [_item(label="SanDisk 64GB")])
    b = emitted[0]
    body = str(b)
    assert "ABC123" not in body          # serial never in payloads
    assert "/media" not in body          # block/mount paths never in payloads
    assert emitted[0].get("class") == "usb_storage"
    assert b.get("pid")


def test_unknown_class_counts_as_error_and_no_entry():
    reg = PeripheralRegistry()
    r = reg.update_host_snapshot("laptop", [
        {"device_class": "", "identity": {"durable": "x"}},
        {"device_class": "toaster", "identity": {"durable": "y"}}])
    assert r["connected"] == [] and len(r["errors"]) == 2


def test_events_capped_and_multi_host_isolation():
    reg = PeripheralRegistry()
    reg.update_host_snapshot("laptop", [_item()])
    reg.update_host_snapshot("phone", [_item(cls="audio", durable="card=A")])
    assert len(reg.list()) == 2
    assert len(reg.list(state="present", host="phone")) == 1
    # snapshot diff never kills another host's records
    reg.update_host_snapshot("laptop", [])
    leftovers = [d for d in reg.list(host="phone") if d.state == "present"]
    assert leftovers and leftovers[0].device_class == "audio"


def test_change_events_for_capability_refresh():
    reg = PeripheralRegistry()
    reg.update_host_snapshot("laptop", [_item(caps={"read": True,
                                                    "write": True})])
    reg.update_host_snapshot("laptop", [_item(caps={"read": True,
                                                    "write": False,
                                                    "ro": True})])
    evs = [e["type"] for e in reg.events()]
    assert evs == ["peripheral_connected", "peripheral_changed"]


# ---------------- storage scan ----------------

def _touch(path, size=10):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)


def test_scan_categories_and_sizes(tmp_path):
    root = str(tmp_path / "drive")
    _touch(f"{root}/a.mp4", 400)
    _touch(f"{root}/b.mp3", 30)
    _touch(f"{root}/c.JPG", 20)     # case-insensitive
    _touch(f"{root}/d.pdf", 12)
    _touch(f"{root}/e.ewe", 7)      # unknown -> other, sampled
    _touch(f"{root}/.hidden.dat", 5)
    os.mkdir(f"{root}/sub")
    _touch(f"{root}/sub/f.png", 6)
    out = scan_tree(root)
    c = out["categories"]
    assert c["video"] == {"count": 1, "bytes": 400}
    assert c["audio"] == {"count": 1, "bytes": 30}
    assert c["image"] == {"count": 2, "bytes": 26}
    assert c["document"] == {"count": 1, "bytes": 12}
    assert c["other"] == {"count": 2, "bytes": 12}  # .ewe + .hidden.dat
    assert out["total_files"] == 7 and out["directories"] == 1
    assert out["hidden_files"] == 1
    sample = {s["name"] for s in out["other_samples"]}
    assert "e.ewe" in sample


def test_scan_symlinks_not_followed_and_counted(tmp_path):
    root = tmp_path / "drv"
    root.mkdir()
    (root / "real.txt").write_text("x" * 5)
    # symlink pointing outside the drive: must not be followed
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"y" * 999)
    sym = root / "sneaky"
    sym.symlink_to(outside)
    out = scan_tree(str(root))
    assert out["symlinks"] == 1
    # the outside file did not leak into stats
    assert out["total_files"] == 1 and out["total_bytes"] == 5
    assert out["symlinks_sample"]


def test_scan_unreadable_dir_is_counted_not_fatal(tmp_path):
    root = tmp_path / "d1"
    root.mkdir()
    (root / "a.txt").write_text("x")
    locked = root / "locked"
    locked.mkdir()
    _touch(str(locked / "b.mp4"), 100)
    os.chmod(locked, 0o000)
    try:
        out = scan_tree(str(root))
        assert out["unreadable_count"] >= 1 or out["total_files"] == 1
    finally:
        os.chmod(locked, 0o755)


def test_scan_cancellation_and_caps(tmp_path):
    root = tmp_path / "d2"
    root.mkdir()
    for i in range(300):
        _touch(str(root / f"f{i:03}.txt"), 1)
    cancel = threading.Event()
    cancel.set()
    out = scan_tree(str(root), cancel=cancel, max_entries=10)
    assert out["cancelled"] is True
    out2 = scan_tree(str(root), max_entries=10)
    assert out2["limits"]["hit_cap"] is True
    assert out2["total_files"] <= 11


def test_scan_bad_root(tmp_path):
    with pytest.raises(ValueError):
        scan_tree(str(tmp_path / "nope"))
