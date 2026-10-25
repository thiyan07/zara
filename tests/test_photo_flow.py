"""Stage 31-P3 phase B (P3c) — pull-only photo backup flow fixtures."""
import threading
import time

from core.app import build_stack
from core.intent import LocalIntentParser, IntentTurnService
from core.models import DeviceKind, DeviceState
from core.execution import ExecutionState

INV = "android.photos.inventory"
INV_DOC = {"id": INV, "name": INV, "descriptor_version": "1",
           "version": "1.0.0", "risk": "confirm",
           "availability": "available"}
LOOK_DOC = {"id": "android.contacts.lookup", "name": "x",
            "descriptor_version": "1", "version": "1.0.0",
            "risk": "confirm", "availability": "available"}


def make_stack():
    return build_stack()


def register(stack, dev="phone0"):
    code = stack["device_auth"].enroll(dev, DeviceKind("android"))
    stack["device_auth"].claim(code)
    stack["devices"].register(DeviceState(
        device_id=dev, kind=DeviceKind("android"),
        capabilities=[LOOK_DOC["id"], INV], online=True,
        status="online"))
    stack["fabric"].advertise([LOOK_DOC, INV_DOC], dev)


def svc_with_flow(stack):
    from core.photo_flow import PhotoFlow
    flow = PhotoFlow(stack["fabric"], stack["engine"], stack["audit"])
    return IntentTurnService(stack["fabric"], stack["resolver"],
                             LocalIntentParser(), stack["audit"],
                             stack["missions"], None, None, None, None,
                             None, flow)


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


def test_invite_parks_per_run_then_reports_counts_and_pull_plan():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("backup my photos")
    assert out["executed"] and out.get("approval_required"), out
    eid = out["execution_id"]
    rec = stack["engine"].get(eid)
    assert rec.tool == INV
    # CONFIRM risk: the approval parks EVERY run (per-run consent).
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    approve_with_result(stack, eid, {
        "count": 812, "total_bytes": 4_120_000_000,
        "newest_ms": 1, "oldest_ms": 2,
        "folders": ["DCIM/Camera"],
        "verification": "counts only"})
    rep = svc.handle_utterance("go on")
    assert rep["executed"] and rep.get("verified") is True, rep
    msg = rep["message"]
    assert "812" in msg and "adb pull" in msg, msg
    # counts-only: no names, no delete promise.
    assert ".jpg" not in msg and "delete anything" in msg


def test_delete_flavor_refused_explicitly():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("backup my photos and delete them after")
    assert out["executed"] is False, out
    assert "DELETE" in out["message"] or "don't delete" in out["message"], out
    # no inventory exec ever ran
    assert out.get("execution_id") is None


def test_zero_library_is_honest():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("photo backup")
    approve_with_result(stack, out["execution_id"], {
        "count": 0, "total_bytes": 0, "newest_ms": -1, "oldest_ms": -1,
        "folders": [], "verification": "counts only"})
    rep = svc.handle_utterance("any update?")
    assert rep["executed"] is True, rep
    assert "empty" in rep["message"], rep["message"]


def test_cancel_before_read_closes_without_report():
    stack = make_stack()
    register(stack)
    svc = svc_with_flow(stack)
    out = svc.handle_utterance("pull my photos")
    assert out.get("approval_required"), out
    cancel = svc.handle_utterance("never mind")
    assert cancel["executed"] is False, cancel
    assert "Cancelled" in cancel["message"]
    assert svc.photo_flow._pending is None
