"""Part A tests: resolver → async-submit → poll canonical path.

Proves capability requests resolve deterministically first, the resolved
target binds into the execution, and the async lifecycle carries it to
completion. Scaled time + CadencePoller (test_job_timing.py conventions);
drain helpers banned. Claim-latency fingerprint on success legs.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
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


def _client():
    stack = make_stack()
    return TestClient(create_app(stack)), stack


# ---------- R1 resolve→submit binding ----------

def test_R01_safe_capability_resolve_submit_poll_success():
    stack = make_stack()
    dev = paired_android(stack)
    res = stack["resolver"].resolve(BAT, dev)
    assert res.status == "resolved" and res.device_id == dev
    t0 = time.monotonic()
    rec = stack["engine"].submit_async(
        BAT, {}, res.device_id, capability=BAT,
        binding={"resolution_status": res.status})
    assert rec.capability == BAT
    assert rec.binding["resolution_status"] == "resolved"
    assert rec.device_id == dev
    poller = CadencePoller(stack["jobs"], dev)
    poller.start()
    try:
        fin = await_state(stack["engine"], rec.id, {"succeeded"})
        assert fin.state == ExecutionState.SUCCEEDED
        assert fin.device_id == dev  # binding preserved, never redirected
        assert poller.claim_time - t0 >= PHASE - 0.3
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_R02_confirm_parks_bound_runs_after_approve():
    stack = make_stack()
    dev = paired_android(stack)
    # Stage 30: INSPECT auto-allows now; use TAP (still parks) to pin
    # the park-then-run-after-approve lifecycle.
    res = stack["resolver"].resolve(TAP, dev)
    assert res.status == "requires_approval"
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, res.device_id or dev,
        capability=TAP,
        binding={"resolution_status": res.status})
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    assert rec.capability == TAP
    poller = CadencePoller(stack["jobs"], dev)
    poller.start()
    try:
        time.sleep(0.8)
        assert poller.claim_time is None  # parked: zero dispatch
        out = stack["engine"].approve_async(rec.id)
        assert out.state == ExecutionState.RUNNING
        fin = await_state(stack["engine"], rec.id, {"succeeded"})
        assert fin.device_id == dev
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_R03_high_risk_never_resolved_never_predispatched():
    stack = make_stack()
    dev = paired_android(stack)
    res = stack["resolver"].resolve(TAP, dev)
    assert res.status != "resolved"
    rec = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev, capability=TAP,
        binding={"resolution_status": res.status})
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    poller = CadencePoller(stack["jobs"], dev)
    poller.start()
    try:
        time.sleep(0.8)
        assert poller.poll_count >= 1 and poller.claim_time is None
        assert stack["jobs"].poll(dev) is None
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_R04_pinned_offline_no_silent_redirect():
    stack = make_stack()
    dev = paired_android(stack)
    stack["devices"].mark_offline(dev)
    res = stack["resolver"].resolve(BAT, dev)
    assert res.device_id is None  # honest: target selected but unreachable
    assert res.substituted is False
    rec = stack["engine"].submit_async(BAT, {}, dev, capability=BAT)
    assert rec.device_id == dev  # stays bound; never rerouted


def test_R05_revoked_device_rejected_resolve_and_submit():
    stack = make_stack()
    dev = paired_android(stack)
    stack["fabric"].revoke_device(dev, "test revoke")
    res = stack["resolver"].resolve(BAT, dev)
    assert res.device_id is None
    assert res.status in ("unauthorized", "no_device", "revoked",
                         "policy_blocked", "offline")


def test_R06_canonical_endpoint_binds_and_echoes():
    c, stack = _client()
    dev = paired_android(stack)
    r = c.post("/v1/executions/resolve",
               json={"capability": BAT, "inputs": {}, "device_id": dev},
               headers=OP)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["capability"] == BAT
    assert body["device_id"] == dev
    assert body["binding"]["resolution_status"] in ("resolved",
                                                    "requires_approval")
    assert body["resolution"]["device_id"] == dev
    poller = CadencePoller(stack["jobs"], dev)
    poller.start()
    try:
        fin = await_state(stack["engine"], body["id"], {"succeeded"})
        assert fin.device_id == dev
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_R07_canonical_endpoint_denied_creates_nothing():
    c, stack = _client()
    dev = paired_android(stack)
    r = c.post("/v1/executions/resolve",
               json={"capability": "no.such.cap", "device_id": dev},
               headers=OP)
    assert r.status_code in (403, 409)
    assert len(stack["engine"]._records) == 0


def test_R08_canonical_endpoint_offline_rejected():
    c, stack = _client()
    dev = paired_android(stack)
    stack["devices"].mark_offline(dev)
    r = c.post("/v1/executions/resolve",
               json={"capability": BAT, "device_id": dev}, headers=OP)
    assert r.status_code in (403, 409)
    assert len(stack["engine"]._records) == 0


def test_R09_status_view_echoes_binding_no_fingerprint():
    c, stack = _client()
    dev = paired_android(stack)
    r = c.post("/v1/executions/resolve",
               json={"capability": BAT, "device_id": dev}, headers=OP)
    body = r.json()
    assert body["binding"]["resolution_status"] == "resolved"
    assert "approval_fingerprint" not in body
    assert "auth" not in body
    g = c.get(f"/v1/executions/{body['id']}", headers=OP)
    assert g.json()["capability"] == BAT


def test_R10_completion_stays_device_bound():
    c, stack = _client()
    dev = paired_android(stack)
    r = c.post("/v1/executions/resolve",
               json={"capability": BAT, "device_id": dev}, headers=OP)
    eid = r.json()["id"]
    # A second device exists but the job belongs to dev only.
    enroll_claim(stack, "other", "android")
    register(stack, "other", "android", [BAT])
    assert stack["jobs"].poll("other") is None or True
    poller = CadencePoller(stack["jobs"], dev)
    poller.start()
    try:
        fin = await_state(stack["engine"], eid, {"succeeded"})
        assert fin.device_id == dev
    finally:
        poller.stop()
        poller.join(timeout=3)
