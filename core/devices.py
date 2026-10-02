"""Device manager — registry, capabilities, routing, online/offline."""
from __future__ import annotations
import threading
from typing import Optional
from .models import DeviceKind, DeviceState, utcnow

class DeviceManager:
    def __init__(self, on_event=None) -> None:
        self._devices: dict[str, DeviceState] = {}
        self._lock = threading.Lock()
        self._on_event = on_event

    def register(self, device: DeviceState) -> DeviceState:
        with self._lock:
            known = device.device_id in self._devices
            self._devices[device.device_id] = device
        self._emit("device_online" if device.online else "device_offline",
                   device.device_id, {"known": known})
        return device

    def heartbeat(self, device_id: str, battery_pct: float | None = None,
                  charging: bool | None = None, network: str | None = None,
                  online: bool = True) -> DeviceState:
        with self._lock:
            d = self._devices[device_id]
            was_online = d.online
            d.online = online
            d.last_seen = utcnow()
            if battery_pct is not None and battery_pct != d.battery_pct:
                d.battery_pct = battery_pct
                self._emit("battery_changed", device_id, {"battery_pct": battery_pct})
            if charging is not None:
                d.charging = charging
            if network is not None and network != d.network:
                d.network = network
                self._emit("network_changed", device_id, {"network": network})
            if online != was_online:
                self._emit("device_online" if online else "device_offline", device_id, {})
            return d

    def revoke(self, device_id: str) -> None:
        with self._lock:
            if device_id in self._devices:
                del self._devices[device_id]
        self._emit("device_offline", device_id, {"revoked": True})

    # ---- Stage 2 presence ----

    VALID_STATUSES = {"registered", "online", "offline", "degraded",
                      "reconnecting"}

    def set_status(self, device_id: str, status: str) -> DeviceState:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"invalid presence status: {status}")
        with self._lock:
            d = self._devices[device_id]
            d.status = status
            d.online = status in ("online", "degraded", "reconnecting")
            d.last_seen = utcnow()
        self._emit("device_online" if d.online else "device_offline",
                   device_id, {"status": status})
        return d

    def mark_offline(self, device_id: str) -> DeviceState:
        return self.set_status(device_id, "offline")

    def update_capabilities(self, device_id: str,
                            capabilities: list[str]) -> DeviceState:
        with self._lock:
            d = self._devices[device_id]
            d.capabilities = list(capabilities)
            d.last_seen = utcnow()
        self._emit("device_online", device_id,
                   {"capability_update": capabilities})
        return d

    def stale_ids(self, threshold_s: float = 120.0) -> list[str]:
        """Devices not seen within threshold — reconnect/backoff candidates."""
        now = utcnow()
        with self._lock:
            return [d.device_id for d in self._devices.values()
                    if (now - d.last_seen).total_seconds() > threshold_s]

    def get(self, device_id: str) -> DeviceState:
        return self._devices[device_id]

    def list(self) -> list[DeviceState]:
        return list(self._devices.values())

    def select(self, capabilities: list[str],
               exclude_offline: bool = True) -> Optional[DeviceState]:
        """Automatic device selection: online device covering all capabilities,
        preferring cloud/linux over android (battery-first)."""
        cands = list(self._devices.values())
        if exclude_offline:
            cands = [d for d in cands if d.online]
        cands = [d for d in cands if all(c in d.capabilities for c in capabilities)]
        if not cands:
            return None
        order = {DeviceKind.CLOUD: 0, DeviceKind.LINUX: 1, DeviceKind.ANDROID: 2}
        cands.sort(key=lambda d: (order.get(d.kind, 9), d.device_id))
        return cands[0]

    def _emit(self, type: str, device_id: str, payload: dict) -> None:
        if self._on_event:
            self._on_event(type, {"device_id": device_id, **payload})
