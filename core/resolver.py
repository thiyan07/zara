"""Deterministic capability resolver + safe escalation ladder (Stage 17).

The resolver answers, without any LLM:

  which capability -> which device -> available? -> trusted? -> present?
  -> policy? -> governor? -> approval? -> execution level?

It ORCHESTRATES existing authorities (DeviceAuth/Fabric trust,
presence, Stage 16 availability, PolicyEngine, ResourceGovernor,
grants) and the existing route/execute paths. It duplicates none of
them: per-candidate checks go through FabricRegistry.authorize_capability
(the strict 13-step pipeline), device selection through
FabricRegistry.route_capability (pinned-device no-failover rule), and
execution stays on execute_on_device. Same state -> same result, always.

Escalation ladder (planning concept, never permission):

  0 NATIVE  — device-implemented capability (descriptors)
  1 TOOL    — registered Core/tool-registry capability
  2 MCP     — MCP-server capability (still Core-authorized)
  3 BROWSER — browser/app-integration capability (still Core-authorized)
  4 GUI     — future screen interaction: represented, NEVER executable
  5 HUMAN   — terminal: a human must do it; deterministic output, not exec

Fallback is explicit Core metadata (fallback_for), never inferred, never
LLM-invented, never authority: a fallback still resolves through the full
pipeline, and destructive (high_risk) capabilities never fail over.
"""
from __future__ import annotations
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

_CAP_ID_RE = None  # lazy: reuse fabric's pattern to avoid duplication


def _cap_id_ok(cid: str) -> bool:
    import re
    global _CAP_ID_RE
    if _CAP_ID_RE is None:
        _CAP_ID_RE = re.compile(r"^[a-z][a-z0-9_.:\-]{1,63}$")
    return isinstance(cid, str) and bool(_CAP_ID_RE.match(cid))


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"  # executable now (approval may still be required)
    REQUIRES_APPROVAL = "requires_approval"  # only gate left is a human yes
    UNAVAILABLE = "unavailable"  # supported but not usable right now
    UNAUTHORIZED = "unauthorized"  # trust/grant failure
    NO_CAPABILITY = "no_capability"  # nothing advertises/registers it
    NO_DEVICE = "no_device"  # capability exists but no eligible device
    OFFLINE = "offline"  # eligible devices exist but none is reachable
    GOVERNOR_BLOCKED = "governor_blocked"
    POLICY_BLOCKED = "policy_blocked"
    REQUIRES_ESCALATION = "requires_escalation"  # needs a higher ladder rung


class EscalationLevel(int, Enum):
    NATIVE = 0
    TOOL = 1
    MCP = 2
    BROWSER = 3
    GUI = 4  # represented only; stage17 never executes this rung
    HUMAN = 5  # terminal planning output; never executable


# Tool-name prefixes that place a registered (non-device) capability on
# the ladder. Everything else registered is a plain TOOL.
_MCP_PREFIX = "mcp."
_BROWSER_PREFIXES = ("browser.", "code.")


def level_of(capability: str, registry=None) -> EscalationLevel:
    """Ladder rung for a capability name. Device descriptors are NATIVE
    (checked by the caller when a descriptor exists); otherwise the tool
    registry decides TOOL/MCP/BROWSER; unknown names sit at HUMAN
    (someone must define or hand-do them). Never executable by itself."""
    if capability.startswith(_MCP_PREFIX):
        return EscalationLevel.MCP
    if capability.startswith(_BROWSER_PREFIXES):
        return EscalationLevel.BROWSER
    if registry is not None:
        try:
            if registry.get(capability) is not None:
                return EscalationLevel.TOOL
        except Exception:  # noqa: BLE001 — unknown reads as absent
            pass
    return EscalationLevel.HUMAN


# ---- explicit fallback registry (Core-controlled metadata) ----

MAX_FALLBACKS_PER_CAP = 5
MAX_FALLBACK_CHAIN = 3


class RelationshipRejected(ValueError):
    """Invalid fallback/relationship registration. Never stored."""


class FallbackRegistry:
    """Explicit fallback_for metadata. Registration validates: known
    capability references (descriptors + tool registry + ladder prefixes),
    known relationship type, no self-reference, no cycles, bounded graph,
    and risk discipline (a fallback may never exceed the original's risk;
    high_risk originals get NO fallback — destructive work never fails
    over). Relationships are planning hints; every fallback candidate
    still resolves through the full authorization pipeline."""

    KNOWN_RELATIONS = frozenset({
        "fallback_for", "implements", "requires", "supersedes",
        "depends_on"})

    def __init__(self):
        self._fallbacks: dict[str, list[str]] = {}
        self._relations: list[dict] = []

    @staticmethod
    def _risk_rank(risk: str) -> int:
        return {"safe": 0, "confirm": 1, "high_risk": 2}.get(risk, 1)

    def register(self, capability: str, fallback: str,
                 relation: str = "fallback_for",
                 known: Optional[set] = None,
                 risks: Optional[dict] = None) -> dict:
        if relation not in self.KNOWN_RELATIONS:
            raise RelationshipRejected(
                f"unknown relationship type: {relation!r}")
        for cid in (capability, fallback):
            if not _cap_id_ok(cid):
                raise RelationshipRejected(f"bad capability id: {cid!r}")
        if capability == fallback:
            raise RelationshipRejected("self-referential relationship")
        if known is not None and \
                (capability not in known or fallback not in known):
            raise RelationshipRejected("unknown capability reference")
        if risks is not None and \
                self._risk_rank(risks.get(fallback, "confirm")) > \
                self._risk_rank(risks.get(capability, "confirm")):
            raise RelationshipRejected(
                "fallback must not exceed original risk")
        if risks is not None and \
                self._risk_rank(risks.get(capability, "safe")) >= 2:
            raise RelationshipRejected(
                "destructive capabilities never fail over")
        # Cycle check over fallback_for edges (other relations document,
        # they never route, so only fallback edges can loop execution).
        if relation == "fallback_for":
            seen, stack = set(), [fallback]
            while stack:
                cur = stack.pop()
                if cur == capability:
                    raise RelationshipRejected("fallback cycle")
                if cur in seen:
                    continue
                seen.add(cur)
                stack.extend(self._fallbacks.get(cur, []))
        if relation == "fallback_for":
            lst = self._fallbacks.setdefault(capability, [])
            if fallback not in lst:
                if len(lst) >= MAX_FALLBACKS_PER_CAP:
                    raise RelationshipRejected("too many fallbacks")
                lst.append(fallback)
        self._relations.append({"capability": capability,
                                "related": fallback,
                                "relation": relation})
        if len(self._relations) > 500:
            raise RelationshipRejected("relationship graph too large")
        return {"capability": capability, "related": fallback,
                "relation": relation}

    def fallbacks_for(self, capability: str) -> list[str]:
        return list(self._fallbacks.get(capability, []))

    def as_dict(self) -> dict:
        return {k: list(v) for k, v in self._fallbacks.items()}


# ---- structured models ----

class Candidate(BaseModel):
    device_id: str = ""
    supported: bool = False
    available: bool = False
    availability: str = ""
    availability_reason: str = ""
    trusted: bool = False
    trust: str = ""
    presence: str = ""
    stale: bool = False
    version: str = ""
    risk: str = "safe"
    escalation: int = 0
    authorized: bool = False
    approval_required: bool = False
    status: str = "no_device"
    reason: str = ""


class Resolution(BaseModel):
    capability: str = ""
    status: str = "no_capability"
    device_id: Optional[str] = None
    candidates: list[Candidate] = Field(default_factory=list)
    fallback_used: Optional[str] = None
    required_escalation: Optional[int] = None
    substituted: bool = False
    reason: str = ""


# Advisory preference keys honored by ranking (never authority).
PREF_PREFERRED_DEVICE = "preferred_device"

_TRUST_RANK = {"trusted": 0, "temporarily_trusted": 1, "restricted": 2,
               "pairing": 3, "discovered": 4, "unknown": 5, "revoked": 6}
_PRESENCE_RANK = {"online": 0, "degraded": 1, "reconnecting": 2,
                  "stale": 3, "offline": 4, "unknown": 5}


def _version_key(version: str) -> tuple:
    try:
        return tuple(int(p) for p in version.split(".")[:3])
    except Exception:  # noqa: BLE001 — unparsable sorts lowest
        return ()


class CapabilityResolver:
    """Deterministic orchestration over FabricRegistry. No LLM, no auth of
    its own, no execution. Ranking order (documented, total, stable):

      authorized first -> usable -> pinned-device match ->
      preferred-device match -> trust rank -> presence rank ->
      device-kind order -> battery desc -> capability version desc ->
      device_id asc (final tiebreak)

    Unavailable data is skipped, never invented; ties always break on
    device_id, so the same state yields the same order."""

    def __init__(self, fabric, fallbacks: Optional[FallbackRegistry] = None):
        self.fabric = fabric
        self.fallbacks = fallbacks or FallbackRegistry()

    # -- known-capability universe (descriptors + registry + ladder) --

    def known_capabilities(self) -> set[str]:
        known: set[str] = set()
        try:
            for v in self.fabric.list_devices():
                for r in self.fabric.capabilities_for(v.device_id):
                    if not r.capability_id.startswith("legacy:"):
                        known.add(r.capability_id)
                        known.add(r.name)
        except Exception:  # noqa: BLE001 — degrade to registry only
            pass
        try:
            for d in self.fabric.engine.registry.list():
                known.add(d.name)
        except Exception:  # noqa: BLE001
            pass
        return known

    def capability_risks(self) -> dict[str, str]:
        risks: dict[str, str] = {}
        try:
            for v in self.fabric.list_devices():
                for r in self.fabric.capabilities_for(v.device_id):
                    if not r.capability_id.startswith("legacy:"):
                        risks.setdefault(r.capability_id, r.risk.value)
                        risks.setdefault(r.name, r.risk.value)
        except Exception:  # noqa: BLE001
            pass
        try:
            for d in self.fabric.engine.registry.list():
                risks.setdefault(d.name, d.risk.value)
        except Exception:  # noqa: BLE001
            pass
        return risks

    # -- candidate collection (one strict authorize per device) --

    def _candidates(self, capability: str, who: str,
                    constraints: dict) -> list[Candidate]:
        out: list[Candidate] = []
        try:
            views = self.fabric.list_devices()
        except Exception:  # noqa: BLE001 — no devices readable
            return out
        for v in views:
            recs = [r for r in self.fabric.capabilities_for(v.device_id)
                    if not r.capability_id.startswith("legacy:") and
                    (r.capability_id == capability or r.name == capability)]
            if not recs:
                continue
            rec = sorted(recs, key=lambda r: r.capability_id)[0]
            if not self._constraints_match(rec, v, constraints):
                continue
            authz = self.fabric.authorize_capability(
                rec.name, v.device_id, who)
            checks = authz.get("checks", {})
            out.append(Candidate(
                device_id=v.device_id, supported=True,
                available=rec.usable(), availability=rec.availability,
                availability_reason=rec.availability_reason,
                trusted=v.trust in ("trusted", "temporarily_trusted"),
                trust=v.trust, presence=v.presence, stale=rec.stale,
                version=rec.version, risk=rec.risk.value,
                escalation=int(EscalationLevel.NATIVE),
                authorized=authz["authorized"],
                approval_required=authz.get("approval_required", False),
                status=self._candidate_status(authz, rec, v),
                reason=authz["reason"]))
        return out

    @staticmethod
    def _candidate_status(authz: dict, rec, view) -> str:
        if authz["authorized"]:
            return ("requires_approval"
                    if authz.get("approval_required") else "resolved")
        reason = authz.get("reason", "")
        checks = authz.get("checks", {})
        if checks.get("trust", "") in ("revoked",) or \
                "trust" in reason and "not executable" in reason:
            return "unauthorized"
        if "governor" in reason:
            return "governor_blocked"
        if "policy" in reason or authz.get("hard_deny"):
            return "policy_blocked"
        if "OFFLINE" in reason.upper() or "stale" in reason:
            return "offline"
        if "usable" in reason or "OS permission" in reason:
            return "unavailable"
        if authz.get("approval_required"):
            return "requires_approval"
        return "unauthorized"

    @staticmethod
    def _constraints_match(rec, view, constraints: dict) -> bool:
        try:
            plat = constraints.get("platform", "")
            if plat and plat not in (rec.platforms or ["any"]) \
                    and plat != "any":
                # Descriptor platforms empty (legacy) means unconstrained.
                if rec.platforms:
                    return False
            min_v = constraints.get("min_version", "")
            if min_v and _version_key(rec.version) < _version_key(min_v):
                return False
            risk_max = constraints.get("risk_max", "")
            rank = {"safe": 0, "confirm": 1, "high_risk": 2}
            if risk_max and rank.get(rec.risk.value, 1) > \
                    rank.get(risk_max, 2):
                return False
            if constraints.get("online_only", False) and \
                    view.presence in ("offline", "unknown"):
                # Explicit opt-in only: by default offline devices are
                # INCLUDED so authorize can report OFFLINE honestly
                # instead of silently vanishing (existence vs reachability).
                return False
        except Exception:  # noqa: BLE001 — malformed constraint never matches
            return False
        return True

    # -- deterministic ranking --

    def _rank_key(self, c: Candidate, pinned: str,
                  preferred: str, kind_order: dict) -> tuple:
        batt = -1.0
        try:
            v = self.fabric.device_view(c.device_id)
            batt = v.battery_pct if v.battery_pct is not None else -1.0
        except Exception:  # noqa: BLE001 — unknown battery sorts last
            pass
        return (not c.authorized, not c.available,
                not (pinned and c.device_id == pinned),
                not (preferred and c.device_id == preferred),
                _TRUST_RANK.get(c.trust, 9),
                _PRESENCE_RANK.get(c.presence, 9),
                kind_order.get(self._device_kind(c.device_id), 9),
                -batt if batt is not None else 0.0,
                tuple(-p for p in _version_key(c.version)),
                c.device_id)

    def _device_kind(self, device_id: str) -> str:
        try:
            return self.fabric.device_view(device_id).device_type
        except Exception:  # noqa: BLE001
            return "unknown"

    # -- public entry points --

    def resolve(self, capability: str, device_id: str = "",
                who: str = "user", constraints: Optional[dict] = None,
                preferred_device: str = "",
                allow_fallback: bool = True,
                _depth: int = 0) -> Resolution:
        """Strict pipeline: exists -> schema -> device -> auth -> trust ->
        presence -> support -> availability -> constraints -> policy ->
        governor -> approval -> rank. Later stages never bypass earlier
        ones (each candidate carries its own authorize verdict)."""
        constraints = dict(constraints or {})
        if not _cap_id_ok(capability):
            return Resolution(
                capability=capability, status="no_capability",
                reason=f"malformed capability id: {capability!r}"[:200],
                required_escalation=int(EscalationLevel.HUMAN))
        if device_id and not _cap_id_ok(device_id) and \
                not self._known_device(device_id):
            return Resolution(
                capability=capability, status="no_device",
                reason=f"unknown device: {device_id}"[:200])
        cands = self._candidates(capability, who, constraints)
        if device_id:
            cands = [c for c in cands if c.device_id == device_id]
            if not cands:
                return self._missing(
                    capability, device_id, who, allow_fallback, _depth,
                    constraints, preferred_device)
        if not cands:
            return self._missing(
                capability, device_id, who, allow_fallback, _depth,
                constraints, preferred_device)
        try:
            from .fabric import _KIND_ORDER
            kind_order = dict(_KIND_ORDER)
        except Exception:  # noqa: BLE001
            kind_order = {}
        cands.sort(key=lambda c: self._rank_key(
            c, device_id, preferred_device, kind_order))
        top = cands[0]
        # Pinned destructive/approval/governor/policy failure: never
        # substitute silently (route_capability enforces the same rule).
        route = self.fabric.route_capability(
            capability, who,
            device_id if device_id else None)
        if device_id and route.device_id != device_id and \
                route.device_id is not None:
            # Substitution happened despite pin: only legal for safe risk
            # on presence/governor/OS failure (route guarantees it).
            chosen = next((c for c in cands
                           if c.device_id == route.device_id), top)
            return Resolution(
                capability=capability, status=top.status
                if top.device_id == route.device_id else chosen.status,
                device_id=route.device_id, candidates=cands,
                substituted=True, reason=route.reason[:300])
        if top.authorized:
            reason = top.reason[:300]
            if top.stale:
                # Stage 16 invariant: stale snapshots still route, but the
                # staleness must be impossible to miss in the verdict.
                reason = (reason + " (snapshot stale — device should "
                          "re-describe)")[:300]
            return Resolution(
                capability=capability,
                status="requires_approval"
                if top.approval_required else "resolved",
                device_id=top.device_id, candidates=cands,
                reason=reason)
        # Nobody executable: most informative failure wins (ranked first).
        # device_id stays None on failure: nobody will execute anything.
        status = top.status if top.status in (
            "governor_blocked", "policy_blocked", "offline",
            "unavailable", "unauthorized", "requires_approval") \
            else "no_device"
        fb = self._try_fallback(
            capability, device_id, who, constraints, preferred_device,
            allow_fallback, _depth)
        if fb is not None:
            return fb
        return Resolution(
            capability=capability, status=status,
            candidates=cands,
            required_escalation=self._escalation_for(
                capability, status),
            reason=top.reason[:300])

    def _missing(self, capability: str, device_id: str, who: str,
                 allow_fallback: bool, depth: int, constraints: dict,
                 preferred: str) -> Resolution:
        fb = self._try_fallback(
            capability, device_id, who, constraints, preferred,
            allow_fallback, depth)
        if fb is not None:
            return fb
        known = self.known_capabilities()
        if capability not in known:
            return Resolution(
                capability=capability, status="no_capability",
                reason=f"no device advertises {capability}"[:200],
                required_escalation=int(self._registry_level(capability)))
        if device_id:
            # The capability exists somewhere: say exactly what is wrong
            # with THIS device (offline? unsupported? unavailable?) via a
            # direct authorize verdict instead of "does not advertise" lie.
            verdict = self._verdict_for(device_id, capability, who)
            if verdict is not None:
                return Resolution(
                    capability=capability, status=verdict.status,
                    candidates=[verdict],
                    required_escalation=int(EscalationLevel.NATIVE),
                    reason=verdict.reason[:300])
            return Resolution(
                capability=capability, status="no_device",
                reason=f"{device_id} does not advertise "
                       f"{capability}"[:200],
                required_escalation=int(EscalationLevel.NATIVE))
        return Resolution(
            capability=capability, status="no_device",
            reason=f"no eligible device for {capability}"[:200],
            required_escalation=int(self._registry_level(capability)))

    def _verdict_for(self, device_id: str, capability: str,
                     who: str) -> Optional[Candidate]:
        """Direct authorize verdict for one device (used when the normal
        candidate sweep finds nothing pinned-usable). Returns None when
        the device does not advertise the capability at all."""
        try:
            recs = [r for r in self.fabric.capabilities_for(device_id)
                    if not r.capability_id.startswith("legacy:") and
                    (r.capability_id == capability or r.name == capability)]
            views = {v.device_id: v for v in self.fabric.list_devices()}
            v = views.get(device_id)
            if not recs or v is None:
                return None
            rec = sorted(recs, key=lambda r: r.capability_id)[0]
            authz = self.fabric.authorize_capability(
                rec.name, device_id, who)
            return Candidate(
                device_id=device_id, supported=True,
                available=rec.usable(), availability=rec.availability,
                availability_reason=rec.availability_reason,
                trusted=v.trust in ("trusted", "temporarily_trusted"),
                trust=v.trust, presence=v.presence, stale=rec.stale,
                version=rec.version, risk=rec.risk.value,
                escalation=int(EscalationLevel.NATIVE),
                authorized=authz["authorized"],
                approval_required=authz.get("approval_required", False),
                status=self._candidate_status(authz, rec, v),
                reason=authz["reason"])
        except Exception:  # noqa: BLE001 — verdict best-effort only
            return None

    def _try_fallback(self, capability: str, device_id: str, who: str,
                      constraints: dict, preferred: str,
                      allow: bool, depth: int) -> Optional[Resolution]:
        if not allow or depth >= MAX_FALLBACK_CHAIN:
            return None
        for fb_cap in self.fallbacks.fallbacks_for(capability):
            # Destructive originals never reach here with fallbacks
            # (registration forbids them), but re-check defensively:
            risks = self.capability_risks()
            rank = {"safe": 0, "confirm": 1, "high_risk": 2}
            if rank.get(risks.get(capability, "safe"), 0) >= 2:
                continue
            sub = self.resolve(fb_cap, device_id, who, constraints,
                               preferred, allow, depth + 1)
            if sub.device_id and sub.status in (
                    "resolved", "requires_approval"):
                sub.fallback_used = fb_cap
                sub.reason = (f"fallback {fb_cap}: {sub.reason}")[:300]
                return sub
        return None

    def _registry_level(self, capability: str) -> EscalationLevel:
        try:
            reg = self.fabric.engine.registry
        except Exception:  # noqa: BLE001
            reg = None
        return level_of(capability, reg)

    def _escalation_for(self, capability: str,
                        status: str) -> Optional[int]:
        if status in ("no_capability",):
            return int(self._registry_level(capability))
        return int(EscalationLevel.NATIVE)

    def _known_device(self, device_id: str) -> bool:
        try:
            return any(v.device_id == device_id
                       for v in self.fabric.list_devices())
        except Exception:  # noqa: BLE001
            return False

    # -- LLM proposal validation (Core derives everything) --

    PROPOSAL_SECURITY_FIELDS = frozenset({
        "authorized", "trust", "trusted", "governor", "policy",
        "approval_granted", "grant_id", "allow", "allowed"})

    def resolve_proposal(self, proposal: Any,
                         who: str = "user") -> Resolution:
        """Validate an untrusted LLM proposal dict. Only capability,
        device, and advisory constraint/preference fields are read;
        any security-claim fields are IGNORED (never trusted, never
        stored). Malformed proposals resolve to safe failure states."""
        if not isinstance(proposal, dict):
            return Resolution(
                capability="", status="no_capability",
                reason="proposal must be an object")
        cap = proposal.get("preferred_capability",
                           proposal.get("capability", ""))
        dev = proposal.get("preferred_device",
                           proposal.get("device_id", ""))
        if not isinstance(cap, str) or not isinstance(dev, str):
            return Resolution(
                capability="", status="no_capability",
                reason="proposal fields must be strings")
        constraints: dict = {}
        raw_constraints = proposal.get("constraints", {})
        if isinstance(raw_constraints, dict):
            for k in ("platform", "min_version", "risk_max",
                      "online_only"):
                if k in raw_constraints:
                    constraints[k] = raw_constraints[k]
        preferred = proposal.get("preferred_device", "")
        if not isinstance(preferred, str):
            preferred = ""
        # NOTE: PROPOSAL_SECURITY_FIELDS are deliberately never read.
        return self.resolve(cap, dev, who, constraints, preferred,
                            allow_fallback=True)

    # -- relationship management (operator-side) --

    def register_fallback(self, capability: str, fallback: str,
                          relation: str = "fallback_for") -> dict:
        return self.fallbacks.register(
            capability, fallback, relation, self.known_capabilities(),
            self.capability_risks())
