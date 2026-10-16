"""Approval/polling race regressions (Core <-> Android lifecycle).

Reproduces, with threading.Barrier (never sleeps-as-sync), the race
windows in the grant -> dispatch -> poll -> result path:

  G1  concurrent double approve -> exactly one grant, one dispatch
  G2  cancel racing grant -> cancel wins or grant wins, never a
      CANCELLED->RUNNING resurrection and never a double dispatch
  G3  cancel landing between _run's cancel check and job enqueue ->
      no orphaned waiter (fails fast instead of blocking to timeout)
  G4  concurrent same-key submits -> one execution (idempotency TOCTOU)
  G5  disconnect/reconnect/resume -> no second dispatch, terminal stable
  G6  duplicate result delivery -> single bus/audit emission

Fail-closed policy, fingerprints, device binding, and idempotency
semantics are otherwise untouched (see test_claim_lease.py etc.).
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.device_tools import make_proxy_handler
from core.execution import ExecutionEngine
from core.jobs import JobQueue
from core.models import (DeviceKind, DeviceState, ExecutionState,
                         PermissionLevel, RiskLevel, ToolDefinition)
from core.policy import PolicyEngine
from core.tools import ToolRegistry

OP = {"Authorization": "Bearer dev-token"}


def make_stack():
    return build_stack(fabric_db="")


def enroll_claim(stack, device_id, kind="linux"):
    code = stack["device_auth"].enroll(device_id, DeviceKind(kind))
    return stack["device_auth"].claim(code)


def register(stack, device_id, kind="linux", caps=None):
    stack["devices"].register(DeviceState(
        device_id=device_id, kind=DeviceKind(kind),
        capabilities=caps or [], online=True, status="online"))


def dheaders(dev, key):
    return {"X-Device-Id": dev, "X-Device-Key": key}


def blocking_engine():
    """Engine with one CONFIRM tool whose handler blocks on an event, so
    tests can park _run mid-dispatch deterministically. Returns
    (engine, entered, release, calls)."""
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def handler(inputs, ctx):
        calls.append("run")
        entered.set()
        assert release.wait(timeout=10), "test did not release stub tool"
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="test.block", description="race stub (blocking)",
        permission=PermissionLevel.RESTRICTED, risk=RiskLevel.CONFIRM),
        handler)
    eng = ExecutionEngine(reg, PolicyEngine())
    return eng, entered, release, calls


def await_exec(eng, eid, want=("succeeded", "failed", "timed_out",
                               "denied", "cancelled"), deadline=10.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        st = eng.get(eid).state.value
        if st in want:
            return eng.get(eid)
        time.sleep(0.02)
    return eng.get(eid)


# ---------- G1: concurrent double approve ----------

def test_G1_concurrent_approve_grants_exactly_once():
    eng, entered, release, calls = blocking_engine()
    rec = eng.submit_async("test.block", {}, "cloud")
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    # Rendezvous gate at the last step before the RUNNING write: both
    # threads must arrive here (i.e. both passed the WAITING check)
    # before either proceeds. On unfixed code both then set RUNNING and
    # dispatch twice; fixed code refuses the second at admission so only
    # one ever arrives (the gate times out and lets it through alone).
    arrived: list[str] = []
    real_grant = eng.policy.grant

    def gated_grant(*a, **k):
        arrived.append("here")
        end = time.monotonic() + 0.5
        while len(arrived) < 2 and time.monotonic() < end:
            time.sleep(0.005)
        return real_grant(*a, **k)

    eng.policy.grant = gated_grant
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def approver(tag):
        barrier.wait()
        try:
            eng.approve_async(rec.id, approver=tag)
            outcomes[tag] = "granted"
        except ValueError as e:
            outcomes[tag] = f"refused: {e}"

    ta = threading.Thread(target=approver, args=("a",), daemon=True)
    tb = threading.Thread(target=approver, args=("b",), daemon=True)
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)
    assert sorted(outcomes.values()).count("granted") == 1, outcomes
    assert sum(1 for v in outcomes.values()
               if v.startswith("refused")) == 1, outcomes
    # The loser was refused either as in-progress or as no-longer-waiting.
    loser = next(v for v in outcomes.values() if v.startswith("refused"))
    assert ("already in progress" in loser) or ("not awaiting" in loser), loser
    release.set()
    final = await_exec(eng, rec.id)
    assert final.state == ExecutionState.SUCCEEDED
    assert calls == ["run"], calls  # exactly one dispatch


# ---------- G2: cancel racing grant ----------

def test_G2_cancel_racing_grant_never_resurrects_nor_duplicates():
    eng, entered, release, calls = blocking_engine()
    rec = eng.submit_async("test.block", {}, "cloud")
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def approver():
        barrier.wait()
        try:
            eng.approve_async(rec.id)
            outcomes["approve"] = "granted"
        except ValueError as e:
            outcomes["approve"] = f"refused: {e}"

    def canceller():
        barrier.wait()
        eng.cancel(rec.id)
        outcomes["cancel"] = "cancelled"

    ta = threading.Thread(target=approver, daemon=True)
    tc = threading.Thread(target=canceller, daemon=True)
    ta.start()
    tc.start()
    ta.join(timeout=10)
    tc.join(timeout=10)
    release.set()
    final = await_exec(eng, rec.id)
    if outcomes["approve"].startswith("refused"):
        # Cancel won: grant refused, nothing ever dispatched.
        assert final.state == ExecutionState.CANCELLED
        assert final.result is None
        assert calls == [], calls
    else:
        # Grant won: at most one dispatch, terminal, never resurrected.
        assert len(calls) <= 1, calls
        assert final.state in (ExecutionState.CANCELLED,
                               ExecutionState.SUCCEEDED)
        if final.state == ExecutionState.CANCELLED:
            assert final.result is None
    # The CANCELLED->RUNNING resurrection is impossible in every order:
    assert not (outcomes["approve"] == "granted"
                and "no longer awaiting" not in outcomes["approve"]
                and final.state == ExecutionState.CANCELLED
                and final.result is not None)


def test_G2b_grant_after_cancel_refused():
    eng, _, release, calls = blocking_engine()
    rec = eng.submit_async("test.block", {}, "cloud")
    eng.cancel(rec.id)
    assert eng.get(rec.id).state == ExecutionState.CANCELLED
    with pytest.raises(ValueError, match="not awaiting"):
        eng.approve_async(rec.id)
    release.set()
    assert eng.get(rec.id).state == ExecutionState.CANCELLED
    assert calls == []


# ---------- G3: stranded waiter ----------

def test_G3_cancel_between_check_and_enqueue_fails_fast():
    q = JobQueue()
    handler = make_proxy_handler(q, timeout_s=5.0)
    handler.tool_name = "system.battery"
    # Exact orphan interleaving: cancel swept the queue (found nothing),
    # then the dispatch enqueued. The post-enqueue check must fail fast.
    ctx = {"device_id": "d1", "execution_id": "exec-1",
           "cancel_requested": lambda: True}
    start = time.monotonic()
    with pytest.raises(InterruptedError, match="cancelled before wait"):
        handler({}, ctx)
    assert time.monotonic() - start < 2.0
    jobs = [j for j in q._jobs.values()]
    assert len(jobs) == 1
    assert jobs[0].state == "cancelled"


def test_G3b_normal_path_unaffected_by_predicate():
    q = JobQueue()
    handler = make_proxy_handler(q, timeout_s=5.0)
    handler.tool_name = "system.battery"
    ctx = {"device_id": "d1", "execution_id": "exec-1",
           "cancel_requested": lambda: False}

    def device():
        job = None
        end = time.monotonic() + 5
        while time.monotonic() < end and job is None:
            job = q.poll("d1")
            time.sleep(0.02)
        assert job is not None
        q.complete(job.id, ok=True, result={"pct": 87})

    t = threading.Thread(target=device, daemon=True)
    t.start()
    assert handler({}, ctx) == {"pct": 87}
    t.join(timeout=5)


# ---------- G4: idempotency TOCTOU ----------

def test_G4_concurrent_same_key_single_execution():
    from core import builtin_tools
    reg = ToolRegistry()
    for d, h in builtin_tools.BUILTINS:
        reg.register(d, h)
    eng = ExecutionEngine(reg, PolicyEngine())
    n = 8
    barrier = threading.Barrier(n)
    ids: list[str] = []

    def submitter():
        barrier.wait()
        rec = eng.submit_async_idem("util.echo", {"text": "x"}, "cloud",
                                    idempotency_key="race-key")
        ids.append(rec.id)

    ts = [threading.Thread(target=submitter, daemon=True) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert len(ids) == n
    assert len(set(ids)) == 1, ids
    assert len(eng._records) == 1


# ---------- G5: disconnect/reconnect/resume ----------

def test_G5_reconnect_resumes_without_second_dispatch():
    stack = make_stack()
    client = TestClient(create_app(stack))
    dev, key = enroll_claim(stack, "d1")
    register(stack, "d1")
    rec = stack["engine"].submit_async("system.battery", {}, "d1")
    assert rec.state == ExecutionState.RUNNING
    # Device claims, then the transport drops (kill, no result yet).
    r = client.post("/v1/agent/jobs/poll", headers=dheaders(dev, key))
    assert r.status_code == 200, r.text
    assert r.json()["job"] is not None
    job_id = r.json()["job"]["job_id"]
    r = client.post("/v1/agent/disconnect", headers=dheaders(dev, key))
    assert r.status_code == 200, r.text
    assert stack["jobs"].get(job_id).state == "claimed"
    # Reconnect: presence back, same credentials, no new identity.
    register(stack, "d1")
    r = client.post("/v1/agent/jobs/poll", headers=dheaders(dev, key))
    assert r.status_code == 200, r.text
    assert r.json() == {"job": None}  # already claimed: nothing re-issued
    # The resumed device completes exactly once.
    r = client.post("/v1/agent/jobs/result",
                    json={"job_id": job_id, "ok": True,
                          "result": {"pct": 87}},
                    headers=dheaders(dev, key))
    assert r.status_code == 200, r.text
    assert stack["jobs"].get(job_id).state == "done"
    claimed = [j for j in stack["jobs"]._jobs.values()
               if j.execution_id == rec.id]
    assert len(claimed) == 1, [j.id for j in claimed]
    end = time.monotonic() + 10
    while time.monotonic() < end:
        if stack["engine"].get(rec.id).state == ExecutionState.SUCCEEDED:
            break
        time.sleep(0.02)
    assert stack["engine"].get(rec.id).state == ExecutionState.SUCCEEDED
    assert stack["engine"].get(rec.id).result == {"pct": 87}


# ---------- G6: duplicate result single emission ----------

def _advertise_cap(stack, dev, cap, risk="confirm"):
    stack["fabric"].advertise([
        {"id": cap, "name": cap, "descriptor_version": "1",
         "version": "1.0.0", "risk": risk, "availability": "available"},
    ], dev)


def test_G6b_concurrent_duplicate_result_single_emission(monkeypatch):
    # The residual race: two simultaneous deliveries could both pass a
    # read-then-complete check and emit twice. The rendezvous forces both
    # threads past the pre-read together; only complete_once()'s atomic
    # flag may authorize emission, so exactly one emits on fixed code
    # (old check-then-act code emits twice and fails this test).
    stack = make_stack()
    app = create_app(stack)
    dev, key = enroll_claim(stack, "d1")
    register(stack, "d1")
    job = stack["jobs"].enqueue("d1", "t", {})
    assert stack["jobs"].poll("d1") is not None
    barrier = threading.Barrier(2)
    real_get = stack["jobs"].get

    def rendezvous_get(job_id):
        found = real_get(job_id)
        barrier.wait(timeout=10)
        return found

    monkeypatch.setattr(stack["jobs"], "get", rendezvous_get)
    body = {"job_id": job.id, "ok": True, "result": {"v": 1}}
    statuses: list[int] = []

    def deliver():
        c = TestClient(app)
        r = c.post("/v1/agent/jobs/result", json=body,
                   headers=dheaders(dev, key))
        statuses.append(r.status_code)

    t1 = threading.Thread(target=deliver, daemon=True)
    t2 = threading.Thread(target=deliver, daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert sorted(statuses) == [200, 200], statuses
    monkeypatch.undo()  # verification reads must not rendezvous
    assert stack["jobs"].get(job.id).state == "done"
    assert stack["jobs"].get(job.id).result == {"v": 1}
    finishing = [e for e in stack["events_log"]
                 if e.get("type") == "execution_finished"
                 and e.get("source") == "device"
                 and (e.get("payload") or {}).get("job_id") == job.id]
    assert len(finishing) == 1, finishing
    audits = [a for a in stack["audit"].query(limit=50)
              if a["action"] == "job_result" and a["target"] == job.id]
    assert len(audits) == 1, audits


def test_G6c_complete_once_flag_atomic_under_contention():
    q = JobQueue()
    job = q.enqueue("d1", "t", {})
    q.poll("d1")
    n = 8
    barrier = threading.Barrier(n)
    flags: list[bool] = []

    def completer():
        barrier.wait(timeout=10)
        _, first = q.complete_once(job.id, True, {"v": 1})
        flags.append(first)

    ts = [threading.Thread(target=completer, daemon=True) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert len(flags) == n
    assert flags.count(True) == 1, flags
    assert q.get(job.id).state == "done"
    assert q.get(job.id).result == {"v": 1}


def test_G7_direct_submit_approves_on_advertised_required_caps():
    # Regression: revalidate looked up the TOOL name ("shell.safe_readonly")
    # as a capability, so every direct-submitted device approval was denied
    # in production even with full advertisement (tests passed only
    # because they advertise tool names). The fix authorizes the tool's
    # required capabilities instead.
    stack = make_stack()
    dev, key = enroll_claim(stack, "d1")
    register(stack, "d1")
    _advertise_cap(stack, "d1", "terminal.safe")
    rec = stack["engine"].submit_async(
        "shell.safe_readonly", {"command": "uname -a"}, "d1")
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    out = stack["engine"].approve_async(rec.id)
    assert out.state == ExecutionState.RUNNING
    # Dispatch reached the queue (proxy thread enqueues promptly).
    end = time.monotonic() + 10
    job = None
    while time.monotonic() < end:
        job = stack["jobs"].get_by_execution(rec.id)
        if job is not None:
            break
        time.sleep(0.02)
    assert job is not None
    assert job.tool == "shell.safe_readonly"
    stack["engine"].cancel(rec.id)  # no device acts: leave nothing stranded


def test_G7b_unadvertised_caps_still_denied():
    stack = make_stack()
    dev, key = enroll_claim(stack, "d1")
    register(stack, "d1")
    rec = stack["engine"].submit_async(
        "shell.safe_readonly", {"command": "uname -a"}, "d1")
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    with pytest.raises(ValueError, match="capability unauthorized"):
        stack["engine"].approve_async(rec.id)
    assert stack["engine"].get(rec.id).state == ExecutionState.DENIED
    assert stack["jobs"].get_by_execution(rec.id) is None

def test_G6_duplicate_result_emits_once():
    stack = make_stack()
    client = TestClient(create_app(stack))
    dev, key = enroll_claim(stack, "d1")
    register(stack, "d1")
    job = stack["jobs"].enqueue("d1", "t", {})
    assert stack["jobs"].poll("d1") is not None
    body = {"job_id": job.id, "ok": True, "result": {"v": 1}}
    r1 = client.post("/v1/agent/jobs/result", json=body,
                     headers=dheaders(dev, key))
    assert r1.status_code == 200, r1.text
    r2 = client.post("/v1/agent/jobs/result", json=body,
                     headers=dheaders(dev, key))
    assert r2.status_code == 200, r2.text
    assert stack["jobs"].get(job.id).state == "done"
    assert stack["jobs"].get(job.id).result == {"v": 1}
    finishing = [e for e in stack["events_log"]
                 if e.get("type") == "execution_finished"
                 and e.get("source") == "device"
                 and (e.get("payload") or {}).get("job_id") == job.id]
    assert len(finishing) == 1, finishing
    audits = [a for a in stack["audit"].query(limit=50)
              if a["action"] == "job_result" and a["target"] == job.id]
    assert len(audits) == 1, audits
