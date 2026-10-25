"""P5 feature-hunt ⑫ (2026-10-23) — free-time reporting.

"when am I free tomorrow?" reports the gaps (>=15 min) inside
08:00–22:00 LOCAL after merging every event overlapping the day
(DTEND honored; an all-day event blocks the day; a DTSTART-only
event blocks 2 hours and the answer SAYS so). "am I free tomorrow
at 3?" answers the point check busy/free, naming the blocking
event. Heavier planning ("next week", dates) stays with the hosted
parser.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core.calendar_flow import CalendarFlow


def _z_on(dt_aware):
    return dt_aware.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _local_aware(target_date, hh, mm=0):
    naive = datetime(target_date.year, target_date.month,
                     target_date.day, hh, mm)
    return naive.astimezone()


class TestFreeSlots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.flow = CalendarFlow(audit=lambda *a, **k: None)
        self.tomorrow = (datetime.now().astimezone()
                         + timedelta(days=1)).date()

    def _ask(self, ics, utterance):
        old = os.environ.get("ZARA_ICS_PATH")
        path = os.path.join(self.tmp, f"f{abs(hash(utterance))%997}.ics")
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

    def test_gaps_between_events(self):
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:a@x\r\nSUMMARY:Morning thing\r\n"
               f"DTSTART:{_z_on(_local_aware(self.tomorrow, 9))}\r\n"
               f"DTEND:{_z_on(_local_aware(self.tomorrow, 10))}\r\n"
               "END:VEVENT\r\n"
               "BEGIN:VEVENT\r\nUID:b@x\r\nSUMMARY:Lunch block\r\n"
               f"DTSTART:{_z_on(_local_aware(self.tomorrow, 12))}\r\n"
               f"DTEND:{_z_on(_local_aware(self.tomorrow, 13, 30))}\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "when am i free tomorrow?")
        msg = out["message"]
        self.assertTrue(out["executed"])
        self.assertIn("Free tomorrow:", msg, msg)
        # morning gap before 09:00 and the 13:30 after-lunch stretch
        self.assertIn("08:00–09:00", msg, msg)
        self.assertIn("13:30–22:00", msg, msg)

    def test_point_check_busy_names_the_event(self):
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:p@x\r\nSUMMARY:Physio session\r\n"
               f"DTSTART:{_z_on(_local_aware(self.tomorrow, 14))}\r\n"
               f"DTEND:{_z_on(_local_aware(self.tomorrow, 15))}\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "am i free tomorrow at 2:30pm")
        msg = out["message"]
        self.assertIn("Busy", msg, msg)
        self.assertIn("Physio session", msg, msg)
        self.assertIn("14:00–15:00", msg, msg)

    def test_point_check_free(self):
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:p@x\r\nSUMMARY:Physio session\r\n"
               f"DTSTART:{_z_on(_local_aware(self.tomorrow, 14))}\r\n"
               f"DTEND:{_z_on(_local_aware(self.tomorrow, 15))}\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "am i free tomorrow at 4pm")
        self.assertIn("You are free tomorrow at 16:00",
                      out["message"], out["message"])

    def test_no_end_event_counts_two_hours_and_says_so(self):
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:n@x\r\nSUMMARY:Mystery block\r\n"
               f"DTSTART:{_z_on(_local_aware(self.tomorrow, 10))}\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "when am i free tomorrow?")
        msg = out["message"]
        self.assertIn("08:00–10:00", msg, msg)  # block starts 10:00
        self.assertIn("12:00–22:00", msg, msg)  # block ends 10:00+2h
        self.assertIn("without an end time", msg, msg)

    def test_allday_blocks_day(self):
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:al@x\r\nSUMMARY:Offsite\r\n"
               f"DTSTART;VALUE=DATE:{self.tomorrow.strftime('%Y%m%d')}"
               "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "when am i free tomorrow?")
        self.assertIn("Not free any time tomorrow",
                      out["message"], out["message"])

    def test_empty_calendar_free_all_day(self):
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:x@x\r\nSUMMARY:Later\r\n"
               f"DTSTART:{_z_on(_local_aware(self.tomorrow, 12))}\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "am i free today")
        self.assertIn("You are free all day today",
                      out["message"], out["message"])

    def test_heavier_planning_stays_hosted(self):
        out = self._ask("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                        "when am i free next week")
        self.assertTrue(out["executed"])
        self.assertIn("single named day", out["message"])

    def test_overlapping_events_merge(self):
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VEVENT\r\nUID:a@x\r\nSUMMARY:One\r\n"
               f"DTSTART:{_z_on(_local_aware(self.tomorrow, 9))}\r\n"
               f"DTEND:{_z_on(_local_aware(self.tomorrow, 10, 30))}\r\n"
               "END:VEVENT\r\n"
               "BEGIN:VEVENT\r\nUID:b@x\r\nSUMMARY:Two\r\n"
               f"DTSTART:{_z_on(_local_aware(self.tomorrow, 10))}\r\n"
               f"DTEND:{_z_on(_local_aware(self.tomorrow, 11))}\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, "when am i free tomorrow?")
        msg = out["message"]
        self.assertIn("11:00–22:00", msg, msg)


if __name__ == "__main__":
    unittest.main()
