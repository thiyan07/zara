"""Weekly review (P6-b27, 2026-10-24) — one deterministic, READ-ONLY
composite answer on the ask "weekly review" / "review my week".

Pinned rules (same shape as MorningBriefFlow):
- every section is a REAL read from an existing service/flow — the
  calendar section delegates to CalendarFlow's own TESTED week digest
  ("calendar this week"), conflicts pass through conflicts_on_date
  for each day of the ISO week, reminders use ReminderService.upcoming,
  routines use RoutineService.list_rows — never a second
  implementation anywhere (R46.1);
- degradation is per-section and NAMED: a service that can't read
  shows its honest failure line, it never silently disappears;
- later user-data surfaces (capture inbox, chat ledgers) plug in as
  optional attrs — until they are wired the section says "not wired",
  it never lies about data;
- read-only: nothing here executes, writes, or approvals; a narrow
  anchor regex keeps other intents out of this flow.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

class WeeklyReviewFlow:
    """P6-b27: the weekly review — one turn, five + optional sections,
    each a real read of a tested service."""

    _INVITE_RE = re.compile(
        r"^\s*(?:weekly review|run(?: my)? weekly review"
        r"|do (?:a|my) weekly review|review my week"
        r"|my week(?:ly)? review|week(?:ly)? review"
        r"|(?:how (?:was|did) my week(?: go)?))\s*(?:,?\s*zara)?"
        r"[\s.,!?]*$",
        re.I)

    def __init__(self, audit, calendar_flow=None, reminders=None,
                 routines=None, capture=None, ledgers=None,
                 habits=None) -> None:
        self.audit = audit
        self.calendar_flow = calendar_flow
        self.reminders = reminders
        self.routines = routines
        self.capture = capture          # batch 29 wires this
        self.ledgers = ledgers          # batch 28 wires this
        self.habits = habits            # batch 32 wires this

    def _audit(self, action: str, who: str, detail: str = "") -> None:
        try:
            self.audit.record("weekly_review", action, who,
                              detail[:300])
        except Exception:  # noqa: BLE001 — audit never breaks turns
            pass

    def wants_turn(self, clean: str) -> bool:
        return self._INVITE_RE.match((clean or "").strip()) is not None

    @staticmethod
    def _dot(s: str) -> str:
        s = (s or "").strip()
        return s if s.endswith((".", "!", "?")) else (s + ".")

    # -- sections ------------------------------------------------------

    def _calendar_line(self, who: str) -> str:
        if self.calendar_flow is None:
            return "Calendar: flow not wired."
        try:
            out = self.calendar_flow.turn(
                "what's on my calendar this week", who=who)
        except Exception as e:  # noqa: BLE001 — reads are data
            return f"Calendar: week digest unavailable ({str(e)[:60]})."
        if isinstance(out, dict) and out.get("message"):
            return "Calendar: " + out["message"].strip()
        return "Calendar: week digest unavailable."

    def _conflicts_line(self) -> str:
        if self.calendar_flow is None or \
                not hasattr(self.calendar_flow, "conflicts_on_date"):
            return "Conflicts: calendar flow unavailable."
        now = datetime.now().astimezone()
        monday = now.date() - timedelta(days=now.weekday())
        total, rendered = 0, []
        for i in range(7):
            got = self.calendar_flow.conflicts_on_date(
                monday + timedelta(days=i))
            if got is None:
                return "Conflicts: conflict read failed this week."
            n, pairs = got[0], got[1]
            total += n
            rendered.extend(pairs)
        if total == 0:
            return "Conflicts: none this week."
        body = "; ".join(rendered[:3])
        if total > 3:
            body += f" +{total - 3} more"
        return f"Conflicts: {total} this week — {body}."

    def _reminders_line(self) -> str:
        if self.reminders is None:
            return ("Reminders: store not wired — the reminder flows "
                    "still work on ask.")
        try:
            up = self.reminders.upcoming()
            now = datetime.now().astimezone()
            week_ahead = (now + timedelta(days=7)).timestamp()
            soon = [r for r in up if r.get("due_epoch", 0) is not None
                    and r["due_epoch"] <= week_ahead]
        except Exception as e:  # noqa: BLE001 — reads are data
            return f"Reminders: read failed ({str(e)[:60]})."
        if not soon:
            return "Reminders: none scheduled in the next 7 days."
        first = soon[0]
        return (f"Reminders: {len(soon)} in the next 7 days; next is "
                f"'{(first.get('text') or '?')[:40]}' at "
                f"{first.get('due')}.")

    def _routines_line(self) -> str:
        if self.routines is None:
            return "Routines: not wired yet."
        try:
            rows = self.routines.list_rows()
        except Exception as e:  # noqa: BLE001 — reads are data
            return f"Routines: read failed ({str(e)[:60]})."
        if not rows:
            return "Routines: none scheduled."
        body = "; ".join(f"'{r.get('name', '?')}' "
                         f"({r.get('cadence_label', '?')}, next "
                         f"{r.get('next')})" for r in rows[:4])
        more = (f" +{len(rows) - 4} more" if len(rows) > 4 else "")
        return f"Routines: {len(rows)} scheduled — {body}{more}."

    def _capture_line(self) -> str:
        if self.capture is None:
            return "Capture inbox: not wired yet."
        try:
            n = self.capture.open_count()
        except Exception as e:  # noqa: BLE001 — reads are data
            return f"Capture inbox: read failed ({str(e)[:60]})."
        return (f"Capture inbox: {n} unsorted item(s)."
                if n else "Capture inbox: clear.")

    def _ledgers_line(self) -> str:
        if self.ledgers is None:
            return "Chats logs: not wired yet."
        try:
            line = self.ledgers.week_line()
        except Exception as e:  # noqa: BLE001 — reads are data
            return f"Chat logs: read failed ({str(e)[:60]})."
        try:
            extra = self.ledgers.budget_line()
        except Exception:  # noqa: BLE001 — budget is optional here
            extra = ""
        return ("Chat logs: " + line + (extra if extra else ""))

    def _habits_line(self) -> str:
        if self.habits is None:
            return "Habits: not wired yet."
        try:
            rows = self.habits.list_rows()
        except Exception as e:  # noqa: BLE001 — reads are data
            return f"Habits: read failed ({str(e)[:60]})."
        if not rows:
            return "Habits: none tracked yet."
        try:
            body = "; ".join(
                f"'{r['name']}' ({r['label']}) streak "
                f"{r['stats']['current']}, best "
                f"{r['stats']['best']}"
                for r in rows[:4])
        except Exception as e:  # noqa: BLE001
            return f"Habits: read failed ({str(e)[:60]})."
        more = (f" +{len(rows) - 4} more" if len(rows) > 4 else "")
        return f"Habits: {len(rows)} tracked — {body}{more}."

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        s = (clean or "").strip()
        if self._INVITE_RE.match(s) is None:
            return None
        try:
            self._audit("weekly_review.assembled", who,
                        f"len={len(s)}")
            sections = []
            # monotonic section order: calendar, conflicts, reminders,
            # routines, capture, chat logs (+ budget tail), habits
            for line in (self._calendar_line(who),
                         self._conflicts_line(),
                         self._reminders_line(),
                         self._routines_line(),
                         self._capture_line(),
                         self._ledgers_line(),
                         self._habits_line()):
                sections.append(self._dot(line))
            header = "Weekly review"
            msg = (f"{header}: " + " ".join(sections) +
                   " (read-only review; nothing was executed)")
            return {"message": msg.strip(),
                    "proposal": None, "resolution": None,
                    "executed": False, "verified": True,
                    "weekly_review": "reported"}
        except Exception as e:  # noqa: BLE001 — the review never breaks
            self._audit("weekly_review.failed", who, str(e)[:80])
            return {"message":
                    f"The weekly review couldn't be assembled just now "
                    f"({str(e)[:80]}). The individual asks still work: "
                    f"'calendar this week', 'show my reminders', "
                    f"'show my routines'.",
                    "proposal": None, "resolution": None,
                    "executed": False, "weekly_review": "failed"}
