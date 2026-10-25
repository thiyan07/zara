"""Execution retention, TTL, and expiry tests (Task 9).

Contract under test (docs/research/EXECUTION_RETENTION_TTL_AUDIT.md):
- Terminal executions/jobs are retained boundedly (count + age) via
  prune(), then reaped. Non-terminal (PENDING/RUNNING/WAITING,
  pending/claimed) are NEVER reaped.
- Evicted ids read back as unknown (KeyError → 404), like never-created.
- Ingest envelopes bounded: inputs ≤ tool max_input_bytes (rejected
  before any record exists), device result ≤ 64 KiB / error ≤ 4 KiB.
- Pairing codes expire (PAIRING_TTL_S); corrupt park time fails closed
  (re-park, never approve); job deadlines are monotonic.

Deterministic: injected `now`, crafted timestamps, barriers. No sleeps.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.device_auth import DeviceAuthStore
from core.execution import ExecutionEngine
from core.jobs import JobQueue
from core.models import DeviceKind, DeviceState, ExecutionRecord, ExecutionState

OP = {"Authorization": "Bearer dev-token"}
BAT = "system.battery"
ECHO = "util.echo"
INSPECT = "gui.screen.inspect"
TAP = "gui.tap"
VICTIM = "dev.zara.lab.privtest"


def utcnow():
    return datetime.now(timezone.utc)


def make_stack():
    return build_stack(fabric_db="")


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
         "version": "1.0.0", "risk": "high_risk", "availability": "available"},
        {"id": BAT, "name": BAT, "descriptor_version": "1",
         "version": "1.0.0", "risk": "safe", "availability": "available"},
    ], dev)
    return dev


def terminal_exec(rid, state, ended_at):
    return ExecutionRecord(id=rid, tool=BAT, inputs={}, device_id="cloud",
                           state=state, ended_at=ended_at)


# ---------- R01-R03 prune eligibility ----------

def test_R01_active_records_never_pruned():
    stack = make_stack()
    eng = stack["engine"]
    now = utcnow()
    ancient = now - timedelta(days=30)
    eng._records["e-pend"] = terminal_exec(
        "e-pend", ExecutionState.PENDING, None)
    eng._records["e-run"] = terminal_exec(
        "e-run", ExecutionState.RUNNING, None)
    eng._records["e-wait"] = terminal_exec(
        "e-wait", ExecutionState.WAITING_FOR_PERMISSION, None)
    eng._records["e-wait"].parked_at = ancient
    eng._records["e-old"] = terminal_exec(
        "e-old", ExecutionState.SUCCEEDED, ancient)
    assert eng.prune(now=now, max_terminal=0, retention_s=0) == 1
    assert set(eng._records) == {"e-pend", "e-run", "e-wait"}


def test_R02_retention_boundary_strictly_greater():
    stack = make_stack()
    eng = stack["engine"]
    now = utcnow()
    eng._records["e-edge"] = terminal_exec(
        "e-edge", ExecutionState.SUCCEEDED, now - timedelta(seconds=60))
    eng._records["e-old"] = terminal_exec(
        "e-old", ExecutionState.FAILED,
        now - timedelta(seconds=61))
    # retention 60s: age==60 retained (strict >), age==61... equal? 61>60
    # pruned. Use retention_s=60.5 for determinism around microseconds.
    assert eng.prune(now=now, max_terminal=99, retention_s=60.5) == 1
    assert set(eng._records) == {"e-edge"}


def test_R03_all_five_terminal_states_prunable():
    stack = make_stack()
    eng = stack["engine"]
    now = utcnow()
    old = now - timedelta(days=2)
    states = [ExecutionState.SUCCEEDED, ExecutionState.FAILED,
              ExecutionState.TIMED_OUT, ExecutionState.DENIED,
              ExecutionState.CANCELLED]
    for i, st in enumerate(states):
        eng._records[f"e-{i}"] = terminal_exec(f"e-{i}", st, old)
    assert eng.prune(now=now) == 5
    assert eng._records == {}


def test_R03b_jobs_all_terminal_states_prunable_actives_kept():
    q = JobQueue()
    now = utcnow()
    old = (now - timedelta(days=2)).isoformat()
    for st in ("done", "failed", "cancelled", "expired"):
        j = q.enqueue("d", "t", {})
        j.state = st
        j.finished_at = old
    p = q.enqueue("d", "t", {})  # pending stays
    c = q.enqueue("d", "t", {})
    q.poll("d")  # claimed stays (poll returns oldest = p... note order)
    assert q.prune(now=now) == 4
    assert set(q._jobs) == {p.id, c.id}


def test_R03c_unageable_terminal_retained():
    q = JobQueue()
    j = q.enqueue("d", "t", {})
    q.complete(j.id, True, result={"v": 1})
    j.finished_at = None
    assert q.prune() == 0
    stack = make_stack()
    eng = stack["engine"]
    eng._records["e-x"] = terminal_exec(
        "e-x", ExecutionState.SUCCEEDED, None)
    assert eng.prune() == 0


# ---------- R04-R06 eviction reads + idempotence + races ----------

def test_R04_evicted_ids_read_as_unknown():
    stack = make_stack()
    client = TestClient(create_app(stack))
    dev = paired_android(stack)
    rec = stack["engine"].submit(ECHO, {"text": "t"}, "cloud")
    assert rec.state == ExecutionState.SUCCEEDED
    assert stack["engine"].prune(max_terminal=0, retention_s=-1) == 1
    with pytest.raises(KeyError):
        stack["engine"].get(rec.id)
    r = client.get(f"/v1/executions/{rec.id}", headers=OP)
    assert r.status_code == 404, r.text
    assert r.json() == {"detail": "unknown execution"}


def test_R04b_pruned_job_result_is_unknown_job():
    stack = make_stack()
    client = TestClient(create_app(stack))
    dev, key = enroll_claim(stack, "d1")
    register(stack, "d1")
    job = stack["jobs"].enqueue("d1", "t", {})
    stack["jobs"].poll("d1")
    stack["jobs"].complete(job.id, True, result={"v": 1})
    assert stack["jobs"].prune(max_terminal=0, retention_s=-1) == 1
    r = client.post("/v1/agent/jobs/result",
                    json={"job_id": job.id, "ok": True, "result": {}},
                    headers={"X-Device-Id": "d1", "X-Device-Key": key})
    assert r.status_code == 404, r.text


def test_R05_duplicate_prune_idempotent():
    stack = make_stack()
    eng = stack["engine"]
    now = utcnow()
    eng._records["e-1"] = terminal_exec(
        "e-1", ExecutionState.FAILED, now - timedelta(days=2))
    assert eng.prune(now=now) == 1
    assert eng.prune(now=now) == 0
    q = JobQueue()
    assert q.prune() == 0
    assert q.prune() == 0


def test_R06_prune_racing_complete_and_cancel():
    import threading
    q = JobQueue(max_terminal_records=4, terminal_retention_s=100)
    jobs = [q.enqueue("d", "t", {}) for _ in range(4)]
    for j in jobs:
        q.poll("d")
    barrier = threading.Barrier(4)
    errors = []

    def do(fn):
        try:
            barrier.wait(timeout=5)
            fn()
        except Exception as e:  # noqa: BLE001 — record, never hide
            errors.append(e)

    ts = [threading.Thread(target=do, args=(lambda j=j: q.complete(
        j.id, True, result={"v": 1}),)) for j in jobs[:2]]
    ts.append(threading.Thread(
        target=do, args=(lambda: q.cancel(jobs[2].id),)))
    ts.append(threading.Thread(
        target=do, args=(lambda: q.prune(retention_s=-1),)))
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert not errors
    # Touched jobs are terminal or reaped; the untouched claimed job must
    # survive prune (active jobs are never eligible).
    for j in jobs[:3]:
        if j.id in q._jobs:
            assert q._jobs[j.id].state in (
                "done", "failed", "cancelled", "expired")
    assert q._jobs[jobs[3].id].state == "claimed"


# ---------- R07 timestamps fail closed ----------

def test_R07_grant_with_corrupt_park_time_reparks_never_approves():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit_async(TAP, {"target_package": VICTIM}, dev)
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    rec.parked_at = "not-a-time"  # type: ignore[assignment]
    with pytest.raises(ValueError, match="unverifiable"):
        stack["engine"]._grant(rec.id)
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    # Self-healing: fresh parked_at makes the next attempt verifiable.
    rec2 = stack["engine"]._grant(rec.id)
    assert rec2.state == ExecutionState.RUNNING


def test_R07b_pairing_code_expires_fail_closed():
    store = DeviceAuthStore()
    code = store.enroll("d1", DeviceKind.LINUX)
    store._creds["d1"].expires_at = (
        utcnow() - timedelta(seconds=601)).isoformat()
    with pytest.raises(ValueError, match="expired"):
        store.claim(code)
    # Missing expiry also fails closed.
    code2 = store.enroll("d2", DeviceKind.LINUX)
    store._creds["d2"].expires_at = None
    with pytest.raises(ValueError, match="expired"):
        store.claim(code2)
    # Fresh enroll/claim unaffected.
    code3 = store.enroll("d3", DeviceKind.LINUX)
    dev, key = store.claim(code3)
    assert dev == "d3" and store.verify(dev, key) is True


# ---------- R08 restart = no durability (documented guarantee) ----------

def test_R08_new_process_knows_nothing():
    s1 = make_stack()
    dev = paired_android(s1)
    rec = s1["engine"].submit(ECHO, {"text": "t"}, "cloud")
    s2 = make_stack()
    with pytest.raises(KeyError):
        s2["engine"].get(rec.id)
    c2 = TestClient(create_app(s2))
    assert c2.get(f"/v1/executions/{rec.id}",
                  headers=OP).status_code == 404


# ---------- R09 device cannot read operator API ----------

def test_R09_device_creds_do_not_authorize_operator_reads():
    stack = make_stack()
    client = TestClient(create_app(stack))
    dev = paired_android(stack)
    rec = stack["engine"].submit(ECHO, {"text": "t"}, "cloud")
    _, key = enroll_claim(stack, "reader")
    r = client.get(f"/v1/executions/{rec.id}",
                   headers={"X-Device-Id": "reader", "X-Device-Key": key})
    assert r.status_code in (401, 403), r.text


# ---------- R15-R16 ingest envelopes ----------

def test_R15_oversize_device_result_rejected():
    stack = make_stack()
    client = TestClient(create_app(stack))
    dev, key = enroll_claim(stack, "d1")
    register(stack, "d1")
    job = stack["jobs"].enqueue("d1", "t", {})
    stack["jobs"].poll("d1")
    big = {"blob": "x" * 70000}
    r = client.post("/v1/agent/jobs/result",
                    json={"job_id": job.id, "ok": True, "result": big},
                    headers={"X-Device-Id": "d1", "X-Device-Key": key})
    assert r.status_code == 422, r.text
    assert stack["jobs"].get(job.id).state == "claimed"  # nothing stored
    r2 = client.post(
        "/v1/agent/jobs/result",
        json={"job_id": job.id, "ok": False, "error": "e" * 5000},
        headers={"X-Device-Id": "d1", "X-Device-Key": key})
    assert r2.status_code == 422, r2.text
    # Normal result still accepted.
    r3 = client.post("/v1/agent/jobs/result",
                     json={"job_id": job.id, "ok": True,
                           "result": {"v": 1}},
                     headers={"X-Device-Id": "d1", "X-Device-Key": key})
    assert r3.status_code == 200, r3.text


def test_R15b_direct_complete_oversize_raises():
    q = JobQueue()
    j = q.enqueue("d", "t", {})
    with pytest.raises(ValueError, match="exceeds"):
        q.complete(j.id, True, result={"b": "x" * 70000})
    with pytest.raises(ValueError, match="exceeds"):
        q.complete(j.id, False, error="e" * 5000)
    assert q.get(j.id).state in ("pending", "claimed")


def test_R16_oversize_inputs_rejected_before_record():
    stack = make_stack()
    dev = paired_android(stack)
    before = len(stack["engine"]._records)
    with pytest.raises(ValueError, match="exceed"):
        stack["engine"].submit_async(ECHO, {"pad": "x" * 5000}, "cloud")
    assert len(stack["engine"]._records) == before
    client = TestClient(create_app(stack))
    r = client.post("/v1/executions",
                    json={"tool": ECHO, "inputs": {"pad": "x" * 5000},
                          "device_id": "cloud"}, headers=OP)
    assert r.status_code == 422, r.text
    assert len(stack["engine"]._records) == before


# ---------- R17 approval TTL edges ----------

def test_R17_approval_ttl_edges():
    from core.execution import APPROVAL_TTL_S
    assert APPROVAL_TTL_S == 300.0
    stack = make_stack()
    # Fresh device per case: a granted approval covers later submits, so
    # reuse would skip the park state entirely (correct grant behavior).
    dev_ok = paired_android(stack, "emu-ok")
    dev_old = paired_android(stack, "emu-old")
    now = utcnow()
    r_ok = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev_ok)
    assert r_ok.state == ExecutionState.WAITING_FOR_PERMISSION
    r_ok.parked_at = now - timedelta(seconds=299)
    assert stack["engine"]._grant(r_ok.id).state == ExecutionState.RUNNING
    r_old = stack["engine"].submit_async(
        TAP, {"target_package": VICTIM}, dev_old)
    assert r_old.state == ExecutionState.WAITING_FOR_PERMISSION
    r_old.parked_at = now - timedelta(seconds=301)
    with pytest.raises(ValueError, match="re-parked"):
        stack["engine"]._grant(r_old.id)
    assert r_old.state == ExecutionState.WAITING_FOR_PERMISSION


# ---------- R19 lazy bound ----------

def test_R19_lazy_prune_bounds_total():
    stack = make_stack()
    eng = stack["engine"]
    eng._max_terminal = 5
    dev = paired_android(stack)
    for _ in range(20):
        eng.submit(ECHO, {"text": "t"}, "cloud")
    assert len(eng._records) <= 10, len(eng._records)
    actives = [r for r in eng._records.values()
               if r.state not in (
                   ExecutionState.SUCCEEDED, ExecutionState.FAILED,
                   ExecutionState.TIMED_OUT, ExecutionState.DENIED,
                   ExecutionState.CANCELLED)]
    assert actives == []


def test_R19b_queue_lazy_prune_bounds_total():
    q = JobQueue(max_terminal_records=5, terminal_retention_s=10**9)
    for _ in range(20):
        j = q.enqueue("d", "t", {})
        q.poll("d")
        q.complete(j.id, True, result={"v": 1})
    assert len(q._jobs) <= 10, len(q._jobs)


# ---------- R20 cancel flag only on transition ----------

def test_R20_cancel_terminal_leaves_flag_clean():
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit(ECHO, {"text": "t"}, "cloud")
    assert rec.state == ExecutionState.SUCCEEDED
    stack["engine"].cancel(rec.id)
    assert rec.state == ExecutionState.SUCCEEDED
    assert rec.cancel_requested is False
    # Non-terminal RUNNING must still be flagged (proxy waiter wake-up).
    from core.models import ExecutionRecord as ER
    run = ER(id="e-run", tool=ECHO, inputs={}, device_id="cloud",
             state=ExecutionState.RUNNING)
    stack["engine"]._records["e-run"] = run
    stack["engine"].cancel("e-run")
    assert run.cancel_requested is True
    assert run.state == ExecutionState.RUNNING


# ---------- R14 no GUI privilege expansion ----------

def test_R14_no_gui_shell_or_reflection():
    import pathlib
    for name in ("android/lib/main.dart", "android/lib/job_runner.dart",
                 "android/lib/device_bridge.dart",
                 "android/lib/core_client.dart"):
        text = pathlib.Path(name).read_text()
        assert "Runtime.getRuntime" not in text, name
        assert "dart:mirrors" not in text, name
