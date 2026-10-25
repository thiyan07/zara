"""Multi-step plan execution through the deterministic intent path.

A parsed plan (proposal.steps) runs step-by-step through the SAME
per-step pipeline as single-step turns: resolver -> policy/governor ->
fabric.execute_on_device -> verification. One attempt per step, never
auto-retried (retrying a non-idempotent step could duplicate a side
effect). The first approval hold, failure, or unresolvable step stops
the plan; later steps never run speculatively. Progress is tracked on
a MissionEngine when injected (mission_id returned for cancel/status).
"""
import threading
import time

import pytest

from core.intent import IntentProposal, IntentTurnService, LocalIntentParser
from core.missions import MissionEngine, MissionState
from core.models import PermissionLevel, RiskLevel, ToolDefinition
from tests.test_intent import doc, enroll_claim, make_stack, register
from tests.test_tanglish import _live_stack  # noqa: F401  (kept for parity)


class FakePlanParser:
    """Deterministic parser stub returning a fixed multi-step proposal."""

    name = "fake-plan"

    def __init__(self, steps):
        self._steps = steps

    def parse(self, utterance, ctx, session_context=""):
        first = self._steps[0]
        return IntentProposal(
            utterance=utterance[:500], capability=first["capability"],
            parameters=dict(first.get("parameters") or {}),
            preferred_device=first.get("preferred_device", ""),
            steps=[dict(s) for s in self._steps],
            confidence=0.9, needs_clarification=False, source=self.name)


def _echo_stack(**kw):
    stack = make_stack()
    stack["registry"].register(
        ToolDefinition(name="test.echo", description="test echo",
                       input_schema={"type": "object", "properties": {}},
                       output_schema={"type": "object"}),
        lambda inputs, ctx=None: {"ok": True, "echo": True})
    enroll_claim(stack, "d1")
    register(stack, "d1")
    stack["fabric"].advertise(
        [doc(cap="test.echo", description="test echo tool")], "d1")
    return stack


def _svc(stack, steps, missions="default"):
    from core.resolver import CapabilityResolver
    eng = MissionEngine() if missions == "default" else missions
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FakePlanParser(steps), stack["audit"], eng)
    return svc, eng


def _echo_steps(n=2):
    return [{"capability": "test.echo", "parameters": {},
             "preferred_device": "", "confidence": 0.9}
            for _ in range(n)]


def test_plan_parses_ready_not_clarification():
    from tests.test_intent import phone_ctx
    p = LocalIntentParser().parse(
        "check my battery then check my network", phone_ctx())
    assert len(p.steps) == 2
    assert p.needs_clarification is False


def test_plan_all_safe_executes_with_mission():
    stack = _echo_stack()
    svc, eng = _svc(stack, _echo_steps(2))
    out = svc.handle_utterance("echo twice")
    assert out["executed"] is True, out
    assert len(out["steps"]) == 2
    assert all(o["status"] == "ok" for o in out["steps"]), out
    assert out["mission_id"]
    m = eng.get(out["mission_id"])
    assert m.state == MissionState.COMPLETED
    assert [s.status for s in m.steps] == ["ok", "ok"]
    assert "2 steps" in out["message"]


def test_plan_second_step_never_runs_after_unresolvable_first():
    stack = _echo_stack()
    calls = {"echo2": 0}

    def _echo2(inputs, ctx=None):
        calls["echo2"] += 1
        return {"ok": True}

    stack["registry"].register(
        ToolDefinition(name="test.echo2", description="second",
                       input_schema={"type": "object", "properties": {}},
                       output_schema={"type": "object"}), _echo2)
    # advertise() has refresh semantics (full snapshot): one batch.
    stack["fabric"].advertise(
        [doc(cap="test.echo", description="test echo tool"),
         doc(cap="test.echo2", description="second")], "d1")
    svc, eng = _svc(stack, [
        {"capability": "nope.unknown", "parameters": {},
         "preferred_device": "", "confidence": 0.5},
        {"capability": "test.echo2", "parameters": {},
         "preferred_device": "", "confidence": 0.9}])
    out = svc.handle_utterance("bad then good")
    assert out["executed"] is False
    assert calls["echo2"] == 0  # never ran speculatively
    assert eng.get(out["mission_id"]).state == MissionState.FAILED
    assert "step 1" in out["message"]


def test_plan_failing_tool_runs_once_never_retried():
    stack = _echo_stack()
    calls = {"n": 0}

    def _flaky(inputs, ctx=None):
        calls["n"] += 1
        raise RuntimeError("boom")

    stack["registry"].register(
        ToolDefinition(name="test.flaky", description="flaky",
                       input_schema={"type": "object", "properties": {}},
                       output_schema={"type": "object"}), _flaky)
    stack["fabric"].advertise(
        [doc(cap="test.echo", description="test echo tool"),
         doc(cap="test.flaky")], "d1")
    svc, eng = _svc(stack, [
        {"capability": "test.flaky", "parameters": {},
         "preferred_device": "", "confidence": 0.9},
        {"capability": "test.echo", "parameters": {},
         "preferred_device": "", "confidence": 0.9}])
    out = svc.handle_utterance("flaky then echo")
    assert calls["n"] == 1  # single attempt, no auto-retry
    assert out["executed"] is False
    assert eng.get(out["mission_id"]).state == MissionState.FAILED


def test_plan_stateless_without_engine_still_executes():
    stack = _echo_stack()
    svc, _ = _svc(stack, _echo_steps(1), missions=None)
    out = svc.handle_utterance("echo once")
    assert out["executed"] is True
    assert out["mission_id"] == ""
    assert out["steps"][0]["status"] == "ok"


def test_plan_cancel_between_steps_stops_plan():
    stack = _echo_stack()
    ran = {"slow": 0, "fast": 0}

    def _slow(inputs, ctx=None):
        time.sleep(0.4)
        ran["slow"] += 1
        return {"ok": True}

    def _fast(inputs, ctx=None):
        ran["fast"] += 1
        return {"ok": True}

    for name, fn in (("test.slow", _slow), ("test.fast", _fast)):
        stack["registry"].register(
            ToolDefinition(name=name, description=name,
                           input_schema={"type": "object", "properties": {}},
                           output_schema={"type": "object"}), fn)
    # advertise() has refresh semantics (full snapshot): one batch.
    stack["fabric"].advertise(
        [doc(cap="test.echo", description="test echo tool"),
         doc(cap="test.slow"), doc(cap="test.fast")], "d1")
    svc, eng = _svc(stack, [
        {"capability": "test.slow", "parameters": {},
         "preferred_device": "", "confidence": 0.9},
        {"capability": "test.fast", "parameters": {},
         "preferred_device": "", "confidence": 0.9}])
    out = {}

    def _run():
        out.update(svc.handle_utterance("slow then fast"))

    t = threading.Thread(target=_run)
    t.start()
    # Wait for the mission to exist, then cancel mid-plan.
    deadline = time.time() + 5
    mid = ""
    while time.time() < deadline:
        if eng.list():
            mid = eng.list()[0].id
            break
        time.sleep(0.01)
    assert mid
    eng.cancel(mid)
    t.join(timeout=10)
    assert ran["fast"] == 0  # cancelled before step 2
    assert any(o["status"] == "cancelled" for o in out["steps"]), out


def test_plan_approval_hold_parks_with_reference():
    stack = make_stack()
    stack["registry"].register(
        ToolDefinition(name="test.gated", description="gated",
                       input_schema={"type": "object", "properties": {}},
                       output_schema={"type": "object"},
                       permission=PermissionLevel.CONFIRM,
                       risk=RiskLevel.CONFIRM),
        lambda inputs, ctx=None: {"ok": True})
    enroll_claim(stack, "d1")
    register(stack, "d1")
    raw = doc(cap="test.gated", description="gated tool")
    raw["risk"] = "confirm"
    stack["fabric"].advertise([raw], "d1")
    svc, eng = _svc(stack, [
        {"capability": "test.gated", "parameters": {},
         "preferred_device": "", "confidence": 0.9},
        {"capability": "test.gated", "parameters": {},
         "preferred_device": "", "confidence": 0.9}])
    out = svc.handle_utterance("gated twice")
    assert out["executed"] is False  # nothing ran
    assert out["steps"][0]["status"] == "waiting_for_permission", out
    assert len(out["steps"]) == 1  # second step never attempted
    assert eng.get(out["mission_id"]).state == \
        MissionState.WAITING_FOR_PERMISSION
