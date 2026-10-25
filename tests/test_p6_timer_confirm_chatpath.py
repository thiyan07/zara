"""P5 feature-hunt stage P6: chat-path CONFIRM receipt for quick actions.

Live-discovered (2026-10-22, emulator): the chat path parks CONFIRM-class
quick actions (android.timer.set) WITHOUT the tool-level `confirm` input
— the user's approval action is that confirmation — so approving ran the
tool with inputs that failed schema validation (`missing required field:
minutes / confirm`). Three structural fixes are pinned here:

1. Grant-time confirm receipt: at approve, the engine satisfies a
   schema-declared `confirm` slot from the approval itself (audited as
   `approval_confirm_injected`) — the fingerprint bound at park is never
   re-seen, so nothing drifts.
2. Timer slot extraction: the local parser extracts a bounded duration
   (+ optional label) for android.timer.set and never leaks the generic
   `.set` direction rule ("start a timer…" must not emit enabled=True).
3. Grounding authority: a properties-less device-advertised stub can no
   longer silently disable the missing-required check — the registry
   (the engine's own validation authority) overrides stubs.
"""
import threading

from core.app import build_stack
from core.execution import ExecutionState
from core.intent import (IntentProposal, LocalIntentParser, ParserContext,
                         ground_proposal)
from core.models import DeviceKind, DeviceState

TIMER = "android.timer.set"
DEV = "emu-android"

TIMER_SCHEMA = {
    "type": "object", "required": ["minutes", "confirm"],
    "properties": {"minutes": {"type": "integer", "minimum": 1,
                               "maximum": 7200},
                   "label": {"type": "string"},
                   "skip_ui": {"type": "boolean"},
                   "confirm": {"type": "boolean"}},
}


def make_stack(fabric_db=""):
    return build_stack(fabric_db=fabric_db)


def enroll_claim(stack, device_id):
    code = stack["device_auth"].enroll(device_id, DeviceKind("android"))
    return stack["device_auth"].claim(code)


def register(stack, device_id, caps):
    stack["devices"].register(DeviceState(
        device_id=device_id, kind=DeviceKind("android"),
        capabilities=caps, online=True, status="online"))


class _TimerAgent(threading.Thread):
    """Fake paired phone: long-polls the queue and completes timer jobs
    with the Dart runner's result shape (hand-off verdict, verified
    false — Zara claims only the hand-off)."""

    def __init__(self, stack):
        super().__init__(daemon=True)
        self.q = stack["jobs"]
        self.stop_ev = threading.Event()
        self.inputs_seen: list[dict] = []

    def run(self):
        while not self.stop_ev.is_set():
            job = self.q.wait_for_job(DEV, timeout=0.4)
            if not job:
                continue
            self.inputs_seen.append(dict(job.inputs))
            self.q.complete(job.id, ok=True, result={
                "verdict": "handed-to-clock",
                "minutes": job.inputs.get("minutes"),
                "verification": "handed-off", "verified": False})

    def stop(self):
        self.stop_ev.set()


def test_approval_satisfies_schema_declared_confirm():
    """The chat-path shape: parked WITHOUT `confirm`; approving IS the
    user's confirm — the run must succeed, not fail validation."""
    stack = make_stack()
    enroll_claim(stack, DEV)
    register(stack, DEV, [TIMER])
    stack["fabric"].advertise(
        [{"id": TIMER, "name": TIMER, "descriptor_version": "1",
          "version": "1.0.0", "risk": "confirm",
          "availability": "available"}], DEV)
    rec = stack["engine"].submit_async(
        TIMER, {"minutes": 5, "label": "eggs"}, DEV, capability=TIMER)
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    assert "confirm" not in rec.inputs
    agent = _TimerAgent(stack)
    agent.start()
    try:
        out = stack["engine"].approve_async(rec.id)
        # RUNNING proves the approval validated; an instant fake agent
        # may finish the whole dispatch before approve_async's return
        # beats it to the assert — SUCCEEDED proves the same thing.
        assert out.state in (ExecutionState.RUNNING,
                             ExecutionState.SUCCEEDED), out
        end = None
        import time
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            rec2 = stack["engine"].get(rec.id)
            if rec2.state == ExecutionState.SUCCEEDED:
                end = rec2
                break
            time.sleep(0.05)
        assert end is not None, \
            f"approval never succeeded: {stack['engine'].get(rec.id)!r}"
        # The receipt lives in the audited inputs handed to the tool.
        assert end.inputs.get("confirm") is True
        assert agent.inputs_seen[0].get("confirm") is True
        assert agent.inputs_seen[0].get("minutes") == 5
    finally:
        agent.stop()
        agent.join(timeout=3)


def test_explicit_confirm_is_never_overridden_by_injection():
    """Direct tool-level shape (inputs already carry confirm=true): the
    grant must succeed too — injection only fills an ABSENT/False
    receipt and never mutates an explicit one into something else."""
    stack = make_stack()
    enroll_claim(stack, DEV)
    register(stack, DEV, [TIMER])
    stack["fabric"].advertise(
        [{"id": TIMER, "name": TIMER, "descriptor_version": "1",
          "version": "1.0.0", "risk": "confirm",
          "availability": "available"}], DEV)
    rec = stack["engine"].submit_async(
        TIMER, {"minutes": 3, "confirm": True}, DEV, capability=TIMER)
    agent = _TimerAgent(stack)
    agent.start()
    try:
        stack["engine"].approve_async(rec.id)
        import time
        deadline = time.monotonic() + 4.0
        fin = None
        while time.monotonic() < deadline:
            r = stack["engine"].get(rec.id)
            if r.state == ExecutionState.SUCCEEDED:
                fin = r
                break
            time.sleep(0.05)
        assert fin is not None
        assert fin.inputs["confirm"] is True
        assert len(agent.inputs_seen) == 1
    finally:
        agent.stop()
        agent.join(timeout=3)


def _timer_ctx():
    return ParserContext(
        known_capabilities={TIMER},
        capability_schemas={TIMER: TIMER_SCHEMA},
        devices=[],
    )


def test_timer_slot_extraction_minutes_and_label():
    p = LocalIntentParser().parse(
        "set a timer for 5 minutes to eggs", _timer_ctx())
    assert p.capability == TIMER
    assert p.parameters.get("minutes") == 5
    assert p.parameters.get("label") == "eggs"
    # The generic .set direction rule must never leak onto the timer.
    assert "enabled" not in p.parameters


def test_timer_without_duration_clarifies_honestly():
    p = LocalIntentParser().parse("set a timer", _timer_ctx())
    assert p.needs_clarification
    assert "how long" in p.clarification_reason.lower()


def test_timer_hours_scale_and_minutes_range_pins():
    p = LocalIntentParser().parse(
        "set a timer for 2 hours", _timer_ctx())
    assert p.parameters.get("minutes") == 120


def test_grounding_required_check_survives_stub_schema():
    """The live-discovered grounding gap: a device-advertised stub
    {"type":"object","required": [...]} (no properties map) must still
    trigger the missing-required clarify — never an approval park with
    empty inputs."""
    ctx = ParserContext(
        known_capabilities={TIMER},
        capability_schemas={TIMER: {"type": "object",
                                    "required": ["minutes"]}},
        devices=[])
    p = ground_proposal(
        IntentProposal(capability=TIMER, confidence=0.8), ctx)
    assert p.needs_clarification
    assert "minutes" in p.clarification_reason
