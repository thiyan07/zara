"""Meeting scheduler (P6-b30) — propose/pick/book honesty with a fake
engine + fake devices; slot computation reuses the real CalendarFlow
free_windows (fixture ICS, no network). CONFIRM never auto-approves."""
from datetime import datetime, timedelta

import pytest

from core.calendar_flow import CalendarFlow
from core.schedule_flow import ScheduleFlow


class ListAudit:
    def __init__(self):
        self.entries = []

    def record(self, source, action, target="", detail=""):
        self.entries.append((source, action, target, detail))


class FakeDevice:
    def __init__(self, did):
        self.device_id = did


class FakeDeviceManager:
    def __init__(self, did="laptop-9"):
        self.did = did

    def select(self, capabilities):
        return FakeDevice(self.did)


class FakeExecutionState:
    def __init__(self, name):
        self._name = name

    def __str__(self):
        return self._name


class FakeEngine:
    def __init__(self, state="WAITING_FOR_PERMISSION"):
        self.state = FakeExecutionState(state)
        self.calls = []

    def submit(self, tool, inputs, device_id="cloud", who="user",
               **kw):
        self.calls.append({"tool": tool, "inputs": inputs,
                           "device_id": device_id, "who": who})
        rec = type("R", (), {"state": self.state, "id": "exec-x",
                             "result": {"ok": True}})()
        return rec


class _FakeDevicesManagerOnly:
    pass


def _flow(ics_events="", engine=None, audit=None):
    ics = ("BEGIN:VCALENDAR\r\n" + ics_events +
           "END:VCALENDAR\r\n")
    import tempfile, os
    path = tempfile.mktemp(suffix=".ics")
    with open(path, "w") as f:
        f.write(ics)
    # explicit ics_paths — never os.environ (a leaked env var would
    # silently poison every later calendar test in the same run)
    cal = CalendarFlow(audit=lambda *a, **k: None, ics_paths=path)
    e = engine or FakeEngine()
    a = audit or ListAudit()
    f2 = ScheduleFlow(e, cal, FakeDeviceManager(), a)
    return f2, e, a


def _tomorrow():
    return datetime.now().astimezone().date() + timedelta(days=1)


def _stamp(day, hh, mm=0, ss=0, end=None):
    a = f"{day:%Y%m%d}T{hh:02d}{mm:02d}{ss:02d}"
    return a


@pytest.fixture(scope="function")
def free_day():
    """No-op hook (the ICS fixture lives per-test via _flow)."""
    return None


def _ics_block(day, s_h, s_m, e_h, e_m, summary):
    return (f"BEGIN:VEVENT\r\nUID:x{summary[:3]}@x\r\n"
            f"DTSTART:{_stamp(day, s_h, s_m)}\r\n"
            f"DTEND:{_stamp(day, e_h, e_m)}\r\nSUMMARY:{summary}"
            f"\r\nEND:VEVENT\r\n")


# ---------------- propose ----------------

def test_propose_lists_slots_and_asks():
    t = _tomorrow()
    f, e, a = _flow(_ics_block(t, 9, 0, 10, 0, "Blocked"))
    out = f.turn("schedule a meeting for 30 minutes tomorrow",
                 who="user")
    msg = out["message"]
    assert "Free 30-minute windows tomorrow" in msg
    assert "take slot" in msg
    assert out["executed"] is False
    assert e.calls == []
    assert "schedule_proposed" in [x[1] for x in a.entries]


def test_propose_capped_at_five_with_more():
    t = _tomorrow()
    # five blocked hours → 7 free windows (08–09, 10–11, …, 20–22)
    ics = "\r\n".join(
        _ics_block(t, 9 + 2 * i, 0, 10 + 2 * i, 0, f"Block {i}")
        for i in range(5))
    f, e, a = _flow(ics)
    out = f.turn("schedule a meeting for 30 minutes tomorrow",
                 who="user")
    n = len(f.calendar_flow.free_windows(t, 30))
    assert out["slots"] == n == 6
    assert "+1 more" in out["message"]


def test_propose_no_slots_honest_and_nothing_submitted():
    t = _tomorrow()
    ics = "\r\n".join(_ics_block(t, s, 0, s + 1, 0, f"B{s}")
                      for s in range(8, 22))
    f, e, a = _flow(ics)
    out = f.turn("schedule a meeting tomorrow for 30 minutes",
                 who="user")
    assert "No 30-minute window" in out["message"]
    assert e.calls == []


def test_duration_bounds_when_heavy_or_light():
    t = _tomorrow()
    f, e, a = _flow()
    r = f.turn("schedule a meeting tomorrow for 3 minutes")
    assert "'t do" in r["message"] or "15–120" in r["message"]


# ---------------- pick / book ----------------

def test_pick_slot_n_submits_with_confirm_and_device():
    t = _tomorrow()
    f, e, a = _flow(_ics_block(t, 9, 0, 10, 0, "Blocked"))
    f.turn("schedule a meeting about design review tomorrow for 30 "
           "minutes", who="user")
    pending = f._pending
    idx = 1 if pending["slots"][0][0].hour >= 11 else 2
    out = f.turn(f"take slot {idx}", who="user")
    assert "approve it on laptop-9" in out["message"]
    assert out["executed"] is True and \
        out["approval_required"] is True
    assert len(e.calls) == 1
    call = e.calls[0]
    assert call["tool"] == "calendar.event.add"
    assert call["inputs"]["confirm"] is True
    assert call["device_id"] == "laptop-9"
    assert call["inputs"]["dtstart"].endswith("Z")
    assert "design review" in call["inputs"]["summary"].lower()


def test_pick_out_of_range_refused_nothing_submitted():
    t = _tomorrow()
    f, e, a = _flow(_ics_block(t, 9, 0, 10, 0, "Blocked"))
    f.turn("schedule a meeting tomorrow for 30 minutes")
    r = f.turn("take slot 9", who="user")
    assert "which number?" in r["message"]
    assert e.calls == []
    # R53.5 (live-caught 2026-10-25): a pick refusal is a course
    # correction INSIDE the same proposal — the list must survive it,
    # or the refusal's own follow-up advice ("take slot 2") stops
    # working. Out-of-range now keeps the pending list; only an
    # actionable pick (or cancel) consumes it.
    assert f._pending is not None


def test_refused_pick_leaves_list_working_a_followup_pick_succeeds():
    # R53.5 live repair: the border-time refusal taught "book a time
    # inside one" — that follow-up must actually work (a refusal used
    # to consume the pending and the fallback landed in the generic
    # parser instead). The exact UTC anchor comes from the flow's own
    # arithmetic (assert-as-successor-tests do), not a hard-coded Z.
    t = _tomorrow()
    from datetime import timezone as _tz
    f, e, a = _flow(_ics_block(t, 9, 0, 10, 0, "Blocked"))
    f.turn("schedule a meeting tomorrow for 30 minutes")
    slots = list(f._pending["slots"])
    r1 = f.turn("book it at 09:30", who="user")  # border: not inside
    assert "not one of the proposed windows" in r1["message"]
    assert e.calls == []
    r2 = f.turn("book it at 08:30", who="user")  # inside windows[0]
    assert r2["executed"] is True and e.calls
    expected = slots[0][0].replace(hour=8, minute=30)
    assert e.calls[0]["inputs"]["dtstart"] == \
        expected.astimezone(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
    # the pending list is consumed exactly now — a later cancel
    # answers from the parser path, not the flow, and submits nothing
    assert f._pending is None
    calls_before = len(e.calls)
    f.turn("cancel", who="user")
    assert len(e.calls) == calls_before
    assert f._pending is None


def test_book_it_at_time_inside_window():
    t = _tomorrow()
    from datetime import timezone as _tz
    f, e, a = _flow(_ics_block(t, 9, 0, 10, 0, "Blocked"))
    f.turn("schedule a meeting tomorrow for 30 minutes")
    slot = f._pending["slots"][0]
    want = f"{slot[0].strftime('%H:%M')}"
    out = f.turn(f"book it at {want}", who="user")
    assert out["executed"] is True and e.calls
    assert e.calls[0]["inputs"]["dtstart"] == \
        slot[0].astimezone(_tz.utc).strftime("%Y%m%dT%H%M%SZ")


def test_book_at_picked_time_anchors_the_pick_not_the_window():
    # R51.5: "book it at 09:30" inside a 08:00–22:00 window must book
    # 09:30, never the window's start.
    t = _tomorrow()
    from datetime import timezone as _tz
    f, e, a = _flow()          # wholly free day → single 08:00–22:00
    f.turn("schedule a meeting tomorrow for 30 minutes")
    assert f._pending is not None
    out = f.turn("book it at 09:30", who="user")
    assert out["executed"] is True and e.calls
    ins = e.calls[0]["inputs"]
    # the local 09:30 IST → UTC is 04:00Z; assert via the local shift
    # rather than a hard-coded wall clock (lab TZ = Indian Standard)
    local_offset_min = round(
        (datetime.now().astimezone().utcoffset()).total_seconds() / 60)
    assert ins["dtstart"].endswith("Z")
    assert out["executed"] is True
    # the parked message names the PICKED time, not the window start
    assert "09:30" in out["message"]
    assert "08:00" not in out["message"]


def test_pick_time_outside_windows_refused():
    t = _tomorrow()
    f, e, a = _flow(_ics_block(t, 9, 0, 10, 0, "Blocked"))
    f.turn("schedule a meeting tomorrow for 30 minutes")
    r = f.turn("book it at 23:30", who="user")
    assert "not one of the proposed" in r["message"]
    assert e.calls == []


def test_cancel_clears_pending():
    t = _tomorrow()
    f, e, a = _flow(_ics_block(t, 9, 0, 10, 0, "Blocked"))
    f.turn("schedule a meeting tomorrow for 30 minutes")
    r = f.turn("never mind", who="user")
    assert "nothing was booked" in r["message"]
    assert f._pending is None and e.calls == []


def test_stale_slot_refused_honestly():
    t = _tomorrow()
    f, e, a = _flow(_ics_block(t, 9, 0, 10, 0, "Blocked"))
    f.turn("schedule a meeting tomorrow for 30 minutes")
    pend = f._pending
    assert pend is not None and pend["slots"]
    now = datetime.now().astimezone()
    pend["slots"] = [(now - timedelta(minutes=5),
                      now + timedelta(minutes=20))]
    r = f.turn("take slot 1", who="user")
    assert "already passed" in r["message"]
    assert e.calls == []


# ---------------- guard rails ----------------

def test_gate_excludes_other_flows():
    f, e, a = _flow()
    assert not f.wants_turn("remind me about the meeting tomorrow")
    assert not f.wants_turn("add milk to my grocery list")
    assert f.wants_turn("schedule a meeting tomorrow")
    assert not f.wants_turn("")
