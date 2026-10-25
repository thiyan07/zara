"""P5 feature-hunt ⑱ (2026-10-24) — everyday unit + timezone
converters (core-local, SAFE, in-turn).

Sources (interface only, stdlib re-implementation): a bounded
city→IANA alias map + world clock (NickCirv/timezone-converter;
mattjohnsonpint/TimeZoneConverter's alias-map idea); GNU units'
exactness deliberately NOT adopted — a chat surface gets everyday
units, honest cross-kind refusals, no invented precision.

Pinned contract:
- same-measurement-kind only; cross-kind asks are honest ValueErrors
  (mass→length etc. never silently "work");
- temperature round-trips through Celsius with the exact formulas;
- data families (decimal KB/MB/GB/TB vs binary KiB/MiB/GiB/TiB)
  keep their distinct factors and never merge;
- common plural unit forms ("kgs", "gibs") parse; a trailing
  period ("5 kg.") parses;
- the world clock resolves common city names AND raw IANA ids;
  unmapped places are honest ValueErrors naming what is supported;
- a pinned host-local clock uses the NEXT-OCCURRENCE convention
  (today's clock if still ahead, else tomorrow) — the same
  convention the calendar day-words use — and reports the chosen
  local date explicitly;
- the chat matcher owns only ITS shapes ("convert <val> <u> to
  <u>", "(what) time in <place>", "<clock> in <place>") and never
  steals reminder/timer asks; absent capabilities fall through;
- the deterministic formatter renders both tool shapes (never a
  bare "Done").
"""
import pytest

import sys
import os
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

from core.builtin_tools import convert_units, time_zone  # noqa: E402

CAPSConverter = {"convert.units", "time.zone", "system.health",
                 "storage.scan"}


def ctx(caps=None):
    from core.intent import ParserContext
    return ParserContext(known_capabilities=caps or CAPSConverter)


def test_length_exact():
    out = convert_units({"value": 3, "unit": "km", "to": "mi"}, {})
    assert out["ok"] and abs(out["result"] - 1.8641135767) < 1e-9
    assert convert_units({"value": 2, "unit": "feet", "to": "cm"},
                         {})["result"] == pytest.approx(60.96)
    assert convert_units({"value": 100, "unit": "cm", "to": "m"},
                         {})["result"] == pytest.approx(1.0)


def test_mass_exact():
    assert convert_units({"value": 5, "unit": "kg", "to": "lb"},
                         {})["result"] == pytest.approx(
        5 / 0.45359237)
    assert convert_units({"value": 16, "unit": "oz", "to": "lb"},
                         {})["result"] == pytest.approx(1.0)


def test_temperature_roundtrip():
    assert convert_units({"value": 20, "unit": "c", "to": "f"},
                         {})["result"] == pytest.approx(68.0)
    assert convert_units({"value": 32, "unit": "f", "to": "c"},
                         {})["result"] == pytest.approx(0.0)
    assert convert_units({"value": 0, "unit": "c", "to": "k"},
                         {})["result"] == pytest.approx(273.15)
    out = convert_units({"value": 100, "unit": "c", "to": "f"}, {})
    back = convert_units({"value": out["result"], "unit": "f",
                          "to": "c"}, {})
    assert back["result"] == pytest.approx(100.0)


def test_data_families_distinct():
    # decimal MB is NOT binary MiB — the families never merge
    assert convert_units({"value": 1, "unit": "gib", "to": "gb"},
                         {})["result"] == pytest.approx(
        2**30 / 10**9)
    assert convert_units({"value": 1000, "unit": "mb", "to": "gb"},
                         {})["result"] == pytest.approx(1.0)


def test_cross_kind_refuses():
    with pytest.raises(ValueError):
        convert_units({"value": 1, "unit": "kg", "to": "m"}, {})
    with pytest.raises(ValueError):
        convert_units({"value": 5, "unit": "f", "to": "kg"}, {})


def test_unknown_unit_names_itself():
    with pytest.raises(ValueError) as e:
        convert_units({"value": 1, "unit": "cubit", "to": "m"}, {})
    assert "cubit" in str(e.value)


def test_plural_and_period_forms():
    assert convert_units({"value": 5, "unit": "kgs.", "to": "lbs"},
                         {})["ok"]
    assert convert_units({"value": 1.5, "unit": "gibs", "to": "mib"},
                         {})["result"] == pytest.approx(1536.0)


def test_bogus_value_refused():
    with pytest.raises(ValueError):
        convert_units({"value": "many", "unit": "kg", "to": "lb"}, {})


def test_zone_now_shape():
    out = time_zone({"zone": "tokyo"}, {})
    assert out["mode"] == "now" and out["zone"] == "Asia/Tokyo"
    assert out["zone_local"] and out["zone_abbr"]
    assert time_zone({"zone": "Asia/Kolkata"}, {})["mode"] == "now"


def test_zone_unknown_place_honest():
    with pytest.raises(ValueError) as e:
        time_zone({"zone": "atlantis"}, {})
    assert "atlantis" in str(e.value) and "IANA" in str(e.value)


def test_zone_pinned_next_occurrence():
    from datetime import datetime, timedelta
    out = time_zone({"zone": "new york", "clock": "09:30"}, {})
    assert out["mode"] == "pinned_clock"
    assert out["pinned_hh"] == 9 and out["pinned_mm"] == 30
    # the pinned UTC instant must equal the HOST-local next 09:30
    now = datetime.now().astimezone()
    target = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    want_utc = target.astimezone(
        __import__("datetime").timezone.utc).strftime(
        "%Y-%m-%dT%H:%MZ")
    assert out["utc"] == want_utc
    assert out["host_date"] == target.strftime("%a %b %d")


def test_zone_pinned_bogus_clock_refused():
    with pytest.raises(ValueError):
        time_zone({"zone": "tokyo", "clock": "25:00"}, {})


def _parser():
    from core.intent import LocalIntentParser
    return LocalIntentParser()


def test_matcher_convert_shapes():
    p = _parser()
    for ask, params in (
            ("convert 5 kg to lb",
             {"value": 5.0, "unit": "kg", "to": "lb"}),
            ("convert 20 c to f",
             {"value": 20.0, "unit": "c", "to": "f"})):
        out = p.parse(ask, ctx())
        assert out.capability == "convert.units", ask
        assert out.parameters == params, (ask, out.parameters)


def test_matcher_time_shapes():
    p = _parser()
    out = p.parse("what time is it in tokyo?", ctx())
    assert out.capability == "time.zone"
    assert out.parameters["zone"] == "tokyo"
    out = p.parse("time in new york", ctx())
    assert out.parameters["zone"] == "new york"
    out = p.parse("9am in new york", ctx())
    assert out.parameters == {"zone": "new york", "clock": "0900"}
    out = p.parse("9:30 in new york tomorrow", ctx())
    assert out.parameters["zone"] == "new york"
    assert out.parameters["clock"] == "0930"


def test_matcher_never_steals_reminders():
    p = _parser()
    out = p.parse("remind me at 9am in my calendar", ctx())
    assert out.capability != "time.zone"
    out = p.parse("set a timer for 9am in my calendar", ctx())
    assert out.capability != "time.zone"


def test_matcher_absent_capability_falls_through():
    p = _parser()
    out = p.parse("convert 5 kg to lb", ctx({"system.health"}))
    assert out.capability != "convert.units"


def test_turn_executes_local_caps_end_to_end():
    """The turn must EXECUTE local exec caps (engine, no device) — not
    answer them with the capabilities listing (the live-caught R41.5
    regression: BUILTIN_LOCAL membership routed them to _local_answer
    and the converter answered 'I can: …')."""
    from core.app import build_stack
    from core.intent import IntentTurnService, LocalIntentParser
    stack = build_stack()
    svc = IntentTurnService(
        stack["fabric"], stack["resolver"], LocalIntentParser(),
        stack["audit"], stack["missions"], None, None, None, None,
        None, None, None)
    out = svc.handle_utterance("convert 5 kg to lb")
    assert out.get("executed") is True, out
    assert "5.0 kg = 11.02 lb." in out.get("message", ""), out
    out = svc.handle_utterance("what time is it in tokyo?")
    assert out.get("executed") is True, out
    assert "Asia/Tokyo" in out.get("message", ""), out
    # honest tool refusal surfaces, still a whole turn
    out = svc.handle_utterance("convert 1 kg to m")
    assert out.get("executed") is False
    assert "different measurement kinds" in out.get("message", ""), out


def test_turn_local_caps_offline_devices_ok():
    """No device register/advertise step at all — the converters must
    still work (they belong to no device by design)."""
    from core.app import build_stack
    from core.intent import IntentTurnService, LocalIntentParser
    stack = build_stack()      # no enroll/register/advertise
    svc = IntentTurnService(
        stack["fabric"], stack["resolver"], LocalIntentParser(),
        stack["audit"], stack["missions"], None, None, None, None,
        None, None, None)
    out = svc.handle_utterance("convert 20 c to f")
    assert out.get("executed") is True and "20.0 c = 68.00 f" in \
        out.get("message", ""), out


def test_formatter_shapes():
    from core.intent import IntentTurnService, IntentProposal

    def prop(cap):
        return IntentProposal(request_id="t", utterance="x",
                              capability=cap, parameters={},
                              confidence=0.8,
                              needs_clarification=False,
                              source="local")
    msg = IntentTurnService._success_message(
        prop("convert.units"),
        {"result": {"value": 5, "unit": "kg", "to": "lb",
                    "result": 11.023113109243878}})
    assert msg.startswith("5 kg = 11.02 lb.")
    msg = IntentTurnService._success_message(
        prop("time.zone"), {"result": {
            "zone": "Asia/Tokyo", "mode": "now", "zone_local": "22:31",
            "zone_abbr": "JST", "zone_offset": "+0900",
            "zone_date": "Sat Oct 24", "host_clock": "19:01",
            "host_abbr": "IST", "hours_diff": 3.5}})
    assert "Tokyo" not in msg or "Asia/Tokyo" in msg
    assert "22:31 JST (+0900)" in msg and "3.5 h ahead of host" in msg
    assert "host 19:01 IST" in msg
    msg = IntentTurnService._success_message(
        prop("time.zone"), {"result": {
            "zone": "America/New_York", "mode": "pinned_clock",
            "host_clock": "19:01", "pinned_hh": 9, "pinned_mm": 30,
            "host_date": "Sun Oct 25", "host_offset": "+0530",
            "utc": "2026-10-25T04:00Z", "zone_local": "00:00",
            "zone_abbr": "EDT", "zone_offset": "-0400",
            "zone_date": "Sun Oct 25", "hours_diff": -9.5}})
    assert "09:30 host-local (Sun Oct 25, +0530) = 00:00 in " \
           "America/New_York" in msg
    assert "UTC 2026-10-25T04:00Z" in msg
