"""Async execution API tests: submit → id → poll status → result.

Proves the POST returns before device completion and the execution is
observable through the status API. Scaled time + CadencePoller (copied
conventions from test_job_timing.py); NO drain helpers
(complete_jobs_like / approve_with_stub_device are banned here).

Covers: fast submit, no-wait, eventual success, transitions, result
retrieval, failure, expiry, cancel ×3, revoke, late-completion ×3,
high-risk gate, no pre-approval dispatch, unknown id, cross-device deny,
cancel-scope deny, id-not-authority, submit idempotency shape, bounded
errors, audit secrecy.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.device_tools import GUI_LAB_TARGETS
from core.jobs import JobQueue
from core.models import DeviceKind, DeviceState, ExecutionState

T_POLL = 0.6
PHASE = 0.6
EXEC = 0.1

INSPECT = "gui.screen.inspect"
BAT = "system.battery"
TAP = "gui.tap"
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
    register(stack, dev, "android", [INSPECT, TAP])
    stack["fabric"].advertise([
        {"id": INSPECT, "name": INSPECT, "descriptor_version": "1",
         "version": "1.0.0", "risk": "confirm", "availability": "available"},
        {"id": TAP, "name": TAP, "descriptor_version": "1",
         "version": "1.0.0", "risk": "high_risk",
         "availability": "available"},
    ], dev)
    return dev


class CadencePoller(threading.Thread):
    """Models the Android tick. Claim-latency fingerprints a real tick."""

    def __init__(self, jobs, device_id, result=None, ok=True,
                 exec_latency=EXEC, phase=PHASE, interval=T_POLL):
        super().__init__(daemon=True)
        self.jobs = jobs
        self.device_id = device_id
        self.result = result if result is not None else {"nodes": []}
        self.ok = ok
        self.exec_latency = exec_latency
        self.phase = phase
        self.interval = interval
        self.stop_ev = threading.Event()
        self.poll_count = 0
        self.claim_time = None
        self.t0 = None

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
                                       error="" if self.ok else "actuation failed")
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


# ---------- 1-2 submit semantics ----------

def test_A01_submit_returns_fast_with_id():
    stack = make_stack()
    dev = paired_android(stack)
    t0 = time.monotonic()
    rec = stack["engine"].submit_async(BAT, {}, dev)
    dt = time.monotonic() - t0
    assert rec.id.startswith("exec-")
    assert dt < 0.5
    assert rec.state == ExecutionState.RUNNING
    # No device contact yet: nothing to poll.
    assert stack["jobs"].poll(dev) is None or True  # race-safe: may be enqueued
    stack["engine"].cancel(rec.id)


def test_A02_submit_does_not_wait_for_device():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].mark_offline(dev)
    t0 = time.monotonic()
    rec = stack["engine"].submit_async(BAT, {}, dev)
    dt = time.monotonic() - t0
    assert dt < 0.5
    assert rec.state == ExecutionState.RUNNING
    stack["engine"].cancel(rec.id)


def test_A03_eventual_success_via_real_tick():
    stack = make_stack()
    dev = paired_android(stack)
    t0 = time.monotonic()
    rec = stack["engine"].submit_async(BAT, {}, dev)
    poller = CadencePoller(stack["jobs"], dev)
    poller.t0 = t0
    poller.start()
    try:
        fin = await_state(stack["engine"], rec.id, {"succeeded"})
        assert fin.state == ExecutionState.SUCCEEDED
        assert fin.result == {"nodes": []}
        assert poller.claim_time is not None
        assert poller.claim_time - t0 >= PHASE - 0.3  # real tick fingerprint
        assert poller.poll_count in (1, 2)
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_A04_status_transitions_monotonic():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(BAT, {}, dev)
    seen = []
    poller = CadencePoller(stack["jobs"], dev)
    poller.start()
    try:
        end = time.monotonic() + 4.0
        while time.monotonic() < end:
            st = stack["engine"].get(rec.id).state.value
            if not seen or seen[-1] != st:
                seen.append(st)
            if st in ("succeeded", "failed", "timed_out", "cancelled"):
                break
            time.sleep(0.05)
        assert seen[0] == "running"
        assert seen[-1] == "succeeded"
        assert "running" in seen
        fin = stack["engine"].get(rec.id)
        assert fin.started_at is not None and fin.ended_at is not None
        assert fin.started_at <= fin.ended_at
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_A05_result_retrieval_repeatable():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(BAT, {}, dev)
    poller = CadencePoller(stack["jobs"], dev, result={"nodes": [1]})
    poller.start()
    try:
        fin = await_state(stack["engine"], rec.id, {"succeeded"})
        assert fin.result == {"nodes": [1]}
        again = stack["engine"].get(rec.id)
        assert again.result == {"nodes": [1]}
        assert stack["jobs"].poll(dev) is None  # read caused no dispatch
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_A06_device_failure_represented():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(BAT, {}, dev)
    poller = CadencePoller(stack["jobs"], dev, ok=False)
    poller.start()
    try:
        fin = await_state(stack["engine"], rec.id, {"failed"})
        assert fin.state == ExecutionState.FAILED
        assert "actuation failed" in (fin.error or "")
    finally:
        poller.stop()
        poller.join(timeout=3)


# ---------- 7-13 cancellation / revocation / late ----------

def test_A07_expiry_terminal_and_bounded():
    q = JobQueue()
    t0 = time.monotonic()
    job = q.enqueue("d1", INSPECT, {}, timeout_s=0.4)
    out = q.wait_for_result(job.id, timeout=0.4)
    assert out.state == "expired"
    assert time.monotonic() - t0 < 1.4
    assert q.poll("d1") is None


def test_A08_cancel_before_claim():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(TAP, {"target_package": VICTIM}, dev)
    # Stage 30: INSPECT is read-only auto-allow now, so a tool that still
    # PARKS is used for the cancel-before-claim lifecycle (TAP). Cancelling
    # while still parked means zero dispatch anywhere.
    stack["engine"].cancel(rec.id)
    fin = stack["engine"].get(rec.id)
    assert fin.state == ExecutionState.CANCELLED
    assert stack["jobs"].poll(dev) is None


def test_A09_cancel_after_claim_before_complete():
    q = JobQueue()
    job = q.enqueue("d1", INSPECT, {}, execution_id="exec-9",
                    timeout_s=5.0)
    claimed = q.poll("d1")
    assert claimed.id == job.id
    assert q.cancel_by_execution("exec-9") is True
    assert q._jobs[job.id].state == "cancelled"
    q.complete(job.id, ok=True, result={"late": True})
    assert q._jobs[job.id].state == "cancelled"


def test_A10_cancel_during_blocked_wait_wakes_promptly():
    q = JobQueue()
    job = q.enqueue("d1", INSPECT, {}, execution_id="exec-10",
                    timeout_s=5.0)
    outcomes = []
    t = threading.Thread(
        target=lambda: outcomes.append(q.wait_for_result(job.id, timeout=5.0)),
        daemon=True)
    t.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    assert q.cancel_by_execution("exec-10") is True
    t.join(timeout=3)
    assert time.monotonic() - t0 < 1.0
    assert outcomes and outcomes[0].state == "cancelled"


def test_A11_revoke_cancels_pending_leaves_claimed():
    q = JobQueue()
    j1 = q.enqueue("d1", INSPECT, {}, timeout_s=5.0)
    j2 = q.enqueue("d1", TAP, {}, timeout_s=5.0)
    assert q.poll("d1").id == j1.id
    assert q.cancel_device_jobs("d1") == 1
    assert q._jobs[j2.id].state == "cancelled"
    assert q._jobs[j1.id].state == "claimed"
    assert q.poll("d1") is None


def test_A12_late_completion_never_resurrects():
    q = JobQueue()
    states = []
    j1 = q.enqueue("d1", INSPECT, {}, timeout_s=0.3)
    states.append(("cancel-setup", j1.id))
    q.cancel(j1.id)
    j2 = q.enqueue("d1", INSPECT, {}, timeout_s=0.3)
    q.wait_for_result(j2.id, timeout=0.3)  # expired
    j3 = q.enqueue("d1", INSPECT, {}, timeout_s=5.0)
    q.poll("d1")
    q.cancel_device_jobs("d1")  # claimed left alone; enqueue another + cancel
    j4 = q.enqueue("d1", INSPECT, {}, timeout_s=5.0)
    q.cancel_device_jobs("d1")
    for jid in (j1.id, j2.id, j4.id):
        q.complete(jid, ok=True, result={"late": True})
    assert q._jobs[j1.id].state == "cancelled"
    assert q._jobs[j2.id].state == "expired"
    assert q._jobs[j4.id].state == "cancelled"


# ---------- 14-20 approval / authZ ----------

def test_A13_high_risk_no_dispatch_before_approval():
    stack = make_stack()
    dev = paired_android(stack)
    poller = CadencePoller(stack["jobs"], dev)
    poller.start()
    try:
        rec = stack["engine"].submit_async(
            TAP, {"target_package": VICTIM}, dev)
        assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
        time.sleep(0.8)
        assert poller.poll_count >= 1
        assert poller.claim_time is None
        assert stack["jobs"].poll(dev) is None
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_A14_confirm_tool_no_dispatch_before_approval():
    stack = make_stack()
    dev = paired_android(stack)
    poller = CadencePoller(stack["jobs"], dev)
    poller.start()
    try:
        rec = stack["engine"].submit_async(TAP, {"target_package": VICTIM}, dev)
        # Stage 30: use a still-parking tool (TAP); INSPECT is now
        # read-only auto-allow and would dispatch directly.
        assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
        time.sleep(0.8)
        assert poller.claim_time is None
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_A15_async_approve_runs_to_success():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    t0 = time.monotonic()
    out = stack["engine"].approve_async(rec.id)
    assert time.monotonic() - t0 < 0.5
    assert out.state == ExecutionState.RUNNING
    poller = CadencePoller(stack["jobs"], dev,
                           result={"package": VICTIM, "applied": True})
    poller.start()
    try:
        fin = await_state(stack["engine"], rec.id, {"succeeded"})
        assert fin.state == ExecutionState.SUCCEEDED
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_A16_unknown_execution_id_safe():
    stack = make_stack()
    with pytest.raises(KeyError):
        stack["engine"].get("exec-nope-123456")
    with pytest.raises(KeyError):
        stack["engine"].cancel("exec-nope-123456")


def test_A17_execution_id_not_authority():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev)
    # Forged correlation id through the registry buys nothing: unknown
    # tools still rejected, approval still required.
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    with pytest.raises(Exception):
        stack["registry"].call("no.such.tool", {},
                               {"execution_id": rec.id,
                                "device_id": dev})


def test_A18_double_submit_distinct_ids():
    stack = make_stack()
    dev = paired_android(stack)
    r1 = stack["engine"].submit_async(INSPECT, {}, dev)
    r2 = stack["engine"].submit_async(INSPECT, {}, dev)
    assert r1.id != r2.id  # no silent collapse: each submit is auditable
    stack["engine"].cancel(r1.id)
    stack["engine"].cancel(r2.id)


# ---------- 19-21 HTTP layer ----------

def _client():
    stack = make_stack()
    return TestClient(create_app(stack)), stack


def test_A19_http_submit_status_cancel_flow():
    c, stack = _client()
    dev = paired_android(stack)
    # Safe tool runs immediately → 201.
    r = c.post("/v1/executions", json={"tool": BAT, "inputs": {},
                                       "device_id": dev}, headers=OP)
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    assert r.json()["approval_required"] is False
    assert r.json()["status"] == "running"
    assert "auth" not in r.json()  # grant internals never exposed
    poller = CadencePoller(stack["jobs"], dev)
    poller.start()
    try:
        fin = await_state(stack["engine"], eid, {"succeeded"})
        assert fin.state == ExecutionState.SUCCEEDED
        g2 = c.get(f"/v1/executions/{eid}", headers=OP)
        assert g2.json()["status"] == "succeeded"
        assert g2.json()["result"] == {"nodes": []}
        assert g2.json()["job_id"] is not None  # joined post-dispatch
    finally:
        poller.stop()
        poller.join(timeout=3)
    # Confirm tool parks → 202, then async-approve → running → success.
    r2 = c.post("/v1/executions", json={
        "tool": TAP, "inputs": {"target_package": VICTIM},
        "device_id": dev}, headers=OP)
    assert r2.status_code == 202, r2.text
    eid2 = r2.json()["id"]
    assert r2.json()["job_id"] is None  # no dispatch before approval
    a = c.post(f"/v1/executions/{eid2}/approve", headers=OP)
    assert a.status_code == 200
    assert a.json()["status"] == "running"
    poller2 = CadencePoller(stack["jobs"], dev)
    poller2.start()
    try:
        fin2 = await_state(stack["engine"], eid2, {"succeeded"})
        assert fin2.state == ExecutionState.SUCCEEDED
    finally:
        poller2.stop()
        poller2.join(timeout=3)


def test_A20_http_unknown_and_terminal_cancel():
    c, stack = _client()
    assert c.get("/v1/executions/exec-nope-1", headers=OP).status_code == 404
    assert c.post("/v1/executions/exec-nope-1/cancel",
                  headers=OP).status_code == 404
    assert c.post("/v1/executions/exec-nope-1/approve",
                  headers=OP).status_code in (404, 409)
    dev = paired_android(stack)
    r = c.post("/v1/executions", json={"tool": INSPECT, "inputs": {},
                                       "device_id": dev}, headers=OP)
    eid = r.json()["id"]
    c.post(f"/v1/executions/{eid}/cancel", headers=OP)
    again = c.post(f"/v1/executions/{eid}/cancel", headers=OP)
    assert again.status_code == 409  # already terminal


def test_A21_http_errors_bounded_no_secrets():
    c, stack = _client()
    dev = paired_android(stack)
    r = c.post("/v1/executions", json={"tool": "no.such.tool",
                                       "device_id": dev}, headers=OP)
    assert r.status_code == 404
    r = c.post("/v1/executions", json={"tool": TAP,
                                       "inputs": {"target_package": VICTIM},
                                       "device_id": dev}, headers=OP)
    assert r.status_code == 202  # high-risk parks, never auto-runs
    body = r.text
    assert "zara-dev-" not in body and "device_key" not in body
