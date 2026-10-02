"""Policy engine — the policy/permission contract. Independent of the LLM."""
from __future__ import annotations
import re
import uuid
from datetime import datetime, timedelta
from typing import Optional
from .models import PolicyDecision, RiskLevel, utcnow

# Even if a tool is mislabelled safe, these request shapes are denied/held.
DENY_PATTERNS = [
    re.compile(r"\brm\s+-rf\s+/(?:\s|$)"),
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r":\(\)\s*\{\s*:\|\:"),
    re.compile(r"\b(passwd|shadow)\b"),
    re.compile(r"\.ssh/id_"),
]
HIGH_RISK_HINTS = [re.compile(r"\b(delete|destroy|drop|format|wipe)\b", re.I)]

class PolicyEngine:
    def __init__(self) -> None:
        self._grants: dict[str, dict] = {}

    def grant(self, who: str, capability: str, device_id: str = "*",
              resource: str = "*", ttl_s: int = 300) -> str:
        gid = f"grant-{uuid.uuid4().hex[:12]}"
        self._grants[gid] = {"who": who, "capability": capability,
                             "device_id": device_id, "resource": resource,
                             "expires_at": utcnow() + timedelta(seconds=ttl_s),
                             "used": False}
        return gid

    def _live_grant(self, who: str, capability: str, device_id: str,
                    resource: str) -> Optional[str]:
        now = utcnow()
        for gid, g in self._grants.items():
            if g["used"] or g["expires_at"] < now:
                continue
            if g["who"] != who or g["capability"] != capability:
                continue
            if g["device_id"] not in ("*", device_id):
                continue
            if g["resource"] not in ("*", resource):
                continue
            return gid
        return None

    def decide(self, who: str, capability: str, device_id: str = "*",
               resource: str = "*", risk: str | RiskLevel = RiskLevel.SAFE,
               context: Optional[dict] = None) -> PolicyDecision:
        context = context or {}
        text = f"{capability} {resource} {context.get('command', '')}"
        if any(p.search(text) for p in DENY_PATTERNS):
            return PolicyDecision(allow=False, requires_approval=True,
                                  scope=f"{capability}@{device_id}:{resource}",
                                  reason="deny-pattern: destructive request held for approval")
        if isinstance(risk, str):
            risk = RiskLevel(risk)
        if risk == RiskLevel.HIGH_RISK or any(p.search(text) for p in HIGH_RISK_HINTS):
            return PolicyDecision(allow=False, requires_approval=True,
                                  scope=f"{capability}@{device_id}:{resource}",
                                  reason="high-risk: explicit human approval required")
        if risk == RiskLevel.CONFIRM:
            gid = self._live_grant(who, capability, device_id, resource)
            if gid:
                self._grants[gid]["used"] = True
                return PolicyDecision(allow=True, grant_id=gid,
                                      scope=f"{capability}@{device_id}:{resource}",
                                      reason="scoped grant consumed")
            return PolicyDecision(allow=False, requires_approval=True,
                                  scope=f"{capability}@{device_id}:{resource}",
                                  reason="confirm: approval required")
        return PolicyDecision(allow=True,
                              scope=f"{capability}@{device_id}:{resource}",
                              reason="safe: allowed")
