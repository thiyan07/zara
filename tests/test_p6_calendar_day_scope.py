"""P5 feature-hunt ⑨ (2026-10-23) — day-scoped calendar answers.

"what's on my calendar tomorrow" must answer about TOMORROW (focused
scope), not bury tomorrow's items in a 7-day sweep. A named day
(today / tomorrow / a weekday, "next" forward-only) filters items by
their LOCAL calendar date, stretches the sweep horizon to reach the
named day, swaps the honest empty wording ("Nothing on the calendar
tomorrow."), and prefixes " Calendar for <day>: ". Unscoped asks
("what's next?") keep the exact 7-day sweep wording.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core.calendar_flow import CalendarFlow
from tests.test_p4_calendar_weather import _iso_z

WEEK_IDX = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}


def _target_for(day_word: str):
    """Mirror the flow's resolver: the local date a dayWord names."""
    now = datetime.now().astimezone()
    if day_word == "today":
        return now.date()
    if day_word == "tomorrow":
        return now.date() + timedelta(days=1)
    add = (WEEK_IDX[day_word] - now.weekday()) % 7 or 7
    return (now + timedelta(days=add)).date()


def _z_on(target_date, hh=10, mm=0):
    d = datetime.combine(
        target_date, datetime.min.time(),
        tzinfo=timezone.utc) + timedelta(hours=hh)
    return _iso_z(d + timedelta(minutes=mm))


class TestDayScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.flow = CalendarFlow(audit=lambda *a, **k: None)

    def _ask(self, ics: str, utterance: str) -> str:
        """Run the flow against one temp calendar; return its message."""
        old = os.environ.get("ZARA_ICS_PATH")
        path = os.path.join(self.tmp, f"c{abs(hash(utterance))}.ics")
        with open(path, "w") as f:
            f.write(ics)
        os.environ["ZARA_ICS_PATH"] = path
        try:
            out = self.flow.turn(utterance, who="user")
        finally:
            if old is None:
                os.environ.pop("ZARA_ICS_PATH")
            else:
                os.environ["ZARA_ICS_PATH"] = old
        self.assertTrue(out["executed"], out)
        return out["message"]

    def test_tomorrow_scope_filters_to_that_day(self):
        tomorrow = _target_for("tomorrow")
        later = tomorrow + timedelta(days=3)
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\nUID:a@x\r\n"
            f"DTSTART:{_z_on(tomorrow, 10)}\r\n"
            f"DTEND:{_z_on(tomorrow, 11)}\r\n"
            "SUMMARY:Journal club\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:b@x\r\n"
            f"DTSTART:{_z_on(later, 9)}\r\n"
            "SUMMARY:Three days out\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n")
        msg = self._ask(ics, "what's on my calendar tomorrow?")
        self.assertIn("Calendar for tomorrow:", msg, msg)
        self.assertIn("Journal club", msg, msg)
        self.assertNotIn("Three days out", msg, msg)
        # Scoped items carry stamp + title only — the head owns the
        # day; a UTC-calendar day label would lie in IST (+5:30).
        self.assertIn("10:00: Journal club", msg, msg)
        self.assertNotIn("today 10:00", msg, msg)

    def test_today_scope_keeps_in_progress_line(self):
        soon = datetime.now(timezone.utc) + timedelta(minutes=30)
        # Near-midnight guard (batch-15 pattern): the fixture promises an
        # event that is on LOCAL today; if now+30min would cross the
        # local midnight line, the forward-only today-scope honestly
        # says "nothing" — a clock artifact, not a scope regression.
        if soon.astimezone().date() != datetime.now().astimezone().date():
            self.skipTest("no late-today window left before midnight")
        later = _target_for("tomorrow")
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\nUID:s@x\r\n"
            f"DTSTART:{_iso_z(soon)}\r\n"
            f"DTEND:{_iso_z(soon + timedelta(minutes=30))}\r\n"
            "SUMMARY:Sprint review\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:t@x\r\n"
            f"DTSTART:{_z_on(later, 15)}\r\n"
            "SUMMARY:Not today\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n")
        msg = self._ask(ics, "what's on my calendar today?")
        self.assertIn("Calendar for today:", msg, msg)
        self.assertIn("Sprint review", msg, msg)
        self.assertNotIn("Not today", msg, msg)

    def test_weekday_scope_reaches_the_named_day(self):
        friday = _target_for("friday")
        other = friday - timedelta(days=1)
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\nUID:f@x\r\n"
            f"DTSTART:{_z_on(friday, 14)}\r\n"
            "SUMMARY:Friday forum\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:o@x\r\n"
            f"DTSTART:{_z_on(other, 14)}\r\n"
            "SUMMARY:Not friday\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n")
        msg = self._ask(ics, "what's on my calendar friday")
        low = msg.lower()
        self.assertIn("friday forum", low, msg)
        self.assertNotIn("not friday", low, msg)

    def test_empty_scope_is_honest_about_the_day(self):
        later = _target_for("tomorrow") + timedelta(days=2)
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\nUID:x@x\r\n"
            f"DTSTART:{_z_on(later, 8)}\r\n"
            "SUMMARY:Two days out\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n")
        msg = self._ask(ics, "what's on my calendar tomorrow?")
        self.assertIn("Nothing on the calendar tomorrow", msg, msg)
        self.assertNotIn("Two days out", msg, msg)

    def test_rrule_occurrences_land_in_scope(self):
        # A DAILY series whose 2nd occurrence lands tomorrow.
        base = (datetime.now(timezone.utc)
                + timedelta(days=2 - (_target_for("tomorrow")
                                     - datetime.now().astimezone()
                                     .date()).days))
        # base = tonight; occurrence +1 day == tomorrow
        base = datetime.now(timezone.utc).replace(
            hour=21, minute=0, second=0, microsecond=0)
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\nUID:r@x\r\n"
            f"DTSTART:{_iso_z(base)}\r\n"
            f"DTEND:{_iso_z(base + timedelta(minutes=30))}\r\n"
            "SUMMARY:Daily digest\r\nRRULE:FREQ=DAILY;COUNT=4\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n")
        msg = self._ask(ics, "what's on my calendar tomorrow?")
        self.assertIn("Calendar for tomorrow:", msg, msg)
        self.assertIn("Daily digest", msg, msg)

    def test_unscoped_sweep_wording_unchanged(self):
        later = _target_for("tomorrow") + timedelta(days=2)
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\nUID:x@x\r\n"
            f"DTSTART:{_z_on(later, 8)}\r\n"
            "SUMMARY:Two days out\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n")
        msg = self._ask(ics, "what's next?")
        self.assertIn("Calendar, next 7 days:", msg, msg)
        self.assertIn("Two days out", msg, msg)

    def test_wants_turn_covers_bare_scoped_asks(self):
        self.assertTrue(self.flow.wants_turn("calendar tomorrow"))
        self.assertTrue(self.flow.wants_turn("calendar friday"))
        self.assertTrue(self.flow.wants_turn(
            "what's on my calendar tomorrow"))
        self.assertTrue(self.flow.wants_turn("what's next?"))


if __name__ == "__main__":
    unittest.main()
