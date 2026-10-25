"""Stage 31-P3 phase B (P3b) — clipboard mission flow fixtures."""
import threading
import time

from core.app import build_stack
from core.intent import LocalIntentParser, IntentTurnService
from core.models import DeviceKind, DeviceState
from core.execution import ExecutionState

CLIP = "android.clipboard.set"
DOC = {"id": CLIP, "name": CLIP, "descriptor_version": "1",
       "version": "1.0.0", "risk": "confirm", "availability": "available"}


def make_stack():
    return build_stack()


def register(stack, dev="phone0"):
    code = stack["device_auth"].enroll(dev, DeviceKind("android"))
    stack["device_auth"].claim(code)
    stack["devices"].register(DeviceState(
        device_id=dev, kind=DeviceKind("android"),
        capabilities=[CLIP], online=True, status="online"))
    stack["fabric"].advertise([DOC], dev)


def svc_with_flow(stack):
    from core.clipboard_flow import ClipboardFlow
    flow = ClipboardFlow(stack["fabric"], stack["engine"], stack["audit"])
    return IntentTurnService(stack["fabric"], stack["resolver"],
                             LocalIntentParser(), stack["audit"],
                             stack["missions"], None, None, None, None,
                             None, None, flow)


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


def test_capture_confirm_parked_write_report():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("copy redemptions live gate zara to my phone")
    # capture: confirm offer shows the text; nothing submitted yet
    assert out["executed"] is False, out
    assert "clipboard_flow_pending" in out, out
    assert "redemptions live gate zara" in out["message"], out["message"]
    # confirm: parks the write
    yes = svc.handle_utterance("yes")
    assert yes["executed"] and yes.get("approval_required"), yes
    eid = yes["execution_id"]
    rec = stack["engine"].get(eid)
    assert rec.tool == CLIP
    assert rec.inputs.get("text") == \
        "redemptions live gate zara", rec.inputs   # full text in the CARD only
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    approve_with_result(stack, eid, {
        "verdict": "written", "text_len": 24,
        "verified": True, "text_len_only": True})
    rep = svc.handle_utterance("any update?")
    assert rep["executed"] and rep.get("verified") is True, rep
    # the TEXT never returns in results or audit rows — length only
    audit_repr = repr(stack["audit"].query(200))
    assert "redemptions" not in audit_repr
    assert any("clip_flow.reported" in r["action"]
               for r in stack["audit"].query(50))


def test_cancel_keeps_clipboard_untouched():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    svc.handle_utterance("copy secret code 4488 to my phone")
    out = svc.handle_utterance("no")
    assert out["executed"] is False and "untouched" in out["message"], out
    assert svc.clipboard_flow._pending is None
    # "secret code 4488" never reached audit
    assert "4488" not in repr(stack["audit"].query(100))


def test_free_text_never_becomes_content_on_drain():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    svc.handle_utterance("copy alpha bravo to my phone")
    out = svc.handle_utterance("tell me the weather instead")
    assert out["executed"] is False, out
    assert "'yes'" in out["message"], out["message"]
    # the weather line NEVER became the clipboard payload
    assert "weather" not in repr(
        stack["audit"].query(100))


def test_overlong_payload_refuses_at_the_mouth():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    big = "x" * 5000
    out = svc.handle_utterance(f"copy {big} to my phone")
    assert out["executed"] is False, out
    assert "bounded" in out["message"], out["message"]
