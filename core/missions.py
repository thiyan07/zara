"""Mission engine — the mission contract. Long-running goals with checkpoints."""
from __future__ import annotations
import threading
import uuid
from typing import Optional
from .models import Mission, MissionState, MissionStep, TERMINAL_MISSIONS, utcnow

# Allowed transitions; terminal states are immutable.
TRANSITIONS: dict[MissionState, set[MissionState]] = {
    MissionState.CREATED: {MissionState.PLANNING, MissionState.CANCELLED},
    MissionState.PLANNING: {MissionState.EXECUTING, MissionState.WAITING_FOR_PERMISSION,
                            MissionState.FAILED, MissionState.CANCELLED},
    MissionState.WAITING_FOR_PERMISSION: {MissionState.EXECUTING, MissionState.CANCELLED,
                                          MissionState.FAILED},
    MissionState.EXECUTING: {MissionState.WAITING, MissionState.RETRYING,
                             MissionState.VERIFYING, MissionState.COMPLETED,
                             MissionState.FAILED, MissionState.WAITING_FOR_PERMISSION,
                             MissionState.CANCELLED},
    MissionState.WAITING: {MissionState.EXECUTING, MissionState.CANCELLED, MissionState.FAILED},
    MissionState.RETRYING: {MissionState.EXECUTING, MissionState.FAILED, MissionState.CANCELLED},
    MissionState.VERIFYING: {MissionState.COMPLETED, MissionState.FAILED,
                             MissionState.RETRYING},
    MissionState.COMPLETED: set(),
    MissionState.FAILED: set(),
    MissionState.CANCELLED: set(),
}

class IllegalTransition(Exception):
    pass


class MissionEngine:
    def __init__(self, on_event=None) -> None:
        self._missions: dict[str, Mission] = {}
        self._lock = threading.Lock()
        self._on_event = on_event

    def create(self, goal: str, steps: Optional[list[dict]] = None,
               device_id: Optional[str] = None) -> Mission:
        m = Mission(id=f"mission-{uuid.uuid4().hex[:12]}", goal=goal,
                    device_id=device_id,
                    steps=[MissionStep(**s) for s in (steps or [])])
        with self._lock:
            self._missions[m.id] = m
        self._emit(m, "created")
        return m

    def get(self, mission_id: str) -> Mission:
        return self._missions[mission_id]

    def list(self) -> list[Mission]:
        return list(self._missions.values())

    def transition(self, mission_id: str, to: MissionState,
                   error: Optional[str] = None) -> Mission:
        with self._lock:
            m = self._missions[mission_id]
            if m.state in TERMINAL_MISSIONS:
                raise IllegalTransition(f"mission {mission_id} is terminal ({m.state})")
            if to not in TRANSITIONS[m.state]:
                raise IllegalTransition(f"{m.state} -> {to} not allowed")
            m.state = to
            if error is not None:
                m.error = error
            m.updated_at = utcnow()
        self._emit(m, f"-> {to.value}")
        return m

    def checkpoint(self, mission_id: str) -> dict:
        m = self.get(mission_id)
        snap = {"state": m.state.value,
                "steps": [s.model_dump() for s in m.steps],
                "retries": m.retries, "at": utcnow().isoformat()}
        with self._lock:
            m.checkpoints.append(snap)
            m.updated_at = utcnow()
        return snap

    def restore(self, mission_id: str, index: int = -1) -> Mission:
        with self._lock:
            m = self._missions[mission_id]
            snap = m.checkpoints[index]
            m.steps = [MissionStep(**s) for s in snap["steps"]]
            m.retries = snap.get("retries", 0)
            m.updated_at = utcnow()
        return m

    def approve(self, mission_id: str, approver: str = "user") -> Mission:
        with self._lock:
            m = self._missions[mission_id]
            m.approvals.append(approver)
        if m.state == MissionState.WAITING_FOR_PERMISSION:
            return self.transition(mission_id, MissionState.EXECUTING)
        return self.get(mission_id)

    def cancel(self, mission_id: str) -> Mission:
        m = self.get(mission_id)
        if m.state in TERMINAL_MISSIONS:
            return m
        return self.transition(mission_id, MissionState.CANCELLED)

    def _emit(self, m: Mission, note: str) -> None:
        if self._on_event:
            self._on_event("mission_state_changed",
                           {"mission_id": m.id, "state": m.state.value, "note": note})



class PersistentMissionEngine(MissionEngine):
    """SQLite-backed missions: checkpoints and lifecycle survive restarts."""

    def __init__(self, path: str, on_event=None) -> None:
        super().__init__(on_event=on_event)
        import json as _json
        import sqlite3 as _sqlite3
        self._json = _json
        self._db = _sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS missions (id TEXT PRIMARY KEY, "
                "goal TEXT NOT NULL, state TEXT NOT NULL, device_id TEXT, "
                "steps TEXT, checkpoints TEXT, approvals TEXT, retries INT, "
                "max_retries INT, error TEXT, created_at TEXT, updated_at TEXT)")
        for row in self._db.execute("SELECT * FROM missions").fetchall():
            try:
                m = Mission(id=row[0], goal=row[1], state=row[2],
                            device_id=row[3],
                            steps=[MissionStep(**s) for s in _json.loads(row[4] or "[]")],
                            checkpoints=_json.loads(row[5] or "[]"),
                            approvals=_json.loads(row[6] or "[]"),
                            retries=row[7] or 0, max_retries=row[8] or 3,
                            error=row[9] or "", created_at=row[10],
                            updated_at=row[11])
                self._missions[m.id] = m
            except Exception:  # noqa: BLE001 — skip corrupt rows, stay up
                continue

    def _save(self, m: Mission) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO missions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (m.id, m.goal, m.state.value, m.device_id,
                 self._json.dumps([s.model_dump(mode="json") for s in m.steps]),
                 self._json.dumps(m.checkpoints),
                 self._json.dumps(m.approvals), m.retries, m.max_retries,
                 m.error or "",
                 m.created_at.isoformat() if hasattr(m.created_at, "isoformat") else m.created_at,
                 m.updated_at.isoformat() if hasattr(m.updated_at, "isoformat") else m.updated_at))

    def create(self, goal: str, steps=None, device_id=None) -> Mission:
        m = super().create(goal, steps, device_id)
        self._save(m)
        return m

    def transition(self, mission_id: str, to, error=None) -> Mission:
        m = super().transition(mission_id, to, error)
        self._save(m)
        return m

    def checkpoint(self, mission_id: str) -> dict:
        snap = super().checkpoint(mission_id)
        self._save(self.get(mission_id))
        return snap
