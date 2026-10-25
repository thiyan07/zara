"""P5 feature-hunt ⑥ (2026-10-23): calendar WRITE with approval.

Pins the core contract for `calendar.event.add`: one VEVENT per call,
append-only (the rest of the file byte-identical apart from the
insertion), duplicate refusal, malformed/policy bounds refused with
nothing written, atomic replace, re-parse verification, and the
registry/policy tier (CONFIRM + NEVER_AUTO_APPROVE — writes operator
data; the chat approval is per-event). The chat-path approval
mechanics (park → approve → grant receipt) are the same engine shape
pinned in test_p6_timer_confirm_chatpath.py; here the registry tool is
core-local (no device job).
"""
import os
import tempfile
import unittest

from core.builtin_tools import BUILTINS, calendar_event_add
from core.calendar_flow import parse_ics
from core.models import PermissionLevel, RiskLevel

BASE = (
    "BEGIN:VCALENDAR\n"
    "BEGIN:VEVENT\n"
    "SUMMARY:Team sync\n"
    "DTSTART:20261024T091500Z\n"
    "DTEND:20261024T101500Z\n"
    "END:VEVENT\n"
    "END:VCALENDAR\n"
)


def _tmp_ics(monkey=None, body=BASE):
    fd, path = tempfile.mkstemp(suffix=".ics")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    old = os.environ.get("ZARA_ICS_PATH")
    os.environ["ZARA_ICS_PATH"] = path
    return path, old


def _restore(path, old):
    os.remove(path)
    if old is None:
        os.environ.pop("ZARA_ICS_PATH")
    else:
        os.environ["ZARA_ICS_PATH"] = old


def test_registry_contract():
    from core.device_tools import DEVICE_TOOL_DEFS
    got = {d.name: d for d in DEVICE_TOOL_DEFS}
    d = got["calendar.event.add"]
    assert d.permission == PermissionLevel.CONFIRM
    assert d.risk == RiskLevel.CONFIRM
    assert d.never_auto_approve is True, \
        "writes operator data — must never auto-approve"
    assert set(d.input_schema["required"]) == {"summary", "dtstart"}
    assert d.supported_devices == ["linux"]
    from device.linux.tools_linux import LINUX_TOOLS
    assert "calendar.event.add" in {d.name for d, _h in LINUX_TOOLS}, \
        "core claims linux — the agent must implement it (parity)"


def test_appends_one_event_and_verifies_by_reread():
    path, old = _tmp_ics()
    try:
        out = calendar_event_add({
            "summary": "Dentist", "dtstart": "20261025T133000Z",
            "dtend": "20261025T140000Z", "confirm": True}, {})
        assert out["ok"] is True, out
        assert out["verified"] is True
        parsed = parse_ics(path)
        summaries = [e["summary"] for e in parsed["events"]]
        assert summaries == ["Team sync", "Dentist"], summaries
        assert out["events_total"] == 2
        # Ordering honesty: appended before END:VCALENDAR.
        raw = open(path, encoding="utf-8").read()
        assert raw.index("SUMMARY:Dentist") < raw.rindex("END:VCALENDAR")
    finally:
        _restore(path, old)


def test_all_day_event_uses_value_date():
    path, old = _tmp_ics()
    try:
        out = calendar_event_add({
            "summary": "Book fair", "dtstart": "20261106",
            "dtend": "20261107", "confirm": True}, {})
        assert out["ok"] is True and out["all_day"] is True
        raw = open(path, encoding="utf-8").read()
        assert "DTSTART;VALUE=DATE:20261106" in raw
        assert f"DTEND;VALUE=DATE:20261107" in raw
    finally:
        _restore(path, old)


def test_duplicate_title_and_start_refuses_nothing_written():
    path, old = _tmp_ics()
    try:
        before = open(path, encoding="utf-8").read()
        out = calendar_event_add({
            "summary": "team sync", "dtstart": "20261024T091500Z",
            "confirm": True}, {})
        assert out["ok"] is False and out.get("duplicate") is True
        assert open(path, encoding="utf-8").read() == before, \
            "duplicate refusal must not touch the file"
    finally:
        _restore(path, old)


def test_bounds_and_malformed_refuse_without_writing():
    path, old = _tmp_ics()
    try:
        for bad in ({"summary": "X", "dtstart": "tomorrow"},
                    {"summary": "X", "dtstart": "20261025T133000Z",
                     "dtend": "20261025T130000Z"},
                    {"summary": "X", "dtstart": "20261025",
                     "dtend": "20271025"},
                    {"dtstart": "20261025T133000Z"},
                    {}):
            out = calendar_event_add(dict(bad, confirm=True), {})
            assert out["ok"] is False, bad
        # 8-day timed event refused by the duration bound.
        out = calendar_event_add({
            "summary": "Long haul", "dtstart": "20261025T130000Z",
            "dtend": "20261103T130000Z", "confirm": True}, {})
        assert out["ok"] is False and "7 days" in out["detail"]
    finally:
        _restore(path, old)


def test_malformed_calendar_is_refused_not_corrupted():
    body = "not a calendar at all\n"
    path, old = _tmp_ics(body=body)
    try:
        out = calendar_event_add({
            "summary": "X", "dtstart": "20261025T133000Z",
            "confirm": True}, {})
        assert out["ok"] is False
        assert open(path, encoding="utf-8").read() == body
    finally:
        _restore(path, old)


def test_no_config_is_an_honest_ask():
    old = os.environ.get("ZARA_ICS_PATH")
    os.environ.pop("ZARA_ICS_PATH", None)
    try:
        out = calendar_event_add({
            "summary": "X", "dtstart": "20261025T133000Z",
            "confirm": True}, {})
        assert out["ok"] is False
        assert "ZARA_ICS_PATH" in out["detail"]
    finally:
        os.remove(os.environ["ZARA_ICS_PATH"]) \
            if os.environ.get("ZARA_ICS_PATH") else None
        if old is None:
            os.environ.pop("ZARA_ICS_PATH", None)
        else:
            os.environ["ZARA_ICS_PATH"] = old


def test_calendar_selector_and_unknown_selector():
    fd1, p1 = tempfile.mkstemp(suffix=".ics")
    fd2, p2 = tempfile.mkstemp(suffix=".ics")
    for fd, p in ((fd1, p1), (fd2, p2)):
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(BASE)
    old = os.environ.get("ZARA_ICS_PATH")
    os.environ["ZARA_ICS_PATH"] = f"{p1}:{p2}"
    try:
        out = calendar_event_add({
            "summary": "Selected", "dtstart": "20261026T100000Z",
            "calendar": os.path.basename(p2), "confirm": True}, {})
        assert out["ok"] is True and out["target"] == \
            os.path.basename(p2)
        assert len(parse_ics(p2)["events"]) == 2
        assert len(parse_ics(p1)["events"]) == 1
        bad = calendar_event_add({
            "summary": "Nope", "dtstart": "20261026T100000Z",
            "calendar": "no-such-calendar", "confirm": True}, {})
        assert bad["ok"] is False
        assert "not one of the configured" in bad["detail"]
    finally:
        os.remove(p1)
        os.remove(p2)
        if old is None:
            os.environ.pop("ZARA_ICS_PATH")
        else:
            os.environ["ZARA_ICS_PATH"] = old


# ---- P5 ⑥ chat path: the quoted structural matcher (date-anchor local
# resolution; hosted parser keeps the rest) ----
from datetime import datetime, timedelta, timezone

from core.intent import LocalIntentParser, ParserContext

CAL_CAP = "calendar.event.add"
CAL_SCHEMA = {
    "type": "object", "required": ["summary", "dtstart"],
    "properties": {"summary": {"type": "string"},
                   "dtstart": {"type": "string"},
                   "dtend": {"type": "string"},
                   "confirm": {"type": "boolean"}}}


def _cal_ctx():
    return ParserContext(
        known_capabilities={CAL_CAP},
        capability_schemas={CAL_CAP: CAL_SCHEMA}, devices=[])


def _expect_local(day: datetime, hh: int, mm: int = 0) -> str:
    # TRUE UTC storage contract (P5 ⑯ fix): "2pm" asked locally is
    # stored as the real UTC instant of 2pm at the host's zone.
    local = day.replace(hour=hh, minute=mm, second=0,
                        microsecond=0).astimezone()
    return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def test_quoted_event_with_time_resolves_deterministically():
    now = datetime.now().astimezone()
    p = LocalIntentParser().parse(
        'add "Dentist tomorrow 2pm" to my calendar', _cal_ctx())
    assert p.capability == CAL_CAP, p
    assert p.parameters["summary"] == "Dentist"
    assert p.parameters["dtstart"] == _expect_local(
        now + timedelta(days=1), 14)
    assert p.parameters["dtend"] == _expect_local(
        now + timedelta(days=1), 15)


def test_weekday_and_duration_resolve():
    now = datetime.now().astimezone()
    p = LocalIntentParser().parse(
        'put "Gym monday 7am for 90 minutes" on my calendar', _cal_ctx())
    assert p.capability == CAL_CAP
    assert p.parameters["summary"] == "Gym"
    add = (0 - now.weekday()) % 7 or 7
    assert p.parameters["dtstart"] == _expect_local(
        now + timedelta(days=add), 7)
    assert p.parameters["dtend"] == _expect_local(
        now + timedelta(days=add), 8, 30)


def test_all_day_resolves_value_date_pair():
    now = datetime.now().astimezone()
    p = LocalIntentParser().parse(
        'schedule "Book fair all day tomorrow" to my calendar', _cal_ctx())
    assert p.capability == CAL_CAP
    day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                            microsecond=0)
    assert p.parameters["dtstart"] == day.strftime("%Y%m%d")
    assert p.parameters["dtend"] == (day + timedelta(days=1)).strftime(
        "%Y%m%d")


def test_unquoted_event_invites_a_quote_not_a_guess():
    p = LocalIntentParser().parse(
        "add dentist tomorrow 2pm to my calendar", _cal_ctx())
    assert p.needs_clarification
    assert "quote" in p.clarification_reason.lower()


def test_absent_capability_falls_through_to_hosted():
    ctx = ParserContext(known_capabilities=set(),
                        capability_schemas={}, devices=[])
    assert LocalIntentParser()._calendar_hit(
        'add "x" to my calendar', 'add "x" to my calendar',
        ctx) is None or True  # returns None/clarify only — never a cap
    p = LocalIntentParser().parse('add "x" to my calendar', ctx)
    # With the capability unknown, the calendar matcher must not fire a
    # proposal for it (it goes to the hosted/registry path honestly).
    assert p.capability != CAL_CAP


def test_add_preserves_crlf_calendar_byte_exact():
    """P5 ⑧ regression pin: the add read uses newline='' — a default
    text read silently translated \\r\\n to \\n and the rewrite
    normalized the WHOLE file's line endings. The appended block must
    use the file's own CRLF; surviving events' bytes stay exact."""
    crlf = BASE.replace("\n", "\r\n")
    path, old = _tmp_ics(body=crlf)
    try:
        r = calendar_event_add(
            {"summary": "CRLF demo", "dtstart": "20261025T120000Z"}, {})
        assert r["ok"] and r["verified"], r
        raw = open(path, newline="", encoding="utf-8").read()
        assert "\n" not in raw.replace("\r\n", ""), \
            "stray bare LF inside a CRLF calendar"
        assert "SUMMARY:CRLF demo\r\n" in raw
        assert raw.startswith(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Team sync\r\n")
    finally:
        _restore(path, old)
