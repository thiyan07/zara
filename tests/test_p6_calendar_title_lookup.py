"""P5 feature-hunt ⑩ (2026-10-23) — title lookup in the calendar read
flow: "when is my dentist appointment?" answers the EARLIEST future
match (with local-tz stamps), counts the rest, walks RRULE
occurrences inside the window, and honestly reports absence. Narrow
routing by design: quoted titles, "next <X>", or noun-tailed shapes
route here; anything with day words ("when is my dentist tomorrow")
or a generic nounless ask ("when is my flight to chennai") stays
with the hosted parser."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core.calendar_flow import CalendarFlow


def _z_on(dt):
    return dt.strftime("%Y%m%dT%H%M%SZ")


class TestTitleLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.flow = CalendarFlow(audit=lambda *a, **k: None)

    def _ask(self, ics, utterance):
        old = os.environ.get("ZARA_ICS_PATH")
        path = os.path.join(self.tmp, f"q{abs(hash(utterance))%997}.ics")
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

    def _ics(self, extra=""):
        fut = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
            hour=9, minute=0, second=0, microsecond=0)
        fut2 = (datetime.now(timezone.utc) + timedelta(days=3)).replace(
            hour=14, minute=30, second=0, microsecond=0)
        return ("BEGIN:VCALENDAR\r\n"
                "BEGIN:VEVENT\r\nUID:d1@x\r\n"
                f"SUMMARY:Dentist cleaning\r\n"
                f"DTSTART:{_z_on(fut)}\r\n"
                f"DTEND:{_z_on(fut + timedelta(minutes=45))}\r\n"
                "END:VEVENT\r\n"
                "BEGIN:VEVENT\r\nUID:d2@x\r\n"
                f"SUMMARY:Dental hygiene talk\r\n"
                f"DTSTART:{_z_on(fut2)}\r\n"
                "END:VEVENT\r\n" + extra + "END:VCALENDAR\r\n")

    def test_earliest_match_with_noun_tail(self):
        out = self._ask(self._ics(), "when is my dentist appointment?")
        msg = out["message"]
        self.assertTrue(out["executed"])
        self.assertIn('Next "dentist"', msg, msg)
        # local-tz stamp: earliest future match, sun/others by clock
        self.assertIn("Dentist cleaning", msg, msg)
        self.assertNotIn("Dental hygiene talk", msg, msg)

    def test_substring_matches_only_true_titles(self):
        out = self._ask(self._ics(), "when is my dental meeting?")
        msg = out["message"]
        # "Dentist" does NOT contain "dental" — substring honesty:
        # exactly the one matching event, no invented siblings.
        self.assertIn("Dental hygiene talk", msg, msg)
        self.assertNotIn("Dentist cleaning", msg, msg)
        self.assertNotIn("later", msg, msg)

    def test_rrule_occurrence_answered(self):
        base = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(
            minute=0, second=0, microsecond=0)
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:r@x\r\n"
               "SUMMARY:Daily stand-up\r\n"
               f"DTSTART:{_z_on(base)}\r\n"
               f"DTEND:{_z_on(base + timedelta(minutes=30))}\r\n"
               "RRULE:FREQ=DAILY;COUNT=5\r\nEND:VEVENT\r\n"
               "END:VCALENDAR\r\n")
        out = self._ask(ics, "when is my stand-up meeting?")
        self.assertIn('Next "stand-up"', out["message"])
        self.assertIn("Daily stand-up", out["message"])

    def test_absence_is_honest(self):
        out = self._ask(self._ics(), "when is my astro session?")
        self.assertIn('No upcoming "astro"', out["message"])
        self.assertIn("next 7 days", out["message"])

    def test_quoted_title(self):
        fut = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=11, minute=0, second=0, microsecond=0)
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:q@x\r\n"
               f"SUMMARY:Dental trivia night\r\n"
               f"DTSTART:{_z_on(fut)}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        out = self._ask(ics, 'when is my "trivia" thing?')
        self.assertIn("Dental trivia night", out["message"])

    def test_day_words_stay_with_the_sweep(self):
        out = self._ask(self._ics(), "when is my dentist tomorrow")
        self.assertIsNone(out, "day word must not hijack the lookup")

    def test_generic_nounless_asks_stay_hosted(self):
        out = self._ask(self._ics(), "when is my flight to chennai")
        self.assertIsNone(out, "this test currently expects the "
                               "hosted parser to own generic asks")


if __name__ == "__main__":
    unittest.main()
