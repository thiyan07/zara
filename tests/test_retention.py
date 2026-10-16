"""Conservative retention lifecycle: default TTL, pinning, auto-sweep.

Sweeps delete strictly-expired, non-pinned, non-superseded rows only.
Nothing here invents expiry for user content: the default TTL is off
unless ZARA_MEMORY_DEFAULT_TTL_DAYS is set positive.
"""
import threading

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.memory import (MemoryStore, PersistentMemoryStore, default_ttl_days,
                         is_pinned)
from core.models import MemoryItem

OP = {"Authorization": "Bearer dev-token"}


def _nosweep(store):
    """Freeze opportunistic sweeps so manual purge/TTL behavior is exact."""
    import time as _t
    store._last_sweep = _t.monotonic() + 10 ** 9
    return store


@pytest.fixture(autouse=True)
def _sweep_on(monkeypatch):
    monkeypatch.setenv("ZARA_MEMORY_AUTO_SWEEP", "1")


def _item(text="remember the lab door code 4821", **kw):
    args = {"category": "episodic", "text": text}
    args.update(kw)
    return MemoryItem(**args)


def test_default_ttl_off_unless_env(monkeypatch):
    monkeypatch.delenv("ZARA_MEMORY_DEFAULT_TTL_DAYS", raising=False)
    assert default_ttl_days() == 0.0
    monkeypatch.setenv("ZARA_MEMORY_DEFAULT_TTL_DAYS", "30")
    assert default_ttl_days() == 30.0
    monkeypatch.setenv("ZARA_MEMORY_DEFAULT_TTL_DAYS", "nope")
    assert default_ttl_days() == 0.0
    monkeypatch.setenv("ZARA_MEMORY_DEFAULT_TTL_DAYS", "-5")
    assert default_ttl_days() == 0.0


def test_remember_applies_default_ttl_only_when_opted_in(monkeypatch):
    monkeypatch.delenv("ZARA_MEMORY_DEFAULT_TTL_DAYS", raising=False)
    s = MemoryStore()
    assert s.remember(_item()).expires_at is None
    monkeypatch.setenv("ZARA_MEMORY_DEFAULT_TTL_DAYS", "7")
    got = s.remember(_item(text="trip notes for june"))
    assert got.expires_at is not None
    # Explicit per-item expiry is never overwritten.
    from datetime import datetime, timezone
    fixed = datetime(2030, 1, 1, tzinfo=timezone.utc)
    got2 = s.remember(_item(text="fixed date note", expires_at=fixed))
    assert got2.expires_at == fixed


def test_pinned_exempt_from_purge_and_sweep(monkeypatch):
    monkeypatch.delenv("ZARA_MEMORY_DEFAULT_TTL_DAYS", raising=False)
    from datetime import datetime, timezone
    past = datetime(2001, 1, 1, tzinfo=timezone.utc)
    s = _nosweep(MemoryStore())
    a = s.remember(_item(text="old note one", expires_at=past))
    b = s.remember(_item(text="old note two",
                         expires_at=past, metadata={"pinned": True}))
    assert is_pinned(b) and not is_pinned(a)
    assert s.set_pinned("missing", True) is False
    assert s.purge_expired() == 1
    assert s.forget(a.id) is False  # already purged
    assert b.id in [i.id for i in s.all()]
    # Explicit forget still removes pinned rows.
    assert s.forget(b.id) is True


def test_sweep_throttled_bounded_and_never_raises(monkeypatch):
    monkeypatch.delenv("ZARA_MEMORY_DEFAULT_TTL_DAYS", raising=False)
    from datetime import datetime, timezone
    past = datetime(2001, 1, 1, tzinfo=timezone.utc)
    s = MemoryStore()
    for i in range(5):
        it = _item(text=f"stale note number {i}", expires_at=past)
        it.id = f"s{i}"
        s._items[f"s{i}"] = it
    # Throttle: a fresh sweep blocks the next one.
    s._last_sweep = 0.0
    assert s._maybe_sweep() == 5
    assert s._maybe_sweep() == 0
    assert s.all() == []
    # Batch cap respected.
    s._last_sweep = 0.0
    for i in range(10):
        s._items[f"k{i}"] = _item(text=f"note {i}", expires_at=past)
        s._items[f"k{i}"].id = f"k{i}"
    import core.memory as M
    monkeypatch.setattr(M, "SWEEP_BATCH_MAX", 3)
    s._last_sweep = 0.0
    assert s._maybe_sweep() == 3
    assert len(s.all()) == 7


def test_concurrent_sweep_safe():
    from datetime import datetime, timezone
    past = datetime(2001, 1, 1, tzinfo=timezone.utc)
    s = MemoryStore()
    for i in range(50):
        it = _item(text=f"concurrent note {i}", expires_at=past)
        it.id = f"c{i}"
        s._items[f"c{i}"] = it
    s._last_sweep = 0.0
    results = []

    def worker():
        s._last_sweep = 0.0
        results.append(s._maybe_sweep())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sum(results) == 50
    assert s.all() == []


def test_persistent_sweep_mirrors_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("ZARA_MEMORY_DEFAULT_TTL_DAYS", raising=False)
    from datetime import datetime, timezone
    past = datetime(2001, 1, 1, tzinfo=timezone.utc)
    db = str(tmp_path / "m.db")
    s = _nosweep(PersistentMemoryStore(db))
    a = s.remember(_item(text="gone soon", expires_at=past))
    b = s.remember(_item(text="stays", metadata={"pinned": True},
                         expires_at=past))
    s._last_sweep = 0.0
    assert s._maybe_sweep() == 1
    rows = s._db.execute("SELECT id FROM memory").fetchall()
    assert [r[0] for r in rows] == [b.id]
    # Restart: pinned row survives reopen, gone row stays gone.
    s2 = PersistentMemoryStore(db)
    assert [i.id for i in s2.all()] == [b.id]
    assert s2.set_pinned(b.id, False) is True
    assert s2.purge_expired() == 1
    assert s2.all() == []


def test_broken_db_sweep_never_breaks_caller(tmp_path, monkeypatch):
    monkeypatch.delenv("ZARA_MEMORY_DEFAULT_TTL_DAYS", raising=False)
    from datetime import datetime, timezone
    s = _nosweep(PersistentMemoryStore(str(tmp_path / "m.db")))
    s.remember(_item(text="ok note"))
    s._db.close()
    s._last_sweep = 0.0
    assert s._maybe_sweep() == 0  # swallowed, caller unaffected


def test_pin_endpoint_roundtrip():
    stack = build_stack(fabric_db="")
    client = TestClient(create_app(stack))
    r = client.post("/v1/memory", json={"text": "keep this note"},
                    headers=OP)
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    r = client.post(f"/v1/memory/{mid}/pin", json={"pinned": True},
                    headers=OP)
    assert r.status_code == 200 and r.json()["retained"] is True
    r = client.post(f"/v1/memory/nope/pin", json={"pinned": True},
                    headers=OP)
    assert r.status_code == 404
    r = client.post(f"/v1/memory/{mid}/pin", json={"pinned": False},
                    headers=OP)
    assert r.json()["retained"] is False


def test_pin_endpoint_requires_operator_auth():
    from fastapi.testclient import TestClient
    from core.app import build_stack, create_app
    stack = build_stack(fabric_db="")
    client = TestClient(create_app(stack))
    r = client.post("/v1/memory", json={"text": "auth check"},
                    headers=OP)
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    r = client.post(f"/v1/memory/{mid}/pin", json={"pinned": True})
    assert r.status_code in (401, 403), r.text
    # Single-tenant store: operator auth is the isolation boundary —
    # unauthenticated callers change nothing.
    from core.memory import is_pinned as _is_pinned
    item = stack["memory"].recall("auth check", top_k=5)[0][0]
    assert _is_pinned(item) is False
