"""Stage 31-P4 (2026-10-22) — .ics calendar + free-endpoint weather."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from core.app import build_stack

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Stand-up with the crew
DTSTART:{next_z}
DTEND:{end_z}
END:VEVENT
BEGIN:VEVENT
SUMMARY:Deep work block
DTSTART:{today_local}
DTEND:{today_local_end}
END:VEVENT
BEGIN:VEVENT
SUMMARY:Conclusion week lunch
DTSTART;TZID=Europe/Lisbon:{naive_tz}
DTEND;TZID=Europe/Lisbon:{naive_tz_end}
END:VEVENT
END:VCALENDAR
"""

ICS_RECUR = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Daily stand-up
DTSTART:{base_z}
DTEND:{end_z}
RRULE:FREQ=DAILY;COUNT=4
END:VEVENT
BEGIN:VEVENT
SUMMARY:Anniversary ritual
DTSTART:{yearly_z}
RRULE:FREQ=YEARLY
END:VEVENT
END:VCALENDAR
"""


def _iso_z(d):
    return d.strftime("%Y%m%dT%H%M%SZ")


def write_ics(tmpdir, name="cal.ics"):
    now = datetime.now(timezone.utc)
    nxt = now + timedelta(hours=2)
    local = datetime.now().replace(tzinfo=None) + timedelta(hours=4)
    path = os.path.join(tmpdir, name)
    open(path, "w").write(ICS.format(
        next_z=_iso_z(nxt), end_z=_iso_z(nxt + timedelta(minutes=30)),
        today_local=local.strftime("%Y%m%dT%H%M%S"),
        today_local_end=(local + timedelta(hours=1)).strftime(
            "%Y%m%dT%H%M%S"),
        naive_tz=local.strftime("%Y%m%dT%H%M%S"),
        naive_tz_end=(local + timedelta(hours=1)).strftime(
            "%Y%m%dT%H%M%S")))
    return path


class TestCalendar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        stack = build_stack()
        self.svc = stack["intent"]
        self.audit = stack["audit"]

    def test_reports_today_and_unresolved_tz_without_rrule_note(self):
        path = write_ics(self.tmp)
        os.environ["ZARA_ICS_PATH"] = path
        try:
            out = self.svc.handle_utterance("what's next?")
            self.assertTrue(out["executed"])
            msg = out["message"]
            self.assertIn("Stand-up with the crew", msg, msg)
            self.assertIn("Deep work block", msg)
            self.assertIn("timezone unresolved", msg)
            # P5-b7: no repeating events in this fixture → no RRULE
            # honesty note (it appears ONLY for unexpanded rules).
            self.assertNotIn("RRULE", msg, msg)
        finally:
            os.environ.pop("ZARA_ICS_PATH")

    def test_plain_recurring_events_expand_in_window(self):
        now = datetime.now(timezone.utc)
        base = now + timedelta(hours=2)
        path = os.path.join(self.tmp, "recur.ics")
        open(path, "w").write(ICS_RECUR.format(
            base_z=_iso_z(base), end_z=_iso_z(base + timedelta(minutes=30)),
            yearly_z=_iso_z(base)))
        os.environ["ZARA_ICS_PATH"] = path
        try:
            out = self.svc.handle_utterance("what's next?")
            self.assertTrue(out["executed"])
            msg = out["message"]
            # DAILY;COUNT=4 → exactly 4 stand-up occurrences named in
            # the answer (the flow caps the WHOLE list at 12 lines, so
            # 4 must all be visible).
            self.assertEqual(msg.count("Daily stand-up"), 4, msg)
            # Unsupported YEARLY rule → the event still shows ONCE at
            # its base occurrence plus the per-event honesty note.
            self.assertIn("Anniversary ritual", msg, msg)
            self.assertIn("1 repeating event(s)", msg, msg)
        finally:
            os.environ.pop("ZARA_ICS_PATH")

    def test_unreadable_file_reported_not_hidden(self):
        bad = os.path.join(self.tmp, "missing.ics")
        os.environ["ZARA_ICS_PATH"] = bad
        try:
            out = self.svc.handle_utterance("my calendar today")
            self.assertTrue(out["executed"])
            self.assertIn("unreadable", out["message"], out)
        finally:
            os.environ.pop("ZARA_ICS_PATH")

    def test_no_config_is_an_honest_ask(self):
        os.environ.pop("ZARA_ICS_PATH", None)
        out = self.svc.handle_utterance("what's on my calendar")
        self.assertFalse(out["executed"])
        self.assertIn("ZARA_ICS_PATH", out["message"])


WEATHER_FIXTURE = {
    "current": {"temperature_2m": 21.3, "weather_code": 2,
                "wind_speed_10m": 9.2, "time": "2026-10-22T15:00"},
    "hourly": {"time": [], "precipitation_probability": []},
    "timezone": "Europe/Lisbon",
}
GEO_FIXTURE = {"results": [{"name": "Lisbon", "latitude": 38.72,
                            "longitude": -9.14, "country": "Portugal",
                            "admin1": "Lisbon"}]}
# daily block for tomorrow = index 1 of the 2-day fetch
_TOMORROW_DATE = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
_TODAY_DATE = datetime.now().strftime("%Y-%m-%d")
TOMORROW_FIXTURE = {
    "current": {"temperature_2m": 21.3, "weather_code": 2,
                "wind_speed_10m": 9.2, "time": _TODAY_DATE + "T15:00"},
    "hourly": {"time": [], "precipitation_probability": []},
    "daily": {"time": [_TODAY_DATE, _TOMORROW_DATE],
              "weather_code": [2, 61],
              "temperature_2m_max": [21.3, 18.4],
              "temperature_2m_min": [15.1, 13.2],
              "precipitation_probability_max": [10, 75]},
    "timezone": "Europe/Lisbon",
}


class TestWeather(unittest.TestCase):
    def setUp(self):
        stack = build_stack()
        self.svc = stack["intent"]
        self.audit = stack["audit"]

    def test_city_geocode_names_the_result_and_labels_source(self):
        os.environ.pop("ZARA_WEATHER_LAT", None)
        with mock.patch("core.weather_flow._fetch_json",
                        side_effect=[GEO_FIXTURE, WEATHER_FIXTURE]):
            out = self.svc.handle_utterance("what's the weather in Lisbon")
        self.assertTrue(out["executed"], out)
        msg = out["message"]
        self.assertIn("Lisbon", msg)
        self.assertIn("Open-Meteo", msg)
        self.assertIn("21.3", msg)

    def test_unknown_city_is_honest_geo_miss(self):
        with mock.patch("core.weather_flow._fetch_json",
                        return_value={"results": []}):
            out = self.svc.handle_utterance("weather in Nowhereland")
        self.assertFalse(out["executed"])
        self.assertIn("couldn't find", out["message"])

    def test_offline_reports_failure_not_fake_weather(self):
        os.environ["ZARA_WEATHER_LAT"] = "38.72"
        os.environ["ZARA_WEATHER_LON"] = "-9.14"
        with mock.patch("core.weather_flow._fetch_json",
                        side_effect=OSError("offline")):
            out = self.svc.handle_utterance("what's the weather")
        self.assertFalse(out["executed"])
        self.assertIn("couldn't reach", out["message"], out["message"])
        os.environ.pop("ZARA_WEATHER_LAT")
        os.environ.pop("ZARA_WEATHER_LON")

    def test_no_location_is_an_honest_ask(self):
        os.environ.pop("ZARA_WEATHER_LAT", None)
        os.environ.pop("ZARA_WEATHER_LON", None)
        out = self.svc.handle_utterance("what's the weather")
        self.assertFalse(out["executed"])
        self.assertIn("Where?", out["message"])

    # ---- P5 feature-hunt ③ (2026-10-23): the answered day matches the
    # asked day ----

    def test_rain_tomorrow_verdict_leads_with_likelihood(self):
        os.environ["ZARA_WEATHER_LAT"] = "38.72"
        os.environ["ZARA_WEATHER_LON"] = "-9.14"
        with mock.patch("core.weather_flow._fetch_json",
                        return_value=TOMORROW_FIXTURE):
            out = self.svc.handle_utterance("will it rain tomorrow")
        self.assertTrue(out["executed"], out)
        msg = out["message"]
        self.assertIn("Tomorrow", msg)
        self.assertIn("rain chance up to 75%", msg)
        self.assertIn("Open-Meteo", msg)
        # The daily block's min/max, not the current temp.
        self.assertIn("18.4", msg)

    def test_rain_tomorrow_low_probability_honest_declines(self):
        os.environ["ZARA_WEATHER_LAT"] = "38.72"
        os.environ["ZARA_WEATHER_LON"] = "-9.14"
        fx = dict(TOMORROW_FIXTURE)
        fx["daily"] = dict(TOMORROW_FIXTURE["daily"])
        fx["daily"]["precipitation_probability_max"] = [5, 5]
        with mock.patch("core.weather_flow._fetch_json",
                        return_value=fx):
            out = self.svc.handle_utterance("will it rain tomorrow")
        self.assertTrue(out["executed"])
        self.assertIn("Probably not", out["message"])

    def test_city_with_tomorrow_geocodes_and_answers_for_tomorrow(self):
        with mock.patch("core.weather_flow._fetch_json",
                        side_effect=[GEO_FIXTURE, TOMORROW_FIXTURE]):
            out = self.svc.handle_utterance(
                "what's the weather in Lisbon tomorrow")
        self.assertTrue(out["executed"], out)
        msg = out["message"]
        self.assertIn("Lisbon", msg)
        self.assertIn(_TOMORROW_DATE, msg)
        self.assertIn("precip chance up to 75%", msg)

    def test_missing_daily_block_is_honest_not_fabricated(self):
        os.environ["ZARA_WEATHER_LAT"] = "38.72"
        os.environ["ZARA_WEATHER_LON"] = "-9.14"
        stub = dict(TOMORROW_FIXTURE)
        stub.pop("daily")
        try:
            with mock.patch("core.weather_flow._fetch_json",
                            return_value=stub):
                out = self.svc.handle_utterance("weather tomorrow")
        finally:
            os.environ.pop("ZARA_WEATHER_LAT")
            os.environ.pop("ZARA_WEATHER_LON")
        self.assertFalse(out["executed"])
        self.assertIn("couldn't read tomorrow", out["message"])


# ---------- P5-b7 (2026-10-23): bounded RRULE read-side expansion ----

class TestExpandRRule(unittest.TestCase):
    """Pure-unit math on FIXED dates — no clock dependence."""

    BASE = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)  # Friday

    def _hz(self, days=7):
        return self.BASE + timedelta(days=days)

    def test_daily_count_bounded(self):
        from core.calendar_flow import expand_rrule
        r = expand_rrule(self.BASE, "FREQ=DAILY;COUNT=3", self._hz())
        self.assertEqual([o.date() for o in r["occurrences"]],
                         [self.BASE.date(), self.BASE.date()
                          + timedelta(days=1),
                          self.BASE.date() + timedelta(days=2)])

    def test_daily_fills_window_exactly(self):
        from core.calendar_flow import expand_rrule
        r = expand_rrule(self.BASE, "FREQ=DAILY", self._hz8()) \
            if False else expand_rrule(self.BASE, "FREQ=DAILY", self._hz())
        self.assertEqual(len(r["occurrences"]), 8)
        self.assertTrue(r["supported"])

    def _hz8(self):
        return self.BASE + timedelta(days=7)

    def test_weekly_byday_sorted_and_in_window(self):
        from core.calendar_flow import expand_rrule
        r = expand_rrule(self.BASE,
                         "FREQ=WEEKLY;BYDAY=MO,WE,FR", self._hz())
        got = [(o.weekday(), o.time()) for o in r["occurrences"]]
        # base week's FR (== window start, accepted) + next week's
        # MO/WE/FR, horizon = base + 7d inclusive
        self.assertEqual([w for w, _ in got], [4, 0, 2, 4])
        self.assertEqual(
            [(o.date() - self.BASE.date()).days for o in
             r["occurrences"]], [0, 3, 5, 7])
        for _, t in got:
            self.assertEqual(t, self.BASE.time())

    def test_weekly_interval_walks_fortnights(self):
        from core.calendar_flow import expand_rrule
        r = expand_rrule(self.BASE, "FREQ=WEEKLY;INTERVAL=2",
                         self.BASE + timedelta(days=22), self.BASE)
        days = [(o.date() - self.BASE.date()).days for o in
                r["occurrences"]]
        self.assertEqual(days, [0, 14])
        self.assertTrue(r["supported"])

    def test_monthly_short_month_skipped_honestly(self):
        from core.calendar_flow import expand_rrule
        base = self.BASE.replace(day=31)  # Oct 31 → Nov has no 31st
        r = expand_rrule(base, "FREQ=MONTHLY",
                         base + timedelta(days=62))
        months = [(o.year, o.month) for o in r["occurrences"]]
        self.assertEqual(months, [(2026, 10), (2026, 12)])
        self.assertTrue(r["supported"])

    def test_until_stops_the_walk(self):
        from core.calendar_flow import expand_rrule
        r = expand_rrule(self.BASE,
                         "FREQ=DAILY;UNTIL=20261026T235900Z", self._hz8())
        self.assertEqual(len(r["occurrences"]), 4)
        self.assertTrue(r["supported"])

    def test_unsupported_forms_stay_honest(self):
        from core.calendar_flow import expand_rrule
        for rr in ("FREQ=YEARLY", "FREQ=MONTHLY;BYDAY=2MO",
                   "FREQ=DAILY;BYMONTH=3", "FREQ=DAILY;INTERVAL=0",
                   "FREQ=DAILY;COUNT=abc", "FREQ=DAILY;UNTIL=xx",
                   "", "garbage"):
            r = expand_rrule(self.BASE, rr, self._hz())
            self.assertFalse(r["supported"], rr)
            self.assertEqual(r["occurrences"], [], rr)

    def test_old_rule_jumps_to_the_window_without_budget_burn(self):
        """P5-b7 design decision: a daily rule with no COUNT created
        500 days ago must NOT burn the candidate budget stepping
        through its past — the walk JUMP-STARTS to the window
        arithmetically and still yields the in-window occurrences."""
        from core.calendar_flow import expand_rrule
        old = self.BASE - timedelta(days=500)
        r = expand_rrule(old, "FREQ=DAILY",
                         old + timedelta(days=7), old)
        self.assertTrue(r["supported"])
        self.assertEqual(len(r["occurrences"]), 8)
        # window at NOW: the jump lands INSIDE the window — the past
        # cost zero budget and the answer is the honest future
        r2 = expand_rrule(old, "FREQ=DAILY", self._hz(),
                          self.BASE - timedelta(seconds=1))
        self.assertTrue(r2["supported"])
        self.assertEqual(len(r2["occurrences"]), 8)
