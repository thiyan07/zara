"""P6-a/P6-b (2026-10-24) — compound multi-day calendar asks and the
schedule-conflict check.

P6-a: "calendar today and tomorrow" / "calendar friday and saturday"
scope the READ to the named days — each named day gets its own honest
section (empty or not), tokens chained only by and/or-commas are
accepted, scattered day words are refused (single-day sweep keeps the
turn), duplicate dates collapse, titles per day cap at 6 with a count,
and an unsupported RRULE is NOTED (never dropped silently).

P6-b: "any conflicts tomorrow?" / "am I double-booked?" check real
OVERLAPS between TIMED events inside the asked window. Back-to-back
(end == start) is never a conflict; an event with no DTEND runs the
honest 2-hour default (counted and noted); all-day events are excluded
(they have no clock times) but counted and named; pairs cap at 5 with
the rest as a count; the sweep window starts NOW (a conflict that
already finished this morning is history, not a conflict). The
conflict check answers BEFORE the sweep and the week digest, so its
empty wording ("No overlapping events tomorrow.") can never be
mistaken for "nothing on the calendar".

Clock safety (house pattern): mutable-day fixtures use relative-to-now
stamps (`_later`), skipped near local midnight; all-day and unknown-day
fixtures use fixed-day naive stamps read as the laptop's LOCAL
time, so asserted clock strings are exact regardless of the host zone.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from core.calendar_flow import CalendarFlow
from tests.test_p6_calendar_day_scope import _target_for


def _stamp(target_date, hh, mm=0, ss=0):
    """Naive local stamp on a fixed local date; minute over 59 carries
    into the hour (fixture convenience, ics-unbounded by design here)."""
    carry, mm = divmod(mm + ss // 60, 60)
    ss = ss % 60
    return f"{target_date:%Y%m%d}T{hh + carry:02d}{mm:02d}{ss:02d}"


class _CalTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.flow = CalendarFlow(audit=lambda *a, **k: None)

    def _later(self, m0, hours=1, m1=None):
        """Start now+m0 minutes, end +hours (or now+m1). Naive LOCAL
        stamps; None when the pair would cross local midnight."""
        now = datetime.now().astimezone()
        a = now + timedelta(minutes=m0)
        b = a + timedelta(hours=hours) if m1 is None \
            else now + timedelta(minutes=m1)
        if a.date() != now.date() or b.date() != now.date():
            return None
        return (a.strftime("%Y%m%dT%H%M%S"), b.strftime("%Y%m%dT%H%M%S"))

    def _ask(self, ics: str, utterance: str) -> str:
        old = os.environ.get("ZARA_ICS_PATH")
        path = os.path.join(self.tmp, f"c{abs(hash(utterance))}.ics")
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
        self.assertTrue(out["executed"], out)
        return out["message"]

    def _vevent(self, uid, start, end=None, summary="E"):
        rows = ["BEGIN:VEVENT", f"UID:{uid}@x", f"DTSTART:{start}"]
        if end:
            rows.append(f"DTEND:{end}")
        rows.append(f"SUMMARY:{summary}")
        rows.append("END:VEVENT")
        return "\r\n".join(rows) + "\r\n"

    def _wrap(self, *vevents):
        return ("BEGIN:VCALENDAR\r\n" + "".join(vevents)
                + "END:VCALENDAR\r\n")


class TestCompoundScope(_CalTestBase):
    def test_today_and_tomorrow_lists_both_days(self):
        win = self._later(90, hours=1)
        if win is None:
            self.skipTest("no room before local midnight")
        tomorrow = _target_for("tomorrow")
        ics = self._wrap(
            self._vevent("a", win[0], win[1], "Morning standup"),
            self._vevent("b", _stamp(tomorrow, 14), _stamp(tomorrow, 15),
                         "Dentist"),
            # R50 fixture fix: a naive-LOCAL 08:00 event on the asked
            # day IS in the whole-day digest (the digest lists every
            # in-window event, empty-or-not — no 08:00–22:00 crop);
            # the old "should be excluded" expectation contradicted
            # the shipped semantics and could never pass.
            self._vevent("c", _stamp(tomorrow, 8), _stamp(tomorrow, 9),
                         "Early consult"),
        )
        msg = self._ask(ics, "my calendar today and tomorrow")
        self.assertIn("Calendar for today and tomorrow (3 event(s)):",
                      msg, msg)
        self.assertIn("Morning standup", msg, msg)
        self.assertIn("Dentist", msg, msg)
        self.assertIn("Early consult", msg, msg)

    def test_compound_empty_scope_names_the_days(self):
        ics = "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        msg = self._ask(ics, "calendar today and tomorrow")
        self.assertIn(
            "Nothing on the calendar for today and tomorrow.", msg, msg)

    def test_compound_every_named_day_has_a_section(self):
        win = self._later(90, hours=1)
        if win is None:
            self.skipTest("no room before local midnight")
        ics = self._wrap(
            self._vevent("a", win[0], win[1], "Sprint review"))
        msg = self._ask(ics, "calendar today and tomorrow")
        self.assertIn("Calendar for today and tomorrow (1 event(s)):",
                      msg, msg)
        self.assertIn("Sprint review", msg, msg)
        # tomorrow's own header with its honest 0 — an empty named day
        # is never hidden.
        self.assertIn(": 0 —", msg, msg)

    def test_compound_per_day_cap_six(self):
        friday = _target_for("friday")
        events = "".join(
            self._vevent(f"u{i}", _stamp(friday, 8, i * 30),
                         _stamp(friday, 9, i * 30), f"Student{i}")
            for i in range(8))
        ics = self._wrap(events)
        msg = self._ask(ics, "calendar friday and saturday")
        self.assertIn("Calendar for friday and saturday (8 event(s)):",
                      msg, msg)
        self.assertIn("+2 more", msg, msg)
        self.assertIn("Student0", msg, msg)
        self.assertNotIn("Student6", msg, msg)

    def test_compound_dedupes_and_orders_days(self):
        friday = _target_for("friday")
        saturday = friday + timedelta(days=1)
        ics = self._wrap(
            self._vevent("a", _stamp(saturday, 10), _stamp(saturday, 11),
                         "Saturday talk"),
            self._vevent("b", _stamp(friday, 10), _stamp(friday, 11),
                         "Friday talk"))
        msg = self._ask(ics, "calendar saturday and friday")
        fi = msg.index("Friday talk")
        si = msg.index("Saturday talk")
        self.assertLess(fi, si, msg)  # chronological, not utterance order

    def test_today_and_today_collapses_to_single_sweep(self):
        win = self._later(90, hours=1)
        if win is None:
            self.skipTest("no room before local midnight")
        ics = self._wrap(
            self._vevent("a", win[0], win[1], "Only one"))
        msg = self._ask(ics, "calendar today and today")
        self.assertIn("Calendar for today:", msg, msg)
        self.assertIn("Only one", msg, msg)


class TestCompoundRefusals(_CalTestBase):
    def test_scattered_day_words_refused(self):
        friday = _target_for("friday")
        ics = self._wrap(
            self._vevent("f", _stamp(friday, 10), _stamp(friday, 11),
                         "Friday item"))
        # "and" between the tokens is prose — not a compound ask, so
        # the single-day sweep keeps the turn.
        msg = self._ask(
            ics, "my calendar friday and a note for the walk to work")
        self.assertIn("Calendar for friday:", msg, msg)
        self.assertIn("Friday item", msg, msg)


class TestConflicts(_CalTestBase):
    def test_conflicts_detected_with_titles(self):
        # R50 fixture fix: the old fixtures stamped events via
        # `_later` (always TODAY by design) yet asked about TOMORROW —
        # structurally unpassable. Overlaps now stamp on the asked
        # day's naive LOCAL times (fixed wall clock, clock-safe any
        # time before midnight).
        tomorrow = _target_for("tomorrow")
        ics = self._wrap(
            self._vevent("a", _stamp(tomorrow, 10),
                         _stamp(tomorrow, 11, 30), "Design review"),
            self._vevent("b", _stamp(tomorrow, 10, 45),
                         _stamp(tomorrow, 11, 45), "Quota sync"))
        msg = self._ask(ics, "any conflicts tomorrow?")
        self.assertIn("1 conflict(s) tomorrow:", msg, msg)
        self.assertIn('"Design review" ↔', msg, msg)
        self.assertIn('"Quota sync"', msg, msg)

    def test_conflicts_none_is_honest(self):
        tomorrow = _target_for("tomorrow")
        ics = self._wrap(
            self._vevent("a", _stamp(tomorrow, 10), _stamp(tomorrow, 11),
                         "Solo talk"))
        msg = self._ask(ics, "any conflicts tomorrow?")
        self.assertIn("No overlapping events tomorrow.", msg, msg)

    def test_back_to_back_is_not_a_conflict(self):
        tomorrow = _target_for("tomorrow")
        ics = self._wrap(
            self._vevent("a", _stamp(tomorrow, 10),
                         _stamp(tomorrow, 11), "Slot one"),
            self._vevent("b", _stamp(tomorrow, 11),
                         _stamp(tomorrow, 12), "Slot two"))
        msg = self._ask(ics, "am i double-booked tomorrow?")
        self.assertIn("No overlapping events tomorrow.", msg, msg)

    def test_no_dtend_runs_honest_two_hours(self):
        tomorrow = _target_for("tomorrow")
        ics = self._wrap(
            self._vevent("a", _stamp(tomorrow, 10),
                         summary="Open slot"),
            self._vevent("b", _stamp(tomorrow, 11),
                         summary="Two oclock"))
        msg = self._ask(ics, "any conflicts tomorrow?")
        self.assertIn("1 conflict(s) tomorrow:", msg, msg)
        self.assertIn("(2 event(s) without an end time counted as "
                      "2 hours)", msg, msg)

    def test_all_day_excluded_but_named(self):
        tomorrow = _target_for("tomorrow")
        ics = self._wrap(
            "BEGIN:VEVENT\r\nUID:ad@x\r\n"
            f"DTSTART:{tomorrow:%Y%m%d}\r\nSUMMARY:Company offsite\r\n"
            "END:VEVENT\r\n")
        msg = self._ask(ics, "any conflicts tomorrow?")
        self.assertIn("No overlapping events tomorrow.", msg, msg)
        self.assertIn("1 all-day event(s) skipped", msg, msg)
        self.assertIn("first: Company offsite", msg, msg)

    def test_scope_today_vs_full_sweep(self):
        win = self._later(90, hours=1)
        win2 = self._later(105)
        if not (win and win2):
            self.skipTest("no room before local midnight")
        tomorrow = _target_for("tomorrow")
        ics = self._wrap(
            self._vevent("a", win[0], win[1], "Today pair one"),
            self._vevent("b", win2[0], win2[1], "Today pair two"),
            self._vevent("c", _stamp(tomorrow, 11), _stamp(tomorrow, 12),
                         "Tomorrow one"),
            self._vevent("d", _stamp(tomorrow, 11, 30),
                         _stamp(tomorrow, 12, 30), "Tomorrow two"))
        msg_today = self._ask(ics, "what conflicts today?")
        self.assertIn("1 conflict(s) today:", msg_today, msg_today)
        self.assertNotIn("Tomorrow two", msg_today, msg_today)
        msg_sweep = self._ask(ics, "any conflicts")
        self.assertIn("2 conflict(s) in the next 7 days:",
                      msg_sweep, msg_sweep)

    def test_pairs_cap_at_five_with_count(self):
        # Seven DISJOINT clusters of two overlapping events on the
        # current week's Sunday — exactly 7 pairs, so the answer shows
        # 5 rows with the honest "+2 more" tail (never a merged blob).
        now = datetime.now().astimezone()
        base = (now.date() - timedelta(days=now.weekday())
                + timedelta(days=6))
        clusters = []
        for i in range(7):
            clusters.append(self._vevent(
                f"x{i}", _stamp(base, 8, i * 40),
                _stamp(base, 8, i * 40 + 35), f"Left{i}"))
            clusters.append(self._vevent(
                f"y{i}", _stamp(base, 8, i * 40 + 30),
                _stamp(base, 8, i * 40 + 35), f"Right{i}"))
        ics = self._wrap("".join(clusters))
        msg = self._ask(ics, "any conflicts this week")
        self.assertIn("7 conflict(s) this week:", msg, msg)
        self.assertIn("+2 more overlap(s)", msg, msg)
        self.assertEqual(msg.count("↔"), 5, msg)


class TestCollisionRegressions(_CalTestBase):
    def test_conflict_anchor_is_search_but_guarded(self):
        tomorrow = _target_for("tomorrow")
        ics = self._wrap(
            self._vevent("a", _stamp(tomorrow, 10), _stamp(tomorrow, 11),
                         "Solo talk"))
        # mid-utterance conflict vocabulary anchors (re.match would
        # need position 0 and miss the ask) …
        msg = self._ask(ics, "tell me if i have any conflicts tomorrow")
        self.assertIn("No overlapping events tomorrow.", msg, msg)
        # … but a co-occurring action verb belongs to another flow.
        out = self.flow.turn("cancel the dentist appointment if it "
                             "conflicts tomorrow", who="user")
        self.assertIsNone(out)

    def test_free_ask_still_uses_free_slots(self):
        tomorrow = _target_for("tomorrow")
        ics = self._wrap(
            self._vevent("a", _stamp(tomorrow, 14), _stamp(tomorrow, 15),
                         "Kit review"))
        msg = self._ask(ics, "am i free tomorrow")
        self.assertIn("Free tomorrow:", msg, msg)

    def test_transfer_conflict_wording_does_not_stick(self):
        # "conflicts" is shared vocabulary with media jobs; the phone's
        # media asks route elsewhere — the calendar surface only takes
        # explicit schedule-conflict asks.
        out = self.flow.turn("media backup with conflicts ask", who="user")
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
