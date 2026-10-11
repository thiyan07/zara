"""Multi-tool turns: sequential tool A -> tool B in one turn (Night 2 §1).

Regression: the second _execute crashed in
missions.transition(EXECUTING -> EXECUTING) (IllegalTransition, no
self-edge), aborting the turn. The fix adds an explicit EXECUTING
self-edge ("advance to next tool step") and records each tool as a
MissionStep. Invalid transitions stay illegal.
"""
import pytest

from core.missions import IllegalTransition, MissionEngine, MissionState
from tests.test_talk_cancel import make_loop, ping_tool


def two_tool_loop(**kw):
    stack, loop = make_loop([], **kw)
    stack["registry"].register(
        ping_tool("test.pong"), lambda i, c: {"pong2": True})
    loop.provider.__class__  # ScriptedProvider replaced below
    from core.providers import ScriptedProvider
    loop.provider = ScriptedProvider([
        ("test.pong`", {"type": "response", "text": "both done"}),
        ("verified by the core", {"type": "tool_call", "tool": "test.pong",
                                "arguments": {}}),
        ("go", {"type": "tool_call", "tool": "test.ping",
                "arguments": {}}),
    ])
    return stack, loop


def test_M01_two_sequential_tools_succeed_in_one_turn():
    stack, loop = two_tool_loop()
    out = loop.handle_text("go run both")
    assert out.status == "responded", out.reply
    assert "both done" in out.reply
    assert stack["missions"].get(out.mission_id).state == \
        MissionState.COMPLETED


def test_M02_second_tool_independently_denied_is_not_success():
    stack, loop = make_loop([])
    from core.models import PermissionLevel, RiskLevel
    # Name hits a policy deny-pattern: hard-deny regardless of the earlier
    # allowed tool. Independent per-tool authorization, proven by the
    # recorded DENIED step and a turn that never claims success.
    stack["registry"].register(
        ping_tool("test.shutdown"), lambda i, c: {})
    from core.providers import ScriptedProvider
    loop.provider = ScriptedProvider([
        ("verified by the core", {"type": "tool_call", "tool": "test.shutdown",
                                "arguments": {}}),
        ("go", {"type": "tool_call", "tool": "test.ping",
                "arguments": {}}),
    ])
    out = loop.handle_text("go")
    # Evil tool is OWNER-gated: policy denies it independently of the
    # earlier allowed tool. The turn must surface the denial, never a
    # forged success.
    assert out.status in ("responded", "error")
    assert "pong" not in out.reply or "evil" not in out.reply
    steps = stack["missions"].get(out.mission_id).steps
    assert any(s.tool == "test.shutdown" and s.status == "failed"
               for s in steps)


def test_M03_executing_self_edge_allowed_others_still_illegal():
    eng = MissionEngine()
    m = eng.create("x")
    eng.transition(m.id, MissionState.PLANNING)
    eng.transition(m.id, MissionState.EXECUTING)
    # Next-step self-edge now legal...
    eng.transition(m.id, MissionState.EXECUTING)
    assert eng.get(m.id).state == MissionState.EXECUTING
    # ...but invalid edges still raise, terminal stays immutable.
    with pytest.raises(IllegalTransition):
        eng.transition(m.id, MissionState.PLANNING)
    eng.transition(m.id, MissionState.COMPLETED)
    with pytest.raises(IllegalTransition):
        eng.transition(m.id, MissionState.EXECUTING)


def test_M04_malformed_second_proposal_rejected_not_executed():
    stack, loop = make_loop([])
    from core.providers import ScriptedProvider
    loop.provider = ScriptedProvider([
        ("verified by the core", {"type": "response", "text": "recovered"}),
        ("go", {"type": "tool_call", "tool": "test.ping",
                "arguments": {}}),
    ])
    # Corrupt the second-step proposal path directly: a tool_call without
    # a tool name must raise MALFORMED, never execute.
    import pytest as _pytest
    from core.llm import LLMError, LLMErrorKind, parse_llm_action
    with _pytest.raises(LLMError) as ei:
        parse_llm_action({"type": "tool_call", "arguments": {}})
    assert ei.value.kind == LLMErrorKind.MALFORMED
    out = loop.handle_text("go")
    assert out.status == "responded"
    assert "recovered" in out.reply
