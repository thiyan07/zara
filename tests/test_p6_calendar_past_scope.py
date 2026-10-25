"""P5 feature-hunt ⑪ (2026-10-23) — past-day calendar history.

"my calendar yesterday" / "calendar last friday" scope to a
HISTORICAL day; events are matched by OVERLAP with that day (start
inside it, or ran through it — DTEND honored, an all-day event
covers its whole day). "yesterday" and "last <weekday>" resolve
backward ("last friday" said ON a friday means a week ago). The
unscoped future sweep and future scopes are untouched.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core.calendar_flow import CalendarFlow

WEEK_IDX = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}


def _iso_z(d):
    return d.strftime("%Y%m%dT%H%M%SZ")


def _local_z(target_date, hh, mm=0):
    """A Z-stamp for a local (IST-host-agnostic) clock time on the
    given local date — the naive local time is converted to TRUE UTC
    (strftime on a local-aware dt would print the IST clock with a
    false Z suffix — the exact tz lie this batch's suite caught in
    another fixture)."""
    naive = datetime(target_date.year, target_date.month,
                     target_date.day, hh, mm)
    return _iso_z(naive.astimezone(timezone.utc))


class TestPastScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.flow = CalendarFlow(audit=lambda *a, **k: None)
        self.yesterday = datetime.now().astimezone().date() \
            - timedelta(days=1)
        self.tomorrow = self.yesterday + timedelta(days=2)

    def _ask(self, ics, utterance):
        old = os.environ.get("ZARA_ICS_PATH")
        path = os.path.join(self.tmp, f"p{abs(hash(utterance))%997}.ics")
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
        return out

    def test_yesterday_events_reported_tomorrow_not(self):
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:a@x\r\nSUMMARY:Morning review\r\n"
               f"DTSTART:{_local_z(self.yesterday, 9)}\r\n"
               f"DTEND:{_local_z(self.yesterday, 10)}\r\n"
               "END:VEVENT\r\n"
               "BEGIN:VEVENT\r\nUID:b@x\r\nSUMMARY:Evening sync\r\n"
               f"DTSTART:{_local_z(self.yesterday, 20)}\r\n"
               f"DTEND:{_local_z(self.yesterday, 21)}\r\n"
               "END:VEVENT\r\n"
               "BEGIN:VEVENT\r\nUID:c@x\r\nSUMMARY:Tomorrow thing\r\n"
               f"DTSTART:{_local_z(self.tomorrow, 12)}\r\n"
               "END:VEVENT\r\n"
               "END:VCALENDAR\r\n")
        out = self._ask(ics, "what was on my calendar yesterday?")
        self.assertTrue(out["executed"])
        msg = out["message"]
        self.assertIn("Calendar for yesterday:", msg, msg)
        self.assertIn("Morning review", msg, msg)
        self.assertIn("Evening sync", msg, msg)
        self.assertNotIn("Tomorrow thing", msg, msg)

    def test_overnight_overlap_counts(self):
        # started yesterday evening, ran past midnight into today
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:o@x\r\nSUMMARY:Long night monitor\r\n"
               f"DTSTART:{_local_z(self.yesterday, 23)}\r\n"
               f"DTEND:{_local_z(self.yesterday + timedelta(days=1), 2)}"
               "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "my calendar yesterday")
        self.assertIn("Long night monitor", out["message"],
                      out["message"])

    def test_last_weekday_resolves_backward(self):
        now = datetime.now().astimezone()
        idx_target = 0  # monday (ISO numbering: monday = 0)
        back = (now.weekday() - idx_target) % 7 or 7
        last_mon = (now - timedelta(days=back)).date()
        other = last_mon - timedelta(days=3)
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:m@x\r\nSUMMARY:Monday briefing\r\n"
               f"DTSTART:{_local_z(last_mon, 9)}\r\n"
               "END:VEVENT\r\n"
               "BEGIN:VEVENT\r\nUID:n@x\r\nSUMMARY:Other day event\r\n"
               f"DTSTART:{_local_z(other, 9)}\r\n"
               "END:VEVENT\r\n"
               "END:VCALENDAR\r\n")
        out = self._ask(ics, "what was on my calendar last monday?")
        msg = out["message"]
        self.assertIn("Calendar for last monday:", msg, msg)
        self.assertIn("Monday briefing", msg, msg)
        self.assertNotIn("Other day event", msg, msg)

    def test_rrule_occurrence_in_past_day(self):
        base = (datetime.now(timezone.utc) - timedelta(days=3)).replace(
            hour=9, minute=0, second=0, microsecond=0)
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:r@x\r\nSUMMARY:Daily stand-up\r\n"
               f"DTSTART:{_iso_z(base)}\r\n"
               f"DTEND:{_iso_z(base + timedelta(minutes=30))}\r\n"
               "RRULE:FREQ=DAILY;COUNT=10\r\nEND:VEVENT\r\n"
               "END:VCALENDAR\r\n")
        out = self._ask(ics, "my calendar yesterday")
        self.assertIn("Daily stand-up", out["message"],
                      out["message"])

    def test_empty_past_day_is_honest(self):
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:t@x\r\nSUMMARY:Future only\r\n"
               f"DTSTART:{_local_z(self.tomorrow, 12)}\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "my calendar yesterday")
        self.assertIn("Nothing on the calendar yesterday.",
                      out["message"], out["message"])

    def test_allday_past_event_covers_whole_day(self):
        # an all-day event on a day 4 days back, asked via "last
        # <weekday>" for THAT date is bulky — ask via yesterday only
        # if yesterday matches... instead pin: all-day event ON
        # yesterday itself.
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:al@x\r\nSUMMARY:All-day summit\r\n"
               f"DTSTART;VALUE=DATE:{self.yesterday.strftime('%Y%m%d')}"
               "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "my calendar yesterday")
        msg = out["message"]
        self.assertIn("All-day summit", msg, msg)
        self.assertIn("all-day", msg, msg)


if __name__ == "__main__":
    unittest.main()
