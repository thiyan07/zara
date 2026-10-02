"""Audit log — append-only operational metadata. Never secrets/CoT."""
from __future__ import annotations
import sqlite3
import threading
from .models import utcnow

class AuditLog:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, "
                "ts TEXT NOT NULL, actor TEXT, action TEXT, target TEXT, "
                "detail TEXT, ok INTEGER)")

    def record(self, actor: str, action: str, target: str = "",
               detail: str = "", ok: bool = True) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO audit (ts, actor, action, target, detail, ok) "
                "VALUES (?,?,?,?,?,?)",
                (utcnow().isoformat(), actor, action, target, detail[:2000],
                 1 if ok else 0))

    def query(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, actor, action, target, detail, ok FROM audit "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "actor": r[1], "action": r[2], "target": r[3],
                 "detail": r[4], "ok": bool(r[5])} for r in rows]
