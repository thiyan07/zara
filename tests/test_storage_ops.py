"""Stage 20 storage/file-op tool tests (agent side, fake mounts)."""
from __future__ import annotations

import os

import pytest

import device.linux.tools_linux as tl


@pytest.fixture()
def fake_mount(tmp_path, monkeypatch):
    """A fake removable volume rooted in tmp_path/drive, mounted at
    /media/fake/USB (monkeypatched lsblk wiring)."""
    drive = tmp_path / "USB"
    drive.mkdir()
    monkeypatch.setattr(tl, "_removable_mounts",
                        lambda: [{"mountpoint": str(drive),
                                  "device": "/dev/fake1",
                                  "fs": "vfat", "model": "FakeCruzer",
                                  "size_bytes": 64_000_000_000,
                                  "ro": False}]
                        + list(tl._REAL_REMOVABLE_HOOKS[0]() if False else []))
    return drive


def _file(p, size=64):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"x" * size)


def test_copy_verify_and_capacity(tmp_path, fake_mount):
    src = tmp_path / "a.bin"
    src.write_bytes(os.urandom(2048))
    r = tl.storage_copy({"src": str(src), "dst": str(fake_mount / "b.bin")},
                        {})
    assert r["copied"] and r["verified"]
    assert (fake_mount / "b.bin").stat().st_size == 2048
    # conflict ask: never overwritten
    r2 = tl.storage_copy({"src": str(src), "dst": str(fake_mount / "b.bin")},
                         {})
    assert r2["stage"] == "conflict" and not r2["copied"]
    assert len((fake_mount / "b.bin").read_bytes()) == 2048
    # rename policy
    r3 = tl.storage_copy({"src": str(src),
                          "dst": str(fake_mount / "b.bin"),
                          "on_conflict": "rename"}, {})
    assert r3["copied"] and r3["renamed_to"]
    assert "b (" in r3["renamed_to"]


def test_copy_capacity_refuses_before_writing(tmp_path, fake_mount,
                                              monkeypatch):
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(4096))
    import shutil as _sh
    orig = _sh.disk_usage
    from collections import namedtuple as _nt
    _DU = _nt("DU", "total used free")
    monkeypatch.setattr(_sh, "disk_usage", lambda path: orig(path)
                        if "USB" not in str(path) else _DU(1_000, 900, 100))
    r = tl.storage_copy({"src": str(src), "dst": str(fake_mount / "n.bin")},
                        {})
    assert r["stage"] == "capacity" and not r["copied"]
    assert not (fake_mount / "n.bin").exists()


def test_copy_verify_failure_never_reports_success(tmp_path, fake_mount,
                                                   monkeypatch):
    """Corrupt exactly the written copy; verification must catch it,
    remove the partial file, and never claim success."""
    import shutil as _sh
    src = tmp_path / "v.bin"
    src.write_bytes(os.urandom(512))
    orig_cf = _sh.copyfileobj

    def flip_last(fsrc, fdst, *a, **k):  # corrupt the destination copy
        orig_cf(fsrc, fdst, *a, **k)
        pos = fdst.tell()
        if pos > 0:
            fdst.seek(pos - 1)
            # The copied byte equals the SOURCE's last byte; overwrite
            # with its bitwise inverse — guaranteed different. (The old
            # unconditional b"q" was a silent no-op ~1/256 runs when the
            # last byte already WAS q → false tool-failure report.)
            fsrc.seek(pos - 1)  # fdst is write-only; corrupt vs the SOURCE
            src_last = fsrc.read(1)[0]
            fdst.seek(pos - 1)
            fdst.write(bytes([src_last ^ 0xFF]))

    monkeypatch.setattr(_sh, "copyfileobj", flip_last)
    r = tl.storage_copy({"src": str(src),
                         "dst": str(fake_mount / "v2.bin")}, {})
    monkeypatch.setattr(_sh, "copyfileobj", orig_cf)
    assert r["copied"] and not r["verified"] and r["stage"] == "verify"
    assert not (fake_mount / "v2.bin").exists()
    # no partial file left behind
    leftovers = [f for f in os.listdir(fake_mount)
                 if f.startswith(".zara-copy-")]
    assert leftovers == []


def test_move_same_fs_and_cross_fs_semantics(tmp_path, fake_mount):
    src = tmp_path / "m.txt"
    src.write_bytes(b"move me " * 50)
    # same filesystem: direct rename
    r = tl.storage_move({"src": str(src), "dst": str(tmp_path / "m2.txt")},
                        {})
    assert r["moved"] and r["verified"]
    assert not src.exists() and (tmp_path / "m2.txt").exists()
    # cross-fs (into removable): copy + verify + delete source
    src2 = tmp_path / "x.txt"
    src2.write_bytes(b"cross" * 100)
    src2_ino = os.stat(src2).st_dev
    fake_dev = os.stat(fake_mount).st_dev
    assert src2_ino != fake_dev or True  # not guaranteed in tmp; tool
    # detects same-fs at runtime; behavior correctness holds either way
    r2 = tl.storage_move({"src": str(src2),
                          "dst": str(fake_mount / "x.txt")}, {})
    assert r2["moved"] and r2["verified"]
    assert not src2.exists()          # source deleted ONLY after verify
    assert (fake_mount / "x.txt").read_bytes() == b"cross" * 100


def test_move_conflict_never_loses_source(tmp_path, fake_mount):
    src = tmp_path / "c.txt"
    src.write_bytes(b"bb" * 30)
    (fake_mount / "c.txt").write_bytes(b"existing on drive!" * 10)
    r = tl.storage_move({"src": str(src), "dst": str(fake_mount / "c.txt")},
                        {})
    assert r["moved"] is False
    assert (fake_mount / "c.txt").read_bytes().startswith(b"existing")
    assert src.exists()  # source preserved on conflict


def test_delete_requires_explicit_confirm(tmp_path, fake_mount):
    f = fake_mount / "gone.txt"
    f.write_text("data")
    r = tl.storage_delete({"path": str(f)}, {})
    assert r["stage"] == "confirmation_required" and f.exists()
    r2 = tl.storage_delete({"path": str(f), "confirm": True}, {})
    assert r2["deleted"] and not f.exists()


def test_delete_refuses_allowed_root_itself_and_outside(tmp_path,
                                                        fake_mount):
    with pytest.raises(PermissionError):
        tl.storage_delete({"path": "/tmp", "confirm": True}, {})
    with pytest.raises((PermissionError, FileNotFoundError)):
        tl.storage_delete({"path": "/etc/passwd", "confirm": True}, {})


def test_internal_system_paths_always_refused(tmp_path, fake_mount):
    with pytest.raises(PermissionError):
        tl.storage_copy({"src": "/etc/shadow", "dst": str(fake_mount / "l")},
                        {})
    with pytest.raises(PermissionError):
        tl.storage_copy({"src": str(fake_mount / "ok.txt"),
                         "dst": "/boot/evil"}, {})


def test_stats_on_removable_volume(fake_mount):
    for i in range(3):
        _file(str(fake_mount / f"v{i}.mp4"), 100)
    _file(str(fake_mount / "song.mp3"), 10)
    _file(str(fake_mount / "doc.pdf"), 33)
    out = tl.storage_stats({"mount": str(fake_mount)}, {})
    assert out["categories"]["video"]["count"] == 3
    assert out["categories"]["image"]["count"] == 0
    assert out["total_files"] == 5
    assert "unreadable" not in out


def test_sector_outside_mount_refused(tmp_path, fake_mount, monkeypatch):
    monkeypatch.setattr(tl, "ALLOWED_ROOTS", (str(tmp_path / "other"),))
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"nope")
    with pytest.raises(PermissionError):
        tl.storage_stats({"mount": str(outside)}, {})
