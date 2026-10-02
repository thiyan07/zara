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
