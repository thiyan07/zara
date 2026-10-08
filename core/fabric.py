"""Device Fabric — discovery/trust/capability/routing/execution layer.

Zara stops thinking of devices as hardcoded destinations. The Fabric is
an ADAPTER over the existing stores — it creates no parallel registry,
no second auth mechanism, no competing policy engine:

  devices      <- DeviceManager (presence, capabilities strings)
  device_auth  <- DeviceAuthStore (enroll/claim/verify/revoke = trust root)
  registry     <- ToolRegistry (tool schemas, risk, supported_devices)
  policy       <- PolicyEngine (sole authorization authority)
  governor     <- ResourceGovernor (sole resource authority)
  jobs/engine  <- execution + verification
  audit/bus    <- observable transitions

Core rule preserved end to end: capability existence != authorization.
A device advertising `camera.capture` means the capability EXISTS; use
requires trust + presence + policy + governor + OS permission, checked
at route/execute time. The LLM may propose; only this layer + Core
decide.

Trust is DERIVED, never self-declared:
  auth revoked/tombstoned            -> REVOKED
  operator restricted                -> RESTRICTED
  paired + live temporary grant      -> TEMPORARILY_TRUSTED
  paired (auth verified)             -> TRUSTED
  enrolled, pairing code outstanding -> PAIRING
  operator-discovered, never enrolled -> DISCOVERED
  otherwise                          -> UNKNOWN
"""
from __future__ import annotations
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field

from .models import RiskLevel, utcnow

# ---------- device types (extensible; never a support claim) ----------


class FabricDeviceType(str):
    LINUX = "linux"
    ANDROID = "android"
    CLOUD = "cloud"
    USB_STORAGE = "usb_storage"
    BLUETOOTH = "bluetooth"
    NETWORK_DEVICE = "network_device"
    CAMERA = "camera"
    AUDIO = "audio"
    PRINTER = "printer"
    DISPLAY = "display"
    UNKNOWN = "unknown"

    ALL = frozenset({"linux", "android", "cloud", "usb_storage",
                     "bluetooth", "network_device", "camera", "audio",
                     "printer", "display", "unknown"})


# ---------- trust ----------

class TrustState(str):
    UNKNOWN = "unknown"
    DISCOVERED = "discovered"
    PAIRING = "pairing"
    TRUSTED = "trusted"
    TEMPORARILY_TRUSTED = "temporarily_trusted"
    RESTRICTED = "restricted"
    REVOKED = "revoked"


TRUST_TRANSITIONS = {
    TrustState.UNKNOWN: {TrustState.DISCOVERED},
    TrustState.DISCOVERED: {TrustState.PAIRING, TrustState.REVOKED},
    TrustState.PAIRING: {TrustState.TRUSTED, TrustState.REVOKED},
    TrustState.TRUSTED: {TrustState.REVOKED, TrustState.RESTRICTED,
                         TrustState.TEMPORARILY_TRUSTED},
    TrustState.TEMPORARILY_TRUSTED: {TrustState.TRUSTED, TrustState.REVOKED,
                                     TrustState.RESTRICTED},
    TrustState.RESTRICTED: {TrustState.TRUSTED, TrustState.REVOKED},
    # REVOKED is terminal except via explicit re-pair (handled by
    # enroll/claim creating a fresh credential, not by transition).
    TrustState.REVOKED: frozenset(),
}

EXECUTABLE_TRUST = frozenset({TrustState.TRUSTED,
                              TrustState.TEMPORARILY_TRUSTED})


class TrustViolation(ValueError):
    """Illegal trust transition or execution attempted without trust."""


def check_trust_transition(frm: str, to: str) -> None:
    if to not in TRUST_TRANSITIONS.get(frm, frozenset()):
        raise TrustViolation(f"illegal trust transition {frm} -> {to}")


# ---------- presence ----------

class Presence(str):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    STALE = "stale"
    UNKNOWN = "unknown"


def derive_presence(status: str, online: bool, last_seen,
                    stale_s: float = 120.0) -> str:
    """Map DeviceState presence onto the Fabric model. A stale device is
    NEVER reported healthy just because it was trusted before."""
    if status == "degraded" and online:
        return Presence.DEGRADED
    if not online or status == "offline":
        return Presence.OFFLINE
    try:
        age = (utcnow() - last_seen).total_seconds()
    except Exception:  # noqa: BLE001 — unparseable timestamp => unknown
        return Presence.UNKNOWN
    if age > stale_s:
        return Presence.STALE
    return Presence.ONLINE


# ---------- capability records ----------

_CAP_ID = re.compile(r"^[a-z][a-z0-9_.:\-]{1,63}$")


class CapabilityRecord(BaseModel):
    """A device-advertised capability. Existence only — authorization is
    decided separately at route/execute time. All descriptive text is
    UNTRUSTED metadata (prompt-injection surface: never executed)."""
    capability_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=128)
    version: str = "1.0.0"
    description: str = ""
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    risk: RiskLevel = RiskLevel.SAFE
    required_permissions: list[str] = Field(default_factory=list)
    # available | unavailable | os_denied | unimplemented —
    # advertised-but-unusable is represented truthfully, never hidden,
    # never claimed as working.
    availability: str = "available"
    availability_reason: str = ""
    os_permission_granted: Optional[bool] = None
    constraints: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    # ---- Stage 16 dynamic descriptor fields (defaults = legacy record).
    # descriptor_version "0" means pre-descriptor/legacy; "1" means the
    # record passed strict descriptor validation.
    descriptor_version: str = "0"
    platforms: list[str] = Field(default_factory=list)
    execution: str = "on-device"
    requires_foreground: bool = False
    requires_network: bool = False
    battery_sensitive: bool = False
    supports_cancellation: bool = False
    supports_streaming: bool = False
    timeout_s: float = 30.0
    aliases: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    # ---- Core-attached snapshot (never device-supplied) ----
    advertised_at: str = ""
    source: str = ""
    snapshot_software: str = ""
    stale: bool = False

    def validate_record(self) -> None:
        if not _CAP_ID.match(self.capability_id):
            raise ValueError(f"bad capability_id: {self.capability_id!r}")
        if not isinstance(self.input_schema, dict) or \
                not isinstance(self.output_schema, dict):
            raise ValueError("capability schemas must be objects")
        if self.availability not in ("available", "unavailable",
                                     "os_denied", "unimplemented"):
            raise ValueError(f"bad availability: {self.availability!r}")
        if self.availability != "available" and \
                not self.availability_reason and \
                self.descriptor_version != "0":
            raise ValueError("unavailable descriptor needs a reason")

    def usable(self) -> bool:
        return self.availability == "available" and \
            self.os_permission_granted is not False


# ---------- temporary grants ----------

RISK_RANK = {"safe": 0, "confirm": 1, "high_risk": 2}


class TempGrant(BaseModel):
    grant_id: str = ""
    device_id: str = ""
    capabilities: list[str] = Field(default_factory=list)
    max_risk: str = "confirm"
    created_at: str = ""
    expires_at: str = ""
    issuer: str = "operator"
    status: str = "live"  # live|revoked|expired

    def live(self) -> bool:
        if self.status != "live":
            return False
        try:
            return self.expires_at >= utcnow().isoformat()
        except Exception:  # noqa: BLE001
            return False

    def covers(self, capability: str, risk: str) -> bool:
        if not self.live():
            return False
        if capability not in self.capabilities:
            return False
        return RISK_RANK.get(risk, 9) <= RISK_RANK.get(self.max_risk, -1)


# ---------- transfers (protocol foundation only) ----------

class TransferRecord(BaseModel):
    transfer_id: str = ""
    source_device: str = ""
    dest_device: str = ""
    capability: str = ""
    state: str = "proposed"  # proposed|authorized|running|verifying|
                             # succeeded|failed|cancelled|rejected|expired
    progress: float = 0.0
    verification: str = ""
    metadata: dict = Field(default_factory=dict)
    # ---- Stage 15 byte-transfer contract (defaults = signaling-only,
    # as in Stage 14; set when real bytes move through TransferEngine) ----
    filename: str = ""
    size_bytes: int = 0
    sha256: str = ""
    content_type: str = "application/octet-stream"
    grant_id: str = ""
    seq_expected: int = 0
    received_bytes: int = 0
    created_at: str = ""
    updated_at: str = ""
    expires_at: str = ""
    completed_at: str = ""
    recipient_verified: bool = False
    error: str = ""


TRANSFER_TRANSITIONS = {
    "proposed": {"authorized", "rejected", "cancelled", "expired"},
    "authorized": {"running", "cancelled", "expired"},
    "running": {"verifying", "failed", "cancelled", "expired"},
    "verifying": {"succeeded", "failed", "expired"},
    "succeeded": frozenset(), "failed": frozenset(),
    "cancelled": frozenset(), "rejected": frozenset(),
    "expired": frozenset(),
}


# ---------- device preferences (advisory only, never authority) ----------

class DevicePref(BaseModel):
    device_id: str = ""
    key: str = ""
    value: str = ""
    updated_at: str = ""


# ---------- composed views ----------

@dataclass
class FabricDeviceView:
    device_id: str
    device_type: str = FabricDeviceType.UNKNOWN
    display_name: str = ""
    transport: str = "unknown"
    trust: str = TrustState.UNKNOWN
    presence: str = Presence.UNKNOWN
    capabilities: list[str] = field(default_factory=list)
    battery_pct: Optional[float] = None
    charging: bool = False
    network: str = "unknown"
    last_seen: str = ""

    def as_dict(self) -> dict:
        return {"device_id": self.device_id, "device_type": self.device_type,
                "display_name": self.display_name or self.device_id,
                "transport": self.transport, "trust": self.trust,
                "presence": self.presence,
                "capabilities": list(self.capabilities),
                "battery_pct": self.battery_pct, "charging": self.charging,
                "network": self.network, "last_seen": self.last_seen}


@dataclass
class FabricRoute:
    device_id: Optional[str]
    action: str  # route|defer|deny
    reason: str
    substituted: bool = False
    checks: dict = field(default_factory=dict)


# ---------- persistent store (reuses Database; memory mode for tests) ----------

FABRIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS fabric_devices (
  device_id TEXT PRIMARY KEY, display_name TEXT DEFAULT '',
  device_type TEXT DEFAULT 'unknown', transport TEXT DEFAULT 'unknown',
  restricted INTEGER DEFAULT 0, metadata TEXT DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fabric_capabilities (
  device_id TEXT NOT NULL, capability_id TEXT NOT NULL,
  record_json TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (device_id, capability_id)
);
CREATE TABLE IF NOT EXISTS fabric_grants (
  grant_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
  capabilities TEXT NOT NULL, max_risk TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  issuer TEXT DEFAULT 'operator', status TEXT DEFAULT 'live'
);
CREATE TABLE IF NOT EXISTS fabric_prefs (
  device_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY (device_id, key)
);
CREATE TABLE IF NOT EXISTS fabric_revocations (
  device_id TEXT PRIMARY KEY, revoked_at TEXT NOT NULL, reason TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS fabric_transfers (
  transfer_id TEXT PRIMARY KEY, record_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class FabricStore:
    """SQLite-backed fabric persistence via the shared Database, or pure
    memory when path is empty (tests / ephemeral dev servers)."""

    def __init__(self, db=None) -> None:
        self._db = db
        self._lock = threading.Lock()
        if self._db is not None:
            with self._lock:
                self._db._db.executescript(FABRIC_SCHEMA)

    @property
    def persistent(self) -> bool:
        return self._db is not None

    # -- devices --

    def save_device(self, device_id: str, display_name: str,
                    device_type: str, transport: str, restricted: bool,
                    metadata: dict) -> None:
        import json as _json
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO fabric_devices (device_id, display_name, "
            "device_type, transport, restricted, metadata, updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
            "display_name=excluded.display_name, "
            "device_type=excluded.device_type, "
            "transport=excluded.transport, restricted=excluded.restricted, "
            "metadata=excluded.metadata, updated_at=excluded.updated_at",
            (device_id, display_name, device_type, transport,
             1 if restricted else 0, _json.dumps(metadata),
             utcnow().isoformat()))

    def load_devices(self) -> list[dict]:
        if self._db is None:
            return []
        rows = self._db.execute(
            "SELECT device_id, display_name, device_type, transport, "
            "restricted, metadata FROM fabric_devices")
        import json as _json
        out = []
        for r in rows:
            try:
                meta = _json.loads(r[5] or "{}")
            except Exception:  # noqa: BLE001 — corrupt row never fatal
                meta = {}
            out.append({"device_id": r[0], "display_name": r[1],
                        "device_type": r[2], "transport": r[3],
                        "restricted": bool(r[4]), "metadata": meta})
        return out

    # -- revocations (tombstones: REVOKED survives restart) --

    def save_revocation(self, device_id: str, reason: str = "") -> None:
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO fabric_revocations (device_id, revoked_at, reason) "
            "VALUES (?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
            "revoked_at=excluded.revoked_at, reason=excluded.reason",
            (device_id, utcnow().isoformat(), reason[:300]))

    def load_revocations(self) -> list[str]:
        if self._db is None:
            return []
        return [r[0] for r in
                self._db.execute("SELECT device_id FROM fabric_revocations")]

    def clear_revocation(self, device_id: str) -> None:
        if self._db is None:
            return
        self._db.execute("DELETE FROM fabric_revocations WHERE device_id=?",
                         (device_id,))

    # -- capabilities --

    def save_capability(self, rec: CapabilityRecord) -> None:
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO fabric_capabilities (device_id, capability_id, "
            "record_json, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(device_id, capability_id) DO UPDATE SET "
            "record_json=excluded.record_json, "
            "updated_at=excluded.updated_at",
            (rec.device_id, rec.capability_id, rec.model_dump_json(),
             utcnow().isoformat()))

    def load_capabilities(self) -> list[dict]:
        if self._db is None:
            return []
        import json as _json
        out = []
        for r in self._db.execute(
                "SELECT record_json FROM fabric_capabilities"):
            try:
                out.append(_json.loads(r[0]))
            except Exception:  # noqa: BLE001
                continue
        return out

    def delete_capabilities(self, device_id: str) -> None:
        if self._db is None:
            return
        self._db.execute("DELETE FROM fabric_capabilities WHERE device_id=?",
                         (device_id,))

    # -- grants --

    def save_grant(self, g: TempGrant) -> None:
        import json as _json
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO fabric_grants (grant_id, device_id, capabilities, "
            "max_risk, created_at, expires_at, issuer, status) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(grant_id) DO UPDATE SET status=excluded.status",
            (g.grant_id, g.device_id, _json.dumps(g.capabilities),
             g.max_risk, g.created_at, g.expires_at, g.issuer, g.status))

    def load_grants(self) -> list[dict]:
        if self._db is None:
            return []
        import json as _json
        out = []
        for r in self._db.execute(
                "SELECT grant_id, device_id, capabilities, max_risk, "
                "created_at, expires_at, issuer, status FROM fabric_grants"):
            try:
                caps = _json.loads(r[2] or "[]")
            except Exception:  # noqa: BLE001
                caps = []
            out.append({"grant_id": r[0], "device_id": r[1],
                        "capabilities": caps, "max_risk": r[2 + 1],
                        "created_at": r[4], "expires_at": r[5],
                        "issuer": r[6], "status": r[7]})
        return out

    # -- prefs --

    def save_pref(self, device_id: str, key: str, value: str) -> None:
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO fabric_prefs (device_id, key, value, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(device_id, key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (device_id, key, value[:2000], utcnow().isoformat()))

    def load_prefs(self, device_id: str) -> dict:
        if self._db is None:
            return {}
        return {r[0]: r[1] for r in self._db.execute(
            "SELECT key, value FROM fabric_prefs WHERE device_id=?",
            (device_id,))}

    # -- byte transfers (Stage 15; record JSON keyed by transfer_id) --

    def save_transfer(self, record_json: str, transfer_id: str) -> None:
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO fabric_transfers (transfer_id, record_json, "
            "updated_at) VALUES (?,?,?) "
            "ON CONFLICT(transfer_id) DO UPDATE SET "
            "record_json=excluded.record_json, "
            "updated_at=excluded.updated_at",
            (transfer_id, record_json, utcnow().isoformat()))

    def load_transfers(self) -> list[dict]:
        if self._db is None:
            return []
        import json as _json
        out = []
        for r in self._db.execute(
                "SELECT record_json FROM fabric_transfers"):
            try:
                out.append(_json.loads(r[0]))
            except Exception:  # noqa: BLE001 — corrupt row never fatal
                continue
        return out

    def delete_transfer(self, transfer_id: str) -> None:
        if self._db is None:
            return
        self._db.execute("DELETE FROM fabric_transfers WHERE transfer_id=?",
                         (transfer_id,))


# ---------- registry (adapter; owns no device truth of its own) ----------

_KIND_ORDER = {"cloud": 0, "linux": 1, "android": 2}
_RISK_COST = {"safe": 0.1, "confirm": 0.3, "high_risk": 0.5}


def _kind_of(device_type: str) -> str:
    return device_type if device_type in ("cloud", "linux", "android") \
        else "unknown"


class FabricRegistry:
    """Deterministic Device Fabric over existing stores.

    Owns: operator metadata (display/type/transport/restricted),
    capability RECORDS, temp grants, transfers, prefs, revocation
    tombstones. Derives: trust (from auth), presence (from manager).
    Never executes tools itself — delegates to ExecutionEngine after
    the full check chain."""

    def __init__(self, devices, device_auth, registry, policy, governor,
                 jobs, engine, audit, bus, store: Optional[FabricStore] = None,
                 stale_s: float = 120.0) -> None:
        self.devices = devices
        self.auth = device_auth
        self.registry = registry
        self.policy = policy
        self.governor = governor
        self.jobs = jobs
        self.engine = engine
        self.audit = audit
        self.bus = bus
        self.store = store or FabricStore()
        self.stale_s = stale_s
        self._lock = threading.Lock()
        self._meta: dict[str, dict] = {}   # device_id -> display/type/...
        self._caps: dict[str, dict[str, CapabilityRecord]] = {}
        self._grants: dict[str, TempGrant] = {}
        self._transfers: dict[str, TransferRecord] = {}
        self._prefs: dict[str, dict[str, str]] = {}
        self._tombstones: set[str] = set()
        self._rehydrate()

    # ----- persistence -----

    def _rehydrate(self) -> None:
        for row in self.store.load_devices():
            self._meta[row["device_id"]] = {
                "display_name": row["display_name"],
                "device_type": row["device_type"], "transport": row["transport"],
                "restricted": row["restricted"],
                "metadata": row["metadata"]}
        for rec in self.store.load_capabilities():
            try:
                r = CapabilityRecord(**rec)
                self._caps.setdefault(r.device_id, {})[r.capability_id] = r
            except Exception:  # noqa: BLE001 — skip corrupt rows
                continue
        for g in self.store.load_grants():
            try:
                self._grants[g["grant_id"]] = TempGrant(**g)
            except Exception:  # noqa: BLE001
                continue
        for device_id in self.store.load_revocations():
            self._tombstones.add(device_id)
            try:
                self.auth.revoke(device_id)
            except Exception:  # noqa: BLE001 — no cred yet; tombstone covers
                pass

    def _persist_meta(self, device_id: str) -> None:
        m = self._meta.get(device_id, {})
        self.store.save_device(
            device_id, m.get("display_name", device_id),
            m.get("device_type", FabricDeviceType.UNKNOWN),
            m.get("transport", "unknown"),
            bool(m.get("restricted", False)), m.get("metadata", {}))

    # ----- trust -----

    def _auth_state(self, device_id: str) -> str:
        """Raw auth facts: revoked|paired|pending|absent (never trust)."""
        creds = getattr(self.auth, "_creds", {})
        cred = creds.get(device_id)
        if cred is None:
            return "absent"
        if getattr(cred, "revoked", False):
            return "revoked"
        if getattr(cred, "paired", False):
            return "paired"
        try:
            exp = getattr(cred, "expires_at", "") or ""
            if exp and exp < utcnow().isoformat():
                return "absent"
        except Exception:  # noqa: BLE001
            pass
        return "pending"

    def trust_of(self, device_id: str) -> str:
        if device_id in self._tombstones:
            return TrustState.REVOKED
        auth = self._auth_state(device_id)
        if auth == "revoked":
            return TrustState.REVOKED
        meta = self._meta.get(device_id, {})
        if meta.get("restricted"):
            # Restricted wins over grants: explicit operator limit.
            if auth == "paired":
                return TrustState.RESTRICTED
            return TrustState.REVOKED if auth == "revoked" \
                else TrustState.RESTRICTED
        if auth == "paired":
            if self._live_grant_for(device_id) is not None:
                return TrustState.TEMPORARILY_TRUSTED
            return TrustState.TRUSTED
        if auth == "pending":
            return TrustState.PAIRING
        if device_id in self._meta:
            return TrustState.DISCOVERED
        return TrustState.UNKNOWN

    def _live_grant_for(self, device_id: str) -> Optional[TempGrant]:
        for g in self._grants.values():
            if g.device_id == device_id and g.live():
                return g
        return None

    # ----- discovery / registration -----

    @staticmethod
    def _clean_meta(text: str, limit: int = 120) -> str:
        # Display names/descriptions are UNTRUSTED: keep printable text,
        # bounded; never executed, never trusted as identity.
        import re as _re
        t = _re.sub(r"[^ -~]", "", text or "")
        return t[:limit]

    def discover(self, device_id: str, device_type: str = "unknown",
                 display_name: str = "", transport: str = "unknown",
                 metadata: dict | None = None) -> dict:
        if not device_id or len(device_id) > 128:
            raise ValueError("bad device_id")
        if device_type not in FabricDeviceType.ALL:
            raise ValueError(f"unknown device type: {device_type}")
        meta = self._meta.get(device_id, {})
        if not meta:
            meta = {"display_name": self._clean_meta(display_name) or
                    device_id, "device_type": device_type,
                    "transport": self._clean_meta(transport, 40),
                    "restricted": False,
                    "metadata": dict(metadata or {})}
            self._meta[device_id] = meta
            self._persist_meta(device_id)
            self._audit("device_discovered", device_id, device_type)
        else:
            # Re-discovery refreshes operator metadata but NEVER identity
            # or trust: a rename keeps the same device_id and trust state.
            if display_name:
                meta["display_name"] = self._clean_meta(display_name) or \
                    device_id
            if metadata:
                meta["metadata"] = dict(metadata)
            self._persist_meta(device_id)
        return {"device_id": device_id, "trust": self.trust_of(device_id)}

    def set_restricted(self, device_id: str, restricted: bool = True) -> str:
        trust = self.trust_of(device_id)
        if restricted:
            if trust not in (TrustState.TRUSTED,
                             TrustState.TEMPORARILY_TRUSTED,
                             TrustState.RESTRICTED):
                raise TrustViolation(
                    f"cannot restrict {device_id} from {trust}")
            meta = self._meta.setdefault(device_id, {})
            meta["restricted"] = True
            self._persist_meta(device_id)
            self._audit("device_restricted", device_id, trust)
            return TrustState.RESTRICTED
        meta = self._meta.get(device_id, {})
        meta["restricted"] = False
        self._persist_meta(device_id)
        self._audit("device_restricted", device_id, "cleared")
        return self.trust_of(device_id)

    def revoke_device(self, device_id: str, reason: str = "") -> dict:
        try:
            self.auth.revoke(device_id)
        except Exception:  # noqa: BLE001 — tombstone still applies
            pass
        self._tombstones.add(device_id)
        self.store.save_revocation(device_id, reason)
        try:
            self.devices.revoke(device_id)
        except Exception:  # noqa: BLE001 — unknown to manager is fine
            pass
        self.store.delete_capabilities(device_id)
        self._caps.pop(device_id, None)
        for g in self._grants.values():
            if g.device_id == device_id and g.status == "live":
                g.status = "revoked"
                self.store.save_grant(g)
        self._audit("device_revoked", device_id, reason[:200])
        return {"device_id": device_id, "trust": TrustState.REVOKED}

    def clear_revocation(self, device_id: str) -> None:
        """Explicit re-enrollment path: operator clears the tombstone so a
        FRESH enroll/claim can proceed. Never restores old credentials."""
        self._tombstones.discard(device_id)
        self.store.clear_revocation(device_id)

    # ----- capabilities -----

    def advertise(self, records: list[dict], device_id: str = "",
                    source: str = "describe") -> dict:
        """Device-advertised capability descriptors (device-auth endpoint).
        Strict validation (Stage 16): bounded, versioned, no authority
        fields, no schema bombs. Identity comes ONLY from the device_id
        parameter (forced from auth by the endpoint) — documents carrying
        their own device_id are rejected, so spoofing is structural, not
        just overwritten. Accepted docs become the device's FULL snapshot
        (refresh semantics): added/removed/changed are diffed and audited;
        absent records are dropped. Unknown names are never executable by
        themselves."""
        from .capabilities import (descriptor_fingerprint,
                                   validate_describe_batch)
        if not isinstance(records, list):
            return {"accepted": [], "rejected": [{
                "record": str(records)[:120],
                "error": "records must be a list"}]}
        if not device_id:
            return {"accepted": [], "rejected": [{
                "record": "batch", "error": "missing device identity"}]}
        try:
            descs, rejected = validate_describe_batch(records)
        except Exception as e:  # noqa: BLE001 — batch-level refusal
            return {"accepted": [], "rejected": [{
                "record": f"{len(records)} records",
                "error": str(e)[:300]}]}
        if not descs:
            # Nothing valid: keep the old snapshot (never destroy state
            # on garbage), report what failed.
            return {"accepted": [], "rejected": rejected}
        prev = self._caps.get(device_id, {})
        prev_fp = {cid: self._record_fingerprint(r)
                   for cid, r in prev.items()}
        new_fp = {d.id: descriptor_fingerprint(d) for d in descs}
        old_ids, new_ids = set(prev_fp), set(new_fp)
        for cid in sorted(new_ids - old_ids):
            self._audit("capability_added", device_id, cid)
        for cid in sorted(old_ids - new_ids):
            self._audit("capability_removed", device_id, cid)
        for cid in sorted(old_ids & new_ids):
            if prev_fp[cid] != new_fp[cid]:
                self._audit("capability_changed", device_id, cid)
        try:
            sw = self.devices.get(device_id).software_version
        except Exception:  # noqa: BLE001 — unknown to manager
            sw = ""
        now = utcnow().isoformat()
        fresh: dict[str, CapabilityRecord] = {}
        accepted: list[str] = []
        for d in descs:
            fresh[d.id] = CapabilityRecord(
                capability_id=d.id, name=d.id, device_id=device_id,
                version=d.version, description=d.description[:500],
                input_schema=d.input_schema,
                output_schema=d.output_schema, risk=d.risk,
                required_permissions=list(d.requires),
                availability=d.availability,
                availability_reason=d.availability_reason,
                os_permission_granted=d.os_permission_granted,
                descriptor_version=d.descriptor_version,
                platforms=list(d.platforms), execution=d.execution,
                requires_foreground=d.requires_foreground,
                requires_network=d.requires_network,
                battery_sensitive=d.battery_sensitive,
                supports_cancellation=d.supports_cancellation,
                supports_streaming=d.supports_streaming,
                timeout_s=d.timeout_s, aliases=list(d.aliases),
                examples=list(d.examples), keywords=list(d.keywords),
                advertised_at=now, source=source[:32],
                snapshot_software=sw[:64], stale=False)
            accepted.append(d.id)
        self._caps[device_id] = fresh
        self._rewrite_capabilities(device_id, fresh)
        return {"accepted": accepted, "rejected": rejected}

    def _rewrite_capabilities(self, device_id: str,
                              fresh: dict) -> None:
        """Persist full-replace snapshot (refresh semantics)."""
        self.store.delete_capabilities(device_id)
        for rec in fresh.values():
            self.store.save_capability(rec)

    @staticmethod
    def _record_fingerprint(rec: CapabilityRecord) -> str:
        from .capabilities import descriptor_fingerprint
        try:
            from .capabilities import CapabilityDescriptor
            d = CapabilityDescriptor(
                id=rec.capability_id, name=rec.name, version=rec.version,
                description=rec.description, risk=rec.risk,
                requires=list(rec.required_permissions),
                platforms=list(rec.platforms) or ["any"],
                execution=rec.execution or "on-device",
                requires_foreground=rec.requires_foreground,
                requires_network=rec.requires_network,
                battery_sensitive=rec.battery_sensitive,
                supports_cancellation=rec.supports_cancellation,
                supports_streaming=rec.supports_streaming,
                timeout_s=rec.timeout_s, input_schema=rec.input_schema,
                output_schema=rec.output_schema,
                availability=rec.availability,
                availability_reason=rec.availability_reason,
                aliases=list(rec.aliases), keywords=list(rec.keywords))
            return descriptor_fingerprint(d)
        except Exception:  # noqa: BLE001 — legacy oddity: raw hash
            import hashlib as _hl
            return _hl.sha256(
                rec.model_dump_json().encode()).hexdigest()[:16]

    def mark_stale(self, device_id: str) -> None:
        """Registration without a fresh describe leaves the previous
        snapshot usable-but-stale: routing still works, resolve reports
        staleness honestly. Cleared by the next describe."""
        for rec in self._caps.get(device_id, {}).values():
            rec.stale = True
            try:
                self.store.save_capability(rec)
            except Exception:  # noqa: BLE001 — memory mode
                pass
        self._audit("capability_stale", device_id,
                    "re-registered without fresh describe")

    def capabilities_for(self, device_id: str) -> list[CapabilityRecord]:
        out = []
        try:
            d = self.devices.get(device_id)
            names = set(d.capabilities or [])
        except Exception:  # noqa: BLE001 — unknown to manager
            names = set()
        for rec in self._caps.get(device_id, {}).values():
            out.append(rec)
            names.discard(rec.name)
        # String-only advertisements (existing protocol) surface as
        # unmapped records: visible, never directly executable.
        for name in sorted(names):
            out.append(CapabilityRecord(
                capability_id=f"legacy:{name}", name=name,
                device_id=device_id, description="legacy string advertisement",
                availability="available"))
        return out

    def remove_capabilities(self, device_id: str) -> None:
        self._caps.pop(device_id, None)
        self.store.delete_capabilities(device_id)
        self._audit("capability_changed", device_id, "removed")

    # ----- presence -----

    def presence_of(self, device_id: str) -> str:
        try:
            d = self.devices.get(device_id)
        except Exception:  # noqa: BLE001
            return Presence.UNKNOWN
        return derive_presence(d.status, d.online, d.last_seen, self.stale_s)

    # ----- views -----

    def device_view(self, device_id: str) -> FabricDeviceView:
        meta = self._meta.get(device_id, {})
        try:
            d = self.devices.get(device_id)
            caps = sorted({r.name for r in self.capabilities_for(device_id)})
            return FabricDeviceView(
                device_id=device_id,
                device_type=meta.get("device_type",
                                    self._kind_from_manager(d.kind)),
                display_name=meta.get("display_name", device_id),
                transport=meta.get("transport", "unknown"),
                trust=self.trust_of(device_id),
                presence=self.presence_of(device_id),
                capabilities=caps, battery_pct=d.battery_pct,
                charging=d.charging, network=d.network,
                last_seen=d.last_seen.isoformat()
                if hasattr(d.last_seen, "isoformat") else str(d.last_seen))
        except Exception:  # noqa: BLE001 — known to fabric, not manager
            return FabricDeviceView(
                device_id=device_id,
                device_type=meta.get("device_type",
                                    FabricDeviceType.UNKNOWN),
                display_name=meta.get("display_name", device_id),
                transport=meta.get("transport", "unknown"),
                trust=self.trust_of(device_id),
                presence=Presence.UNKNOWN)

    @staticmethod
    def _kind_from_manager(kind) -> str:
        v = getattr(kind, "value", kind)
        return v if v in FabricDeviceType.ALL else FabricDeviceType.UNKNOWN

    def list_devices(self) -> list[FabricDeviceView]:
        ids = set(self._meta) | {d.device_id
                                 for d in self.devices.list()}
        return [self.device_view(i) for i in sorted(ids)]

    # ----- authorization + routing -----

    def _snapshot_for(self, device_id: str):
        from .governor import ResourceSnapshot
        try:
            d = self.devices.get(device_id)
            return ResourceSnapshot(
                battery_pct=d.battery_pct, charging=d.charging,
                network=d.network,
                device_kind=_kind_of(self.device_view(device_id).device_type))
        except Exception:  # noqa: BLE001
            from .governor import ResourceSnapshot
            return ResourceSnapshot()

    def authorize_capability(self, capability: str, device_id: str,
                             who: str = "user") -> dict:
        """Existence vs authorization, decided deterministically. Returns
        {authorized, reason, checks}. Never executes."""
        checks: dict = {}
        rec = next((r for r in self.capabilities_for(device_id)
                    if r.name == capability or
                    r.capability_id == capability), None)
        checks["exists"] = rec is not None
        if rec is None:
            return {"authorized": False, "reason": "unknown capability",
                    "checks": checks}
        trust = self.trust_of(device_id)
        checks["trust"] = trust
        if trust not in EXECUTABLE_TRUST:
            if trust == TrustState.RESTRICTED:
                grant = next(
                    (g for g in self._grants.values()
                     if g.device_id == device_id and
                     g.covers(rec.name, rec.risk.value)), None)
                checks["temp_grant"] = grant.grant_id if grant else None
                if grant is None:
                    return {"authorized": False,
                            "reason": "restricted device without live grant",
                            "checks": checks}
            else:
                return {"authorized": False,
                        "reason": f"device trust is {trust}, not executable",
                        "checks": checks}
        presence = self.presence_of(device_id)
        checks["presence"] = presence
        if presence in (Presence.OFFLINE, Presence.UNKNOWN, Presence.STALE):
            return {"authorized": False,
                    "reason": f"device {presence}; stale is never healthy",
                    "checks": checks}
        checks["os_usable"] = rec.usable()
        if not rec.usable():
            return {"authorized": False,
                    "reason": "capability exists but OS permission denied "
                              "or unimplemented (not usable)",
                    "checks": checks}
        pol = self.policy.decide(who, capability=f"capability:{rec.name}",
                                 device_id=device_id, resource=rec.name,
                                 risk=rec.risk)
        checks["policy"] = {"allow": pol.allow,
                            "approval": pol.requires_approval,
                            "hard_deny": pol.hard_deny}
        if not pol.allow and (pol.hard_deny or not pol.requires_approval):
            return {"authorized": False,
                    "reason": "policy refusal" if pol.hard_deny
                    else "policy hold: approval required", "checks": checks,
                    "approval_required": pol.requires_approval,
                    "hard_deny": pol.hard_deny}
        gate = self.governor.check(
            self._snapshot_for(device_id),
            _RISK_COST.get(rec.risk.value, 0.3), rec.risk.value)
        checks["governor"] = gate.action
        if gate.action == "defer":
            return {"authorized": False,
                    "reason": f"governor defer: {gate.reason}",
                    "checks": checks}
        return {"authorized": True,
                "reason": f"authorized on {device_id}"
                          + (" (approval still required)"
                             if pol.requires_approval else ""),
                "checks": checks,
                "approval_required": pol.requires_approval}

    def route_capability(self, capability: str, who: str = "user",
                         device_id: Optional[str] = None) -> FabricRoute:
        """Deterministic capability routing. A pinned device_id is honored:
        safe risks may substitute on non-policy failures; riskier ones
        never substitute silently."""
        recs: list[CapabilityRecord] = []
        for view in self.list_devices():
            for r in self.capabilities_for(view.device_id):
                if r.name == capability or r.capability_id == capability:
                    recs.append(r)
        if not recs:
            return FabricRoute(None, "deny",
                               f"no device advertises {capability}", False,
                               {"exists": False})
        if device_id is not None:
            recs = [r for r in recs if r.device_id == device_id]
            if not recs:
                return FabricRoute(None, "deny",
                                   f"{device_id} does not advertise "
                                   f"{capability}", False, {"exists": False})
        scored = []
        for r in recs:
            authz = self.authorize_capability(r.name, r.device_id, who)
            if authz["authorized"]:
                view = self.device_view(r.device_id)
                scored.append((self._rank(view), r, authz))
        if scored:
            scored.sort(key=lambda t: (t[0], t[1].device_id))
            _, rec, authz = scored[0]
            sub = device_id is not None and rec.device_id != device_id
            return FabricRoute(
                rec.device_id, "route",
                f"selected {rec.device_id}"
                + (" (substituted, safe risk)" if sub else "")
                + f": {authz['reason']}", sub,
                {"capability": rec.name, "risk": rec.risk.value})
        # Nobody authorized: report the most informative failure. If a
        # pinned device failed only on presence/governor (not policy) and
        # the risk is safe, allow substitution to a healthy device.
        details = [self.authorize_capability(r.name, r.device_id, who)
                   for r in recs]
        hard = any(d.get("hard_deny") for d in details)
        if hard:
            return FabricRoute(None, "deny",
                               "policy hard-deny; not approvable", False,
                               {"details": [d["reason"] for d in details]})
        if device_id is not None:
            only = details[0]
            rec = recs[0]
            # Safe substitution: the pinned device failed on presence /
            # governor / OS-usability (not policy). Policy-blocked or
            # approval-gated pins never substitute silently.
            if rec.risk.value == "safe" and \
                    not only.get("hard_deny", False) and \
                    not only.get("approval_required", False):
                alts = [r for r in self._all_records(capability)
                        if r.device_id != device_id]
                for alt in sorted(alts, key=lambda r: r.device_id):
                    a2 = self.authorize_capability(alt.name, alt.device_id,
                                                   who)
                    if a2["authorized"]:
                        return FabricRoute(
                            alt.device_id, "route",
                            f"substituted {alt.device_id} (safe risk; "
                            f"{device_id}: {only['reason']})", True,
                            {"capability": alt.name})
            return FabricRoute(None,
                               "defer" if "governor" in only["reason"] or
                               "stale" in only["reason"] or
                               "OFFLINE" in only["reason"].upper()
                               else "deny",
                               f"{device_id}: {only['reason']}", False,
                               {"details": [only["reason"]]})
        gov = any("governor" in d["reason"] or "stale" in d["reason"] or
                  "OFFLINE" in d["reason"].upper() for d in details)
        return FabricRoute(None, "defer" if gov else "deny",
                           "; ".join(d["reason"] for d in details), False,
                           {"details": [d["reason"] for d in details]})

    def _all_records(self, capability: str) -> list[CapabilityRecord]:
        out = []
        for view in self.list_devices():
            for r in self.capabilities_for(view.device_id):
                if r.name == capability or r.capability_id == capability:
                    out.append(r)
        return out

    @staticmethod
    def _rank(view: FabricDeviceView) -> tuple:
        batt = view.battery_pct if view.battery_pct is not None else -1.0
        return (_KIND_ORDER.get(view.device_type, 9),
                0 if view.presence == "online" else 1, -batt)

    # ----- temporary grants -----

    def create_grant(self, device_id: str, capabilities: list[str],
                     max_risk: str = "confirm", ttl_s: int = 1800,
                     issuer: str = "operator") -> TempGrant:
        if self.trust_of(device_id) not in (
                TrustState.TRUSTED, TrustState.RESTRICTED,
                TrustState.TEMPORARILY_TRUSTED):
            raise TrustViolation(
                f"grants require a paired device, {device_id} is "
                f"{self.trust_of(device_id)}")
        if max_risk not in RISK_RANK:
            raise ValueError(f"bad max_risk: {max_risk}")
        if not capabilities or len(capabilities) > 20:
            raise ValueError("grant needs 1..20 capabilities")
        now = utcnow()
        from datetime import timedelta
        g = TempGrant(
            grant_id=f"tmp-{uuid.uuid4().hex[:12]}", device_id=device_id,
            capabilities=[c[:64] for c in capabilities], max_risk=max_risk,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=min(ttl_s, 86400))).isoformat(),
            issuer=issuer[:64])
        self._grants[g.grant_id] = g
        self.store.save_grant(g)
        self._audit("temporary_grant_created", device_id, g.grant_id)
        return g

    def revoke_grant(self, grant_id: str) -> bool:
        g = self._grants.get(grant_id)
        if g is None or g.status != "live":
            return False
        g.status = "revoked"
        self.store.save_grant(g)
        self._audit("temporary_grant_expired", g.device_id, grant_id)
        return True

    def list_grants(self, device_id: str = "") -> list[TempGrant]:
        out = [g for g in self._grants.values()
               if not device_id or g.device_id == device_id]
        return sorted(out, key=lambda g: g.grant_id)

    # ----- transfers (foundation) -----

    def create_transfer(self, source: str, dest: str,
                        capability: str, metadata: dict | None = None) -> TransferRecord:
        for d in (source, dest):
            if self.trust_of(d) not in EXECUTABLE_TRUST:
                raise TrustViolation(
                    f"transfer endpoint {d} is {self.trust_of(d)}")
        t = TransferRecord(
            transfer_id=f"xfer-{uuid.uuid4().hex[:12]}",
            source_device=source, dest_device=dest, capability=capability,
            metadata={k[:64]: str(v)[:300]
                      for k, v in (metadata or {}).items()})
        self._transfers[t.transfer_id] = t
        self._audit("transfer_proposed", source, f"{source}->{dest}")
        return t

    def transition_transfer(self, transfer_id: str, to: str,
                            verification: str = "") -> TransferRecord:
        t = self._transfers.get(transfer_id)
        if t is None:
            raise KeyError(f"unknown transfer: {transfer_id}")
        if to not in TRANSFER_TRANSITIONS.get(t.state, frozenset()):
            raise TrustViolation(
                f"illegal transfer transition {t.state} -> {to}")
        t.state = to
        if verification:
            t.verification = verification[:500]
        return t

    # ----- preferences (advisory) -----

    def set_pref(self, device_id: str, key: str, value: str) -> None:
        if self.trust_of(device_id) == TrustState.UNKNOWN:
            raise TrustViolation("prefs require a known device")
        self._prefs.setdefault(device_id, {})[key[:64]] = value[:2000]
        self.store.save_pref(device_id, key[:64], value[:2000])

    def get_prefs(self, device_id: str) -> dict:
        prefs = dict(self._prefs.get(device_id, {}))
        prefs.update(self.store.load_prefs(device_id))
        return prefs

    # ----- execution -----

    def execute_on_device(self, tool: str, inputs: dict, device_id: str,
                          who: str = "user",
                          mission_id: str = "") -> dict:
        """Full deterministic pipeline: capability -> identity -> trust ->
        presence -> permission -> risk/policy -> governor -> execute ->
        verify -> audit. Returns stage + record summary, never fake success.
        Approval-gated tools return the hold (no bypass possible here)."""
        try:
            definition = self.registry.get(tool)
        except KeyError:
            return {"stage": "rejected", "reason": f"unknown tool: {tool}"}
        # Identity gate FIRST: revoked/unknown devices never reach policy
        # or the engine, no matter what the tool is.
        trust = self.trust_of(device_id)
        if trust not in EXECUTABLE_TRUST:
            if trust == TrustState.RESTRICTED and self._grant_covers_tool(
                    device_id, definition):
                pass
            else:
                self._audit("execution_rejected", device_id,
                            f"{tool}: trust is {trust}")
                return {"stage": "rejected",
                        "reason": f"device trust is {trust}, not executable"}
        if self.presence_of(device_id) in (Presence.OFFLINE,
                                           Presence.UNKNOWN, Presence.STALE):
            return {"stage": "rejected",
                    "reason": "device not present (offline/stale/unknown)"}
        need = definition.required_capabilities or []
        for cap in need:
            authz = self.authorize_capability(cap, device_id, who)
            if not authz["authorized"]:
                self._audit("execution_rejected", device_id,
                            f"{tool}: {authz['reason']}")
                action = "defer" if authz["reason"].startswith(
                    "governor") else "rejected"
                return {"stage": action, "reason": authz["reason"],
                        "checks": authz["checks"]}
        from .governor import ResourceSnapshot
        gate = self.governor.check(
            self._snapshot_for(device_id), definition.estimated_cost,
            definition.risk.value)
        if gate.action == "defer":
            return {"stage": "deferred", "reason": gate.reason}
        try:
            rec = self.engine.submit(tool, dict(inputs), device_id,
                                     who=who, mission_id=mission_id or None)
        except Exception as e:  # noqa: BLE001 — engine raises PolicyDenied
            self._audit("execution_rejected", device_id,
                        f"{tool}: {type(e).__name__}")
            raise
        verified, why = self.verify_device_result(tool, rec)
        self._audit("execution_finished", device_id,
                    f"{tool} {rec.state.value} verified={verified}")
        return {"stage": rec.state.value, "execution_id": rec.id,
                "result": rec.result, "error": rec.error,
                "verified": verified, "verification": why}

    def _grant_covers_tool(self, device_id: str, definition) -> bool:
        """A live grant covering every required capability at the tool's
        risk lifts RESTRICTED for this execution only. It never satisfies
        policy approval holds — those still flow through the engine.
        Tools with no required capabilities are never grant-lifted
        (a grant must name what it unlocks)."""
        need = definition.required_capabilities or []
        if not need:
            return False
        risk = getattr(definition.risk, "value", definition.risk)
        for cap in need:
            grant = next(
                (g for g in self._grants.values()
                 if g.device_id == device_id and g.covers(cap, risk)), None)
            if grant is None:
                return False
        return True

    def verify_device_result(self, tool: str, rec) -> tuple[bool, str]:
        """Post-execution verification using the tool's declared mode."""
        try:
            definition = self.registry.get(tool)
            mode = definition.verification or "none"
        except KeyError:
            return False, "unknown tool"
        state = getattr(rec.state, "value", rec.state)
        if state != "succeeded":
            return False, f"state={state}"
        if mode == "none":
            return True, "succeeded (no output contract)"
        if mode == "output_schema":
            schema = definition.output_schema or {}
            required = schema.get("required", []) \
                if isinstance(schema, dict) else []
            result = rec.result or {}
            missing = [k for k in required if k not in result]
            if missing:
                self._audit("verification_failed", rec.device_id,
                            f"{tool} missing={missing}")
                return False, f"missing keys: {missing}"
            return True, "output schema satisfied"
        return bool(rec.verified), "explicit verification flag"

    # ----- natural-language query helpers (facts for the LLM to phrase) -----

    def describe_fleet(self) -> str:
        views = self.list_devices()
        if not views:
            return "No devices known."
        parts = []
        for v in views:
            parts.append(
                f"{v.display_name} ({v.device_type}): trust={v.trust}, "
                f"presence={v.presence}, caps={len(v.capabilities)}")
        return "; ".join(parts) + "."

    def describe_device(self, device_id: str) -> str:
        v = self.device_view(device_id)
        lines = [f"{v.display_name} ({v.device_type}): trust={v.trust}, "
                 f"presence={v.presence}."]
        for r in sorted(self.capabilities_for(device_id),
                        key=lambda r: r.name):
            state = "usable" if r.usable() else \
                f"registered but {r.availability}"
            lines.append(f"- {r.name}: {state} (risk {r.risk.value}).")
        if v.trust not in EXECUTABLE_TRUST:
            lines.append(f"Note: {device_id} is {v.trust}, so its "
                         f"capabilities are not authorized for use.")
        return "\n".join(lines)

    # ----- audit helper -----

    def _audit(self, action: str, target: str, detail: str = "") -> None:
        try:
            self.audit.record("fabric", action, target, detail[:500])
        except Exception:  # noqa: BLE001 — audit never breaks fabric
            pass
        try:
            self.bus.publish(action, source="fabric",
                             payload={"device_id": target, "detail": detail})
        except Exception:  # noqa: BLE001
            pass
