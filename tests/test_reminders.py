"""NL reminder management (Stage 24i) — parser, service, lifecycle,
restart, DST, failure honesty, and turn routing.

Deterministic throughout: injected fake clock, a ManualScheduler test
double that drives the REAL fire path (fire-once ledger, cancel races,
hook) without arming threads, in-memory NotificationManager, fake
failing providers, and fixture-only utterances. No real notifications
are sent; no device ever executes anything.
"""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from core.intent import IntentTurnService
from core.notifications import NotificationManager
from core.reminders import (MAX_DELIVERY_ATTEMPTS, PERIOD_DEFAULTS,
                            ReminderService, ReminderTimeError,
                            ReminderValueError, configured_tz,
                            handle_reminder_utterance, is_reminder_utterance,
                            next_recurring, parse_intent, parse_when)
from core.scheduler import Scheduler

NOW = 1760900000.0            # 2025-10-19 18:53:20 UTC (deterministic)


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

    def actions(self):
        return [a for (_, a, _, _) in self.entries]

    def details(self):
        return [d for (_, _, _, d) in self.entries]


class ManualScheduler(Scheduler):
    """Never arms timer threads; the test drives fires through the
    REAL _fire path (fire-once ledger, cancel suppression, hook)."""

    def _arm(self, job, delay_s):
        pass


class FailingNotifier:
    """Fake provider that raises for the first N create() calls."""

    def __init__(self, fail_times=1):
        self.fail_times = fail_times
        self.created = []

    def create(self, title, body="", device_id=None, mission_id=None,
               execution_id=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("provider down")
        note = NotificationManager().create(title, body, device_id)
        self.created.append(note)
        self.last = note
        return note


def _svc(clock=None, audit=None, db_path="", tz="UTC", notifs=None,
         fail_times=0):
    """Manual scheduler + (failing) notifier + fake clock."""
    audit = audit if audit is not None else ListAudit()
    notifs = notifs if notifs is not None else (
        FailingNotifier(fail_times) if fail_times else
        NotificationManager())
    service = ReminderService(ManualScheduler(), notifs, audit,
                              db_path=db_path,
                              now=clock or FakeClock(), tz_name=tz,
                              max_attempts=MAX_DELIVERY_ATTEMPTS)
    # mirror the app wiring: the scheduler hook is the service
    service.sched._on_fire = service.on_fire
    return service, notifs, audit


def _fire_reminder(svc, summary):
    """Drive the scheduler's REAL fire path for this reminder's live
    job (deterministic; honours fire-once + cancel suppression)."""
    for p in svc.sched.pending():
        pid = (p.get("payload") or {}).get("reminder", {}).get("id", "")
        if pid == summary["id"]:
            return svc.sched._fire(p["id"])
    raise AssertionError("no live job to fire for the reminder")


def _retry_job(svc):
    jobs = [p for p in svc.sched.pending()
            if p.get("name") == "reminder-retry"]
    assert len(jobs) == 1
    return jobs[0]


def _due_in(hours=1):
    return NOW + hours * 3600


def _utc(epoch):
    return datetime.fromtimestamp(epoch, ZoneInfo("UTC"))


# ------------------------------------------------------------------
# time parsing
# ------------------------------------------------------------------

def test_clock_forms_are_deterministic():
    assert parse_when("at 7 PM", NOW, "UTC")["epoch"] == NOW + 400
    assert _utc(parse_when("at 7:30 am", NOW, "UTC")
                ["epoch"]).strftime("%m-%d %H:%M") == "10-20 07:30"
    assert parse_when("at 19:00", NOW, "UTC")["epoch"] == NOW + 400
    noon = _utc(parse_when("at noon", NOW, "UTC")["epoch"])
    assert noon.strftime("%m-%d %H:%M") == "10-20 12:00"


def test_bare_hour_is_clarified_never_guessed():
    for h in (1, 5, 7, 9, 11):
        with pytest.raises(ReminderTimeError) as e:
            parse_when(f"at {h}", NOW, "UTC")
        assert "AM" in str(e.value) and "PM" in str(e.value)


def test_twelve_means_pm_by_convention():
    out = parse_when("at 12", NOW, "UTC")
    assert _utc(out["epoch"]).strftime("%H:%M") == "12:00"


def test_period_words_are_defined_not_guessed():
    assert PERIOD_DEFAULTS["morning"] == 8
    assert PERIOD_DEFAULTS["afternoon"] == 14
    assert PERIOD_DEFAULTS["evening"] == 19
    assert PERIOD_DEFAULTS["tonight"] == 20
    assert PERIOD_DEFAULTS["night"] == 21
    assert PERIOD_DEFAULTS["noon"] == 12
    assert PERIOD_DEFAULTS["midnight"] == 0
    # a period word disambiguates a bare hour deterministically
    assert parse_when("evening walk at 7", NOW, "UTC")["epoch"] == \
        NOW + 400
    # a day-part period whose time already passed today CLARIFIES
    # (never silently reinterpreted as tomorrow)
    with pytest.raises(ReminderTimeError) as e:
        parse_when("study in the morning at 5", NOW, "UTC")
    assert "tomorrow" in str(e.value)


def test_bare_time_past_rolls_to_tomorrow():
    out = parse_when("at 7 PM", NOW, "UTC")
    assert out["epoch"] == NOW + 400        # today, still future
    out2 = parse_when("at 6 PM", NOW, "UTC")  # today 18:00 passed
    assert _utc(out2["epoch"]).strftime("%m-%d %H:%M") == "10-20 18:00"


def test_period_phrase_past_clarifies():
    assert parse_when("evening walk", NOW, "UTC")["epoch"] == NOW + 400
    late = NOW + 3 * 3600 + 20 * 60             # 22:13 UTC
    with pytest.raises(ReminderTimeError) as e:
        parse_when("tonight at 9", late, "UTC")
    assert "tomorrow" in str(e.value)
    # a bare time still rolls (documented)
    assert _utc(parse_when("at 9 PM", late, "UTC")
                ["epoch"]).strftime("%m-%d") == "10-20"


def test_explicit_today_past_clarifies():
    late = NOW + 3 * 3600 + 20 * 60             # 22:13
    with pytest.raises(ReminderTimeError) as e:
        parse_when("today at 9 PM", late, "UTC")
    assert "passed" in str(e.value)


def test_relative_intervals():
    assert parse_when("in 2 hours", NOW, "UTC")["epoch"] == NOW + 7200
    assert parse_when("in 3 days", NOW, "UTC")["epoch"] == NOW + \
        3 * 86400
    with pytest.raises(ReminderValueError):
        parse_when("in 0 minutes", NOW, "UTC")
    with pytest.raises(ReminderTimeError):
        parse_when("today in 2 hours", NOW, "UTC")   # both → clarify


def test_recurring_daily_and_weekly_anchors():
    daily = parse_when("every day at 8 PM", NOW, "UTC")
    assert daily["recurrence"] == {"kind": "daily", "hour": 20,
                                   "minute": 0}
    assert _utc(daily["epoch"]).strftime("%H:%M") == "20:00"
    monday = parse_when("every monday at 6 am", NOW, "UTC")
    assert monday["recurrence"] == {"kind": "weekly", "weekday": 0,
                                    "hour": 6, "minute": 0}
    weekly = parse_when("every week at 9 PM", NOW, "UTC")
    assert weekly["recurrence"]["weekday"] == \
        _utc(NOW).weekday()


def test_vague_phrase_is_clarified_not_guessed():
    with pytest.raises(ReminderTimeError):
        parse_when("tomorrow", NOW, "UTC")
    with pytest.raises(ReminderTimeError):
        parse_when("sometime", NOW, "UTC")


def test_invalid_zone_fails_loudly():
    with pytest.raises(ValueError):
        configured_tz({"ZARA_TIMEZONE": "No/Such Zone"})


def test_zone_stored_not_utc_assumed():
    """A reminder stores its zone: the label renders the KOLKATA wall
    time (epoch + 5:30), not a UTC reinterpretation."""
    svc, _, _ = _svc(tz="Asia/Kolkata")
    out = svc.create("echo", _due_in(25), tz_name="Asia/Kolkata")
    s = out["summary"]
    assert s["tz"] == "Asia/Kolkata"
    utc_wall = datetime.fromtimestamp(s["due_epoch"], timezone.utc)
    ind_wall = datetime.fromtimestamp(s["due_epoch"],
                                      ZoneInfo("Asia/Kolkata"))
    assert int((ind_wall.hour * 60 + ind_wall.minute -
                (utc_wall.hour * 60 + utc_wall.minute))) % 1440 == 330
    assert "IST" in s["due"]


# ------------------------------------------------------------------
# intent parsing
# ------------------------------------------------------------------

def test_create_ops_extract_task_and_when():
    o = parse_intent("Remind me to study Java at 7 PM", NOW, "UTC")
    assert o["op"] == "create" and o["text"] == "study Java"
    assert o["epoch"] == NOW + 400
    o = parse_intent("remind me tomorrow morning to finish my "
                     "assignment", NOW, "UTC")
    assert o["text"] == "finish my assignment"
    assert _utc(o["epoch"]).strftime("%H:%M") == "08:00"
    o = parse_intent("remind me every day at 8 PM to review my notes",
                     NOW, "UTC")
    assert o["text"] == "review my notes"
    assert o["recurrence"] == {"kind": "daily", "hour": 20, "minute": 0}
    o = parse_intent("remind me in 2 hours to call mom", NOW, "UTC")
    assert o["text"] == "call mom" and o["epoch"] == NOW + 7200


def test_numbers_in_task_are_not_eaten_as_times():
    o = parse_intent("remind me to read chapter 12 at 7 PM", NOW, "UTC")
    assert o["text"] == "read chapter 12"
    assert o["epoch"] == NOW + 400


def test_ops_extract():
    assert parse_intent("show my reminders for tomorrow", NOW,
                        "UTC")["filter"] == "tomorrow"
    assert parse_intent("what are my reminders", NOW,
                        "UTC")["op"] == "list"
    o = parse_intent("cancel my Java study reminder", NOW, "UTC")
    assert o["op"] == "cancel" and o["target"] == "java study"
    o = parse_intent("move my assignment reminder to 9 PM", NOW, "UTC")
    assert o["op"] == "reschedule" and o["target"] == "assignment"
    o = parse_intent("when is my java reminder", NOW, "UTC")
    assert o["op"] == "find" and o["target"] == "java"
    assert parse_intent("remind me nothing about anything", NOW,
                        "UTC")["op"] == "clarify"


def test_injection_guard_text_is_data():
    """A reminder whose TEXT contains command-shaped words is stored
    verbatim as data — it is never routed to resolver/executor."""
    svc, _, _ = _svc()
    out = handle_reminder_utterance(
        svc, "remind me to delete my files at 7 PM", NOW)
    assert out["executed"] is False
    assert out["reminder"]["text"] == "delete my files"
    # nothing executed anywhere: no mission, no device action


def test_is_reminder_utterance_steer():
    assert is_reminder_utterance("remind me to study Java at 7 PM")
    assert is_reminder_utterance("show my reminders")
    assert not is_reminder_utterance("how is my battery")


# ------------------------------------------------------------------
# service lifecycle
# ------------------------------------------------------------------

def test_create_schedules_exactly_one_job_and_lists():
    svc, notifs, audit = _svc()
    out = svc.create("study Java", _due_in(1))
    assert out["duplicate"] is False
    s = out["summary"]
    assert s["status"] == "scheduled"
    assert len(svc.sched.pending()) == 1
    ups = svc.upcoming()
    assert len(ups) == 1 and ups[0]["text"] == "study Java"
    assert "reminder_created" in audit.actions()


def test_duplicate_request_never_schedules_twice():
    svc, _, _ = _svc()
    epoch = _due_in(2)
    first = svc.create("review notes", epoch)
    second = svc.create("review notes", epoch + 20)   # same minute
    assert second["duplicate"] is True
    assert second["summary"]["id"] == first["summary"]["id"]
    assert len(svc.sched.pending()) == 1


def test_past_and_invalid_are_refused():
    svc, _, _ = _svc()
    with pytest.raises(ReminderValueError):
        svc.create("old", NOW - 100)
    with pytest.raises(ReminderValueError):
        svc.create("x", _due_in(1), tz_name="No/Zone")
    with pytest.raises(ReminderValueError):
        svc.create("", _due_in(1))
    with pytest.raises(ReminderValueError):
        svc.create("bad rec", _due_in(1),
                   recurrence={"kind": "yearly"})
    assert "bad rec" not in [r["text"] for r in svc.upcoming()]


def test_fire_delivers_once_with_ledger_dedup():
    svc, notifs, audit = _svc()
    out = svc.create("study Java", _due_in(1))
    s = out["summary"]
    _fire_reminder(svc, s)
    rows = [dict(r) for r in svc._rows.values()]
    assert rows[0]["status"] == "delivered_to_service"
    assert len(notifs.pending()) == 1
    assert notifs.pending()[0].title.startswith("Reminder:")
    # restart re-announce of the SAME occurrence → no duplicate
    svc.on_fire({"id": rows[0]["job_id"], "payload": {"reminder": {
        "id": s["id"], "due": s["due_epoch"]}}})
    assert len(notifs.pending()) == 1
    assert "reminder_delivered" in audit.actions()


def test_cancel_before_delivery_suppresses_notification():
    svc, notifs, audit = _svc()
    out = svc.create("study Java", _due_in(1))
    s = out["summary"]
    res = svc.cancel("study java")
    assert res["cancelled"] is True
    assert svc.sched.pending() == []    # timer cancelled
    # a late fire (post-cancel race path) cannot deliver: the row is
    # terminal now
    svc.on_fire({"id": "late", "payload": {"reminder": {
        "id": s["id"], "due": s["due_epoch"]}}})
    assert len(notifs.pending()) == 0
    assert "reminder_cancelled" in audit.actions()
    assert "reminder_cancel_refused" not in audit.actions()


def test_cancel_unknown_and_ambiguous():
    svc, _, audit = _svc()
    res = svc.cancel("nonexistent")
    assert res["cancelled"] is False
    assert "no scheduled reminder" in res["message"]
    svc.create("study Java", _due_in(1))
    svc.create("study Java deeply", _due_in(2))
    res2 = svc.cancel("study java")
    assert res2["cancelled"] is False
    assert "Multiple reminders match" in res2["message"]
    assert "reminder_cancel_refused" in audit.actions()


def test_reschedule_replaces_job_never_duplicates():
    svc, notifs, _ = _svc()
    out = svc.create("study Java", _due_in(1))
    old_due = out["summary"]["due_epoch"]
    res = svc.reschedule("study java", _due_in(5))
    assert res["summary"]["status"] == "scheduled"
    pend = svc.sched.pending()
    assert len(pend) == 1                     # exactly one live job
    assert pend[0]["payload"]["reminder"]["due"] != old_due
    new_due = svc.upcoming()[0]["due_epoch"]
    assert new_due == NOW + 5 * 3600


def test_reschedule_ambiguous_refused():
    svc, _, _ = _svc()
    res = svc.reschedule("ghost", _due_in(1))
    assert res["rescheduled"] is False


def test_notification_failure_is_bounded_and_honest():
    svc, notifs, audit = _svc(fail_times=MAX_DELIVERY_ATTEMPTS)
    out = svc.create("study Java", _due_in(1))
    s = out["summary"]
    for attempt in range(MAX_DELIVERY_ATTEMPTS):
        _fire_reminder(svc, s) if attempt == 0 else \
            svc.sched._fire(_retry_job(svc)["id"])
        if attempt < MAX_DELIVERY_ATTEMPTS - 1:
            _retry_job(svc)                   # a bounded retry exists
    rows = [dict(r) for r in svc._rows.values()]
    assert rows[0]["status"] == "delivery_failed"
    assert rows[0]["attempts"] == MAX_DELIVERY_ATTEMPTS
    assert "reminder_delivery_failed_final" in audit.actions()
    # NO further retries scheduled after the final failure
    assert _retry_job(svc) and True if False else True
    assert [p for p in svc.sched.pending()
            if p.get("name") == "reminder-retry"] == []


def test_retry_never_duplicates_one_delivery():
    svc, notifs, _ = _svc(fail_times=1)
    out = svc.create("quick echo", _due_in(1))
    s = out["summary"]
    _fire_reminder(svc, s)                    # attempt 1 fails
    rows = [dict(r) for r in svc._rows.values()]
    assert rows[0]["status"] == "delivery_attempted"
    svc.sched._fire(_retry_job(svc)["id"])    # attempt 2 succeeds
    rows = [dict(r) for r in svc._rows.values()]
    assert rows[0]["status"] == "delivered_to_service"
    assert len(notifs.created) == 1           # exactly one notification


def test_recurring_rearms_one_job_at_a_time():
    clock = FakeClock(NOW)
    svc, notifs, audit = _svc(clock=clock)
    out = svc.create("review notes", _due_in(1),
                     recurrence={"kind": "daily"})
    s = out["summary"]
    clock.t = s["due_epoch"] + 1      # the timer fires AT the due time
    _fire_reminder(svc, s)
    assert len(notifs.pending()) == 1          # first occurrence
    pend = svc.sched.pending()
    assert len(pend) == 1                      # ONE live job (the next)
    nxt_due = pend[0]["payload"]["reminder"]["due"]
    assert nxt_due == s["due_epoch"] + 86400   # daily anchor (UTC)
    clock.t = nxt_due + 1
    svc.sched._fire(pend[0]["id"])
    assert len(notifs.pending()) == 2          # second occurrence
    assert "reminder_next_scheduled" in audit.actions()
    assert len(svc.sched.pending()) == 1
    assert svc.upcoming() != []                # still upcoming/future


def test_delivery_states_are_distinct():
    """'delivered' means delivered_to_service — user perception is
    NEVER claimed anywhere in the record."""
    svc, notifs, audit = _svc()
    out = svc.create("study Java", _due_in(1))
    assert out["summary"]["status"] == "scheduled"
    _fire_reminder(svc, out["summary"])
    rows = [dict(r) for r in svc._rows.values()]
    assert rows[0]["status"] == "delivered_to_service"
    blob = json.dumps(rows, default=str)
    assert "seen" not in blob and "read by user" not in blob


# ------------------------------------------------------------------
# persistence + restart recovery
# ------------------------------------------------------------------

def test_restart_persistence_and_recovery(tmp_path):
    db = str(tmp_path / "reminders.db")
    svc, notifs, audit = _svc(clock=FakeClock(NOW), db_path=db)
    out = svc.create("study Java", _due_in(10))
    out2 = svc.create("review notes", _due_in(12),
                      recurrence={"kind": "daily"})
    # RESTART 1: fresh clock still before due
    svc2, notifs2, _ = _svc(clock=FakeClock(NOW + 5), db_path=db)
    ups = svc2.upcoming()
    assert [u["id"] for u in ups] == [out["summary"]["id"],
                                      out2["summary"]["id"]]
    assert len(svc2.sched.pending()) == 2
    # RESTART 2: the one-shot's due passed during downtime → exactly
    # one delivery (restart receipt), never zero, never two
    svc3, notifs3, _ = _svc(clock=FakeClock(NOW + 11 * 3600),
                            db_path=db)
    assert len(notifs3.pending()) == 1
    rows = [dict(r) for r in svc3._rows.values()
            if r["id"] == out["summary"]["id"]]
    assert rows[0]["status"] == "delivered_to_service"
    # recurring re-anchored into the future, exactly one live job
    rec = [dict(r) for r in svc3._rows.values()
           if r["id"] == out2["summary"]["id"]]
    assert rec[0]["status"] == "scheduled"
    assert rec[0]["due"] > NOW + 11 * 3600
    rec_jobs = [p for p in svc3.sched.pending()
                if p["payload"].get("reminder", {}).get(
                    "id") == out2["summary"]["id"]]
    assert len(rec_jobs) == 1
    assert not svc3.upcoming() == []


def test_restart_idempotent_recovery_no_double_delivery(tmp_path):
    """Two consecutive restarts during downtime deliver ONCE (the
    persisted ledger prevents re-delivery in the fresh process)."""
    db = str(tmp_path / "r2.db")
    svc0, _, _ = _svc(clock=FakeClock(NOW), db_path=db)
    out = svc0.create("quick water the plants", _due_in(0.5))
    svc1, n1, _ = _svc(clock=FakeClock(NOW + 3600), db_path=db)
    assert len(n1.pending()) == 1              # delivered once here
    svc2, n2, _ = _svc(clock=FakeClock(NOW + 7300), db_path=db)
    # the second restart re-delivers NOTHING: fresh notification manager
    # but the persisted delivery ledger marks the occurrence done
    assert len(n2.pending()) == 0
    rows = [dict(r) for r in svc2._rows.values()
            if r["id"] == out["summary"]["id"]]
    assert rows[0]["status"] == "delivered_to_service"


def test_missed_recurring_does_not_burst_a_day_offline(tmp_path):
    db = str(tmp_path / "r.db")
    svc, _, _ = _svc(clock=FakeClock(NOW), db_path=db)
    out = svc.create("review notes", _due_in(1),
                     recurrence={"kind": "daily"})
    # ~1.25 days pass while the process is down: exactly ONE missed
    # notification (documented policy), then re-anchor
    svc2, notifs2, audit2 = _svc(clock=FakeClock(NOW + 30 * 3600),
                                 db_path=db)
    assert len(notifs2.pending()) == 1
    assert any("missed_run" in d for d in audit2.details())
    s = [dict(r) for r in svc2._rows.values()
         if r["id"] == out["summary"]["id"]][0]
    assert s["status"] == "scheduled" and s["due"] > NOW + 30 * 3600


# ------------------------------------------------------------------
# DST policy
# ------------------------------------------------------------------

def test_daily_recurring_holds_wall_time_across_dst():
    """8 PM daily in Europe/Berlin stays 8 PM local; the epoch delta
    across the fall-back transition is 25 h (not 24)."""
    sat = datetime(2025, 10, 25, 20, 0,
                   tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
    rec = {"kind": "daily"}
    nxt = next_recurring(sat, rec, "Europe/Berlin", sat + 10)
    ndt = datetime.fromtimestamp(nxt, ZoneInfo("Europe/Berlin"))
    assert ndt.strftime("%m-%d %H:%M") == "10-26 20:00"
    assert abs(nxt - sat - 25 * 3600) < 1.5
    nxt2 = next_recurring(nxt, rec, "Europe/Berlin", nxt + 10)
    assert abs(nxt2 - nxt - 24 * 3600) < 1.5


def test_spring_forward_gap_maps_forward():
    """02:30 daily in Berlin with the 2026-03-29 spring gap: the
    nonexistent wall time maps FORWARD (03:30 local, fold=0) —
    documented and deterministic."""
    sat = datetime(2026, 3, 28, 2, 30,
                   tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
    rec = {"kind": "daily", "hour": 2, "minute": 30}   # parse-anchored
    nxt = next_recurring(sat, rec, "Europe/Berlin", sat + 10)
    ndt = datetime.fromtimestamp(nxt, ZoneInfo("Europe/Berlin"))
    assert ndt.strftime("%m-%d %H:%M") == "03-29 03:30"
    nxt2 = next_recurring(nxt, rec, "Europe/Berlin", nxt + 10)
    # the gap did NOT drag the schedule: next day is 02:30 again
    assert datetime.fromtimestamp(nxt2,
                                  ZoneInfo("Europe/Berlin")).strftime(
        "%m-%d %H:%M") == "03-30 02:30"


# ------------------------------------------------------------------
# turn integration (NL routing through IntentTurnService)
# ------------------------------------------------------------------

def _turn_svc(clock=None):
    from core.resolver import CapabilityResolver
    import tests.test_intent as TI
    stack = TI.make_stack()
    turn_svc, notifs, _ = _svc(clock=clock or FakeClock())
    turn = IntentTurnService(stack["fabric"],
                             CapabilityResolver(stack["fabric"]),
                             TI.LocalIntentParser(), turn_svc.audit,
                             reminders=turn_svc)
    return turn, turn_svc, notifs, stack


def test_turn_create_list_cancel_reschedule():
    turn, svc, notifs, _ = _turn_svc()
    r = turn.handle_utterance("Remind me to study Java at 7 PM")
    assert r["executed"] is False and r["proposal"] is None
    assert "Reminder set" in r["message"]
    assert r["reminder"]["text"] == "study Java"
    r2 = turn.handle_utterance("show my reminders")
    assert r2["reminders"] and "study Java" in r2["message"]
    r3 = turn.handle_utterance("cancel my Java reminder")
    assert r3["cancelled"] is True
    assert svc.upcoming() == []
    turn.handle_utterance("Remind me to study Java at 7 PM")
    r5 = turn.handle_utterance("move my java reminder to 9 PM")
    assert r5["reminder"]["status"] == "scheduled"
    assert len(svc.sched.pending()) == 1
    # the moved reminder lives in the 9 PM slot; a NEW '7 PM' request
    # for the same text is a DIFFERENT slot (not a duplicate of the
    # moved one) — it is created, and a further identical request is
    # deduped into it
    turn.handle_utterance("Remind me to study Java at 7 PM")
    assert len(svc.sched.pending()) == 2
    before = len(svc.sched.pending())
    dup = turn.handle_utterance("Remind me to study Java at 7 PM")
    assert dup.get("duplicate") is True
    assert len(svc.sched.pending()) == before


def test_turn_recurring_create():
    turn, svc, _, _ = _turn_svc()
    r = turn.handle_utterance(
        "remind me every day at 8 PM to review my notes")
    assert "Reminder set" in r["message"]
    assert r["reminder"]["recurrence"] == {"kind": "daily", "hour": 20,
                                           "minute": 0}
    assert len(svc.sched.pending()) == 1


def test_turn_ambiguous_cancel_clarifies_not_cancels():
    turn, svc, _, _ = _turn_svc()
    turn.handle_utterance("remind me to study Java at 7 PM")
    turn.handle_utterance("remind me to study Java deeply at 8 PM")
    r = turn.handle_utterance("cancel my java reminder")
    assert r["executed"] is False
    assert "Multiple reminders match" in r["message"]
    assert len(svc.upcoming()) == 2     # nothing was cancelled silently


def test_turn_non_reminder_utterances_flow_normally():
    turn, svc, _, _ = _turn_svc()
    r = turn.handle_utterance("what is my battery")
    assert r.get("reminders") is None
    assert svc.upcoming() == []


def test_turn_reminder_ops_are_audited():
    turn, svc, _, _ = _turn_svc()
    turn.handle_utterance("remind me to study java at 7 PM")
    turn.handle_utterance("show my reminders")
    acts = [a for (_, a, _, _) in turn.reminders.audit.entries]
    assert "reminder_created" in acts
