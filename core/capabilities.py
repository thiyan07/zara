"""Dynamic device capability schema + discovery (Stage 16).

A capability descriptor says what a device CAN do. It NEVER grants
permission — authorization stays solely with PolicyEngine + Governor +
FabricRegistry trust/presence checks at route/execute time, and execution
stays on the existing tool/job paths. Capability documents are UNTRUSTED
input: strictly validated, bounded, never executed, never eval'd.

Concepts borrowed from OACP (implemented natively, no OACP code):
versioned manifests, namespaced IDs, aliases/examples for future
matching. Differences: no device-dictated invocation (no intents,
no broadcasts, no invoke blocks — Core decides how execution happens),
no embedding/keyword matching in Stage 16 (deterministic resolution
foundation only: capability -> devices -> availability -> policy ->
governor -> authorization).
"""
from __future__ import annotations
import json
import re

from pydantic import BaseModel, Field

from .models import RiskLevel, utcnow

DESCRIPTOR_VERSION = "1"

MAX_DOC_BYTES = 32768
MAX_DESCRIBE_BATCH = 100
MAX_DESCRIPTION_LEN = 500
MAX_PERMISSIONS = 20
MAX_ALIASES = 10
MAX_EXAMPLES = 5
MAX_KEYWORDS = 15
MAX_SCHEMA_BYTES = 8192
MAX_SCHEMA_DEPTH = 5

_CAP_ID = re.compile(r"^[a-z][a-z0-9_.:\-]{1,63}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_PERM = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}$")
_PLATFORM = {"any", "linux", "android"}
_EXECUTION = {"on-device", "core-mediated"}
_AVAILABILITY = {"available", "unavailable", "os_denied", "unimplemented"}

# Field names that must never appear in a capability document, at any
# depth outside a JSON Schema ``properties`` mapping. A device claiming
# "authorized"/"allow"/"trust"/"grant_id" is lying about authority it
# does not have — reject the whole document.
#
# Inside a schema ``properties`` mapping, keys are field names being
# *documented* (transfer documents a ``grant_id`` input; the allowlisted
# shell tool documents a ``command`` input) — never authority. They are
# exempt at exactly that one level because nothing ever executes schema
# content: execution runs only registered allowlisted handlers through
# Policy + Governor + grants. A device documenting a ``shell`` field
# still cannot execute anything (test_30 proves it).
FORBIDDEN_KEYS = frozenset({
    "authorized", "allow", "allowed", "grant", "grant_id", "policy",
    "trust", "trusted", "exec", "execute", "execution_command", "command",
    "shell", "eval", "system", "subprocess", "popen", "secret", "password",
    "passwd", "token", "api_key", "apikey", "private_key", "credential",
    "__proto__", "constructor", "prototype",
})


class CapabilityRejected(ValueError):
    """Malformed or unsafe capability document. Audited, never stored."""


def _scan_tree(node, depth: int, path: str,
               _in_schema_props: bool = False) -> None:
    """Recursive safety scan: depth cap, forbidden keys, control chars.

    Keys inside a JSON Schema ``properties`` mapping are field names being
    *described* (e.g. the transfer interface legitimately documents a
    ``grant_id`` input field) — not authority claims — so the forbidden-key
    check is lifted exactly there. Values are still scanned everywhere.
    """
    if depth > MAX_SCHEMA_DEPTH + 2:
        raise CapabilityRejected(f"nesting too deep at {path}")
    if isinstance(node, dict):
        for k, v in node.items():
            if not isinstance(k, str):
                raise CapabilityRejected(f"non-string key at {path}")
            if not _in_schema_props and k.lower() in FORBIDDEN_KEYS:
                raise CapabilityRejected(
                    f"forbidden field {k!r} at {path}: devices cannot "
                    f"claim authority")
            if len(k) > 128:
                raise CapabilityRejected(f"key too long at {path}")
            # A "properties" mapping's *immediate* keys declare schema field
            # names (e.g. transfer documents a ``grant_id`` input) — exempt
            # exactly that one level; anything deeper is scanned strictly.
            _scan_tree(v, depth + 1, f"{path}.{k}",
                       _in_schema_props=(k == "properties"))
    elif isinstance(node, list):
        if len(node) > 64:
            raise CapabilityRejected(f"list too long at {path}")
        for i, v in enumerate(node):
            _scan_tree(v, depth + 1, f"{path}[{i}]")
    elif isinstance(node, str):
        if len(node) > 2048:
            raise CapabilityRejected(f"string too long at {path}")
        if any(ord(c) < 0x20 and c not in ("\n", "\t") for c in node):
            raise CapabilityRejected(f"control characters at {path}")
    elif not isinstance(node, (int, float, bool)) and node is not None:
        raise CapabilityRejected(f"bad value type at {path}")


def _check_schema(schema, what: str) -> dict:
    if not isinstance(schema, dict):
        raise CapabilityRejected(f"{what} must be an object")
    raw = json.dumps(schema)
    if len(raw) > MAX_SCHEMA_BYTES:
        raise CapabilityRejected(f"{what} too large")
    _scan_tree(schema, 0, what)
    return schema


class CapabilityDescriptor(BaseModel, extra="forbid"):
    """Strict device-supplied descriptor. Core-attached snapshot fields
    (advertised_at, source, software_version) are deliberately ABSENT:
    a device sending them is rejected — Core sets them itself."""

    model_config = {"extra": "forbid"}

    id: str = Field(min_length=1, max_length=64)
    descriptor_version: str = "1"
    name: str = Field(min_length=1, max_length=64)
    version: str = "1.0.0"
    description: str = Field(default="", max_length=MAX_DESCRIPTION_LEN)
    risk: RiskLevel = RiskLevel.SAFE
    requires: list[str] = Field(default_factory=list)  # OS permissions
    platforms: list[str] = Field(default_factory=lambda: ["any"])
    execution: str = "on-device"
    requires_foreground: bool = False
    requires_network: bool = False
    battery_sensitive: bool = False
    supports_cancellation: bool = False
    supports_streaming: bool = False
    timeout_s: float = Field(default=30.0, gt=0, le=600)
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    availability: str = "available"
    availability_reason: str = Field(default="", max_length=200)
    os_permission_granted: bool | None = None
    aliases: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    def validate_descriptor(self) -> None:
        if not _CAP_ID.match(self.id):
            raise CapabilityRejected(f"bad capability id: {self.id!r}")
        if not _CAP_ID.match(self.name):
            raise CapabilityRejected(f"bad capability name: {self.name!r}")
        if self.descriptor_version != DESCRIPTOR_VERSION:
            raise CapabilityRejected(
                f"unknown descriptor version: {self.descriptor_version!r}")
        if not _VERSION.match(self.version):
            raise CapabilityRejected(
                f"version must be MAJOR.MINOR.PATCH: {self.version!r}")
        if len(self.requires) > MAX_PERMISSIONS:
            raise CapabilityRejected("too many required permissions")
        for p in self.requires:
            if not isinstance(p, str) or not _PERM.match(p):
                raise CapabilityRejected(f"bad permission: {p!r}")
        for p in self.platforms:
            if p not in _PLATFORM:
                raise CapabilityRejected(f"bad platform: {p!r}")
        if not self.platforms:
            raise CapabilityRejected("platforms must not be empty")
        if self.execution not in _EXECUTION:
            raise CapabilityRejected(f"bad execution: {self.execution!r}")
        if self.availability not in _AVAILABILITY:
            raise CapabilityRejected(f"bad availability: {self.availability!r}")
        if self.availability != "available" and not self.availability_reason:
            raise CapabilityRejected(
                "unavailable capabilities must state a reason")
        for label, items, lim, slen in (
                ("aliases", self.aliases, MAX_ALIASES, 80),
                ("examples", self.examples, MAX_EXAMPLES, 160),
                ("keywords", self.keywords, MAX_KEYWORDS, 40)):
            if len(items) > lim:
                raise CapabilityRejected(f"too many {label}")
            seen = set()
            for s in items:
                if not isinstance(s, str) or not s or len(s) > slen:
                    raise CapabilityRejected(f"bad {label} entry")
                if any(ord(c) < 0x20 for c in s):
                    raise CapabilityRejected(
                        f"control characters in {label}")
                if s.lower() in seen:
                    raise CapabilityRejected(f"duplicate {label}: {s!r}")
                seen.add(s.lower())
        _check_schema(self.input_schema, "input_schema")
        _check_schema(self.output_schema, "output_schema")

    def usable(self) -> bool:
        return self.availability == "available" and \
            self.os_permission_granted is not False


def validate_descriptor_doc(doc) -> CapabilityDescriptor:
    """Parse + fully validate one untrusted descriptor document."""
    if not isinstance(doc, dict):
        raise CapabilityRejected("descriptor must be an object")
    raw = json.dumps(doc)
    if len(raw) > MAX_DOC_BYTES:
        raise CapabilityRejected("descriptor document too large")
    _scan_tree(doc, 0, "descriptor")
    try:
        desc = CapabilityDescriptor(**doc)
    except Exception as e:  # noqa: BLE001 — pydantic shape errors
        raise CapabilityRejected(f"descriptor shape: {e}") from e
    desc.validate_descriptor()
    return desc


def validate_describe_batch(
        records) -> tuple[list[CapabilityDescriptor], list[dict]]:
    """Validate a device describe batch. Returns (accepted, rejected).
    Rejects: non-list, oversize batch, non-dict items, dup IDs, and any
    per-document failure. Order of accepted follows input order."""
    if not isinstance(records, list):
        raise CapabilityRejected("records must be a list")
    if len(records) > MAX_DESCRIBE_BATCH:
        raise CapabilityRejected(
            f"too many descriptors (max {MAX_DESCRIBE_BATCH})")
    if not records:
        return [], []
    accepted, rejected, seen = [], [], set()
    for raw in records:
        try:
            if not isinstance(raw, dict):
                raise CapabilityRejected("descriptor must be an object")
            desc = validate_descriptor_doc(raw)
            if desc.id in seen:
                raise CapabilityRejected(
                    f"duplicate capability id: {desc.id!r}")
            seen.add(desc.id)
            accepted.append(desc)
        except CapabilityRejected as e:
            rejected.append({"record": str(raw)[:120], "error": str(e)[:200]})
    return accepted, rejected


def descriptor_fingerprint(desc: CapabilityDescriptor) -> str:
    """Canonical fingerprint for diffing (version, availability, risk,
    permissions, schemas, aliases). Identity + snapshot excluded."""
    import hashlib as _hl
    canon = json.dumps({
        "version": desc.version, "availability": desc.availability,
        "availability_reason": desc.availability_reason,
        "risk": desc.risk.value, "requires": sorted(desc.requires),
        "input_schema": desc.input_schema,
        "output_schema": desc.output_schema,
        "aliases": sorted(a.lower() for a in desc.aliases),
        "platforms": sorted(desc.platforms),
        "execution": desc.execution,
        "requires_foreground": desc.requires_foreground,
        "requires_network": desc.requires_network,
        "timeout_s": desc.timeout_s,
    }, sort_keys=True)
    return _hl.sha256(canon.encode()).hexdigest()[:16]


def diff_descriptors(old_ids: dict[str, str],
                     new: list[CapabilityDescriptor]) -> dict:
    """Compare {id: fingerprint} snapshot vs new descriptors.
    Returns {added, removed, changed} id lists (sorted)."""
    new_map = {d.id: descriptor_fingerprint(d) for d in new}
    old_set, new_set = set(old_ids), set(new_map)
    changed = sorted(i for i in old_set & new_set
                     if old_ids[i] != new_map[i])
    return {"added": sorted(new_set - old_set),
            "removed": sorted(old_set - new_set), "changed": changed}


def transfer_descriptor() -> dict:
    """Canonical descriptor for the EXISTING Stage 15 transfer interface.
    Documents; does not authorize (grants + policy + governor still rule).
    Values mirror core/transfer.py limits — a consistency test enforces it."""
    from .transfer import (CHUNK_TIMEOUT_S, MAX_CHUNK_BYTES,
                           MAX_TRANSFER_BYTES, TRANSFER_CAPABILITY,
                           TRANSFER_RISK)
    return {
        "id": TRANSFER_CAPABILITY,
        "descriptor_version": DESCRIPTOR_VERSION,
        "name": TRANSFER_CAPABILITY,
        "version": "1.0.0",
        "description": ("Move bytes between paired devices through the "
                        "Core relay in bounded chunks with SHA-256 "
                        "verification. Requires a live sender-scoped temp "
                        "grant; Core policy + governor still apply."),
        "risk": TRANSFER_RISK,
        "requires": [],
        "platforms": ["any"],
        "execution": "core-mediated",
        "requires_foreground": False,
        "requires_network": True,
        "battery_sensitive": True,
        "supports_cancellation": True,
        "supports_streaming": False,
        "timeout_s": float(CHUNK_TIMEOUT_S),
        "input_schema": {"type": "object", "required": ["recipient_device",
                         "filename", "size_bytes", "sha256"],
                         "properties": {
                             "recipient_device": {"type": "string"},
                             "filename": {"type": "string"},
                             "size_bytes": {"type": "integer", "minimum": 1,
                                            "maximum": MAX_TRANSFER_BYTES},
                             "sha256": {"type": "string"},
                             "grant_id": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "availability": "available",
        "availability_reason": "",
        "aliases": ["send file", "share file", "copy file to device"],
        "examples": ["send this file to my laptop",
                     "copy the report to my phone"],
        "keywords": ["transfer", "send", "file", "copy", "share"],
        # Core-side only (never submitted by devices; extra="forbid"
        # would reject it): lets the consistency test pin doc vs code.
        "_limits": {"max_transfer_bytes": MAX_TRANSFER_BYTES,
                    "max_chunk_bytes": MAX_CHUNK_BYTES},
    }


def linux_descriptors() -> list[dict]:
    """Build descriptor docs from the Linux safe tool allowlist — single
    source of truth, so advertisement cannot drift from implementation.
    One descriptor per required_capability; risk is the max of the tools
    behind it; schemas merge only when tools genuinely share a cap."""
    from device.linux.tools_linux import LINUX_TOOLS
    by_cap: dict[str, list] = {}
    for definition, _handler in LINUX_TOOLS:
        for cap in definition.required_capabilities or []:
            by_cap.setdefault(cap, []).append(definition)
    docs = []
    for cap in sorted(by_cap):
        defs = by_cap[cap]
        rank = {"safe": 0, "confirm": 1, "high_risk": 2}
        top = max(defs, key=lambda d: rank.get(d.risk.value, 0))
        schemas = [d.input_schema for d in defs]
        docs.append({
            "id": cap, "descriptor_version": DESCRIPTOR_VERSION,
            "name": cap, "version": top.version,
            "description": top.description,
            "risk": top.risk.value, "requires": [],
            "platforms": ["linux"], "execution": "on-device",
            "requires_foreground": False, "requires_network": False,
            "battery_sensitive": False,
            "supports_cancellation": False, "supports_streaming": False,
            "timeout_s": min(d.timeout_s for d in defs),
            "input_schema": schemas[0] if len(schemas) == 1 else
            {"anyOf": schemas},
            "output_schema": {"type": "object"},
            "availability": "available", "availability_reason": "",
            "aliases": [], "examples": [], "keywords": [],
        })
    # files.transfer is Core-mediated (Stage 15 engine) but the Linux
    # agent implements its side (xfer_request/upload/pending/download).
    # Advertise the canonical descriptor minus Core-only fields so the
    # consistency rule (advertised == implemented) holds both ways.
    _xfer = transfer_descriptor()
    _xfer.pop("_limits", None)
    docs.append(_xfer)
    return sorted(docs, key=lambda d: d["id"])
