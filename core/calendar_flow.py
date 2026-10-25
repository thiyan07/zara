"""Stage 31-P4 (2026-10-22, user-picked): local .ics calendar read.

USER DECISION 2026-10-22: the calendar comes from LOCAL .ics files
(CalDAV was the alternative; dropped for this round — zero network
dependencies, offline-first). Scope kept honest:

- a BOUNDED stdlib parser (no dependencies): VEVENT blocks with
  SUMMARY + DTSTART/DTEND; unfolds continuation lines; Z-suffixed
  times read as UTC, naive times read as the laptop's local time;
  events with a TZID we can't resolve are SHOWN with the raw stamp
  and labeled "timezone unresolved" — never silently shifted;
- RRULE recurrences: a BOUNDED subset expands honestly into real
  occurrences (DAILY/WEEKLY+BYDAY/MONTHLY, INTERVAL, COUNT, UNTIL;
  jump-started walk — a rule created years ago still answers);
  anything beyond that (YEARLY, exotic BY* rules) stays UNEXPANDED
  and is per-event labeled "a rule I don't expand" — never
  invented, never silently dropped;
- paths come from ZARA_ICS_PATH (colon-separated files). Absent
  config is an honest ask, not a guess; unreadable files are
  reported per-file;
- the flow is READ-ONLY: it never writes calendar files.
"""
from __future__ import annotations

import math
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from threading import Lock

_DT_Z = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$")
_DT_NAIVE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})$")
_DATE_ONLY = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_UTC = timezone.utc


def parse_ics(path: str, limit_entries: int = 500) -> dict:
    """Bounded .ics parse. Returns {events, errors} — never raises."""
    events = []
    errors = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read(2_000_000)  # bounded read
    except OSError as e:
        return {"events": [], "errors": [f"{os.path.basename(path)}: "
                                         f"unreadable ({e.strerror})"]}
    # unfold (continuations start with a space or tab)
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded = []
    for ln in lines:
        if ln[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += ln[1:]
        else:
            unfolded.append(ln)
    cur = None
    depth_seen = 0
    for ln in unfolded[:8000]:  # bounded line budget
        stripped = ln.strip()
        if stripped.upper().startswith("BEGIN:VEVENT"):
            if depth_seen >= limit_entries:
                break
            depth_seen += 1
            cur = {"raw_dtstart": None, "summary": "",
                   "tzid": None, "allday": False}
            continue
        if stripped.upper().startswith("END:VEVENT"):
            if cur is not None and cur["raw_dtstart"]:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        val = val.strip()
        up = key.upper()
        tzid_m = re.search(r"TZID=([^;]+)", up)
        if up.startswith("SUMMARY"):
            cur["summary"] = val[:200]
        elif up.startswith("DTSTART"):
            cur["raw_dtstart"] = val
            if tzid_m:
                cur["tzid"] = tzid_m.group(1)[:40]
            if _DATE_ONLY.match(val):
                cur["allday"] = True
        elif up.startswith("DTEND"):
            # P5-b7: captured for recurrence-instance duration (the
            # base DTEND defines each generated occurrence's END —
            # a weekly 10:00-11:00 meeting still shows 10:00).
            cur["raw_dtend"] = val[:40]
        elif up.startswith("RRULE"):
            cur["raw_rrule"] = val.rstrip()[:200]
    return {"events": events, "errors": errors}


def to_utc(raw: str, tzid: str | None) -> dict:
    """UTC conversion with honest unresolved-timezone handling."""
    m = _DT_Z.match(raw or "")
    if m:
        try:
            d = datetime(*[int(x) for x in m.groups()], tzinfo=_UTC)
        except ValueError:
            return {"dt": None, "allday": False, "unresolved": True}
        return {"dt": d, "allday": False, "unresolved": False}
    if _DATE_ONLY.match(raw or ""):
        grp = _DATE_ONLY.match(raw).groups()
        d = datetime(*[int(x) for x in grp]).astimezone()
        return {"dt": d, "allday": True, "unresolved": False}
    if tzid:  # naive + TZID: honest, unshifted, LABELED
        m2 = _DT_NAIVE.match(raw or "")
        if m2:
            d = datetime(*[int(x) for x in m2.groups()]).astimezone()
            return {"dt": d, "allday": False, "unresolved": True}
        return {"dt": None, "allday": False, "unresolved": True}
    m = _DT_NAIVE.match(raw or "")
    if m:
        try:
            d = datetime(*[int(x) for x in m.groups()]).astimezone()
        except ValueError:
            # regex-plausible but calendar-invalid (minute 90, month
            # 13 …) — never raise: it is an unresolved stamp, like a
            # TZID we can't resolve.
            return {"dt": None, "allday": False, "unresolved": True}
        return {"dt": d, "allday": False, "unresolved": False}
    return {"dt": None, "allday": False, "unresolved": True}


_DAY_TOKEN_RE = re.compile(
    r"\b(?:(next|last)\s+)?(today|tomorrow|yesterday|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday)\b")

_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_RRULE_STEP_CAP = 400     # candidate steps, bounded by design
_RRULE_COUNT_CAP = 366    # COUNT ceiling (bounded generation budget)
_RRULE_INTERVAL_CAP = 365


def _rrule_params(rrule: str) -> dict | None:
    """Parse `FREQ=DAILY;COUNT=3`-style params; None = malformed."""
    out = {}
    try:
        for part in (rrule or "").split(";"):
            if not part:
                continue
            k, _, v = part.partition("=")
            k = k.strip().upper()
            out[k] = (v or "").strip().upper()
    except (ValueError, AttributeError):
        return None
    if not out:
        return None
    return out


def expand_rrule(base: datetime, rrule: str, horizon: datetime,
                 window_start: datetime | None = None,
                 budget: int = _RRULE_STEP_CAP) -> dict:
    """P5-b7: expand a BOUNDED subset of RRULE recurrences into concrete
    occurrence datetimes anchored to the event's base time (already
    converted by to_utc). Zero dependencies, bounded by design.

    Supported honestly: FREQ=DAILY | WEEKLY (+ a BYDAY weekday list) |
    MONTHLY (base day-of-month; short months are honestly skipped),
    INTERVAL, COUNT, UNTIL. Anything beyond that (YEARLY, exotic BY*
    members, ordinal BYDAY, malformed values) returns supported=False
    so the flow shows that ONE event at its base occurrence with the
    honest note — never an invented recurrence.

    Bounding: at most `budget` candidate steps per event. A budget
    exhausted BEFORE a single in-window occurrence is reached (e.g. a
    daily rule starting years ago with no COUNT) degrades the event
    to the honest not-expanded note; occurrences already accepted are
    kept (they genuinely belong to the rule)."""
    ws = window_start or base
    p = _rrule_params(rrule)
    if p is None:
        return {"occurrences": [], "supported": False,
                "reason": "malformed RRULE"}
    freq = p.get("FREQ", "")
    if freq not in ("DAILY", "WEEKLY", "MONTHLY"):
        return {"occurrences": [], "supported": False,
                "reason": "FREQ not daily/weekly/monthly"}
    if any(k in ("BYWEEKNO", "BYYEARDAY", "BYMONTH", "BYSETPOS",
                 "BYHOUR", "BYMINUTE", "BYSECOND", "BYMONTHDAY",
                 "BYWEEKNO", "BYYEARDAY") for k in p):
        return {"occurrences": [], "supported": False,
                "reason": "unsupported BY* member"}
    inter = p.get("INTERVAL", "1")
    try:
        x = int(inter)
    except ValueError:
        return {"occurrences": [], "supported": False,
                "reason": "malformed INTERVAL"}
    if x < 1:
        return {"occurrences": [], "supported": False,
                "reason": "non-positive INTERVAL"}
    inter = min(x, _RRULE_INTERVAL_CAP)
    count = None
    if "COUNT" in p:
        try:
            count = int(p["COUNT"])
        except ValueError:
            return {"occurrences": [], "supported": False,
                    "reason": "malformed COUNT"}
        if count < 1:
            return {"occurrences": [], "supported": False,
                    "reason": "non-positive COUNT"}
        count = min(count, _RRULE_COUNT_CAP)
    until = None
    if "UNTIL" in p:
        m = _DT_Z.match(p["UNTIL"])
        if m:
            until = datetime(*[int(x) for x in m.groups()], tzinfo=_UTC)
        elif _DATE_ONLY.match(p["UNTIL"]):
            g = _DATE_ONLY.match(p["UNTIL"]).groups()
            until = datetime(*[int(x) for x in g]).astimezone().replace(
                hour=23, minute=59, second=59)
        elif _DT_NAIVE.match(p["UNTIL"]):
            g = _DT_NAIVE.match(p["UNTIL"]).groups()
            until = datetime(*[int(x) for x in g]).astimezone()
        else:
            return {"occurrences": [], "supported": False,
                    "reason": "malformed UNTIL"}
    byday: list[int] = []
    if "BYDAY" in p:
        if freq != "WEEKLY":
            return {"occurrences": [], "supported": False,
                    "reason": "BYDAY only supported for FREQ=WEEKLY"}
        for tok in p["BYDAY"].split(","):
            t = tok.strip().upper()
            if t not in _WEEKDAYS:
                return {"occurrences": [], "supported": False,
                        "reason": "unsupported BYDAY form"}
            byday.append(_WEEKDAYS[t])
        if len(set(byday)) != len(byday):
            return {"occurrences": [], "supported": False,
                    "reason": "duplicated BYDAY member"}

    out: list[datetime] = []
    produced_from_base = 0  # every candidate since BASE (COUNT semantics)
    steps_left = budget

    def accept(cand: datetime) -> bool:
        return ws <= cand <= horizon

    if freq == "DAILY":
        # Jump-start: the first candidate index whose occurrence can
        # reach the window — a rule created years ago costs ZERO
        # budget to skip its past (arithmetic, not stepping).
        delta_days = (ws - base).total_seconds() / 86400.0
        # FLOOR: the interval bucket CONTAINING the window start is
        # still walked (its tail may be in-window).
        k = max(0, math.floor(delta_days / inter)) if delta_days > 0 \
            else 0
        while True:
            cand = base + timedelta(days=k * inter)
            if count is not None and k >= count:
                break
            if until is not None and cand > until:
                break
            if accept(cand):
                out.append(cand)
            produced_from_base += 1
            if cand > horizon:
                break
            steps_left -= 1
            if steps_left <= 0:
                break
            k += 1
    elif freq == "WEEKLY":
        days = sorted(byday) if byday else [base.weekday()]
        tod = base.timetz()  # occurrence time-of-day, tz untouched
        week0 = base.date() - timedelta(days=base.weekday())
        # Jump start to the first candidate WEEK that can touch the
        # window (arithmetic — past weeks cost nothing).
        # FLOOR: the base week may still hold candidates >= window
        # start (per-day accept() filters), so never skip it.
        wk = (ws.date() - week0).days / 7.0
        k = (max(0, math.floor(wk / inter)) if wk > 0 else 0) \
            if count is None else 0
        while True:
            week_start = week0 + timedelta(weeks=k * inter)
            if week_start > max(horizon.date(), (until.date()
                                 if until else None) or horizon.date()):
                break
            if count is not None and produced_from_base >= count:
                break
            budget_now = steps_left
            for wd in days:
                if count is not None and produced_from_base >= count:
                    break
                if steps_left <= 0:
                    break
                cand = datetime.combine(
                    week_start + timedelta(days=wd), tod)
                if until is not None and cand > until:
                    continue
                if accept(cand):
                    out.append(cand)
                produced_from_base += 1
                steps_left -= 1
            if steps_left <= 0 and out == []:
                break
            if week_start > horizon.date():
                break
            if steps_left <= 0:
                break
            k += 1
    else:  # MONTHLY: base day-of-month; short months honestly skipped
        dom = base.day
        month_span = (ws.year - base.year) * 12 + (ws.month - base.month)
        # FLOOR: the bucket holding the window start is still walked.
        k = max(0, math.floor(month_span / inter)) if month_span > 0 \
            else 0
        while True:
            if count is not None and k >= count:
                break
            tot = (base.year * 12 + (base.month - 1)) + k * inter
            y, mo = divmod(tot, 12)
            mo += 1
            try:
                cand = base.replace(year=y, month=mo, day=dom)
            except ValueError:
                pass  # short month: no occurrence, keep walking
            else:
                if until is not None and cand > until:
                    break
                if accept(cand):
                    out.append(cand)
                produced_from_base += 1
                if cand > horizon:
                    break
            steps_left -= 1
            if steps_left <= 0:
                break
            k += 1

    if not out and steps_left <= 0:
        return {"occurrences": [], "supported": False,
                "reason": "rule predates the window; too long to "
                          "expand within budget"}
    return {"occurrences": out, "supported": True, "reason": ""}
class CalendarFlow:
    """P4 laptop-side flow: 'what's next' reads the configured .ics
    files and reports today (and the next 7 days), read-only."""

    def __init__(self, audit, ics_paths: str | None = None) -> None:
        self.audit = audit
        self._paths = ics_paths or os.environ.get("ZARA_ICS_PATH", "")
        self._lock = Lock()
        self._req_id = ""

    def _answer_title_query(self, title: str, who: str,
                            rid: str) -> dict:
        """P5 ⑩: earliest-future matches by title substring
        (case-insensitive), from every configured calendar, including
        expanding RRULE occurrences inside the next-7-days window.
        Honest by construction: at most two stamps, then a count;
        nothing matches → it says so and nothing is invented."""
        now = datetime.now().astimezone()
        horizon = now + timedelta(days=7)
        hits: list[tuple[object, str]] = []
        paths = (self._paths or os.environ.get("ZARA_ICS_PATH", ""))
        t_low = title.lower()
        for path in paths.split(":"):
            path = path.strip()
            if not path:
                continue
            parsed = parse_ics(path)
            for ev in parsed["events"]:
                summ = (ev["summary"] or "").strip().lower()
                if t_low not in summ:
                    continue
                conv = to_utc(ev["raw_dtstart"], ev["tzid"])
                if conv["dt"] is None:
                    continue
                rr = ev.get("raw_rrule")
                occs = []
                if rr:
                    exp = expand_rrule(conv["dt"], rr, horizon, now)
                    if exp["supported"]:
                        occs = [o for o in exp["occurrences"]
                                if now <= o <= horizon]
                for o in occs:
                    hits.append((o, ev["summary"] or "(no title)"))
                if not occs:
                    dur = timedelta(hours=2)
                    if ev.get("raw_dtend"):
                        endc = to_utc(ev["raw_dtend"], ev["tzid"])
                        if endc["dt"] is not None:
                            dur = max(endc["dt"] - conv["dt"],
                                      timedelta(minutes=5))
                    if now <= conv["dt"] <= horizon:
                        hits.append((conv["dt"],
                                     ev["summary"] or "(no title)"))
        hits.sort(key=lambda h: h[0])
        n = len(hits)
        self._audit("calendar.title_query", who, f"q={title!r} n={n} rid={rid}")
        if n == 0:
            return {"message":
                    f'No upcoming "{title[:40]}" on the calendar '
                    "(next 7 days).", "proposal": None,
                    "resolution": None, "executed": True,
                    "verified": True, "calendar_flow": "title_query"}
        parts = []
        for w, summ in hits[:2]:
            lw = w.astimezone() if w.tzinfo is not None else w
            parts.append(
                f"{lw.strftime('%a %b %d %H:%M')} "
                f"({lw.strftime('%z')}): {summ}")
        more = f" +{n - 2} later" if n > 2 else ""
        msg = (f'Next "{title[:40]}" on the calendar: '
               + "; ".join(parts) + "." + more)
        return {"message": msg,
                "proposal": None, "resolution": None,
                "executed": True, "verified": True,
                "calendar_flow": "title_query"}

    # P6-b (2026-10-24): conflict-check vocabulary anchors by SEARCH
    # (the phrase can sit mid-utterance), GUARDED: an utterance that
    # also carries an action verb (cancel/remove/move/add/open …)
    # belongs to another flow, never to the read-only conflict check.
    _CONF_ANCHOR = re.compile(
        r"\b(?:any|what|which|do i have)\s+(?:schedule\s+)?conflicts?\b"
        r"|\bconflict check\b|\bam i double.?booked\b|\bdouble.?booked\b"
        r"|\boverlapping\s+(?:meetings?|events?|appointments?)\b"
        r"|\b(?:meetings?|events?|appointments?)\s+(?:overlap|clashes?)\b")
    _ACTION_EXCLUDE = re.compile(
        r"\b(cancel|remove|delete|move|shift|reschedule|add|put|"
        r"schedule|send|open|launch|backup|transfer)\b")

    def wants_turn(self, clean: str) -> bool:
        s = (clean or "").strip().lower()
        if self._CONF_ANCHOR.search(s) is not None and \
                self._ACTION_EXCLUDE.search(s) is None:
            return True
        return bool(re.match(
            r"^(what(?:'?s|\s+is|\s+was) next\b|"
            r"what(?:'?s|\s+is|\s+was) on my calendar\b"
            r"|my calendar\b|calendar (?:today|tomorrow|yesterday|"
            r"next\s+|last\s+)?"
            r"(?:monday|tuesday|wednesday|thursday|friday|saturday|"
            r"sunday|today|tomorrow)?\b|calendar today\b"
            r"|next (?:event|meeting)s?\b"
            r"|my (?:last |previous )?week\b|calendar (?:last |this )?"
            r"week\b|what did i do (?:last |this )?week\b"
            r"|(?:when am i free\b|am i free\b|whats? my free\b|"
            r"what(?:'s|\s+is|\s+are) my free\b|when do i have time\b|"
            r"any free time\b)"
            r"|when(?:'?s|\s+is)\s+(?:my|the)\s+\"[^\"]{1,40}\""
            r"|when(?:'?s|\s+is)\s+(?:my|the)\s+next\s+"
            r"[a-z0-9][a-z0-9 '\-]{0,39}\b"
            r"|when(?:'?s|\s+is)\s+(?:my|the)\s+(?:next\s+)?"
            r"[a-z0-9][a-z0-9 ' \-]{0,39}\s?(?:meetings?|"
            r"appointments?|events?|sessions?|calls?)\b)",
            s))

    def _week_scope(self, s: str):
        """P5 ⑭ (2026-10-23): "this week"/"last week" digest — a
        WEEK window (Monday-anchored, ISO) changes the REPORT SHAPE:
        per-day grouping with counts. Returns (start_local_date,
        label) for "this week" or None (past-only: "last week" is
        the whole previous Mon–Sun; "this week" covers from Monday
        of the currentweek to Sunday)."""
        m = re.search(
            r"\b(this|last|previous) week\b", s)
        if not m:
            return None
        now = datetime.now().astimezone()
        monday = now.date() - timedelta(days=now.weekday())
        if m.group(1) == "this":
            return monday, "this week"
        return monday - timedelta(days=7), "last week"

    def _answer_week(self, wk: tuple, who: str, rid: str) -> dict:
        """P5 ⑭: events grouped by their LOCAL calendar day inside
        the week window; RRULE occurrences included (walker anchored
        at the window start); days are listed in order with counts —
        honest: no events → named empty; >6 titles per day collapses
        to a count (bounds, not guesses)."""
        start, label = wk
        now = datetime.now().astimezone()
        ws = datetime.combine(start, datetime.min.time(),
                              tzinfo=now.tzinfo)
        we = datetime.combine(
            start + timedelta(days=7), datetime.min.time(),
            tzinfo=now.tzinfo)
        by_day: dict[str, list[str]] = {}
        total = 0
        paths = (self._paths or os.environ.get("ZARA_ICS_PATH", ""))
        for path in paths.split(":"):
            path = path.strip()
            if not path:
                continue
            for ev in parse_ics(path)["events"]:
                conv = to_utc(ev["raw_dtstart"], ev["tzid"])
                if conv["dt"] is None:
                    continue
                summ = ev["summary"] or "(no title)"
                if ev.get("raw_rrule"):
                    exp = expand_rrule(conv["dt"], ev["raw_rrule"],
                                       we, ws)
                    if exp["supported"]:
                        for o in exp["occurrences"]:
                            if ws <= o < we:
                                by_day.setdefault(
                                    o.astimezone()
                                    .strftime("%a %b %d"), []
                                ).append(summ)
                                total += 1
                        continue
                if conv["allday"]:
                    d = (conv["dt"].date() if conv["dt"].tzinfo is None
                         else conv["dt"].astimezone().date())
                    if ws.date() <= d < we.date():
                        by_day.setdefault(
                            d.strftime("%a %b %d"), []) \
                            .append(summ + " (all-day)")
                        total += 1
                    continue
                if ws <= conv["dt"] < we:
                    by_day.setdefault(
                        conv["dt"].astimezone()
                        .strftime("%a %b %d"), []).append(summ)
                    total += 1
        self._audit("calendar.week_digest", who,
                    f"start={start} total={total} rid={rid}")
        if total == 0:
            return {"message": f"Nothing on the calendar {label}.",
                    "proposal": None, "resolution": None,
                    "executed": True, "verified": True,
                    "calendar_flow": "week_digest"}
        parts = []
        for dkey in sorted(by_day,
                           key=lambda k: datetime.strptime(
                               k, "%a %b %d")):
            titles = by_day[dkey]
            shown = "; ".join(titles[:6])
            if len(titles) > 6:
                shown += f" +{len(titles) - 6} more"
            parts.append(f"{dkey}: {len(titles)} — {shown}")
        return {"message": (
            f"Calendar {label} ({total} event(s)): "
            + "; ".join(parts) + "."),
            "proposal": None, "resolution": None,
            "executed": True, "verified": True,
            "calendar_flow": "week_digest"}

    def _free_query(self, s: str):
        """P5 ⑫ (2026-10-23): "when am I free <day>?" (gap report) or
        "am I free <day> at <H:MM>?" (point check). Returns a dict
        {day: target_date|None, point: (hh,mm)|None} or None when the
        utterance carries fields this bounded reader refuses (dates,
        duration maths, 'next week', a range)."""
        if not re.search(
                r"\b(am i free\b|when am i free\b|whats? my free\b|"
                r"what(?:'s|\s+is|\s+are) my free\b|when do i have "
                r"time\b|any free time\b)", s):
            return None
        if re.search(r"\b(next week|next month|weekend|\d{4}\b)", s):
            return None  # heavier planning → hosted parser
        scoped = self._day_scope(s)
        day = scoped[0] if scoped else None
        label = scoped[1] if scoped else None
        pm = re.search(r"\bat (\d{1,2})(?::(\d{2}))?\s*"
                       r"(a\.?m\.?|p\.?m\.?)?", s)
        point = None
        if pm:
            hh = int(pm.group(1))
            mm = int(pm.group(2) or 0)
            ap = (pm.group(3) or "").replace(".", "").lower()
            if ap.startswith("p") and hh < 12:
                hh += 12
            elif ap.startswith("a") and hh == 12:
                hh = 0
            if not (0 <= hh < 24 and 0 <= mm < 60):
                return None
            point = (hh, mm)
        return {"day": day, "label": label, "point": point}

    def _busy_di(self, target):
        """P6-b30 extraction: the busy-interval collection for ONE
        local date, shared by the free-slot answer (chat) and the
        scheduler proposals — ONE computation, never a second copy.
        Returns (iv, merged, no_end, allday_hit)."""
        now = datetime.now().astimezone()
        sod = datetime.combine(target, datetime.min.time(),
                               tzinfo=now.tzinfo)
        eod = datetime.combine(target, datetime.max.time(),
                               tzinfo=now.tzinfo)
        iv: list[list] = []
        no_end = 0
        allday_hit = False
        paths = (self._paths or os.environ.get("ZARA_ICS_PATH", ""))
        for path in paths.split(":"):
            path = path.strip()
            if not path:
                continue
            for ev in parse_ics(path)["events"]:
                conv = to_utc(ev["raw_dtstart"], ev["tzid"])
                if conv["dt"] is None:
                    continue
                if conv["allday"]:
                    d = conv["dt"].date() if conv["dt"].tzinfo is None \
                        else conv["dt"].astimezone().date()
                    if d == target:
                        allday_hit = True
                    continue
                starts = []
                if ev.get("raw_rrule"):
                    exp = expand_rrule(
                        conv["dt"], ev["raw_rrule"], eod,
                        sod if target < now.date() else now)
                    if exp["supported"]:
                        starts = [o for o in exp["occurrences"]
                                  if sod <= o <= eod]
                if not starts:
                    if conv["dt"].tzinfo is None:
                        starts = [conv["dt"]] \
                            if conv["dt"].date() == target else []
                    elif sod <= conv["dt"] <= eod:
                        starts = [conv["dt"]]
                for st in starts:
                    if ev.get("raw_dtend"):
                        endc = to_utc(ev["raw_dtend"], ev["tzid"])
                        # DTEND IS the block length (never the 2-hour
                        # default that would inflate short events)
                        end = (max(endc["dt"], st)
                               if endc["dt"] is not None
                               else st + timedelta(hours=2))
                        if endc["dt"] is None:
                            no_end += 1
                    else:
                        end = st + timedelta(hours=2)
                        no_end += 1
                    iv.append([st, end, ev["summary"] or "(no title)"])
        iv.sort(key=lambda i: i[0])
        merged: list[list] = []
        for a, b, summ in iv:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
                if summ and summ not in merged[-1][2]:
                    merged[-1][2] = (merged[-1][2]
                                     + (" + " + summ if merged[-1][2]
                                        else summ))[:80]
            else:
                merged.append([a, b, summ])
        return iv, merged, no_end, allday_hit

    def free_windows(self, target, min_minutes: int = 30) -> list:
        """P6-b30: structured free gaps inside 08:00–22:00 LOCAL for
        the given local date — tz-aware (start, end) tuples of at
        least min_minutes. Same gap math as _answer_free_slots (it
        was extracted from there verbatim)."""
        _, merged, _, allday_hit = self._busy_di(target)
        if allday_hit:
            return []
        now = datetime.now().astimezone()
        day0, day1 = 8 * 60, 22 * 60

        def _lm(x):
            lw = (x if x.tzinfo is None else x.astimezone())
            return ((lw.hour * 60 + lw.minute)
                    if lw.date() == target
                    else (-1 if lw.date() < target else 1441))

        out = []
        cursor = day0
        # slots carry the ASKED day's date (b31 fix: a "tomorrow" ask
        # used to be dated today — the pick then anchored the pick-time
        # to today and a fresh same-day slot could read as stale)
        sdt = datetime.combine(target, datetime.min.time(),
                               tzinfo=now.tzinfo)
        for a, b, _s in merged:
            ia, ib = max(_lm(a), day0), min(_lm(b), day1)
            if ia >= ib:
                continue
            if ia - cursor >= min_minutes:
                start = sdt + timedelta(minutes=cursor)
                end = sdt + timedelta(minutes=ia)
                out.append((start, end))
            cursor = max(cursor, ib)
        if day1 - cursor >= min_minutes:
            start = sdt + timedelta(minutes=cursor)
            end = sdt + timedelta(minutes=day1)
            out.append((start, end))
        return out

    def _answer_free_slots(self, freeq: dict, who: str, rid: str) -> dict:
        """P5 ⑫: busy intervals from every event overlapping the
        asked local day (DTEND honored; an all-day event blocks the
        whole day; an event with NO end blocks 2 hours — and the
        answer says so), merged, then reported as gaps inside
        08:00–22:00 LOCAL (>=15 min). A point check answers busy or
        free, naming the blocking event. Nothing invented."""
        now = datetime.now().astimezone()
        target = freeq["day"] or now.date()
        label = freeq["label"] or "today"
        point = freeq["point"]
        iv, merged, no_end, allday_hit = self._busy_di(target)
        self._audit("calendar.free_slots", who,
                    f"day={target} n={len(iv)} noend={no_end} rid={rid}")

        def _lm(x):  # minutes-from-midnight in LOCAL clock
            lw = (x if x.tzinfo is None else x.astimezone())
            return ((lw.hour * 60 + lw.minute)
                    if lw.date() == target
                    else (-1 if lw.date() < target else 1441))

        def _fmt(m):
            return f"{int(m) // 60:02d}:{int(m) % 60:02d}"

        day0, day1 = 8 * 60, 22 * 60
        if point is not None:
            pm = point[0] * 60 + point[1]
            for a, b, summ in merged:
                if _lm(a) <= pm <= _lm(b):
                    msg = (f"Busy {label} at "
                           f"{point[0]:02d}:{point[1]:02d} — "
                           f"{summ[:60]} "
                           f"({_fmt(max(_lm(a), 0))}–"
                           f"{_fmt(min(_lm(b), day1))}).")
                    return {"message": msg, "proposal": None,
                            "resolution": None, "executed": True,
                            "verified": True,
                            "calendar_flow": "free_slots"}
            if allday_hit:
                msg = (f"Busy {label} all day at "
                       f"{point[0]:02d}:{point[1]:02d} — an all-day "
                       "event blocks the day.")
            else:
                msg = (f"You are free {label} at "
                       f"{point[0]:02d}:{point[1]:02d}.")
            return {"message": msg, "proposal": None,
                    "resolution": None, "executed": True,
                    "verified": True, "calendar_flow": "free_slots"}
        free_parts: list[str] = []
        if allday_hit:
            msg = (f"Not free any time {label} — an all-day event "
                   "blocks the whole day.")
        elif not merged:
            msg = (f"You are free all day {label} "
                   "(08:00–22:00 local).")
        else:
            cursor = day0
            for a, b, _ in merged:
                ia, ib = max(_lm(a), day0), min(_lm(b), day1)
                if ia >= ib:
                    continue
                if ia - cursor >= 15:
                    free_parts.append(f"{_fmt(cursor)}–{_fmt(ia)}")
                cursor = max(cursor, ib)
            if day1 - cursor >= 15:
                free_parts.append(f"{_fmt(cursor)}–{_fmt(day1)}")
            tail_note = (f" ({no_end} event(s) without an end time "
                         "counted as 2 hours)" if no_end else "")
            if free_parts:
                msg = (f"Free {label}: " + ", ".join(free_parts)
                       + " (08:00–22:00 local)." + tail_note)
            else:
                msg = (f"No free window {label} between 08:00 and "
                       f"22:00.{tail_note}")
        return {"message": msg, "proposal": None, "resolution": None,
                "executed": True, "verified": True,
                "calendar_flow": "free_slots"}

    def _title_query(self, s: str):
        """P5 ⑩ (2026-10-23): "when is my/the (next) <X>" asks WHERE/
        WHEN a named meeting sits — a title LOOKUP, not the sweep.
        Returns the search text (<=40 chars) or None. Quoted titles,
        noun-tailed shapes ("when is my dentist appointment"), and
        nounless "my next <X>" are accepted; day-scope words inside
        the query ("when is my dentist tomorrow") are refused so the
        sweep/scope machinery keeps the turn."""
        m = re.search(
            r"^when(?:'?s|\s+is)\s+(?:my\s+|the\s+)(?:next\s+)?"
            r"(?P<rest>.+?)\s*\??$", s)
        if not m:
            return None
        rest = m.group("rest")
        qm = re.search(r"\"([^\"]{1,40})\"", rest)
        if qm:
            title = qm.group(1)
        else:
            title = re.sub(
                r"\b(meetings?|appointments?|events?|sessions?|"
                r"calls?)\b$", "", rest).strip()
        title = re.sub(r"\s+", " ", title).strip(" '\"")
        if not title or len(title) > 40:
            return None
        if re.search(
                r"\b(today|tomorrow|yesterday|tonight|next week|"
                r"monday|tuesday|wednesday|thursday|friday|"
                r"saturday|sunday)\b", title):
            return None  # the sweep/scope machinery owns day words
        return title

    def _day_scope(self, s: str):
        """P5 ⑨ (2026-10-23): a NAMED day scopes the read answer to
        that date (today/tomorrow/a weekday — "next" forward-only),
        instead of the default next-7-days sweep. P5 ⑪: the PAST is
        answerable too — "yesterday" and "last <weekday>" scope to a
        historical day; events are matched by OVERLAP with that day.
        Returns a (local_date, label) pair, or None. The horizon
        stretches to reach the named day when it lies beyond 7
        days; items are filtered by their LOCAL calendar date."""
        dm = re.search(
            r"\b(today|tomorrow|yesterday|last\s+(monday|tuesday|"
            r"wednesday|thursday|friday|saturday|sunday)|next\s+"
            r"(monday|tuesday|wednesday|thursday|friday|saturday|"
            r"sunday)|monday|tuesday|wednesday|thursday|friday|"
            r"saturday|sunday)\b", s)
        if not dm:
            return None
        w = dm.group(0)
        now = datetime.now().astimezone()
        if w == "today":
            return now.date(), "today"
        if w == "tomorrow":
            return now.date() + timedelta(days=1), "tomorrow"
        if w == "yesterday":
            return now.date() - timedelta(days=1), "yesterday"
        idx = {"monday": 0, "tuesday": 1, "wednesday": 2,
               "thursday": 3, "friday": 4, "saturday": 5,
               "sunday": 6}.get(w.replace("next ", "")
                                .replace("last ", ""))
        if idx is None:
            return None
        if w.startswith("last "):
            # backward: "last friday" on a Friday means a week ago
            back = (now.weekday() - idx) % 7 or 7
            return (now - timedelta(days=back)).date(), w
        add = (idx - now.weekday()) % 7 or 7
        return (now + timedelta(days=add)).date(), w

    def _resolve_day_token(self, word: str, qualifier: str | None):
        """P6-a (2026-10-24): resolve ONE day token to a LOCAL date —
        today/tomorrow/yesterday, or a weekday ("next" forward-only,
        "last" backward-only, bare weekday forward). Mirrors the
        _day_scope arithmetic so both surfaces answer the same date
        for the same word."""
        now = datetime.now().astimezone()
        if word == "today":
            return now.date()
        if word == "tomorrow":
            return now.date() + timedelta(days=1)
        if word == "yesterday":
            return now.date() - timedelta(days=1)
        idx = {"monday": 0, "tuesday": 1, "wednesday": 2,
               "thursday": 3, "friday": 4, "saturday": 5,
               "sunday": 6}.get(word)
        if idx is None:
            return None
        if qualifier == "last":
            back = (now.weekday() - idx) % 7 or 7
            return (now - timedelta(days=back)).date()
        add = (idx - now.weekday()) % 7 or 7
        return (now + timedelta(days=add)).date()

    def _compound_scope(self, s: str):
        """P6-a (2026-10-24): "calendar today and tomorrow" /
        "friday and saturday" — a LIST of distinct local days whose
        day tokens are chained ONLY by connectors and/or or-commas
        (" and ", ", ", ", and "). Anything where day words are
        spread across other sentence parts ("flight monday …
        wednesday") is refused (None) so the single-day sweep keeps
        the turn — never a guessed scope. Capped at 4 days; duplicate
        dates collapse to one."""
        toks = list(_DAY_TOKEN_RE.finditer(s))
        if len(toks) < 2:
            return None
        for a, b in zip(toks, toks[1:]):
            sep = s[a.end():b.start()].strip()
            if sep not in ("and", ",", ", and", ",and", "or"):
                return None
        days: list[tuple] = []
        seen = set()
        for m in toks[:4]:
            d = self._resolve_day_token(m.group(2), m.group(1))
            if d is None:
                return None
            if d not in seen:
                seen.add(d)
                days.append((d, m.group(0)))
        return days if len(days) >= 2 else None

    def _answer_compound(self, days, who: str, rid: str) -> dict:
        """P6-a: the multi-day listing — one bounded walk over the
        configured calendars, each event filed under its LOCAL
        calendar date (RRULE occurrences expanded inside the covered
        window; an unsupported rule is NOTED, never dropped silently).
        Every scope day gets a section, empty or not — an honest
        multi-day answer never hides a day. Per-day titles capped at
        6, the rest are a count. Read-only."""
        now = datetime.now().astimezone()
        chron = sorted(days, key=lambda x: x[0])
        labels: list[str] = []
        for _, lab in chron:
            if lab not in labels:
                labels.append(lab)
        start_d = chron[0][0]
        end_d = chron[-1][0]
        ws = datetime.combine(start_d, datetime.min.time(),
                              tzinfo=now.tzinfo)
        we = datetime.combine(end_d + timedelta(days=1),
                              datetime.min.time(), tzinfo=now.tzinfo)
        day_set = {d for d, _ in days}
        by_day: dict = {d: [] for d in day_set}
        total = 0
        rrule_unexpanded = 0
        paths = (self._paths or os.environ.get("ZARA_ICS_PATH", ""))
        for path in paths.split(":"):
            path = path.strip()
            if not path:
                continue
            for ev in parse_ics(path)["events"]:
                conv = to_utc(ev["raw_dtstart"], ev["tzid"])
                if conv["dt"] is None:
                    continue
                summ = (ev["summary"] or "(no title)")
                summ += " (all-day)" if conv["allday"] else ""
                filed = False
                if ev.get("raw_rrule"):
                    exp = expand_rrule(conv["dt"], ev["raw_rrule"],
                                       we, ws)
                    if exp["supported"]:
                        for o in exp["occurrences"]:
                            ld = (o if o.tzinfo is None
                                  else o.astimezone()).date()
                            if ld in day_set:
                                by_day[ld].append(summ)
                                total += 1
                                filed = True
                        continue
                    rrule_unexpanded += 1
                ld = (conv["dt"] if conv["dt"].tzinfo is None
                      else conv["dt"].astimezone()).date()
                if ld in day_set and not filed:
                    by_day[ld].append(summ)
                    total += 1
        self._audit("calendar.compound_digest", who,
                    f"days={labels} total={total} rid={rid}")
        rrule_note = ""
        if rrule_unexpanded:
            rrule_note = (
                f" ({rrule_unexpanded} repeating event(s) use a rule I "
                "don't expand — each still shows once at its base "
                "occurrence.)")
        if total == 0:
            return {"message":
                    f"Nothing on the calendar for "
                    f"{' and '.join(labels)}.{rrule_note}",
                    "proposal": None, "resolution": None,
                    "executed": True, "verified": True,
                    "calendar_flow": "compound_digest"}
        parts = []
        for d in sorted(by_day):
            titles = by_day[d]
            shown = "; ".join(titles[:6])
            if len(titles) > 6:
                shown += f" +{len(titles) - 6} more"
            parts.append(f"{d.strftime('%a %b %d')}: "
                         f"{len(titles)} — {shown}")
        msg = (f"Calendar for {' and '.join(labels)} "
               f"({total} event(s)): " + "; ".join(parts)
               + "." + rrule_note)
        return {"message": msg,
                "proposal": None, "resolution": None,
                "executed": True, "verified": True,
                "calendar_flow": "compound_digest"}

    def _conflict_query(self, s: str):
        """P6-b (2026-10-24): "any conflicts tomorrow?" /
        "am I double-booked?" / "overlapping meetings this week" — a
        schedule-CONFLICT check, distinct from the free-gap report.
        Returns the scope (compound day list | week | single day |
        default 7-day sweep) plus its window, or None."""
        if not re.search(
                r"\b(?:any|what|which)\s+(?:schedule\s+)?conflicts?\b"
                r"|\b(?:schedule\s+)?conflicts?\s+(?:on|in|for|with)\b"
                r"|\bam i double.?booked\b|\bdouble.?booked\b"
                r"|\bconflict check\b|do i have (?:any )?conflicts\b"
                r"|\boverlapping\s+(?:meetings?|events?|appointments?)\b"
                r"|\b(?:meetings?|events?|appointments?)\s+"
                r"(?:overlap|clashes?)\b", s):
            return None
        days = self._compound_scope(s)
        if days is not None:
            chron = sorted(days, key=lambda x: x[0])
            labels: list[str] = []
            for _, lab in chron:
                if lab not in labels:
                    labels.append(lab)
            return {"kind": "compound", "days": chron,
                    "label": "for " + " and ".join(labels)}
        wk = self._week_scope(s)
        if wk is not None:
            return {"kind": "week", "days": None, "label": wk[1],
                    "start": wk[0]}
        ds = self._day_scope(s)
        if ds is not None:
            return {"kind": "day", "days": None, "label": ds[1],
                    "date": ds[0]}
        return {"kind": "sweep", "days": None,
                "label": "in the next 7 days"}

    @staticmethod
    def _hhmm(dt) -> str:
        lw = dt if dt.tzinfo is None else dt.astimezone()
        return lw.strftime("%H:%M")

    def conflicts_on_date(self, target):
        """P6-P25: the morning brief's source — the EXACT tested
        collection (._collect_pairs) over one LOCAL calendar day,
        start-of-day to end-of-day. Returns (n, up to 3 rendered
        pair strings, allday count, no-DTEND count); never raises."""
        try:
            now = datetime.now().astimezone()
            ws = datetime.combine(target, datetime.min.time(),
                                  tzinfo=now.tzinfo)
            we = datetime.combine(target, datetime.max.time(),
                                  tzinfo=now.tzinfo)
            pairs, allday, _, no_end = self._collect_pairs(ws, we)
            rendered = [
                f'{self._hhmm(s0)}–{self._hhmm(e0)} "{t1[:40]}" ↔ '
                f'"{t2[:40]}"'
                for s0, e0, t1, t2 in pairs[:3]]
            return len(pairs), rendered, allday, no_end
        except Exception:  # noqa: BLE001 — reads never break a turn
            return None

    def _collect_pairs(self, ws, we):
        """P6-P25 (2026-10-24): the conflict core, SEPARATED from its
        wording so other read-only surfaces (the morning brief) can
        reuse the exact tested collection. Returns (pairs, allday
        count, first all-day label, no-DTEND count) inside [ws, we).
        Per-event busy intervals (RRULE occurrences expanded with
        their base duration; no DTEND → the honest 2-hour default,
        counted); back-to-back is never a conflict."""
        iv: list[list] = []   # [start, end, summary]
        allday_count = 0
        allday_label = ""
        no_end_total = 0
        paths = (self._paths or os.environ.get("ZARA_ICS_PATH", ""))
        for path in paths.split(":"):
            path = path.strip()
            if not path:
                continue
            for ev in parse_ics(path)["events"]:
                conv = to_utc(ev["raw_dtstart"], ev["tzid"])
                if conv["dt"] is None:
                    continue
                if conv["allday"]:
                    d = (conv["dt"].date() if conv["dt"].tzinfo is None
                         else conv["dt"].astimezone().date())
                    # the day-scoped window ENDS inside the asked day
                    # (23:59:59); inclusive on both ends.
                    if ws.date() <= d <= (we - timedelta(seconds=1)).date():
                        allday_count += 1
                        allday_label = (allday_label
                                        or ev["summary"] or "(no title)")
                    continue
                starts: list = []
                if ev.get("raw_rrule"):
                    exp = expand_rrule(conv["dt"], ev["raw_rrule"], we, ws)
                    if exp["supported"]:
                        starts = exp["occurrences"]
                    else:
                        # unexpanded rule: the base occurrence only
                        starts = [conv["dt"]] if conv["dt"] < we else []
                else:
                    starts = [conv["dt"]]
                for st in starts:
                    if st >= we:
                        continue
                    if ev.get("raw_dtend"):
                        endc = to_utc(ev["raw_dtend"], ev["tzid"])
                        end = (endc["dt"] if endc["dt"] is not None
                               else st + timedelta(hours=2))
                        if endc["dt"] is None:
                            no_end_total += 1
                    else:
                        end = st + timedelta(hours=2)
                        no_end_total += 1
                    if end > ws and end > st:
                        iv.append([st, end,
                                   ev["summary"] or "(no title)"])
        iv.sort(key=lambda x: x[0])
        pairs: list[tuple] = []
        for i in range(len(iv)):
            for j in range(i + 1, len(iv)):
                if iv[j][0] >= iv[i][1]:
                    break
                s0 = max(iv[i][0], iv[j][0])
                e0 = min(iv[i][1], iv[j][1])
                if s0 < e0:
                    pairs.append((s0, e0, iv[i][2], iv[j][2]))
        return pairs, allday_count, allday_label, no_end_total

    def _answer_conflicts(self, q: dict, who: str, rid: str) -> dict:
        """P6-b: real OVERLAPS between TIMED events inside the asked
        window — sorted, then adjacent-sweep pairwise overlap
        detection. Bounded: at most 5 pairs shown, the rest is a
        count. All-day events are excluded (they have no clock times
        to collide with) but counted and named. Collection rides on
        _collect_pairs (also reused by the morning brief)."""
        now = datetime.now().astimezone()
        kind = q["kind"]
        if q["kind"] == "compound":
            chron = q["days"]
            ws = datetime.combine(chron[0][0], datetime.min.time(),
                                  tzinfo=now.tzinfo)
            we = datetime.combine(chron[-1][0] + timedelta(days=1),
                                  datetime.min.time(),
                                  tzinfo=now.tzinfo)
        elif kind == "week":
            start = q["start"]
            ws = datetime.combine(start, datetime.min.time(),
                                  tzinfo=now.tzinfo)
            we = datetime.combine(start + timedelta(days=7),
                                  datetime.min.time(),
                                  tzinfo=now.tzinfo)
        elif kind == "day":
            target = q["date"]
            ws = datetime.combine(target, datetime.min.time(),
                                  tzinfo=now.tzinfo)
            we = datetime.combine(target, datetime.max.time(),
                                  tzinfo=now.tzinfo)
        else:
            ws = now
            we = now + timedelta(days=7)
        label = q["label"]
        pairs, allday_count, allday_label, no_end_total = \
            self._collect_pairs(ws, we)
        n = len(pairs)
        self._audit("calendar.conflicts", who,
                    f"kind={kind} n={n} allday={allday_count} "
                    f"noend={no_end_total} rid={rid}")
        notes = ""
        tail = ""
        if no_end_total:
            tail += (f" ({no_end_total} event(s) without an end time "
                     "counted as 2 hours)")
        if allday_count:
            notes += (f" {allday_count} all-day event(s) skipped — "
                      "they have no clock times to collide with"
                      f" (first: {allday_label[:60]}).")
        if n == 0:
            msg = (f"No overlapping events {label}.{tail}"
                   + (notes if notes else ""))
            return {"message": msg,
                    "proposal": None, "resolution": None,
                    "executed": True, "verified": True,
                    "calendar_flow": "conflicts"}
        parts = []
        for s0, e0, t1, t2 in pairs[:5]:
            lw = (s0 if s0.tzinfo is None else s0.astimezone())
            day = lw.strftime("%a %b %d")
            parts.append(
                f"{day} {self._hhmm(s0)}–{self._hhmm(e0)}: "
                f'"{t1[:40]}" ↔ "{t2[:40]}"')
        more = f" +{n - 5} more overlap(s)" if n > 5 else ""
        msg = (f"{n} conflict(s) {label}: " + "; ".join(parts)
               + more + "." + tail + notes)
        return {"message": msg,
                "proposal": None, "resolution": None,
                "executed": True, "verified": True,
                "calendar_flow": "conflicts"}

    def _audit(self, action: str, who: str, detail: str = "") -> None:
        try:
            self.audit.record("calendar_flow", action, who, detail[:300])
        except Exception:  # noqa: BLE001
            pass

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        s = (clean or "").strip().lower()
        if not self.wants_turn(s):
            return None
        rid = uuid.uuid4().hex[:8]
        self._req_id = rid
        if not (self._paths or os.environ.get("ZARA_ICS_PATH", "")):
            self._audit("calendar.no_config", who, rid)
            return {"message":
                    "No calendar configured — point me at .ics "
                    "files with the ZARA_ICS_PATH environment "
                    "variable (colon-separated; each is a calendar "
                    "export) and ask again. I only read: I never "
                    "write or decline anything.",
                    "proposal": None, "resolution": None,
                    "executed": False, "calendar_flow": "no_config"}
        tq = self._title_query(s)
        if tq is not None:
            return self._answer_title_query(tq, who, rid)
        freeq = self._free_query(s)
        if freeq is not None:
            return self._answer_free_slots(freeq, who, rid)
        # P6-b: a conflict check answers BEFORE the week digest — "any
        # conflicts this week" asks for OVERLAPS, not a per-day
        # listing; its empty wording must never read as "nothing on
        # the calendar".
        cq = self._conflict_query(s)
        if cq is not None:
            return self._answer_conflicts(cq, who, rid)
        wk = self._week_scope(s)
        if wk is not None:
            return self._answer_week(wk, who, rid)
        # P6-a: a compound multi-day ask scopes the read to the named
        # days; single-day or unscoped asks fall through to the sweep.
        multi = self._compound_scope(s)
        if multi is not None:
            return self._answer_compound(multi, who, rid)
        if re.search(
                r"\b(am i free\b|when am i free\b|whats? my free\b|"
                r"what(?:'s|\s+is|\s+are) my free\b|when do i have "
                r"time\b|any free time\b)", s):
            self._audit("calendar.free_unsupported", who, rid)
            return {"message":
                    "I compute free time for a single named day "
                    "(today, tomorrow, a weekday) — ask e.g. 'when "
                    "am I free tomorrow' or 'am I free tomorrow at "
                    "3pm'.", "proposal": None, "resolution": None,
                    "executed": True, "verified": True,
                    "calendar_flow": "free_unsupported"}
        now = datetime.now().astimezone()
        scoped = self._day_scope(s)
        target, day_label = scoped if scoped else (None, "")
        past = target is not None and target < now.date()
        start_of_day = None
        if target is not None:
            end_of_day = datetime.combine(
                target, datetime.max.time(), tzinfo=now.tzinfo)
            start_of_day = datetime.combine(
                target, datetime.min.time(), tzinfo=now.tzinfo)
            horizon = max(now + timedelta(days=7), end_of_day)
        else:
            horizon = now + timedelta(days=7)
        today = now.date()
        shown, right_now, all_errors = [], None, []
        rrule_unexpanded = 0
        paths = (self._paths or os.environ.get("ZARA_ICS_PATH", ""))
        for path in paths.split(":"):
            path = path.strip()
            if not path:
                continue
            parsed = parse_ics(path)
            all_errors.extend(
                e for e in parsed["errors"] if e and "unreadable" in e)
            for ev in parsed["events"]:
                conv = to_utc(ev["raw_dtstart"], ev["tzid"])
                if conv["dt"] is None:
                    continue
                rr = ev.get("raw_rrule")
                occs = []
                if rr:
                    # P5 ⑪: a historical window must let the walker
                    # START in the past (the jump-start bucket anchors
                    # at the window start, which for a past day is
                    # its start-of-day) or yesterday's occurrences
                    # are invisible.
                    exp = expand_rrule(
                        conv["dt"], rr, horizon,
                        start_of_day if past else now)
                    if exp["supported"]:
                        occs = [o for o in exp["occurrences"]
                                if (start_of_day if past else now)
                                <= o <= horizon]
                if occs:
                    # Each generated occurrence keeps the event's base
                    # duration (or the 2-hour in-progress window when
                    # no DTEND is present) for the "right now" check.
                    dur = timedelta(hours=2)
                    if ev.get("raw_dtend"):
                        endc = to_utc(ev["raw_dtend"], ev["tzid"])
                        if endc["dt"] is not None:
                            dur = max(endc["dt"] - conv["dt"],
                                      timedelta(minutes=5))
                    for o in occs:
                        item = {"when": o,
                                "summary": ev["summary"] or "(no title)",
                                "allday": conv["allday"],
                                "unresolved": conv["unresolved"],
                                "is_today": o.date() == today}
                        if o <= now < o + dur:
                            right_now = right_now or item
                        shown.append(item)
                    continue
                if rr:
                    rrule_unexpanded += 1
                if past:
                    # P5 ⑪: a HISTORICAL day is answered by OVERLAP:
                    # the event ran during that day (start before its
                    # end-of-day, end after its start-of-day, DTEND
                    # honored; an all-day event covers the whole day
                    # regardless of clock time).
                    if not (start_of_day <= conv["dt"] <= end_of_day):
                        end_dt = conv["dt"] + timedelta(hours=2)
                        if conv["allday"]:
                            end_dt = conv["dt"].replace(
                                hour=0, minute=0, second=0,
                                microsecond=0) + timedelta(days=1)
                        elif ev.get("raw_dtend"):
                            endc = to_utc(ev["raw_dtend"], ev["tzid"])
                            if endc["dt"] is not None:
                                end_dt = max(end_dt, endc["dt"])
                        if not (conv["dt"] < end_of_day
                                and end_dt > start_of_day):
                            continue
                    shown.append({
                        "when": conv["dt"],
                        "summary": ev["summary"] or "(no title)",
                        "allday": conv["allday"],
                        "unresolved": conv["unresolved"],
                        "is_today": False})
                    continue
                if not (now <= conv["dt"] <= horizon):
                    # P5 ⑨ (2026-10-23): an event that already
                    # STARTED is not "upcoming", but while it is
                    # still running the honest answer to "what's
                    # next today?" is "Right now: …" — the DTEND
                    # (or the 2-hour default) decides "running".
                    end_dt = conv["dt"] + timedelta(hours=2)
                    if conv["allday"]:
                        # An all-day event runs its whole local day —
                        # midnight to midnight, not a 2-hour window.
                        end_dt = conv["dt"].replace(
                            hour=0, minute=0, second=0,
                            microsecond=0) + timedelta(days=1)
                    elif ev.get("raw_dtend"):
                        endc = to_utc(ev["raw_dtend"], ev["tzid"])
                        if endc["dt"] is not None:
                            end_dt = endc["dt"]  # DTEND wins
                    if conv["dt"] <= now < end_dt:
                        right_now = right_now or {
                            "when": conv["dt"],
                            "summary": ev["summary"] or "(no title)",
                            "allday": conv["allday"],
                            "unresolved": conv["unresolved"],
                            "is_today": conv["dt"].astimezone().date()
                            == today}
                    continue
                item = {"when": conv["dt"],
                        "summary": ev["summary"] or "(no title)",
                        "allday": conv["allday"],
                        "unresolved": conv["unresolved"],
                        "is_today": conv["dt"].date() == today}
                shown.append(item)
        shown.sort(key=lambda i: i["when"])
        if target is not None:
            def _ld(w):
                if w.tzinfo is not None:
                    w = w.astimezone()
                return w.date()
            shown = [i for i in shown if _ld(i["when"]) == target]
            if target != today:
                right_now = None
        shown = shown[:12]
        n = len(shown)
        self._audit("calendar.reported", who, f"count={n} rid={rid}")
        if n == 0 and right_now is None:
            lead = (f"Nothing on the calendar {day_label}"
                    if target is not None
                    else "Nothing on the calendar in the next 7 days")
            msg = (lead
                   + ("." if not all_errors
                      else " (some files were unreadable: "
                           + "; ".join(all_errors[:3]) + ")"))
        else:
            parts = []
            head = (f" Calendar for {day_label}: "
                    if target is not None else " Calendar, next 7 days: ")
            if right_now is not None:
                parts.append(
                    "Right now: " + right_now["summary"]
                    + (" (in progress)" if right_now["is_today"] else ""))
            for it in shown:
                # In a scoped answer every item IS the named local
                # day — the UTC-calendar label would be a lie (a
                # 23:30Z event labeled "today" can be tomorrow
                # locally in IST). The head already carries the day;
                # items keep stamp + title only.
                day = ("" if target is not None
                       else ("today " if it["is_today"]
                             else it["when"].strftime("%a %b %d") + " "))
                stamp = (it["when"].strftime("%H:%M")
                         if not it["allday"] else "all-day")
                tz_note = " [timezone unresolved]" if it["unresolved"] else ""
                parts.append(f"{day}{stamp}: {it['summary']}{tz_note}")
            rrule_note = ""
            if rrule_unexpanded:
                rrule_note = (
                    f" {rrule_unexpanded} repeating event(s) use a rule I "
                    "don't expand — each still shows once at its base "
                    "occurrence.")
            msg = (head + "; ".join(parts)
                   + "." + rrule_note
                   + (f" Unreadable files: {'; '.join(all_errors[:3])}"
                      if all_errors else ""))
        return {"message": msg,
                "proposal": None, "resolution": None,
                "executed": True, "verified": True,
                "calendar_flow": "reported"}
