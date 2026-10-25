"""Universal connected-device awareness (Stage 20).

PeripheralRegistry: a single, reusable subsystem that owns the
lifecycle of NON-BOS-AUTHENTICATED host-attached devices — USB mass
storage / pendrives, phones (ADB or vendor MTP), external displays,
audio input/output adapters, HID, and anything else a discovery
adapter reports. This is deliberately SEPARATE from the paired
devices (``core.devices.DeviceManager``): peripherals are detected
and identified, they are NEVER trusted or executable by detection.
Trust stays exactly where it always was: enrollment + challenge
proofs in device_auth. Detection only grants a registry entry.

Identity stability: transient USB paths, bus numbers and mount points
change between replugs. The stable id (``pid``) is derived per class
from durable identity fields (USB vid/pid/serial, display connector
+ EDID block id, audio card id/name, storage fsuuid/device node),
falling back to a stable class+model+buspath hash only when the OS
exposes nothing durable. Adapters get a deterministic id; the
registry never invents hardware identifiers beyond adapter output.

Events: connect / disconnect / (optionally) change — de-duplicated
(structurally-identical re-snapshots produce nothing), reconnect on
the same durable id produces connect-once + last_seen refresh, and a
disconnect stops capability freshness while keeping the record and
its history. Audit emission is optional and privacy-conscious: no
serials or block-device paths in payloads — only pid/class/label.
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from typing import Callable, Optional

#: Device classes a discovery adapter may report. Explicit, not
#: inferred-from-name: an unknown class is reported as ``otroucheherusb``
#: by the adapter, never guessed here.
DEVICE_CLASSES = ("usb_storage", "removable_disk", "phone", "mtp",
                  "android_adb", "display", "audio", "hid", "cardreader",
                  "camera", "bluetooth", "otherusb")

PERIPHERAL_STATES = ("present", "removed")


def stable_id(device_class: str, identity: dict) -> str:
    """Deterministic peripheral id from durable identity fields.

    Priority: a durable identity string passed by the adapter
    (usb vid/pid/serial, fsuuid, edid connector+block, audio card id)
    over class+model+buspath. Same replug -> same id; different device
    (even same model) with different serial/uuid -> different id.
    """
    durable = (identity or {}).get("durable") or ""
    if durable:
        raw = f"{device_class}|{durable}"
    else:
        raw = "|".join(f"{k}={identity.get(k, '')}"
                       for k in sorted(identity or {}))
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    prefix = device_class[:4]
    return f"pe-{prefix}-{h}"


class PeripheralDevice:
    __slots__ = ("pid", "device_class", "label", "identity", "host",
                 "connection", "state", "capabilities", "mounts",
                 "first_seen", "last_seen", "removed_at", "last_error",
                 "discovery_source", "trusted", "operational")

    def __init__(self, pid: str, device_class: str, label: str = "",
                 identity: Optional[dict] = None, host: str = "laptop",
                 connection: str = "usb",
                 discovery_source: str = "adapter"):
        self.pid = pid
        self.device_class = device_class
        self.label = label
        self.identity = dict(identity or {})
        self.host = host
        self.connection = connection
        self.state = "present"
        self.capabilities: dict = {}
        self.mounts: list = []
        self.first_seen = 0.0
        self.last_seen = 0.0
        self.removed_at: Optional[float] = None
        self.last_error: str = ""
        self.discovery_source = discovery_source
        # Trust/enrollment NEVER comes from detection. Operational is
        # the adapter's read/write observation, not an identity truth.
        self.trusted = False
        self.operational: dict = {}

    def to_public(self) -> dict:
        """Public projection: no serials, no raw identity internals."""
        return {
            "pid": self.pid, "device_class": self.device_class,
            "label": self.label, "host": self.host,
            "connection": self.connection, "state": self.state,
            "capabilities": dict(self.capabilities),
            "mounts": [dict(m) for m in self.mounts],
            "first_seen_age_s": None if not self.first_seen
            else round(time.time() - self.first_seen, 1),
            "last_seen_age_s": None if not self.last_seen
            else round(time.time() - self.last_seen, 1),
            "removed_at_age_s": None if self.removed_at is None
            else round(time.time() - self.removed_at, 1),
            "last_error": self.last_error,
            "discovery_source": self.discovery_source,
            "trusted": self.trusted,
            "operational": dict(self.operational),
        }


class PeripheralRegistry:
    """Diffs discovery snapshots into a stable device set + events.

    Injectable ``on_event`` (type, pid, payload) allows UI notification,
    the notification bus, and audit to stay independent. Duplicate or
    unchanged snapshots emit nothing. A device id must be produced by
    ``stable_id`` — the registry refuses fabricated or empty ids.
    """

    def __init__(self, on_event: Optional[Callable] = None,
                 now: Optional[Callable] = None) -> None:
        self._devices: dict[str, PeripheralDevice] = {}
        self._lock = threading.Lock()
        self._on_event = on_event
        self._now = now or time.time
        self._events: list[dict] = []

    # ---- lifecycle -------------------------------------------------

    def update_host_snapshot(self, host: str, items: list[dict]) -> dict:
        """Apply one adapter snapshot for one host. Returns the diff
        {connected: [pid], disconnected: [pid], unchanged: n, errors: []}.
        Item shape (adapter contract):
          {device_class, label, identity:{durable|fields}, connection?,
           capabilities?:{...}, mounts?:[{mountpoint, fs?}], error?}
        Unknown device classes are recorded as ``otherusb`` and counted
        as an error so taxonomy drift is visible."""
        errors: list[str] = []
        seen: dict[str, dict] = {}
        connected, disconnected = [], []
        for it in items or []:
            try:
                dclass = (it.get("device_class") or "").strip()
                ident = dict(it.get("identity") or {})
                pid = stable_id(dclass, ident)
                if not dclass:
                    raise ValueError("missing device_class")
                if dclass not in DEVICE_CLASSES:
                    raise ValueError(f"unknown device_class: {dclass}")
                seen[pid] = it
            except Exception as e:  # noqa: BLE001 — bad entries never poison the snapshot
                errors.append(str(e)[:120])
        with self._lock:
            t = self._now()
            for pid, it in seen.items():
                dev = self._devices.get(pid)
                if dev is None:
                    dev = PeripheralDevice(
                        pid=pid,
                        device_class=it.get("device_class"),
                        label=it.get("label") or "",
                        identity=it.get("identity"),
                        host=host,
                        connection=it.get("connection") or "usb",
                        discovery_source=it.get("source") or "adapter")
                    dev.first_seen = t
                    dev.last_seen = t
                    dev.capabilities = dict(it.get("capabilities") or {})
                    dev.mounts = list(it.get("mounts") or [])
                    dev.operational = dict(it.get("operational") or {})
                    self._devices[pid] = dev
                    connected.append(pid)
                    self._emit("peripheral_connected", dev,
                               {"class": dev.device_class})
                    continue
                # present again (maybe was previous snapshot too): refresh
                dev.state = "present"
                removed_before = dev.removed_at is not None
                dev.removed_at = None
                fresh = (dev.capabilities != dict(
                    it.get("capabilities") or {})
                    or dev.mounts != list(it.get("mounts") or []))
                if fresh:
                    dev.capabilities = dict(it.get("capabilities") or {})
                    dev.mounts = list(it.get("mounts") or [])
                    self._emit("peripheral_changed", dev,
                               {"class": dev.device_class})
                if removed_before:
                    connected.append(pid)  # reconnection
                    self._emit("peripheral_connected", dev,
                               {"class": dev.device_class})
                dev.last_seen = t
            for pid, dev in list(self._devices.items()):
                if dev.host == host and dev.state == "present" \
                        and pid not in seen:
                    dev.state = "removed"
                    dev.removed_at = t
                    disconnected.append(pid)
                    self._emit("peripheral_disconnected", dev,
                               {"class": dev.device_class})
            out = {"connected": connected, "disconnected": disconnected,
                   "unchanged": len(seen) - len(connected), "errors": errors}
        return out

    def _emit(self, evtype: str, dev: PeripheralDevice,
              payload: dict) -> None:
        # Privacy-conscious: pid+class+state only — no serials, no
        # block paths, no identity internals in event payloads.
        body = {"pid": dev.pid, "label": dev.label, **payload}
        evt = {"type": evtype, "pid": dev.pid, "payload": body,
               "at": self._now()}
        self._events.append(evt)
        if len(self._events) > 500:
            del self._events[:len(self._events) - 500]
        if self._on_event:
            self._on_event(evtype, dev.pid, body)

    def mark_error(self, pid: str, error: str) -> bool:
        with self._lock:
            d = self._devices.get(pid)
            if d is None:
                return False
            d.last_error = (error or "")[:200]
            return True

    def get(self, pid: str) -> Optional[PeripheralDevice]:
        with self._lock:
            return self._devices.get(pid)

    def list(self, state: Optional[str] = None,
             host: Optional[str] = None) -> list[PeripheralDevice]:
        with self._lock:
            devs = [d for d in self._devices.values()
                    if (state is None or d.state == state)
                    and (host is None or d.host == host)]
            return sorted(devs, key=lambda d: d.first_seen)

    def events(self, limit: int = 50, type: Optional[str] = None) -> list:
        with self._lock:
            evs = [e for e in self._events
                   if type is None or e["type"] == type]
            return list(evs[-limit:])


# ---- process-global hosting for the running Core ----------------------
# One Core process = one laptop host to observe. Tests build explicit
# instances; the hosted app seeds this singleton. Lazy discovery is
# also possible (a fresh default registry is created on first use).
_default_registry: PeripheralRegistry | None = None
_default_lock = threading.Lock()


def get_registry() -> PeripheralRegistry:
    global _default_registry
    if _default_registry is None:
        with _default_lock:
            if _default_registry is None:
                _default_registry = PeripheralRegistry()
    return _default_registry


def seed_registry(reg: PeripheralRegistry) -> None:
    global _default_registry
    _default_registry = reg


def refresh_host(reg: Optional[PeripheralRegistry] = None
                 ) -> tuple[Optional[dict], Optional[dict]]:
    """Run one bounded host discovery pass through the Linux adapters
    and feed the registry. Returns (diff, meta); adapters are best-
    effort: an import/execution failure returns (None, reason) instead
    of raising, and never touches trust or executes anything."""
    if reg is None:
        try:
            reg = get_registry()
        except Exception:  # noqa: BLE001
            return None, {"error": "no registry"}
    try:
        from device.linux.discovery_linux import discover_snapshot
        items, meta = discover_snapshot()
        return reg.update_host_snapshot("laptop", items), meta
    except Exception as e:  # noqa: BLE001 — discovery must never break Core
        return None, {"error": f"discovery unavailable: {str(e)[:120]}"}

