"""Stage 31-P3 phase B (P3e) — native carrier SMS flow tests.

Same spine as the call flow (shared contacts lookup + confirmed alias
memory + per-flow pending ring), different final surface. Each fixture
pins one discipline claim:

- the full draft echoes in CHAT and in the APPROVAL INPUTS, but NEVER
  in audit rows and NEVER in the job result (length-only);
- raw-number recipients are refused before any device touch;
- "yes" submits android.sms.send (HIGH risk) which STILL parks for its
  own approval — double gate;
- the honest verdict chain: parked → radio-accepted "sent" reported as
  SENT-UNCONFIRMED (delivery is NOT confirmable) — never "delivered";
- the bare 'text <name>' invite asks for a draft; cancel words never
  become a draft; retyping replaces, never sends;
- a >400-char draft is refused, not truncated.
"""
import threading
import time

from core.app import build_stack
from core.call_flow import SmsFlow
from core.intent import LocalIntentParser, IntentTurnService
from core.models import DeviceKind, DeviceState, ExecutionState

LOOK = "android.contacts.lookup"
SEND = "android.sms.send"
LOOK_DOC = {"id": LOOK, "name": LOOK, "descriptor_version": "1",
            "version": "1.0.0", "risk": "confirm",
            "availability": "available"}
SEND_DOC = {"id": SEND, "name": SEND, "descriptor_version": "1",
            "version": "1.0.0", "risk": "high_risk",
            "availability": "available"}
MATCHES1 = [{"contact_id": "c1", "display_name": "Sarah Chen",
             "number": "+15551230100", "starred": False}]


def make_stack():
    return build_stack()


def register(stack, dev="phone0"):
    code = stack["device_auth"].enroll(dev, DeviceKind("android"))
    stack["device_auth"].claim(code)
    stack["devices"].register(DeviceState(
        device_id=dev, kind=DeviceKind("android"),
        capabilities=[LOOK, SEND], online=True, status="online"))
    stack["fabric"].advertise([LOOK_DOC, SEND_DOC], dev)


def svc_with_flow(stack):
    flow = SmsFlow(stack["fabric"], stack["resolver"], stack["engine"],
                   stack["memory"], stack["audit"], stack["devices"])
    return IntentTurnService(stack["fabric"], stack["resolver"],
                             LocalIntentParser(), stack["audit"],
                             stack["missions"], None, None, flow)


def approve_with_stack_result(stack, eid, result):
    t = threading.Thread(target=lambda: _drain(stack, result), daemon=True)
    t.start()
    out = stack["engine"].approve(eid)
    t.join(timeout=30)
    return out


def _drain(stack, result):
    deadline = time.time() + 25.0
    while time.time() < deadline:
        job = stack["jobs"].poll("phone0")
        if job is not None:
            stack["jobs"].complete(job.id, ok=True, result=result)
            return
        time.sleep(0.05)


def test_full_path_park_confirm_send_honest_sent_unconfirmed():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance(
        "text sarah chen saying I'm running 20 minutes late")
    assert out["executed"] is True and out.get("approval_required"), out
    eid = out["execution_id"]
    approve_with_stack_result(
        stack, eid, {"query": "sarah chen", "count": 1,
                     "matches": MATCHES1})
    confirm = svc.handle_utterance("continue the task")
    msg = confirm["message"]
    # The DRAFT echoes in chat (the user reviews exactly what goes);
    # the number is tail-masked.
    assert "Send this SMS to Sarah Chen" in msg, msg
    assert "I'm running 20 minutes late" in msg
    assert "0100" in msg and "1555123" not in msg.replace("0100", "")
    yes = svc.handle_utterance("yes")
    assert yes["executed"] is True and yes.get("approval_required"), yes
    rec = stack["engine"].get(yes["execution_id"])
    # the DOUBLE gate: the per-send approval holds before dispatch.
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    assert rec.tool == SEND
    # the approval card sees the full draft (inputs);
    assert rec.inputs["text"] == "I'm running 20 minutes late"
    assert rec.inputs["number"] == "+15551230100"
    approve_with_stack_result(
        stack, yes["execution_id"],
        {"verdict": "sent", "text_len": 27, "number_tail": "*0100"})
    done = svc.handle_utterance("any update?")
    assert done["verified"] is False, done
    assert "SENT-UNCONFIRMED" in done["message"], done["message"]
    # privacy: the DRAFT text never lands in audit; the RESULT carries
    # length only; the full number never enters results.
    audit_repr = repr(stack["audit"].query(200))
    assert "running 20 minutes late" not in audit_repr
    assert "15551230100" not in repr(rec.result)
    assert rec.result["text_len"] == 27


def test_bare_invite_asks_draft_and_cancel_words_never_send():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("text sarah chen")
    eid = out["execution_id"]
    approve_with_stack_result(
        stack, eid, {"query": "sarah chen", "count": 1,
                     "matches": MATCHES1})
    ask = svc.handle_utterance("continue the task")
    assert "what should the text say?" in ask["message"], ask
    # a cancel-shaped utterance is treated AS cancel, never as a draft
    cancel = svc.handle_utterance("never mind")
    assert cancel["executed"] is False, cancel
    assert "no draft was written" in cancel["message"], cancel["message"]
    # nothing was ever dispatched / nothing sent
    assert stack["jobs"].poll("phone0") is None


def test_draft_captured_retyped_then_refused_overlong():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("text sarah chen")
    approve_with_stack_result(
        stack, out["execution_id"], {"query": "sarah chen", "count": 1,
                                     "matches": MATCHES1})
    ask = svc.handle_utterance("continue the task")
    assert "what should the text say?" in ask["message"], ask
    cap = svc.handle_utterance("on my way")
    assert "Draft captured: 'on my way'" in cap["message"], cap
    ret = svc.handle_utterance("on my way, see you at 8")
    assert "Draft replaced" in ret["message"], ret
    long_draft = "x" * 401
    ref = svc.handle_utterance(long_draft)
    assert ref["executed"] is False and ref.get("refused") is True, ref
    assert "400" in ref["message"]
    # the flow stays alive at draft stage — retype a good one
    okd = svc.handle_utterance("short one")
    assert "Draft replaced" in okd["message"], okd
    go = svc.handle_utterance("send it")
    assert "Send this SMS to Sarah Chen" in go["message"], go


def test_raw_number_recipient_refused_and_fallthrough():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("text +15551230100 saying hi")
    assert out.get("refused") is True, out
    assert "raw numbers" in out["message"].lower()
    # unrelated word falls through to the parser (no SMS kiss-of-death)
    fall = svc.handle_utterance("textbook definition")
    assert fall.get("refused") is None, fall
    assert stack["jobs"].poll("phone0") is None
