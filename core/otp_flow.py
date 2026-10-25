"""Stage 31-P3 phase B (P3f): OTP reading with per-use consent.

Exactly one fresh-scan per approval, structured so the ambient state
of the system can NEVER carry the consent:

- the tool def is HIGH_RISK×CONFIRM (policy's existing invariant: a
  high-risk tool parks for approval EVERY run — no auto-allow path);
- the flow keeps a ring-of-one pending exec (10-min TTL,
  machine-local) and drains the terminal record at the next turn;
- the CODE surfaces ONLY in the asking turn: the flow never writes
  memory, and its audit rows carry counts/ids only (fixture-pinned);
- freshness is enforced at the device (<= max_age_ms in the ring); a
  zero-count scan is an honest answer, never a retry or a guess.
- the executor is picked exactly like the call flow picks its phone:
  an ONLINE android, requester first; 0 = honest none, >1 = ask.
"""
from __future__ import annotations

import re
import time
from threading import Lock

from core.models import ExecutionState

_OTP_INVITE_RE = re.compile(
    r"^\s*(?:read|get|find|give|tell|whats|what's|is\s+there)\s+(?:my\s+|the\s+)?"
    r"(?:otp\b|one[- ]?time\s+code\b|verification\s+code\b|verify\s+code\b|"
    r"passcode\b|code\b)",
    re.I)
_OTP_HINT_RE = re.compile(r"\s+(?:from|for)\s+([a-z0-9._\-]{1,40})\s*$", re.I)
_YES_RE = re.compile(r"^\s*(?:yes|yep|yeah|sure|ok|okay|go ahead)\b[,.! ]*$",
                     re.I)
_NO_RE = re.compile(r"^\s*(?:no|nope|no thanks|cancel|never mind|forget it)"
                    r"\b[,.! ]*$", re.I)
_MAX_AGE_MS = 300_000
_TTL_S = 600.0


class _OtpPending:
    __slots__ = ("exec_id", "created", "sender_hint", "device_id")

    def __init__(self) -> None:
        self.exec_id = ""
        self.created = 0.0
        self.sender_hint = ""
        self.device_id = ""


class OtpFlow:
    """P3f deterministic flow: invite → parked read → next turn reports
    the fresh code (or the honest absence). No memory writes; the
    flow's audit rows carry counts/ids only."""

    def __init__(self, fabric, engine, audit) -> None:
        self.fabric = fabric
        self.engine = engine
        self.audit = audit
        self._pending: _OtpPending | None = None
        self._lock = Lock()

    def wants_turn(self, clean: str) -> bool:
        s = (clean or "").strip()
        if self._pending:
            return True
        return _OTP_INVITE_RE.match(s) is not None

    def _audit(self, action: str, who: str, detail: str = "") -> None:
        try:
            self.audit.record("otp_flow", action, who, detail[:300])
        except Exception:  # noqa: BLE001 — audit is never load-bearing
            pass

    def _pick_phone(self, requester_device: str) -> dict:
        cands = []
        for v in self.fabric.list_devices():
            dtype = str(getattr(v, "device_type",
                                getattr(v, "kind", ""))).lower()
            if "android" not in dtype:
                continue
            if str(getattr(v, "presence", "")) != "online":
                continue
            cands.append(getattr(v, "device_id", None) or "")
        cands = [c for c in cands if c]
        if requester_device and requester_device in cands:
            return {"device_id": requester_device}
        if len(cands) == 1:
            return {"device_id": cands[0]}
        return {"device_id": "", "ask": True, "phones": cands[:10]}

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        s = (clean or "").strip()
        low = s.lower()
        pend = self._pending
        if pend and time.time() - pend.created > _TTL_S:
            self._pending = None
            self._audit("otp_flow.ttl_expired", who, "")
            pend = None
        if pend is not None:
            return self._drain(s, pend, who)
        m = _OTP_INVITE_RE.match(s)
        if m is None:
            if not _YES_RE.match(low) and not _NO_RE.match(low):
                self._audit("otp_flow.fallthrough", who, s[:60])
            return None
        phone = self._pick_phone(requester_device)
        if phone.get("ask") and not phone.get("phones"):
            self._audit("otp_flow.no_phone", who, "")
            return {"message":
                    "No online Android to read the code from — the "
                    "OTP scan reads the PHONE's notification ring, "
                    "and none is online right now.",
                    "proposal": None, "resolution": None,
                    "executed": False}
        hint_m = _OTP_HINT_RE.search(s)
        hint = hint_m.group(1) if hint_m else ""
        rec = self.engine.submit(
            "android.otp.read",
            {"max_age_ms": _MAX_AGE_MS,
             **({"sender_hint": hint} if hint else {})},
            phone["device_id"], who=who)
        self._audit("otp_flow.submit", who, rec.id)
        if rec.state == ExecutionState.WAITING_FOR_PERMISSION:
            with self._lock:
                p = _OtpPending()
                p.exec_id = rec.id
                p.created = time.time()
                p.sender_hint = hint
                p.device_id = phone["device_id"]
                self._pending = p
            return {"message":
                    "I need your approval to read a fresh one-time "
                    "code from the phone's notifications (fresh "
                    "window: 5 minutes; I read the code, never the "
                    "full message, and I never store it). Approve "
                    "it there, then tell me anything and I'll read "
                    "the scan back.",
                    "proposal": None, "resolution": None,
                    "executed": True, "execution_id": rec.id,
                    "approval_required": True, "otp_flow": "pending"}
        if rec.state == ExecutionState.DENIED:
            self._audit("otp_flow.denied", who, "")
            return {"message": "The OTP read was denied before "
                               "dispatch — nothing was read.",
                    "proposal": None, "resolution": None,
                    "executed": False, "otp_flow": "denied"}
        return self._report(rec, who)

    def _drain(self, s: str, pend: _OtpPending, who: str) -> dict:
        low = s.lower().strip()
        if _NO_RE.match(low):
            self._pending = None
            self._audit("otp_flow.cancelled", who, "")
            return {"message": "Cancelled — I won't read the scan "
                               "back; approve it next time you "
                               "actually need it.",
                    "proposal": None, "resolution": None,
                    "executed": False, "otp_flow": "closed"}
        rec = self.engine.get(pend.exec_id)
        if rec.state in (ExecutionState.PENDING, ExecutionState.RUNNING):
            return {"message": "Still working — the scan is running "
                               "on the phone. Say anything in a "
                               "moment and I'll read it back.",
                    "proposal": None, "resolution": None,
                    "executed": False, "otp_flow": "wait"}
        return self._report(rec, who)

    def _report(self, rec, who: str) -> dict:
        self._pending = None
        out = rec.result or {}
        try:
            count = int(out.get("count", 0))
        except Exception:
            count = 0
        if rec.state != ExecutionState.SUCCEEDED:
            self._audit("otp_flow.not_run", who, str(rec.state))
            return {"message":
                    f"The OTP read didn't run ({str(rec.state)}) — "
                    "nothing was read.",
                    "proposal": None, "resolution": None,
                    "executed": False, "otp_flow": "closed"}
        cands = out.get("candidates") or []
        if count <= 0 or not cands:
            self._audit("otp_flow.zero_fresh", who, "count=0")
            return {"message":
                    "No one-time code posted in the fresh window "
                    "(last 5 minutes) — I'd rather tell you that "
                    "than guess. When it arrives, say 'read the "
                    "code' again.",
                    "proposal": None, "resolution": None,
                    "executed": True, "otp_flow": "empty"}
        lines = []
        for c in cands[:3]:
            age = int(c.get("age_ms", 0) or 0) // 1000
            app = str(c.get("package", "?")).split(".")[-1]
            code = str(c.get("code", ""))
            if not code:
                continue
            lines.append(f"{code} (from {app}, " +
                         (f"{age}s old" if age > 5 else "just now") + ")")
        if not lines:
            self._audit("otp_flow.zero_fresh", who, "count=%d" % count)
            return {"message":
                    "The scan found notifications but no usable "
                    "code shape among them — I won't invent one. "
                    "Say 'read the code' again after the message "
                    "arrives.",
                    "proposal": None, "resolution": None,
                    "executed": True, "otp_flow": "empty"}
        self._audit("otp_flow.reported", who, f"count={count}")
        msg = ("Fresh code" + ("s" if len(lines) > 1 else "") + ": "
               + "; ".join(lines)
               + " — surfaced only here: it never goes into my "
                 "memory or audit trail, and the execution "
                 "record carries it only until its retention TTL "
                 "clears.")
        return {"message": msg,
                "proposal": None, "resolution": None,
                "executed": True, "verified": True,
                "otp_flow": "reported"}
