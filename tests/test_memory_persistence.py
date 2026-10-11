"""Memory restart persistence + no-stale-resurrection + provenance.

Proves PersistentMemoryStore (SQLite) survives process restart:
write -> reopen same file -> recall returns it; expired / superseded /
forgotten rows are never resurrected by reopen; provenance fields
(source/confidence/importance/metadata) round-trip; secret refusal
writes nothing durable.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from core.memory import PersistentMemoryStore
from core.memory_policy import MemoryService
from core.models import MemoryItem, utcnow


def _item(text, **kw):
    kw.setdefault("category", "preferences")
    kw.setdefault("importance", 0.8)
    return MemoryItem(text=text, **kw)


def _ids(hits):
    return {m.id for m, _ in hits}


# ---------- restart persistence ----------

def test_restart_persistence_recall(tmp_path):
    db = str(tmp_path / "mem.db")
    s1 = PersistentMemoryStore(db)
    kept = s1.remember(_item("user prefers dark mode everywhere"))
    assert kept is not None
    # reopen same file = simulated restart (s2 loads from SQLite, not s1 dict)
    s2 = PersistentMemoryStore(db)
    hits = s2.recall("dark mode preference")
    assert kept.id in _ids(hits)
    assert any("dark mode everywhere" in m.text for m, _ in hits)


def test_provenance_fields_round_trip(tmp_path):
    db = str(tmp_path / "mem.db")
    s1 = PersistentMemoryStore(db)
    kept = s1.remember(MemoryItem(
        category="project",
        text="project atlas uses postgres for persistence layer here",
        source="conversation",
        confidence=0.77,
        importance=0.83,
        metadata={"privacy": "user-private", "note": "prov-check"},
    ))
    assert kept is not None
    s2 = PersistentMemoryStore(db)
    got = next(m for m in s2.all() if m.id == kept.id)
    assert got.source == "conversation"
    assert got.confidence == pytest.approx(0.77)
    assert got.importance == pytest.approx(0.83)
    assert got.metadata.get("privacy") == "user-private"
    assert got.metadata.get("note") == "prov-check"
    # recall path exposes the same provenance item
    hits = dict((m.id, (m, sc)) for m, sc in s2.recall("atlas postgres"))
    assert kept.id in hits
    assert hits[kept.id][0].source == "conversation"


# ---------- no stale resurrection ----------

def test_expired_never_recalled_after_reopen(tmp_path):
    db = str(tmp_path / "mem.db")
    s1 = PersistentMemoryStore(db)
    stale = s1.remember(_item(
        "expired preference for restart resurrection check",
        expires_at=utcnow() - timedelta(seconds=5)))
    live = s1.remember(_item(
        "live preference for restart resurrection check"))
    assert stale is not None and live is not None
    s2 = PersistentMemoryStore(db)
    hits = s2.recall("restart resurrection check preference")
    assert stale.id not in _ids(hits)
    assert live.id in _ids(hits)


def test_forgotten_never_resurrected_after_reopen(tmp_path):
    db = str(tmp_path / "mem.db")
    s1 = PersistentMemoryStore(db)
    kept = s1.remember(_item("user prefers dark mode everywhere"))
    assert kept is not None
    assert s1.forget(kept.id) is True
    s2 = PersistentMemoryStore(db)
    assert all(m.id != kept.id for m in s2.all())
    assert kept.id not in _ids(s2.recall("dark mode preference"))


def test_superseded_never_recalled_after_reopen(tmp_path):
    db = str(tmp_path / "mem.db")
    svc1 = MemoryService(PersistentMemoryStore(db))
    r = svc1.consider("Remember that my favorite editor is VS Code")
    assert r["stored"]
    old_id = svc1.store.recall("favorite editor")[0][0].id
    out = svc1.correct("favorite editor", "My favorite editor is Neovim")
    assert out["stored"]
    # before restart: old kept as provenance, never recalled
    old = next(m for m in svc1.store.all() if m.id == old_id)
    assert old.metadata.get("superseded_by") == out["id"]
    assert old_id not in _ids(svc1.store.recall("favorite editor"))
    # after restart: same — marker round-trips, old still excluded
    svc2 = MemoryService(PersistentMemoryStore(db))
    reopened_old = next(m for m in svc2.store.all() if m.id == old_id)
    assert reopened_old.metadata.get("superseded_by") == out["id"]
    hits = svc2.store.recall("favorite editor")
    assert old_id not in _ids(hits)
    assert out["id"] in _ids(hits)


# ---------- secret refusal persists ----------

SECRET_TEXTS = [
    "Remember my API key is SECRET_TEST_KEY",
    "my password is hunter2 hunter2 hunter2 hunter2 hunter",
    "the shared secret phrase for vault is bluebird nine",
]


@pytest.mark.parametrize("text", SECRET_TEXTS)
def test_secret_refusal_persists_after_reopen(tmp_path, text):
    db = str(tmp_path / "mem.db")
    s1 = PersistentMemoryStore(db)
    with pytest.raises(ValueError):
        s1.remember(MemoryItem(text=text, importance=0.9))
    # nothing durable leaked: absent after reopen
    s2 = PersistentMemoryStore(db)
    assert s2.all() == []
    assert s2.recall(text) == []


def test_secret_category_refusal_persists_after_reopen(tmp_path):
    db = str(tmp_path / "mem.db")
    s1 = PersistentMemoryStore(db)
    with pytest.raises(ValueError):
        s1.remember(MemoryItem(text="harmless note", category="secret",
                               importance=0.9))
    s2 = PersistentMemoryStore(db)
    assert s2.all() == []
