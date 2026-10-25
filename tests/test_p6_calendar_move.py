"""P5 feature-hunt ⑯ (2026-10-23) — moving a calendar event.

"push my \"demo\" meeting to friday 3pm" is a WRITE to operator
data: same honesty class as removal. Pinned here:
- selection EXACTLY like remove (uid, or summary + optional
  dtstart/date pin; ambiguity refuses with candidates; absence
  refuses — nothing is ever guessed);
- time KINDS never mix (all-day stays all-day; a timed event keeps
  its clock); a move NEVER invents a default 09:00 start;
- duration preserved (DTEND shifts by the same delta) or pinned by
  new_end; a block without DTEND moves its start only and SAYS so;
- a repeating parent moves the whole series and says so;
- a same-title collision at the destination refuses proactively;
- atomic tmp+rename, byte preservation of untouched content, and
  re-parse verification under the SAME selector semantics (count
  unchanged, old start gone, new start exact);
- the chat shape: selector from the quote ONLY, destination from
  the tail after the quote (day+clock → new_start / day-only →
  new_date / clock-only → new_clock / all-day → date; neither →
  honest ask), the cancel/remove verbs keep their own matcher, and
  the tool is laptop-side with approval parity (CONFIRM,
  never-auto-approve).
"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core.builtin_tools import calendar_event_move, calendar_event_add
from core.device_tools import DEVICE_TOOL_DEFS
from core.models import PermissionLevel, RiskLevel
from core.intent import LocalIntentParser, ParserContext
from core.calendar_flow import CalendarFlow

CAL_MOVE = "calendar.event.move"
CAL_REM = "calendar.event.remove"


def _z_on(target_date, hh, mm=0):
    naive = datetime(target_date.year, target_date.month,
                     target_date.day, hh, mm)
    return naive.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _expect_local(day: datetime, hh: int, mm: int = 0) -> str:
    # TRUE UTC storage contract (P5 ⑯ fix)
    local = day.replace(hour=hh, minute=mm, second=0,
                        microsecond=0).astimezone()
    return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


ICS2 = ("BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\nUID:a@x\r\nSUMMARY:Dentist\r\n"
        "DTSTART:20261026T100000Z\r\nDTEND:20261026T110000Z\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:b@x\r\nSUMMARY:Standup\r\n"
        "DTSTART:20261026T140000Z\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:c@x\r\nSUMMARY:Offsite\r\n"
        "DTSTART;VALUE=DATE:20261101\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n")


class TestCalendarMoveTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "lab.ics")
        self.old = os.environ.get("ZARA_ICS_PATH")
        os.environ["ZARA_ICS_PATH"] = self.path

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ZARA_ICS_PATH")
        else:
            os.environ["ZARA_ICS_PATH"] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, body):
        with open(self.path, "w", newline="") as f:
            f.write(body)

    def _read(self):
        with open(self.path, encoding="utf-8", newline="") as f:
            return f.read()

    def test_registry_contract(self):
        d = {d.name: d for d in DEVICE_TOOL_DEFS}[CAL_MOVE]
        self.assertEqual(d.permission, PermissionLevel.CONFIRM)
        self.assertEqual(d.risk, RiskLevel.CONFIRM)
        self.assertTrue(d.never_auto_approve)
        self.assertEqual(d.supported_devices, ["linux"])
        self.assertEqual(d.required_capabilities, ["calendar.write"])
        from device.linux.tools_linux import LINUX_TOOLS
        self.assertIn(CAL_MOVE, {t.name for t, _ in LINUX_TOOLS})

    def test_uid_selector_shifts_start_and_keeps_duration(self):
        self._write(ICS2)
        out = calendar_event_move({"uid": "a@x",
                                   "new_start": "20261027T103000Z",
                                   "confirm": True}, {})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["from"], "20261026T100000Z")
        self.assertEqual(out["to"], "20261027T103000Z")
        raw = self._read()
        # duration preserved: +30 min start shift → +30 min end
        self.assertIn("DTSTART:20261027T103000Z\r\n"
                      "DTEND:20261027T113000Z", raw)
        # byte preservation outside the moved block: Standup untouched
        self.assertIn("DTSTART:20261026T140000Z", raw)
        self.assertEqual(raw.count("BEGIN:VEVENT"), 3)

    def test_summary_selector_with_pin(self):
        self._write(ICS2)
        out = calendar_event_move({"summary": "dentist",
                                   "dtstart": "20261026T100000Z",
                                   "new_start": "20261027T103000Z",
                                   "confirm": True}, {})
        self.assertTrue(out["ok"], out)
        self.assertIn("DTSTART:20261027T103000Z", self._read())

    def test_absence_ambiguity_and_unnamed_refuse(self):
        self._write(ICS2)
        out = calendar_event_move({"summary": "ghost",
                                   "new_start": "20261027T103000Z"}, {})
        self.assertFalse(out["ok"])
        self.assertIn("no event matches", out["refusal"])
        out = calendar_event_move({}, {})
        self.assertFalse(out["ok"])
        self.assertIn("never move an unnamed", out["refusal"])
        out = calendar_event_move({"summary": "Dentist"}, {})
        self.assertFalse(out["ok"])
        self.assertIn("where it should move", out["refusal"])
        out = calendar_event_move({"summary": "Dentist",
                                   "new_start": "99939930"}, {})
        self.assertFalse(out["ok"])
        self.assertIn("new_start must look like", out["refusal"])

    def test_kind_never_mixes(self):
        self._write(ICS2)
        out = calendar_event_move({"summary": "Offsite",
                                   "new_start": "20261102T090000Z",
                                   "confirm": True}, {})
        self.assertFalse(out["ok"])
        self.assertIn("all-day", out["refusal"])
        out = calendar_event_move({"summary": "Dentist",
                                   "new_start": "20261102",
                                   "confirm": True}, {})
        self.assertFalse(out["ok"])
        self.assertIn("drops its end time", out["refusal"])
        out = calendar_event_move({"summary": "Dentist",
                                   "new_date": "20261102",
                                   "confirm": True}, {})
        self.assertFalse(out["ok"])
        self.assertIn("drops its clock", out["refusal"])
        out = calendar_event_move({"summary": "Offsite",
                                   "new_clock": "14:30",
                                   "confirm": True}, {})
        self.assertFalse(out["ok"])
        self.assertIn("no clock time", out["refusal"])

    def test_clock_only_keeps_date_changes_time(self):
        # expected values are the RFC (UTC) forms: 16:30 LOCAL is the
        # event's stored calendar time shifted by +1h
        self._write(ICS2)
        out = calendar_event_move({"summary": "Dentist",
                                   "new_clock": "16:30",
                                   "confirm": True}, {})
        self.assertTrue(out["ok"], out)
        raw = self._read()
        self.assertIn("DTSTART:20261026T110000Z", raw, raw)
        self.assertIn("DTEND:20261026T120000Z", raw, raw)
        # the sibling keeps its own slot
        self.assertIn("DTSTART:20261026T140000Z", raw)

    def test_date_only_with_clock(self):
        # day+clock destination composed by the intent layer:
        # new_date (from the day word) + new_clock (from the clock);
        # expected values are the RFC UTC forms of the local clock
        self._write(ICS2)
        out = calendar_event_move({"summary": "Dentist",
                                   "new_date": "20261103",
                                   "new_clock": "07:30",
                                   "confirm": True}, {})
        self.assertTrue(out["ok"], out)
        raw = self._read()
        self.assertIn("DTSTART:20261103T020000Z", raw, raw)
        self.assertIn("DTEND:20261103T030000Z", raw, raw)

    def test_repeating_parent_series_and_sayso(self):
        self._write("BEGIN:VCALENDAR\r\n"
                    "BEGIN:VEVENT\r\nUID:r@x\r\nSUMMARY:Standup\r\n"
                    "DTSTART:20261026T100000Z\r\n"
                    "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
                    "END:VEVENT\r\nEND:VCALENDAR\r\n")
        out = calendar_event_move({"uid": "r@x",
                                   "new_start": "20261027T100000Z",
                                   "confirm": True}, {})
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["repeating"])
        self.assertIn("whole series", out["detail"])

    def test_collision_at_destination_refuses(self):
        self._write("BEGIN:VCALENDAR\r\n"
                    "BEGIN:VEVENT\r\nUID:a@x\r\nSUMMARY:Demo\r\n"
                    "DTSTART:20261026T100000Z\r\nEND:VEVENT\r\n"
                    "BEGIN:VEVENT\r\nUID:b@x\r\nSUMMARY:Demo\r\n"
                    "DTSTART:20261027T103000Z\r\nEND:VEVENT\r\n"
                    "END:VCALENDAR\r\n")
        out = calendar_event_move({"uid": "a@x",
                                   "new_start": "20261027T103000Z",
                                   "confirm": True}, {})
        self.assertFalse(out["ok"])
        self.assertIn("collide", out["refusal"])
        self.assertIn("DTSTART:20261026T100000Z", self._read())

    def test_all_day_to_all_day_and_pinned_end(self):
        self._write(ICS2)
        out = calendar_event_move({"summary": "Offsite",
                                   "new_start": "20261103",
                                   "new_end": "20261105",
                                   "confirm": True}, {})
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["all_day"])
        raw = self._read()
        self.assertIn("DTSTART;VALUE=DATE:20261103", raw)
        self.assertIn("DTEND;VALUE=DATE:20261105", raw)

    def test_tdend_absent_says_default_applies(self):
        self._write("BEGIN:VCALENDAR\r\n"
                    "BEGIN:VEVENT\r\nUID:b@x\r\nSUMMARY:Standup\r\n"
                    "DTSTART:20261026T140000Z\r\nEND:VEVENT\r\n"
                    "END:VCALENDAR\r\n")
        out = calendar_event_move({"uid": "b@x",
                                   "new_start": "20261027T140000Z",
                                   "confirm": True}, {})
        self.assertTrue(out["ok"], out)
        self.assertIn("no DTEND line", out["detail"])

    def test_tzid_param_preserved_value_replaced(self):
        self._write("BEGIN:VCALENDAR\r\n"
                    "BEGIN:VEVENT\r\nUID:t@x\r\n"
                    "SUMMARY:Tzid sync\r\n"
                    "DTSTART;TZID=Asia/Tokyo:20261026T180000\r\n"
                    "END:VEVENT\r\nEND:VCALENDAR\r\n")
        out = calendar_event_move({"uid": "t@x",
                                   "new_start": "20261027T100000Z",
                                   "confirm": True}, {})
        self.assertTrue(out["ok"], out)
        raw = self._read()
        self.assertIn("DTSTART;TZID=Asia/Tokyo:20261027T100000Z", raw)
        # the whole old value is gone (verify is converted-exact, not
        # substring-forgiving)
        self.assertNotIn("20261026T180000", raw)

    def test_roundtrip_with_real_add_and_read(self):
        # the composed chain through the REAL add, the move, and the
        # next-event read: at its NEW clock the event answers
        the_day = datetime.now().astimezone().date() + timedelta(days=5)
        self._write("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
        out = calendar_event_add({"summary": "Move demo",
                                  "dtstart": _z_on(the_day, 10, 0),
                                  "confirm": True}, {})
        self.assertTrue(out["verified"], out)
        out = calendar_event_move({"summary": "Move demo",
                                   "new_clock": "14:30",
                                   "confirm": True}, {})
        self.assertTrue(out["verified"], out)
        flow = CalendarFlow(audit=lambda *a, **k: None)
        r = flow.turn("when is my next move demo?", who="user")
        self.assertIn("Move demo", r["message"], r)
        self.assertIn("14:30", r["message"], r)


CAL_MOVE_SCHEMA = {
    "type": "object", "required": [],
    "properties": {"uid": {"type": "string"},
                   "summary": {"type": "string"},
                   "dtstart": {"type": "string"},
                   "new_start": {"type": "string"},
                   "new_date": {"type": "string"},
                   "new_clock": {"type": "string"},
                   "confirm": {"type": "boolean"}}}


def _ctx():
    return ParserContext(
        known_capabilities={CAL_MOVE, CAL_REM},
        capability_schemas={CAL_MOVE: CAL_MOVE_SCHEMA},
        devices=[])


class TestMoveMatcher(unittest.TestCase):
    def _friday(self, base=None):
        now = base or datetime.now().astimezone()
        add = (4 - now.weekday()) % 7 or 7
        return now + timedelta(days=add)

    def test_day_and_clock_full_pin(self):
        p = LocalIntentParser().parse(
            'push "Dentist" in my calendar to friday 3pm', _ctx())
        self.assertEqual(p.capability, CAL_MOVE, p)
        self.assertEqual(p.parameters["summary"], "Dentist")
        self.assertEqual(p.parameters["new_start"],
                         _expect_local(self._friday(), 15))
        self.assertNotIn("dtstart", p.parameters)

    def test_selector_reads_only_the_quote(self):
        # a day word in the destination must NOT pin the selector;
        # a day word inside the quote must
        p = LocalIntentParser().parse(
            'shift "Lab sync Tuesday" in my calendar to thursday 1pm',
            _ctx())
        # _cal_shape strips the in-title day word from the summary
        # (same contract as the add/remove shapes); the pin carries it
        self.assertEqual(p.parameters["summary"], "Lab sync")
        tue = datetime.now().astimezone()
        add_tue = (1 - tue.weekday()) % 7 or 7
        add_thu = (3 - tue.weekday()) % 7 or 7
        self.assertEqual(p.parameters["dtstart"],
                         (tue + timedelta(days=add_tue)).strftime(
                             "%Y%m%d"))
        self.assertEqual(p.parameters["new_start"],
                         _expect_local(tue + timedelta(days=add_thu),
                                       13))

    def test_clock_only_keeps_event_date(self):
        p = LocalIntentParser().parse(
            'push "Dentist" in my calendar to 15:30', _ctx())
        self.assertEqual(p.capability, CAL_MOVE)
        self.assertNotIn("new_start", p.parameters)
        self.assertEqual(p.parameters["new_clock"], "1530")

    def test_day_only_is_a_date_move(self):
        p = LocalIntentParser().parse(
            'move "Dentist" in my calendar to tomorrow', _ctx())
        self.assertEqual(p.capability, CAL_MOVE)
        self.assertEqual(p.parameters["new_date"],
                         (datetime.now().astimezone()
                          + timedelta(days=1)).strftime("%Y%m%d"))
        self.assertNotIn("dtstart", p.parameters,
                         "a title-only selector must NOT invent a pin")

    def test_no_destination_asks(self):
        p = LocalIntentParser().parse(
            'move "Dentist" in my calendar', _ctx())
        self.assertTrue(p.needs_clarification)
        self.assertIn("day and/or a clock", p.clarification_reason)

    def test_unquoted_asks_for_the_quote(self):
        p = LocalIntentParser().parse(
            "push my dentist in my calendar to friday 3pm", _ctx())
        self.assertTrue(p.needs_clarification)
        self.assertIn("quote", p.clarification_reason.lower())

    def test_absent_capability_falls_through(self):
        ctx = ParserContext(known_capabilities=set(),
                            capability_schemas={}, devices=[])
        p = LocalIntentParser().parse(
            'push "Dentist" in my calendar to friday 3pm', ctx)
        self.assertNotEqual(p.capability, CAL_MOVE)

    def test_remove_verb_keeps_remove_matcher(self):
        p = LocalIntentParser().parse(
            'cancel "Dentist" from my calendar', _ctx())
        self.assertEqual(p.capability, CAL_REM)


if __name__ == "__main__":
    unittest.main()
