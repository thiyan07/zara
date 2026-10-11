"""Conversation sessions — bounded, compacted, expiring. Not infinite logs."""
from __future__ import annotations
import sqlite3
import threading
import uuid
from typing import Optional
from pydantic import BaseModel, Field
from .models import utcnow
from .tracing import redact

MAX_MESSAGES = 60          # hard cap per session
COMPACT_KEEP = 20          # keep most recent N on compaction
SESSION_TTL_S = 7 * 86400  # idle expiry


class SessionMessage(BaseModel):
    role: str  # user | assistant | system | tool
    text: str
    tool: str = ""
    created_at: str = ""


class Session(BaseModel):
    id: str = ""
    user: str = "user"
    device_id: str = "cloud"
    messages: list[SessionMessage] = Field(default_factory=list)
    mission_id: Optional[str] = None
    execution_id: Optional[str] = None
    voice_state: str = "idle"
    context_refs: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    compacted: int = 0


class SessionStore:
    """Bounded in-memory session log with optional SQLite persistence.

    Memory mode (default, ``db_path=""``) is byte-identical to the
    original: nothing touches disk. When ``db_path`` is set, every
    create/append upserts the full (bounded) session as JSON and
    expire_idle/delete remove the row; rows are loaded on init with
    corrupt rows skipped.
    """

    _SCHEMA = ("CREATE TABLE IF NOT EXISTS sessions "
               "(id TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def __init__(self, db_path: str = "") -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._db_path = db_path
        if self._db_path:
            self._db_init()

    # ----- persistence helpers (no-op unless db_path set; no lock taken) -----
    def _db_init(self) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.execute(self._SCHEMA)
            con.commit()
            rows = con.execute("SELECT data FROM sessions").fetchall()
        finally:
            con.close()
        for (data,) in rows:
            try:
                s = Session.model_validate_json(data)
            except Exception:
                continue  # corrupt row: skip, never crash load
            self._sessions[s.id] = s

    def _db_save(self, s: Session) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.execute(self._SCHEMA)
            con.execute(
                "INSERT OR REPLACE INTO sessions (id, data) VALUES (?, ?)",
                (s.id, s.model_dump_json()))
            con.commit()
        finally:
            con.close()

    def _db_delete(self, session_id: str) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.execute(self._SCHEMA)
            con.execute("DELETE FROM sessions WHERE id = ?",
                        (session_id,))
            con.commit()
        finally:
            con.close()

    def create(self, user: str = "user", device_id: str = "cloud") -> Session:
        now = utcnow().isoformat()
        s = Session(id=f"ses-{uuid.uuid4().hex[:12]}", user=user,
                    device_id=device_id, created_at=now, updated_at=now)
        with self._lock:
            self._sessions[s.id] = s
            if self._db_path:
                self._db_save(s)
        return s

    def get(self, session_id: str) -> Session:
        return self._sessions[session_id]

    def append(self, session_id: str, role: str, text: str,
               tool: str = "") -> Session:
        safe = redact(text)[:2000]  # raw tokens/keys never persist
        with self._lock:
            s = self._sessions[session_id]
            s.messages.append(SessionMessage(role=role, text=safe,
                                             tool=tool,
                                             created_at=utcnow().isoformat()))
            s.updated_at = utcnow().isoformat()
            if len(s.messages) > MAX_MESSAGES:
                drop = len(s.messages) - COMPACT_KEEP
                s.messages = [SessionMessage(
                    role="system",
                    text=f"[compacted {drop} older messages]",
                    created_at=utcnow().isoformat())] + s.messages[-COMPACT_KEEP:]
                s.compacted += drop
            if self._db_path:
                self._db_save(s)
            return s

    def expire_idle(self, now_iso: Optional[str] = None) -> int:
        now = now_iso or utcnow().isoformat()
        with self._lock:
            dead = [sid for sid, s in self._sessions.items()
                    if (now > s.updated_at and
                        _age_s(s.updated_at, now) > SESSION_TTL_S)]
            for sid in dead:
                del self._sessions[sid]
                if self._db_path:
                    self._db_delete(sid)
        return len(dead)

    def delete(self, session_id: str) -> bool:
        """Remove a session from memory and disk. False if unknown."""
        with self._lock:
            if session_id not in self._sessions:
                return False
            del self._sessions[session_id]
            if self._db_path:
                self._db_delete(session_id)
            return True

    def list_recent(self, limit: int = 20) -> list[dict]:
        """Newest-first summaries; last_text truncated to 120, redacted."""
        with self._lock:
            ordered = sorted(self._sessions.values(),
                             key=lambda s: s.updated_at,
                             reverse=True)[:max(0, limit)]
            return [{"id": s.id, "updated_at": s.updated_at,
                     "message_count": len(s.messages),
                     "last_text": redact(s.messages[-1].text
                                         if s.messages else "")[:120]}
                    for s in ordered]

    def pending_reply(self, session_id: str) -> bool:
        """True only when the last message is role=user with no following
        assistant message — i.e. the turn was interrupted (e.g. restart)
        and is awaiting a reply. Reopenable; never auto-marked
        successful. False for unknown/empty sessions."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None or not s.messages:
                return False
            return s.messages[-1].role == "user"

    def turn_status(self, session_id: str) -> str:
        """Coarse turn state: 'active' (last message is user-role, awaiting
        reply), 'completed' (last message is assistant-role), or
        'unknown' (missing/empty session or other trailing role)."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None or not s.messages:
                return "unknown"
            last = s.messages[-1].role
            if last == "user":
                return "active"
            if last == "assistant":
                return "completed"
            return "unknown"


def _age_s(then_iso: str, now_iso: str) -> float:
    from datetime import datetime
    try:
        then = datetime.fromisoformat(then_iso)
        now = datetime.fromisoformat(now_iso)
        return (now - then).total_seconds()
    except ValueError:
        return 0.0
