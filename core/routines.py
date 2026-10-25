"""Routines (P6-b27, 2026-10-24) — named recurring plans.

Built ON the existing scheduler + notifications + audit (never a
second scheduler or engine), exactly like ReminderService: each
routine keeps exactly ONE live scheduler job, the next occurrence is
re-armed at fire time, delivery goes through the existing
NotificationManager, and every lifecycle change is audited. Zero
execution authority is added: the routine PLAN is opaque data, stored
verbatim and NEVER interpreted as an instruction — it never reaches
the resolver, policy, or executor. A routine firing is a REVIEW
PROMPT (the notification asks the user to run/acknowledge their plan),
never a run.

What makes a routine more than a reminder: it carries a stable NAME,
may repeat on a SET of weekdays ("every monday, wednesday, friday"),
and the weekly review flow lists them by name with their next fire.

Cadence semantics (all in the routine's stored zone, wall-clock
anchored like ReminderService):
- "daily"    → every local day at hour:minute.
- "weekly"   → every matching local day in `weekdays` (a non-empty
  ascending set of 0–6). Wall times inside a spring-forward gap map
  forward per fold=0 semantics (documented; ReminderService rule).
- Missed-run policy mirrors reminders: after downtime a due routine
  is announced ONCE (audited restart_receipt / missed_run by gap) and
  the schedule re-anchors forward. A routine never bursts.

NL surface (deterministic, no model; this file owns parse + exclude
guards). The word "routine" with "remind|reminder" present routes to
the reminder flow (exclusion guard — control asks never get swallowed).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time as _time
import uuid
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

# deterministic reuse: the clock parser and period-word constants are
# ReminderService's TESTED time grammar (never a second implementation)
from .reminders import (PERIOD_DEFAULTS, _clock_in_text,
                        ReminderTimeError, ReminderValueError)

_TEXT_MAX = 300
_NAME_MAX = 40
_MIN_LEAD_S = 5.0
_WEEKDAY_WORDS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                  "friday": 4, "saturday": 5, "sunday": 6}
_DAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
               "Saturday", "Sunday"]


def _fmt_weekdays(days: tuple[int, ...]) -> str:
    return ", ".join(_DAY_LABELS[d] for d in days) if days else ""


class RoutineService:
    """Owns routine rows; drives the EXISTING scheduler +
    notifications. Exactly ONE live scheduler job per routine; restart
    recovery reconciles from the additive `routines` table."""

    def __init__(self, scheduler, notifications, audit=None,
                 db_path: str = "",
                 now: Optional[Callable[[], float]] = None,
                 tz_name: Optional[str] = None,
                 env: Optional[dict] = None) -> None:
        self.sched = scheduler
        self.notifs = notifications
        self.audit = audit
        self._now = now or (lambda: _time.time())
        env = dict(os.environ if env is None else env)
        tz = (env.get("ZARA_TIMEZONE") or "").strip()
        if tz:
            ZoneInfo(tz)          # loud validation, like reminders
            self._tz = tz
        else:
            local = datetime.now().astimezone().tzinfo
            self._tz = getattr(local, "key", None) or "UTC"
        self._lock = threading.RLock()
        self._rows: dict[str, dict] = {}
        self._db = None
        if db_path:
            self._db = sqlite3.connect(db_path, check_same_thread=False)
            self._setup_db()
        self._recover()

    def _setup_db(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS routines ("
                "id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "plan TEXT NOT NULL, tz TEXT NOT NULL, "
                "cadence TEXT NOT NULL, due REAL NOT NULL, "
                "status TEXT NOT NULL, job_id TEXT, "
                "attempts INT DEFAULT 0, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL)")
            self._db.commit()

    def _audit(self, action: str, target: str = "", detail: str = ""):
        try:
            if self.audit is not None:
                self.audit.record("routines", action, target,
                                  detail[:500])
        except Exception:  # noqa: BLE001 — audit never breaks routines
            pass

    def _persist(self, row: dict) -> None:
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO routines (id, name, plan,"
                    " tz, cadence, due, status, job_id, attempts,"
                    " created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (row["id"], row["name"], row["plan"], row["tz"],
                     json.dumps(row["cadence"]), row["due"],
                     row["status"], row["job_id"], row["attempts"],
                     row["created_at"], _time.time()))
                self._db.commit()
        except Exception:  # noqa: BLE001 — memory state remains honest
            self._audit("routine_persist_failed", row["id"])

    def _load_rows(self) -> None:
        if self._db is None:
            return
        c = self._db.cursor()
        c.execute("SELECT id, name, plan, tz, cadence, due, status, "
                  "job_id, attempts, created_at FROM routines")
        for (rid, name, plan, tz, cad, due, status, job_id, attempts,
             created_at) in c.fetchall():
            try:
                cadence = json.loads(cad or "{}")
            except Exception:  # noqa: BLE001 — corrupt fields fail safe
                cadence = {}
            self._rows[rid] = {
                "id": rid, "name": name, "plan": plan, "tz": tz,
                "cadence": cadence if isinstance(cadence, dict) else {},
                "due": float(due), "status": status, "job_id": job_id,
                "attempts": int(attempts), "created_at":
                    float(created_at)}

    def _recover(self) -> None:
        """Restart reconciliation: a due-but-unannounced routine is
        announced ONCE (restart_receipt / missed_run), future jobs are
        re-armed when the scheduler lost them."""
        self._load_rows()
        now = self._now()
        for row in list(self._rows.values()):
            if row["status"] == "cancelled":
                continue
            due = row["due"]
            try:
                pending_job = any(
                    p.get("id") == row["job_id"]
                    for p in self.sched.pending())
            except Exception:  # noqa: BLE001
                pending_job = False
            if due <= now:
                note = ("missed_run" if self._now() - due > 120
                        else "restart_receipt")
                self._announce(row, due, note=note)
                self._rearm(row, self.next_fire(row, now))
            elif not pending_job and row["status"] == "scheduled":
                self._rearm(row, due)
            self._persist(row)

    class RoutineValueError(ValueError):
        """Hard refusal (invalid cadence / impossible schedule)."""

    # -- cadence math --------------------------------------------------

    def next_fire(self, row: dict, now_epoch: float) -> float:
        """Next epoch strictly > now + lead for the cadence's local
        wall time (daily, or any weekday in the weekly set)."""
        cadence = row["cadence"]
        kind = cadence.get("kind")
        hour, minute = int(cadence.get("hour", 0)), \
            int(cadence.get("minute", 0))
        tz = ZoneInfo(row["tz"])
        base = datetime.fromtimestamp(now_epoch, tz).date()
        days = None
        if kind == "weekly":
            days = tuple(sorted(
                set(int(d) for d in cadence.get("weekdays", [])
                    if isinstance(d, int) and 0 <= d <= 6)))
            if not days:
                raise self.RoutineValueError("Invalid cadence.")
        date = base
        for _ in range(460):
            if days is None or date.weekday() in days:
                epoch = datetime(date.year, date.month, date.day,
                                 hour, minute, fold=0,
                                 tzinfo=tz).timestamp()
                if epoch > now_epoch + _MIN_LEAD_S:
                    return epoch
            date += timedelta(days=1)
        raise self.RoutineValueError("Could not resolve a future run.")

    # -- CRUD ----------------------------------------------------------

    def create(self, name: str, plan: str, cadence: dict,
               who: str = "user") -> dict:
        """Validate + persist + schedule exactly ONE job. Duplicate
        names refuse (a routine is addressed by name — two rows with
        the same name would make cancel/find ambiguous)."""
        name = " ".join((name or "").split())[:_NAME_MAX]
        plan = " ".join((plan or "").split())[:_TEXT_MAX]
        if not name or len(name) < 2:
            raise self.RoutineValueError("A routine needs a name.")
        if not plan or len(plan) < 2:
            raise self.RoutineValueError(
                "What should the routine do? Say e.g. '… do the "
                "morning stretch'.")
        cadence = dict(cadence or {})
        kind = cadence.get("kind")
        hour, minute = cadence.get("hour"), cadence.get("minute")
        if kind not in ("daily", "weekly") or \
                not isinstance(hour, int) or not 0 <= hour <= 23 or \
                not isinstance(minute, int) or not 0 <= minute <= 59:
            raise self.RoutineValueError("Invalid cadence.")
        if kind == "weekly":
            days = tuple(sorted(set(
                int(d) for d in cadence.get("weekdays", [])
                if isinstance(d, int) and 0 <= d <= 6)))
            if not days:
                raise self.RoutineValueError("Invalid cadence.")
            cadence["weekdays"] = list(days)
        with self._lock:
            for r in self._rows.values():
                if r["status"] != "cancelled" and \
                        r["name"].lower() == name.lower():
                    raise self.RoutineValueError(
                        f"A routine named '{r['name']}' already "
                        f"exists. Cancel it first or pick another "
                        f"name.")
        now = self._now()
        rid = f"rou-{uuid.uuid4().hex[:12]}"
        row = {"id": rid, "name": name, "plan": plan, "tz": self._tz,
               "cadence": cadence, "due": 0.0, "status": "scheduled",
               "job_id": "", "attempts": 0, "created_at": now}
        due = self.next_fire(row, now)
        row["due"] = float(due)
        delay = max(0.05, due - now)
        try:
            jid = self.sched.schedule_once(
                "routine", delay,
                {"routine": {"id": rid, "due": float(due)}})
        except Exception as e:
            raise self.RoutineValueError(
                f"scheduling failed: {e}") from e
        row["job_id"] = jid
        with self._lock:
            self._rows[rid] = row
        self._persist(row)
        self._audit("routine_created", rid,
                    f"name={name} cadence={json.dumps(cadence)} "
                    f"due={due:.0f} tz={self._tz} who={who}")
        return self._summary(row)

    def list_rows(self) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._rows.values()
                    if r["status"] == "scheduled"]
        rows.sort(key=lambda r: r["due"])
        return [self._summary(r) for r in rows]

    def _resolve(self, name: str) -> tuple[Optional[dict], str]:
        target = (name or "").strip().lower()
        with self._lock:
            rows = [dict(r) for r in self._rows.values()
                    if r["status"] == "scheduled"]
        hits = [r for r in rows if r["name"].lower() == target] \
            if target else []
        if not hits and target:
            hits = [r for r in rows if target in r["name"].lower()]
            if len(hits) > 1:
                return None, f"'{name}' matches several routines."
        if len(hits) != 1:
            return None, ("No scheduled routine with that name. Say "
                          "'show my routines'.")
        with self._lock:
            return dict(self._rows[hits[0]["id"]]), ""

    def cancel(self, name: str, who: str = "user") -> dict:
        row, err = self._resolve(name)
        if row is None:
            self._audit("routine_cancel_refused", (name or "")[:60],
                        err)
            return {"message": err, "cancelled": False}
        try:
            self.sched.cancel(row["job_id"])
        except Exception:  # noqa: BLE001 — still marked cancelled
            pass
        with self._lock:
            self._rows[row["id"]]["status"] = "cancelled"
            self._persist(self._rows[row["id"]])
        self._audit("routine_cancelled", row["id"], f"who={who}")
        return {"message": f"Cancelled routine '{row['name']}'.",
                "cancelled": True}

    # -- firing ----------------------------------------------------------

    def on_fire(self, job: dict) -> None:
        """Scheduler fire hook: ANNOUNCE the routine (a review prompt,
        never a run), then re-arm the next occurrence."""
        payload = ((job.get("payload") or {}).get("routine") or {})
        rid = str(payload.get("id", ""))
        with self._lock:
            row = dict(self._rows.get(rid) or {}) or None
        if row is None or row["status"] == "cancelled":
            return
        due = float(payload.get("due", row["due"]))
        try:
            cancelled = self.sched.is_cancelled(job.get("id", ""))
        except Exception:  # noqa: BLE001
            cancelled = False
        if cancelled:
            return
        self._announce(row, due)
        self._rearm(row, self.next_fire(row, self._now()))

    def _announce(self, row: dict, due: float, note: str = "") -> None:
        try:
            self.notifs.create(
                title=f"Routine: {row['name'][:60]}",
                body=f"{row['plan'][:200]}" +
                     (f" ({note})" if note else ""),
                device_id=row.get("device_id") or None)
        except Exception as e:  # noqa: BLE001 — honest failure
            with self._lock:
                live = self._rows[row["id"]]
                live["attempts"] = int(live["attempts"]) + 1
                self._persist(live)
            self._audit("routine_announce_failed", row["id"],
                        str(e)[:200])
            return
        with self._lock:
            live = self._rows[row["id"]]
            live["attempts"] = 0
            self._persist(live)
        self._audit("routine_announced", row["id"],
                    f"due={due:.0f}"
                    f"{' ' + note if note else ''}".strip())

    def _rearm(self, row: dict, due: float) -> None:
        now = self._now()
        delay = max(0.05, due - now)
        jid = self.sched.schedule_once(
            "routine", delay,
            {"routine": {"id": row["id"], "due": float(due)}})
        with self._lock:
            live = self._rows[row["id"]]
            live["due"] = float(due)
            live["job_id"] = jid
            live["status"] = "scheduled"
            self._persist(live)
        self._audit("routine_next_scheduled", row["id"],
                    f"due={due:.0f} job={jid}")

    def _fmt(self, due: float, tz_name: str) -> str:
        try:
            return datetime.fromtimestamp(
                due, ZoneInfo(tz_name)).strftime("%a %b %d, %H:%M")
        except Exception:  # noqa: BLE001
            return datetime.fromtimestamp(
                due).strftime("%a %b %d, %H:%M")

    def _summary(self, row: dict) -> dict:
        cadence = row["cadence"]
        if cadence.get("kind") == "daily":
            rec = "daily"
        else:
            rec = f"weekly ({_fmt_weekdays(cadence.get('weekdays', []))})"
        return {"id": row["id"], "name": row["name"],
                "plan": row["plan"], "cadence": cadence,
                "cadence_label": rec,
                "next": self._fmt(row["due"], row["tz"]),
                "next_epoch": row["due"], "tz": row["tz"],
                "status": row["status"]}


# ------------------------------------------------------------------
# NL intent parsing — routine CONTROL (deterministic, no model)
# ------------------------------------------------------------------

_DAYSPEC_RE = re.compile(
    r"\b(?P<spec>day(?:s)?|weekday|weekdays|weekend|weekends|"
    r"(?:s?un|mon|tues|wednes|thur|thurs|fri|satur)day"
    r"(?:\s*(?:,|and)\s*(?:s?un|mon|tues|wednes|thur|thurs|fri|satur)"
    r"day)*)", re.I)
_FRAG_RE = re.compile(
    r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:rs?"
    r"(?:day)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b")


def parse_dayspec(text: str) -> Optional[tuple[int, ...]]:
    """'day' → all seven; 'weekdays' → Mon–Fri; 'weekends' → Sat+Sun;
    a list of weekday words (', ' and ' and' separated) → that set.
    Full names and common abbreviations (mon, weds, fri) both work.
    Returns None when nothing parses."""
    low = (text or "").lower().strip()
    if not low:
        return None
    if low in ("day", "days"):
        return (0, 1, 2, 3, 4, 5, 6)
    if low == "weekday" or low == "weekdays":
        return (0, 1, 2, 3, 4)
    if low == "weekend" or low == "weekends":
        return (5, 6)
    frags = [f.strip(" ,") for f in re.split(r",|and", low) if f.strip()]
    days: set[int] = set()
    for frag in frags:
        m = _FRAG_RE.match(frag)
        if m is None:
            return None
        w = m.group(1).lower()
        if w.startswith("mo"):
            days.add(0)
        elif w.startswith("tu"):
            days.add(1)
        elif w.startswith("we"):
            days.add(2)
        elif w.startswith("th"):
            days.add(3)
        elif w.startswith("fr"):
            days.add(4)
        elif w.startswith("sa"):
            days.add(5)
        elif w.startswith("su"):
            days.add(6)
    if not days:
        return None
    return tuple(sorted(days))


def parse_routine_intent(utterance: str) -> dict:
    """Deterministic op parse. Returns {"op": create|list|cancel|find|
    unknown, ...}. Nothing here executes."""
    raw = " ".join((utterance or "").strip().split())
    low = raw.lower()
    m = re.fullmatch(r"(?:show|list|what are|tell me)(?: me)?(?: my|"
                     r" the| all)? routines?[\s.,!?]*", low)
    if m:
        return {"op": "list"}
    m = re.fullmatch(r"(?:cancel|delete|remove|stop)(?: the| my)?\s*"
                     r"routine (?:called )?(?P<name>[a-z0-9][a-z0-9 "
                     r"'\-]{0,38})[\s.,!?]*", low)
    if m:
        return {"op": "cancel", "name": m.group("name")}
    m = re.fullmatch(r"(?:when|next time) (?:is|does run|will run)?\s*"
                     r"(?:the )?(?P<name>[a-z0-9][a-z0-9 '\-]{0,38})"
                     r"\s+routine[\s.?]*|"
                     r"next run of (?:the )?(?P<name2>[a-z0-9][a-z0-9"
                     r" '\-]{0,38}) routine[\s.?]*", low)
    if m:
        return {"op": "find",
                "name": m.group("name") or m.group("name2") or ""}
    # create: "add routine <name> every <days> at <clock> do <plan>"
    m = re.search(
        r"\broutine\s+(?:(?:called|named)\s+)?"
        r"(?P<name>[a-z0-9][a-z0-9 '\-]{0,38}?)\s+every"
        r"\s+(?P<days>[a-z][a-z, '\-]*?)\s+at\s+(?P<clock>\d{1,2}"
        r"(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?|noon|midnight)\s+"
        r"(?:do|to|:)\s*(?P<plan>.+)$", low)
    if m:
        days = parse_dayspec(m.group("days"))
        if days is None:
            return {"op": "clarify",
                    "message": "Which days? Say 'every day', "
                               "'every weekday', 'every weekend', or "
                               "e.g. 'every monday, wednesday and "
                               "friday'."}
        try:
            clock = _clock_in_text("at " + m.group("clock").strip())
        except ReminderTimeError as e:
            return {"op": "clarify", "message": str(e)}
        if clock is None:
            return {"op": "clarify",
                    "message": "I couldn't find the time of day. Say "
                               "e.g. 'at 7 AM'."
                               " Example: 'add routine workout every "
                               "day at 7 AM to warm up and stretch'."}
        hour, minute = clock[0], clock[1]
        return {"op": "create", "name": m.group("name"),
                "plan": m.group("plan").strip(" ,."),
                "cadence": {"kind": "daily" if len(days) == 7 else
                            "weekly",
                            "hour": hour, "minute": minute,
                            "weekdays": list(days)}}
    # create without clock: "... every <days> do <plan>" is refused —
    # a routine always carries an explicit wall time
    if re.search(r"\broutine\b", low) and \
            re.search(r"\bevery\b.*\b(?:do|to)\b", low):
        return {"op": "clarify",
                "message": "A routine needs a time of day. Say e.g. "
                           "'add routine workout every day at 7 AM to "
                           "warm up'."}
    # a creation ask with no every-spec is taught, never guessed
    # (calendar/meeting asks never land here — they belong to their
    # own flows)
    if re.search(r"^(?:add|create|set(?:\s+up)?|new)\b", low) and \
            re.search(r"\broutine\b", low) and \
            not re.search(r"\b(?:calendar|appointment|meeting|event)"
                          r"\b", low):
        return {"op": "clarify",
                "message": "Routines look like: 'add routine workout "
                           "every monday, wednesday and friday at 7 "
                           "AM do the morning stretch'."}
    return {"op": "unknown"}


def is_routine_utterance(clean: str) -> bool:
    """Anchor word 'routine' with an EXCLUSION guard: reminder-shaped
    control asks never land here (they belong to the reminder flow)."""
    return bool(re.search(r"\broutine", clean or "")) and \
        not re.search(r"\bremind", clean or "")


class RoutineFlow:
    """Turn-shaped deterministic surface over RoutineService for the
    IntentTurnService flow loop: `wants_turn` owns the exclusion guard
    (reminder-shaped asks never land here), `turn` never executes."""

    def __init__(self, service: RoutineService) -> None:
        self.service = service

    def wants_turn(self, clean: str) -> bool:
        return is_routine_utterance(clean)

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        return handle_routine_utterance(self.service, clean, who)


def handle_routine_utterance(service: RoutineService, utterance: str,
                             who: str = "user") -> Optional[dict]:
    """Parse + dispatch one routine utterance into a turn-shaped
    result. `executed` is always False: routines touch notifications,
    never devices or tools. Unknown op shapes return None so other
    flows still see the utterance."""
    op = parse_routine_intent(utterance)
    kind = op.get("op")
    if kind == "unknown":
        return None
    if kind == "clarify":
        return {"message": op["message"], "executed": False,
                "clarified": True}
    if kind == "list":
        rows = service.list_rows()
        if not rows:
            return {"message": "No routines yet. Add one, e.g. 'add "
                               "routine workout every day at 7 AM to "
                               "warm up and stretch'.",
                    "executed": False, "routines": []}
        body = "; ".join(
            f"'{r['name']}' ({r['cadence_label']}, next {r['next']})"
            for r in rows[:5])
        more = f" — and {len(rows) - 5} more" if len(rows) > 5 else ""
        return {"message": f"{len(rows)} routine(s): {body}{more}.",
                "executed": False, "routines": rows}
    name = (op.get("name") or "").strip()
    if kind == "find":
        row, err = service._resolve(name)
        if row is None:
            return {"message": err, "executed": False}
        s = service._summary(row)
        return {"message": f"Routine '{s['name']}' runs "
                           f"{s['cadence_label']} at "
                           f"{s['next'].split(', ', 1)[-1]}, next on "
                           f"{s['next']}.",
                "executed": False, "routine": s}
    if kind == "cancel":
        out = service.cancel(name, who)
        return {"message": out["message"], "executed": False,
                "cancelled": out["cancelled"]}
    if kind == "create":
        try:
            s = service.create(op["name"], op["plan"], op["cadence"],
                               who=who)
        except (RoutineService.RoutineValueError, ReminderValueError) \
                as e:
            return {"message": f"I won't set that: {e}",
                    "executed": False, "refused": True}
        inst = s["cadence_label"]
        hh = int(s["cadence"].get("hour", 0))
        mm = int(s["cadence"].get("minute", 0))
        clock = f"{hh % 12 or 12}:{mm:02d} " + ("AM" if hh < 12 else
                                               "PM")
        return {"message": f"Routine set: '{s['name']}' — {inst} at "
                           f"{clock}, first run {s['next']}. I'll "
                           f"prompt you; nothing runs on its own.",
                "executed": False, "routine": s}
    return {"message": "I couldn't process that routine.",
            "executed": False}
