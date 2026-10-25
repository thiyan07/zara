"""Stage 31-P3 phase B (P3f) — OTP per-use-consent flow fixtures."""
import threading
import time

from core.app import build_stack
from core.intent import LocalIntentParser, IntentTurnService
from core.models import DeviceKind, DeviceState
from core.execution import ExecutionState

READ = "android.otp.read"
READ_DOC = {"id": READ, "name": READ, "descriptor_version": "1",
            "version": "1.0.0", "risk": "high_risk",
            "availability": "available"}
CAND1 = [{"posted_at_ms": 0, "age_ms": 3000,
          "package": "com.google.android.gms",
          "title": "Google Verify", "code": "483920"}]


def make_stack():
    return build_stack()


def register(stack, dev="phone0"):
    from tests.test_call_flow import LOOK_DOC
    code = stack["device_auth"].enroll(dev, DeviceKind("android"))
    stack["device_auth"].claim(code)
    stack["devices"].register(DeviceState(
        device_id=dev, kind=DeviceKind("android"),
        capabilities=[LOOK_DOC["id"], READ], online=True,
        status="online"))
    stack["fabric"].advertise([LOOK_DOC, READ_DOC], dev)


def svc_with_flow(stack):
    from core.otp_flow import OtpFlow
    flow = OtpFlow(stack["fabric"], stack["engine"], stack["audit"])
    return IntentTurnService(stack["fabric"], stack["resolver"],
                             LocalIntentParser(), stack["audit"],
                             stack["missions"], None, None, None, None,
                             flow)


def approve_with_result(stack, eid, result):
    def drain():
        deadline = time.time() + 25.0
        while time.time() < deadline:
            job = stack["jobs"].poll("phone0")
            if job is not None:
                stack["jobs"].complete(job.id, ok=True, result=result)
                return
            time.sleep(0.05)
    t = threading.Thread(target=drain, daemon=True)
    t.start()
    out = stack["engine"].approve(eid)
    t.join(timeout=30)
    return out


def test_invite_parks_per_use_then_reports_code():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("read the otp")
    assert out["executed"] and out.get("approval_required"), out
    eid = out["execution_id"]
    rec = stack["engine"].get(eid)
    assert rec.tool == READ
    # HIGH-risk: per-use consent — the approval parks EVERY run.
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    approve_with_result(stack, eid, {
        "count": 1, "fresh_count": 2,
        "candidates": CAND1,
        "verification": "fresh-window ring scan"})
    rep = svc.handle_utterance("any update?")
    assert rep["executed"] and rep.get("verified") is True, rep
    assert "483920" in rep["message"], rep["message"]
    assert "gms" in rep["message"]
    # stack-privacy: the CODE never enters memory or audit rows.
    audit_repr = repr(stack["audit"].query(200))
    assert "483920" not in audit_repr
    # the flow's own audit rows carry counts only.
    assert any("otp_flow.reported" in r["action"]
               for r in stack["audit"].query(50))


def test_zero_fresh_is_honest_data_not_guess():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("what's the verification code")
    approve_with_result(stack, out["execution_id"], {
        "count": 0, "fresh_count": 0, "candidates": []})
    rep = svc.handle_utterance("go on")
    assert rep["executed"] is True, rep
    assert "No one-time code" in rep["message"], rep["message"]


def test_sender_hint_rides_the_inputs_and_cancel_never_reads():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("read the otp from google")
    rec = stack["engine"].get(out["execution_id"])
    assert rec.inputs.get("sender_hint") == "google", rec.inputs
    # cancel BEFORE reading: the pending drain closes without report.
    cancel = svc.handle_utterance("never mind")
    assert cancel["executed"] is False, cancel
    assert "won't read" in cancel["message"]
    # the pending flow is gone: a follow-up invite starts a NEW read.
    assert svc.otp_flow._pending is None


def test_fallthrough_leaves_non_otp_to_parser():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("read my battery")
    assert out.get("otp_flow") is None, out
