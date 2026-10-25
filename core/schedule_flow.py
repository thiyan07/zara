"""Meeting scheduler (P6-b30, 2026-10-24) — propose slots, book ONLY
on the user's pick, and only through the engine's CONFIRM gate.

The open-source interface reference (design only, never their code):
MarimerLLC/scheduler — the organizer proposes candidate times that
already work for THEM, invites vote/pick, and only a picked time is
created. Here the pick plus the tool's own approval IS the ask-first
gate: booking happens in three deliberate states

  propose (slots computed from the calendar) →
  pick ("take slot 2" / "book it at 14:30") →
  engine.submit("calendar.event.add", … confirm=True) →
  the CONFIRM approval in the app (never auto-approved).

Slot computation is CalendarFlow's TESTED machine: `free_windows`
was extracted from _answer_free_slots so the free-ask chat surface
and the scheduler always show the same day picture (one computation,
two surfaces — never a second implementation). Proposals cap at 5
with "+N more"; a pick re-checks freshness deterministically (the
stored slot may be consumed or past — the flow refuses and re-asks,
never books a stale window silently).
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from typing import Optional

_MIN = 15
_MAX = 120


class ScheduleProposeError(ValueError):
    """User-facing clarify on malformed schedule asks."""


class ScheduleFlow:
    """Turn-shaped deterministic surface over engine + calendar."""

    _PROPOSE_RE = re.compile(
        r"\b(?:schedule|book|set up|plan)\s+(?:a\s+)?meeting\b|"
        r"\bmeeting\b.{0,40}\b(?:tomorrow|today|next week|monday|"
        r"tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
        r"\b(?:propose|suggest)\s+(?:meeting\s+)?(?:times|slots)\b|"
        r"\b(?:find|does)\s+(?:me\s+)?(?:a\s+)?(?:time|slot)\b"
        r".{0,20}\bmeeting\b", re.I)

    _PICK_RE = re.compile(
        r"\b(?:take|pick|choose|book|use)\b\s*(?:slot\s*)?"
        r"|#?(?P<n>\d{1,2})\b|\bat\s+(?P<h>\d{1,2}(?::\d{2})?)\s*"
        r"(?P<mer>[ap]\.?m\.?)?\b|^\s*book it\b|^\s*yes,? book\b")
    _DIRECT_PICK_RE = re.compile(
        r"^\s*(?:take|pick|choose|book|use)(?:\s+(?:the|that))?"
        r"(?:\s+slot)?\s*#?(?P<n>\d{1,2})\s*\.*$|"
        r"^\s*(?:book|use)\s+it\s+(?:at\s+)?(?P<h>\d{1,2})("
        r"?::(?P<m>\d{2}))?\s*(?P<mer>[ap]\.?m\.?)?\s*\.*$|"
        r"^\s*book it\b|^\s*(?:yes|confirm),? (?:book|take)", re.I)

    _CANCEL_RE = re.compile(
        r"^\s*(?:cancel|forget(?: it)?|never\s?mind|no\b|stop)\b", re.I)

    _DUR_RE = re.compile(r"\bfor\s+(\d{1,3})\s*(minutes?|mins?|"
                         r"hours?|hrs?)\b", re.I)

    _GUARD_RE = re.compile(
        r"\b(remind|reminder|grocer|groceries|water|expense|"
        r"capture|inbox)\b", re.I)

    def __init__(self, engine, calendar_flow, devices, audit) -> None:
        self.engine = engine
        self.calendar_flow = calendar_flow
        self.devices = devices
        self.audit = audit
        self._pending: Optional[dict] = None
        self._lock = threading.Lock()

    def _log(self, action: str, who: str, detail: str = "") -> None:
        try:
            if self.audit is not None:
                self.audit.record("schedule", action, who,
                                  detail[:200])
        except Exception:  # noqa: BLE001 — audit never breaks flows
            pass

    # -- NL anchoring ----------------------------------------------------

    def wants_turn(self, clean: str) -> bool:
        c = (clean or "").strip()
        if self._GUARD_RE.search(c):
            return False
        if self._pending is not None:
            return True          # continuation control belongs here
        return bool(self._PROPOSE_RE.search(c))

    # -- scope / duration / title ---------------------------------------

    def _scope_day(self, clean: str):
        """The asked day — CalendarFlow's own day-token comprehension
        (never a second date grammar). Fallback: today."""
        try:
            ds = self.calendar_flow._day_scope(clean)
        except Exception:  # noqa: BLE001
            ds = None
        if ds and ds[0] is not None:
            return ds[0], ds[1]
        now = datetime.now().astimezone()
        return now.date(), "today"

    def _duration(self, clean: str) -> int:
        m = self._DUR_RE.search(clean)
        if not m:
            return 30
        n = int(m.group(1))
        if m.group(2).lower().startswith(("hour", "hr")):
            n *= 60
        if n < _MIN or n > _MAX:
            raise ScheduleProposeError(
                "Meeting length must be 15–120 minutes (say e.g. "
                "'for 30 minutes').")
        return n

    def _title(self, clean: str) -> str:
        m = re.search(
            r"\b(?:meeting|call)\b(?:\s+(?:about|for|on|named|to "
            r"discuss))?\s+([A-Za-z0-9][A-Za-z0-9 ,'\-]{2,79})",
            clean)
        if not m:
            return ""
        name = m.group(1)
        # day words and durations are scope tokens, not a title
        name = re.sub(
            r"\b(?:tomorrow|today|next week|monday|tuesday|"
            r"wednesday|thursday|friday|saturday|sunday)\b", "",
            name, flags=re.I)
        name = re.sub(r"\bfor\s+\d{1,3}\s*(?:minutes?|mins?|hours?|"
                      r"hrs?)\b.*$", "", name, flags=re.I)
        name = re.sub(r"^(?:about|for|on|named|to discuss|discuss)\b",
                      "", name, flags=re.I)
        name = re.sub(r"^[,.\s]+|[,.\s]+$", "", name)
        return name[:80]

    # -- propose / pick ---------------------------------------------------

    def _propose(self, clean: str, who: str) -> dict:
        try:
            duration = self._duration(clean)
        except ScheduleProposeError as e:
            return {"message": str(e), "executed": False,
                    "clarified": True}
        target, label = self._scope_day(clean)
        title = self._title(clean) or "Meeting"
        slots = self.calendar_flow.free_windows(target, duration)
        if not slots:
            return {"message": f"No {duration}-minute window is "
                               f"free {label} "
                               f"(08:00–22:00 local). Try another "
                               f"day or a shorter length; nothing "
                               f"was booked.",
                    "executed": False, "schedule": "no_slots"}
        shown = slots[:5]
        lines = "; ".join(
            f"{i + 1}. {s.strftime('%H:%M')}–"
            f"{e.strftime('%H:%M')}"
            for i, (s, e) in enumerate(shown))
        more = f" (+{len(slots) - 5} more)" if len(slots) > 5 else ""
        with self._lock:
            self._pending = {"title": title, "target": target,
                             "label": label, "duration": duration,
                             "slots": slots}
        self._log("schedule_proposed", who,
                  f"day={target} dur={duration} n={len(slots)}")
        return {"message": (
                    f"Free {duration}-minute windows {label}: "
                    f"{lines}{more}. Which one? Say e.g. "
                    f"'take slot 2' or 'book it at "
                    f"{shown[0][0].strftime('%H:%M')}'.")
                + (" I will propose only — nothing is booked until "
                   "you pick, and the booking still needs its "
                   "approval."),
                "executed": False, "schedule": "proposed",
                "slots": len(slots)}

    def _book(self, pick: dict, who: str) -> dict:
        """Book THE PICKED slot through the engine's CONFIRM gate."""
        with self._lock:
            pend = self._pending
        if pend is None:
            return None
        target = pend["target"]
        slots = pend["slots"]
        idx = pick.get("n")
        if idx is not None:
            if idx < 1 or idx > len(slots):
                return {"message": f"I proposed {len(slots)} slot(s) "
                                   f"{pend['label']}; which number? "
                                   f"Nothing was booked.",
                        "executed": False, "schedule": "clarify"}
            slot = slots[idx - 1]
        else:
            want = pick.get("minutes")
            if want is None:
                return {"message": "Say which slot: e.g. 'take slot "
                                   "2' or 'book it at 14:30'.",
                        "executed": False, "schedule": "clarify"}
            slot = next(((s, e) for (s, e) in slots
                         if s.hour * 60 + s.minute <= want
                         < e.hour * 60 + e.minute), None)
            if slot is None:
                return {"message": "That time is not one of the "
                                   "proposed windows — say 'take "
                                   "slot 2' against the list, or a "
                                   "time inside one. Nothing was "
                                   "booked.",
                        "executed": False, "schedule": "clarify"}
        s, e = slot
        # an at-time pick anchors AT THE PICKED TIME (inside the
        # window), never at the window's start — "book it at 09:30"
        # into a 08:00–22:00 window books 09:30.
        if pick.get("minutes") is not None:
            want = pick["minutes"]
            anchor = s.replace(hour=want // 60, minute=want % 60)
            if not (s <= anchor and
                    anchor + timedelta(minutes=pend["duration"]) <= e):
                return {"message": "That time does not fit the "
                                   "proposed window — pick a time with "
                                   "enough room before the window "
                                   "ends. Nothing was booked.",
                        "executed": False, "schedule": "clarify"}
            s = anchor
        # freshness: a stored slot is only honored while it is in the
        # future; a stale proposal re-proposes honestly
        if s <= datetime.now().astimezone():
            self._log("schedule_slot_stale", who, pend["label"])
            return {"message": "That window has already passed — "
                               "propose again to get fresh slots.",
                    "executed": False, "schedule": "stale"}
        from datetime import timezone as _tz
        # calendar.event.add wants 2028XXXX(TXXXXXXZ) — UTC keys from
        # the tz-aware local slots
        end = min(e, s + timedelta(minutes=pend["duration"]))
        start_key = s.astimezone(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
        end_key = end.astimezone(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
        # the pick is actionable NOW — only here is the pending
        # proposal consumed (a refusal must keep the list; observed
        # live 2026-10-25: a border-time refusal consumed the pending
        # and the refusal's own follow-up advice stopped working)
        with self._lock:
            self._pending = None
        dev = self.devices.select(["calendar.write"])
        device_id = dev.device_id if dev is not None else "cloud"
        self._log("schedule_book_submitted", who,
                  f"day={target} start={start_key} dur="
                  f"{pend['duration']} dev={device_id}")
        rec = self.engine.submit(
            "calendar.event.add",
            {"summary": pend["title"][:120], "dtstart": start_key,
             "dtend": end_key, "confirm": True},
            device_id, who=who)
        waiting = str(rec.state).endswith("WAITING_FOR_PERMISSION")
        if waiting:
            return {"message":
                    f"Submitted '{pend['title'][:60]}' for "
                    f"{pend['label']} "
                    f"{s.strftime('%H:%M')}–"
                    f"{end.strftime('%H:%M')} — approve it on "
                    f"{device_id}; I'll report what was actually "
                    f"written.",
                    "executed": True, "execution_id": rec.id,
                    "approval_required": True,
                    "schedule": "booked_pending"}
        return {"message": f"The calendar add didn't park for "
                           f"approval ({str(rec.state)[:40]}).",
                "executed": False, "schedule": "not_parked"}

    def _parse_pick(self, clean: str) -> Optional[dict]:
        m = self._DIRECT_PICK_RE.match(clean)
        if m:
            if m.group("n") is not None:
                return {"n": int(m.group("n"))}
            hraw = m.group("h")
            if hraw is None:
                return {"book_it": True}
            h = int(hraw)
            mm = int(m.group("m") or 0)
            mer = (m.group("mer") or "").replace(".", "")
            if not (0 <= h <= 23 and 0 <= mm <= 59):
                return None
            if mer == "pm" and h < 12:
                h += 12
            if mer == "am" and h == 12:
                h = 0
            return {"minutes": h * 60 + mm}
        m2 = re.match(r"^\s*(?:take|pick|choose)(?:\s+slot)?\s+"
                      r"(\d{1,2})\s*\.*$", clean, re.I)
        if m2:
            return {"n": int(m2.group(1))}
        m3 = re.match(r"^\s*(?:book|take)\s+it\s+at\s+(\d{1,2}):"
                      r"(\d{2})(?:\s*([ap])\.?m\.?)?\s*\.*$", clean,
                      re.I)
        if m3:
            h = int(m3.group(1))
            mm = int(m3.group(2))
            if m3.group(3) and h < 12:
                pass        # 'am/pm' helper handled below
            return self._minutes(h, mm, (m3.group(3) or ""))
        m4 = re.match(r"^\s*(?:book|take)\s+it\s+at\s+(\d{1,2})\s*"
                      r"([ap])\.?m\.?\s*\.*$", clean, re.I)
        if m4:
            return self._minutes(int(m4.group(1)), 0, m4.group(2))
        if re.match(r"^\s*(?:yes|confirm|ok)\b.*\bbook\b|^\s*book "
                    r"it\b", clean, re.I):
            return {"book_it": True}
        return None

    @staticmethod
    def _minutes(h: int, m: int, mer: str) -> dict:
        mer = (mer or "").strip().lower()
        if mer == "p" and h < 12:
            h += 12
        if mer == "a" and h == 12:
            h = 0
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return {"minutes": h * 60 + m}

    # -- turn -------------------------------------------------------------

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        c = (clean or "").strip()
        if self._GUARD_RE.search(c):
            return None
        with self._lock:
            has_pending = self._pending is not None
        if has_pending:
            if self._CANCEL_RE.match(c):
                with self._lock:
                    self._pending = None
                self._log("schedule_proposal_cancelled", who)
                return {"message": "Cancelled — nothing was booked "
                                   "and no proposal stays open.",
                        "executed": False, "schedule": "cancelled"}
            pick = self._parse_pick(c)
            if pick is not None:
                return self._book(pick, who)
            if self._PROPOSE_RE.search(c):
                return self._propose(c, who)     # re-propose cleanly
            return None
        if self._PROPOSE_RE.search(c):
            return self._propose(c, who)
        return None
