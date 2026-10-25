"""File-workflow capabilities on the Linux agent (real filesystem, real
permission semantics — nothing simulated):

- `filesystem.search`: bounded read-only name search under allowed roots.
- `filesystem.edit`: one unambiguous substring replacement in a scratch
  file — backup-first, atomic replace, VERIFIED by re-read.
- `filesystem.move`: scratch-dir rename — conflict-safe, verified.

Unit/simulated classification: real local filesystem via pytest tmp dirs
under /tmp; no external account, no network, no device fleet. The
boundaries exercised (HOME/tmp scope, scratch confinement, O_NOFOLLOW,
sensitive-name refusal) are the same ones live-validated on the device in
prior rungs (`filesystem.write/delete`, `test_scratch_files.py`).
"""
import os

import pytest

from device.linux import tools_linux as t


def _mk(root, name, text="hello"):
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "fh" / "zara-scratch"
    monkeypatch.setattr(t, "HOME", str(root.parent))
    return str(root.parent)


# ---------------- filesystem.search ----------------

def test_search_finds_visible_matches_only(home):
    _mk(home, "report.txt")
    _mk(home, "sub/nested.ledger.txt")
    _mk(home, ".cache/junk.txt")          # hidden: never enumerated
    _mk(home, "secret.txt")
    out = t.filesystem_search({"pattern": "*.txt"}, {})
    names = [os.path.basename(m["path"]) for m in out["matches"]]
    assert sorted(names) == ["nested.ledger.txt", "report.txt"], names
    assert out["count"] == 2          # secret.txt is filtered (sensitive)
    assert out["root"] == home
    assert not out["truncated"]
    assert out["unreadable_dirs"] == 0


def test_search_is_case_insensitive_and_globs(home):
    _mk(home, "Report.TXT")
    out = t.filesystem_search({"pattern": "report.txt"}, {})
    assert out["count"] == 1
    assert out["matches"][0]["path"].endswith("Report.TXT")


def test_search_reports_unreadable_dir_honestly(home):
    _mk(home, "a.txt")
    locked = os.path.join(home, "locked")
    os.makedirs(locked, exist_ok=True)
    os.chmod(locked, 0)
    try:
        out = t.filesystem_search({"pattern": "*.txt"}, {})
        assert out["count"] == 1
        assert out["unreadable_dirs"] >= 1   # honest, not a fake empty scan
    finally:
        os.chmod(locked, 0o755)


def test_search_caps_and_reports_truncation(home):
    for i in range(6):
        _mk(home, f"f{i}.dat")
    out = t.filesystem_search({"pattern": "*.dat", "max_results": 3}, {})
    assert out["count"] == 3 and out["truncated"] is True


def test_search_refuses_out_of_scope_and_bad_pattern():
    with pytest.raises(PermissionError):
        t.filesystem_search({"pattern": "*.txt", "path": "/etc"}, {})
    with pytest.raises(ValueError):
        t.filesystem_search({"pattern": "   "}, {})
    with pytest.raises(ValueError):
        t.filesystem_search({"pattern": "a\nb"}, {})


def test_search_hidden_dirs_are_not_descended(home):
    _mk(home, ".config/inside/target.txt")
    _mk(home, "top.txt")
    out = t.filesystem_search({"pattern": "*.txt"}, {})
    assert out["count"] == 1


# ---------------- filesystem.edit ----------------

def test_edit_replaces_once_with_verified_backup(home):
    original = "pay invoice 42 tomorrow"
    t.filesystem_write({"name": "note.txt", "text": original}, {})
    out = t.filesystem_edit({"name": "note.txt",
                             "old": "invoice 42", "new": "invoice 43"}, {})
    assert out["replaced"] is True and out["verified"] is True
    assert out["backup"] and os.path.basename(out["backup"]) == "note.txt.bak"
    # Verification is real: re-read both endpoints.
    rd = t.filesystem_read({"path": out["path"]}, {})
    assert rd["text"] == "pay invoice 43 tomorrow"
    with open(out["backup"], "rb") as f:
        assert f.read().decode() == original


def test_edit_not_found_and_ambiguous_refused(home):
    t.filesystem_write({"name": "note.txt", "text": "a b a b"}, {})
    with pytest.raises(ValueError):
        t.filesystem_edit({"name": "note.txt", "old": "zz", "new": "y"}, {})
    with pytest.raises(ValueError) as e:
        t.filesystem_edit({"name": "note.txt", "old": "a", "new": "y"}, {})
    assert "each" in str(e.value) or "times" in str(e.value)
    # File unchanged after the refusals.
    assert t.filesystem_read({"path": str(home) + "/zara-scratch/note.txt"},
                             {})["text"] == "a b a b"


def test_edit_never_overwrites_an_existing_backup(home):
    t.filesystem_write({"name": "note.txt", "text": "one"}, {})
    t.filesystem_edit({"name": "note.txt", "old": "one", "new": "two"}, {})
    with open(os.path.join(home, "zara-scratch", "note.txt.bak"), "w") as f:
        pass  # leave the backup in place
    with pytest.raises(PermissionError):
        t.filesystem_edit({"name": "note.txt", "old": "two", "new": "three"},
                          {})
    assert t.filesystem_read(
        {"path": str(home) + "/zara-scratch/note.txt"}, {})["text"] == "two"


def test_edit_leaves_no_temp_files_and_refuses_cap(home):
    t.filesystem_write({"name": "note.txt", "text": "x"}, {})
    out = t.filesystem_edit({"name": "note.txt", "old": "x", "new": "y"}, {})
    scratch = os.listdir(os.path.join(home, "zara-scratch"))
    assert not any(n.endswith(".tmp") for n in scratch), scratch
    with pytest.raises(ValueError):
        t.filesystem_edit({"name": "note.txt", "old": "y",
                           "new": "z" * (t.SCRATCH_MAX_BYTES + 1)}, {})


def test_edit_missing_file_refused(home):
    with pytest.raises(ValueError):
        t.filesystem_edit({"name": "ghost.txt", "old": "a", "new": "b"}, {})


def test_edit_verification_failure_is_reported_never_faked(home):
    """A tampered verification read must surface, never report ok."""
    t.filesystem_write({"name": "note.txt", "text": "hello"}, {})
    real_read = t._scratch_open_rd

    def poisoned(name, pfd, _real=real_read, _calls=[]):
        data = _real(name, pfd)
        # Only the FINAL (verification) read is corrupted: the first
        # read is the pre-edit content, which must stay truthful.
        if len(_calls) >= 1:
            _calls.append(len(data))
            return b"other"
        _calls.append(len(data))
        return data

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(t, "_scratch_open_rd", poisoned)
        out = t.filesystem_edit({"name": "note.txt", "old": "hello",
                                 "new": "there"}, {})
    assert out["verified"] is False and out["replaced"] is False


def test_edit_refuses_traversal_and_sensitive_names(home):
    for bad in ("../evil.txt", "a/b.txt", "id_rsa", "c.token"):
        with pytest.raises((ValueError, PermissionError)):
            t.filesystem_edit({"name": bad, "old": "a", "new": "b"}, {})


# ---------------- filesystem.move ----------------

def test_move_renames_and_verifies(home):
    t.filesystem_write({"name": "old.txt", "text": "data"}, {})
    out = t.filesystem_move({"src": "old.txt", "dst": "new.txt"}, {})
    assert out["moved"] is True and out["verified"] is True
    assert not os.path.exists(out["src"]) and os.path.isfile(out["dst"])
    assert t.filesystem_read({"path": out["dst"]}, {})["text"] == "data"


def test_move_conflict_is_structured_and_source_untouched(home):
    t.filesystem_write({"name": "a.txt", "text": "A"}, {})
    t.filesystem_write({"name": "b.txt", "text": "B"}, {})
    out = t.filesystem_move({"src": "a.txt", "dst": "b.txt"}, {})
    assert out["moved"] is False and out["conflict"] == "exists"
    assert t.filesystem_read(
        {"path": str(home) + "/zara-scratch/a.txt"}, {})["text"] == "A"
    assert t.filesystem_read(
        {"path": str(home) + "/zara-scratch/b.txt"}, {})["text"] == "B"


def test_move_confirmed_overwrite_is_atomic_and_verified(home):
    t.filesystem_write({"name": "a.txt", "text": "A"}, {})
    t.filesystem_write({"name": "b.txt", "text": "B"}, {})
    out = t.filesystem_move({"src": "a.txt", "dst": "b.txt",
                             "overwrite": True}, {})
    assert out["moved"] is True and out["verified"] is True
    assert t.filesystem_read(
        {"path": str(home) + "/zara-scratch/b.txt"}, {})["text"] == "A"
    fys = os.listdir(os.path.join(home, "zara-scratch"))
    assert "a.txt" not in fys


def test_move_refuses_symlinks_both_endpoints(home):
    t.filesystem_write({"name": "real.txt", "text": "R"}, {})
    link = os.path.join(home, "zara-scratch", "link.txt")
    try:
        os.symlink(os.path.join(home, "zara-scratch", "real.txt"), link)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(PermissionError):
        t.filesystem_move({"src": "link.txt", "dst": "any.txt"}, {})
    t.filesystem_write({"name": "src.txt", "text": "S"}, {})
    with pytest.raises(PermissionError):
        t.filesystem_move({"src": "src.txt", "dst": "link.txt",
                           "overwrite": True}, {})
    # Nothing was changed by the refusals.
    assert t.filesystem_read(
        {"path": str(home) + "/zara-scratch/src.txt"}, {})["text"] == "S"
    assert t.filesystem_read(
        {"path": str(home) + "/zara-scratch/real.txt"}, {})["text"] == "R"


def test_move_missing_source_and_same_name_refused(home):
    with pytest.raises(ValueError):
        t.filesystem_move({"src": "ghost.txt", "dst": "n.txt"}, {})
    with pytest.raises(ValueError):
        t.filesystem_move({"src": "same.txt", "dst": "same.txt"}, {})


def test_move_directory_is_refused(home):
    d = os.path.join(home, "zara-scratch", "adir")
    os.makedirs(d, exist_ok=True)
    t.filesystem_write({"name": "f.txt", "text": "x"}, {})
    with pytest.raises(PermissionError):
        t.filesystem_move({"src": "adir", "dst": "f.txt",
                           "overwrite": True}, {})
    assert os.path.isdir(d)   # directory untouched


# ---------------- filesystem.extract ----------------

def test_extract_text_md_json_csv_and_unsupported(home):
    p = _mk(home, "doc.md", "# Title\nbody line\n")
    out = t.filesystem_extract({"path": p}, {})
    assert out["supported"] is True and out["kind"] == "text"
    assert out["lines"] == 2 and out["words"] == 4

    jp = os.path.join(home, "data.json")
    with open(jp, "w") as f:
        f.write('{"b": 1, "a": {"x": [1, 2]}}')
    j = t.filesystem_extract({"path": jp}, {})
    assert j["supported"] is True and j["value_type"] == "object"
    assert j["top_keys"] == ["a", "b"]

    bad = os.path.join(home, "broken.json")
    with open(bad, "w") as f:
        f.write("{not json")
    with pytest.raises((ValueError,)):
        # parse error must SURFACE, not degrade into something plausible
        t.filesystem_extract({"path": bad}, {})

    cp = os.path.join(home, "table.csv")
    with open(cp, "w") as f:
        f.write("a,b\n1,2\n3,4\n5,6\n7,8\n9,10\n11,12\n")
    c = t.filesystem_extract({"path": cp}, {})
    assert c["supported"] is True and c["data_rows"] == 6
    assert c["header"] == ["a", "b"]
    assert len(c["preview"]) == 5

    binp = os.path.join(home, "blob.bin")
    with open(binp, "wb") as f:
        f.write(b"\x00\x01")
    bad2 = t.filesystem_extract({"path": binp}, {})
    assert bad2["supported"] is False and "unsupported" in bad2["error"]


def test_extract_refuses_out_of_scope_and_sensitive(home):
    with pytest.raises(PermissionError):
        t.filesystem_extract({"path": "/etc/passwd"}, {})
    sensitive = os.path.join(home, "credential.md")
    os.makedirs(home, exist_ok=True)
    with open(sensitive, "w") as f:
        f.write("secret")
    with pytest.raises(PermissionError):
        t.filesystem_extract({"path": sensitive}, {})
