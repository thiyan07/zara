"""Habit chains (P6-b33 / batch-32) — service math, NL flow, and
honest streak semantics.

The streak contract is adopted as a DESIGN from Loop Habit Tracker
(github.com/iSoron/uhabits — interface/design only, never their code):
marks belong to days, streaks are consecutive scheduled-day runs, and
a chain closes only when a scheduled occurrence that already PASSED
went unmarked. The ledger's free-text habit diary (test_p6_ledgers)
is a different surface and stays untouched.

Deterministic: injected fake clock (TZ-free line arithmetic on
ISO dates), in-memory store unless a tmp_path db is requested.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.habits import (HabitService, HabitsFlow, HabitValueError,
                         handle_habit_utterance, parse_habit_intent)

NOW = 1760900000.0     # 2025-10-19 18:53:20 UTC (a Sunday)


class ListAudit:
    def __init__(self):
        self.entries = []

    def record(self, source, action, target="", detail=""):
        self.entries.append((source, action, target, detail))


class FakeClock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def shift(self, days: float) -> None:
        self.t += days * 86400.0


def _svc(clock=None, db_path=""):
    audit = ListAudit()
    svc = HabitService(audit, db_path=db_path,
                       now=clock or FakeClock())
    return svc, audit


# ---------------- parser shapes ----------------

def test_gate_and_exclusions():
    f = HabitsFlow(_svc()[0])
    assert f.wants_turn("add habit gym every day")
    assert f.wants_turn("mark gym done")
    assert f.wants_turn("show my habits")
    assert f.wants_turn("did gym")
    assert not f.wants_turn("add water 2 glasses")
    assert not f.wants_turn("capture (A) call mom")
    # excluded nouns stay out even when the phrase says 'habit'
    assert not f.wants_turn("add water habit reminder")


def test_parse_shapes():
    assert parse_habit_intent("add habit gym every day") == \
        {"op": "create", "name": "gym", "cadence": "day"}
    assert parse_habit_intent("add habit study spanish every "
                              "monday and thursday") == \
        {"op": "create", "name": "study spanish",
         "cadence": "monday and thursday"}
    m = parse_habit_intent("mark gym done")
    assert m["op"] == "mark" and m["name"] == "gym"
    assert parse_habit_intent("gym is done")["op"] == "mark"
    assert parse_habit_intent("did gym")["name"] in ("gym",)
    assert parse_habit_intent("show my habits")["op"] == "list"
    d = parse_habit_intent("remove habit gym")
    assert d["op"] == "delete" and d["name"] == "gym"
    assert parse_habit_intent("set up a meeting tomorrow")["op"] == \
        "unknown"


# ---------------- streak math (uhabits contract) ----------------

def test_create_daily_and_first_mark():
    svc, audit = _svc()
    row = svc.create("gym", "day")
    assert row["days"] == "0,1,2,3,4,5,6"
    out = handle_habit_utterance(svc, "mark gym done", who="user")
    assert out["executed"] is False and "streak now 1" in \
        out["message"]
    assert any(a[1] == "habit_marked" for a in audit.entries)


def test_second_today_mark_idempotent():
    svc, _ = _svc()
    svc.create("gym", "day")
    r1 = handle_habit_utterance(svc, "mark gym done")
    r2 = handle_habit_utterance(svc, "mark gym done")
    assert "chain does not grow twice" in r2["message"]
    s = svc._streak_stats(svc._find("gym"))
    assert s["n"] == 1 and s["current"] == 1


def test_chain_grows_and_gap_closes_it():
    clock = FakeClock()
    svc, _ = _svc(clock)
    svc.create("gym", "day")
    # three consecutive days then a skipped day
    handle_habit_utterance(svc, "mark gym done")
    clock.shift(1)
    handle_habit_utterance(svc, "mark gym done")
    clock.shift(1)
    r = handle_habit_utterance(svc, "mark gym done")
    assert "streak now 3" in r["message"]
    # the skipped day: do NOT mark today or tomorrow
    clock.shift(2)   # one day missed entirely (yesterday unmarked)
    out = svc.list_rows()[0]
    assert out["stats"]["closed"] is True
    assert out["stats"]["current"] == 3     # last run, kept honest
    assert "ended" in svc.status_line(svc._find("gym"))


def test_today_unmarked_does_not_break_the_streak():
    clock = FakeClock()
    svc, _ = _svc(clock)
    svc.create("gym", "day")
    clock.shift(1)
    handle_habit_utterance(svc, "mark gym done")
    clock.shift(1)          # today is now NOT marked yet
    stats = svc._streak_stats(svc._find("gym"))
    assert stats["closed"] is False
    assert stats["current"] == 1
    assert stats["today_marked"] is False
    out = handle_habit_utterance(svc, "show my habits")
    assert "today not marked yet" in out["message"]


def test_best_streak_counts_the_longest_run():
    clock = FakeClock()
    svc, _ = _svc(clock)
    svc.create("gym", "day")
    for _ in range(3):                      # 3-day run
        handle_habit_utterance(svc, "mark gym done")
        clock.shift(1)
    clock.shift(2)                          # break
    handle_habit_utterance(svc, "mark gym done")
    clock.shift(1)
    handle_habit_utterance(svc, "mark gym done")   # 2-day run
    s = svc._streak_stats(svc._find("gym"))
    assert s["best"] == 3 and s["current"] == 2


def _walk_to_weekday(svc, clock, target_wd: int) -> None:
    """Advance the CLOCK in 6h steps until the SERVICE-local date is
    the requested weekday (the service's _today() is the truth —
    never the test's UTC arithmetic)."""
    while date.fromisoformat(svc._today()).weekday() != target_wd:
        clock.t += 6 * 3600.0
    clock.t += 6 * 3600.0     # well inside the target day (not 00:0x)


def test_weekday_habit_only_counts_scheduled_days():
    clock = FakeClock()
    svc, _ = _svc(clock)
    svc.create("gym", "every monday and thursday")
    assert svc._find("gym")["days"] == "0,3"
    _walk_to_weekday(svc, clock, 0)          # Monday
    handle_habit_utterance(svc, "mark gym done")
    clock.shift(3)                            # Thursday
    stats = svc._streak_stats(svc._find("gym"))
    assert stats["current"] == 1 and stats["closed"] is False
    r = handle_habit_utterance(svc, "mark gym done")
    assert "streak now 2" in r["message"]     # adjacent occurrence kept


def test_weekday_habit_closes_on_missed_occurrence():
    clock = FakeClock()
    svc, _ = _svc(clock)
    svc.create("gym", "every monday and thursday")
    _walk_to_weekday(svc, clock, 0)          # Monday
    handle_habit_utterance(svc, "mark gym done")
    clock.shift(7)      # skip the Thursday occurrence entirely
    stats = svc._streak_stats(svc._find("gym"))
    assert stats["closed"] is True and stats["current"] == 1


def test_weekly_cadence_refused_with_reason():
    svc, _ = _svc()
    out = handle_habit_utterance(
        svc, "add habit deep clean every week", who="user")
    assert out["executed"] is False and "refused" in out
    assert "Weekly-count habits" in out["message"]
    assert svc._find("deep clean") is None


def test_duplicate_create_refused():
    svc, _ = _svc()
    svc.create("gym", "day")
    out = handle_habit_utterance(svc, "add habit gym every day")
    assert "already tracked" in out["message"]


def test_unknown_name_falls_through_not_refuses():
    svc, _ = _svc()
    assert handle_habit_utterance(svc, "mark dentist done") is None
    assert handle_habit_utterance(svc, "did python practice") is None


def test_delete_removes_chain_named():
    svc, audit = _svc()
    svc.create("gym", "day")
    handle_habit_utterance(svc, "mark gym done")
    out = handle_habit_utterance(svc, "remove habit gym")
    assert "'gym' and its marks" in out["message"]
    assert svc._find("gym") is None
    assert any(a[1] == "habit_deleted" for a in audit.entries)


def test_persistence_across_restart(tmp_path):
    db = str(tmp_path / "zara_habits.db")
    clock = FakeClock()
    svc, _ = _svc(clock, db_path=db)
    svc.create("gym", "day")
    handle_habit_utterance(svc, "mark gym done")
    svc2, _ = _svc(clock, db_path=db)
    stats = svc2._streak_stats(svc2._find("gym"))
    assert stats["n"] == 1 and stats["current"] == 1
    assert svc2.list_rows()[0]["name"] == "gym"


def test_invalid_names_cadence_honest():
    svc, _ = _svc()
    for bad in ("", "x"):
        try:
            svc.create(bad, "day")
            raise AssertionError(f"accepted name {bad!r}")
        except HabitValueError:
            pass
    out = handle_habit_utterance(svc, "add habit m every "
                                 "someday-maybe")
    assert "refused" in out
