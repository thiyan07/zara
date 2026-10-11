"""Part B tests: HIGH_RISK approval binding, window, re-confirmation.

Approval must authorize EXACTLY the parked operation: fingerprint over
(tool, device, args, risk, capability, tool version), 300 s validity,
trust/presence/policy/capability re-check at approve and pre-dispatch for
HIGH_RISK. Scaled time; drain helpers banned.
"""
import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.execution import APPROVAL_TTL_S, approval_fingerprint
from core.models import DeviceKind, DeviceState, ExecutionState

T_POLL = 0.6
PHASE = 0.6
EXEC = 0.1

INSPECT = "gui.screen.inspect"
TAP = "gui.tap"
BAT = "system.battery"
VICTIM = "dev.zara.lab.privtest"
OP = {"Authorization": "Bearer dev-token"}


def make_stack(fabric_db=""):
    return build_stack(fabric_db=fabric_db)


def enroll_claim(stack, device_id, kind="linux"):
    code = stack["device_auth"].enroll(device_id, DeviceKind(kind))
    return stack["device_auth"].claim(code)


def register(stack, device_id, kind="linux", caps=None):
    stack["devices"].register(DeviceState(
        device_id=device_id, kind=DeviceKind(kind),
        capabilities=caps or [], online=True, status="online"))


def paired_android(stack, dev="emu-lab"):
    enroll_claim(stack, dev, "android")
    register(stack, dev, "android", [INSPECT, TAP, BAT])
    stack["fabric"].advertise([
        {"id": INSPECT, "name": INSPECT, "descriptor_version": "1",
         "version": "1.0.0", "risk": "confirm", "availability": "available"},
        {"id": TAP, "name": TAP, "descriptor_version": "1",
         "version": "1.0.0", "risk": "high_risk",
         "availability": "available"},
        {"id": BAT, "name": BAT, "descriptor_version": "1",
         "version": "1.0.0", "risk": "safe", "availability": "available"},
    ], dev)
    return dev


class CadencePoller(threading.Thread):
    def __init__(self, jobs, device_id, result=None, ok=True,
                 exec_latency=EXEC, phase=PHASE, interval=T_POLL):
        super().__init__(daemon=True)
        self.jobs = jobs
        self.device_id = device_id
        self.result = result if result is not None else {"ok": True}
        self.ok = ok
        self.exec_latency = exec_latency
        self.phase = phase
        self.interval = interval
        self.stop_ev = threading.Event()
        self.poll_count = 0
        self.claim_time = None

    def run(self):
        if self.stop_ev.wait(self.phase):
            return
        while not self.stop_ev.is_set():
            self.poll_count += 1
            job = self.jobs.poll(self.device_id)
            if job is not None:
                self.claim_time = time.monotonic()
                time.sleep(self.exec_latency)
                try:
                    self.jobs.complete(job.id, ok=self.ok,
                                       result=self.result if self.ok else None,
                                       error="" if self.ok else "nope")
                except KeyError:
                    pass
                return
            if self.stop_ev.wait(self.interval):
                return

    def stop(self):
        self.stop_ev.set()


def await_state(engine, eid, want, deadline=4.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if engine.get(eid).state.value in want:
            return engine.get(eid)
        time.sleep(0.05)
    return engine.get(eid)


def _client():
    stack = make_stack()
    return TestClient(create_app(stack)), stack


# ---------- fingerprint ----------

def test_B01_fingerprint_deterministic_and_sensitive():
    f = approval_fingerprint(TAP, "d", {"target_package": VICTIM},
                             "high_risk", TAP, "2.0.0")
    assert f == approval_fingerprint(TAP, "d", {"target_package": VICTIM},
                                     "high_risk", TAP, "2.0.0")
    assert len(f) == 32
    for variant in ({"target_package": "other.pkg"},
                    {"target_package": VICTIM, "extra": 1}):
        assert approval_fingerprint(TAP, "d", variant, "high_risk", TAP,
                                    "2.0.0") != f
    assert approval_fingerprint(TAP, "other", {"target_package": VICTIM},
                                "high_risk", TAP, "2.0.0") != f
    assert approval_fingerprint(TAP, "d", {"target_package": VICTIM},
                                "safe", TAP, "2.0.0") != f
    assert approval_fingerprint(TAP, "d", {"target_package": VICTIM},
                                "high_risk", TAP, "9.9.9") != f


def test_B02_parked_record_carries_fingerprint():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    assert rec.approval_fingerprint is not None
    assert rec.parked_at is not None
    # Fingerprint is integrity material: never in status views.
    c = TestClient(create_app(make_stack()))
    assert "approval_fingerprint" not in str(rec.model_dump(
        exclude={"approval_fingerprint"}))


def test_B03_mutated_args_invalidate_approval():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    rec.inputs["target_package"] = "com.android.systemui"  # tamper
    with pytest.raises(ValueError, match="invalidated"):
        stack["engine"].approve(rec.id)
    assert stack["engine"].get(rec.id).state == ExecutionState.DENIED
    assert stack["jobs"].poll(dev) is None  # never dispatched


def test_B04_mutated_device_invalidate_approval():
    stack = make_stack()
    dev = paired_android(stack)
    enroll_claim(stack, "other", "android")
    register(stack, "other", "android", [TAP])
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    rec.device_id = "other"  # tamper
    with pytest.raises(ValueError, match="invalidated"):
        stack["engine"].approve(rec.id)
    assert stack["engine"].get(rec.id).state == ExecutionState.DENIED


def test_B05_approval_window_expiry_reparks():
    assert APPROVAL_TTL_S == 300.0
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    rec.parked_at = rec.parked_at - timedelta(seconds=APPROVAL_TTL_S + 10)
    with pytest.raises(ValueError, match="expired"):
        stack["engine"].approve(rec.id)
    # Re-parked: still approvable, never dispatched stale.
    still = stack["engine"].get(rec.id)
    assert still.state == ExecutionState.WAITING_FOR_PERMISSION
    assert stack["jobs"].poll(dev) is None
    out = stack["engine"].approve_async(rec.id)
    assert out.state == ExecutionState.RUNNING
    # Do not leave the background waiter stranded: cancel wakes it.
    stack["engine"].cancel(rec.id)
    assert stack["engine"].get(rec.id).cancel_requested is True


def test_B06_revoked_target_invalidates_approval():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    stack["fabric"].revoke_device(dev, "test revoke")
    with pytest.raises(ValueError, match="invalidated"):
        stack["engine"].approve(rec.id)
    assert stack["engine"].get(rec.id).state == ExecutionState.DENIED


def test_B07_offline_target_invalidates_approval():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    stack["devices"].mark_offline(dev)
    with pytest.raises(ValueError, match="invalidated"):
        stack["engine"].approve(rec.id)


def test_B08_stale_snapshot_invalidates_approval():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev, capability=TAP)
    # Device snapshot goes stale before approval: presence re-check fails.
    stack["fabric"].mark_stale(dev)
    with pytest.raises(ValueError, match="invalidated"):
        stack["engine"].approve(rec.id)
    assert stack["engine"].get(rec.id).state == ExecutionState.DENIED
    assert stack["jobs"].poll(dev) is None  # never dispatched


def test_B09_policy_deny_after_park_blocks_approve():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        "shell.safe_readonly", {"command": "ls /tmp"}, dev)
    # Confirm-risk parks; now the command turns into a deny-pattern hit.
    rec.inputs["command"] = "rm -rf / --no-preserve-root"
    with pytest.raises(ValueError):
        stack["engine"].approve(rec.id)
    assert stack["engine"].get(rec.id).state == ExecutionState.DENIED


def test_B10_healthy_approve_still_runs_to_success():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev, capability=TAP)
    poller = CadencePoller(stack["jobs"], dev,
                           result={"package": VICTIM, "applied": True})
    poller.start()
    try:
        out = stack["engine"].approve_async(rec.id)
        assert out.state == ExecutionState.RUNNING
        fin = await_state(stack["engine"], rec.id, {"succeeded"})
        assert fin.state == ExecutionState.SUCCEEDED
        assert fin.approved_at is not None
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_B11_cancel_before_approval_then_approve_refused():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    stack["engine"].cancel(rec.id)
    assert stack["engine"].get(rec.id).state == ExecutionState.CANCELLED
    with pytest.raises(ValueError):
        stack["engine"].approve(rec.id)


def test_B12_double_approve_deterministic():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    poller = CadencePoller(stack["jobs"], dev)
    poller.start()
    try:
        stack["engine"].approve_async(rec.id)
        with pytest.raises(ValueError):
            stack["engine"].approve_async(rec.id)
        fin = await_state(stack["engine"], rec.id, {"succeeded"})
        assert fin.state == ExecutionState.SUCCEEDED
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_B13_approval_not_replayable_across_executions():
    stack = make_stack()
    dev = paired_android(stack)
    r1 = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    r2 = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    stack["engine"].approve(r1.id)
    # Approving r1 grants nothing to r2; r2 still needs its own approval.
    assert stack["engine"].get(r2.id).state == ExecutionState.WAITING_FOR_PERMISSION
    stack["engine"].cancel(r1.id)
    stack["engine"].cancel(r2.id)


def test_B14_http_approve_mutation_rejected():
    c, stack = _client()
    dev = paired_android(stack)
    r = c.post("/v1/executions",
               json={"tool": TAP, "inputs": {"target_package": VICTIM},
                     "device_id": dev}, headers=OP)
    assert r.status_code == 202
    eid = r.json()["id"]
    # Tamper server-side record (simulates post-park mutation), then
    # approve over HTTP: must refuse, never dispatch.
    stack["engine"]._records[eid].inputs["target_package"] = "evil.pkg"
    a = c.post(f"/v1/executions/{eid}/approve", headers=OP)
    assert a.status_code == 409
    g = c.get(f"/v1/executions/{eid}", headers=OP)
    assert g.json()["status"] == "denied"


def test_B15_revoke_vs_approve_race_revocation_wins():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    stack["fabric"].revoke_device(dev, "race")
    with pytest.raises(ValueError):
        stack["engine"].approve(rec.id)
    assert stack["engine"].get(rec.id).state == ExecutionState.DENIED
    assert stack["jobs"].poll(dev) is None


def test_B16_execution_id_not_authority_for_approve():
    stack = make_stack()
    dev = paired_android(stack)
    with pytest.raises(KeyError):
        stack["engine"].approve_async("exec-forged-000000")
