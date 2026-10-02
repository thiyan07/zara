"""Rate limiting — bounded token buckets per client key. Abuse controls that
fail closed (429) without touching the security model."""
from __future__ import annotations
import threading
import time


class RateLimiter:
    def __init__(self, max_hits: int, window_s: float = 60.0) -> None:
        self.max_hits = max_hits
        self.window_s = window_s
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """Returns (allowed, retry_after_s). Thread-safe, bounded memory."""
        now = time.time()
        with self._lock:
            stamps = [t for t in self._hits.get(key, [])
                      if now - t < self.window_s]
            if len(stamps) >= self.max_hits:
                oldest = min(stamps) if stamps else now
                return False, max(0.0, self.window_s - (now - oldest))
            stamps.append(now)
            self._hits[key] = stamps[-self.max_hits:]
            if len(self._hits) > 10000:
                # prune idle keys; cheapest bounded guard
                self._hits = {k: v for k, v in self._hits.items() if v and
                              now - v[-1] < self.window_s}
            return True, 0.0
