"""Battery/resource governor + re-exports (notifications/audit live in their
own modules per architecture; imported here for convenience)."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from .audit import AuditLog
from .notifications import NotificationManager

__all__ = ["AuditLog", "NotificationManager", "ResourceSnapshot",
           "GovernorDecision", "ResourceGovernor", "canonical_snapshot",
           "normalize_device_kind", "demand_defer_reason",
           "MEMORY_PRESSURE_BOUNDS", "CPU_PRESSURE_BOUNDS",
           "BATTERY_LOW_BAND", "PolicyVerdict", "evaluate_policy",
           "governor_v2_enabled", "governor_shadow_enabled"]


def _flag_on(name: str) -> bool:
    """Env flag: only explicit truthy values enable. Missing, empty, or
    anything else resolves to disabled (fail-closed default)."""
    return os.environ.get(name, "0").strip().lower() in (
        "1", "true", "yes", "on")


def governor_v2_enabled() -> bool:
    """Governor v2 rollout switch. Disabled by default; invalid resolves
    to disabled. No telemetry value or evidence record can enable it."""
    return _flag_on("ZARA_GOVERNOR_V2")


def governor_shadow_enabled() -> bool:
    """Shadow-comparison switch (read-only verdict comparison, never
    dispatch). Separate flag, also default off."""
    return _flag_on("ZARA_GOVERNOR_SHADOW")


@dataclass
class PolicyVerdict:
    """Typed Governor v2 policy boundary: normalized request/candidate +
    snapshot → deterministic decision. Closed states:
    - eligible: may proceed to ranking (never an authorization).
    - deferred: retryable availability hold (unknown/stale/pressure).
    - rejected: hard constraint failed (deny-equivalent, terminal).
    - legacy: Governor disabled; caller uses the legacy path.
    Pure data; no authority. Identical inputs → identical verdicts.
    """
    state: str  # eligible|deferred|rejected|legacy
    reason_code: str = ""
    detail: str = ""
    demand_reason: str | None = None


POLICY_REASON_CODES = frozenset({
    "eligible", "demand-defer", "cost-defer", "constraint-reject",
    "unknown-demand-defer", "stale-demand-defer", "legacy-mode",
    "unreadable-demand",
})


def evaluate_policy(snapshot: "ResourceSnapshot", demands=None,
                    constraints_ok: bool = True,
                    constraint_reason: str = "",
                    telemetry_stale: bool = False,
                    estimated_cost: float = 0.1,
                    risk: str = "safe") -> PolicyVerdict:
    """Governor v2 policy boundary (pure: no I/O, locks, mutation).

    Structural classification (no reason-string sniffing): the legacy
    cost gates run first on the demand-free path; only if they proceed
    does the demand predicate run. Unknown/stale follow existing demand
    discipline; missing never becomes zero; unsupported metrics stay
    unsupported. Never authorizes — eligibility only.
    """
    if constraints_ok is False:
        return PolicyVerdict(state="rejected",
                             reason_code="constraint-reject",
                             detail=constraint_reason[:200])
    gov = ResourceGovernor()
    legacy = gov.check(snapshot, estimated_cost, risk)
    if legacy.action == "defer":
        return PolicyVerdict(state="deferred", reason_code="cost-defer",
                             detail=legacy.reason[:200])
    if legacy.action == "reroute":
        return PolicyVerdict(state="deferred", reason_code="cost-defer",
                             detail=legacy.reason[:200])
    if demands is not None:
        reason = demand_defer_reason(snapshot, demands,
                                     telemetry_stale=telemetry_stale)
        if reason is not None:
            code = "unreadable-demand" if reason.startswith("unreadable") \
                else "unknown-demand-defer" if "unverifiable" in reason \
                else "demand-defer"
            return PolicyVerdict(state="deferred", reason_code=code,
                                 detail=reason[:200], demand_reason=reason[:200])
    return PolicyVerdict(state="eligible", reason_code="eligible",
                         detail="within budget")

@dataclass
class ResourceSnapshot:
    battery_pct: float | None = None
    charging: bool = False
    network: str = "unknown"  # offline|metered|wifi|wired|unknown
    device_kind: str = "cloud"
    # Legacy pressure hint: None = unknown (never reported). Superseded by
    # cpu_pct below; kept for backward compatibility. Governor ignores it.
    cpu_pressure: float | None = None
    # All telemetry below is OBSERVATION, not authority: validated,
    # bounded, nullable (None = unknown, never zero-as-healthy). Only
    # low_power_mode feeds the pre-existing gate; the rest is read-only
    # context for future calibration. No scheduler reads these yet.
    low_power_mode: bool | None = None
    cpu_pct: float | None = None        # 0-100, device-reported CPU usage
    mem_pct: float | None = None        # 0-100, device-reported RAM usage
    storage_free_mb: int | None = None
    storage_total_mb: int | None = None  # context for free (ratio reads);
        # no gate reads storage; never zero-as-healthy
    battery_temp_c: float | None = None
    thermal_state: str | None = None    # none|light|moderate|severe|critical

@dataclass
class GovernorDecision:
    action: str  # proceed|defer|reroute
    reason: str


def normalize_device_kind(kind: str) -> str:
    """Single kind normalizer for every snapshot builder: the closed set
    passes through, anything else becomes "unknown". Governor only
    branches on "android", so this unifies routing/fabric/app behavior."""
    return kind if kind in ("cloud", "linux", "android") else "unknown"


# Demand-threshold table (initial calibration, NOT scheduler weights).
# Each entry: tier -> strictly-greater-than pressure bound (0-100) above
# which a tool declaring that demand tier defers on that dimension.
# Values justified: emulator idles ~79% mem (so HIGH must clear 80 with
# margin), Linux idles ~8% cpu; near-total exhaustion (>98) is unsafe
# for anything. MODERATE sits between. Boundaries are STRICT `>`
# (matching existing governor style): value == threshold proceeds.
# These are eligibility predicates, not scores — no ranking, no cost
# adjustment, no cross-dimension tradeoff. Calibration may adjust the
# numbers; the inclusivity + unknown-discipline are the contract.
MEMORY_PRESSURE_BOUNDS = {"low": 98.0, "moderate": 93.0, "high": 85.0}
CPU_PRESSURE_BOUNDS = {"low": 98.0, "moderate": 90.0, "high": 75.0}
# Battery demand reuses the governor's own bands: "low" battery state
# means <30% uncharged (cf. CAUTION_BATTERY), and low-power mode is an
# explicit OS backpressure signal.
BATTERY_LOW_BAND = 30.0


def _finite_pct(value) -> float | None:
    """Normalize a telemetry reading to a finite 0-100 float, else None.

    None / NaN / ±inf / out-of-range / wrong-type all mean UNVERIFIABLE —
    never zero-as-healthy. Callers apply per-tier unknown policy."""
    try:
        if value is None:
            return None
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        if not 0.0 <= f <= 100.0:
            return None
        return f
    except (TypeError, ValueError):
        return None


def demand_defer_reason(snapshot: "ResourceSnapshot",
                        demands, telemetry_stale: bool = False) -> str | None:
    """Pure demand-threshold predicate: snapshot + demand → defer reason.

    Returns None when eligible, else a deterministic reason string.
    No I/O, no locks, no mutation, no ranking, no scores.

    Dimension semantics (each independent, first hit wins, fixed order
    memory → cpu → battery):
    - memory/cpu: value valid + strictly above tier bound → defer.
    - battery: MODERATE/HIGH defer on (low_power is True) or (valid
      battery below BATTERY_LOW_BAND while uncharged). LOW adds nothing
      beyond the existing cost-based battery gates.
    - network: NO predicate yet (snapshot carries only the network name;
      no latency/bandwidth telemetry exists). Tier ignored, documented.
    - UNKNOWN/STALE/UNAVAILABLE: LOW tolerates (existing architecture
      permits); MODERATE/HIGH defer as unverifiable — except battery,
      where a valid healthy reading verifies despite other unknowns.
      Stale snapshots are treated field-wise as unknown (never silently
      refreshed here).
    """
    from .models import ResourceDemands
    if demands is None:
        return None
    if isinstance(demands, dict):
        try:
            demands = ResourceDemands(**demands)
        except Exception:
            return "unreadable demand vector: defer"
    try:
        cpu_t = demands.cpu.value if hasattr(demands.cpu, "value") \
            else str(demands.cpu)
        mem_t = demands.memory.value if hasattr(demands.memory, "value") \
            else str(demands.memory)
        bat_t = demands.battery.value if hasattr(demands.battery, "value") \
            else str(demands.battery)
    except Exception:
        return "unreadable demand vector: defer"
    if telemetry_stale:
        mem_v = cpu_v = None
        stale = True
    else:
        mem_v = _finite_pct(getattr(snapshot, "mem_pct", None))
        cpu_v = _finite_pct(getattr(snapshot, "cpu_pct", None))
        stale = False
    # Memory axis.
    if mem_t in MEMORY_PRESSURE_BOUNDS:
        bound = MEMORY_PRESSURE_BOUNDS[mem_t]
        if mem_v is not None and mem_v > bound:
            return (f"memory pressure {mem_v:.1f}% exceeds {mem_t} "
                    f"demand bound {bound:.0f}%: defer")
        if mem_v is None and mem_t in ("moderate", "high"):
            return (f"memory pressure unverifiable "
                    f"({'stale' if stale else 'unknown'}) for {mem_t} "
                    "demand: defer")
    # CPU axis.
    if cpu_t in CPU_PRESSURE_BOUNDS:
        bound = CPU_PRESSURE_BOUNDS[cpu_t]
        if cpu_v is not None and cpu_v > bound:
            return (f"cpu pressure {cpu_v:.1f}% exceeds {cpu_t} "
                    f"demand bound {bound:.0f}%: defer")
        if cpu_v is None and cpu_t in ("moderate", "high"):
            return (f"cpu pressure unverifiable "
                    f"({'stale' if stale else 'unknown'}) for {cpu_t} "
                    "demand: defer")
    # Battery axis (LOW adds nothing beyond existing cost gates).
    if bat_t in ("moderate", "high"):
        lp = getattr(snapshot, "low_power_mode", None)
        if lp is True:
            return "low-power mode active: defer"
        batt = _finite_pct(getattr(snapshot, "battery_pct", None)) \
            if not telemetry_stale else None
        charging = bool(getattr(snapshot, "charging", False))
        if batt is not None and not charging and batt < BATTERY_LOW_BAND:
            return (f"battery {batt:.0f}% below {BATTERY_LOW_BAND:.0f}% "
                    "for battery demand: defer")
        if bat_t == "high" and lp is None and batt is None:
            return ("battery state unverifiable "
                    f"({'stale' if stale else 'unknown'}) for high "
                    "demand: defer")
    # Network axis: no predicate yet (name-only telemetry). Deliberate.
    return None


def canonical_snapshot(battery_pct: float | None = None,
                       charging: bool = False,
                       network: str = "unknown",
                       device_kind: str = "cloud",
                       low_power_mode: bool | None = None,
                       cpu_pct: float | None = None,
                       mem_pct: float | None = None,
                       storage_free_mb: int | None = None,
                       storage_total_mb: int | None = None,
                       battery_temp_c: float | None = None,
                       thermal_state: str | None = None) -> ResourceSnapshot:
    """ONE canonical ResourceSnapshot constructor. Every production
    builder (fabric, routing, app exec paths) must call this instead of
    hand-building ResourceSnapshot — one source of truth for defaults
    and normalization. Values are read-only observation; consumers must
    still treat None as unknown (see governor.check discipline)."""
    return ResourceSnapshot(
        battery_pct=battery_pct, charging=charging, network=network,
        device_kind=normalize_device_kind(device_kind),
        low_power_mode=low_power_mode, cpu_pct=cpu_pct, mem_pct=mem_pct,
        storage_free_mb=storage_free_mb,
        storage_total_mb=storage_total_mb,
        battery_temp_c=battery_temp_c,
        thermal_state=thermal_state)

class ResourceGovernor:
    """Battery-first execution gate. Consulted before running non-trivial tools."""
    LOW_BATTERY = 15.0
    CAUTION_BATTERY = 30.0

    def check(self, snap: ResourceSnapshot, estimated_cost: float = 0.1,
              risk: str = "safe", demands=None,
              telemetry_stale: bool = False) -> GovernorDecision:
        # Demand-threshold predicates (opt-in; demands=None preserves
        # legacy behavior exactly). Evaluated AFTER the existing
        # cost-based gates so established defer reasons keep priority and
        # wording. A demand defer is availability-only (retryable), never
        # authorization.
        demand_reason = demand_defer_reason(snap, demands, telemetry_stale) \
            if demands is not None else None
        # Unknown/low-power discipline: None or non-finite readings are
        # treated as unknown (no gate on that axis), never as healthy and
        # never as a defer trigger. Telemetry can only ever defer on
        # explicitly valid adverse readings, never authorize.
        if snap.low_power_mode is True and estimated_cost > 0.2:
            return GovernorDecision("defer", "low-power mode: deferring costly work")
        batt = snap.battery_pct
        try:
            batt_ok = batt is not None and float(batt) == float(batt) \
                and 0.0 <= float(batt) <= 100.0
        except (TypeError, ValueError):
            batt_ok = False
        if batt_ok and not snap.charging:
            if float(batt) < self.LOW_BATTERY:
                if estimated_cost > 0.2 or risk != "safe":
                    return GovernorDecision("defer", f"battery {snap.battery_pct}%: defer")
            elif float(batt) < self.CAUTION_BATTERY and estimated_cost > 0.5:
                return GovernorDecision("defer", f"battery {snap.battery_pct}%: heavy task deferred")
        if snap.network in ("offline",) and estimated_cost > 0.3:
            return GovernorDecision("defer", "offline: heavy work waits for connectivity")
        if snap.network == "metered" and estimated_cost > 0.6:
            return GovernorDecision("defer", "metered network: bulk work deferred")
        if snap.device_kind == "android" and estimated_cost > 0.6:
            return GovernorDecision("reroute", "heavy task better on laptop/cloud")
        if demand_reason is not None:
            return GovernorDecision("defer", demand_reason)
        return GovernorDecision("proceed", "within budget")
