"""Hybrid memory: structured metadata + semantic retrieval, pluggable embeddings."""
from __future__ import annotations
import math
import os as _os
import re
import threading
import time as _time
import uuid
from collections import Counter
from .models import MemoryItem, utcnow

SECRET_MARKERS = [re.compile(p, re.I) for p in
                  (r"secret", r"password", r"api[ _-]?key", r"\btoken\b",
                   r"private[ _-]?key", r"credential")]
SECRET_CATEGORY = "secret"

def _as_aware(dt):
    """Coerce datetime/ISO-string to aware datetime for expiry comparison."""
    from datetime import timezone as _tz
    from datetime import datetime as _dt
    if isinstance(dt, str):
        dt = _dt.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return dt

def _coerce_now(now):
    if now is None:
        return utcnow()
    return _as_aware(now)


# ---------- conservative retention ----------

#: Opportunistic sweep cadence (seconds) and batch cap. Sweeps delete
#: strictly-expired, non-pinned, non-superseded rows only — the same
#: predicate as purge_expired — so they can never remove anything the
#: manual purge would keep.
SWEEP_MIN_INTERVAL_S = 3600.0
SWEEP_BATCH_MAX = 500


def is_pinned(item: MemoryItem) -> bool:
    """User-pinned marker lives in metadata (no schema migration):
    metadata {"pinned": true} survives forget/re-remember and SQLite
    round-trips. Pinned rows are exempt from every automatic deletion."""
    try:
        return bool((item.metadata or {}).get("pinned") is True)
    except Exception:  # noqa: BLE001 — malformed metadata fails safe
        return False


def default_ttl_days() -> float:
    """Conservative configurable default TTL for new memories, in days.
    0 (default) disables it: memories live until forgotten/purged.
    Set ZARA_MEMORY_DEFAULT_TTL_DAYS to a positive number to opt in.
    Invalid values fail safe to 0."""
    try:
        v = float((_os.environ.get("ZARA_MEMORY_DEFAULT_TTL_DAYS",
                                   "") or "0").strip())
    except ValueError:
        return 0.0
    return v if v > 0 else 0.0

def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]{2,}", text.lower())

def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0

class EmbeddingProvider:
    """Replaceable embedding backend. Default is a local TF-hash fallback
    (no network, no paid API). Real models plug in here in Stage 3."""
    dim: int = 128

    def embed(self, text: str) -> list[float]:
        import hashlib
        v = [0.0] * self.dim
        for tok in _tokens(text):
            # stable hash: Python hash() is seed-randomized per process
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


class LocalEmbeddingProvider(EmbeddingProvider):
    """ONNX-backed local embeddings (default bge-small-en-v1.5, 384 dim,
    ~65 MB). Needs the `fastembed` package + one-time model download.
    Falls back to nothing — construction fails loudly if unavailable, so
    callers can catch it and keep the hash fallback. Set
    ZARA_EMBEDDINGS=local to enable; ZARA_EMBED_MODEL overrides the model."""

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5",
                 cache_dir: str = "") -> None:
        import os as _os
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise RuntimeError("fastembed not installed: pip install fastembed")
        self._model = TextEmbedding(
            model,
            cache_dir=cache_dir or _os.path.expanduser("~/.cache/fastembed"))
        probe = list(self._model.embed(["probe"]))
        self.dim = len(probe[0])

    def embed(self, text: str) -> list[float]:
        return list(self._model.embed([text]))[0].tolist()

class MemoryPolicy:
    """Decides what deserves long-term storage. Not every turn is memory."""
    MIN_IMPORTANCE = 0.35

    def keep(self, item: MemoryItem) -> bool:
        if item.category == SECRET_CATEGORY:
            return False
        if any(p.search(item.text) for p in SECRET_MARKERS):
            return False
        return item.importance >= self.MIN_IMPORTANCE and len(item.text.strip()) >= 3

class MemoryStore:
    def __init__(self, embedder: EmbeddingProvider | None = None,
                 policy: MemoryPolicy | None = None) -> None:
        self._items: dict[str, MemoryItem] = {}
        self._lock = threading.Lock()
        self.embedder = embedder or EmbeddingProvider()
        self.policy = policy or MemoryPolicy()
        self._last_sweep = 0.0

    def remember(self, item: MemoryItem) -> MemoryItem | None:
        """Returns the stored item, or None if policy/secrets rejected it."""
        if item.category == SECRET_CATEGORY or \
                any(p.search(item.text) for p in SECRET_MARKERS):
            raise ValueError("refused: secrets must not enter RAG memory")
        if not self.policy.keep(item):
            return None
        if item.expires_at is None:
            ttl = default_ttl_days()
            if ttl > 0:
                from datetime import timedelta as _td
                item.expires_at = utcnow() + _td(days=ttl)
        item.id = item.id or f"mem-{uuid.uuid4().hex[:12]}"
        item.embedding = self.embedder.embed(item.text)
        item.updated_at = utcnow()
        with self._lock:
            self._items[item.id] = item
        self._maybe_sweep()
        return item

    def forget(self, item_id: str) -> bool:
        with self._lock:
            return self._items.pop(item_id, None) is not None

    def set_pinned(self, item_id: str, pinned: bool) -> bool:
        """Mark/unmark a row as user-retained. Pinned rows are exempt
        from every automatic deletion (sweeps and purges); explicit
        forget() still removes them. Returns False for unknown ids."""
        with self._lock:
            it = self._items.get(item_id)
            if it is None:
                return False
            meta = dict(it.metadata or {})
            if pinned:
                meta["pinned"] = True
            else:
                meta.pop("pinned", None)
            it.metadata = meta
            return True

    def purge_expired(self, now=None) -> int:
        """Delete rows with expires_at < now. Returns count removed.

        Boundary: expires_at == now or None is kept. Superseded
        provenance rows (metadata.superseded_by) are never purged.
        Pinned rows (metadata.pinned) are never purged.
        Idempotent: second call with same clock returns 0.
        """
        ref = _coerce_now(now)
        with self._lock:
            expired = [
                k for k, it in self._items.items()
                if it.expires_at is not None
                and not (it.metadata or {}).get("superseded_by")
                and not is_pinned(it)
                and _as_aware(it.expires_at) < ref
            ]
            for k in expired:
                self._items.pop(k, None)
        return len(expired)

    def _maybe_sweep(self) -> int:
        """Opportunistic expired-only cleanup, throttled and bounded.
        Opt-in via ZARA_MEMORY_AUTO_SWEEP=1 (default off: existing
        behavior is byte-for-byte unchanged without it). Never raises: a
        sweep failure must not corrupt the calling operation. Returns
        rows removed (0 on throttle/failure/disabled)."""
        if (_os.environ.get("ZARA_MEMORY_AUTO_SWEEP", "").strip().lower()
                not in ("1", "true", "yes", "on")):
            return 0
        now_mono = _time.monotonic()
        # _last_sweep <= 0 means "never swept" (fresh store or test
        # reset): the first sweep is always eligible instead of being
        # throttled for up to SWEEP_MIN_INTERVAL_S after process start
        # on machines with low monotonic uptime.
        if self._last_sweep > 0 and \
                now_mono - self._last_sweep < SWEEP_MIN_INTERVAL_S:
            return 0
        try:
            removed = self._sweep_batch(SWEEP_BATCH_MAX)
        except Exception:  # noqa: BLE001 — cleanup never breaks callers
            return 0
        self._last_sweep = now_mono
        return removed

    def _sweep_batch(self, limit: int) -> int:
        """One bounded expired-only pass using the purge predicate."""
        ref = utcnow()
        with self._lock:
            expired = [
                k for k, it in self._items.items()
                if it.expires_at is not None
                and not (it.metadata or {}).get("superseded_by")
                and not is_pinned(it)
                and _as_aware(it.expires_at) < ref
            ][:max(1, limit)]
            for k in expired:
                self._items.pop(k, None)
        return len(expired)

    def recall(self, query: str, top_k: int = 5,
               category: str | None = None) -> list[tuple[MemoryItem, float]]:
        """Hybrid: embedding cosine blended with keyword overlap."""
        q_emb = self.embedder.embed(query)
        q_toks = set(_tokens(query))
        scored: list[tuple[MemoryItem, float]] = []
        with self._lock:
            items = list(self._items.values())
        now = utcnow()
        for it in items:
            if category and it.category != category:
                continue
            if it.expires_at and it.expires_at < now:
                continue
            if it.metadata.get("superseded_by"):
                continue  # corrected memories lose authority, kept as provenance
            sem = _cosine(q_emb, it.embedding or [])
            kw_toks = set(_tokens(it.text))
            kw = len(q_toks & kw_toks) / max(1, len(q_toks | kw_toks))
            score = 0.7 * sem + 0.3 * kw
            scored.append((it, score))
        scored.sort(key=lambda s: s[1], reverse=True)
        # Opportunistic retention sweep rides the read path (throttled,
        # bounded, never raising); recall results themselves are computed
        # from the pre-sweep snapshot above.
        self._maybe_sweep()
        return scored[:top_k]

    def all(self) -> list[MemoryItem]:
        return list(self._items.values())


class PersistentMemoryStore(MemoryStore):
    """SQLite-backed memory: same policy/retrieval, actually persisted.

    Embeddings serialize as JSON (local path). Production path is
    PostgreSQL + pgvector — same rows, vector column (see db.py notes).
    """

    def __init__(self, path: str = "assistant.db",
                 embedder=None, policy=None) -> None:
        import json as _json
        import sqlite3 as _sqlite3
        self._json = _json
        super().__init__(embedder=embedder, policy=policy)
        self._db = _sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS memory (id TEXT PRIMARY KEY, "
                "category TEXT NOT NULL, text TEXT NOT NULL, source TEXT, "
                "confidence REAL, importance REAL, embedding TEXT, "
                "metadata TEXT, created_at TEXT, updated_at TEXT, "
                "expires_at TEXT)")
        for row in self._db.execute("SELECT * FROM memory").fetchall():
            try:
                item = self._row(row)
                if item.embedding is None:
                    item.embedding = self.embedder.embed(item.text)
                self._items[item.id] = item
            except Exception:  # noqa: BLE001 — skip corrupt rows, stay up
                continue

    def _row(self, row) -> MemoryItem:
        return MemoryItem(id=row[0], category=row[1], text=row[2],
                          source=row[3] or "", confidence=row[4] or 0.5,
                          importance=row[5] if row[5] is not None else 0.5,
                          embedding=self._json.loads(row[6]) if row[6] else None,
                          metadata=self._json.loads(row[7]) if row[7] else {},
                          created_at=row[8], updated_at=row[9],
                          expires_at=row[10])

    def remember(self, item: MemoryItem):
        stored = super().remember(item)
        if stored is None:
            return None
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO memory VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (stored.id, stored.category, stored.text, stored.source,
                 stored.confidence, stored.importance,
                 self._json.dumps(stored.embedding or []),
                 self._json.dumps(stored.metadata),
                 stored.created_at.isoformat()
                 if hasattr(stored.created_at, "isoformat")
                 else stored.created_at,
                 stored.updated_at.isoformat()
                 if hasattr(stored.updated_at, "isoformat")
                 else stored.updated_at,
                 stored.expires_at.isoformat()
                 if stored.expires_at and hasattr(stored.expires_at, "isoformat")
                 else stored.expires_at))
        return stored

    def forget(self, item_id: str) -> bool:
        gone = super().forget(item_id)
        with self._lock, self._db:
            self._db.execute("DELETE FROM memory WHERE id=?", (item_id,))
        return gone

    def set_pinned(self, item_id: str, pinned: bool) -> bool:
        ok = super().set_pinned(item_id, pinned)
        if not ok:
            return False
        with self._lock, self._db:
            item = self._items.get(item_id)
            self._db.execute("UPDATE memory SET metadata=? WHERE id=?",
                             (self._json.dumps(item.metadata if item else {}),
                              item_id))
        return True

    def purge_expired(self, now=None) -> int:
        """Delete rows with expires_at < now from SQLite + dict sweep.

        Same boundary/provenance rules as MemoryStore.purge_expired:
        only strictly-expired, non-superseded rows go. Idempotent.
        """
        ref = _coerce_now(now)
        with self._lock:
            expired = [
                k for k, it in self._items.items()
                if it.expires_at is not None
                and not (it.metadata or {}).get("superseded_by")
                and not is_pinned(it)
                and _as_aware(it.expires_at) < ref
            ]
        if expired:
            with self._lock, self._db:
                self._db.execute(
                    f"DELETE FROM memory WHERE id IN "
                    f"({','.join('?' * len(expired))})", expired)
                for k in expired:
                    self._items.pop(k, None)
        return len(expired)

    def _sweep_batch(self, limit: int) -> int:
        """Bounded expired-only pass, mirrored to SQLite in one
        transaction: commit applies to both stores, rollback to neither."""
        ref = utcnow()
        with self._lock:
            expired = [
                k for k, it in self._items.items()
                if it.expires_at is not None
                and not (it.metadata or {}).get("superseded_by")
                and not is_pinned(it)
                and _as_aware(it.expires_at) < ref
            ][:max(1, limit)]
        if expired:
            with self._lock, self._db:
                self._db.execute(
                    f"DELETE FROM memory WHERE id IN "
                    f"({','.join('?' * len(expired))})", expired)
                for k in expired:
                    self._items.pop(k, None)
        return len(expired)
