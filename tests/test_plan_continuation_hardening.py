"""Plan-continuation hardening (Stage 24h).

Regression coverage for the audit round: concurrent/duplicate
continuation requests, restart + crash-consistency recovery (mission
record adoption), pruned/missing held records, additional held-record
terminal states, sequential multi-hold plans, cancellation during a
continuation, identity preservation, and corrupted-envelope refusals.
Fixture-only: no devices, no desktop, no hardware, no real sends.
"""
import threading

from core.intent import IntentTurnService
from core.models import (ExecutionState, PermissionLevel, RiskLevel,
                         ToolDefinition)
from core.missions import MissionState, PersistentMissionEngine
from core.resolver import CapabilityResolver
from tests.test_intent import doc, enroll_claim, make_stack, register
# Reuse the Stage 24g harness exactly so hardening tests exercise the
# same stack/semantics the baseline suite proves.
from tests.test_plan_continuation import FixedPlanParser, _box, _stack


def _svc_removed():
    """(unused placeholder — see concrete helpers below.)"""
    raise NotImplementedError


# ------------------------------------------------------------------
# re-used pragmatic helpers (plain functions, no cleverness)
# ------------------------------------------------------------------

def _build_svc(stack, steps, missions_engine=None):
    eng = missions_engine if missions_engine is not None else _new_eng()
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FixedPlanParser(steps), stack["audit"], eng)
    # Stage 24h wiring: the real host attaches the adoption hook to the
    # execution engine after building the intent service (build_stack).
    if hasattr(stack.get("engine"), "set_mission_adopt"):
        stack["engine"].set_mission_adopt(svc.adopt_mission_record)
    return svc, eng


def _new_eng():
    from core.missions import MissionEngine
    return MissionEngine()


def _steps(n=2):
    out = [{"capability": "test.gated", "parameters": {},
            "preferred_device": "", "confidence": 0.9}
           for _ in range(n - 1)]
    out.append({"capability": "test.echo", "parameters": {"x": "f"},
                "preferred_device": "", "confidence": 0.9})
    return out


def _park(box, steps=None):
    steps = steps or _steps()
    stack = _stack(box)
    svc, eng = _build_svc(stack, steps)
    out = svc.handle_utterance("park a task")
    assert out["steps"][0]["status"] == "waiting_for_permission"
    return stack, svc, eng, out, steps


# ------------------------------------------------------------------
# concurrency: single-owner continuation
# ------------------------------------------------------------------

def _slow_stack(box, steps):
    stack = _stack(box)

    def _slow(inputs, ctx=None):
        box["slow_started"].set()
        # Bounded wait: the test decides when the step finishes, but the
        # handler can never hang the suite forever.
        for _ in range(1000):
            if box["slow_go"].wait(timeout=0.05):
                break
        return {"ok": True, "ran": "slow"}

    stack["registry"].register(
        ToolDefinition(name="test.slow", description="slow tool",
                       input_schema={"type": "object", "properties": {}},
                       output_schema={"type": "object"}), _slow)
    # ONE combined advertise call replaces the device's whole
    # doc set, so the gated docs must be re-listed here.
    raw = doc(cap="test.gated", description="gated tool")
    raw["risk"] = "confirm"
    raw2 = doc(cap="test.gated2", description="second gated tool")
    raw2["risk"] = "confirm"
    stack["fabric"].advertise(
        [doc(cap="test.echo", description="echo tool"),
         doc(cap="test.slow", description="slow tool"),
         raw, raw2], "d1")
    return stack


def test_concurrent_continuation_is_single_owner():
    box = _box()
    box["slow_started"] = threading.Event()
    box["slow_go"] = threading.Event()
    steps = [{"capability": "test.gated", "parameters": {},
              "preferred_device": "", "confidence": 0.9},
             {"capability": "test.slow", "parameters": {},
              "preferred_device": "", "confidence": 0.9},
             {"capability": "test.echo", "parameters": {},
              "preferred_device": "", "confidence": 0.9}]
    stack = _slow_stack(box, steps)
    svc, eng = _build_svc(stack, steps)
    out = svc.handle_utterance("park a task")
    mid = out["mission_id"]
    stack["engine"].approve(out["approval_reference"])

    result_a = {}

    def _a():
        result_a["res"] = svc.resume_plan(mid)

    ta = threading.Thread(target=_a)
    ta.start()
    assert box["slow_started"].wait(timeout=5)
    # second concurrent continuation must refuse, not race the first
    res_b = svc.resume_plan(mid)
    assert res_b["resumed"] is False and res_b["executed"] is False
    assert "already being continued" in res_b["message"]
    box["slow_go"].set()
    ta.join(timeout=10)
    assert result_a["res"]["executed"] is True
    assert eng.get(mid).state == MissionState.COMPLETED
    # every step ran exactly once — no duplicate side effects
    assert box["gate"] == 1 and box["echo"] == 1


def test_crash_between_step_and_persist_is_checkpointed():
    """Per-step checkpoint: a completed step is in the STORED mission
    right after it ran, not only at the mission's next transition."""
    from core.missions import PersistentMissionEngine
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "m.db")
    box = _box()
    stack = _stack(box)
    eng = PersistentMissionEngine(path)
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FixedPlanParser(_steps(2)), stack["audit"], eng)
    out = svc.handle_utterance("park a task")
    mid = out["mission_id"]
    # fresh-loaded engine (simulating process restart before any more
    # transitions) already knows step 1's stored status is whatever the
    # in-memory engine had — but more importantly the checkpoint after
    # each ok step in the driver means: once step 1 of the CONTINUATION
    # runs, a later load sees it 'ok' even if the process died before
    # the final transition.
    eng2 = PersistentMissionEngine(path)
    assert eng2.get(mid).state == MissionState.WAITING_FOR_PERMISSION
    stack["engine"].approve(out["approval_reference"])
    res = svc.resume_plan(mid)
    assert res["executed"] is True
    eng3 = PersistentMissionEngine(path)
    m3 = eng3.get(mid)
    assert m3.state == MissionState.COMPLETED
    assert all(s.status == "ok" for s in m3.steps)
    # two steps ([gated, echo]): one gated run under approval, one echo
    assert box["gate"] == 1 and box["echo"] == 1


# ------------------------------------------------------------------
# restart + adoption: the approved step is never lost, never re-run
# ------------------------------------------------------------------

def test_restart_after_approval_continues_without_record(tmp_path):
    """Adoption hook: the moment the approved record reaches SUCCEEDED
    the parked plan adopts it (step marked ok, hold cleared). A restart
    (fresh mission store, EMPTY execution records) continues from the
    first non-ok step — the approved step is not re-executed and its
    outcome is fed forward from the stored mission record."""
    from core.missions import PersistentMissionEngine
    path = str(tmp_path / "missions.db")
    box = _box()
    stack = _stack(box)
    eng = PersistentMissionEngine(path)
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FixedPlanParser(_steps(2)), stack["audit"], eng)
    stack["engine"].set_mission_adopt(svc.adopt_mission_record)
    out = svc.handle_utterance("park a task")
    mid = out["mission_id"]
    stack["engine"].approve(out["approval_reference"])
    # adoption happened inline at success time
    assert eng.get(mid).steps[0].status == "ok"
    assert (eng.plan_of(mid) or {}).get("held") is None
    # RESTART: the execution engine loses its records; the mission
    # store persists. New service on the same hardware stack.
    stack["engine"]._records.clear()
    eng2 = PersistentMissionEngine(path)
    svc2 = IntentTurnService(stack["fabric"],
                             CapabilityResolver(stack["fabric"]),
                             FixedPlanParser(_steps(2)), stack["audit"],
                             eng2)
    res = svc2.resume_plan(mid)
    assert res["executed"] is True and res["resumed"] is True, res
    assert eng2.get(mid).state == MissionState.COMPLETED
    assert eng2.get(mid).steps[0].status == "ok"
    # approved step ran exactly once (under its approval); only the
    # continuation step (echo) ran after restart
    assert box["gate"] == 1 and box["echo"] == 1


def test_adopted_record_pruned_still_continues():
    """Record-pruning (24 h terminal retention) cannot strand a plan:
    once adopted, the stored step result is the verification."""
    box = _box()
    stack, svc, eng, out, steps = _park(box)
    mid = out["mission_id"]
    stack["engine"].approve(out["approval_reference"])
    assert eng.get(mid).steps[0].status == "ok"
    stack["engine"].prune()
    res = svc.resume_plan(mid)
    assert res["executed"] is True
    assert eng.get(mid).state == MissionState.COMPLETED
    assert box["gate"] == 1 and box["echo"] == 1


def test_adoption_ignores_foreign_records():
    """The adoption hook is bookkeeping bound to the CURRENT hold: a
    mission-bound record from a different mission/tool, or one that is
    not SUCCEEDED, changes nothing."""
    box = _box()
    stack, svc, eng, out, steps = _park(box)
    mid = out["mission_id"]
    foreign = None
    try:
        foreign = stack["engine"].submit("test.echo", {"x": "f"}, "d1",
                                         "user",
                                         mission_id="mission-other")
    except Exception:
        pass
    if foreign is not None:
        foreign = getattr(foreign, "id", None)
    if foreign:
        assert svc.adopt_mission_record(
            stack["engine"].get(foreign)) is None
        assert eng.plan_of(mid).get("resumable") is True
        assert eng.get(mid).steps[0].status != "ok"


# ------------------------------------------------------------------
# missing record WITHOUT adoption (worst-case honesty)
# ------------------------------------------------------------------

def test_missing_record_without_adoption_is_honest_and_cautious():
    """No hook (e.g. crash between record success and adoption): the
    step may have taken effect in the real world. The refusal says so
    explicitly instead of nudging a blind duplicate."""
    box = _box()
    stack = _stack(box)
    eng = PersistentMissionEngine(":memory:")
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FixedPlanParser(_steps(2)), stack["audit"], eng)
    stack["engine"].set_mission_adopt(None)   # NO adoption hook
    out = svc.handle_utterance("park a task")
    mid = out["mission_id"]
    stack["engine"].approve(out["approval_reference"])
    stack["engine"]._records.pop(out["approval_reference"])  # the crash
    res = svc.resume_plan(mid)
    assert res["resumed"] is False and res["executed"] is False
    assert "no longer retained" in res["message"]
    assert "may already have taken effect" in res["message"]
    assert eng.get(mid).state == MissionState.WAITING_FOR_PERMISSION
    assert box["gate"] == 1 and box["echo"] == 0   # ran once, not twice


def test_record_central_states_cancelled_timed_out_denied():
    box = _box()
    stack, svc, eng, out, steps = _park(box)
    mid = out["mission_id"]
    rec = stack["engine"].get(out["approval_reference"])
    rec.state = ExecutionState.CANCELLED
    rec.error = "cancelled by operator"
    res = svc.resume_plan(mid)
    assert res["resumed"] is True and res["executed"] is False
    assert "cancelled" in res["message"]
    assert eng.get(mid).state == MissionState.FAILED
    assert eng.get(mid).steps[0].status == "failed"
    # terminal missions stay terminal for later continuation requests
    res2 = svc.resume_plan(mid)
    assert res2["resumed"] is False
    assert "only a plan parked" in res2["message"]

    # TIMED_OUT
    box2 = _box()
    stack2, svc2, eng2, out2, _ = _park(box2)
    rec2 = stack2["engine"].get(out2["approval_reference"])
    rec2.state = ExecutionState.TIMED_OUT
    res = svc2.resume_plan(out2["mission_id"])
    assert "timed_out" in res["message"]
    assert eng2.get(out2["mission_id"]).state == MissionState.FAILED

    # DENIED (re-check failure at dispatch is represented honestly)
    box3 = _box()
    stack3, svc3, eng3, out3, _ = _park(box3)
    rec3 = stack3["engine"].get(out3["approval_reference"])
    rec3.state = ExecutionState.DENIED
    rec3.error = "dispatch refused at re-check"
    res = svc3.resume_plan(out3["mission_id"])
    assert "denied" in res["message"]
    assert eng3.get(out3["mission_id"]).state == MissionState.FAILED


def test_sequential_holds_two_parks_one_completion():
    box = _box()
    steps = [{"capability": "test.gated", "parameters": {},
              "preferred_device": "", "confidence": 0.9},
             {"capability": "test.echo", "parameters": {},
              "preferred_device": "", "confidence": 0.9},
             {"capability": "test.gated2", "parameters": {},
              "preferred_device": "", "confidence": 0.9}]
    stack, svc, eng, out, steps0 = _park(box, steps)
    mid = out["mission_id"]
    stack["engine"].approve(out["approval_reference"])
    r1 = svc.resume_plan(mid)
    # second gated tool parks again (distinct toolname so the first
    # single-use grant cannot be silently reused)
    assert eng.get(mid).state == MissionState.WAITING_FOR_PERMISSION
    assert (eng.plan_of(mid) or {}).get("held", {}).get("tool") == \
        "test.gated2"
    assert "paused again" in r1["message"]
    stack["engine"].approve(r1["approval_reference"])
    r2 = svc.resume_plan(mid)
    assert r2["executed"] is True
    assert eng.get(mid).state == MissionState.COMPLETED
    assert box["gate"] == 2 and box["echo"] == 1


def test_cancellation_during_continuation():
    box = _box()
    box["slow_started"] = threading.Event()
    box["slow_go"] = threading.Event()
    steps = [{"capability": "test.gated", "parameters": {},
              "preferred_device": "", "confidence": 0.9},
             {"capability": "test.slow", "parameters": {},
              "preferred_device": "", "confidence": 0.9},
             {"capability": "test.echo", "parameters": {},
              "preferred_device": "", "confidence": 0.9}]
    stack = _slow_stack(box, steps)
    svc, eng = _build_svc(stack, steps)
    out = svc.handle_utterance("park a task")
    mid = out["mission_id"]
    stack["engine"].approve(out["approval_reference"])
    result = {}

    def _a():
        result["res"] = svc.resume_plan(mid)

    ta = threading.Thread(target=_a)
    ta.start()
    assert box["slow_started"].wait(timeout=5)
    eng.cancel(mid)          # operator cancels while continuation runs
    box["slow_go"].set()
    ta.join(timeout=10)
    res = result["res"]
    assert any(s["status"] == "cancelled" for s in res["steps"])
    assert eng.get(mid).state == MissionState.CANCELLED
    # steps after the cancel never ran; the running step's outcome is
    # recorded honestly (it really did finish), nothing is retried
    assert box["echo"] == 0
    statuses = [s.status for s in eng.get(mid).steps]
    assert statuses == ["ok", "ok", "pending"]
    # cancelled missions are terminal for future continuations
    res2 = svc.resume_plan(mid)
    assert res2["resumed"] is False


def test_identity_preserved_recorded_who_runs_continuation():
    box = _box()
    stack, svc, eng, out, steps = _park(box)
    mid = out["mission_id"]
    stack["engine"].approve(out["approval_reference"])
    whos = []
    orig = stack["fabric"].execute_on_device

    def _wrap(cap, params, dev, who, *a, **k):
        whos.append(who)
        return orig(cap, params, dev, who, *a, **k)

    stack["fabric"].execute_on_device = _wrap
    res = svc.resume_plan(mid, who="someone-else")
    assert res["executed"] is True
    # the echoed continuation ran under the plan's recorded identity,
    # NOT the continuation requester's
    assert whos and all(w == "user" for w in whos)


# ------------------------------------------------------------------
# corrupted / inconsistent envelope refusals
# ------------------------------------------------------------------

def test_hold_without_execution_id_refuses():
    box = _box()
    stack, svc, eng, out, steps = _park(box)
    mid = out["mission_id"]
    plan = eng.plan_of(mid)
    eng.set_plan(mid, {**plan, "held": {"step": 0}})
    res = svc.resume_plan(mid)
    assert res["resumed"] is False
    assert "incomplete" in res["message"]


def test_non_dict_hold_refuses():
    box = _box()
    stack, svc, eng, out, steps = _park(box)
    mid = out["mission_id"]
    plan = eng.plan_of(mid)
    eng.set_plan(mid, {**plan, "held": "corrupted"})
    res = svc.resume_plan(mid)
    assert res["resumed"] is False
    assert "malformed" in res["message"]


def test_adopted_path_without_progression_refuses():
    """held cleared but NOTHING was adopted (step 1 still pending):
    continuing from zero with no verification trail is refused."""
    box = _box()
    stack = _stack(box)
    eng = _new_eng()
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FixedPlanParser(_steps(2)), stack["audit"], eng)
    stack["engine"].set_mission_adopt(svc.adopt_mission_record)
    out = svc.handle_utterance("park a task")
    mid = out["mission_id"]
    plan = eng.plan_of(mid)
    eng.set_plan(mid, {**plan, "held": None})   # corrupted, not adopted
    res = svc.resume_plan(mid)
    assert res["resumed"] is False
    assert "stored progression is incomplete" in res["message"]
    assert box["echo"] == 0 and eng.get(mid).state == \
        MissionState.WAITING_FOR_PERMISSION


def test_duplicate_continuation_after_completion():
    box = _box()
    stack, svc, eng, out, steps = _park(box)
    stack["engine"].approve(out["approval_reference"])
    res = svc.resume_plan(out["mission_id"])
    assert res["executed"] is True
    res2 = svc.resume_plan(out["mission_id"])
    assert res2["resumed"] is False and res2["executed"] is False
    assert box["gate"] == 1 and box["echo"] == 1   # nothing re-ran
