"""Capability-based router — the CORE decides the device, never the LLM.

Steps: required capability -> online candidates -> policy -> governor ->
select (prefer cloud/linux over android) -> dispatch decision.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from .devices import DeviceManager
from .governor import (ResourceGovernor, ResourceSnapshot,
                         canonical_snapshot)
from .models import DeviceState
from .policy import PolicyEngine
from .tools import ToolRegistry


@dataclass
class RoutingDecision:
    device_id: Optional[str]
    action: str  # route|defer|deny
    reason: str


class Router:
    def __init__(self, devices: DeviceManager, registry: ToolRegistry,
                 policy: PolicyEngine, governor: ResourceGovernor,
                 local_device_id: str = "local-core") -> None:
        self.devices = devices
        self.registry = registry
        self.policy = policy
        self.governor = governor
        # In-process fallback: tools scoped to core-local execution
        # (supported_devices ["any"]/["cloud"]) may run on the core itself
        # when no registered device matches. Device-backed tools
        # (["linux"]/["android"]) never fall back — they deny/defer.
        self.local_device_id = local_device_id

    def route(self, tool: str, who: str = "user") -> RoutingDecision:
        try:
            definition = self.registry.get(tool)
        except KeyError:
            return RoutingDecision(None, "deny", f"unknown tool: {tool}")
        required = definition.required_capabilities or []
        candidates = [d for d in self.devices.list() if d.online]
        candidates = [d for d in candidates
                      if all(c in d.capabilities for c in required)]
        if not candidates:
            return self._route_local(tool, definition, who, required)
        # Battery-first order: cloud, linux, android. Key assembly is
        # canonical (core.scheduler.kind_device_key — identical map).
        from .scheduler import kind_device_key
        candidates.sort(key=lambda d: kind_device_key(
            d.kind.value, d.device_id))
        policy_blocked = False
        hard_refused = False
        constrained = False
        for dev in candidates:
            pol = self.policy.decide(who, capability=f"tool:{tool}",
                                     device_id=dev.device_id, resource=tool,
                                     risk=definition.risk)
            if not pol.allow:
                if pol.hard_deny:
                    hard_refused = True
                else:
                    policy_blocked = True
                if pol.hard_deny or not pol.requires_approval:
                    continue
                # confirm/high-risk hold: still routable — execution waits
                # for approval and runs only after a human approves.
            snap = canonical_snapshot(
                battery_pct=dev.battery_pct, charging=dev.charging,
                network=dev.network, device_kind=dev.kind.value,
                low_power_mode=dev.low_power_mode,
                cpu_pct=dev.cpu_pct, mem_pct=dev.mem_pct,
                storage_free_mb=dev.storage_free_mb,
                battery_temp_c=dev.battery_temp_c,
                thermal_state=dev.thermal_state)
            gate = self.governor.check(snap, definition.estimated_cost,
                                       definition.risk.value,
                                       demands=definition.resource_demands)
            if gate.action == "defer":
                constrained = True
                continue
            if gate.action == "reroute":
                constrained = True
                continue  # try a less constrained device
            return RoutingDecision(dev.device_id, "route",
                                   f"selected {dev.device_id}: {gate.reason}")
        if hard_refused:
            return RoutingDecision(
                None, "deny",
                f"policy refusal for {tool}: destructive request, not approvable")
        if policy_blocked and not constrained:
            return RoutingDecision(
                None, "deny",
                f"policy hold for {tool}: explicit approval required")
        # All capable devices deferred by governor -> defer, not deny.
        return RoutingDecision(None, "defer",
                               "all capable devices constrained (battery/offline)")

    def _route_local(self, tool: str, definition, who: str,
                     required: list[str]) -> RoutingDecision:
        """Core-local fallback for in-process tools only."""
        supported = definition.supported_devices or ["any"]
        if "any" not in supported and "cloud" not in supported:
            return RoutingDecision(
                None, "deny",
                f"no online device has capabilities {required} for {tool}")
        pol = self.policy.decide(who, capability=f"tool:{tool}",
                                 device_id=self.local_device_id,
                                 resource=tool, risk=definition.risk)
        if not pol.allow and (pol.hard_deny or not pol.requires_approval):
            if pol.hard_deny:
                return RoutingDecision(
                    None, "deny",
                    f"policy refusal for {tool}: destructive request, "
                    f"not approvable")
            return RoutingDecision(
                None, "deny",
                f"policy hold for {tool}: explicit approval required")
        # Confirm/high-risk holds stay routable — execution waits for approval.
        gate = self.governor.check(canonical_snapshot(),
                                   definition.estimated_cost,
                                   definition.risk.value)
        if gate.action != "proceed":
            return RoutingDecision(None, "defer", gate.reason)
        if pol.requires_approval:
            return RoutingDecision(self.local_device_id, "route",
                                   f"selected {self.local_device_id} "
                                   f"(approval required before execution)")
        return RoutingDecision(self.local_device_id, "route",
                               f"selected {self.local_device_id}: local tool")
