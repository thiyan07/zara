"""Turn-level cancellation for the LLM conversation loop.

Covers: cancel before the first model call, cancel mid-tool-loop (no
further tools run, in-flight result stays recorded), cancelled turns never
claim success, TurnRegistry bounds + unknown-cancel no-op, and unchanged
behavior when cancel=None.
"""
import threading

from core.app import build_stack
from core.conversation import ConversationLoop, TurnRegistry
from core.llm import LLMError, LLMErrorKind
from core.memory_policy import MemoryService
from core.models import PermissionLevel, RiskLevel, ToolDefinition
from core.missions import MissionState
from core.providers import ScriptedProvider


def ping_tool(name="test.ping", risk=RiskLevel.SAFE,
              perm=PermissionLevel.PUBLIC):
    return ToolDefinition(name=name, description="test ping tool",
                          input_schema={"type": "object", "properties": {
                              "command": {"type": "string"}}},
                          permission=perm, risk=risk, timeout_s=5.0)


def make_loop(script, handler=None, **kw):
    stack = build_stack()
    stack["registry"].register(
        ping_tool(), handler or (lambda i, c: {"pong": True}))
    provider = ScriptedProvider(script)
    loop = ConversationLoop(provider=provider, ctx=stack["ctx"],
                            memory=MemoryService(stack["memory"]),
                            registry=stack["registry"], policy=stack["policy"],
                            engine=stack["engine"], router=stack["router"],
                            devices=stack["devices"],
                            governor=stack["gov"], missions=stack["missions"],
                            audit=stack["audit"], bus=stack["bus"],
                            sessions=stack["sessions"], tracer=stack["tracer"],
                            config=kw.get("config"))
    return stack, loop


def test_cancel_before_first_model_call_aborts_cleanly():
    ran = []
    stack, loop = make_loop(
        [
            ("go", {"type": "tool_call", "tool": "test.ping",
                    "arguments": {}}),
        ],
        handler=lambda i, c: ran.append(1) or {"pong": True})
    cancel = threading.Event()
    cancel.set()  # user cancelled before the turn started
    out = loop.handle_text("go run ping", cancel=cancel)
    assert out.status == "cancelled"
    assert "cancel" in out.reply.lower()
    assert loop.provider.calls == []  # model never consulted
    assert ran == []  # no tool executed
    assert out.turn_id and len(out.turn_id) == 32
    int(out.turn_id, 16)  # uuid hex
    assert stack["missions"].get(out.mission_id).state == \
        MissionState.CANCELLED


def test_cancel_mid_tool_loop_stops_further_tools():
    cancel = threading.Event()
    ran = []

    def handler(inputs, ctx):
        ran.append(dict(inputs))
        cancel.set()  # user hits cancel while the first tool runs
        return {"pong": True}

    stack, loop = make_loop([], handler=handler)
    # "alpha" matches the first request; "verified" matches the follow-up
    # prompt after a verified tool result (would chain a second tool).
    loop.provider = ScriptedProvider([
        ("verified", {"type": "tool_call", "tool": "test.ping",
                      "arguments": {"command": "second"}}),
        ("alpha", {"type": "tool_call", "tool": "test.ping",
                   "arguments": {"command": "first"}}),
    ])
    out = loop.handle_text("alpha start", cancel=cancel)
    assert out.status == "cancelled"
    assert len(ran) == 1  # in-flight tool finished, no further tools
    assert len(loop.provider.calls) == 1  # no second reasoning step
    # In-flight result stays recorded: consistent with engine state.
    assert out.execution_id and out.tool == "test.ping"
    rec = stack["engine"].get(out.execution_id)
    assert rec.tool == "test.ping"

    # Control: without cancel the same first tool runs and the turn
    # continues to a second reasoning step and completes normally.
    ran2 = []
    stack2, loop2 = make_loop(
        [
            ("verified", {"type": "response",
                          "text": "Pong confirmed."}),
            ("alpha", {"type": "tool_call", "tool": "test.ping",
                       "arguments": {"command": "first"}}),
        ],
        handler=lambda i, c: ran2.append(dict(i)) or {"pong": True})
    out2 = loop2.handle_text("alpha start", cancel=None)
    assert len(ran2) == 1 and len(loop2.provider.calls) == 2
    assert out2.status == "responded"


def test_cancelled_turn_never_claims_success():
    stack, loop = make_loop([
        ("go", {"type": "tool_call", "tool": "test.ping", "arguments": {}}),
    ])
    cancel = threading.Event()
    cancel.set()
    out = loop.handle_text("go run ping", cancel=cancel)
    assert out.status == "cancelled"
    assert out.status not in ("responded",)
    lowered = out.reply.lower()
    assert "cancel" in lowered
    assert "succeeded" not in lowered and "verified" not in lowered
    assert "pong" not in lowered


def test_provider_cancelled_error_maps_to_cancelled_status():
    # A provider that raises CANCELLED on its own (no event passed) still
    # yields the marked outcome, never a success claim.
    from core.providers import ModelProvider

    class Canceller(ModelProvider):
        name = "canceller"

        def reason(self, *a, **k):
            raise LLMError(LLMErrorKind.CANCELLED, "client hung up")

    stack = build_stack()
    loop = ConversationLoop(provider=Canceller(), ctx=stack["ctx"],
                            memory=MemoryService(stack["memory"]),
                            registry=stack["registry"], policy=stack["policy"],
                            engine=stack["engine"], router=stack["router"],
                            devices=stack["devices"], governor=stack["gov"],
                            missions=stack["missions"], audit=stack["audit"],
                            bus=stack["bus"], sessions=stack["sessions"],
                            tracer=stack["tracer"])
    out = loop.handle_text("hello", cancel=None)
    assert out.status == "cancelled"
    assert "cancel" in out.reply.lower()
    assert out.turn_id


def test_reason_passes_cancel_to_provider():
    stack, loop = make_loop([])
    cancel = threading.Event()
    cancel.set()
    with _raises_cancelled():
        loop._reason(None, "", "cloud", "hi", cancel=cancel)
    # Without cancel the same provider answers normally.
    out = loop._reason(None, "", "cloud", "hi", cancel=None)
    assert out.action is not None


class _raises_cancelled:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        assert exc_type is not None and issubclass(exc_type, LLMError)
        assert exc.kind == LLMErrorKind.CANCELLED
        return True


def test_registry_bounded_oldest_finished_first():
    reg = TurnRegistry(max_entries=10)
    first = [reg.create()[0] for _ in range(10)]
    for tid in first:
        reg.finish(tid)
    more = [reg.create()[0] for _ in range(5)]
    assert len(reg) == 10
    for tid in first[:5]:  # oldest finished evicted
        assert reg.get(tid) is None
    for tid in first[5:] + more:  # newest retained
        assert reg.get(tid) is not None


def test_registry_never_evicts_live_turns():
    reg = TurnRegistry(max_entries=3)
    live = [reg.create()[0] for _ in range(3)]
    overflow, _ = reg.create()  # all live: overflow, never kill a live turn
    assert reg.get(live[0]) is not None
    assert reg.get(overflow) is not None
    reg.finish(live[0])
    reg.create()
    assert reg.get(live[0]) is None  # finished oldest evicted first
    assert reg.get(live[1]) is not None


def test_registry_unknown_cancel_is_safe_noop():
    reg = TurnRegistry()
    assert reg.cancel("does-not-exist") is False
    assert len(reg) == 0
    tid, _ = reg.create()
    assert reg.cancel(tid) is True
    assert reg.get(tid) is not None and reg.get(tid).is_set()


def test_turn_cancel_via_registry_event():
    # HTTP wiring flow: pre-create a turn, cancel it mid-loop via the
    # registry id, and the running turn observes the same event.
    ran = []
    current: list = []

    def _handler(inputs, ctx):
        ran.append(1)
        loop_ref[0].turns.cancel(current[0])
        return {"pong": True}

    stack, loop = make_loop([
        ("verified", {"type": "response", "text": "Pong confirmed."}),
        ("ping", {"type": "tool_call", "tool": "test.ping",
                  "arguments": {}}),
    ], handler=_handler)
    loop_ref = [loop]
    tid, ev = loop.turns.create()
    current.append(tid)
    out = loop.handle_text("ping it", cancel=ev, turn_id=tid)
    assert out.status == "cancelled"
    assert out.turn_id == tid
    assert len(ran) == 1  # first tool finished, second never proposed
    # Finished turns stay resolvable but their cancel is a truthful no-op
    # (False: nothing left to abort). Callers must not mistake it for an
    # in-flight abort.
    assert loop.turns.get(tid) is not None
    assert loop.turns.cancel(tid) is False


def test_resume_after_approval_cancel_aborts_summary():
    stack, loop = make_loop([
        ("do confirm", {"type": "tool_call", "tool": "test.confirm",
                        "arguments": {}}),
    ])
    stack["registry"].register(
        ping_tool("test.confirm", risk=RiskLevel.CONFIRM,
                  perm=PermissionLevel.CONFIRM), lambda i, c: {"ok": True})
    out = loop.handle_text("do confirm thing")
    assert out.status == "approval_required" and out.execution_id
    loop.provider = ScriptedProvider([
        ("verified", {"type": "response", "text": "Approved and done."}),
    ])
    cancel = threading.Event()
    cancel.set()
    resumed = loop.resume_after_approval(out.execution_id, cancel=cancel)
    assert resumed.status == "cancelled"
    assert resumed.execution_id == out.execution_id  # engine record intact
    assert "cancel" in resumed.reply.lower()
    assert resumed.status != "responded"


def test_no_cancel_behavior_unchanged():
    stack, loop = make_loop([
        ("verified", {"type": "response", "text": "Pong confirmed."}),
        ("battery", {"type": "tool_call", "tool": "test.ping",
                     "arguments": {}, "reason": "check"}),
    ])
    out = loop.handle_text("check thing battery", cancel=None)
    assert out.status == "responded" and "Pong" in out.reply
    assert out.tool == "test.ping"
    assert out.turn_id and len(out.turn_id) == 32
    assert stack["missions"].get(out.mission_id).state == \
        MissionState.COMPLETED
    assert stack["tracer"].for_trace(out.trace_id)
    assert loop.turns.get(out.turn_id) is not None  # retained, finished
    assert out.turn_id not in loop.turns.active_ids()
