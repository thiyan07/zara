"""Plan continuation after an approval hold (Stage 24g).

A plan parked at WAITING_FOR_PERMISSION can be continued once the held
step's real execution record is SUCCEEDED. The continuation re-enters
the same per-step pipeline; the held step is NEVER re-executed (its
record is verified first), fetch bindings are re-fed from the stored
step results, and a mid-continuation approval parks the plan again.
Fixture-only: no devices, no desktop, no hardware.
"""
import sqlite3
import threading
import time

from fastapi.testclient import TestClient

from core.app import create_app
from core.intent import IntentProposal, IntentTurnService
from core.models import PermissionLevel, RiskLevel, ToolDefinition
from core.missions import MissionEngine, MissionState, \
    PersistentMissionEngine
from core.resolver import CapabilityResolver
from tests.test_intent import doc, enroll_claim, make_stack, register

H = {"Authorization": "Bearer dev-token"}


def _box():
    return {"gate": 0, "echo": 0, "echo_inputs": None, "gate_raise": False,
            "gate_token": "tok1"}


def _stack(box):
    """Stack with a SAFE echo tool and a CONFIRM-gated tool (returns a
    token result in its handler output)."""
    stack = make_stack()

    def _echo(inputs, ctx=None):
        box["echo"] += 1
        box["echo_inputs"] = dict(inputs or {})
        return {"ok": True, "echoed": True, "token": "tok-final"}

    def _gate(inputs, ctx=None):
        box["gate"] += 1
        if box["gate_raise"]:
            raise RuntimeError("gate blew up")
        return {"ok": True, "token": box["gate_token"]}

    stack["registry"].register(
        ToolDefinition(name="test.echo", description="echo tool",
                       input_schema={"type": "object", "properties": {
                           "x": {"type": "string"}},
                           "required": []},
                       output_schema={"type": "object"}), _echo)
    stack["registry"].register(
        ToolDefinition(name="test.gated", description="gated tool",
                       input_schema={"type": "object", "properties": {}},
                       output_schema={"type": "object"},
                       permission=PermissionLevel.CONFIRM,
                       risk=RiskLevel.CONFIRM), _gate)

    def _gate2(inputs, ctx=None):
        box["gate"] += 1
        return {"ok": True}

    stack["registry"].register(
        ToolDefinition(name="test.gated2", description="second gated tool",
                       input_schema={"type": "object", "properties": {}},
                       output_schema={"type": "object"},
                       permission=PermissionLevel.CONFIRM,
                       risk=RiskLevel.CONFIRM), _gate2)
    enroll_claim(stack, "d1")
    register(stack, "d1")
    raw = doc(cap="test.gated", description="gated tool")
    raw["risk"] = "confirm"
    raw2 = doc(cap="test.gated2", description="second gated tool")
    raw2["risk"] = "confirm"
    stack["fabric"].advertise(
        [doc(cap="test.echo", description="echo tool"), raw, raw2], "d1")
    return stack


def _svc(stack, steps, missions="default"):
    eng = MissionEngine() if missions == "default" else missions
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FixedPlanParser(steps), stack["audit"], eng)
    return svc, eng


class FixedPlanParser:
    name = "fixed-continuation"

    def __init__(self, steps):
        self._steps = steps

    def parse(self, utterance, ctx, session_context=""):
        first = self._steps[0]
        return IntentProposal(
            utterance=utterance[:500], capability=first["capability"],
            parameters=dict(first.get("parameters") or {}),
            preferred_device=first.get("preferred_device", ""),
            steps=[dict(s) for s in self._steps],
            confidence=0.9, needs_clarification=False,
            source=self.name)


def _gate_steps(n=2):
    return [{"capability": "test.gated", "parameters": {},
             "preferred_device": "", "confidence": 0.9}
            for _ in range(n)]


# ------------------------------------------------------------------
# park → stored plan + real hold binding
# ------------------------------------------------------------------

def test_park_stores_plan_and_binds_held_execution():
    box = _box()
    stack = _stack(box)
    svc, eng = _svc(stack, _gate_steps(2))
    out = svc.handle_utterance("do gated twice")
    assert out["executed"] is False
    assert out["steps"][0]["status"] == "waiting_for_permission"
    mid = out["mission_id"]
    plan = eng.plan_of(mid)
    assert plan and plan.get("resumable") is True
    held = plan.get("held") or {}
    assert held.get("step") == 0
    assert held.get("execution_id") == out["approval_reference"]
    assert held.get("tool") == "test.gated"
    assert eng.get(mid).state == MissionState.WAITING_FOR_PERMISSION
    assert box["gate"] == 0 and box["echo"] == 0  # hold ran nothing


# ------------------------------------------------------------------
# resume before / after approval
# ------------------------------------------------------------------

def test_resume_before_approval_finishes_refused():
    box = _box()
    stack = _stack(box)
    svc, eng = _svc(stack, _gate_steps(2))
    out = svc.handle_utterance("do gated twice")
    mid = out["mission_id"]
    res = svc.resume_plan(mid)
    assert res["resumed"] is False
    assert "not finished yet" in res["message"]
    assert eng.get(mid).state == MissionState.WAITING_FOR_PERMISSION
    assert box["gate"] == 0 and box["echo"] == 0  # nothing ran again


def test_resume_after_approval_finishes_plan_without_rerun():
    box = _box()
    stack = _stack(box)
    svc, eng = _svc(stack, _gate_steps(2))
    out = svc.handle_utterance("do gated twice")
    mid = out["mission_id"]
    stack["engine"].approve(out["approval_reference"])
    res = svc.resume_plan(mid)
    assert res["executed"] is True and res["resumed"] is True, res
    assert eng.get(mid).state == MissionState.COMPLETED
    assert "Done (2 steps)" in res["message"]
    # held gated step ran exactly once (under its approval, gate=1),
    # then step 2 — the same gated tool — ran once in continuation
    assert box["gate"] == 2 and box["echo"] == 0
    assert eng.get(mid).steps[0].status == "ok"


def test_resume_failed_held_record_is_honest_end():
    box = _box()
    stack = _stack(box)
    svc, eng = _svc(stack, _gate_steps(2))
    out = svc.handle_utterance("do gated twice")
    mid = out["mission_id"]
    box["gate_raise"] = True    # approval granted, dispatch then fails
    stack["engine"].approve(out["approval_reference"])
    res = svc.resume_plan(mid)
    assert res["resumed"] is True and res["executed"] is False
    assert "did not complete" in res["message"]
    assert eng.get(mid).state == MissionState.FAILED
    assert box["gate"] == 1 and box["echo"] == 0
    # terminal: continuing again is refused honestly
    res2 = svc.resume_plan(mid)
    assert res2["resumed"] is False and "can be continued" in res2["message"]


def test_resume_unknown_and_wrong_state_refuse():
    box = _box()
    stack = _stack(box)
    svc, eng = _svc(stack, _gate_steps(1))
    assert "No mission with reference" in svc.resume_plan("mission-zz")[
        "message"]
    out = svc.handle_utterance("do gated once")
    mid = out["mission_id"]
    stack["engine"].approve(out["approval_reference"])
    svc.resume_plan(mid)       # completes → terminal
    res = svc.resume_plan(mid)
    assert res["resumed"] is False and out["mission_id"] in res["message"]


def test_continuation_feeds_fetch_from_stored_step_result():
    box = _box()
    stack = _stack(box)
    svc, eng = _svc(stack, [
        {"capability": "test.gated", "parameters": {},
         "preferred_device": "", "confidence": 0.9},
        {"capability": "test.echo", "parameters": {},
         "fetch": {"x": {"from": 0, "key": "token"}},
         "preferred_device": "", "confidence": 0.9}])
    out = svc.handle_utterance("gate then echo with its token")
    assert out["executed"] is False
    stack["engine"].approve(out["approval_reference"])
    res = svc.resume_plan(out["mission_id"])
    assert res["executed"] is True, res
    # the echoed step received the FIRST run's stored result — the held
    # step was never re-executed (call count proves it)
    assert box["gate"] == 1
    assert box["echo_inputs"] == {"x": "tok1"}
    assert eng.get(out["mission_id"]).state == MissionState.COMPLETED


def test_continuation_parks_again_on_second_approval():
    box = _box()
    stack = _stack(box)
    svc, eng = _svc(stack, [
        {"capability": "test.gated", "parameters": {},
         "preferred_device": "", "confidence": 0.9},
        {"capability": "test.gated2", "parameters": {},
         "preferred_device": "", "confidence": 0.9}])
    out = svc.handle_utterance("gate twice")
    assert out["executed"] is False
    stack["engine"].approve(out["approval_reference"])
    res = svc.resume_plan(out["mission_id"])
    assert "paused again" in res["message"], res
    assert eng.get(out["mission_id"]).state == \
        MissionState.WAITING_FOR_PERMISSION
    assert res["approval_reference"] and \
        res["approval_reference"] != out["approval_reference"]
    plan = eng.plan_of(res["mission_id"])
    assert (plan.get("held") or {}).get("step") == 1
    assert (plan.get("held") or {}).get("tool") == "test.gated2"


def test_resume_record_mismatch_refuses():
    box = _box()
    stack = _stack(box)
    svc, eng = _svc(stack, _gate_steps(2))
    out = svc.handle_utterance("do gated twice")
    mid = out["mission_id"]
    plan = eng.plan_of(mid)
    # tampered hold (never happens in production; prove detection)
    plan["held"] = dict(plan["held"])
    plan["held"]["execution_id"] = "exec-ghost"
    eng.set_plan(mid, plan)
    res = svc.resume_plan(mid)
    assert res["resumed"] is False and "no longer retained" in res["message"]
    assert eng.get(mid).state == MissionState.WAITING_FOR_PERMISSION


def test_plan_without_resumable_envelope_refuses():
    box = _box()
    stack = _stack(box)
    svc, eng = _svc(stack, _gate_steps(2))
    out = svc.handle_utterance("do gated twice")
    mid = out["mission_id"]
    eng.set_plan(mid, {"resumable": False, "held": None})
    res = svc.resume_plan(mid)
    assert res["resumed"] is False and "resumable plan" in res["message"]


# ------------------------------------------------------------------
# NL continuation control (exact phrases only)
# ------------------------------------------------------------------

def test_continue_utterance_drives_latest_parked_mission():
    box = _box()
    stack = _stack(box)
    svc, eng = _svc(stack, _gate_steps(2))
    out = svc.handle_utterance("do gated twice")
    stack["engine"].approve(out["approval_reference"])
    res = svc.handle_utterance("continue the task")
    assert res["executed"] is True and res["resumed"] is True
    assert eng.get(out["mission_id"]).state == MissionState.COMPLETED


def test_continue_utterance_without_parked_task_is_honest():
    box = _box()
    stack = _stack(box)
    svc, eng = _svc(stack, _gate_steps(1))
    res = svc.handle_utterance("continue")
    assert res["executed"] is False
    assert "No task is waiting to continue" in res["message"]
    assert res.get("proposal") is None


# ------------------------------------------------------------------
# engine persistence: plan envelope + schema migration
# ------------------------------------------------------------------

def test_plan_envelope_survives_restart(tmp_path):
    path = str(tmp_path / "missions.db")
    e1 = PersistentMissionEngine(path)
    m = e1.create("old plan goal", [{"tool": "x", "inputs": {}}])
    e1.transition(m.id, MissionState.PLANNING)
    assert e1.set_plan(m.id, {"proposal": {"utterance": "u"},
                              "resumable": True, "held": None})
    e2 = PersistentMissionEngine(path)
    p = e2.plan_of(m.id)
    assert p and p.get("resumable") is True
    # terminal missions refuse re-storage (immutable end)
    e1.transition(m.id, MissionState.FAILED)
    assert e1.set_plan(m.id, {"resumable": True}) is False


def test_oversized_plan_is_refused_not_truncated(tmp_path):
    e = PersistentMissionEngine(str(tmp_path / "m.db"))
    m = e.create("goal")
    e.transition(m.id, MissionState.PLANNING)
    big = {"blob": "x" * 20000}
    assert e.set_plan(m.id, big) is False


def test_old_schema_database_migrates_in_place(tmp_path):
    path = str(tmp_path / "legacy.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE missions (id TEXT PRIMARY KEY, goal TEXT NOT NULL, "
        "state TEXT NOT NULL, device_id TEXT, steps TEXT, checkpoints TEXT, "
        "approvals TEXT, retries INT, max_retries INT, error TEXT, "
        "created_at TEXT, updated_at TEXT)")
    con.execute(
        "INSERT INTO missions VALUES ('mission-legacy','goal old','failed',"
        "NULL,'[]','[]','[]',0,3,'','2026-01-01T00:00:00','"
        "2026-01-01T00:00:01')")
    con.commit()
    con.close()
    eng = PersistentMissionEngine(path)
    m = eng.get("mission-legacy")
    assert m.state == MissionState.FAILED and m.plan is None
    # the migrated table accepts (and persists) a new plan envelope
    e2 = PersistentMissionEngine(path)
    m2 = e2.create("goal new")
    e2.transition(m2.id, MissionState.PLANNING)
    e2.set_plan(m2.id, {"resumable": True})
    assert PersistentMissionEngine(path).plan_of(m2.id) == {
        "resumable": True}
    assert eng.get("mission-legacy").goal == "goal old"


# ------------------------------------------------------------------
# HTTP endpoint
# ------------------------------------------------------------------

def _client_stack(box):
    stack = _stack(box)
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FixedPlanParser(_gate_steps(2)),
                            stack["audit"], stack["missions"])
    stack["_svc"] = svc
    return stack


def test_mission_continue_endpoint_roundtrip():
    box = _box()
    stack = _client_stack(box)
    out = stack["_svc"].handle_utterance("do gated twice")
    mid = out["mission_id"]
    c = TestClient(create_app(stack))
    # unknown mission → honest 404
    r404 = c.post("/v1/missions/mission-ghost/continue", headers=H)
    assert r404.status_code == 404
    # nothing finished yet → honest refusal body, not an execution
    r_wait = c.post(f"/v1/missions/{mid}/continue", headers=H).json()
    assert "not finished yet" in r_wait["message"]
    # approve the held record through the normal gate, then continue
    stack["engine"].approve(out["approval_reference"])
    r = c.post(f"/v1/missions/{mid}/continue", headers=H).json()
    assert r["executed"] is True and r["resumed"] is True
    assert stack["missions"].get(mid).state == MissionState.COMPLETED
    # held gated step ran once (gate=1 under approval) + step 2 gated
    # once in continuation (gate=2); nothing re-ran, nothing duplicated
    assert box["gate"] == 2 and box["echo"] == 0


def test_mission_continue_endpoint_mid_run_mission_refuses():
    box = _box()
    stack = _client_stack(box)
    # a slow SAFE first step keeps the mission mid-flight (EXECUTING)
    def _slow(inputs, ctx=None):
        time.sleep(0.35)
        return {"ok": True}
    stack["registry"].register(
        ToolDefinition(name="test.slow", description="slow safe tool",
                       input_schema={"type": "object", "properties": {}},
                       output_schema={"type": "object"}), _slow)
    stack["fabric"].advertise(
        stack["fabric"].capabilities_for("d1") +
        [doc(cap="test.slow", description="slow safe tool")], "d1")
    stack["_svc"].parser._steps = [
        {"capability": "test.slow", "parameters": {},
         "preferred_device": "", "confidence": 0.9},
        {"capability": "test.echo", "parameters": {},
         "preferred_device": "", "confidence": 0.9}]
    svc = stack["_svc"]
    out = {}
    t = threading.Thread(target=lambda: out.update(
        svc.handle_utterance("slow then echo")))
    t.start()
    deadline = time.time() + 5
    mid = ""
    while time.time() < deadline:
        ms = stack["missions"].list()
        if ms and ms[0].state.name == "EXECUTING" and \
                ms[0].id not in out:
            mid = ms[0].id
            break
        time.sleep(0.01)
    assert mid
    c = TestClient(create_app(stack))
    r = c.post(f"/v1/missions/{mid}/continue", headers=H).json()
    assert "only a plan parked" in r["message"]
    t.join(timeout=10)
    assert out["executed"] is True  # the mission itself completed fine
