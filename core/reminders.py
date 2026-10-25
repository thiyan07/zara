"""Reminders (Stage 24i) — natural-language reminder management.

Built ON the existing scheduler + notifications + audit (never a
second scheduler or engine): every reminder keeps exactly ONE live
scheduler job (recurring reminders re-arm the next occurrence at fire
time — no unbounded job creation), delivery goes through the existing
NotificationManager, and every lifecycle change is audited. Zero
execution authority is added: reminder text is opaque data, stored
verbatim and NEVER interpreted as an instruction — it never reaches
the resolver, policy, or executor.

Deterministic time interpretation (no model in the loop; a hosted
parse can never invent a schedule — the service re-validates every
epoch and recurrence itself):
- Timezone: ZARA_TIMEZONE env (IANA zone), loudly validated. When
  unset, the machine's local zone is used (IANA key when available,
  else a fixed UTC offset). Documented limitation of the offset
  fallback: it cannot track DST. Each reminder STORES its zone name,
  so restarts reconstruct the intended wall time instead of silently
  flipping to UTC.
- Period words are DEFINED (not guessed): morning 08:00, noon 12:00,
  afternoon 14:00, evening 19:00, tonight 20:00, night 21:00,
  midnight 00:00. A period word also disambiguates a bare hour
  ("evening at 7" → 19:00).
- Bare clock hours 1–11 with NO period word or AM/PM are AMBIGUOUS →
  clarified, never guessed. 12 means 12 PM by documented convention;
  explicit am/pm always wins; 13–23 (24 h) and H:MM are literal.
- A bare wall time that already passed today rolls to TOMORROW
  (documented). An explicit day or period phrase whose resolved time
  is past ("today at 7 PM" at 20:30, "tonight at 9" at 22:00) is
  CLARIFIED instead of silently reinterpreted.
- Relative "in N minutes|hours|days" must be positive, lands in the
  future, and may not be combined with day words (both together →
  clarify).
- Recurrence: "every day", "every <period>", "every week", "every
  <weekday>" — anchored to the reminder's local wall time in its
  stored zone (8 PM stays 8 PM across DST transitions; the epoch
  delta is 23/24/25 h). Wall times inside a spring-forward gap map
  forward per zoneinfo fold=0 semantics (documented).

Delivery honesty: "delivered_to_service" means created in the
notification service (a queued user-facing message). The service
NEVER claims the user saw a reminder. States: scheduled → due →
delivery_attempted → delivered_to_service / delivery_failed (final
after bounded MAX_DELIVERY_ATTEMPTS retries) / cancelled. Retries are
bounded and deduped by (reminder_id, occurrence) — a retry never
duplicates a delivered notification.

Missed-run policy (explicit): after downtime, a one-shot due in the
gap is delivered ONCE (audited as restart_receipt); a recurring
reminder NEVER bursts — one notification for the most recent missed
occurrence (audited as missed_run) when the gap is non-trivial, then
the schedule re-anchors to the next future occurrence.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time as _time
import uuid
from datetime import datetime, timedelta, timezone as _rtc
from typing import Callable, Optional
from zoneinfo import ZoneInfo

PERIOD_DEFAULTS = {
    "morning": 8, "noon": 12, "afternoon": 14, "evening": 19,
    "tonight": 20, "night": 21, "midnight": 0,
}
# period word → implied meridiem for bare-hour disambiguation
PM_PERIODS = {"afternoon", "evening", "tonight", "night"}
PERIOD_MERIDIEM = {p: ("pm" if p in PM_PERIODS else "am")
                   for p in PERIOD_DEFAULTS}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_TEXT_MAX = 300
_MIN_LEAD_S = 5.0            # a reminder must sit in the future
_RETRY_DELAY_S = 20.0
# day-part period words: an explicit day-part phrase whose resolved
# time already passed today is CLARIFIED, never silently reinterpreted
# (noon/midnight are clock quantities and follow the rolling rule)
_DAYPARTS = frozenset({"morning", "afternoon", "evening", "tonight",
                       "night"})
MAX_DELIVERY_ATTEMPTS = 3
_DAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
               "Saturday", "Sunday"]


class ReminderTimeError(ValueError):
    """Clarifying time-parse failure (message is user-facing)."""


class ReminderValueError(ValueError):
    """Hard refusal (invalid schedule / impossible time)."""


def configured_tz(env: dict | None = None) -> str:
    """Resolve the configured zone name.

    ZARA_TIMEZONE (explicit, validated) wins; else the machine's local
    zone (IANA key when available, else fixed UTC offset)."""
    _env = dict(os.environ if env is None else env)
    name = (_env.get("ZARA_TIMEZONE") or "").strip()
    if name:
        try:
            ZoneInfo(name)
        except Exception as e:  # noqa: BLE001 — bad config fails loudly
            raise ValueError(f"ZARA_TIMEZONE is not a valid IANA zone: "
                             f"{name} ({e})") from e
        return name
    local = datetime.now().astimezone().tzinfo
    key = getattr(local, "key", None)
    if key:
        return key
    off = local.utcoffset(None) or timedelta(0)
    total = int(abs(off.total_seconds()))
    sign = "+" if off.total_seconds() >= 0 else "-"
    return f"UTC{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _wall(tz_name: str, epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, ZoneInfo(tz_name))


# ------------------------------------------------------------------
# deterministic time parsing
# ------------------------------------------------------------------

_CLOCK = re.compile(
    r"\bat\s+(?P<h>\d{1,2})(?P<min>:\d{2})?\s*(?P<mer>a\.?m\.?|p\.?m\.?)?"
    r"|(?P<h2>\d{1,2})(?P<min2>:\d{2})\s*(?P<mer2>a\.?m\.?|p\.?m\.?)\b"
    r"|(?P<h3>\d{1,2})\s*(?P<mer3>a\.?m\.?|p\.?m\.?)\b"
    r"|(?P<word>noon|midnight)", re.I)
_PERIOD = re.compile(
    r"\b(morning|afternoon|evening|tonight|noon|midnight|night)\b", re.I)
_IN = re.compile(
    r"\bin\s+(\d{1,5})\s*(minutes?|mins?|hours?|hrs?|days?)\b", re.I)
_DAYS = re.compile(
    r"\b(tomorrow|today|next\s+week|every\s+day|"
    r"every\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|"
    r"sunday)|every\s+(?:morning|afternoon|evening|night)|every\s+week"
    r"|(?:mon|tues|wednes|thurs|fri|satur|sun)day)\b", re.I)
_MER = re.compile(r"\b(?:am|pm)\b", re.I)


def _normalize_mer(mstr: Optional[str]) -> Optional[str]:
    if not mstr:
        return None
    s = mstr.lower().replace(".", "")
    return "am" if s == "am" else ("pm" if s == "pm" else None)


def _clock_in_text(text: str, mer_hint: Optional[str] = None
                   ) -> Optional[tuple[int, int, bool]]:
    """(hour24, minute, had_explicit_meridiem) or None.

    Bare hours 1–11 are resolved with mer_hint (from a period word)
    when present; otherwise they are AMBIGUOUS → ReminderTimeError.
    12 means 12 PM by documented convention; 13–23 are literal."""
    m = _CLOCK.search(text)
    if not m:
        return None
    if m.group("word"):
        w = m.group("word").lower()
        return PERIOD_DEFAULTS[w], 0, False
    mer = (_normalize_mer(m.group("mer")) or
           _normalize_mer(m.group("mer2")) or
           _normalize_mer(m.group("mer3")))
    hraw = m.group("h") or m.group("h2") or m.group("h3")
    h = int(hraw)
    mraw = m.group("min") or m.group("min2")
    minute = int(mraw.lstrip(":")) if mraw else 0
    if mer is None and 1 <= h <= 11:
        # bare hour: use the period word's implied meridiem when the
        # phrase has one; otherwise the time is AMBIGUOUS → clarify,
        # never guess
        if mer_hint:
            mer = mer_hint
        else:
            raise ReminderTimeError(
                f"Did you mean {h} AM or {h} PM? Please say which "
                f"(or a time of day like 'evening').")
    if mer is None and h == 12:
        mer = "pm"          # documented convention
    if mer == "pm" and 1 <= h <= 11:
        h += 12
    elif mer == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= minute <= 59):
        raise ReminderTimeError(f"'{hraw}' is not a valid clock time.")
    return h, minute, bool(mer)


def _resolve_clock_period(clock, period):
    if clock:
        return clock[0], clock[1]
    if period:
        return PERIOD_DEFAULTS[period], 0
    raise ReminderTimeError("No time could be resolved in that phrase.")


def _wall_hour(hour: int, minute: int, base: datetime, tz,
               now_epoch: float, weekday: Optional[int] = None,
               max_steps: int = 500) -> float:
    """Next epoch strictly > now + lead with the given local wall time
    (optionally on `weekday`), walking local DAYS forward. Handles
    spring-forward-gap candidates (they land at/below now or inside
    the gap → the walk continues). fold=0 documented."""
    date = base.date()
    for _ in range(max_steps):
        if weekday is None or date.weekday() == weekday:
            epoch = datetime(date.year, date.month, date.day, hour,
                             minute, fold=0, tzinfo=tz).timestamp()
            if epoch > now_epoch + _MIN_LEAD_S:
                return epoch
        date += timedelta(days=1)
    raise ReminderValueError("Could not resolve a future time.")


def parse_when(text: str, now_epoch: float, tz_name: str) -> dict:
    """Parse a when-clause into {"epoch", "recurrence", "kind"}.

    Raises ReminderTimeError (clarify) or ReminderValueError (refuse).
    Recurrence: None | {"kind": "daily"} |
    {"kind": "weekly", "weekday": 0-6}."""
    low = " ".join((text or "").split()).lower()
    period = None
    pm = _PERIOD.search(low)
    if pm:
        period = pm.group(1).lower()
    relative = _IN.search(low)
    day_m = _DAYS.search(low)
    try:
        clock = _clock_in_text(low, mer_hint=PERIOD_MERIDIEM.get(period))
    except ReminderTimeError:
        raise
    if relative:
        if day_m or clock or period:
            raise ReminderTimeError(
                "Use either 'in 2 hours' or a concrete time, not both.")
        n = int(relative.group(1))
        unit = relative.group(2).lower()
        mult = 60 if unit.startswith(("minute", "min")) else \
            (3600 if unit.startswith(("hour", "hr")) else 86400)
        if n <= 0:
            raise ReminderValueError("A reminder interval must be "
                                     "positive.")
        epoch = now_epoch + n * mult
        if epoch < now_epoch + _MIN_LEAD_S:
            raise ReminderValueError("That time is already in the "
                                     "past.")
        return {"epoch": epoch, "recurrence": None, "kind": "in-rel"}
    if day_m:
        d = day_m.group(1).lower()
        if d.startswith("every "):
            word = d.split(None, 1)[1]
            base = _wall(tz_name, now_epoch)
            hour, minute = _resolve_clock_period(clock, period)
            if word == "week":
                out_rec = {"kind": "weekly", "weekday": base.weekday(),
                           "hour": hour, "minute": minute}
                epoch = _wall_hour(hour, minute, base, ZoneInfo(tz_name),
                                   now_epoch, base.weekday())
            elif word in _WEEKDAYS:
                wd = _WEEKDAYS[word]
                out_rec = {"kind": "weekly", "weekday": wd,
                           "hour": hour, "minute": minute}
                epoch = _wall_hour(hour, minute, base, ZoneInfo(tz_name),
                                   now_epoch, wd)
            else:
                out_rec = {"kind": "daily", "hour": hour,
                           "minute": minute}
                epoch = _wall_hour(hour, minute, base, ZoneInfo(tz_name),
                                   now_epoch)
            return {"epoch": epoch, "recurrence": out_rec, "kind":
                    "every"}
        if not clock and not period:
            raise ReminderTimeError(
                "What time should the reminder be? For example "
                "'7 PM', 'tomorrow morning', or 'in 2 hours'.")
        hour, minute = _resolve_clock_period(clock, period)
        tz = ZoneInfo(tz_name)
        if d in ("tomorrow", "next week"):
            base = _wall(tz_name, now_epoch) + timedelta(
                days=1 if d == "tomorrow" else 7)
            return {"epoch": _wall_hour(hour, minute, base, tz,
                                        now_epoch),
                    "recurrence": None, "kind": d}
        if d in _WEEKDAYS:      # bare weekday: next such day, one-shot
            return {"epoch": _wall_hour(hour, minute,
                                        _wall(tz_name, now_epoch), tz,
                                        now_epoch, _WEEKDAYS[d]),
                    "recurrence": None, "kind": d}
        # today / tonight: explicit day semantics — past is CLARIFIED
        base_day = _wall(tz_name, now_epoch)
        cand = datetime(base_day.year, base_day.month, base_day.day,
                        hour, minute, fold=0, tzinfo=tz).timestamp()
        if cand < now_epoch + _MIN_LEAD_S:
            mer = "PM" if hour >= 12 else "AM"
            raise ReminderTimeError(
                f"That time has passed. Did you mean tomorrow? Try "
                f"'tomorrow at {hour % 12 or 12}:{minute:02d} "
                f"{mer}'.")
        return {"epoch": cand, "recurrence": None, "kind": "today"}
    if clock or period:
        hour, minute = _resolve_clock_period(clock, period)
        epoch = _wall_hour(hour, minute, _wall(tz_name, now_epoch),
                           ZoneInfo(tz_name), now_epoch)
        # Bare wall time without a day/period word rolls to tomorrow
        # when it is already past (documented rule). When a DAY-PART
        # period word named the day-part ("evening", "tonight", …) and
        # the resolution landed on a later local date, clarify instead
        # of silently reinterpreting what the user said. (noon/midnight
        # are clock quantities, not day-parts → the rolling rule.)
        if period in _DAYPARTS and \
                _wall(tz_name, epoch).date() != \
                _wall(tz_name, now_epoch).date():
            mer = "PM" if hour >= 12 else "AM"
            raise ReminderTimeError(
                f"{period.capitalize()} time has passed today. Did "
                f"you mean tomorrow at {hour % 12 or 12}"
                f":{minute:02d} {mer}?")
        return {"epoch": epoch, "recurrence": None, "kind": "at"}
    raise ReminderTimeError(
        "I couldn't find a time in that. Try 'at 7 PM', 'tomorrow "
        "morning', 'in 2 hours', or 'every day at 8 PM'.")


def next_recurring(due_epoch: float, recurrence: dict, tz_name: str,
                   now_epoch: float) -> float:
    """Next occurrence strictly after now, anchored to the stored
    local wall time. Recurrence dicts carry an EXPLICIT anchor
    (hour/minute, weekday for weekly) recorded at creation — so a
    spring-forward gap that moved one occurrence forward never drags
    the following occurrences with it (the schedule re-anchors to the
    intended wall time). Time-of-day is fixed in the reminder's zone;
    the epoch delta is NOT fixed across DST."""
    kind = (recurrence or {}).get("kind")
    tz = ZoneInfo(tz_name)
    local = _wall(tz_name, due_epoch)
    ahour = recurrence.get("hour")
    aminute = recurrence.get("minute")
    hour = ahour if isinstance(ahour, int) else local.hour
    minute = aminute if isinstance(aminute, int) else local.minute
    if kind == "daily":
        return _wall_hour(hour, minute, local, tz, now_epoch)
    if kind == "weekly":
        wd = int(recurrence.get("weekday", local.weekday()))
        return _wall_hour(hour, minute, local, tz, now_epoch, wd)
    raise ReminderValueError(f"Unknown recurrence kind: {kind!r}")


# ------------------------------------------------------------------
# NL intent parsing — reminder CONTROL (deterministic, no model)
# ------------------------------------------------------------------

def is_reminder_utterance(clean: str) -> bool:
    return bool(re.search(r"\bremind|reminder", (clean or "").lower()))


# When-clause token grammar (case-insensitive; spans used to (a)
# extract the task text with original casing, (b) compose the when
# spec deterministically).
_TOK_EVERY = re.compile(
    r"\bevery\s+(?:day|week|monday|tuesday|wednesday|thursday|friday"
    r"|saturday|sunday|morning|afternoon|evening|night)\b", re.I)
_TOK_DAY = re.compile(r"\b(?:tomorrow|today|tonight|next week)\b", re.I)
_TOK_PERIOD = re.compile(
    r"\b(?:morning|afternoon|evening|noon|midnight|night)\b", re.I)
_TOK_CLOCK = re.compile(
    r"\b(?:at\s+)?\d{1,2}:\d{2}(?:\s*(?:am|pm))?\b"
    r"|\b(?:at\s+)\d{1,2}(?:\s*(?:am|pm))?\b"
    r"|\b\d{1,2}\s*(?:am|pm)\b", re.I)   # bare "at 8 PM" style only
_TOK_REL = re.compile(
    r"\bin\s+\d{1,5}\s*(?:minutes?|mins?|hours?|hrs?|days?)\b", re.I)


def _when_tokens(text: str) -> list[tuple[int, int, str]]:
    """Positional when-token spans of the raw (case-preserved) text."""
    spans: list[tuple[int, int, str]] = []
    for pattern in (_TOK_EVERY, _TOK_DAY, _TOK_PERIOD, _TOK_CLOCK,
                    _TOK_REL):
        for m in pattern.finditer(text):
            if pattern is _TOK_CLOCK:
                # A bare clock token must carry am/pm, minutes, or the
                # word 'at' — otherwise it could eat numbers from the
                # task text ("read chapter 12").
                tok = m.group(0).strip()
                if not (_MER.search(tok) or ":" in tok or
                        tok.lower().startswith("at ")):
                    continue
            spans.append((m.start(), m.end(), m.group(0)))
    # remove overlaps (same phrase matched by two token families, e.g.
    # 'noon' in both _TOK_PERIOD and _TOK_CLOCK): keep the earliest /
    # longest span
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    out: list[tuple[int, int, str]] = []
    last_end = -1
    for s in spans:
        if s[0] >= last_end:
            out.append(s)
            last_end = s[1]
    return out


def parse_intent(utterance: str, now_epoch: float, tz_name: str) -> dict:
    """Deterministic op parse of a cleaned utterance.

    Returns {"op": "list"|"cancel"|"reschedule"|"find"|"create"|
    "clarify"|"invalid"|"unknown", ...}. Nothing here executes."""
    raw = (utterance or "").strip()
    low = re.sub(r"^\s+", "", raw.lower())
    m = re.fullmatch(
        r"(?:show|list|what are|tell me)(?: me)?(?: my| the| all)?\s*"
        r"reminders?(?:\s+for\s+(tomorrow|today|this week|next week))?",
        low)
    if m:
        return {"op": "list", "filter": m.group(1) or ""}
    m = re.fullmatch(
        r"(?:cancel|delete|remove)(?:\s+my|\s+the)?\s*"
        r"(?P<t>[^']*?)(?:\s+reminder|\s+reminders)?", low)
    if m:
        return {"op": "cancel", "target": m.group("t").strip()}
    m = re.fullmatch(
        r"(?:move|reschedule|push|postpone)(?:\s+my|\s+the)?\s*"
        r"(?P<t>[^']*?)(?:\s+reminder)?\s+to\s+(?P<w>.+)", low)
    if m:
        return {"op": "reschedule", "target": m.group("t").strip(),
                "when": m.group("w").strip()}
    m = re.fullmatch(
        r"(?:when is|what time is|where is|find|check)(?:\s+my|\s+the)"
        r"?\s*(?P<t>[^']*?)(?:\s+reminder)?", low)
    if m:
        return {"op": "find", "target": m.group("t").strip()}
    m = re.fullmatch(r"remind me(?:\s+(?:to|about|that)\b|\s+to$)?\s*"
                     r"(?P<rest>.+)", raw, re.I)
    if m:
        # operate on the raw (case-preserved) text with the same
        # offsets: raw = "remind me " + rest-in-original-casing
        rest0 = m.start("rest")            # raw-string offset of rest
        rest_raw = raw[rest0:].rstrip(".!")
        toks = _when_tokens(rest_raw)
        if not toks:
            return {"op": "clarify", "message":
                    "I can set a reminder — tell me the time. For "
                    "example: 'remind me to study Java at 7 PM', "
                    "'remind me tomorrow morning to finish my "
                    "assignment', or 'remind me every day at 8 PM to "
                    "review my notes'."}
        # deterministic removal: strip the when-token spans from the
        # raw text right-to-left (keeps earlier offsets valid)
        removed = rest_raw
        for (s, e, _) in reversed(toks):
            removed = removed[:s] + " " + removed[e:]
        when_spec = " ".join(t for (_, _, t) in toks)
        try:
            w = parse_when(when_spec, now_epoch, tz_name)
        except ReminderTimeError as e:
            return {"op": "clarify", "message": str(e)}
        except ReminderValueError as e:
            return {"op": "invalid", "message": str(e)}
        task = " ".join(removed.split())
        task = re.sub(r"^(?:to|about|that)\s+", "", task, flags=re.I)
        task = re.sub(r"\s+$", "", task).strip(" ,")
        if not task:
            return {"op": "clarify", "message":
                    "What should the reminder say? For example "
                    "'remind me to study Java at 7 PM'."}
        return {"op": "create", "text": task,
                "epoch": w["epoch"], "recurrence": w["recurrence"],
                "when_text": when_spec}
    return {"op": "unknown"}


# ------------------------------------------------------------------
# service
# ------------------------------------------------------------------

class ReminderService:
    """Owns reminder rows; drives the EXISTING scheduler +
    notifications. Exactly ONE live scheduler job per reminder;
    delivery is ledger-deduped and retries are bounded; restart
    recovery reconciles from the additive `reminders` table without
    depending on hook availability. Zero tool/execution authority is
    added."""

    def __init__(self, scheduler, notifications, audit=None,
                 db_path: str = "",
                 now: Optional[Callable[[], float]] = None,
                 tz_name: Optional[str] = None,
                 env: Optional[dict] = None,
                 max_attempts: int = MAX_DELIVERY_ATTEMPTS) -> None:
        self.sched = scheduler
        self.notifs = notifications
        self.audit = audit
        self._now = now or (lambda: _time.time())
        self._tz = tz_name if tz_name is not None else configured_tz(env)
        self._max_attempts = max(1, int(max_attempts))
        # RLock: persist may run while a caller (cancel/reschedule/
        # deliver) already holds the row lock.
        self._lock = threading.RLock()
        self._rows: dict[str, dict] = {}
        self._delivered: dict[str, float] = {}
        self._db = None
        if db_path:
            self._db = sqlite3.connect(db_path, check_same_thread=False)
            self._setup_db()
        self._recover()

    def _setup_db(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS reminders ("
                "id TEXT PRIMARY KEY, text TEXT NOT NULL, "
                "due REAL NOT NULL, tz TEXT NOT NULL, "
                "recurrence TEXT DEFAULT '{}', status TEXT NOT NULL, "
                "job_id TEXT, who TEXT, device_id TEXT, "
                "attempts INT DEFAULT 0, "
                "delivered_keys TEXT DEFAULT '[]', "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL)")
            self._db.commit()

    def _audit(self, action: str, target: str = "", detail: str = ""):
        try:
            if self.audit is not None:
                self.audit.record("reminders", action, target,
                                  detail[:500])
        except Exception:  # noqa: BLE001 — audit never breaks reminders
            pass

    # -- persistence -------------------------------------------------

    def _persist(self, row: dict) -> None:
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO reminders (id, text, due,"
                    " tz, recurrence, status, job_id, who, device_id,"
                    " attempts, delivered_keys, created_at, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["id"], row["text"], row["due"], row["tz"],
                     json.dumps(row["recurrence"]), row["status"],
                     row["job_id"], row["who"], row["device_id"],
                     row["attempts"],
                     json.dumps(sorted(row["delivered_keys"])),
                     row["created_at"], _time.time()))
                self._db.commit()
        except Exception:  # noqa: BLE001 — memory state remains honest
            self._audit("reminder_persist_failed", row["id"], )

    def _load_rows(self) -> None:
        if self._db is None:
            return
        c = self._db.cursor()
        c.execute("SELECT id, text, due, tz, recurrence, status, "
                  "job_id, who, device_id, attempts, delivered_keys, "
                  "created_at FROM reminders")
        for (rid, text, due, tz, rec, status, job_id, who, device_id,
             attempts, dkeys, created_at) in c.fetchall():
            try:
                rec = json.loads(rec or "{}")
                keys = [k for k in (json.loads(dkeys or "[]") or [])
                        if isinstance(k, str)]
            except Exception:  # noqa: BLE001 — corrupt fields fail safe
                rec, keys = {}, []
            self._rows[rid] = {
                "id": rid, "text": text, "due": float(due), "tz": tz,
                "recurrence": rec if isinstance(rec, dict) else {},
                "status": status, "job_id": job_id, "who": who,
                "device_id": device_id, "attempts": int(attempts),
                "delivered_keys": set(keys),
                "created_at": float(created_at)}
            for k in keys:
                self._delivered[k] = float(due)

    def _recover(self) -> None:
        """Restart reconciliation (ledger-based; never hook-dependent):
        any scheduled occurrence that is due and not yet on the
        delivery ledger is delivered through the normal path
        (missed-run policy); future timers are verified and re-armed
        once if the scheduler lost them."""
        self._load_rows()
        now = self._now()
        for row in list(self._rows.values()):
            if row["status"] in ("cancelled", "delivery_failed"):
                continue
            due = row["due"]
            key = f"{row['id']}:{due:.0f}"
            try:
                pending_job = any(
                    p.get("id") == row["job_id"]
                    for p in self.sched.pending())
            except Exception:  # noqa: BLE001
                pending_job = False
            if due <= now:
                if key not in self._delivered:
                    note = ("missed_run"
                            if self._now() - due > 120
                            else "restart_receipt")
                    self._deliver(row, due, note=note)
                if row["recurrence"]:
                    self._rearm(row, next_recurring(
                        due, row["recurrence"], row["tz"], now))
            elif not pending_job and row["status"] == "scheduled":
                self._rearm(row, due)
            self._persist(row)

    # -- CRUD --------------------------------------------------------

    def create(self, text: str, due_epoch: float, who: str = "user",
               requester_device: str = "",
               recurrence: Optional[dict] = None,
               tz_name: Optional[str] = None,
               dedup: bool = True) -> dict:
        """Validate + persist + schedule exactly ONE job."""
        text = " ".join((text or "").split())[:_TEXT_MAX]
        if not text or len(text) < 2:
            raise ReminderValueError("Reminder text is required.")
        tz_name = tz_name or self._tz
        ZoneInfo(tz_name)      # unknown zone → loud validation error
        now = self._now()
        recurrence = dict(recurrence or {})
        if recurrence:
            kind = recurrence.get("kind")
            if kind not in ("daily", "weekly"):
                raise ReminderValueError("Unsupported recurrence.")
            if kind == "weekly" and not (
                    isinstance(recurrence.get("weekday"), int) and
                    0 <= recurrence["weekday"] <= 6):
                raise ReminderValueError("Invalid weekday.")
            # explicit anchor (hour/minute) is validated when present
            ah = recurrence.get("hour")
            am = recurrence.get("minute")
            if ah is not None and \
                    (not isinstance(ah, int) or not 0 <= ah <= 23):
                raise ReminderValueError("Invalid recurrence hour.")
            if am is not None and \
                    (not isinstance(am, int) or not 0 <= am <= 59):
                raise ReminderValueError("Invalid recurrence minute.")
            due_epoch = next_recurring(due_epoch, recurrence, tz_name,
                                       now)
        if due_epoch < now + _MIN_LEAD_S:
            raise ReminderValueError("A reminder must be in the "
                                     "future.")
        if dedup:
            bucket = int(due_epoch // 60)
            with self._lock:
                rows = [dict(r) for r in self._rows.values()
                        if r["status"] not in ("cancelled",
                                               "delivery_failed")]
            for row in rows:
                if row["text"] == text and \
                        int(row["due"] // 60) == bucket and \
                        row["recurrence"] == recurrence:
                    return {"summary": self._summary(row),
                            "duplicate": True}
        rid = f"rem-{uuid.uuid4().hex[:12]}"
        row = {"id": rid, "text": text, "due": float(due_epoch),
               "tz": tz_name, "recurrence": recurrence,
               "status": "scheduled", "job_id": "", "who": who,
               "device_id": requester_device, "attempts": 0,
               "delivered_keys": set(), "created_at": now}
        delay = max(0.05, due_epoch - now)
        try:
            jid = self.sched.schedule_once(
                "reminder", delay,
                {"reminder": {"id": rid, "due": float(due_epoch)}})
        except Exception as e:
            raise ReminderValueError(f"scheduling failed: {e}") from e
        row["job_id"] = jid
        with self._lock:
            self._rows[rid] = row
        self._persist(row)
        self._audit(
            "reminder_created", rid,
            f"due={self._fmt(due_epoch, tz_name)} tz={tz_name} "
            f"rec={row['recurrence']} who={who} job={jid}")
        return {"summary": self._summary(row), "duplicate": False}

    def upcoming(self, now: Optional[float] = None) -> list[dict]:
        now = self._now() if now is None else now
        with self._lock:
            rows = [dict(r) for r in self._rows.values()
                    if r["status"] == "scheduled" and r["due"] > now]
        rows.sort(key=lambda r: r["due"])
        return [self._summary(r) for r in rows]

    def _scheduled_rows(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rows.values()
                    if r["status"] == "scheduled"]

    def find(self, target: str) -> dict:
        """Match by id-prefix OR case-insensitive substring of text.
        Read-only."""
        target = (target or "").strip().lower()
        rows = self._scheduled_rows()
        if not target:
            hits = rows
        else:
            hits = [r for r in rows
                    if r["id"].startswith(target)
                    or target in r["text"].lower()]
        return {"matches": [self._summary(r) for r in hits]}

    def _resolve(self, target: str) -> tuple[Optional[dict], str]:
        hits = self.find(target)["matches"]
        if not hits:
            return None, ("I have no scheduled reminder matching "
                          "that. Say 'show my reminders' to list "
                          "them.")
        if len(hits) > 1:
            names = "; ".join(f"'{h['text'][:60]}' ({h['id'][:8]})"
                              for h in hits[:5])
            return None, ("Multiple reminders match. Which one? "
                          f"{names}")
        with self._lock:
            return dict(self._rows[hits[0]["id"]]), ""

    def cancel(self, target: str, who: str = "user") -> dict:
        row, err = self._resolve(target)
        if row is None:
            self._audit("reminder_cancel_refused", (target or
                                                    "")[:100], err)
            return {"message": err, "cancelled": False}
        try:
            self.sched.cancel(row["job_id"])
        except Exception:  # noqa: BLE001 — still marked cancelled
            pass
        with self._lock:
            self._rows[row["id"]]["status"] = "cancelled"
            self._persist(self._rows[row["id"]])
        self._audit("reminder_cancelled", row["id"], f"who={who}")
        return {"message": f"Cancelled reminder "
                           f"'{row['text'][:100]}'.",
                "cancelled": True}

    def reschedule(self, target: str, epoch: float, who: str = "user",
                   recurrence: Optional[dict] = None) -> dict:
        row, err = self._resolve(target)
        if row is None:
            self._audit("reminder_reschedule_refused",
                        (target or "")[:100], err)
            return {"message": err, "rescheduled": False}
        now = self._now()
        if recurrence:
            epoch = next_recurring(epoch, recurrence, row["tz"], now)
        if epoch < now + _MIN_LEAD_S:
            raise ReminderValueError("That time is already in the "
                                     "past.")
        try:
            self.sched.cancel(row["job_id"])
        except Exception:  # noqa: BLE001 — replaced below regardless
            pass
        delay = max(0.05, epoch - now)
        jid = self.sched.schedule_once(
            "reminder", delay,
            {"reminder": {"id": row["id"], "due": float(epoch)}})
        with self._lock:
            live = self._rows[row["id"]]
            live["due"] = float(epoch)
            live["job_id"] = jid
            live["status"] = "scheduled"
            live["attempts"] = 0
            if recurrence:
                live["recurrence"] = dict(recurrence)
            summary = self._summary(live)
            self._persist(live)
        self._audit("reminder_rescheduled", row["id"],
                    f"who={who} due={epoch:.0f} job={jid}")
        return {"message": f"Moved '{row['text'][:100]}' to "
                           f"{self._fmt(epoch, row['tz'])}.",
                "summary": summary}

    # -- firing / delivery -------------------------------------------

    def on_fire(self, job: dict) -> None:
        """Scheduler fire hook for reminder jobs: deliver + re-decide
        the next occurrence for recurring reminders. Never executes
        anything and never decides permissions."""
        payload = ((job.get("payload") or {}).get("reminder") or {})
        rid = str(payload.get("id", ""))
        with self._lock:
            row = dict(self._rows.get(rid) or {}) or None
        if row is None or row["status"] in ("cancelled",
                                            "delivery_failed"):
            return
        due = float(payload.get("due", row["due"]))
        try:
            cancelled = self.sched.is_cancelled(job.get("id", ""))
        except Exception:  # noqa: BLE001
            cancelled = False
        if cancelled:
            return
        self._deliver(row, due)
        if row["recurrence"]:
            self._rearm(row, next_recurring(due, row["recurrence"],
                                            row["tz"], self._now()))

    def _deliver(self, row: dict, due: float, note: str = "") -> None:
        """One delivery per (reminder, occurrence); bounded retries."""
        key = f"{row['id']}:{due:.0f}"
        if key in self._delivered:
            return
        with self._lock:
            live = self._rows[row["id"]]
            live["status"] = "due"
            attempt = int(live["attempts"]) + 1
            live["attempts"] = attempt
            self._persist(live)
        self._audit("reminder_due", row["id"],
                    f"attempt={attempt} due={due:.0f}"
                    f"{' ' + note if note else ''}".strip())
        err = ""
        nid = ""
        try:
            n = self.notifs.create(
                title=f"Reminder: {row['text'][:120]}",
                body=f"{self._fmt(due, row['tz'])}: "
                     f"{row['text'][:200]}" +
                     (f" ({note})" if note else ""),
                device_id=row["device_id"] or None)
            nid = getattr(n, "id", "")
        except Exception as e:  # noqa: BLE001 — honest failure
            err = str(e)[:200]
        if err:
            with self._lock:
                live = self._rows[row["id"]]
                live["status"] = "delivery_attempted"
                self._persist(live)
            self._audit("reminder_delivery_failed", row["id"],
                        f"attempt={attempt}/{self._max_attempts} "
                        f"error={err}")
            if row["recurrence"]:
                # documented: recurring reminders chase the schedule,
                # not past deliveries — the next occurrence re-delivers
                return
            if attempt < self._max_attempts:
                self.sched.schedule_once(
                    "reminder-retry", _RETRY_DELAY_S * attempt,
                    {"reminder": {"id": row["id"], "due": due}})
            else:
                with self._lock:
                    live = self._rows[row["id"]]
                    live["status"] = "delivery_failed"
                    self._persist(live)
                self._audit("reminder_delivery_failed_final",
                            row["id"], f"attempts={attempt}")
            return
        with self._lock:
            live = self._rows[row["id"]]
            live["status"] = "delivered_to_service"
            live["delivered_keys"].add(key)
            self._persist(live)
        self._delivered[key] = _time.time()
        self._audit("reminder_delivered", row["id"],
                    f"notification={nid} attempt={attempt}"

                    f"{' ' + note if note else ''}")

    def _rearm(self, row: dict, due: float) -> None:
        """Schedule the next occurrence — exactly one live job."""
        now = self._now()
        delay = max(0.05, due - now)
        jid = self.sched.schedule_once(
            "reminder", delay,
            {"reminder": {"id": row["id"], "due": float(due)}})
        with self._lock:
            live = self._rows[row["id"]]
            live["due"] = float(due)
            live["job_id"] = jid
            live["status"] = "scheduled"
            live["attempts"] = 0
            self._persist(live)
        self._audit("reminder_next_scheduled", row["id"],
                    f"due={due:.0f} job={jid}")

    # -- formatting / introspection ----------------------------------

    def _fmt(self, due: float, tz_name: str) -> str:
        try:
            return datetime.fromtimestamp(
                due, ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")
        except Exception:  # noqa: BLE001
            return datetime.fromtimestamp(
                due, _rtc.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _summary(self, row: dict) -> dict:
        return {"id": row["id"], "text": row["text"],
                "due": self._fmt(row["due"], row["tz"]),
                "due_epoch": row["due"], "tz": row["tz"],
                "recurrence": row["recurrence"],
                "status": row["status"]}

    def stats(self) -> dict:
        with self._lock:
            by_status: dict[str, int] = {}
            for r in self._rows.values():
                by_status[r["status"]] = by_status.get(r["status"],
                                                        0) + 1
        by_status["live_jobs"] = len(self.sched.pending())
        return {"total": len(self._rows), "by_status": by_status}


# ------------------------------------------------------------------
# turn integration: utterance → reminder result
# ------------------------------------------------------------------

_LIST_FILTER_DAYS = {"today": 1, "tomorrow": 2, "this week": 7,
                     "next week": 7}


def _rec_label(rec: Optional[dict]) -> str:
    if not rec:
        return ""
    if rec.get("kind") == "daily":
        return "repeats daily"
    if rec.get("kind") == "weekly":
        return f"repeats weekly on {_DAY_LABELS[int(rec.get('weekday', 0))]}"
    return ""


def handle_reminder_utterance(service: ReminderService, utterance: str,
                              now_epoch: float, who: str = "user",
                              requester_device: str = "") -> Optional[dict]:
    """Parse + dispatch one reminder utterance into a turn-shaped
    result. Read-only ops never mutate; mutating ops are validated and
    audited by the service. Returns None when the utterance is not a
    reminder operation. `executed` is always False: reminders touch
    data and notifications, never devices or tools."""
    tz_name = service._tz
    op = parse_intent(utterance, now_epoch, tz_name)
    kind = op.get("op")
    if kind == "unknown":
        return None
    if kind == "clarify":
        return {"message": op["message"], "executed": False,
                "clarified": True}
    if kind == "invalid":
        return {"message": f"I won't set that: {op['message']}",
                "executed": False, "refused": True}
    if kind == "list":
        ups = service.upcoming()
        filt = (op.get("filter") or "").lower()
        if filt and ups:
            anchor = _wall(tz_name, now_epoch).date()
            if filt == "tomorrow":
                lo = datetime(anchor.year, anchor.month, anchor.day,
                              tzinfo=ZoneInfo(tz_name)) + timedelta(
                    days=1)
                hi = lo + timedelta(days=1)
            elif filt == "today":
                lo = datetime(anchor.year, anchor.month, anchor.day,
                              tzinfo=ZoneInfo(tz_name))
                hi = lo + timedelta(days=1)
            else:
                lo = datetime(anchor.year, anchor.month, anchor.day,
                              tzinfo=ZoneInfo(tz_name))
                hi = lo + timedelta(days=_LIST_FILTER_DAYS.get(filt, 7))
            ups = [u for u in ups
                   if lo.timestamp() <= u["due_epoch"] <
                   hi.timestamp()]
        if not ups:
            return {"message": "No upcoming reminders"
                               f"{f' within {filt}' if filt else ''}.",
                    "executed": False, "reminders": []}
        body = "; ".join(
            f"'{u['text'][:60]}' at {u['due']}"
            f"{' (' + _rec_label(u['recurrence']) + ')' if u['recurrence'] else ''}"
            for u in ups[:5])
        more = f" — and {len(ups) - 5} more" if len(ups) > 5 else ""
        return {"message": f"{len(ups)} upcoming reminder(s): "
                           f"{body}{more}.", "executed": False,
                "reminders": ups}
    target = (op.get("target") or "").strip()
    if kind == "find":
        res = service.find(target)
        if not res["matches"]:
            return {"message": "No scheduled reminder matches that.",
                    "executed": False, "reminders": []}
        if len(res["matches"]) > 1:
            return {"message": f"{len(res['matches'])} reminders "
                               f"match; say 'show my reminders'.",
                    "executed": False, "reminders": res["matches"]}
        s = res["matches"][0]
        when = (f"{s['due']}"
                f"{' ' + _rec_label(s['recurrence']) if s['recurrence'] else ''}")
        return {"message": f"'{s['text'][:100]}' is set for {when}.",
                "executed": False, "reminder": s}
    if kind == "cancel":
        out = service.cancel(target, who)
        return {"message": out["message"], "executed": False,
                "cancelled": out["cancelled"]}
    if kind == "reschedule":
        try:
            w = parse_when(op.get("when", ""), now_epoch, tz_name)
        except ReminderTimeError as e:
            return {"message": str(e), "executed": False,
                    "clarified": True}
        except ReminderValueError as e:
            return {"message": f"I won't move it: {e}",
                    "executed": False, "refused": True}
        try:
            out = service.reschedule(
                target, w["epoch"], who,
                recurrence=w["recurrence"] or None)
        except ReminderValueError as e:
            return {"message": f"I can't move it: {e}",
                    "executed": False, "refused": True}
        return {"message": out["message"], "executed": False,
                "reminder": out.get("summary")}
    if kind == "create":
        try:
            out = service.create(
                op["text"], op["epoch"], who=who,
                requester_device=requester_device,
                recurrence=op["recurrence"] or None)
        except ReminderValueError as e:
            return {"message": f"I won't set that: {e}",
                    "executed": False, "refused": True}
        s = out["summary"]
        msg = (f"Noted — you already have that reminder "
               f"({s['due']})." if out["duplicate"] else
               f"Reminder set: '{s['text'][:100]}' at {s['due']}"
               f"{' ' + _rec_label(s['recurrence']) if s['recurrence'] else ''}.")
        return {"message": msg, "executed": False,
                "reminder": s, "duplicate": out["duplicate"]}
    return {"message": "I couldn't process that reminder.",
            "executed": False}
