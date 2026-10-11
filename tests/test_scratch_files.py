"""Scoped scratch files (Night 5 §D): approval-gated, traversal-proof,
capped, reversible writes confined to ~/zara-scratch."""
import os

import pytest

from device.linux import tools_linux as t


def test_F01_write_read_delete_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "HOME", str(tmp_path))
    out = t.filesystem_write({"name": "note.txt", "text": "hello"}, {})
    assert out["bytes"] == 5 and out["path"].endswith("note.txt")
    rd = t.filesystem_read({"path": out["path"]}, {})
    assert rd["text"] == "hello"
    assert t.filesystem_delete({"name": "note.txt"}, {})["deleted"] is True
    assert t.filesystem_delete({"name": "note.txt"}, {})["deleted"] is False


def test_F02_traversal_and_sensitive_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "HOME", str(tmp_path))
    for bad in ("../evil.txt", "a/b.txt", "..", ".", "id_rsa", "x.token",
                "my credential"):
        with pytest.raises((PermissionError, ValueError)):
            t.filesystem_write({"name": bad, "text": "x"}, {})
    assert os.listdir(tmp_path) == [] or \
        not any("evil" in str(p) for p in os.listdir(tmp_path))


def test_F06_symlink_read_refused(tmp_path, monkeypatch):
    # Even an in-scope symlink is refused at open time (O_NOFOLLOW): the
    # node could be swapped between validation and use. Documented
    # trade-off: read symlink targets by their real path instead.
    monkeypatch.setattr(t, "HOME", str(tmp_path))
    target = tmp_path / "data.txt"
    target.write_text("hello")
    link = tmp_path / "link.txt"
    try:
        os.symlink(str(target), str(link))
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(PermissionError):
        t.filesystem_read({"path": str(link)}, {})
    assert t.filesystem_read({"path": str(target)}, {})["text"] == "hello"


def test_F03_size_cap_and_types(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "HOME", str(tmp_path))
    with pytest.raises(ValueError):
        t.filesystem_write({"name": "big.txt", "text": "x" * 70000}, {})
    with pytest.raises(ValueError):
        t.filesystem_write({"name": 1, "text": "x"}, {})
    assert not (tmp_path / t.SCRATCH_DIR_NAME / "big.txt").exists()


def test_F04_write_is_approval_gated():
    from core.app import build_stack
    stack = build_stack()
    rec = stack["engine"]._prepare(
        "filesystem.write", {"name": "n", "text": "x"}, "cloud")
    from core.models import ExecutionState
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION


def test_F05_symlink_escape_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "HOME", str(tmp_path))
    outside = tmp_path / "outside.txt"
    outside.write_text("s3cret")
    link = tmp_path / t.SCRATCH_DIR_NAME / "link.txt"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(str(outside), str(link))
        have_link = True
    except OSError:
        have_link = False
    if have_link:
        # Writes through the link are refused (O_NOFOLLOW)...
        with pytest.raises(PermissionError):
            t.filesystem_write({"name": "link.txt", "text": "pwn"}, {})
        # ...while unlink removes only the link itself, never the target
        # (unlink(2) does not follow the final component).
        assert t.filesystem_delete({"name": "link.txt"}, {})["deleted"] is True
        assert outside.read_text() == "s3cret"
        assert not link.exists() and not link.is_symlink()
