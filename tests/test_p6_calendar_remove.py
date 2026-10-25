"""P5 feature-hunt ⑧ (2026-10-23) — calendar REMOVE with approval.

Contract pins: laptop-side tool (parity), never auto-approved,
selector honesty (uid OR quoted title; ambiguity refuses with a
candidate list — nothing guessed), repeating parents remove the
whole series and SAY so, atomic tmp+rename write, re-parse
verification, byte preservation of untouched content (CRLF too),
and the quoted remove matcher in the chat path.
"""
import os
import tempfile
import unittest

from core.device_tools import DEVICE_TOOL_DEFS
from core.models import PermissionLevel, RiskLevel

FIXTURE = """BEGIN:VCALENDAR\r
BEGIN:VEVENT\r
UID:d1@x\r
DTSTART:20261023T140000Z\r
DTEND:20261023T150000Z\r
SUMMARY:Dentist\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:d2@x\r
DTSTART:20261030T140000Z\r
DTEND:20261030T150000Z\r
SUMMARY:Dentist\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:p1@x\r
DTSTART:20261024T090000Z\r
DTEND:20261024T091500Z\r
SUMMARY:Physio\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:rp1@x\r
DTSTART:20261023T080000Z\r
DTEND:20261023T083000Z\r
SUMMARY:Daily stand-up\r
RRULE:FREQ=DAILY;COUNT=5\r
END:VEVENT\r
END:VCALENDAR\r
"""


def _ctx_ical(handler):
    """Decorate tests that need ZARA_ICS_PATH pointed at the fixture."""
    def run(self):
        path = os.path.join(self.tmp, "lab.ics")
        with open(path, "w", newline="") as f:
            f.write(FIXTURE)
        old = os.environ.get("ZARA_ICS_PATH")
        os.environ["ZARA_ICS_PATH"] = path
        try:
            return handler(self, path)
        finally:
            if old is None:
                os.environ.pop("ZARA_ICS_PATH")
            else:
                os.environ["ZARA_ICS_PATH"] = old
    return run


class TestCalendarRemoveTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _count(self, path):
        from core.calendar_flow import parse_ics
        return len(parse_ics(path)["events"])

    def test_registry_contract(self):
        got = {d.name: d for d in DEVICE_TOOL_DEFS}
        d = got["calendar.event.remove"]
        self.assertEqual(d.permission, PermissionLevel.CONFIRM)
        self.assertEqual(d.risk, RiskLevel.CONFIRM)
        self.assertTrue(d.never_auto_approve,
                        "deletes operator data — never auto-approve")
        self.assertEqual(d.supported_devices, ["linux"])
        from device.linux.tools_linux import LINUX_TOOLS
        assert d.name in {x.name for x, _h in LINUX_TOOLS}, \
            "core claims linux — the agent must implement it (parity)"

    @_ctx_ical
    def test_remove_by_title_with_start_pin(self, path):
        from core.builtin_tools import calendar_event_remove
        r = calendar_event_remove(
            {"summary": "Dentist", "dtstart": "20261023T140000Z"}, {})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["verified"], r)
        self.assertEqual(r["events_total"], 3)
        self.assertEqual(self._count(path), 3)
        raw = open(path).read()
        self.assertNotIn("UID:d1@x", raw)
        self.assertIn("UID:d2@x", raw)  # the other Dentist stays

    @_ctx_ical
    def test_ambiguous_title_refuses_with_candidates(self, path):
        from core.builtin_tools import calendar_event_remove
        r = calendar_event_remove({"summary": "Dentist"}, {})
        self.assertFalse(r["ok"], r)
        self.assertTrue(r.get("ambiguous"), r)
        self.assertIn("2 events match", r["refusal"], r)
        self.assertIn("20261023T140000Z", r["refusal"], r)
        self.assertEqual(self._count(path), 4)  # untouched

    @_ctx_ical
    def test_unique_title_without_start_removes(self, path):
        from core.builtin_tools import calendar_event_remove
        r = calendar_event_remove({"summary": "Physio"}, {})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._count(path), 3)
        raw = open(path).read()
        self.assertNotIn("UID:p1@x", raw)
        self.assertNotIn("SUMMARY:Physio", raw)

    @_ctx_ical
    def test_absent_event_refuses(self, path):
        from core.builtin_tools import calendar_event_remove
        r = calendar_event_remove({"summary": "No such thing"}, {})
        self.assertFalse(r["ok"], r)
        self.assertIn("no event matches", r["refusal"], r)
        self.assertEqual(self._count(path), 4)

    @_ctx_ical
    def test_never_deletes_an_unnamed_thing(self, path):
        from core.builtin_tools import calendar_event_remove
        r = calendar_event_remove({}, {})
        self.assertFalse(r["ok"], r)
        self.assertIn("never delete an unnamed", r["refusal"], r)
        self.assertEqual(self._count(path), 4)

    @_ctx_ical
    def test_repeating_parent_removes_whole_series_and_says_so(
            self, path):
        from core.builtin_tools import calendar_event_remove
        r = calendar_event_remove(
            {"summary": "Daily stand-up",
             "dtstart": "20261023T080000Z"}, {})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r.get("repeating"), r)
        self.assertIn("whole series", r["detail"], r)
        self.assertEqual(self._count(path), 3)
        raw = open(path)
        raw2 = raw.read()
        self.assertNotIn("RRULE", raw2)
        self.assertNotIn("UID:rp1@x", raw2)

    @_ctx_ical
    def test_remove_by_uid_only(self, path):
        from core.builtin_tools import calendar_event_remove
        r = calendar_event_remove({"uid": "p1@x"}, {})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["verified"], r)
        raw = open(path).read()
        self.assertNotIn("UID:p1@x", raw)
        self.assertIn("SUMMARY:Dentist", raw)

    @_ctx_ical
    def test_untouched_blocks_survive_byte_exact_crlf(self, path):
        from core.builtin_tools import calendar_event_remove
        before = open(path, newline="").read()
        r = calendar_event_remove(
            {"summary": "Dentist", "dtstart": "20261030T140000Z"}, {})
        self.assertTrue(r["ok"], r)
        after = open(path, newline="").read()
        removed = "\r\n".join(
            ["BEGIN:VEVENT", "UID:d2@x", "DTSTART:20261030T140000Z",
             "DTEND:20261030T150000Z", "SUMMARY:Dentist",
             "END:VEVENT", ""])
        # The surviving file is byte-equal to the fixture minus the
        # removed 6-line block (CRLF endings included).
        self.assertEqual(after,
                         before.replace(removed, "", 1))


# ---- P5 ⑧ chat path: the quoted remove matcher -----------------------
from datetime import datetime, timedelta, timezone  # noqa: E402

from core.intent import LocalIntentParser, ParserContext  # noqa: E402

CAL_REM = "calendar.event.remove"
CAL_SCHEMA = {
    "type": "object", "required": [],
    "properties": {"uid": {"type": "string"},
                   "summary": {"type": "string"},
                   "dtstart": {"type": "string"},
                   "confirm": {"type": "boolean"}}}


def _ctx():
    return ParserContext(
        known_capabilities={CAL_REM}, capability_schemas={CAL_REM:
                                                         CAL_SCHEMA},
        devices=[])


def _expect_local(day: datetime, hh: int, mm: int = 0) -> str:
    # TRUE UTC storage contract (P5 ⑯ fix): the matcher converts the
    # quoted local clock to a real UTC instant rather than stamping
    # local digits with a Z.
    local = day.replace(hour=hh, minute=mm, second=0,
                        microsecond=0).astimezone()
    return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class TestRemoveMatcher(unittest.TestCase):
    def test_quoted_with_time_pins_exact_start(self):
        now = datetime.now().astimezone()
        p = LocalIntentParser().parse(
            'cancel "Dentist tomorrow 2pm" from my calendar', _ctx())
        self.assertEqual(p.capability, CAL_REM)
        self.assertEqual(p.parameters["summary"], "Dentist")
        self.assertEqual(p.parameters["dtstart"],
                         _expect_local(now + timedelta(days=1), 14))
        self.assertNotIn("dtend", p.parameters)

    def test_quoted_without_time_is_title_only(self):
        p = LocalIntentParser().parse(
            'remove "Physio" from my calendar', _ctx())
        self.assertEqual(p.capability, CAL_REM)
        self.assertEqual(p.parameters["summary"], "Physio")
        self.assertNotIn("dtstart", p.parameters,
                         "a bare title must NOT invent a start")

    def test_quoted_with_day_date_pins(self):
        now = datetime.now().astimezone()
        p = LocalIntentParser().parse(
            'delete "Dentist tomorrow" from my calendar', _ctx())
        self.assertEqual(p.capability, CAL_REM)
        day = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0).astimezone()
        self.assertEqual(p.parameters["summary"], "Dentist")
        self.assertEqual(p.parameters["dtstart"],
                         day.strftime("%Y%m%d"))

    def test_unquoted_asks_for_the_quote(self):
        p = LocalIntentParser().parse(
            "cancel my dentist tomorrow 2pm from my calendar", _ctx())
        self.assertTrue(p.needs_clarification)
        self.assertIn("quote", p.clarification_reason.lower())

    def test_absent_capability_falls_through(self):
        ctx = ParserContext(known_capabilities=set(),
                            capability_schemas={}, devices=[])
        p = LocalIntentParser().parse(
            'cancel "Physio" from my calendar', ctx)
        self.assertNotEqual(p.capability, CAL_REM)


if __name__ == "__main__":
    unittest.main()
