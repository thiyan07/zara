"""Stage 31-P3 phase B (P3b): the clipboard mission flow.

Phase A shipped the bridge surface (`android.clipboard.set`, verbatim
re-read compare, length-only result, honest focus refusals). This
flow is the CONVERSATIONAL piece the plan marked "TODO phase B":

- "copy <text> to my phone" parses the payload once, at the invite
  mouth, and the FULL TEXT rides the approval card (the only place
  it is ever shown again — result and audit rows carry lengths only);
- the confirm step re-shows the text for a verbal yes/no before any
  dispatch (clipboard contents are user-visible secrets: a price, a
  password, a redemption code);
- Android 10+ focus requirements ride the existing bridge honestly;
  a focus refusal is reported as a refusal, never retried silently.
"""
from __future__ import annotations

import re
import time
from threading import Lock

from core.models import ExecutionState

_INVITE_RE = re.compile(
    r"^\s*(?:copy|put)\s+(?P<text>.+?)\s+(?:to|on) my (?:phone|android)"
    r"(?:\s+clipboard)?\s*[,.! ]*$", re.I)
_CONFIRM_RE = re.compile(r"^\s*(?:yes|yep|yeah|sure|ok|okay|go ahead)"
                         r"\b[,.! ]*$", re.I)
_NO_RE = re.compile(r"^\s*(?:no|nope|no thanks|cancel|never mind|forget it)"
                    r"\b[,.! ]*$", re.I)
_MAX_TEXT = 4096


class _ClipPending:
    __slots__ = ("phase", "text", "exec_id", "created")

    def __init__(self) -> None:
        self.phase = ""
        self.text = ""
        self.exec_id = ""
        self.created = 0.0


class ClipboardFlow:
    """P3b deterministic flow: invite (text captured) → verbal confirm
    → parked per-run write → next turn reports the verbatim-compare
    verdict. The text is shown at the invite and the confirm; after
    that only its LENGTH travels."""

    def __init__(self, fabric, engine, audit) -> None:
        self.fabric = fabric
        self.engine = engine
        self.audit = audit
        self._pending: _ClipPending | None = None
        self._lock = Lock()

    def wants_turn(self, clean: str) -> bool:
        s = (clean or "").strip()
        if self._pending:
            return True
        return _INVITE_RE.match(s) is not None

    def _audit(self, action: str, who: str, detail: str = "") -> None:
        try:
            self.audit.record("clip_flow", action, who, detail[:300])
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
        if pend and time.time() - pend.created > 600.0:
            self._pending = None
            self._audit("clip_flow.ttl_expired", who, "")
            pend = None
        if pend is not None:
            return self._drain(s, low, pend, who, requester_device)
        m = _INVITE_RE.match(s)
        if m is None:
            return None
        text = (m.group("text") or "").strip().strip('"').strip()
        if not text or len(text) > _MAX_TEXT:
            self._audit("clip_flow.bad_text", who, f"len={len(text)}")
            return {"message":
                    "Clipboard payloads are bounded to 4096 "
                    "characters — say 'copy <text> to my phone' "
                    "again with a shorter payload.",
                    "proposal": None, "resolution": None,
                    "executed": False}
        with self._lock:
            p = _ClipPending()
            p.phase = "confirm"
            p.text = text
            p.created = time.time()
            self._pending = p
        self._audit("clip_flow.captured", who, f"len={len(text)}")
        shown = text if len(text) <= 200 else text[:200] + "…"
        return {"message":
                f"Put this on the phone's clipboard? “{shown}” — "
                "say 'yes' to write it, 'no' to cancel. (The text "
                "stays between us: results and audit rows carry "
                "only its length.)",
                "proposal": None, "resolution": None,
                "executed": False, "clipboard_flow": "confirm",
                "clipboard_flow_pending": True}

    def _drain(self, s: str, low: str, pend: _ClipPending, who: str,
               requester_device: str) -> dict:
        if _NO_RE.match(low):
            self._pending = None
            self._audit("clip_flow.cancelled", who, "len=%d" % len(pend.text))
            return {"message": "Cancelled — the phone's clipboard is "
                               "untouched.",
                    "proposal": None, "resolution": None,
                    "executed": False, "clipboard_flow": "closed"}
        if pend.phase == "confirm":
            if _CONFIRM_RE.match(low):
                phone = self._pick_phone(requester_device)
                if phone.get("ask") and not phone.get("phones"):
                    self._audit("clip_flow.no_phone", who, "")
                    self._pending = None
                    return {"message":
                            "No online Android to write the clip "
                            "to — say 'copy … to my phone' again "
                            "once one is online.",
                            "proposal": None, "resolution": None,
                            "executed": False}
                rec = self.engine.submit(
                    "android.clipboard.set",
                    {"text": pend.text, "confirm": True},
                    phone["device_id"], who=who)
                pend.phase = "write"
                pend.exec_id = rec.id
                self._audit("clip_flow.submit", who, rec.id)
                if rec.state == ExecutionState.WAITING_FOR_PERMISSION:
                    return {"message":
                            "Approval parked: writing the clip to "
                            "the phone (the card shows the full "
                            "text; the result will carry its "
                            "length and the verbatim-compare "
                            "verdict only).",
                            "proposal": None, "resolution": None,
                            "executed": True,
                            "execution_id": rec.id,
                            "approval_required": True,
                            "clipboard_flow": "pending"}
                return self._report(rec, who)
            # any other free text never becomes clipboard content for
            # a phase the user has not seen the offer of
            self._audit("clip_flow.drain_mouth", who, "len=0")
            return {"message":
                    "Say 'yes' to write that clip, or 'no' to "
                    "cancel — a new payload needs a fresh "
                    "'copy … to my phone'.",
                    "proposal": None, "resolution": None,
                    "executed": False, "clipboard_flow": "confirm"}
        # phase == write: report the terminal record
        rec = self.engine.get(pend.exec_id)
        if rec.state in (ExecutionState.PENDING, ExecutionState.RUNNING):
            return {"message":
                    "Still working — the clip write is on the "
                    "phone (approval or write in flight).",
                    "proposal": None, "resolution": None,
                    "executed": False, "clipboard_flow": "wait"}
        return self._report(rec, who)

    def _report(self, rec, who: str) -> dict:
        self._pending = None
        if rec.state != ExecutionState.SUCCEEDED:
            self._audit("clip_flow.not_written", who, str(rec.state))
            reason = ""
            try:
                reason = str(rec.error or "")
            except Exception:
                reason = ""
            return {"message":
                    "The clipboard write didn't complete "
                    f"({str(rec.state)}) — the clip is unchanged. "
                    + ("The phone reports: " + reason[:120]
                       if reason else ""),
                    "proposal": None, "resolution": None,
                    "executed": False, "clipboard_flow": "closed"}
        out = rec.result or {}
        self._audit("clip_flow.reported", who,
                    f"len={out.get('text_len', '?')}")
        return {"message":
                "Clipboard written — the phone re-read it verbatim, "
                "so what you paste IS what we intended. The text "
                "lives only on the phone's clipboard now.",
                "proposal": None, "resolution": None,
                "executed": True, "verified": True,
                "clipboard_flow": "written"}
