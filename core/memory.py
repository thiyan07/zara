"""Hybrid memory: structured metadata + semantic retrieval, pluggable embeddings."""
from __future__ import annotations
import math
import re
import threading
import uuid
from collections import Counter
from .models import MemoryItem, utcnow

SECRET_MARKERS = [re.compile(p, re.I) for p in
                  (r"secret", r"password", r"api[_-]?key", r"\btoken\b",
                   r"private[_-]?key", r"credential")]
SECRET_CATEGORY = "secret"

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
        v = [0.0] * self.dim
        for tok in _tokens(text):
            v[hash(tok) % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

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

    def remember(self, item: MemoryItem) -> MemoryItem | None:
        """Returns the stored item, or None if policy/secrets rejected it."""
        if item.category == SECRET_CATEGORY or \
                any(p.search(item.text) for p in SECRET_MARKERS):
            raise ValueError("refused: secrets must not enter RAG memory")
        if not self.policy.keep(item):
            return None
        item.id = item.id or f"mem-{uuid.uuid4().hex[:12]}"
        item.embedding = self.embedder.embed(item.text)
        item.updated_at = utcnow()
        with self._lock:
            self._items[item.id] = item
        return item

    def forget(self, item_id: str) -> bool:
        with self._lock:
            return self._items.pop(item_id, None) is not None

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
            sem = _cosine(q_emb, it.embedding or [])
            kw_toks = set(_tokens(it.text))
            kw = len(q_toks & kw_toks) / max(1, len(q_toks | kw_toks))
            score = 0.7 * sem + 0.3 * kw
            scored.append((it, score))
        scored.sort(key=lambda s: s[1], reverse=True)
        return scored[:top_k]

    def all(self) -> list[MemoryItem]:
        return list(self._items.values())
