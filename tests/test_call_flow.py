"""Stage 31-P3 phase B (P3d) — call with contact resolution flow tests.

The flow is DETERMINISTIC and rides the existing engine (no new
authorization path): every fixture here pins one discipline claim of
the user's standing spec —

- zero matches ask the alias question (never guessed names);
- an aliased second lookup writes the CONFIRMED fact to memory;
- exactly one match offers "Calling <name> — correct?"; the digits are
  tail-masked (result tails only; full number lives in the approval);
- "yes" submits android.call.place which STILL parks for its per-call
  approval; the flow never claims placed, only the a11y verdict does;
- raw numbers from the utterance are refused;
- the flow's executor is the phone — none/multiple is honest data.
"""
import threading
import time

import pytest

from core.app import build_stack
from core.call_flow import CallFlow
from core.fabric import FabricRegistry
from core.intent import LocalIntentParser, IntentTurnService
from core.models import DeviceKind, DeviceState, ExecutionState

LOOK = "android.contacts.lookup"
CALL = "android.call.place"
PRIV = "dev.zara.lab.privtest"
OP = {"Authorization": "Bearer dev-token"}
LOOK_DOC = {"id": LOOK, "name": LOOK, "descriptor_version": "1",
            "version": "1.0.0", "risk": "confirm",
            "availability": "available"}
CALL_DOC = {"id": CALL, "name": CALL, "descriptor_version": "1",
            "version": "1.0.0", "risk": "high_risk",
            "availability": "available"}


def make_stack():
    return build_stack()


def register(stack, dev, caps, kind="android", online=True):
    code = stack["device_auth"].enroll(dev, DeviceKind(kind))
    stack["device_auth"].claim(code)
    return stack["devices"].register(DeviceState(
        device_id=dev, kind=DeviceKind(kind),
        capabilities=list(caps), online=online, status="online"))


def advertise(stack, dev, docs):
    stack["fabric"].advertise(docs, dev)


def complete_job_like(stack, device_id, result, timeout_s=25.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = stack["jobs"].poll(device_id)
        if job is not None:
            stack["jobs"].complete(job.id, ok=True, result=result)
            return True
        time.sleep(0.05)
    return False


def svc_with_flow(stack):
    flow = CallFlow(stack["fabric"], stack["resolver"], stack["engine"],
                    stack["memory"], stack["audit"], stack["devices"])
    return IntentTurnService(stack["fabric"], stack["resolver"],
                             LocalIntentParser(), stack["audit"],
                             stack["missions"], None, None, flow)


def approve_with_stack_result(stack, eid, result):
    t = threading.Thread(target=complete_job_like,
                         args=(stack, "phone0", result), daemon=True)
    t.start()
    out = stack["engine"].approve(eid)
    t.join(timeout=30)
    return out


def matched(name, number, starred=False):
    return {"contact_id": "c1", "display_name": name,
            "number": number, "starred": starred}


def test_invite_parks_then_one_match_offers_confirm_masked():
    stack = make_stack()
    register(stack, "phone0", [LOOK, CALL])
    advertise(stack, "phone0", [LOOK_DOC, CALL_DOC])
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("call sarah chen")
    assert out["executed"] is True and out.get("approval_required"), out
    eid = out["execution_id"]
    assert stack["engine"].get(eid).state == \
        ExecutionState.WAITING_FOR_PERMISSION
    approve_with_stack_result(
        stack, eid,
        {"query": "sarah chen", "count": 1,
         "matches": [matched("Sarah Chen", "+15551230100")]})
    out2 = svc.handle_utterance("continue the task")
    msg = out2["message"]
    assert "Calling Sarah Chen" in msg, msg
    # Digits are tail-masked: the last FOUR digits ride in chat text
    # ('********0100' — an asterisk-storm plus the 0100 tail); the full
    # number never appears.
    assert "+1555" not in msg and "230100" not in msg
    assert "*" in msg and "0100" in msg
    # zero taps: no mutating call submitted yet — the flow is parked at
    # the confirm step by design.
    assert stack["engine"].get(eid).state == \
        ExecutionState.SUCCEEDED


def test_zero_match_asks_alias_then_writes_confirmed_memory():
    stack = make_stack()
    register(stack, "phone0", [LOOK, CALL])
    advertise(stack, "phone0", [LOOK_DOC, CALL_DOC])
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("call priya")
    eid = out["execution_id"]
    approve_with_stack_result(
        stack, eid, {"query": "priya", "count": 0, "matches": []})
    ask = svc.handle_utterance("continue the task")
    assert "different name" in ask["message"], ask["message"]
    # The user's confirmatory answer drives the SECOND lookup.
    out2 = svc.handle_utterance("saved as Priya Raman")
    assert out2["executed"] is True and out2.get("approval_required"), out2
    eid2 = out2["execution_id"]
    approve_with_stack_result(
        stack, eid2,
        {"query": "Priya Raman", "count": 1,
         "matches": [matched("Priya Raman", "+15559990123")]})
    confirm = svc.handle_utterance("continue the task")
    assert "Calling Priya Raman" in confirm["message"], confirm["message"]
    # the CONFIRMED alias fact is in memory with its provenance.
    hits = stack["memory"].recall("alias priya contacts",
                                  top_k=10, category="preferences")
    alias_items = [it for it, _ in hits or [] if
                   (it.metadata or {}).get("type") == "contact_alias"]
    assert len(alias_items) == 1, alias_items
    it = alias_items[0]
    assert it.metadata["alias"] == "priya"
    assert it.metadata["contact_name"] == "Priya Raman"


def test_confirm_yes_still_requires_per_call_approval_then_reports():
    stack = make_stack()
    register(stack, "phone0", [LOOK, CALL])
    advertise(stack, "phone0", [LOOK_DOC, CALL_DOC])
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("call sarah chen")
    approve_with_stack_result(
        stack, out["execution_id"],
        {"query": "sarah chen", "count": 1,
         "matches": [matched("Sarah Chen", "+15551230100")]})
    svc.handle_utterance("continue the task")     # confirm offer
    yes = svc.handle_utterance("yes")
    assert yes["executed"] is True and yes.get("approval_required"), yes
    eid_call = yes["execution_id"]
    rec = stack["engine"].get(eid_call)
    # the per-call approval gate holds BEFORE dispatch (nothing dialed).
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    assert rec.tool == CALL
    approve_with_stack_result(
        stack, eid_call,
        {"fired": True, "verdict": "confirmed",
         "verified": True, "number_tail": "*0100"})
    done = svc.handle_utterance("any update?")
    assert done["verified"] is True, done
    assert "call screen" in done["message"], done["message"]
    # the result never echoes the number.
    assert "15551230100" not in repr(stack["engine"].get(eid_call).result)
    # mission-free driver: audit rows never carry the full number.
    audit_repr = repr(stack["audit"].query(200))
    assert "15551230100" not in audit_repr


def test_call_refusal_paths_are_data_never_guessed():
    stack = make_stack()
    register(stack, "phone0", [LOOK, CALL])
    advertise(stack, "phone0", [LOOK_DOC, CALL_DOC])
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("call sarah chen")
    approve_with_stack_result(
        stack, out["execution_id"],
        {"query": "sarah chen", "count": 1,
         "matches": [matched("Sarah Chen", "+15551230100")]})
    svc.handle_utterance("continue the task")
    yes = svc.handle_utterance("yes")
    # An honest "fired but no call screen" is NOT placed:
    approve_with_stack_result(
        stack, yes["execution_id"],
        {"fired": True, "verdict": "absent",
         "verified": False, "number_tail": "*0100"})
    done = svc.handle_utterance("any update?")
    assert done["verified"] is False, done
    assert "NOT placed" in done["message"], done["message"]


def test_raw_number_refused_multiple_matches_ask_disambiguation():
    stack = make_stack()
    register(stack, "phone0", [LOOK, CALL])
    advertise(stack, "phone0", [LOOK_DOC, CALL_DOC])
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("call 555 1234567")
    assert out.get("refused") is True
    assert "raw numbers" in out["message"].lower()
    out2 = svc.handle_utterance("call sarah")
    eid = out2["execution_id"]
    approve_with_stack_result(
        stack, eid,
        {"query": "sarah", "count": 2,
         "matches": [
             matched("Sarah Chen", "+15551230100"),
             matched("Sarah Ouyang", "+15558761111", starred=True)]})
    dis = svc.handle_utterance("continue the task")
    assert "Which one?" in dis["message"], dis["message"]
    assert "Sarah Ouyang" in dis["message"]
    pick = svc.handle_utterance("2")
    assert "Calling Sarah Ouyang" in pick["message"], pick["message"]
    cancel = svc.handle_utterance("no")
    assert "Cancelled" in cancel["message"], cancel["message"]
    assert stack["jobs"].poll("phone0") is None  # nothing dispatched


def test_no_phone_is_honest_no_memory_write_and_lookup_falls_back():
    stack = make_stack()
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("call sarah")
    assert "No phone is online" in out["message"], out["message"]
    assert out["executed"] is False
    # no memory noise from the failed flow
    hits = stack["memory"].recall("alias", top_k=5)
    assert not [it for it, _ in hits or [] if
                (it.metadata or {}).get("type") == "contact_alias"]


def test_alias_memory_resolves_future_calls():
    stack = make_stack()
    register(stack, "phone0", [LOOK, CALL])
    advertise(stack, "phone0", [LOOK_DOC, CALL_DOC])
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("call priya")
    approve_with_stack_result(
        stack, out["execution_id"], {"query": "priya", "count": 0,
                                     "matches": []})
    svc.handle_utterance("continue the task")
    out2 = svc.handle_utterance("saved as Priya Raman")
    approve_with_stack_result(
        stack, out2["execution_id"],
        {"query": "Priya Raman", "count": 1,
         "matches": [matched("Priya Raman", "+15559990123")]})
    svc.handle_utterance("continue the task")     # confirm offered
    svc.handle_utterance("no")                    # cancel — memory wrote
    # A future "call priya" uses the alias memory: the SECOND lookup is
    # issued for the RESOLVED name, not the alias.
    fut = svc.handle_utterance("call priya")
    assert fut["executed"] is True
    rec = stack["engine"].get(fut["execution_id"])
    # The alias memory resolved BEFORE any device lookup: the parked
    # lookup's input is the RESOLVED contact name, never the alias.
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    assert rec.tool == LOOK
    assert rec.inputs["name"] == "Priya Raman", rec.inputs
    approve_with_stack_result(
        stack, rec.id,
        {"query": "Priya Raman", "count": 1,
         "matches": [matched("Priya Raman", "+15559990123")]})
    follow = svc.handle_utterance("continue the task")
    assert "Calling Priya Raman" in follow["message"], follow["message"]
    svc.handle_utterance("no")
