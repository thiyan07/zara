"""Habit chains (P6-b33 / batch-32, 2026-10-26) — named recurring
habits with done-marks and honest streaks.

Design adopted from proven repos (interface/design ONLY, never their
code — stdlib-only is pinned for this repo):
- Loop Habit Tracker (github.com/iSoron/uhabits, GPL-3.0, ~10.3k★):
  a mark belongs to a DAY; streaks are consecutive SCHEDULED-day runs
  (the frequency filter runs before streak math); "best" is the
  longest run ever; today-not-marked never shortens the chain — a
  chain breaks only when a scheduled occurrence that already PASSED
  went unmarked. Skipped/unmarked days are simply absent from the
  marked set (nothing invented).
- todo.txt grammar (github.com/todotxt, GPL-3.0): nothing here — the
  capture flow adopts that grammar (see capture.py).

Honesty rules (same posture as ledgers/capture):
- a habit IS the user's own diary: deterministic parse, validated
  values, audited — no policy/execution authority and no device
  actions are added (data + reports only, like ReminderService);
- marks are stored with the HOST-local date; there is no "backdate a
  mark" guessing in v1, and marking twice on the same day is
  idempotent (the chain does not grow twice);
- reports show BOTH the run length and whether today is already
  marked, and when a scheduled occurrence that already passed went
  unmarked they say the run ENDED there — never "streak 12" while
  yesterday was missed;
- cadence in v1 is daily or an explicit weekday list (a weekly-count
  target needs a different chain algorithm — refused with the exact
  reason, never guessed).

The ledger's free-text "habit" diary entries (core/ledgers.py) are a
DIFFERENT surface and keep working: routed phrases must not collide
(see HabitsFlow.wants_turn / known-name resolution below).
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time as _time
import uuid
from datetime import date, datetime, timedelta
from typing import Callable, Optional

_NAME_MAX = 40
_WALK_CAP = 400          # occurrences scanned at most (bound the math)
_WDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
          "saturday", "sunday")


def _local_date(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().strftime(
        "%Y-%m-%d")


class HabitValueError(ValueError):
    """Hard refusal (invalid name / cadence)."""


class HabitUnknownError(ValueError):
    """The habit name is not tracked — the flow returns None on this
    so other flows still see the utterance; direct callers get the
    error."""


class HabitService:
    """Two additive tables (habits + marks) in one sqlite; memory
    fallback when no db path."""

    def __init__(self, audit=None, db_path: str = "",
                 now: Optional[Callable[[], float]] = None) -> None:
        self.audit = audit
        self._now = now or (lambda: _time.time())
        self._lock = threading.RLock()
        self._db = None
        self._habits: list[dict] = []      # memory store rows
        self._mem_marks: dict[str, set] = {}  # habit_id -> {date iso}
        if db_path:
            self._db = sqlite3.connect(db_path,
                                       check_same_thread=False)
            self._setup_db()

    def _setup_db(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS habit_defs ("
                "id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "days TEXT NOT NULL, created REAL NOT NULL, "
                "who TEXT)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS habit_marks ("
                "habit_id TEXT NOT NULL, day TEXT NOT NULL, "
                "at REAL NOT NULL, "
                "PRIMARY KEY (habit_id, day))")
            self._db.commit()

    def _audit(self, action: str, who: str = "user",
               detail: str = "") -> None:
        try:
            if self.audit is not None:
                self.audit.record("habits", action, who, detail[:200])
        except Exception:  # noqa: BLE001 — audit never breaks marks
            pass

    # -- writes ---------------------------------------------------------

    def create(self, name: str, cadence: str) -> dict:
        name = " ".join((name or "").split())
        if not name or len(name) < 2 or len(name) > _NAME_MAX:
            raise HabitValueError(
                "Habit name must be 2–40 characters.")
        days = self._parse_days(cadence)
        with self._lock:
            if self._find(name) is not None:
                raise HabitValueError(
                    f"A habit called '{name}' is already tracked. Say "
                    f"'show my habits'.")
            row = {"id": f"hb-{uuid.uuid4().hex[:10]}", "name": name,
                   "days": ",".join(str(d) for d in
                                    (range(7) if days is None
                                     else sorted(days))),
                   "created": self._now(), "who": "user"}
            if self._db is not None:
                self._db.execute(
                    "INSERT INTO habit_defs (id, name, days,"
                    " created, who) VALUES (?,?,?,?,?)",
                    (row["id"], row["name"], row["days"],
                     row["created"], row["who"]))
                self._db.commit()
            else:
                self._habits.append(row)
                self._mem_marks[row["id"]] = set()
        self._audit("habit_created", f"{name!r} days={row['days']}")
        return row

    def _parse_days(self, cadence: str) -> Optional[set]:
        """None = daily (all 7); a set = the explicit weekdays."""
        c = " ".join((cadence or "").strip().lower().split())
        c = re.sub(r"^(?:every|each)\s+", "", c)
        if c in ("", "day", "daily", "every day", "everyday",
                 "wholeday"):
            return None
        if c in ("weekday", "weekdays", "weekdays only", "workdays",
                 "business days", "every weekday"):
            return set(range(5))
        toks = [t for t in re.split(r",|\band\b|\s+", c) if t]
        if not toks:
            raise HabitValueError("Which days? Say e.g. 'every day' "
                                  "or 'every monday and thursday'.")
        if any(t in ("week", "weekly") for t in toks):
            raise HabitValueError(
                "Weekly-count habits need a different streak "
                "algorithm — this build does daily or specific "
                "weekdays.")
        days: set = set()
        for t in toks:
            for i, w in enumerate(_WDAYS):
                if w.startswith(t[:3]) and len(t[:3]) == 3:
                    days.add(i)
                    break
            else:
                raise HabitValueError(
                    f"Unknown day word '{t}' — say e.g. 'every "
                    f"monday and thursday'.")
        if not days:
            raise HabitValueError("Which days of the week?")
        return days

    def _find(self, name: str) -> Optional[dict]:
        low = (name or "").strip().lower()
        for r in (self._db_rows() if self._db is not None
                  else list(self._habits)):
            if r["name"].lower() == low:
                return r
        return None

    def _db_rows(self) -> list[dict]:
        with self._lock:
            rs = self._db.execute(
                "SELECT id, name, days, created, who FROM habit_defs"
                ).fetchall()
        return [{"id": r[0], "name": r[1], "days": r[2],
                 "created": r[3], "who": r[4]} for r in rs]

    def mark(self, name: str) -> dict:
        row = self._find(name)
        if row is None:
            raise HabitUnknownError(
                f"No habit called '{(name or '').strip()}'. Say "
                f"'show my habits'.")
        today = self._today()
        if self._marked(row["id"], today):
            s = self._streak_stats(row)
            self._audit("habit_mark_idempotent", row["name"])
            return {"row": row, "dup": True, "day": today,
                    "stats": s}
        now = self._now()
        if self._db is not None:
            with self._lock:
                self._db.execute(
                    "INSERT OR IGNORE INTO habit_marks (habit_id,"
                    " day, at) VALUES (?,?,?)",
                    (row["id"], today, now))
                self._db.commit()
        else:
            self._mem_marks.setdefault(row["id"], set()).add(today)
        s = self._streak_stats(row)
        self._audit("habit_marked", row["name"], today)
        return {"row": row, "dup": False, "day": today, "stats": s}

    def _marked(self, habit_id: str, day: str) -> bool:
        if self._db is not None:
            with self._lock:
                got = self._db.execute(
                    "SELECT 1 FROM habit_marks WHERE habit_id=? AND"
                    " day=?", (habit_id, day)).fetchone()
            return got is not None
        return day in self._mem_marks.get(habit_id, set())

    def _marks(self, habit_id: str) -> set:
        if self._db is not None:
            with self._lock:
                rs = self._db.execute(
                    "SELECT day FROM habit_marks WHERE habit_id=?",
                    (habit_id,)).fetchall()
            return {r[0] for r in rs}
        return set(self._mem_marks.get(habit_id, set()))

    def delete(self, name: str) -> dict:
        row = self._find(name)
        if row is None:
            raise HabitUnknownError(
                f"No habit called '{(name or '').strip()}'.")
        if self._db is not None:
            with self._lock:
                self._db.execute(
                    "DELETE FROM habit_defs WHERE id=?", (row["id"],))
                self._db.execute(
                    "DELETE FROM habit_marks WHERE habit_id=?",
                    (row["id"],))
                self._db.commit()
        else:
            self._habits = [r for r in self._habits
                            if r["id"] != row["id"]]
            self._mem_marks.pop(row["id"], None)
        self._audit("habit_deleted", row["name"])
        return row

    # -- reads ----------------------------------------------------------

    def _today(self) -> str:
        return _local_date(self._now())

    def list_rows(self) -> list[dict]:
        rows = self._db_rows() if self._db is not None \
            else list(self._habits)
        out = []
        for r in sorted(rows, key=lambda x: x["created"]):
            s = self._streak_stats(r)
            out.append({"name": r["name"], "label": self._label(r),
                        "stats": s})
        return out

    def any(self) -> bool:
        return bool(self._db_rows() if self._db is not None
                    else self._habits)

    def _label(self, row: dict) -> str:
        days = row["days"] or ""
        if days.count(",") == 6 or set(self._days_set(row)) == \
                set(range(7)):
            return "daily"
        names = [_WDAYS[i] for i in self._days_set(row)]
        if names == _WDAYS[:5]:
            return "weekdays"
        return "every " + ", ".join(names[:-1]) + (" and " +
                                                   names[-1]
                                                   if len(names) > 1
                                                   else names[0]) \
            if names else "daily"

    def _days_set(self, row: dict) -> set:
        raw = (row["days"] or "").strip()
        if not raw:
            return set(range(7))
        return {int(x) for x in raw.split(",") if x != ""}

    def _streak_stats(self, row: dict) -> dict:
        marks = self._marks(row["id"])
        days = self._days_set(row)
        today = date.fromisoformat(self._today())
        if not marks:
            return {"current": 0, "today_marked": False, "best": 0,
                    "closed": True, "last": "", "n": 0}
        occ = []
        d = date.fromisoformat(min(marks))
        while d <= today and len(occ) < _WALK_CAP:
            if d.weekday() in days:
                occ.append(d.isoformat())
            d += timedelta(days=1)
        runs: list[list[str]] = []
        run: list[str] = []
        for dayiso in occ:
            if dayiso in marks:
                run.append(dayiso)
            else:
                if run:
                    runs.append(run)
                run = []
        if run:
            runs.append(run)
        best = max((len(r) for r in runs), default=0)
        last = runs[-1] if runs else []
        # the chain closes only when an occurrence that already
        # PASSED (strictly before today) went unmarked; today's
        # unmarked occurrence is still open (uhabits semantics)
        last_iso = last[-1] if last else ""
        closed = False
        if last_iso:
            end = date.fromisoformat(last_iso)
            d = end + timedelta(days=1)
            while d < today:
                if d.weekday() in days and \
                        (d.isoformat() not in marks):
                    closed = True
                    break
                d += timedelta(days=1)
        return {"current": len(last), "today_marked": today.isoformat()
                in marks, "best": best, "closed": closed,
                "last": last_iso, "n": len(marks)}

    def status_line(self, row: dict) -> str:
        return _row_line({"name": row["name"],
                          "label": self._label(row),
                          "stats": self._streak_stats(row)})


# ------------------------------------------------------------------
# NL surface — deterministic parse (no model), honest shapes
# ------------------------------------------------------------------

_EXCLUDE_RE = re.compile(
    r"\b(remind|reminder|routine|calendar|appointment|meeting|"
    r"grocer|expense|water|medication)\b", re.I)
_CREATE_RE = re.compile(
    r"^(?:add|start|create|track)\s+(?:an?\s+)?habits?\s+(?P<n>.+?)"
    r"(?:\s+every\s+(?P<d>.+?))?\s*\.*$", re.I)
_MARK_RE = re.compile(
    r"^(?:mark\s+)?(?P<n>.+?)\s+(?:is\s+)?done\s*\.*$|"
    r"^(?:i\s+)?(?:did|finished|completed)\s+(?:my\s+|the\s+)?"
    r"(?P<n2>.+?)\s*\.*$", re.I)
_LIST_RE = re.compile(
    r"^(?:show|what are|list)(?: my)?\s+habits?\s*$|"
    r"^(?:my|the)\s+habit\s+chains?\s*$|"
    r"^(?:show|show my)\s+(?:habit\s+)?streaks?\s*\.*$", re.I)
_CANCEL_RE = re.compile(
    r"^(?:cancel|remove|delete|stop tracking)\s+(?:the\s+|my\s+)?"
    r"habits?\s+(?P<n>.+?)\s*\.*$", re.I)


def _resolve_name(service: HabitService, raw_name: str) -> str:
    """Known-name resolution: tokens of the ask must all be prefixes
    of the tracked name; never guess beyond the tracked list."""
    name = re.sub(r"\s+habit$", "", (raw_name or "").strip(),
                  flags=re.I)
    name = re.sub(r"^my\s+", "", name, flags=re.I)
    name = " ".join(name.split()).strip(" .,!?")
    if not name:
        return ""
    row = service._find(name)
    if row is not None:
        return row["name"]
    # fallback: whole-phrase prefix match over tracked names
    low = name.lower()
    for r in (service._db_rows() if service._db is not None
              else list(service._habits)):
        if r["name"].lower().startswith(low):
            return r["name"]
    for r in (service._db_rows() if service._db is not None
              else list(service._habits)):
        if low in r["name"].lower():
            return r["name"]
    return ""


def _row_line(r: dict) -> str:
    s = r["stats"]
    if s["closed"] and s["last"]:
        return (f"'{r['name']}' ({r['label']}): last run "
                f"{s['current']} day(s), ended {s['last']}; best "
                f"{s['best']}, {s['n']} marks.")
    if s["current"] == 0:
        return (f"'{r['name']}' ({r['label']}): no marks yet — say "
                f"'mark {r['name']} done' to start the chain.")
    today_note = ("marked today" if s["today_marked"] else
                  "today not marked yet")
    return (f"'{r['name']}' ({r['label']}): streak {s['current']} "
            f"({today_note}), best {s['best']}, {s['n']} marks.")


def parse_habit_intent(utterance: str) -> dict:
    raw = " ".join((utterance or "").strip().split())
    low = raw.lower()
    if _EXCLUDE_RE.search(low) and \
            not re.match(r"^(?:add|start|create|track|cancel|remove|"
                         r"delete|stop)\s+(?:an?\s+)?habits?\b", low):
        return {"op": "unknown"}
    if _LIST_RE.match(raw):
        return {"op": "list"}
    m = _CREATE_RE.match(raw)
    if m:
        return {"op": "create", "name": m.group("n").strip(),
                "cadence": (m.group("d") or "").strip() or "day"}
    m = _CANCEL_RE.match(raw)
    if m:
        return {"op": "delete", "name": m.group("n")}
    m = _MARK_RE.match(raw)
    if m:
        name = m.group("n") or m.group("n2") or ""
        return {"op": "mark", "name": name}
    return {"op": "unknown"}


def handle_habit_utterance(service: HabitService, utterance: str,
                           who: str = "user") -> Optional[dict]:
    """Parse + dispatch one habit utterance. Returns None when the ask
    is not shaped like the habit flow at all (other flows still see
    it); returns an honest refusal/clarify when it IS habit-shaped but
    unresolvable."""
    op = parse_habit_intent(utterance)
    kind = op.get("op")
    if kind == "unknown":
        return None
    if kind == "list":
        rows = service.list_rows()
        if not rows:
            return {"message": "No habits tracked yet. Add one, e.g. "
                               "'add habit gym every day'.",
                    "executed": False, "habits": []}
        body = "; ".join(_row_line(r) for r in rows[:10])
        return {"message": "Habits — " + body,
                "executed": False, "habits": rows}
    if kind == "create":
        try:
            row = service.create(op["name"], op["cadence"])
        except HabitValueError as e:
            return {"message": f"I won't track that habit: {e}",
                    "executed": False, "refused": True}
        return {"message": f"Habit set: '{row['name']}' — "
                           f"{service._label(row)}. I'll count the "
                           f"chain when you mark it ('mark "
                           f"{row['name']} done'); nothing runs on "
                           f"its own.",
                "executed": False, "created": row["name"]}
    if kind == "mark":
        name = _resolve_name(service, op["name"])
        if not name:
            return None      # not a known habit → other flows/parser
        try:
            res = service.mark(name)
        except HabitValueError as e:
            return {"message": f"I couldn't mark that: {e}",
                    "executed": False, "refused": True}
        stats = res["stats"]
        if res["dup"]:
            return {"message": f"'{name}' is already marked today — "
                               f"the chain does not grow twice "
                               f"(current streak {stats['current']}"
                               f").",
                    "executed": False, "dup": True}
        extra = "" if stats["today_marked"] else ""
        return {"message": f"Marked '{name}' done for {res['day']} — "
                           f"streak now {stats['current']} (best "
                           f"{stats['best']}).{extra}",
                "executed": False, "marked": name}
    if kind == "delete":
        name = _resolve_name(service, op["name"])
        if not name:
            return None
        row = service.delete(name)
        return {"message": f"Deleted habit '{row['name']}' and its "
                           f"marks — the chain is gone from the "
                           f"lists.",
                "executed": False, "deleted": row["name"]}
    return {"message": "I couldn't process that habit ask.",
            "executed": False}


class HabitsFlow:
    """Turn-shaped surface for the IntentTurnService flow loop."""

    def __init__(self, service: HabitService) -> None:
        self.service = service

    def wants_turn(self, clean: str) -> bool:
        c = (clean or "").strip()
        if not c:
            return False
        if _EXCLUDE_RE.search(c) and \
                not re.match(r"^(?:add|start|create|track|cancel|"
                             r"remove|delete|stop)\s+(?:an?\s+)?"
                             r"habits?\b", c, re.I):
            return False
        return bool(re.search(
            r"\bhabits?\b|\bchains?\b|\bstreaks?\b|^\s*mark\s+\S+|"
            r"^(?:i\s+)?(?:did|finished|completed)\s+\S+", c, re.I))

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        return handle_habit_utterance(self.service, clean, who)
