"""Battery follow-up regression: the recharge-time defect.

Physical symptom (Vivo V2338): "What is the battery percent?" answers
correctly, but "How much time is needed to fully recharge it?" ended the
turn with "I will calculate and tell you" (fresh session, zero tools run)
— a promise nothing fulfills, so the user waits forever.

Root cause, all Core-side:
- system.battery returns one instantaneous reading (pct/charging/source/
  health) with no time-to-full; nothing documents that limit.
- The loop accepted a promissory `response` as a terminal turn.

Fix: documented tool contract + ACTION_CONTRACT rule + a bounded
promissory-response guard (one redirect, then an honest fallback).

All battery data here is MOCKED (deterministic handler); physical-device
validation is a separate manual step, never claimed by these tests.
"""
import threading

from core.app import build_stack
from core.conversation import (
    ConversationLoop,
    PROMISE_FALLBACK_REPLY,
    looks_promissory,
)
from core.llm import ACTION_CONTRACT
from core.memory_policy import MemoryService
from core.models import MissionState, PermissionLevel, RiskLevel, ToolDefinition
from core.providers import ScriptedProvider

# Mocked Vivo V2338 reading: 82%, charging via USB. No time-to-full exists
# on this path (Android ACTION_BATTERY_CHANGED carries none).
MOCKED_BATTERY = {
    "supported": True,
    "battery_pct": 82,
    "charging": True,
    "power_save": False,
    "source": "usb",
    "health": "good",
}


def battery_tool():
    return ToolDefinition(
        name="test.battery",
        description="Device battery state: instantaneous reading",
        input_schema={"type": "object", "properties": {}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
        timeout_s=5.0)


def make_battery_loop(script, handler=None, default_text="done.", **kw):
    stack = build_stack()
    stack["registry"].register(
        battery_tool(), handler or (lambda i, c: dict(MOCKED_BATTERY)))
    loop = ConversationLoop(
        provider=ScriptedProvider(script, default_text=default_text), ctx=stack["ctx"],
        memory=MemoryService(stack["memory"]), registry=stack["registry"],
        policy=stack["policy"], engine=stack["engine"], router=stack["router"],
        devices=stack["devices"], governor=stack["gov"],
        missions=stack["missions"], audit=stack["audit"], bus=stack["bus"],
        sessions=stack["sessions"], tracer=stack["tracer"],
        config=kw.get("config"))
    return stack, loop


def _terminal(loop, mission_id):
    return loop.missions.get(mission_id).state in (
        MissionState.COMPLETED, MissionState.FAILED, MissionState.CANCELLED)


def test_battery_percent_returns_mocked_value_terminal():
    stack, loop = make_battery_loop([
        ("verified", {"type": "response", "text": "Battery is at 82%."}),
        ("battery percent", {"type": "tool_call", "tool": "test.battery",
                             "arguments": {}}),
    ])
    out = loop.handle_text("What is the battery percent in this mobile?")
    assert out.status == "responded"
    assert "82%" in out.reply
    assert out.tool == "test.battery" and out.execution_id
    assert _terminal(loop, out.mission_id)


def test_promissory_recharge_reply_redirects_to_honest_answer():
    # Exact reproduced defect: zero tools, vow of future calculation.
    stack, loop = make_battery_loop([
        ("promises future action",
         {"type": "response",
          "text": "Battery is at 82% and charging; one reading cannot "
                  "estimate time-to-full."}),
        ("recharge", {"type": "response",
                      "text": "I will calculate and tell you."}),
    ])
    out = loop.handle_text("How much time is needed to fully recharge it?")
    assert out.status == "responded"
    assert len(loop.provider.calls) == 2  # one bounded redirect, no loop
    assert not looks_promissory(out.reply)  # never stops at the vow
    assert "82%" in out.reply  # honest, grounded answer
    assert _terminal(loop, out.mission_id)


def test_stubborn_promise_falls_back_honestly_and_stays_bounded():
    stack, loop = make_battery_loop(
        [], default_text="I will calculate it shortly.")
    out = loop.handle_text("How much time is needed to fully recharge it?")
    assert out.status == "responded"
    assert len(loop.provider.calls) == 2  # redirect + one retry, then stop
    assert out.reply == PROMISE_FALLBACK_REPLY
    assert not looks_promissory(out.reply)
    assert "Nothing was started and nothing is pending" in out.reply
    assert _terminal(loop, out.mission_id)


def test_honest_clarification_passes_through_untouched():
    # The good in-session reply observed live: no vow, must not redirect.
    stack, loop = make_battery_loop([
        ("recharge", {"type": "response",
                      "text": "I'm not sure how long it will take. Could you "
                              "let me know if it's currently plugged in?"}),
    ])
    out = loop.handle_text("How much time is needed to fully recharge it?")
    assert out.status == "responded"
    assert len(loop.provider.calls) == 1  # no redirect fired
    assert "plugged in" in out.reply


def test_tool_failure_reaches_terminal_without_stranding():
    def fail(inputs, ctx):
        raise RuntimeError("device unreachable")

    stack, loop = make_battery_loop([
        ("could not run", {"type": "response",
                           "text": "The battery check failed: unreachable."}),
        ("battery", {"type": "tool_call", "tool": "test.battery",
                     "arguments": {}}),
    ], handler=fail)
    out = loop.handle_text("What is the battery percent?")
    assert out.status in ("responded", "error")
    assert "fail" in out.reply.lower()  # failure communicated, no spinner
    assert _terminal(loop, out.mission_id)


def test_repeated_identical_questions_do_not_duplicate_tasks():
    ran = []
    stack, loop = make_battery_loop([
        ("battery", {"type": "tool_call", "tool": "test.battery",
                     "arguments": {}}),
    ], handler=lambda i, c: ran.append(1) or dict(MOCKED_BATTERY))
    out = loop.handle_text("battery percent please")
    assert len(ran) == 1  # repeat guard: second identical call refused
    assert out.status in ("responded", "error")  # terminal either way
    assert _terminal(loop, out.mission_id)


def test_cancel_leaves_no_pending_turn():
    stack, loop = make_battery_loop([
        ("battery", {"type": "tool_call", "tool": "test.battery",
                     "arguments": {}}),
    ])
    cancel = threading.Event()
    cancel.set()
    out = loop.handle_text("battery percent", cancel=cancel)
    assert out.status == "cancelled"
    assert _terminal(loop, out.mission_id)


def test_streaming_recharge_promise_also_redirects():
    stack, loop = make_battery_loop([
        ("promises future action",
         {"type": "response", "text": "One reading cannot estimate that."}),
        ("recharge", {"type": "response",
                      "text": "I will calculate and tell you."}),
    ])
    events = []
    out = loop.handle_text("How much time to fully recharge?",
                           emit=lambda n, d: events.append((n, d)))
    assert out.status == "responded"
    assert not looks_promissory(out.reply)
    names = [n for n, _ in events]
    assert names[0] == "turn" and names[-1] == "done"  # clean terminal emit
    assert _terminal(loop, out.mission_id)


def test_contract_documents_limits_and_matcher_is_narrow():
    from core.device_tools import DEVICE_TOOL_DEFS
    real = next(d for d in DEVICE_TOOL_DEFS if d.name == "system.battery")
    assert "time-to-full" in real.description  # limit documented for the LLM
    assert "single reading cannot estimate" in real.description
    assert "time_to_full" not in MOCKED_BATTERY
    assert "charge_time" not in "".join(MOCKED_BATTERY)
    assert "promise future action" in ACTION_CONTRACT
    # Narrow: vows match; questions, summaries, and real answers do not.
    assert looks_promissory("I will calculate and tell you")
    assert looks_promissory("Let me check the battery and estimate")
    assert looks_promissory("I\u2019ll monitor it and report back")
    assert not looks_promissory("The battery is currently at 82%.")
    assert not looks_promissory(
        "Could you let me know if it's currently plugged in?")
    assert not looks_promissory("Battery is at 82% and charging via usb.")
    assert not looks_promissory("")
    assert not looks_promissory(PROMISE_FALLBACK_REPLY)


def test_timeout_after_verified_tool_names_work_done():
    """Live defect (Vivo): system.battery verified, then the turn budget
    died on the summary step and the UI claimed "nothing was executed".
    The timeout reply must name the completed tool + execution, preserve
    the execution id on the turn, and stay terminal."""
    import time as _time

    import core.conversation as _conv

    ran = []
    real_time = _time.time
    t0 = real_time()

    def fake_time():
        # Real clock until the tool runs; then jump past any budget so
        # the NEXT loop-top check raises the turn-budget timeout.
        if ran:
            return t0 + 3600.0
        return real_time()

    stack, loop = make_battery_loop([
        ("battery", {"type": "tool_call", "tool": "test.battery",
                     "arguments": {}}),
    ], handler=lambda i, c: ran.append(1) or dict(MOCKED_BATTERY))
    _conv.time.time, saved = fake_time, _conv.time.time
    try:
        out = loop.handle_text("battery percent please")
    finally:
        _conv.time.time = saved
    assert ran == [1]  # tool really executed once
    assert out.status == "error"
    assert "test.battery" in out.reply  # work named, not denied
    assert "nothing was executed" not in out.reply
    assert "Tasks tab" in out.reply  # result location, actionable
    assert out.execution_id  # preserved for Tasks lookup
    rec = stack["engine"].get(out.execution_id)
    assert rec.tool == "test.battery"
    assert _terminal(loop, out.mission_id)


def test_timeout_with_zero_tools_keeps_honest_nothing_ran():
    """Control: when truly nothing ran, "nothing was executed" is true
    and stays."""
    import time as _time

    import core.conversation as _conv

    real_time = _time.time
    t0 = real_time()
    stack, loop = make_battery_loop([
        ("battery", {"type": "tool_call", "tool": "test.battery",
                     "arguments": {}}),
    ])
    # Stepped clock: every read jumps 1000 s, so the turn budget
    # (120 s) is already blown at the first loop-top check — zero
    # provider calls, zero tools, deterministically.
    tick = {"t": t0}

    def fake_time():
        tick["t"] += 1000.0
        return tick["t"]

    _conv.time.time, saved = fake_time, _conv.time.time
    try:
        out = loop.handle_text("battery percent please")
    finally:
        _conv.time.time = saved
    assert out.status == "error"
    assert "nothing was executed" in out.reply  # true here: zero tools ran
    assert out.execution_id == ""
    assert loop.provider.calls == []  # model never even consulted
    assert _terminal(loop, out.mission_id)
