"""Chat ledgers (P6-b28, 2026-10-24) — the user's own daily logs
(water, expenses, grocery list, meals, habits, medications), typed in
natural language and kept in ONE additive sqlite table.

Money-and-units honesty (pinned):
- numbers are stored verbatim; totals are shown AS sums of the units
  being counted — glasses and ml are NEVER merged, and expenses are a
  bare sum + count with NO currency invented (the user's unit is the
  truth, not the assistant's);
- a ledger write is the user's own diary — deterministic parse,
  validated values, audited — no policy/execution authority is added
  (same posture as ReminderService: data + notifications, never
  devices or tools);
- reports are the same computation used by the weekly review's
  chat-logs line (`week_line`), so composite answers can never drift;
- "remind"-shaped asks route to the ReminderService (exclusion guard):
  a routine reminder about water is a reminder, not a water entry.

Batch-32 addition (2026-10-26): Firefly-III-style monthly budget
(github.com/firefly-iii/firefly-iii, MIT — design only, never their
code): ONE honest monthly cap compared against the SAME expense rows
this service logs, with the four budget insights Firefly shows —
spent per period, left to spend, days left, left per day. No second
implementation of the expense totals anywhere: the budget reads
`entries(kind="expense")`. Categories were deliberately NOT adopted:
guessing a category from the free-text note would violate the
no-currency/no-guessing honesty rule, so the budget is one monthly
number (unset until the user sets it; "no budget" stays silent in
lines that only make sense with one).
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time as _time
import uuid
from datetime import datetime, timedelta
from typing import Callable, Optional

_KINDS = ("water", "expense", "grocery", "meal", "habit", "medication")
_TEXT_MAX = 200


def _local_date(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().strftime(
        "%Y-%m-%d")


class LedgerService:
    """One additive table, per-entry validation, day/week windows from
    the HOST timezone local dates."""

    def __init__(self, audit=None, db_path: str = "",
                 now: Optional[Callable[[], float]] = None,
                 env: Optional[dict] = None) -> None:
        self.audit = audit
        self._now = now or (lambda: _time.time())
        self._lock = threading.RLock()
        self._db = None
        self._mem: list[dict] = []     # when no db path: memory store
        if db_path:
            self._db = sqlite3.connect(db_path,
                                       check_same_thread=False)
            self._setup_db()

    def _setup_db(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS ledger_entries ("
                "id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "status TEXT DEFAULT 'logged', "
                "amount REAL, unit TEXT, note TEXT, "
                "at REAL NOT NULL, local_date TEXT NOT NULL, "
                "who TEXT)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS ledger_budgets ("
                "month_key TEXT PRIMARY KEY, amount REAL NOT NULL, "
                "set_at REAL NOT NULL)")
            self._db.commit()

    def _audit(self, action: str, detail: str = "") -> None:
        try:
            if self.audit is not None:
                self.audit.record("ledgers", action, "", detail[:300])
        except Exception:  # noqa: BLE001 — audit never breaks logs
            pass

    # -- writes ---------------------------------------------------------

    def _insert(self, kind: str, status: str, amount: Optional[float],
                unit: str, note: str) -> dict:
        if kind not in _KINDS:
            raise LedgerValueError(f"Unknown ledger kind: {kind!r}")
        now = self._now()
        row = {"id": f"lg-{uuid.uuid4().hex[:12]}", "kind": kind,
               "status": status, "amount": amount, "unit": unit or "",
               "note": note, "at": now,
               "local_date": _local_date(now), "who": "user"}
        if self._db is not None:
            try:
                with self._lock:
                    self._db.execute(
                        "INSERT INTO ledger_entries (id, kind, status,"
                        " amount, unit, note, at, local_date, who)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (row["id"], row["kind"], row["status"],
                         row["amount"], row["unit"], row["note"],
                         row["at"], row["local_date"], row["who"]))
                    self._db.commit()
            except Exception as e:  # noqa: BLE001 — honest failure
                self._audit("ledger_write_failed", str(e)[:200])
                raise LedgerValueError(
                    f"recording failed: {e}") from e
        else:
            with self._lock:
                self._mem.append(row)
        self._entry_hooks(row)
        self._audit(f"ledger_{kind}_logged",
                    (note or (unit or "") or "")[:120])
        return row

    def _entry_hooks(self, row: dict) -> None:
        pass        # reserved: report hooks stay uncoupled

    def log_water(self, amount: float, unit: str) -> dict:
        if amount <= 0 or amount > 100:
            raise LedgerValueError(
                "Water amount must be between 1 and 100 "
                "(in whole glasses or ml — say which).")
        return self._insert("water", "logged", float(amount),
                            unit.strip(), "")

    def log_expense(self, amount: float, note: str) -> dict:
        if amount <= 0 or amount > 1_000_000:
            raise LedgerValueError(
                "Expense amount must be a positive number under a "
                "million.")
        note = " ".join(note.split())[:_TEXT_MAX]
        return self._insert("expense", "logged", round(amount, 2), "",
                            note)

    def grocery_add(self, item: str) -> dict:
        item = " ".join(item.split())[:_TEXT_MAX]
        if not item:
            raise LedgerValueError("What item should I add?")
        with self._lock:
            opens = self._open_grocery()
        if any(o["note"].lower() == item.lower() for o in opens):
            raise LedgerValueError(
                f"'{item}' is already on the grocery list.")
        return self._insert("grocery", "open", None, "", item)

    def grocery_done(self, item: str) -> dict:
        hit = self._grocery_match(item.strip())
        if hit is None:
            raise LedgerValueError(
                f"No open '{item.strip()}' on the grocery list. Say "
                f"'show my grocery list'.")
        return self._set_status(hit, "done")

    def grocery_remove(self, item: str) -> dict:
        hit = self._grocery_match(item.strip())
        if hit is None:
            raise LedgerValueError(
                f"No open '{item.strip()}' on the grocery list. Say "
                f"'show my grocery list'.")
        return self._set_status(hit, "removed")

    def _grocery_match(self, item: str) -> Optional[dict]:
        if not item:
            raise LedgerValueError("Which item?")
        opens = self._open_grocery()
        hits = [e for e in opens if e["note"].lower() == item.lower()]
        if not hits:
            hits = [e for e in opens
                    if item.lower() in e["note"].lower()]
        if len(hits) != 1:
            return None
        return hits[0]

    def _open_grocery(self) -> list[dict]:
        rows = self._rows(kind="grocery", status="open")
        return [self._rowdict(r) for r in rows]

    def _set_status(self, row: dict, status: str) -> dict:
        if self._db is not None:
            with self._lock:
                self._db.execute(
                    "UPDATE ledger_entries SET status=? WHERE id=?",
                    (status, row["id"]))
                self._db.commit()
        else:
            with self._lock:
                for r in self._mem:
                    if r["id"] == row["id"]:
                        r["status"] = status
        self._audit("ledger_grocery_status",
                    f"{row['id']} -> {status}")
        out = dict(row)
        out["status"] = status
        return out

    def log_note(self, kind: str, note: str, status="logged") -> dict:
        """meal / habit / medication / any text-kind entry."""
        note = " ".join((note or "").split())[:_TEXT_MAX]
        if not note:
            raise LedgerValueError(f"What should the {kind} entry say?")
        return self._insert(kind, status, None, "", note)

    # -- reads -----------------------------------------------------------

    def _rows(self, kind=None, status=None) -> list[tuple]:
        if self._db is None:
            rows = [
                (r["id"], r["kind"], r["status"], r["amount"],
                 r["unit"], r["note"], r["at"], r["local_date"],
                 r["who"])
                for r in sorted(self._mem, key=lambda x: x["at"],
                                reverse=True)
                if (kind is None or r["kind"] == kind) and
                (status is None or r["status"] == status)]
            return rows[:500]
        q = ("SELECT id, kind, status, amount, unit, note, at, "
             "local_date, who FROM ledger_entries WHERE 1=1")
        args: list = []
        if kind:
            q += " AND kind=?"
            args.append(kind)
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY at DESC LIMIT 500"
        with self._lock:
            return self._db.execute(q, args).fetchall()

    @staticmethod
    def _rowdict(r: tuple) -> dict:
        return {"id": r[0], "kind": r[1], "status": r[2],
                "amount": r[3], "unit": r[4] or "", "note": r[5] or "",
                "at": r[6], "local_date": r[7]}

    def entries(self, kind: str = "", status: str = "",
                days: int = 0) -> list[dict]:
        rows = self._rows(kind or None, status or None)
        out = [self._rowdict(r) for r in rows]
        if days:
            and_anchor = self._day_anchor(int(days))
            out = [e for e in out if e["local_date"]
                   >= and_anchor.strftime("%Y-%m-%d")]
        return out

    def _day_anchor(self, days: int):
        return datetime.now().astimezone().date() - \
            timedelta(days=max(0, days - 1))

    def water_line(self, days: int = 1) -> str:
        es = self.entries(kind="water", days=days)
        if not es:
            return f"no water logged in the last {'day' if days == 1 else f'{days} days'}."
        per_unit: dict[str, float] = {}
        n_glasses = 0
        for e in es:
            u = e["unit"] or "?"
            per_unit[u] = per_unit.get(u, 0.0) + float(
                e["amount"] or 0)
            if u.lower() in ("glass", "glasses"):
                n_glasses += int(e["amount"] or 0)
        body = "; ".join(f"{v:g} {u}" for (u, v) in sorted(
            per_unit.items()))
        return (f"{len(es)} entries — {body}; "
                f"{n_glasses} whole glasses so far.")

    def expense_line(self, days: int = 1) -> str:
        es = self.entries(kind="expense", days=days)
        if not es:
            return f"no expenses logged in the last {'day' if days == 1 else f'{days} days'}."
        total = sum(float(e["amount"] or 0) for e in es)
        return (f"{len(es)} entries totalling {total:.2f} (sum of the "
                "amounts you gave; no currency assumed).")

    def week_line(self) -> str:
        """The weekly review's chat-logs line — the ONE totals view
        both surfaces share (never a second tail computation)."""
        parts = []
        w = self.water_line(7)
        e = self.expense_line(7)
        parts.append("water: " + w)
        parts.append("expenses: " + e)
        other = self.entries(days=7)
        kinds = [k for k in _KINDS
                 if k not in ("water", "expense")]
        counts = {k: 0 for k in kinds}
        for r in other:
            if r["kind"] in counts:
                counts[r["kind"]] += 1
        extra = [f"{k} {v}" for k, v in counts.items() if v]
        parts.append("other entries this week: " +
                     (", ".join(extra) if extra else "none"))
        return "; ".join(parts)

    def journal_summary(self, kind: str, days: int = 1) -> list[dict]:
        return self.entries(kind=kind, days=days)

    # -- budget (batch-32: Firefly-III-style monthly cap; design only,
    #    one number, the SAME expense sums, no currency invented) ---

    def set_monthly_budget(self, amount: float) -> dict:
        amount = round(float(amount), 2)
        if amount <= 0 or amount > 1_000_000:
            raise LedgerValueError(
                "Budget amount must be a positive number under a "
                "million.")
        now = self._now()
        if self._db is not None:
            with self._lock:
                self._db.execute(
                    "INSERT INTO ledger_budgets (month_key, amount,"
                    " set_at) VALUES ('default', ?, ?)"
                    " ON CONFLICT(month_key)"
                    " DO UPDATE SET amount=excluded.amount,"
                    " set_at=excluded.set_at",
                    (amount, now))
                self._db.commit()
        else:
            self._budget_default = amount
            if not hasattr(self, "_budget_mem"):
                self._budget_mem: dict = {}
            self._budget_mem["default"] = {"amount": amount,
                                           "set_at": now}
        self._audit("ledger_budget_set", f"monthly={amount:.2f}")
        return {"amount": amount, "set_at": now}

    def clear_monthly_budget(self) -> dict:
        got = self.monthly_budget()
        if self._db is not None:
            with self._lock:
                self._db.execute(
                    "DELETE FROM ledger_budgets")
                self._db.commit()
        else:
            if hasattr(self, "_budget_mem"):
                self._budget_mem.clear()
        self._audit("ledger_budget_cleared",
                    f"was={got:.2f}" if got else "none")
        return {"was": got}

    def _budget_mem_default(self) -> Optional[float]:
        if not hasattr(self, "_budget_mem"):
            return None
        row = self._budget_mem.get("default")
        return float(row["amount"]) if row else None

    def monthly_budget(self) -> Optional[float]:
        if self._db is not None:
            with self._lock:
                got = self._db.execute(
                    "SELECT amount FROM ledger_budgets WHERE "
                    "month_key='default'").fetchone()
            return float(got[0]) if got else None
        return self._budget_mem_default()

    def budget_status(self) -> Optional[dict]:
        b = self.monthly_budget()
        if b is None:
            return None
        now = datetime.fromtimestamp(self._now()).astimezone()
        month_key = now.strftime("%Y-%m")
        spent = 0.0
        for e in self.entries(kind="expense"):
            if (e["local_date"] or "").startswith(month_key):
                spent += float(e["amount"] or 0)
        left = round(b - spent, 2)
        days_in_month = 31
        nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
        days_in_month = (nxt - timedelta(days=1)).day
        days_left = days_in_month - now.day + 1
        per_day = round(left / days_left, 2) if days_left > 0 else 0.0
        return {"budget": b, "spent": round(spent, 2),
                "left": left, "days_left": days_left,
                "days_in_month": days_in_month, "per_day": per_day,
                "over": left < 0}

    def budget_line(self) -> str:
        """The weekly-review's optional budget tail — "" when no
        budget is set (the review text stays honest and unchanged)."""
        s = self.budget_status()
        if s is None:
            return ""
        head = (f"over budget by {abs(s['left']):.2f}"
                if s["over"] else f"{s['left']:.2f} left")
        return (f"; monthly budget: spent {s['spent']:.2f} of "
                f"{s['budget']:.2f} — {head}, {s['days_left']} day(s)"
                f" left, {s['per_day']:.2f}/day (no currency "
                f"assumed)")


class LedgerValueError(ValueError):
    """Hard refusal (invalid amount / confusing ask)."""


# ------------------------------------------------------------------
# NL surface — deterministic parse (no model), honest shapes
# ------------------------------------------------------------------

_WATER_RE = re.compile(
    r"^(?:i\s+)?(?:add\s+|log\s+|drank\s+|had\s+|drink\s+)?"
    r"(?:(?:water\s+(?P<n1>\d+(?:\.\d+)?)\s*(?P<u1>glasses?|cups?|"
    r"bottles?|ml)?)"
    r"|(?P<n2>\d+(?:\.\d+)?)\s*(?P<u2>glasses?|cups?|bottles?|ml)\s*"
    r"(?:of\s+)?water)\s*\.*$", re.I)
_EXPENSE_RE = re.compile(
    r"^(?:add\s+)?(?:expense|spent)\s+(?P<a>\d+(?:\.\d{1,2})?)\s*"
    r"(?:for|on)?\s*(?P<n>[^;:].*)?\s*$", re.I)
_GROC_ADD_RE = re.compile(
    r"^(?:add\s+)?(?P<i>.+?)\s+to\s+(?:my\s+)?grocery\s+list\s*$", re.I)
_GROC_LIST_RE = re.compile(
    r"^(?:show|list|what(?:'s| is) (?:on|in))(?: the| my)?\s*grocery"
    r"\s+list\s*$", re.I)
_GROC_GET_RE = re.compile(
    r"^(?:buy|get)\s+(?P<i>.+?)\s+(?:from|at)\s+(?:the\s+)?grocer"
    r"(?:y|ies)?\b.*$", re.I)
_GROC_RM_RE = re.compile(
    r"^(?:remove|delete)\s+(?P<i>.+?)\s+from\s+(?:my\s+)?grocery"
    r"\s+list\s*$", re.I)
_GROC_DONE_RE = re.compile(
    r"^(?:check\s+off|mark)\s+(?P<i>.+?)(?:\s+as\s+(?:bought|done))?\s*$",
    re.I)
_HABIT_RE = re.compile(
    r"^(?:log\s+)?(?:i\s+)?(?:did|completed|finished)\s+(?:the\s+)?"
    r"(?P<h>habit\s+.*|.*\s+habit)$", re.I)
_HABIT2_RE = re.compile(r"^log\s+habit\s+(?P<h>.+)$", re.I)
_MED_RE = re.compile(
    r"^(?:log\s+)?(?:took|taken)\s+(?:my\s+|the\s+)?medication:?\s+"
    r"(?P<m>.+)$|^(?:log\s+medication\s+(?P<m2>.+))$", re.I)
_MEAL_RE = re.compile(
    r"^(?:log\s+)?meal:?\s+(?P<m>.+)|^(?:log\s+meal\s+(?P<m2>.+))$|"
    r"^(?:i\s+ate\s+(?P<m3>.+))$", re.I)
_REP_WATER_RE = re.compile(
    r"^(?:how\s+much\s+water|water)\s+(?:today|this week)\s*$", re.I)
_REP_EXP_RE = re.compile(
    r"^(?:how\s+much\s+(?:did\s+i\s+spend)|expenses?|spending)\s+"
    r"(?:today|this week)\s*$", re.I)
_REP_EAT_RE = re.compile(
    r"^what\s+did\s+i\s+eat(?:\s+today)?\s*$", re.I)
_REP_MED_RE = re.compile(r"^medications?(?:\s+today)?\s*$", re.I)
_REP_HABIT_RE = re.compile(r"^habits?(?:\s+today)?\s*$", re.I)
_BUDGET_SET_RE = re.compile(
    r"^(?:set\s+(?:my\s+)?monthly\s+budget\s+(?:to\s+)?"
    r"|budget\s+)(?P<a>\d+(?:\.\d{1,2})?)\s*(?:a month|per month|"
    r"monthly)?\s*\.*$", re.I)
_BUDGET_CLEAR_RE = re.compile(
    r"^(?:remove|clear|delete)\s+(?:my\s+)?(?:monthly\s+)?budget"
    r"\s*\.*$", re.I)
_BUDGET_STATUS_RE = re.compile(
    r"^(?:how(?:'s| is)\s+my\s+budget|how much of my budget|"
    r"budget\s+status|show\s+(?:my\s+)?budget|what(?:'s| is)\s+my"
    r"\s+budget|(?:my\s+)?budget)\s*\.*$", re.I)
_EXCLUDE_RE = re.compile(
    r"\b(remind|reminder|routine|calendar|appointment|meeting)\b|"
    r"\bshow my reminders\b", re.I)


def parse_ledger_intent(utterance: str) -> dict:
    """Deterministic op parse. {"op": ...|unknown}. Nothing here
    executes."""
    raw = " ".join((utterance or "").strip().split())
    low = raw.lower()
    if _EXCLUDE_RE.search(low):
        return {"op": "unknown"}
    if re.fullmatch(r"show (?:my )?(?:logs|journal|ledger)", low):
        return {"op": "journal"}
    m = _BUDGET_CLEAR_RE.match(raw)
    if m:
        return {"op": "budget_clear"}
    m = _BUDGET_SET_RE.match(raw)
    if m:
        return {"op": "budget_set", "amount": float(m.group("a"))}
    if _BUDGET_STATUS_RE.match(raw):
        return {"op": "budget_status"}
    m = _WATER_RE.match(low) or _WATER_RE.match(raw)
    if m:
        if m.group("n1") is not None:
            amt = float(m.group("n1"))
            unit = (m.group("u1") or "").strip()
            if not unit:
                return {"op": "clarify",
                        "message": "Water units matter — glasses, "
                                   "cups, bottles, or ml. Say e.g. "
                                   "'add water 2 glasses'."}
        else:
            amt = float(m.group("n2"))
            unit = (m.group("u2") or "").strip()
        unit = " ".join(unit.split()).lower()
        unit = {"glasses": "glass", "cups": "cup", "bottles": "bottle",
                "glass": "glass", "cup": "cup", "bottle": "bottle",
                "ml": "ml"}.get(unit, unit)
        return {"op": "water", "amount": amt, "unit": unit}
    m = _EXPENSE_RE.match(raw)
    if m:
        return {"op": "expense", "amount": float(m.group("a")),
                "note": (m.group("n") or "").strip()}
    if re.search(r"\bgrocer", low):
        m = _GROC_ADD_RE.match(raw)
        if m:
            return {"op": "grocery_add", "item": m.group("i")}
        m = _GROC_RM_RE.match(raw)
        if m:
            return {"op": "grocery_remove", "item": m.group("i")}
        if _GROC_LIST_RE.match(raw):
            return {"op": "grocery_list"}
        if _GROC_GET_RE.match(raw):
            return {"op": "clarify",
                    "message": "To file a shopping item say 'add "
                               "<item> to my grocery list', and to "
                               "check one off say 'check off "
                               "<item>'."}
    m = _GROC_DONE_RE.match(raw)
    if m and re.search(r"^(?:check\s+off|mark)\b", raw, re.I):
        return {"op": "grocery_done", "item": m.group("i")}
    m = _HABIT2_RE.match(raw)
    if m:
        return {"op": "habit", "name": m.group("h")}
    m = _HABIT_RE.match(raw)
    if m:
        name = m.group("h")
        name = re.sub(r"\s*habit$", "", name.strip(), flags=re.I)
        name = re.sub(r"^habit\s+", "", name.strip(), flags=re.I)
        return {"op": "habit", "name": name.strip()}
    m = _MED_RE.match(raw)
    if m:
        return {"op": "medication",
                "name": (m.group("m") or m.group("m2") or "").strip()}
    m = _MEAL_RE.match(raw)
    if m:
        note = m.group("m") or m.group("m2") or m.group("m3") or ""
        return {"op": "meal", "note": note.strip()}
    if _REP_WATER_RE.match(raw):
        return {"op": "report_water",
                "days": 7 if "week" in low else 1}
    if _REP_EXP_RE.match(raw):
        return {"op": "report_expense",
                "days": 7 if "week" in low else 1}
    if _REP_EAT_RE.match(raw):
        return {"op": "report_meal"}
    if _REP_MED_RE.match(raw):
        return {"op": "report_medication"}
    if _REP_HABIT_RE.match(raw):
        return {"op": "report_habit"}
    return {"op": "unknown"}


def _journal_message(service: LedgerService) -> str:
    counts = {k: 0 for k in _KINDS}
    for e in service.entries():
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    today_counts: dict[str, int] = {}
    today = _local_date(service._now())
    for e in service.entries():
        if e["local_date"] == today:
            today_counts[e["kind"]] = \
                today_counts.get(e["kind"], 0) + 1
    total = sum(counts.values())
    if total == 0:
        return ("The ledger is empty. Try 'add expense 40 for "
                "coffee', 'add water 2 glasses', or 'add milk to my "
                "grocery list'.")
    per = ", ".join(f"{k}: {v}" for k, v in counts.items() if v)
    return (f"Ledger so far — {per}; today: " +
            (", ".join(f"{k}: {v}" for k, v in
                       today_counts.items()) or "nothing yet") + ".")


def handle_ledger_utterance(service: LedgerService, utterance: str,
                            who: str = "user") -> Optional[dict]:
    """Parse + dispatch one ledger utterance. `executed` is always
    False: ledger writes touch the user's OWN data store and nothing
    else. Unknown shapes return None so other flows still see the
    utterance."""
    op = parse_ledger_intent(utterance)
    kind = op.get("op")
    if kind == "unknown":
        return None
    if kind == "clarify":
        return {"message": op["message"], "executed": False,
                "clarified": True}
    if kind == "journal":
        return {"message": _journal_message(service),
                "executed": False, "ledger": "journal"}
    try:
        if kind == "budget_set":
            try:
                row = service.set_monthly_budget(op["amount"])
            except LedgerValueError as e:
                return {"message": f"I won't set that budget: {e}",
                        "executed": False, "refused": True}
            return {"message": f"Monthly budget set to "
                               f"{row['amount']:.2f} (no currency "
                               f"assumed). Status: 'budget status'.",
                    "executed": False, "budget": row["amount"]}
        if kind == "budget_clear":
            res = service.clear_monthly_budget()
            if res["was"] is None:
                return {"message": "No budget was set — nothing to "
                                   "clear.",
                        "executed": False, "cleared": False}
            return {"message": f"Budget cleared (was "
                               f"{res['was']:.2f}/month) — expenses "
                               f"stay logged of course.",
                    "executed": False, "cleared": True}
        if kind == "budget_status":
            s = service.budget_status()
            if s is None:
                return {"message": "No budget set yet — say 'set my "
                                   "monthly budget to 500' (no "
                                   "currency invented by me).",
                        "executed": False, "budget": None}
            head = (f"over budget by {abs(s['left']):.2f}"
                    if s["over"] else f"{s['left']:.2f} left")
            return {"message": f"Budget this month — spent "
                               f"{s['spent']:.2f} of "
                               f"{s['budget']:.2f}: {head}, "
                               f"{s['days_left']} day(s) left in "
                               f"{s['days_in_month']}, "
                               f"{s['per_day']:.2f}/day (no currency "
                               f"assumed).",
                    "executed": False, "budget": s}
        if kind == "water":
            row = service.log_water(op["amount"], op["unit"])
            return {"message": f"Logged {row['amount']:g} "
                               f"{row['unit'] or 'units'} of water — "
                               f"{service.water_line(1).rstrip('.')}.",
                    "executed": False, "ledger": "water"}
        if kind == "expense":
            row = service.log_expense(op["amount"], op["note"])
            note = f" for {row['note']}" if row["note"] else ""
            return {"message": f"Logged expense {row['amount']:.2f}"
                               f"{note} — "
                               f"{service.expense_line(1).rstrip('.')}"
                               f".",
                    "executed": False, "ledger": "expense"}
        if kind == "grocery_add":
            row = service.grocery_add(op["item"])
            return {"message": f"Added '{row['note']}' to the grocery "
                               f"list ({len(service.entries(kind='grocery', status='open'))} open "
                               f"items).",
                    "executed": False, "ledger": "grocery"}
        if kind == "grocery_remove":
            row = service.grocery_remove(op["item"])
            return {"message": f"Removed '{row['note']}' from the "
                               f"grocery list.",
                    "executed": False, "ledger": "grocery"}
        if kind == "grocery_done":
            row = service.grocery_done(op["item"])
            return {"message": f"Checked off '{row['note']}'.",
                    "executed": False, "ledger": "grocery"}
        if kind == "grocery_list":
            opens = service.entries(kind="grocery", status="open")
            done = service.entries(kind="grocery", status="done")
            if not opens and not done:
                return {"message": "The grocery list is empty. Say "
                                   "'add milk to my grocery list'.",
                        "executed": False, "ledger": "grocery"}
            msg = ("; ".join(f"□ {e['note']}" for e in opens[:12]) or
                   "nothing open")
            if done:
                msg += (" | done: " +
                        ", ".join(e["note"] for e in done[:6]))
            return {"message": f"Grocery list — {msg}.",
                    "executed": False, "ledger": "grocery"}
        if kind == "habit":
            row = service.log_note("habit", op["name"])
            return {"message": f"Logged habit '{row['note']}' "
                               f"(today: {len(service.entries(kind='habit', days=1))} "
                               f"logged).",
                    "executed": False, "ledger": "habit"}
        if kind == "medication":
            row = service.log_note("medication", op["name"])
            return {"message": f"Logged medication '{row['note']}' "
                               f"(today: {len(service.entries(kind='medication', days=1))} "
                               f"logged).",
                    "executed": False, "ledger": "medication"}
        if kind == "meal":
            row = service.log_note("meal", op["note"])
            return {"message": f"Logged meal '{row['note']}'.",
                    "executed": False, "ledger": "meal"}
        if kind == "report_water":
            return {"message": f"Water — {service.water_line(op['days'])}"
                               f".",
                    "executed": False, "ledger": "water"}
        if kind == "report_expense":
            return {"message": f"Expenses — "
                               f"{service.expense_line(op['days'])}.",
                    "executed": False, "ledger": "expense"}
        if kind == "report_meal":
            es = service.entries(kind="meal", days=1)
            body = "; ".join(e["note"] for e in es[:8]) \
                if es else "nothing logged today"
            return {"message": f"Meals today — {body}.",
                    "executed": False, "ledger": "meal"}
        if kind == "report_medication":
            es = service.entries(kind="medication", days=1)
            body = "; ".join(e["note"] for e in es[:8]) \
                if es else "none logged today"
            return {"message": f"Medications today — {body}.",
                    "executed": False, "ledger": "medication"}
        if kind == "report_habit":
            es = service.entries(kind="habit", days=1)
            body = "; ".join(e["note"] for e in es[:8]) \
                if es else "none logged today"
            return {"message": f"Habits today — {body}.",
                    "executed": False, "ledger": "habit"}
    except LedgerValueError as e:
        return {"message": f"I won't log that: {e}",
                "executed": False, "refused": True}
    return {"message": "I couldn't process that ledger ask.",
            "executed": False}


class LedgerFlow:
    """Turn-shaped surface for the IntentTurnService flow loop."""

    def __init__(self, service: LedgerService) -> None:
        self.service = service

    def wants_turn(self, clean: str) -> bool:
        c = (clean or "").strip()
        if _EXCLUDE_RE.search(c):
            return False
        return bool(re.search(
            r"\bwater\b|\bexpenses?\b|\bspent\b|\bgrocer|\bmeal\b|"
            r"\bi ate\b|\bwhat did i eat\b|\bhabit\b|\bmedication\b|"
            r"\bmedications?\b|\bledger\b|\bjournal\b|\bbudget\b",
            c, re.I))

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        return handle_ledger_utterance(self.service, clean, who)
