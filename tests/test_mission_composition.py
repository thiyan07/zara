"""Mission-composition unit: typed result passing between steps,
progress/partial reporting, mission HTTP surface, and device-scoped
capability discovery.

CORE-UNIT: real core chain (FakePlanParser -> IntentTurnService ->
resolver -> fabric -> engine), stub-registered bounded tools. The
"fetch" mechanism is dataflow plumbing, not an authority: fetched
values still have to pass the target tool's registered schema, and a
missing source step/key stops the plan without inventing values.
"""
import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.models import ExecutionState, MissionState, ToolDefinition
from tests.test_intent import doc, enroll_claim, make_stack, register

OP = {"Authorization": "Bearer dev-token"}


def _stack_with_tools(calls=None, boom=False):
    """Stack with:
    - test.emit  -> {'value': 42}
    - test.mirror(x: integer) -> {'echoed': x}
    - test.boom  (optional, always raises)
    registered + advertised on device d1. `calls` records executed
    inputs per tool through proper closures."""
    stack = make_stack()

    def emit(inputs, ctx=None):
        if calls is not None:
            calls["emit"] = dict(inputs)
        return {"value": 42}

    def mirror(inputs, ctx=None):
        if calls is not None:
            calls["mirror"] = dict(inputs)
        return {"echoed": inputs.get("x")}

    stack["registry"].register(
        ToolDefinition(name="test.emit", description="result source",
                       input_schema={"type": "object", "properties": {}},
                       output_schema={"type": "object"}), emit)
    stack["registry"].register(
        ToolDefinition(name="test.mirror", description="echo typed param",
                       input_schema={"type": "object",
                                     "required": ["x"],
                                     "properties": {
                                         "x": {"type": "integer"}}},
                       output_schema={"type": "object"}), mirror)
    if boom:
        stack["registry"].register(
            ToolDefinition(name="test.boom", description="always fails",
                           input_schema={"type": "object",
                                         "properties": {}},
                           output_schema={"type": "object"}),
            lambda inputs, ctx=None: (_ for _ in ()).throw(
                RuntimeError("boom")))
    enroll_claim(stack, "d1")
    register(stack, "d1")
    caps = [doc(cap="test.emit"), doc(cap="test.mirror")]
    if boom:
        caps.append(doc(cap="test.boom"))
    stack["fabric"].advertise(caps, "d1")
    return stack


def _steps(spec):
    return [{"capability": s[0],
             "parameters": s[1] if len(s) > 1 else {},
             "fetch": (s[2] if len(s) > 2 else None),
             "preferred_device": "", "confidence": 0.9} for s in spec]


def _turn(stack, steps, calls=None):
    from tests.test_intent_plans import FakePlanParser
    from core.intent import IntentTurnService
    from core.resolver import CapabilityResolver
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FakePlanParser(steps), stack["audit"],
                            stack["missions"])
    return svc.handle_utterance("run the plan")


def test_F1_step_result_passes_to_later_step():
    calls = {}
    stack = _stack_with_tools(calls)
    steps = _steps([("test.emit", {}), ("test.mirror", {},
                                       {"x": {"from": 0,
                                              "key": "value"}})])
    out = _turn(stack, steps, calls)
    assert out["executed"] is True, out
    assert all(o["status"] == "ok" for o in out["steps"]), out
    # The typed binding carried the prior step's REAL result into step 2.
    assert calls["mirror"] == {"x": 42}
    m = stack["missions"].get(out["mission_id"])
    assert m.state == MissionState.COMPLETED
    assert m.steps[1].status == "ok"


def test_F2_fetch_missing_key_refuses_without_inventing():
    calls = {}
    stack = _stack_with_tools(calls)
    steps = _steps([("test.emit", {}), ("test.mirror", {},
                                       {"x": {"from": 0,
                                              "key": "absent"}})])
    out = _turn(stack, steps, calls)
    assert calls.get("mirror") is None  # never executed
    assert out["steps"][-1]["status"] == "failed"
    assert "absent" in (out["message"] or "")
    m = stack["missions"].get(out["mission_id"])
    assert m.state == MissionState.FAILED


@pytest.mark.parametrize("bad_from", [1, 2, -1, "2x"])
def test_F3_fetch_self_or_future_or_bogus_index_refused(bad_from):
    calls = {}
    stack = _stack_with_tools(calls)
    steps = _steps([("test.emit", {}), ("test.mirror", {},
                                       {"x": {"from": bad_from,
                                              "key": "value"}})])
    out = _turn(stack, steps, calls)
    assert out["steps"][-1]["status"] == "failed", out
    assert calls.get("mirror") is None


def test_F4_fetch_from_failed_source_never_speculates():
    calls = {}
    stack = _stack_with_tools(calls, boom=True)
    steps = _steps([("test.boom", {}), ("test.mirror", {},
                                       {"x": {"from": 0,
                                              "key": "value"}})])
    out = _turn(stack, steps, calls)
    assert out["executed"] is False
    assert len(out["steps"]) == 1  # step 2 never attempted
    assert "failed" in out["steps"][-1]["detail"]
    assert calls.get("mirror") is None


def test_F5_schema_gate_stays_final_for_fetched_values():
    calls = {}
    stack = _stack_with_tools(calls, boom=False)
    # emit returns value=42 (int); bind it into a tool whose schema
    # demands a STRING: fetch is plumbing only — the registered schema
    # still refuses non-conforming inputs (no silent coercion).
    stack["registry"].register(
        ToolDefinition(name="test.wants_str",
                       description="string param only",
                       input_schema={"type": "object",
                                     "required": ["s"],
                                     "properties": {
                                         "s": {"type": "string",
                                               "maxLength": 8}}},
                       output_schema={"type": "object"}),
        lambda inputs, ctx=None: {"ok": True})
    stack["fabric"].advertise(
        [doc(cap="test.emit"), doc(cap="test.wants_str")], "d1")
    steps = _steps([("test.emit", {}), ("test.wants_str", {},
                                       {"s": {"from": 0,
                                              "key": "value"}})])
    out = _turn(stack, steps, calls)
    assert out["steps"][-1]["status"] == "failed", out
    # Step 1 DID run; the failed binding is a partial success and the
    # mission must end FAILED, never silently coerced.
    assert out["partial"] is True
    m = stack["missions"].get(out["mission_id"])
    assert m.state == MissionState.FAILED


def test_F6_progress_and_partial_reporting():
    calls = {}
    stack = _stack_with_tools(calls, boom=True)
    steps = _steps([("test.emit", {}), ("test.emit", {}),
                    ("test.boom", {}), ("test.emit", {})])
    out = _turn(stack, steps, calls)
    p = out["progress"]
    assert p["total"] == 4 and p["done"] == 2
    assert p["statuses"] == ["ok", "ok", "failed"]
    assert p["remaining"] == 1
    assert out["partial"] is True
    assert "partial" in out["message"].lower()
    m = stack["missions"].get(out["mission_id"])
    assert m.state == MissionState.FAILED


def test_F7_progress_done_full_plan_success():
    calls = {}
    stack = _stack_with_tools(calls)
    steps = _steps([("test.emit", {}),
                    ("test.mirror", {"x": 7})])  # literal, no fetch
    out = _turn(stack, steps, calls)
    p = out["progress"]
    assert (p["done"], p["total"]) == (2, 2)
    assert p["statuses"] == ["ok", "ok"]
    assert out["partial"] is False
    assert out["executed"] is True


# ---------- mission HTTP surface ----------

def _client():
    stack = build_stack(fabric_db="")
    return TestClient(create_app(stack)), stack


def test_M1_mission_create_get_transition_cancel_flow():
    client, stack = _client()
    r = client.post("/v1/missions", headers=OP,
                    json={"goal": "two-step plan", "steps": [
                        {"tool": "test.emit", "inputs": {}},
                        {"tool": "test.mirror", "inputs": {"x": 1}}]})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    assert r.json()["state"] == MissionState.PLANNING.value
    got = client.get(f"/v1/missions/{mid}", headers=OP)
    assert got.status_code == 200
    assert got.json()["id"] == mid
    assert any(m["id"] == mid for m in
               client.get("/v1/missions", headers=OP).json())
    t = client.post(f"/v1/missions/{mid}/transition?to=cancelled",
                    headers=OP)
    assert t.status_code == 200
    assert stack["missions"].get(mid).state == MissionState.CANCELLED


def test_M2_mission_get_unknown_is_404():
    client, _ = _client()
    assert client.get("/v1/missions",
                      headers=OP).status_code == 200
    assert client.get("/v1/missions/nope-1234",
                      headers=OP).status_code == 404


def test_M3_mission_endpoints_require_auth():
    client, _ = _client()
    assert client.get("/v1/missions").status_code in (401, 403)
    assert client.get("/v1/missions/x").status_code in (401, 403)


# ---------- structured plan endpoint (typed mission submission) ----------

def _plan_client():
    stack = build_stack(fabric_db="")
    calls = {}
    client_stack = _stack_with_tools(calls)
    return TestClient(create_app(client_stack)), client_stack, calls


def test_P1_structured_plan_with_fetch_via_http():
    client, stack, calls = _plan_client()
    r = client.post("/v1/intent/plan", headers=OP, json={
        "utterance": "emit then mirror",
        "steps": [
            {"capability": "test.emit", "parameters": {},
             "preferred_device": ""},
            {"capability": "test.mirror", "parameters": {},
             "fetch": {"x": {"from": 0, "key": "value"}},
             "preferred_device": ""}]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["executed"] is True
    assert all(o["status"] == "ok" for o in out["steps"]), out["steps"]
    assert out["progress"]["done"] == 2
    assert out["partial"] is False
    # The validated result actually flowed step 1 -> step 2.
    assert calls["mirror"] == {"x": 42}
    # Mission is queryable and COMPLETED.
    got = client.get(f"/v1/missions/{out['mission_id']}", headers=OP)
    assert got.status_code == 200
    assert got.json()["state"] == MissionState.COMPLETED.value


def test_P2_structured_plan_bounded():
    client, _, _ = _plan_client()
    r = client.post("/v1/intent/plan", headers=OP,
                    json={"steps": []})
    assert r.status_code == 400
    r = client.post("/v1/intent/plan", headers=OP, json={
        "steps": [{"capability": "test.emit", "parameters": {}}] * 9})
    assert r.status_code == 400


def test_P3_structured_unknown_capability_fails_honestly():
    client, stack, calls = _plan_client()
    r = client.post("/v1/intent/plan", headers=OP, json={
        "steps": [{"capability": "test.emit", "parameters": {},
                   "preferred_device": ""},
                  {"capability": "test.never_registered",
                   "parameters": {}, "preferred_device": ""}]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["executed"] is True  # step 1 ran; step 2 refused
    assert out["steps"][-1]["status"] != "ok"
    assert out["partial"] is True

    mg = client.get(f"/v1/missions/{out['mission_id']}", headers=OP)
    assert mg.json()["state"] == MissionState.FAILED.value


def test_P4_structured_plan_requires_auth():
    client, _, _ = _plan_client()
    assert client.post("/v1/intent/plan",
                       json={"steps": [{"capability": "x"}]}).status_code \
        in (401, 403)


# ---------- device-scoped capability discovery (builtin) ----------

def _fabric_stack():
    stack = build_stack(fabric_db="")
    from core.models import DeviceKind, DeviceState
    for dev, kind, caps in (("laptop-1", "linux", ["system.battery"]),
                            ("android-phone", "android",
                             ["system.battery"])):
        code = stack["device_auth"].enroll(dev, DeviceKind(kind))
        stack["device_auth"].claim(code)
        stack["devices"].register(DeviceState(
            device_id=dev, kind=DeviceKind(kind),
            capabilities=caps, online=True, status="online"))
    return stack


def test_D1_device_capabilities_answering_from_fabric_truth():
    stack = _fabric_stack()
    # advertise a couple of caps on the laptop
    stack["fabric"].advertise(
        [doc(cap="system.battery", description="battery read"),
         doc(cap="desktop.window.list",
             description="window listing")], "laptop-1")
    out = stack["intent"].handle_utterance(
        "what can you do on my laptop", "user")
    assert "battery" in out["message"].lower(), out["message"][:200]
    assert out["executed"] is False  # discovery, never an action


def test_D2_device_capabilities_ambiguous_device_asks():
    stack = _fabric_stack()
    stack["fabric"].advertise(
        [doc(cap="system.battery")], "laptop-1")
    stack["fabric"].advertise(
        [doc(cap="system.battery")], "android-phone")
    out = stack["intent"].handle_utterance(
        "device capabilities", "user")
    assert "which device" in out["message"].lower()


def test_D3_device_capabilities_unknown_device_honest():
    stack = _fabric_stack()
    from core.intent import IntentProposal, build_context
    # Direct proposal path (the same one handle_utterance would get from
    # a parsing layer): an unknown device hint must be refused honestly,
    # never answered with invented capabilities.
    proposal = IntentProposal(
        utterance="what can you do on ghost-9",
        capability="device.capabilities",
        preferred_device="ghost-9", confidence=0.9,
        needs_clarification=False, source="test")
    out = stack["intent"]._local_answer(
        proposal, build_context(stack["fabric"], ""))
    assert "don't know that device" in out.lower() \
        or "paired" in out.lower()
