"""Policy engine — the policy/permission contract. Independent of the LLM."""
from __future__ import annotations
import re
import uuid
from datetime import datetime, timedelta
from typing import Optional
from .models import PolicyDecision, RiskLevel, utcnow

# Even if a tool is mislabelled safe, these request shapes are denied/held.
DENY_PATTERNS = [
    re.compile(r"\brm\s+-rf\s+/(?![\w/])"),
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r":\(\)\s*\{\s*:\|\:"),
    re.compile(r"\b(passwd|shadow)\b"),
    re.compile(r"\.ssh/id_"),
]
HIGH_RISK_HINTS = [re.compile(r"\b(delete|destroy|drop|format|wipe)\b", re.I)]

# Actions that a standing (ambient, TTL, batch) grant can NEVER unlock —
# the "never auto-approve" class (OpenDroid mining, Stage 29). Membership
# is checked on the BARE capability name, so `tool:X` / `capability:X`
# spellings hit the same rule. Enforcement shape (decide()): these
# capabilities ignore the grant store entirely and always require
# explicit per-use human approval. They are NOT hard-denied: the real
# approval paths (engine hold → approve; mission-bound approval mint)
# complete with a grant minted with source="approval" — nothing runs on
# a standing grant. Class rule for future edits: any destructive /
# irreversible-capability definition must be added here, not merely
# labelled CONFIRM — mislabels never downgrade this list.
NEVER_AUTO_APPROVE_CAPS: frozenset = frozenset({
    "android.app.force_stop",  # kills a process; only the explicit
                               # Stage-24b lab consent flow re-enables it
    "storage.delete",          # irreversible operator-data deletion
    "filesystem.delete",       # scratch deletion — same class, less stakes
    "messaging.send",          # irreversible effect on another person's
                               # inbox (record; the gateway has its own
                               # per-send approval protocol already)
    "calendar.event.add",      # P5 2026-10-23: writes operator DATA
    "calendar.event.remove",   # P5 2026-10-23: deletes operator DATA
                               # (their calendar file) — every append is
                               # its own approval, event-by-event
    "calendar.event.move",     # P5 ⑯ 2026-10-23: rewrites operator
                               # DATA (same class as remove — one move,
                               # one approval)
})

# Stage 30 (dummy mining): read-only observation family — policy-level
# prompt skip. Membership list, checked on the BARE capability name.
# Rule for future edits: ONLY join with evidence the capability cannot
# put data anywhere (writes, sends, deletes, casts, transfers — none).
# Keeping approval where it is: gui.tap/set_text/scroll/back,
# storage.delete, filesystem.delete, android.app.force_stop,
# messaging.send, clipboard write, xfer/transfer, screen-cast consent.
READ_ONLY_AUTO_ALLOW_CAPS: frozenset = frozenset({
    "gui.screen.inspect",      # accessibility snapshot; writes nothing
    "gui.find",                # selector lookup in a fresh tree
    "gui.wait",                # bounded poll for an element
    "android.notifications.read",  # notification read; read-only
})

class PolicyEngine:
    def __init__(self) -> None:
        self._grants: dict[str, dict] = {}

    def grant(self, who: str, capability: str, device_id: str = "*",
              resource: str = "*", ttl_s: int = 300,
              used_now: bool = False, mission_id: str = "",
              source: str = "standing") -> str:
        """Mint a grant. Two narrowings for minters that pass the
        resulting PolicyDecision EXPLICITLY to the dispatch they approve
        (engine approve, mission-bound messaging):
        - used_now=True: the mint itself IS the single use — no later
          decide() can ever consume a leftover approval grant for an
          unrelated request (single-turn CONFIRM tools).
        - mission_id: the grant is consumable ONLY by decide() calls
          carrying that same mission_id (plan continuation: the held
          step was approved; the SAME mission's remaining steps may
          consume it once, per the Stage 24g continuation semantics).
        - source="approval": minted by a per-use human approval path
          (engine approve, mission-bound approval). Only provenance that
          can inch a never-auto-approve action forward — and even then
          only via that path's own injected auth, never via decide()'s
          grant store. Default "standing" = ambient mint; standing grants
          never unlock NEVER_AUTO_APPROVE_CAPS.
        Default False keeps the decide()-consumed explicit-grant flow
        unchanged."""
        gid = f"grant-{uuid.uuid4().hex[:12]}"
        self._grants[gid] = {"who": who, "capability": capability,
                             "device_id": device_id, "resource": resource,
                             "expires_at": utcnow() + timedelta(seconds=ttl_s),
                             "used": used_now, "mission_id": mission_id,
                             # Provenance: "standing" (default) = any
                             # ambient mint; "approval" = minted by a
                             # per-use human approval path (engine
                             # approve / mission-bound approval). The
                             # never-auto class answers only to "approval"
                             # provenance — via the approval paths'
                             # injected auth — never to ambient mints.
                             "source": source}
        return gid

    def _live_grant(self, who: str, capability: str, device_id: str,
                    resource: str, mission_id: str = "") -> Optional[str]:
        now = utcnow()
        for gid, g in self._grants.items():
            if g["used"] or g["expires_at"] < now:
                continue
            # Mission-scoped approval grants answer only to the SAME
            # mission (continuation); unrelated traffic — including a
            # bare next request with no mission — never consumes them.
            if g.get("mission_id") and g["mission_id"] != mission_id:
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
               context: Optional[dict] = None,
               mission_id: str = "") -> PolicyDecision:
        context = context or {}
        text = f"{capability} {resource} {context.get('command', '')}"
        if any(p.search(text) for p in DENY_PATTERNS):
            return PolicyDecision(allow=False, requires_approval=True,
                                  hard_deny=True,
                                  scope=f"{capability}@{device_id}:{resource}",
                                  reason="deny-pattern: destructive request refused")
        if isinstance(risk, str):
            risk = RiskLevel(risk)
        # Never-auto class: standing grants never unlock these, whatever
        # their risk label says (mislabel protection; HIGH_RISK labels
        # are already immune below). Enforced BEFORE the risk branches so
        # a future SAFE/CONFIRM relabel cannot widen them. Approval still
        # completes — but only through the per-use approval paths, whose
        # mints carry source="approval" and dispatch via injected auth.
        bare_cap = capability.split(":", 1)[-1]
        if bare_cap in NEVER_AUTO_APPROVE_CAPS:
            return PolicyDecision(allow=False, requires_approval=True,
                                  scope=f"{capability}@{device_id}:{resource}",
                                  reason="never-auto: standing grants never "
                                  "unlock this; explicit per-use human "
                                  "approval required")
        # Stage 30 (dummy mining): harmless READ-ONLY observation is
        # policy-allowed WITHOUT a prompt. Prompting for reads trains
        # rubber-stamping, which destroys approval's value for the
        # dangerous class. Membership is explicit and POLICY-level
        # (never label-derived) so a tool relabel cannot widen the set —
        # and the never-auto check above still outranks this branch: a
        # read-only capability that joins the never-auto class parks
        # again regardless of membership here (regression guard).
        # Only observation lives here: these cannot write, send, move
        # money, or change state anywhere. DENY_PATTERNS still outrank.
        if bare_cap in READ_ONLY_AUTO_ALLOW_CAPS:
            return PolicyDecision(
                allow=True,
                scope=f"{capability}@{device_id}:{resource}",
                reason="read-only observation: policy-level allow, "
                "no approval currency spent")
        if risk == RiskLevel.HIGH_RISK or any(p.search(text) for p in HIGH_RISK_HINTS):
            return PolicyDecision(allow=False, requires_approval=True,
                                  scope=f"{capability}@{device_id}:{resource}",
                                  reason="high-risk: explicit human approval required")
        if risk == RiskLevel.CONFIRM:
            gid = self._live_grant(who, capability, device_id, resource,
                                   mission_id=mission_id)
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
