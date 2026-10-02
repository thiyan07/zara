"""Push registry — server-side device association for Core -> device nudges.

Stage 9: push is a best-effort WAKE-UP, never a command channel and never
the only channel (poll fallback always works: jobs/poll + notifications).
Payloads carry IDs + short titles only — no secrets, no tool args, no
credentials. Real FCM delivery needs operator credentials that are NOT in
this repo; until then the mock transport records deliveries for tests and
docs label FCM_PHYSICAL/PROVIDER_VALIDATION_DEFERRED.
"""
from __future__ import annotations
import threading
from typing import Optional
from .tracing import redact


class PushRegistration:
    def __init__(self, device_id: str, token: str,
                 provider: str = "mock") -> None:
        self.device_id = device_id
        self.token = token
        self.provider = provider
        self.invalid = False


class PushRegistry:
    """Token lifecycle + dedup + payload policy. Thread-safe."""

    MAX_TITLE = 120
    MAX_SEEN = 1000

    def __init__(self) -> None:
        self._regs: dict[str, PushRegistration] = {}
        self._seen: set[tuple[str, str]] = set()  # (device_id, event_id)
        self._lock = threading.Lock()
        self.outbox: list[dict] = []  # mock-transport deliveries (tests)

    def register(self, device_id: str, token: str,
                 provider: str = "mock") -> PushRegistration:
        if not token or len(token) > 512:
            raise ValueError("invalid push token")
        if provider not in ("mock", "fcm"):
            raise ValueError("unknown push provider")
        with self._lock:
            reg = PushRegistration(device_id, token, provider)
            self._regs[device_id] = reg
            return reg

    def rotate(self, device_id: str, new_token: str) -> PushRegistration:
        """Rotation kills the old token: only the newest token is valid."""
        with self._lock:
            old = self._regs.get(device_id)
            provider = old.provider if old is not None else "mock"
        return self.register(device_id, new_token, provider)

    def invalidate(self, device_id: str) -> bool:
        with self._lock:
            reg = self._regs.get(device_id)
            if reg is None:
                return False
            reg.invalid = True
            return True

    def send(self, device_id: str, event_id: str, title: str) -> dict:
        """Best-effort nudge. Deduped per (device, event). Never raises for
        transport issues — returns a status dict the caller can log."""
        key = (device_id, event_id)
        with self._lock:
            reg = self._regs.get(device_id)
            if reg is None:
                return {"sent": False, "reason": "no-registration"}
            if reg.invalid:
                return {"sent": False, "reason": "invalid-token"}
            if key in self._seen:
                return {"sent": False, "reason": "duplicate"}
            self._seen.add(key)
            if len(self._seen) > self.MAX_SEEN:
                self._seen = set(list(self._seen)[-self.MAX_SEEN:])
            payload = {"device_id": device_id, "event_id": event_id,
                       "title": redact(title)[:self.MAX_TITLE]}
            if reg.provider == "mock":
                self.outbox.append(payload)
                return {"sent": True, "provider": "mock"}
            # fcm: no credentials in this repo — record intent, report pending
            self.outbox.append({**payload, "provider": "fcm-pending"})
            return {"sent": False, "reason": "fcm-credentials-deferred",
                    "provider": "fcm"}

    def registration(self, device_id: str) -> Optional[PushRegistration]:
        with self._lock:
            return self._regs.get(device_id)
