"""P5 feature-hunt ⑮ (2026-10-23) — the morning brief.

One deterministic READ-ONLY turn stitching the three first-thing
facts: today's calendar (CalendarFlow's today scope, reused — not
re-implemented), today's weather (WeatherFlow, message verbatim),
laptop health (same numbers the system.health answer reports).
Honesty: per-section named degradation; narrow anchor routing (no
device asks swallowed); local-day header.
"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core.calendar_flow import CalendarFlow


def _z_on(target_date, hh=10, mm=0):
    naive = datetime(target_date.year, target_date.month,
                     target_date.day, hh, mm)
    return naive.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _z_later_today(minutes_ahead=90, hh=None, mm=0):
    """A local-stamped event later today (the today-scope is
    forward-only, so past-stamped fixtures report honest empties)."""
    t = datetime.now().astimezone() + timedelta(minutes=minutes_ahead)
    if hh is not None:
        t = t.replace(hour=hh, minute=mm)
    if t.date() != datetime.now().astimezone().date():
        return None  # caller skips (near-midnight fixture)
    return t.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _mk_cal(events):
    parts = ["BEGIN:VCALENDAR\r\n"]
    for row in events:
        uid, summ, dt = row[:3]
        parts.append("BEGIN:VEVENT\r\n")
        parts.append(f"UID:{uid}\r\nSUMMARY:{summ}\r\n")
        if dt:
            parts.append(f"DTSTART:{dt}\r\n")
        if len(row) > 3 and row[3]:
            parts.append(f"DTEND:{row[3]}\r\n")
        parts.append("END:VEVENT\r\n")
    parts.append("END:VCALENDAR\r\n")
    return "".join(parts)


class _StubReminders:
    """ReminderService stand-in: same `upcoming` rows the real service
    returns (_summary keys); rows carry a local-day due_epoch."""

    def __init__(self, rows):
        self._rows = rows

    def upcoming(self, now=None):
        return list(self._rows)



class _NoNetWeather:
    """A weather flow stand-in that can only fail honestly (no env,
    no network): exercises the verbatim-passthrough degradation."""

    def wants_turn(self, clean):
        return "weather" in (clean or "")

    def turn(self, clean, who="user", requester_device=""):
        return {"message": "Where? Name a city ('weather in Lisbon')",
                "proposal": None, "resolution": None,
                "executed": False, "weather_flow": "need_location"}


class _BrokenWeather:
    def wants_turn(self, clean):
        return True

    def turn(self, clean, who="user", requester_device=""):
        raise OSError("network down")


class _BrokenCalendar:
    def wants_turn(self, clean):
        return True

    def turn(self, clean, who="user", requester_device=""):
        raise OSError("ics locked")


def _broken_health(inputs, ctx):
    raise OSError("proc missing")


def _ok_health(inputs, ctx):
    return {"ok": True, "disk_total_gb": 199.1, "disk_free_gb": 79.1,
            "disk_used_pct": 60.3, "ram_used_pct": 46.0,
            "ram_available_gb": 8.4, "ram_total_gb": 15.5,
            "load_1m": 0.5, "load_5m": 0.4, "load_15m": 0.3,
            "cpu_cores": 8, "uptime_days": 0.13, "detail": "ok"}


class TestMorningBrief(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cal = CalendarFlow(audit=lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _brief(self, ics, utterance="good morning",
               weather=None, health=None, reminders=None):
        from core.morning_brief import MorningBriefFlow
        path = os.path.join(self.tmp, "brief.ics")
        with open(path, "w", newline="") as f:
            f.write(ics)
        old = os.environ.get("ZARA_ICS_PATH")
        os.environ["ZARA_ICS_PATH"] = path
        try:
            flow = MorningBriefFlow(audit=lambda *a, **k: None,
                                    calendar_flow=self.cal,
                                    weather_flow=weather,
                                    health_fn=health or _ok_health,
                                    reminders=reminders)
            return flow.turn(utterance, who="user")
        finally:
            if old is None:
                os.environ.pop("ZARA_ICS_PATH")
            else:
                os.environ["ZARA_ICS_PATH"] = old

    # ---- routing: narrow anchors, no device-ask swallowing

    def test_wants_turn_narrow_anchors(self):
        from core.morning_brief import MorningBriefFlow
        flow = MorningBriefFlow(audit=lambda *a, **k: None)
        for u in ("good morning", "Good morning, zara?",
                  "morning brief", "daily briefing",
                  "brief me", "what's my day like",
                  "what's on my plate today?"):
            self.assertTrue(flow.wants_turn(u), u)
        for u in ("add test meeting to my calendar",
                  "cancel the demo from my calendar",
                  "weather in Lisbon", "send a text to joe",
                  "call mom", "when is my next test dive",
                  "good morning, add scotch to my calendar"):
            self.assertFalse(flow.wants_turn(u), u)

    # ---- the assembled brief

    def test_brief_with_event_today(self):
        z = _z_later_today()
        if z is None:
            self.skipTest("no late-today window left before midnight")
        ics = _mk_cal([("t@x", "Test recovery demo", z)])
        out = self._brief(ics, weather=_NoNetWeather())
        self.assertFalse(out["executed"])
        msg = out["message"]
        self.assertTrue(msg.startswith("Morning brief for "),
                        msg)
        # LOCAL-day header (host-TZ pin)
        self.assertIn(datetime.now().astimezone().strftime("%A, %b %d"),
                      msg, msg)
        # calendar section carries the real read
        self.assertIn("Test recovery demo", msg, msg)
        # weather section is honest about needing a location
        self.assertIn("Weather: Where? Name a city", msg, msg)
        # health section reuses the system.health numbers verbatim
        self.assertIn("disk 79.1 GB free of 199.1 GB", msg, msg)
        self.assertIn("RAM 46.0% used", msg, msg)
        self.assertIn("read-only brief", msg, msg)
        self.assertNotIn("..", msg, msg)

    def test_brief_calendar_prefix_stripped_for_today(self):
        # The calendar flow's scoped head ("Calendar for today: …")
        # must not double-print inside the already-labeled section.
        z = _z_later_today()
        if z is None:
            self.skipTest("no late-today window left before midnight")
        ics = _mk_cal([("t@x", "Zara status sync", z)])
        out = self._brief(ics, weather=_NoNetWeather())
        msg = out["message"]
        self.assertNotIn("Calendar for today:", msg, msg)
        self.assertIn("Zara status sync", msg, msg)

    def test_brief_empty_calendar_honest(self):
        out = self._brief(_mk_cal([]), weather=_NoNetWeather())
        self.assertIn("Nothing on the calendar today", out["message"],
                      out["message"])

    def test_brief_tomorrow_event_is_not_today(self):
        # a LOCAL-tomorrow event must NOT land in the today section.
        # Anchored to the live clock (batch-16 near-midnight lesson,
        # write side): the old literal 18:30Z fixture READS as today
        # 00:00 local on this host (IST), and its guard made the test
        # vacuous everywhere else — the first run it ever actually
        # asserted was the run that proved its premise inverted.
        tomorrow = (datetime.now().astimezone() + timedelta(days=1)
                    ).replace(hour=9, minute=30, second=0,
                              microsecond=0)
        z = tomorrow.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ics = _mk_cal([("t@x", "Nightly UTC", z)])
        out = self._brief(ics, weather=_NoNetWeather())
        self.assertNotIn("Nightly UTC", out["message"],
                         out["message"])

    def test_brief_degradation_each_section_named(self):
        out = self._brief(_mk_cal([]), weather=_BrokenWeather(),
                          health=_broken_health)
        msg = out["message"]
        self.assertIn("Weather: weather unavailable", msg, msg)
        self.assertIn("Laptop: health read unavailable", msg, msg)

    def test_brief_calendar_failure_named(self):
        from core.morning_brief import MorningBriefFlow
        flow = MorningBriefFlow(audit=lambda *a, **k: None,
                                calendar_flow=_BrokenCalendar(),
                                weather_flow=None,
                                health_fn=_ok_health)
        out = flow.turn("morning brief", who="user")
        msg = out["message"]
        self.assertIn("Calendar: calendar read unavailable: ics locked",
                      msg, msg)
        # no calendar flow + whatever weather degrades: never crash
        self.assertIn("Weather:", msg, msg)
        self.assertIn("Laptop: disk 79.1", msg, msg)

    def test_brief_never_mentions_calendar_complete(self):
        # identical fixture via the real talk fastpath composition:
        # the brief alone must answer (assembled from the composite
        # read path) without the calendar sweep masquerading apart.
        out = self._brief(_mk_cal([]), "what's my day like",
                          weather=_NoNetWeather())
        self.assertTrue(out.get("verified"))
        self.assertIn("Morning brief", out["message"])

    # ---- P6-b25 (2026-10-24): conflicts + reminders sections

    def test_brief_conflicts_detected_from_real_collection(self):
        # Two events 30 min apart with NO DTEND: the collection's
        # honest 2-hour default makes them overlap — the brief names
        # the pair AND the two-hour counted note from the same
        # calendar-flow machinery the chat surface uses.
        a = _z_later_today(minutes_ahead=10)
        b = _z_later_today(minutes_ahead=40)
        if not (a and b):
            self.skipTest("no late-today window left before midnight")
        ics = _mk_cal([("a@x", "Overlap one", a),
                       ("b@x", "Overlap two", b)])
        out = self._brief(ics, weather=_NoNetWeather())
        msg = out["message"]
        self.assertIn("Conflicts: 1 today —", msg, msg)
        self.assertIn('"Overlap one" ↔', msg, msg)
        self.assertIn("2 event(s) without an end time counted as "
                      "2 hours", msg, msg)

    def test_brief_conflicts_none_today(self):
        out = self._brief(_mk_cal([]), weather=_NoNetWeather())
        self.assertIn("Conflicts: none today.", out["message"],
                      out["message"])

    def test_brief_reminders_due_today_listed(self):
        # clock-safe fixture: anchor to TODAY'S LOCAL noon, never
        # now+offset (a run near midnight pushed +20 min into
        # tomorrow and made this test hour-of-day fragile — R45.1)
        from datetime import datetime as _dt
        local = _dt.now().astimezone()
        noon = local.replace(hour=12, minute=0, second=0,
                             microsecond=0).timestamp()
        stub = _StubReminders([
            {"id": "r1", "text": "call mom", "due": "evening",
             "due_epoch": noon + 3600, "status": "scheduled"},
            {"id": "r2", "text": "take trash", "due": "night",
             "due_epoch": noon + 7200, "status": "scheduled"},
            {"id": "r3", "text": "Much later chore", "due": "Friday",
             "due_epoch": noon + 3 * 86400, "status": "scheduled"},
        ])
        out = self._brief(_mk_cal([]), weather=_NoNetWeather(),
                          reminders=stub)
        msg = out["message"]
        self.assertIn("Reminders: 2 pending today", msg, msg)
        self.assertIn("'call mom'", msg, msg)
        self.assertNotIn("Much later chore", msg, msg)

    def test_brief_reminders_empty_and_unwired(self):
        empty = _StubReminders([])
        out = self._brief(_mk_cal([]), weather=_NoNetWeather(),
                          reminders=empty)
        self.assertIn("Reminders: none pending today.",
                      out["message"], out["message"])
        out2 = self._brief(_mk_cal([]), weather=_NoNetWeather())
        self.assertIn("Reminders: store not wired",
                      out2["message"], out2["message"])

    def test_brief_reminders_failure_named(self):
        import core.morning_brief as mb

        class _Boom:
            def upcoming(self, now=None):
                raise OSError("db locked")

        flow = mb.MorningBriefFlow(lambda *a, **k: None,
                                   calendar_flow=self.cal,
                                   weather_flow=None,
                                   health_fn=_ok_health,
                                   reminders=_Boom())
        out = flow.turn("brief me", who="user")
        self.assertIn("Reminders: read failed (db locked)",
                      out["message"], out["message"])
        # the rest of the brief still assembled
        self.assertIn("Laptop: disk 79.1", out["message"],
                      out["message"])


if __name__ == "__main__":
    unittest.main()
