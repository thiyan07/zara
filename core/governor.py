"""Battery/resource governor + re-exports (notifications/audit live in their
own modules per architecture; imported here for convenience)."""
from __future__ import annotations
from dataclasses import dataclass
from .audit import AuditLog
from .notifications import NotificationManager

__all__ = ["AuditLog", "NotificationManager", "ResourceSnapshot",
           "GovernorDecision", "ResourceGovernor"]

@dataclass
class ResourceSnapshot:
    battery_pct: float | None = None
    charging: bool = False
    network: str = "unknown"  # offline|metered|wifi|wired|unknown
    device_kind: str = "cloud"
    cpu_pressure: float = 0.0
    low_power_mode: bool = False

@dataclass
class GovernorDecision:
    action: str  # proceed|defer|reroute
    reason: str

class ResourceGovernor:
    """Battery-first execution gate. Consulted before running non-trivial tools."""
    LOW_BATTERY = 15.0
    CAUTION_BATTERY = 30.0

    def check(self, snap: ResourceSnapshot, estimated_cost: float = 0.1,
              risk: str = "safe") -> GovernorDecision:
        if snap.low_power_mode and estimated_cost > 0.2:
            return GovernorDecision("defer", "low-power mode: deferring costly work")
        if snap.battery_pct is not None and not snap.charging:
            if snap.battery_pct < self.LOW_BATTERY:
                if estimated_cost > 0.2 or risk != "safe":
                    return GovernorDecision("defer", f"battery {snap.battery_pct}%: defer")
            elif snap.battery_pct < self.CAUTION_BATTERY and estimated_cost > 0.5:
                return GovernorDecision("defer", f"battery {snap.battery_pct}%: heavy task deferred")
        if snap.network in ("offline",) and estimated_cost > 0.3:
            return GovernorDecision("defer", "offline: heavy work waits for connectivity")
        if snap.network == "metered" and estimated_cost > 0.6:
            return GovernorDecision("defer", "metered network: bulk work deferred")
        if snap.device_kind == "android" and estimated_cost > 0.6:
            return GovernorDecision("reroute", "heavy task better on laptop/cloud")
        return GovernorDecision("proceed", "within budget")
