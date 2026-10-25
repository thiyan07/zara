"""P5 feature-hunt ⑭ (2026-10-23) — weekly calendar digests.

"this week" / "last week" / "what did I do <this|last> week"
changes the report shape: a Monday-anchored ISO week window with
events grouped by their LOCAL day (counts in order; >6 collapse
with an honest "+N more"; all-day events labelled; RRULE
occurrences walk the window with its start as the jump-start
anchor). Nothing on the week → named empty (no sweep masquerade).
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core.calendar_flow import CalendarFlow


def _z_on(target_date, hh=10, mm=0):
    naive = datetime(target_date.year, target_date.month,
                     target_date.day, hh, mm)
    return naive.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class TestWeekDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.flow = CalendarFlow(audit=lambda *a, **k: None)
        now = datetime.now().astimezone()
        self.this_monday = now.date() - timedelta(days=now.weekday())
        self.last_monday = self.this_monday - timedelta(days=7)

    def _ask(self, ics, utterance):
        old = os.environ.get("ZARA_ICS_PATH")
        path = os.path.join(self.tmp, f"w{abs(hash(utterance))%997}.ics")
        with open(path, "w") as f:
            f.write(ics)
        os.environ["ZARA_ICS_PATH"] = path
        try:
            return self.flow.turn(utterance, who="user")
        finally:
            if old is None:
                os.environ.pop("ZARA_ICS_PATH")
            else:
                os.environ["ZARA_ICS_PATH"] = old

    def test_this_week_groups_by_day(self):
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:a@x\r\nSUMMARY:Monday recap\r\n"
               f"DTSTART:{_z_on(self.this_monday)}\r\n"
               "END:VEVENT\r\n"
               "BEGIN:VEVENT\r\nUID:w@x\r\nSUMMARY:Friday review\r\n"
               f"DTSTART:{_z_on(self.this_monday + timedelta(days=4))}"
               "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "what did I do this week?")
        self.assertTrue(out["executed"])
        msg = out["message"]
        self.assertIn("Calendar this week (2 event(s)):", msg, msg)
        self.assertIn("Monday recap", msg, msg)
        self.assertIn("Friday review", msg, msg)

    def test_last_week_is_empty_or_honest(self):
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:a@x\r\nSUMMARY:This week only\r\n"
               f"DTSTART:{_z_on(self.this_monday)}\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "what did I do last week?")
        self.assertIn("Nothing on the calendar last week",
                      out["message"], out["message"])

    def test_last_week_events_report_when_present(self):
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:o@x\r\nSUMMARY:Old team sync\r\n"
               f"DTSTART:{_z_on(self.last_monday, 14)}\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "what did I do last week?")
        msg = out["message"]
        self.assertIn("Old team sync", msg, msg)
        self.assertIn("last week (1 event(s))", msg, msg)

    def test_count_collapse_over_six(self):
        # eight events on ONE day: titles collapse to 6 + "+2 more"
        events = []
        for i in range(8):
            events.append(
                "BEGIN:VEVENT\r\nUID:c%d@x\r\nSUMMARY:Meeting %d\r\n"
                "DTSTART:%s\r\nEND:VEVENT\r\n"
                % (i, i, _z_on(self.this_monday, 8, (i * 13) % 55)))
        ics = ("BEGIN:VCALENDAR\r\n" + "".join(events)
               + "END:VCALENDAR\r\n")
        out = self._ask(ics, "what did I do this week?")
        msg = out["message"]
        self.assertIn("+2 more", msg, msg)
        self.assertIn("(8 event(s))", msg, msg)

    def test_rrule_occurrences_in_the_week(self):
        # daily series starting before the week: occurrences spread
        base = datetime.now(timezone.utc) - timedelta(days=2)
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:r@x\r\n"
               "SUMMARY:Daily digest\r\n"
               f"DTSTART:{base.strftime('%Y%m%dT090000Z')}\r\n"
               f"DTEND:{base.strftime('%Y%m%dT093000Z')}\r\n"
               "RRULE:FREQ=DAILY;COUNT=12\r\nEND:VEVENT\r\n"
               "END:VCALENDAR\r\n")
        out = self._ask(ics, "what did I do this week?")
        msg = out["message"]
        self.assertIn("Daily digest", msg, msg)
        self.assertIn("event(s)", msg, msg)

    def test_heavy_scopes_stay_out(self):
        out = self._ask("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                        "what did I do last month")
        self.assertIsNone(out, "month-asks stay with the hosted path")


if __name__ == "__main__":
    unittest.main()
