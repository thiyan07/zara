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
                  online: bool = True,
                  cpu_pct: float | None = None,
                  mem_pct: float | None = None,
                  low_power_mode: bool | None = None,
                  storage_free_mb: int | None = None,
                  storage_total_mb: int | None = None,
                  battery_temp_c: float | None = None,
                  thermal_state: str | None = None) -> DeviceState:
        with self._lock:
            d = self._devices[device_id]
            was_online = d.online
            d.online = online
            d.last_seen = utcnow()
            if online and d.status == "offline":
                # A graceful disconnect from a dying agent can land after
                # the successor's register (both run during an agent
                # restart) — heartbeats must then restore the presence
                # status or every approval re-check fails closed forever.
                # Observed live 2026-10-25: online=True, status=offline.
                d.status = "online"
                self._emit("device_online", device_id, {"status": "online"})
            if battery_pct is not None and battery_pct != d.battery_pct:
                d.battery_pct = battery_pct
                self._emit("battery_changed", device_id, {"battery_pct": battery_pct})
            if charging is not None:
                d.charging = charging
            if network is not None:
                # Normalize form (case/whitespace/length) without inventing
                # semantics: governor still matches exact known values.
                network = str(network).strip().lower()[:32] or "unknown"
                if network != d.network:
                    d.network = network
                    self._emit("network_changed", device_id, {"network": network})
            telemetry_seen = False
            # Second-layer clamp (defense in depth for non-HTTP writers):
            # mirrors HeartbeatPayload bounds so direct heartbeat() callers
            # get identical guarantees. Out-of-range values are dropped
            # (field stays at its old value), never stored, never crash.
            def _pct(v):
                try:
                    if isinstance(v, bool):
                        return None
                    f = float(v)
                    if f != f or f in (float("inf"), float("-inf")):
                        return None
                    return f if 0.0 <= f <= 100.0 else None
                except (TypeError, ValueError):
                    return None

            def _sint(v, cap=10**7):
                try:
                    if isinstance(v, bool):
                        return None
                    i = int(v)
                    return i if 0 <= i <= cap else None
                except (TypeError, ValueError):
                    return None

            def _temp(v):
                try:
                    if isinstance(v, bool):
                        return None
                    f = float(v)
                    if f != f or f in (float("inf"), float("-inf")):
                        return None
                    return f if -50.0 <= f <= 100.0 else None
                except (TypeError, ValueError):
                    return None

            _thermal = (thermal_state[:16]
                        if isinstance(thermal_state, str) else None)
            if _thermal is not None and _thermal not in (
                    "none", "light", "moderate", "severe", "critical",
                    "emergency", "shutdown", "unknown"):
                _thermal = None
            _free = _sint(storage_free_mb)
            _total = _sint(storage_total_mb)
            if _free is not None and _total is not None \
                    and _free > _total:
                # Impossible combination: unverifiable, drop both rather
                # than clamp (never invent a consistent pair).
                _free = _total = None
            for _field, _value in (
                    ("cpu_pct", _pct(cpu_pct)), ("mem_pct", _pct(mem_pct)),
                    ("low_power_mode", low_power_mode
                     if isinstance(low_power_mode, bool) else None),
                    ("storage_free_mb", _free),
                    ("storage_total_mb", _total),
                    ("battery_temp_c", _temp(battery_temp_c)),
                    ("thermal_state", _thermal)):
                if _value is not None:
                    setattr(d, _field, _value)
                    telemetry_seen = True
            if telemetry_seen:
                d.last_telemetry_at = utcnow()
            if online != was_online:
                self._emit("device_online" if online else "device_offline", device_id, {})
            return d

    def telemetry_stale(self, device_id: str,
                        threshold_s: float = 120.0) -> bool:
        """True when no telemetry-bearing heartbeat arrived within the
        threshold (or never). Stale telemetry stays readable but must be
        treated as unknown by readers — never as live, never as zero."""
        with self._lock:
            d = self._devices.get(device_id)
            if d is None or d.last_telemetry_at is None:
                return True
            try:
                age = (utcnow() - d.last_telemetry_at).total_seconds()
            except Exception:
                return True
            return age > threshold_s

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

    def snapshot(self, device_id: str) -> DeviceState:
        """Atomic copy-out for snapshot readers. Deep-copies under the
        manager lock — never the live registry object — so multi-field
        readers (views, snapshot builders) cannot observe torn
        half-heartbeats and iteration cannot race revoke. Writers already
        serialize on this lock; no new lock introduced. get()/list() are
        left untouched for backward compatibility."""
        with self._lock:
            return self._devices[device_id].model_copy(deep=True)

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
        # Canonical kind-device key (core.scheduler); identical order
        # (DeviceKind is a str Enum, so mapping is value-identical).
        from .scheduler import kind_device_key
        cands.sort(key=lambda d: kind_device_key(d.kind, d.device_id))
        return cands[0]

    def _emit(self, type: str, device_id: str, payload: dict) -> None:
        if self._on_event:
            self._on_event(type, {"device_id": device_id, **payload})
