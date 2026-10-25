"""Memory consent + deletion + retention (stage15-real-transfer subagent).

Covers: forget (in-memory + persistent), confirm_candidate accept/reject,
secret/credential refusal (existing markers), purge_expired boundary
(expired/at/valid/None), purge idempotence, superseded rows untouched.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.memory import MemoryStore, PersistentMemoryStore
from core.memory_policy import MemoryService, classify_write
from core.models import MemoryItem, utcnow

OP = {"Authorization": "Bearer dev-token"}


def _item(text, **kw):
    kw.setdefault("category", "preferences")
    kw.setdefault("importance", 0.8)
    return MemoryItem(text=text, **kw)


# ---------- forget ----------

def test_forget_memory_store():
    ms = MemoryStore()
    kept = ms.remember(_item("user prefers dark mode everywhere"))
    assert kept is not None
    assert ms.forget(kept.id) is True
    assert all(m.id != kept.id for m in ms.all())
    assert ms.recall("dark mode preference") == [] or all(
        "dark mode everywhere" not in m.text
        for m, _ in ms.recall("dark mode preference"))
    assert ms.forget(kept.id) is False  # already gone
    assert ms.forget("no-such-id") is False


def test_forget_persistent_store(tmp_path):
    db = str(tmp_path / "mem.db")
    s1 = PersistentMemoryStore(db)
    kept = s1.remember(_item("user prefers dark mode everywhere"))
    assert kept is not None
    assert s1.forget(kept.id) is True
    # gone after reopen (SQLite row deleted, not just dict sweep)
    s2 = PersistentMemoryStore(db)
    assert all(m.id != kept.id for m in s2.all())
    assert s1.forget(kept.id) is False
    assert s1.forget("no-such-id") is False

def test_forget_persistent_store_deletes_every_derived_surface(tmp_path):
    """Stage 22 evidence: a forget leaves NO accessible copy anywhere
    the store controls — recall (in-memory + embedding), export, and
    the SQLite row itself (embedding column lives in the same row)."""
    import sqlite3
    db = str(tmp_path / "mem.db")
    s = PersistentMemoryStore(db)
    kept = s.remember(_item("user prefers dark mode everywhere"))
    assert s.forget(kept.id) is True
    assert s.recall("dark mode preference") == []
    exported_ids = [m.id for m in s.export()["items"]]
    assert kept.id not in exported_ids
    row = sqlite3.connect(db).execute(
        "SELECT id FROM memory WHERE id=?", (kept.id,)).fetchone()
    assert row is None


# ---------- confirm_candidate accept / reject ----------

def test_confirm_accept_stores():
    svc = MemoryService(MemoryStore())
    c = svc.consider("My team uses Go for services and has done so for years")
    assert c["decision"] == "candidate" and not c["stored"]
    ok = svc.confirm_candidate(c["candidate_id"], True)
    assert ok["stored"] and ok["id"]
    assert c["candidate_id"] not in svc.pending_candidates


def test_confirm_accept_default_ok():
    svc = MemoryService(MemoryStore())  # legacy accept-only call shape
    c = svc.consider("My team uses Go for services and has done so for years")
    ok = svc.confirm_candidate(c["candidate_id"])
    assert ok["stored"]


def test_confirm_reject_drops():
    svc = MemoryService(MemoryStore())
    c = svc.consider("My team uses Go for services and has done so for years")
    out = svc.confirm_candidate(c["candidate_id"], False)
    assert out["stored"] is False
    assert c["candidate_id"] not in svc.pending_candidates
    assert svc.store.recall("team uses Go") == [] or all(
        "team uses Go" not in m.text
        for m, _ in svc.store.recall("team uses Go"))


def test_confirm_unknown_candidate():
    svc = MemoryService(MemoryStore())
    out = svc.confirm_candidate("cand-00000000", True)
    assert out == {"stored": False, "reason": "unknown candidate"}
    out2 = svc.confirm_candidate("cand-00000000", False)
    assert out2["stored"] is False


# ---------- secret / credential refusal (existing markers) ----------

def test_secret_category_refused():
    ms = MemoryStore()
    with pytest.raises(ValueError):
        ms.remember(MemoryItem(text="harmless note", category="secret",
                               importance=0.9))


@pytest.mark.parametrize("text", [
    "Remember my API key is SECRET_TEST_KEY",
    "my password is hunter2 hunter2 hunter2 hunter2 hunter",
    "here is my private key material stored safely here ok",
    "the user credential bundle for login is saved here ok",
    "my token is ABCDEF123456 and keep it safe here ok",
    "the shared secret phrase for vault is bluebird nine",
])
def test_secret_markers_refused(text):
    ms = MemoryStore()
    with pytest.raises(ValueError):
        ms.remember(MemoryItem(text=text, importance=0.9))
    svc = MemoryService(MemoryStore())
    r = svc.consider(text)
    assert r["stored"] is False and r["decision"] == "never_store"
    assert classify_write(text)[0].value == "never_store"


# ---------- purge_expired boundary ----------

def _seed_boundary(store, now):
    expired = store.remember(_item(
        "expired preference for purge testing purposes",
        expires_at=now - timedelta(seconds=1)))
    at = store.remember(_item(
        "boundary preference for purge testing purposes",
        expires_at=now))
    valid = store.remember(_item(
        "valid preference for purge testing purposes",
        expires_at=now + timedelta(hours=1)))
    noexp = store.remember(_item(
        "undated preference for purge testing purposes"))
    assert all(x is not None for x in (expired, at, valid, noexp))
    return expired, at, valid, noexp


def test_purge_boundary_memory():
    ms = MemoryStore()
    now = utcnow()
    expired, at, valid, noexp = _seed_boundary(ms, now)
    removed = ms.purge_expired(now=now)
    assert removed == 1
    ids = {m.id for m in ms.all()}
    assert expired.id not in ids
    assert {at.id, valid.id, noexp.id} <= ids


def test_purge_boundary_persistent(tmp_path):
    db = str(tmp_path / "mem.db")
    s1 = PersistentMemoryStore(db)
    now = utcnow()
    expired, at, valid, noexp = _seed_boundary(s1, now)
    removed = s1.purge_expired(now=now)
    assert removed == 1
    # SQLite row gone: invisible after reopen
    s2 = PersistentMemoryStore(db)
    ids = {m.id for m in s2.all()}
    assert expired.id not in ids
    assert {at.id, valid.id, noexp.id} <= ids


def test_purge_idempotent():
    ms = MemoryStore()
    now = utcnow()
    ms.remember(_item("stale preference for idempotence check here",
                       expires_at=now - timedelta(seconds=5)))
    assert ms.purge_expired(now=now) == 1
    assert ms.purge_expired(now=now) == 0
    assert ms.purge_expired() == 0  # default clock, nothing left to drop


def test_purge_empty_is_zero():
    assert MemoryStore().purge_expired() == 0


# ---------- superseded rows untouched ----------

def test_purge_keeps_superseded_memory():
    svc = MemoryService(MemoryStore())
    svc.consider("Remember that my favorite editor is VS Code")
    old_id = svc.store.recall("favorite editor")[0][0].id
    out = svc.correct("favorite editor", "My favorite editor is Neovim")
    assert out["stored"]
    old = next(m for m in svc.store.all() if m.id == old_id)
    assert old.metadata.get("superseded_by") == out["id"]
    # force the provenance row past expiry: purge must still keep it
    old.expires_at = utcnow() - timedelta(seconds=1)
    assert svc.store.purge_expired() == 0
    assert any(m.id == old_id for m in svc.store.all())


def test_purge_keeps_superseded_persistent(tmp_path):
    db = str(tmp_path / "mem.db")
    svc = MemoryService(PersistentMemoryStore(db))
    svc.consider("Remember that my favorite editor is VS Code")
    old_id = svc.store.recall("favorite editor")[0][0].id
    out = svc.correct("favorite editor", "My favorite editor is Neovim")
    assert out["stored"]
    old = next(m for m in svc.store.all() if m.id == old_id)
    old.expires_at = utcnow() - timedelta(seconds=1)
    svc.store.remember(old)  # re-persist the expired marker
    assert svc.store.purge_expired() == 0
    fresh = PersistentMemoryStore(db)
    assert any(m.id == old_id for m in fresh.all())


# ---------- documented export (zara-memory-export-1) ----------

def test_export_shape_and_excluded_derived_data():
    ms = MemoryStore()
    kept = ms.remember(_item("user prefers dark mode everywhere"))
    out = ms.export()
    assert out["format"] == "zara-memory-export-1"
    assert out["count"] == 1
    row = out["items"][0]
    for f in ("id", "text", "category", "source", "confidence",
              "importance", "metadata", "created_at", "updated_at",
              "expires_at"):
        assert f in row, f
    # derived embedding column is not exported
    assert "embedding" not in row


def test_export_reflects_deletion_and_persistence(tmp_path):
    db = str(tmp_path / "mem.db")
    s1 = PersistentMemoryStore(db)
    a = s1.remember(_item("export survives restarts"))
    b = s1.remember(_item("temporary note to forget"))
    s1.forget(b.id)
    # restart: reopen the same file
    s2 = PersistentMemoryStore(db)
    out = s2.export()
    texts = [r["text"] for r in out["items"]]
    assert "export survives restarts" in texts
    assert "temporary note to forget" not in texts
    assert out["count"] == 1


def test_export_endpoint_live():
    stack = build_stack()
    client = TestClient(create_app(stack))
    r = client.post("/v1/memory",
                    json={"text": "endpoint export fixture note",
                          "category": "notes", "importance": 0.9},
                    headers=OP)
    assert r.status_code == 200 and r.json()["stored"]
    r2 = client.get("/v1/memory/export", headers=OP)
    assert r2.status_code == 200
    out = r2.json()
    assert out["format"] == "zara-memory-export-1"
    body = r2.text.lower()
    # no secret-material in exports even if someone had tried to store it
    assert "password" not in body and "apikey" not in body
    for it in out["items"]:
        assert "embedding" not in it
