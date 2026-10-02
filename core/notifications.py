"""Notification manager — queued user-facing messages, per-device outbox."""
from __future__ import annotations
import threading
import uuid
from .models import Notification

class NotificationManager:
    def __init__(self, on_event=None) -> None:
        self._items: list[Notification] = []
        self._lock = threading.Lock()
        self._on_event = on_event

    def create(self, title: str, body: str = "",
               device_id: str | None = None,
               mission_id: str | None = None) -> Notification:
        n = Notification(id=f"notif-{uuid.uuid4().hex[:12]}", title=title,
                         body=body, device_id=device_id, mission_id=mission_id)
        with self._lock:
            self._items.append(n)
        if self._on_event:
            self._on_event("notification_created",
                           {"notification_id": n.id, "title": title})
        return n

    def pending(self, device_id: str | None = None) -> list[Notification]:
        with self._lock:
            return [n for n in self._items
                    if not n.delivered and (device_id is None or n.device_id in (None, device_id))]

    def mark_delivered(self, notification_id: str) -> None:
        with self._lock:
            for n in self._items:
                if n.id == notification_id:
                    n.delivered = True
