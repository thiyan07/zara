"""Conversation sessions — bounded, compacted, expiring. Not infinite logs."""
from __future__ import annotations
import threading
import uuid
from typing import Optional
from pydantic import BaseModel, Field
from .models import utcnow

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
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, user: str = "user", device_id: str = "cloud") -> Session:
        now = utcnow().isoformat()
        s = Session(id=f"ses-{uuid.uuid4().hex[:12]}", user=user,
                    device_id=device_id, created_at=now, updated_at=now)
        with self._lock:
            self._sessions[s.id] = s
        return s

    def get(self, session_id: str) -> Session:
        return self._sessions[session_id]

    def append(self, session_id: str, role: str, text: str,
               tool: str = "") -> Session:
        with self._lock:
            s = self._sessions[session_id]
            s.messages.append(SessionMessage(role=role, text=text[:2000],
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
            return s

    def expire_idle(self, now_iso: Optional[str] = None) -> int:
        now = now_iso or utcnow().isoformat()
        with self._lock:
            dead = [sid for sid, s in self._sessions.items()
                    if (now > s.updated_at and
                        _age_s(s.updated_at, now) > SESSION_TTL_S)]
            for sid in dead:
                del self._sessions[sid]
        return len(dead)


def _age_s(then_iso: str, now_iso: str) -> float:
    from datetime import datetime
    try:
        then = datetime.fromisoformat(then_iso)
        now = datetime.fromisoformat(now_iso)
        return (now - then).total_seconds()
    except ValueError:
        return 0.0
