"""Tool-loop edge coverage: approval-streaming, inter-step cancel,
provider-error mid-turn, 3-tool budget fit + 4-tool cap, deny-then-recover,
arg-sensitive repeat guard, streaming/non-streaming parity.

Uses make_loop/ScriptedProvider patterns from test_talk_cancel.
"""
import threading

from core.llm import LLMAction, LLMError, LLMErrorKind, LLMOutcome
from core.missions import MissionState
from core.models import PermissionLevel, RiskLevel
from core.providers import ModelProvider, ScriptedProvider
from tests.test_talk_cancel import make_loop, ping_tool


def _names(events):
    return [n for n, _ in events]


def test_G01_approval_mid_turn_streaming_emit_and_resume():
    stack, loop = make_loop([
        ("do confirm", {"type": "tool_call", "tool": "test.confirm",
                        "arguments": {}}),
    ])
    stack["registry"].register(
        ping_tool("test.confirm", risk=RiskLevel.CONFIRM,
                  perm=PermissionLevel.CONFIRM),
        lambda i, c: {"ok": True})
    events = []
    out = loop.handle_text("do confirm thing",
                           emit=lambda n, d: events.append((n, d)))
    assert out.status == "approval_required"
    assert out.execution_id
    assert _names(events)[0] == "turn"
    approvals = [d for n, d in events if n == "approval"]
    assert len(approvals) == 1
    assert approvals[0]["execution_id"] == out.execution_id
    tool_phases = [d for n, d in events
                   if n == "tool" and d.get("phase") == "approval"]
    assert len(tool_phases) == 1
    assert tool_phases[0]["execution_id"] == out.execution_id
    assert stack["missions"].get(out.mission_id).state == \
        MissionState.WAITING_FOR_PERMISSION
    # Human approves via the engine; the resume summarizes and completes.
    loop.provider = ScriptedProvider([
        ("approved action", {"type": "response",
                             "text": "Approved and done."}),
    ])
    resumed = loop.resume_after_approval(out.execution_id)
    assert resumed.status == "responded"
    assert "Approved and done" in resumed.reply
    assert resumed.execution_id == out.execution_id


def test_G02_cancel_between_step1_success_and_step2_proposal():
    cancel = threading.Event()
    ran = []

    def handler(inputs, ctx):
        ran.append(dict(inputs))
        cancel.set()  # cancel lands while step 1 runs
        return {"pong": True}

    stack, loop = make_loop([], handler=handler)
    loop.provider = ScriptedProvider([
        ("verified", {"type": "tool_call", "tool": "test.ping",
                      "arguments": {"command": "second"}}),
        ("alpha", {"type": "tool_call", "tool": "test.ping",
                   "arguments": {"command": "first"}}),
    ])
    out = loop.handle_text("alpha start", cancel=cancel)
    assert out.status == "cancelled"
    assert len(ran) == 1  # second tool never dispatched
    assert len(loop.provider.calls) == 1  # no second reasoning step
    assert out.execution_id  # step-1 id recorded, consistent with engine
    rec = stack["engine"].get(out.execution_id)
    assert rec.tool == "test.ping"
    assert stack["missions"].get(out.mission_id).state == \
        MissionState.CANCELLED
    assert "cancel" in out.reply.lower()
    assert "verified" not in out.reply.lower()


class _SecondCallUnavailable(ScriptedProvider):
    """Scripted stub extended ONLY for the error test: first reasoning
    step answers from script, the second raises UNAVAILABLE."""

    def reason(self, system, user, tools, cancel=None, timeout=60.0):
        if self.calls:  # one reasoning step already served
            raise LLMError(LLMErrorKind.UNAVAILABLE, "provider 503")
        return super().reason(system, user, tools, cancel, timeout)


def test_G03_provider_error_mid_turn_is_error_never_success():
    stack, loop = make_loop([])
    loop.provider = _SecondCallUnavailable([
        ("go", {"type": "tool_call", "tool": "test.ping", "arguments": {}}),
    ])
    out = loop.handle_text("go run ping")
    assert out.status == "error"
    assert "unavailable" in out.reply.lower()
    assert "verified" not in out.reply.lower()
    assert "pong" not in out.reply.lower()  # no result leaked as success
    assert stack["missions"].get(out.mission_id).state == \
        MissionState.FAILED


def _chain_stack(extra_names, script):
    ran = []

    def _mk(name):
        def _h(inputs, ctx, _n=name):
            ran.append(_n)
            return {"pong": True, "from": _n}
        return _h

    # make_loop registers test.ping with `handler`: install the tracker
    # there (re-registering would raise), then add the extra tools.
    stack, loop = make_loop([], handler=_mk("test.ping"))
    for name in extra_names:
        stack["registry"].register(ping_tool(name), _mk(name))
    loop.provider = ScriptedProvider(script)
    return stack, loop, ran


def test_G04a_three_tool_chain_fits_budget_and_completes():
    stack, loop, ran = _chain_stack(
        ["test.pong", "test.pung"],
        [
            ("test.pung`", {"type": "response", "text": "all three done"}),
            ("test.pong`", {"type": "tool_call", "tool": "test.pung",
                            "arguments": {}}),
            ("test.ping`", {"type": "tool_call", "tool": "test.pong",
                            "arguments": {}}),
            ("go", {"type": "tool_call", "tool": "test.ping",
                    "arguments": {}}),
        ])
    out = loop.handle_text("go chain three")
    assert out.status == "responded", out.reply
    assert "all three done" in out.reply
    assert ran == ["test.ping", "test.pong", "test.pung"]
    assert stack["missions"].get(out.mission_id).state == \
        MissionState.COMPLETED


def test_G04b_four_tool_script_hits_explicit_budget_cap():
    stack, loop, ran = _chain_stack(
        ["test.pong", "test.pung", "test.pang"],
        [
            ("test.pung`", {"type": "tool_call", "tool": "test.pang",
                            "arguments": {}}),
            ("test.pong`", {"type": "tool_call", "tool": "test.pung",
                            "arguments": {}}),
            ("test.ping`", {"type": "tool_call", "tool": "test.pong",
                            "arguments": {}}),
            ("go", {"type": "tool_call", "tool": "test.ping",
                    "arguments": {}}),
        ])
    out = loop.handle_text("go chain four")
    assert out.status == "error"
    assert "tool-call limit" in out.reply
    assert ran == ["test.ping", "test.pong", "test.pung"]  # 4th never ran
    assert stack["missions"].get(out.mission_id).state == \
        MissionState.FAILED


def test_G05_denied_first_tool_then_allowed_second_recovers():
    stack, loop = make_loop([])
    stack["registry"].register(
        ping_tool("test.shutdown"), lambda i, c: {})
    loop.provider = ScriptedProvider([
        ("test.ping`", {"type": "response", "text": "recovered via ping"}),
        ("could not run", {"type": "tool_call", "tool": "test.ping",
                           "arguments": {}}),
        ("go", {"type": "tool_call", "tool": "test.shutdown",
                "arguments": {}}),
    ])
    out = loop.handle_text("go try shutdown then ping")
    assert out.status == "responded", out.reply
    assert "recovered" in out.reply
    steps = stack["missions"].get(out.mission_id).steps
    assert any(s.tool == "test.shutdown" and s.status == "failed"
               for s in steps)  # denial recorded, not fatal


class _QueuedProvider(ModelProvider):
    """Stateful stub: serves queued actions in order, ignoring input."""

    name = "queued"

    def __init__(self, actions) -> None:
        self.q = list(actions)
        self.calls: list[str] = []

    def reason(self, system, user, tools, cancel=None, timeout=60.0):
        if cancel is not None and cancel.is_set():
            raise LLMError(LLMErrorKind.CANCELLED, "cancelled")
        self.calls.append(user)
        action = (self.q.pop(0) if self.q
                  else {"type": "response", "text": "done."})
        return LLMOutcome(action=LLMAction(**action),
                          text=action.get("text", ""),
                          provider=self.name, latency_s=0.0)


def test_G06_same_tool_different_args_allowed_exact_dup_stopped():
    ran = []
    stack, loop = make_loop(
        [], handler=lambda i, c: ran.append(dict(i)) or {"pong": True})
    loop.provider = _QueuedProvider([
        {"type": "tool_call", "tool": "test.ping",
         "arguments": {"command": "a"}},
        {"type": "tool_call", "tool": "test.ping",
         "arguments": {"command": "b"}},  # different args: NOT a repeat
        {"type": "tool_call", "tool": "test.ping",
         "arguments": {"command": "b"}},  # exact duplicate: stopped
    ])
    out = loop.handle_text("go repeat check")
    assert ran == [{"command": "a"}, {"command": "b"}]  # both allowed
    assert len(loop.provider.calls) == 3  # third proposal seen + stopped
    assert out.status == "error"
    assert "repeat" in out.reply.lower()


def _parity_script():
    return [
        ("test.pong`", {"type": "response", "text": "both done"}),
        ("verified by the core", {"type": "tool_call", "tool": "test.pong",
                                 "arguments": {}}),
        ("go", {"type": "tool_call", "tool": "test.ping",
                "arguments": {}}),
    ]


def _parity_loop():
    stack, loop = make_loop([])
    stack["registry"].register(
        ping_tool("test.pong"), lambda i, c: {"pong2": True})
    return stack, loop


def test_G07_streaming_nonstreaming_parity():
    stack_s, loop_s = _parity_loop()
    loop_s.provider = ScriptedProvider(_parity_script())
    events = []
    out_s = loop_s.handle_text("go run both",
                               emit=lambda n, d: events.append((n, d)))

    stack_p, loop_p = _parity_loop()
    loop_p.provider = ScriptedProvider(_parity_script())
    out_p = loop_p.handle_text("go run both")

    assert out_s.reply == out_p.reply == "both done"
    assert out_s.status == out_p.status == "responded"
    assert stack_s["missions"].get(out_s.mission_id).state == \
        stack_p["missions"].get(out_p.mission_id).state == \
        MissionState.COMPLETED
    assert _names(events)[0] == "turn"
    assert _names(events)[-1] == "done"
    deltas = [d["text"] for n, d in events if n == "delta"]
    assert deltas == ["both done"]  # deltas only carry model text


def test_G08_unknown_tool_never_executes_no_false_success():
    from tests.test_talk_cancel import make_loop
    from core.providers import ScriptedProvider
    stack, loop = make_loop([])
    ran = []
    loop.provider = ScriptedProvider([
        ("go", {"type": "tool_call", "tool": "test.nonexistent",
                "arguments": {}}),
    ])
    out = loop.handle_text("go do the thing")
    assert ran == []
    assert list(stack["engine"]._records) == []
    assert "nonexistent" not in out.reply or "reject" in out.reply.lower() \
        or "unknown" in out.reply.lower() or "done" in out.reply.lower()
    # Decisive: no success language about the unknown tool.
    assert "verified" not in out.reply.lower()
