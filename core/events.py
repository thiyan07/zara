"""Event bus — the event contract. In-process pub/sub; app.py persists/fans out."""
from __future__ import annotations
import threading
import uuid
from collections import defaultdict
from typing import Callable
from .models import Event, utcnow

CANONICAL_EVENTS = {
    "user_message", "task_completed", "task_failed", "device_online",
    "device_offline", "battery_changed", "network_changed", "file_changed",
    "process_failed", "scheduled_event", "permission_required",
    "permission_granted", "mission_state_changed", "notification_created",
    "execution_finished",
}

class EventBus:
    def __init__(self, max_history: int = 1000) -> None:
        self._subs: dict[str, list[Callable[[Event], None]]] = defaultdict(list)
        self._history: list[Event] = []
        self._max = max_history
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, fn: Callable[[Event], None]) -> None:
        with self._lock:
            self._subs[event_type].append(fn)

    def unsubscribe(self, event_type: str, fn: Callable[[Event], None]) -> None:
        with self._lock:
            try:
                self._subs[event_type].remove(fn)
            except ValueError:
                pass

    def publish(self, type: str, source: str = "core",
                device_id: str | None = None, mission_id: str | None = None,
                payload: dict | None = None) -> Event:
        ev = Event(id=f"evt-{uuid.uuid4().hex[:12]}", type=type, source=source,
                   device_id=device_id, mission_id=mission_id,
                   payload=payload or {}, created_at=utcnow())
        with self._lock:
            subs = list(self._subs.get(type, [])) + list(self._subs.get("*", []))
            self._history.append(ev)
            self._history = self._history[-self._max:]
        for fn in subs:
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 — subscriber faults never break publish
                pass
        return ev

    def history(self, event_type: str | None = None, limit: int = 100) -> list[Event]:
        with self._lock:
            items = [e for e in self._history if not event_type or e.type == event_type]
        return items[-limit:]
