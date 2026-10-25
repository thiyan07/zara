"""Stage 31-P4 close-out (P5 feature-hunt ⑮, 2026-10-23): the morning
brief — one deterministic, READ-ONLY answer that stitches together the
three facts a person actually checks first: today's calendar, the
weather, and the laptop's health.

Honesty rules, pinned:
- every section is a REAL read from an existing flow (CalendarFlow's
  today scope, WeatherFlow's home/city fetch, system_health's /proc
  facts) — the brief never summarizes from memory and never invents;
- degradation is per-section and named: an offline weather fetch or a
  failed health read shows the section's honest failure line, not a
  quietly missing section;
- read-only: nothing here executes, writes, or approvals. Utterances
  with other intents do not match (narrow anchor regex), so the brief
  cannot swallow device-flow asks.
- timezone: the header day is the LOCAL day (host-TZ rule — an
  18:30Z edition of a "morning" brief still labels the local date).
"""
from __future__ import annotations

import re
from datetime import datetime


class MorningBriefFlow:
    """P5 ⑮: one-turn composite read; sections reuse the tested flows.

    P6-b25 (2026-10-24) — the brief reads FIVE first-thing facts:
    today's calendar, today's schedule CONFLICTS (CalendarFlow's own
    tested _collect_pairs — never a second implementation), pending
    reminders due today, the weather, and laptop health. Conflict and
    reminder degradation stays per-section and named, same as every
    other section; the brief touches nothing and executes nothing."""

    # Narrow by design: greetings + explicit brief nouns only. Anything
    # that names an action (send, add, cancel…) or a scope word belongs
    # to another flow and never lands here.
    _INVITE_RE = re.compile(
        r"^\s*(?:good\s+morning|morning\s+brief(?:ing)?|daily\s+brief"
        r"(?:ing)?|brief\s+me|what(?:'s|\s+is)\s+my\s+day\s+like"
        r"|what(?:'s|\s+is)\s+on\s+my\s+plate)(?:\s+today)?\b"
        r"(?:\s*,?\s*zara)?[\s.,!?]*$",
        re.I)

    _TODAY_HEAD = "Calendar for today:"

    @staticmethod
    def _dot(s: str) -> str:
        """One trailing period — never '..' (double punctuation is a
        cosmetic lie about the sentence structure)."""
        s = (s or "").strip()
        return s if s.endswith((".", "!", "?")) else s + "."

    def __init__(self, audit, calendar_flow=None, weather_flow=None,
                 health_fn=None, reminders=None) -> None:
        super().__init__()
        self.audit = audit
        self.calendar_flow = calendar_flow
        self.weather_flow = weather_flow
        # P6-b25: the reminder service (ReminderService) — optional so
        # old constructions keep working; an absent store degrades the
        # section honestly, it does not disappear.
        self.reminders = reminders
        # lazily imported by default so the module stays import-safe
        # in unit contexts without the full app stack
        self._health_fn = health_fn

    def _audit(self, action: str, who: str, detail: str = "") -> None:
        try:
            self.audit.record("morning_brief", action, who, detail[:300])
        except Exception:  # noqa: BLE001 — audit never breaks turns
            pass

    def _reminders_line(self) -> str:
        """P6-b25: reminders still pending TODAY (ReminderService's
        own `upcoming`, filtered by the local day on each due epoch).
        Honest naming: an absent store, a failed read, next-day-only
        items — all say what they are, nothing invented."""
        if self.reminders is None:
            return ("Reminders: store not wired — the reminder flows "
                    "still work on ask.")
        try:
            now = datetime.now().astimezone()
            eod = (datetime.combine(now.date(), datetime.max.time(),
                                    tzinfo=now.tzinfo)).timestamp()
            up = self.reminders.upcoming()
            todays = [r for r in up
                      if r.get("due_epoch") is not None
                      and r["due_epoch"] <= eod]
        except Exception as e:  # noqa: BLE001 — reads never break turns
            return f"Reminders: read failed ({str(e)[:60]})."
        if not todays:
            if up:
                return f"Reminders: none pending today ({len(up)} later)."
            return "Reminders: none pending today."
        shown = "; ".join(
            f"'{(r.get('text') or '?')[:40]}' (due {r.get('due')})"
            for r in todays[:2])
        more = f" +{len(todays) - 2} later today" \
            if len(todays) > 2 else ""
        return f"Reminders: {len(todays)} pending today — {shown}{more}."

    def _conflicts_line(self) -> str:
        """P6-b25: today's schedule conflicts straight from
        CalendarFlow.conflicts_on_date (the tested collection) — all-
        day events and no-DTEND defaults are named too. A calendar
        flow that can't answer keeps the section with its reason."""
        if self.calendar_flow is None or \
                not hasattr(self.calendar_flow, "conflicts_on_date"):
            return "Conflicts: calendar flow unavailable today."
        got = self.calendar_flow.conflicts_on_date(
            datetime.now().astimezone().date())
        if got is None:
            return "Conflicts: today — conflict read failed."
        n, rendered, allday, no_end = got
        if n == 0:
            body = "none today."
        elif n == 1:
            body = ("1 today — " + rendered[0] + "."
                    if rendered else "1 today.")
        else:
            body = (f"{n} today — " + "; ".join(rendered[:2])
                    + (f" +{n - 2} more" if n > 2 else "") + ".")
        extras = []
        if allday:
            extras.append(f"{allday} all-day event(s) not clock-checked")
        if no_end:
            extras.append(f"{no_end} event(s) without an end time "
                          "counted as 2 hours")
        if extras:
            body += " (" + "; ".join(extras) + ")"
        return "Conflicts: " + body

    def _health(self) -> str:
        """One deterministic health line — same numbers the
        system.health answer reports (subset, never paraphrased
        differently). Failure keeps the section with its honest reason.
        """
        if self._health_fn is None:
            from .builtin_tools import system_health
            self._health_fn = system_health
        h = self._health_fn({}, {})
        if not h.get("ok"):
            return ("Laptop: health read unavailable — "
                    + str(h.get("detail", ""))[:80])
        return (f"Laptop: disk {h.get('disk_free_gb')} GB free of "
                f"{h.get('disk_total_gb')} GB "
                f"({h.get('disk_used_pct')}% used); RAM "
                f"{h.get('ram_used_pct')}% used; up "
                f"{h.get('uptime_days')} days.")

    def wants_turn(self, clean: str) -> bool:
        return self._INVITE_RE.match((clean or "").strip()) is not None

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        s = (clean or "").strip()
        if self._INVITE_RE.match(s) is None:
            return None
        try:
            self._audit("morning_brief.assembled", who,
                        f"len={len(s)}")

            # --- Calendar section: reuse the tested today-scope read.
            cal = ""
            try:
                if self.calendar_flow is not None and \
                        self.calendar_flow.wants_turn(
                            "what's on my calendar today"):
                    out = self.calendar_flow.turn(
                        "what's on my calendar today", who=who,
                        requester_device=requester_device)
                    if isinstance(out, dict) and out.get("message"):
                        cal = out["message"].strip()
                        if cal.startswith(self._TODAY_HEAD):
                            cal = cal[len(self._TODAY_HEAD):].strip()
            except Exception as e:  # noqa: BLE001 — read failures are data
                cal = f"calendar read unavailable: {str(e)[:60]}"
            if not cal:
                cal = "calendar read unavailable"

            # --- Weather section: the flow owns every honesty rule
            # (source, time label, failure wording); its message goes
            # through verbatim, whatever it decided.
            wx = ""
            try:
                if self.weather_flow is not None and \
                        self.weather_flow.wants_turn("weather today"):
                    out = self.weather_flow.turn(
                        "weather today", who=who,
                        requester_device=requester_device)
                    if isinstance(out, dict) and out.get("message"):
                        wx = out["message"].strip()
            except Exception as e:  # noqa: BLE001 — read failures are data
                wx = f"weather unavailable: {str(e)[:60]}"
            if not wx:
                wx = ("weather unavailable — no location set; say "
                      "'weather in <city>' or set ZARA_WEATHER_LAT/LON")

            # --- Conflict + reminder sections (P6-b25): real reads
            # from the tested services; each named if it can't read.
            cf = ""
            try:
                cf = self._conflicts_line()
            except Exception as e:  # noqa: BLE001 — reads are data
                cf = f"Conflicts: read failed ({str(e)[:60]})."
            rm = ""
            try:
                rm = self._reminders_line()
            except Exception as e:  # noqa: BLE001 — reads are data
                rm = f"Reminders: read failed ({str(e)[:60]})."

            # --- Laptop health section.
            hp = ""
            try:
                hp = self._health()
            except Exception as e:  # noqa: BLE001 — read failures are data
                hp = f"Laptop: health read unavailable: {str(e)[:60]}"

            now = datetime.now().astimezone()
            header = f"Morning brief for {now.strftime('%A, %b %d')}"
            msg = (f"{header}: Calendar: {self._dot(cal)} "
                   f"Weather: {self._dot(wx)} {self._dot(cf)} "
                   f"{self._dot(rm)} {self._dot(hp)} "
                   "(read-only brief; nothing was executed)")
            return {"message": msg.strip(),
                    "proposal": None, "resolution": None,
                    "executed": False, "verified": True,
                    "morning_brief": "reported"}
        except Exception as e:  # noqa: BLE001 — the brief never breaks
            self._audit("morning_brief.failed", who, str(e)[:80])
            return {"message":
                    f"The morning brief couldn't be assembled just now "
                    f"({str(e)[:80]}). The individual asks still work: "
                    "'what's on my calendar today', 'weather today', "
                    "'how is my laptop'.",
                    "proposal": None, "resolution": None,
                    "executed": False,
                    "morning_brief": "failed"}
