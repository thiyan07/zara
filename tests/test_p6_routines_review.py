"""Routines + weekly review (P6-b27) — parser, service, fire/re-a.rm,
turn routing, review assembly, and honesty.

Deterministic throughout: injected fake clock, a ManualScheduler
double that drives the REAL fire path, in-memory NotificationManager,
and fixture-only utterances. No real notifications are sent; no device
ever executes anything. Zone is UTC for determinism (wall-clock
anchoring is ReminderService's tested contract; routines reuse the
same grammar for the clock token only).
"""
from datetime import datetime, timedelta

import pytest

from core.notifications import NotificationManager
from core.routines import (RoutineService, handle_routine_utterance,
                           is_routine_utterance, parse_dayspec,
                           parse_routine_intent)
from core.scheduler import Scheduler as _RealScheduler
from core.weekly_review import WeeklyReviewFlow

NOW = 1760900000.0            # 2025-10-19 18:53:20 UTC (a Sunday)


class FakeClock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class ListAudit:
    def __init__(self):
        self.entries = []

    def record(self, source, action, target="", detail=""):
        self.entries.append((source, action, target, detail))


class ManualScheduler(_RealScheduler):
    """Never arms timer threads; the test drives fires through the
    REAL _fire path."""

    def _arm(self, job, delay_s):
        pass


def _svc(clock=None, db_path="", tz="UTC"):
    audit = ListAudit()
    notifs = NotificationManager()
    service = RoutineService(ManualScheduler(), notifs, audit,
                             db_path=db_path, now=clock or FakeClock(),
                             tz_name=tz)
    service.sched._on_fire = service.on_fire
    return service, notifs, audit


def _throwing_calendar():
    class C:
        def wants_turn(self, clean):
            return False

        def turn(self, clean, who="user"):
            raise RuntimeError("calendar down")

        def conflicts_on_date(self, target):
            return None

    return C()


@pytest.fixture
def fake_now():
    return FakeClock()


# ---------------- cadence spec parsing ----------------

def test_dayspec_variants():
    assert parse_dayspec("day") == (0, 1, 2, 3, 4, 5, 6)
    assert parse_dayspec("weekdays") == (0, 1, 2, 3, 4)
    assert parse_dayspec("weekend") == (5, 6)
    assert parse_dayspec("monday, wednesday and friday") == (0, 2, 4)
    assert parse_dayspec("tues, thurs") == (1, 3)
    assert parse_dayspec("sun") == (6,)
    assert parse_dayspec("someday soon") is None
    assert parse_dayspec("") is None


# ---------------- routine surface routing ----------------

def test_routine_gate_and_exclusions():
    assert is_routine_utterance("add routine workout every day at 7 am "
                                "to stretch")
    assert not is_routine_utterance("remind me to keep my routine")
    assert not is_routine_utterance("show my reminders")
    assert is_routine_utterance("cancel routine workout")


def test_routine_parse_ops():
    op = parse_routine_intent("add routine called stretch every day "
                              "at 7 AM do full body warm up")
    assert op["op"] == "create"
    assert op["name"] == "stretch"
    assert "warm up" in op["plan"]
    assert op["cadence"]["kind"] == "daily"
    assert op["cadence"]["hour"] == 7
    op2 = parse_routine_intent("show my routines")
    assert op2["op"] == "list"
    op3 = parse_routine_intent("cancel routine stretch")
    assert op3["op"] == "cancel" and op3["name"] == "stretch"
    op4 = parse_routine_intent("add routine walk")
    assert op4["op"] == "clarify" and "Routines look like" in op4["message"]
    # no days/clock → must not silently create
    assert op4["op"] in ("unknown", "clarify")


def test_routine_create_without_time_is_clarified():
    op = parse_routine_intent("add routine walk every day do laps")
    assert op["op"] == "clarify"
    assert "time of day" in (op["message"] or "")


# ---------------- service lifecycle ----------------

def test_create_list_cancel_round_trip(fake_now):
    svc, notifs, audit = _svc()
    s = svc.create("stretch", "full body warm up",
                   {"kind": "daily", "hour": 8, "minute": 0})
    assert s["name"] == "stretch" and s["status"] == "scheduled"
    rows = svc.list_rows()
    assert len(rows) == 1 and rows[0]["cadence_label"] == "daily"
    out = svc.cancel("stretch")
    assert out["cancelled"] is True
    assert svc.list_rows() == []
    assert "routine_cancelled" in [a for (_, a, _, _)
                                   in audit.entries]


def test_create_weekday_set_anchors_five_day_cadence(fake_now):
    svc, _, _ = _svc()
    # NOW is a Sunday; Mon/Wed/Fri cadence → next fire is Monday
    s = svc.create("gym", "lift",
                   {"kind": "weekly", "hour": 7, "minute": 30,
                    "weekdays": [0, 2, 4]})
    nxt = datetime.fromtimestamp(s["next_epoch"])
    assert nxt.weekday() == 0          # Monday


def test_duplicate_name_refused(fake_now):
    svc, _, _ = _svc()
    svc.create("stretch", "alpha", {"kind": "daily", "hour": 8,
                                    "minute": 0})
    with pytest.raises(RoutineService.RoutineValueError):
        svc.create("stretch", "beta", {"kind": "daily", "hour": 8,
                                       "minute": 0})


def test_fire_announces_and_rearms(fake_now):
    svc, notifs, audit = _svc(clock=fake_now)
    s = svc.create("stretch", "full body warm up",
                   {"kind": "daily", "hour": 8, "minute": 0})
    # fire at the OCCURRENCE time (the service re-anchors from now)
    fake_now.t = s["next_epoch"] + 5
    for p in list(svc.sched.pending()):
        svc.sched._fire(p["id"])
    titles = [n.title for n in notifs.pending(None)]
    assert any("Routine: stretch" in t for t in titles)
    # re-armed to the next day, same wall time
    rows = svc.list_rows()
    assert len(rows) == 1
    assert rows[0]["next_epoch"] > s["next_epoch"] + 20 * 3600
    assert "routine_announced" in [a for (_, a, _, _) in
                                   audit.entries]


def test_restart_persistence_survives(fake_now, tmp_path):
    db = str(tmp_path / "routines.db")
    svc, _, _ = _svc(clock=FakeClock(NOW), db_path=db)
    svc.create("hydrate", "drink a glass",
               {"kind": "daily", "hour": 9, "minute": 0})
    svc2, _, _ = _svc(clock=FakeClock(NOW + 60), db_path=db)
    rows = svc2.list_rows()
    assert len(rows) == 1 and rows[0]["name"] == "hydrate"


# ---------------- weekly review flow ----------------

class _FakeCalendar:
    def wants_turn(self, clean):
        return "calendar" in clean

    def turn(self, clean, who="user"):
        return {"message": "Calendar for this week: 2 event(s)."}

    def conflicts_on_date(self, target):
        return None


def test_review_helpers_report_absent_stores():
    flow = WeeklyReviewFlow(audit=ListAudit(), reminders=None,
                            routines=None, capture=None, ledgers=None)
    assert not flow.wants_turn("what's on my calendar today")
    assert flow.wants_turn("weekly review")
    assert flow.wants_turn("run my weekly review")
    assert flow.wants_turn("review my week")
    assert "not wired" in flow._reminders_line()
    assert "not wired" in flow._routines_line()
    assert "not wired" in flow._capture_line()
    assert "not wired" in flow._ledgers_line()


def test_review_full_assembly():
    audit = ListAudit()
    flow = WeeklyReviewFlow(audit=audit,
                            calendar_flow=_FakeCalendar(),
                            reminders=None, routines=None,
                            capture=None, ledgers=None)
    out = flow.turn("weekly review")
    assert out is not None and out["message"]
    msg = out["message"]
    assert "Weekly review" in msg
    assert "Conflicts" in msg         # named section even on failure
    assert "read failed" in msg or "Conflicts: none" in msg or \
        "conflict read failed" in msg
    assert out["executed"] is False and out["verified"] is True


def test_review_counts_conflicts_across_week():
    class Conf:
        wants = False

        def __init__(self, flow):
            self.f = flow

        def conflicts_on_date(self, target):
            # exactly one conflict on Wednesdays
            if target.weekday() == 2:
                return (1, ["10:00–11:00 × 10:30–12:00 → Test overlap"],
                        0, 0)
            return (0, [], 0, 0)

    audit = ListAudit()
    flow = WeeklyReviewFlow(audit=audit, calendar_flow=None)
    flow.calendar_flow = Conf(flow)
    msg = flow.turn("review my week")["message"]
    assert "Conflicts: 1 this week" in msg
    assert "Test overlap" in msg


def test_review_degrades_on_calendar_exception():
    audit = ListAudit()
    flow = WeeklyReviewFlow(audit=audit,
                            calendar_flow=_throwing_calendar())
    msg = flow.turn("weekly review")["message"]
    # per-section honesty: the calendar line names the failure; the
    # review itself still assembles
    assert "Calendar" in msg
    assert "unavailable" in msg


def test_review_routines_section_real_read(fake_now):
    svc, _, _ = _svc()
    svc.create("stretch", "warm up",
               {"kind": "daily", "hour": 8, "minute": 0})
    audit = ListAudit()
    flow = WeeklyReviewFlow(audit=audit, calendar_flow=None,
                            routines=svc)
    msg = flow.turn("weekly review")["message"]
    assert "Routine" in msg and "'stretch'" in msg


# ---------------- review habits section (batch-32) ----------------

def test_review_habits_line_not_wired_and_section_named():
    flow = WeeklyReviewFlow(audit=ListAudit())
    assert "not wired" in flow._habits_line()


def test_review_habits_section_real_read():
    from core.habits import HabitService
    svc = HabitService()
    svc.create("gym", "day")
    svc.mark("gym")
    flow = WeeklyReviewFlow(audit=ListAudit(), habits=svc)
    msg = flow.turn("weekly review")["message"]
    assert "Habits: 1 tracked — 'gym' (daily) streak 1" in msg
    assert "best 1" in msg


def test_review_budget_tail_real_read_batch32():
    from core.ledgers import LedgerService
    svc = LedgerService()
    svc.log_expense(45, "tea")
    svc.set_monthly_budget(500)
    flow = WeeklyReviewFlow(audit=ListAudit(), ledgers=svc)
    msg = flow.turn("weekly review")["message"]
    assert "monthly budget" in msg and "no currency" in msg
    flow2 = WeeklyReviewFlow(audit=ListAudit(), ledgers=LedgerService())
    msg2 = flow2.turn("weekly review")["message"]
    assert "monthly budget" not in msg2      # unset → honest silence
