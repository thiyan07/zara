"""Stage 31-P3 phase B (P3c): pull-only photo backup.

Honest scope of this surface:
- the flow reads a COUNT-ONLY inventory (no names, no content, no
  thumbnails — the device never leaks what the user didn't ask);
- the BACKUP itself is the operator's PULL (adb from the laptop),
  consented PER RUN — every run parks the inventory approval, so no
  ambient grant can carry it and the pull plan is re-proven each time;
- DELETE NEVER: this flow deletes nothing, generates no destructive
  command, and the copy direction is phone→laptop only. Any "delete
  after backup" utterance is refused explicitly, never parsed away.
"""
from __future__ import annotations

import re
import time
from threading import Lock

from core.models import ExecutionState

_INVITE_RE = re.compile(
    r"^\s*(?:backup|pull|save|copy)\s+(?:my\s+)?(?:photos?|images?|"
    r"camera\s+(?:roll|photos))\b|^\s*(?:photo|image)\s+backup\s*",
    re.I)
_DELETE_RE = re.compile(
    r"\bdelete\b|\berase\b|\bfree up\b|\bwipe\b|\bremove\s+them\b", re.I)
_NO_RE = re.compile(r"^\s*(?:no|nope|no thanks|cancel|never mind|forget it)"
                    r"\b[,.! ]*$", re.I)
_TTL_S = 600.0


class _PhotoPending:
    __slots__ = ("exec_id", "created")

    def __init__(self) -> None:
        self.exec_id = ""
        self.created = 0.0


class PhotoFlow:
    """P3c deterministic flow: invite → parked COUNT-ONLY inventory →
    next turn reports counts + the operator's pull plan. Refuses any
    delete flavor in the invite. No memory writes; audit counts/ids
    only."""

    def __init__(self, fabric, engine, audit) -> None:
        self.fabric = fabric
        self.engine = engine
        self.audit = audit
        self._pending: _PhotoPending | None = None
        self._lock = Lock()

    def wants_turn(self, clean: str) -> bool:
        s = (clean or "").strip()
        if self._pending:
            return True
        return _INVITE_RE.match(s) is not None

    def _audit(self, action: str, who: str, detail: str = "") -> None:
        try:
            self.audit.record("photo_flow", action, who, detail[:300])
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
            self._audit("photo_flow.ttl_expired", who, "")
            pend = None
        if pend is not None:
            return self._drain(low, pend, who)
        if _DELETE_RE.search(low) and _INVITE_RE.match(s):
            self._audit("photo_flow.delete_refused", who, s[:60])
            return {
                "message":
                    "I can inventory your photos and walk through the "
                    "pull-backup, but I DELIBERATELY don't delete: "
                    "'delete after backup' is a destructive promise I "
                    "won't make — storage cleanup stays yours.",
                "proposal": None, "resolution": None,
                "executed": False, "photo_flow": "delete_refused"}
        m = _INVITE_RE.match(s)
        if m is None:
            return None
        phone = self._pick_phone(requester_device)
        if phone.get("ask") and not phone.get("phones"):
            self._audit("photo_flow.no_phone", who, "")
            return {"message":
                    "No online Android to inventory — the photo "
                    "backup plan starts on the PHONE's MediaStore, "
                    "and none is online right now.",
                    "proposal": None, "resolution": None,
                    "executed": False}
        rec = self.engine.submit("android.photos.inventory", {},
                                 phone["device_id"], who=who)
        self._audit("photo_flow.submit", who, rec.id)
        if rec.state == ExecutionState.WAITING_FOR_PERMISSION:
            with self._lock:
                p = _PhotoPending()
                p.exec_id = rec.id
                p.created = time.time()
                self._pending = p
            return {"message":
                    "I need your approval to read a count-only "
                    "inventory of the phone's photo library (no file "
                    "names, no content — and I never delete; the "
                    "backup itself is a pull you consent to per "
                    "run). Approve it there, then tell me anything "
                    "and I'll read the inventory back with the pull "
                    "plan.",
                    "proposal": None, "resolution": None,
                    "executed": True, "execution_id": rec.id,
                    "approval_required": True, "photo_flow": "pending"}
        if rec.state == ExecutionState.DENIED:
            self._audit("photo_flow.denied", who, "")
            return {"message": "The photo inventory was denied "
                               "before dispatch — nothing was read.",
                    "proposal": None, "resolution": None,
                    "executed": False, "photo_flow": "denied"}
        return self._report(rec, who)

    def _drain(self, low: str, pend: _PhotoPending, who: str) -> dict:
        if _NO_RE.match(low):
            self._pending = None
            self._audit("photo_flow.cancelled", who, "")
            return {"message": "Cancelled — no inventory read; the "
                               "pull stays yours.",
                    "proposal": None, "resolution": None,
                    "executed": False, "photo_flow": "closed"}
        rec = self.engine.get(pend.exec_id)
        if rec.state in (ExecutionState.PENDING, ExecutionState.RUNNING):
            return {"message": "Still working — the inventory scan "
                               "is running on the phone.",
                    "proposal": None, "resolution": None,
                    "executed": False, "photo_flow": "wait"}
        return self._report(rec, who)

    def _report(self, rec, who: str) -> dict:
        self._pending = None
        if rec.state != ExecutionState.SUCCEEDED:
            self._audit("photo_flow.not_run", who, str(rec.state))
            return {"message":
                    f"The inventory didn't run ({str(rec.state)}) — "
                    "nothing was read.",
                    "proposal": None, "resolution": None,
                    "executed": False, "photo_flow": "closed"}
        out = rec.result or {}
        count = out.get("count", -1)
        if not isinstance(count, int) or count < 0:
            self._audit("photo_flow.bad_shape", who, "count")
            return {"message":
                    "The inventory came back malformed — I won't "
                    "build a backup plan on a guess.",
                    "proposal": None, "resolution": None,
                    "executed": True, "photo_flow": "closed"}
        if count == 0:
            self._audit("photo_flow.zero", who, "count=0")
            return {"message":
                    "The phone's photo library is empty (or the "
                    "MediaStore scan found nothing) — nothing to "
                    "pull. When photos exist, say 'backup my "
                    "photos' again.",
                    "proposal": None, "resolution": None,
                    "executed": True, "photo_flow": "empty"}
        total_gb = (out.get("total_bytes", 0) or 0) / 1_000_000_000.0
        folders = " + ".join(out.get("folders") or []) or "camera roll"
        self._audit("photo_flow.reported", who, f"count={count}")
        return {"message":
                f"Inventory (counts only): {count} photos, "
                f"~{total_gb:.2f} GB under {folders}. The backup is "
                "a PULL from your laptop operator seam, consented "
                "per run — from the laptop: `adb pull /sdcard/DCIM/ "
                "backups/photos/` (folders only; I never see file "
                "names). I don't delete anything, ever; cleanup "
                "stays yours.",
                "proposal": None, "resolution": None,
                "executed": True, "verified": True,
                "photo_flow": "reported"}
