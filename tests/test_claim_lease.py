"""Device claim/lease lifecycle tests (Task 8).

State model under test (core/jobs.py + core/device_auth.py + app poll):
  pending -> claimed -> done|failed            (claim = poll transition)
  pending|claimed -> cancelled                 (cancel / cancel_by_execution)
  pending -> cancelled                         (revoke: cancel_device_jobs)
  pending|claimed -> expired                   (proxy waiter timeout; terminal)
  expired/done/failed/cancelled are terminal; late complete is a no-op.

A claim is bounded by the job's own timeout_s (proxy wait, default 90s),
not by a lease object: CLAIM_TIMEOUT_S is deliberately unwired (claimed
jobs may be actuating). No generation token exists because an expired or
cancelled job never returns to pending — the same id is never re-issued.

Deterministic: threading.Barrier for races, short real timeouts only
where expiry itself is the subject. No sleeps-as-sync.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.jobs import JobQueue
from core.models import DeviceKind, DeviceState

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


# ---------- C01-C03 claim exclusivity ----------

def test_C01_concurrent_claims_exactly_one_winner():
    q = JobQueue()
    q.enqueue("d1", "t", {})
    barrier = threading.Barrier(8)
    won = []
    lock = threading.Lock()

    def worker():
        barrier.wait(timeout=5)
        job = q.poll("d1")
        if job is not None:
            with lock:
                won.append(job.id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(won) == 1
    assert q.get(won[0]).state == "claimed"


def test_C02_repeat_poll_same_device_gets_nothing():
    q = JobQueue()
    q.enqueue("d1", "t", {})
    first = q.poll("d1")
    assert first is not None and first.state == "claimed"
    assert q.poll("d1") is None


def test_C03_cross_device_poll_gets_nothing():
    q = JobQueue()
    q.enqueue("d1", "t", {})
    assert q.poll("d2") is None
    # Owner still claims afterwards.
    assert q.poll("d1") is not None


# ---------- C04-C05 expiry bounds ----------

def test_C04_claim_after_expiry_impossible_complete_no_resurrect():
    q = JobQueue()
    job = q.enqueue("d1", "t", {}, timeout_s=0.05)
    expired = q.wait_for_result(job.id, timeout=0.05)
    assert expired.state == "expired"
    assert q.poll("d1") is None  # expired never returns to pending
    late = q.complete(job.id, True, result={"x": 1})
    assert late.state == "expired"
    assert late.result is None


def test_C05_second_waiter_on_expired_returns_immediately():
    q = JobQueue()
    job = q.enqueue("d1", "t", {}, timeout_s=0.05)
    first = q.wait_for_result(job.id, timeout=0.05)
    assert first.state == "expired"
    t0 = time.monotonic()
    second = q.wait_for_result(job.id, timeout=5.0)
    elapsed = time.monotonic() - t0
    assert second.state == "expired"
    assert elapsed < 4.0  # immediate, not a second full wait


# ---------- C06-C08 cancel precedence ----------

def test_C06_complete_after_cancel_keeps_cancelled():
    q = JobQueue()
    job = q.enqueue("d1", "t", {})
    assert q.poll("d1") is not None
    assert q.cancel(job.id) is True
    late = q.complete(job.id, True, result={"x": 1})
    assert late.state == "cancelled"
    assert late.result is None


def test_C07_duplicate_completion_idempotent():
    q = JobQueue()
    job = q.enqueue("d1", "t", {})
    q.poll("d1")
    first = q.complete(job.id, True, result={"v": 1})
    second = q.complete(job.id, False, error="stale retry")
    assert (second.state, second.result, second.error) == (
        first.state, first.result, first.error)
    assert second.state == "done"


def test_C08_cancel_by_execution_after_claim_wakes_waiter():
    q = JobQueue()
    job = q.enqueue("d1", "t", {}, execution_id="exec-1", timeout_s=5.0)
    assert q.poll("d1") is not None  # claim first...
    outcome = {}

    def waiter():
        outcome["job"] = q.wait_for_result(job.id, timeout=5.0)

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.1)
    assert q.cancel_by_execution("exec-1") is True  # ...then cancel wins
    t.join(timeout=5)
    assert outcome["job"].state == "cancelled"
    assert q.poll("d1") is None


def test_C09_revoke_cancels_pending_only_claimed_stays():
    q = JobQueue()
    pending_job = q.enqueue("d1", "t", {})
    claimed_job = q.enqueue("d1", "t", {})
    assert q.poll("d1").id == pending_job.id
    # Second poll claims the other job; revoke must leave claimed alone.
    assert q.poll("d1").id == claimed_job.id
    q2 = JobQueue()
    p = q2.enqueue("d1", "t", {})
    c = q2.enqueue("d1", "t", {})
    assert q2.poll("d1").id == p.id
    n = q2.cancel_device_jobs("d1")
    assert n == 1
    assert q2.get(p.id).state == "claimed"  # claimed: device-owned, kept
    assert q2.get(c.id).state == "cancelled"  # pending: revoked away
    # Claimed job in q keeps device ownership.
    assert q.get(claimed_job.id).state == "claimed"


# ---------- C10-C12 device claim vs revoke ----------

def test_C10_enroll_revoke_claim_refused_pending_dropped():
    stack = make_stack()
    code = stack["device_auth"].enroll("d1", DeviceKind.LINUX)
    assert stack["device_auth"].revoke("d1") is True
    # Revoke drops the pre-revoke pairing code: no key can be minted.
    with pytest.raises(ValueError, match="invalid or expired"):
        stack["device_auth"].claim(code)
    assert stack["device_auth"].verify("d1", "anything") is False


def test_C10b_claim_on_revoked_credential_refused_even_if_pending():
    stack = make_stack()
    code = stack["device_auth"].enroll("d1", DeviceKind.LINUX)
    # Simulate a pending entry surviving against a revoked credential
    # (defense-in-depth branch): claim must still refuse.
    stack["device_auth"]._creds["d1"].revoked = True
    with pytest.raises(ValueError, match="revoked"):
        stack["device_auth"].claim(code)


def test_C11_revoke_then_reenroll_repairs_via_fresh_credential():
    stack = make_stack()
    enroll_claim(stack, "d1")
    assert stack["device_auth"].revoke("d1") is True
    code = stack["device_auth"].enroll("d1", DeviceKind.LINUX)
    dev, key = stack["device_auth"].claim(code)
    assert dev == "d1"
    assert stack["device_auth"].verify(dev, key) is True


def test_C12_missing_expiry_never_denies_verify():
    stack = make_stack()
    dev, key = enroll_claim(stack, "d1")
    stack["device_auth"]._creds[dev].expires_at = None
    assert stack["device_auth"].verify(dev, key) is True
    assert stack["device_auth"].is_revoked(dev) is False


# ---------- C13-C15 HTTP binding, revoke race, disconnect ----------

def test_C13_queue_complete_unchecked_http_binding_enforced():
    stack = make_stack()
    client = TestClient(create_app(stack))
    dev, key = enroll_claim(stack, "d1")
    register(stack, "d1")
    other, other_key = enroll_claim(stack, "d2")
    register(stack, "d2")
    job = stack["jobs"].enqueue("d1", "t", {})
    # Wrong device is rejected at the HTTP gate with audit, job untouched.
    r = client.post("/v1/agent/jobs/result",
                    json={"job_id": job.id, "ok": True, "result": {}},
                    headers=dheaders(other, other_key))
    assert r.status_code == 403, r.text
    assert stack["jobs"].get(job.id).state == "pending"
    # Owner completes fine.
    r2 = client.post("/v1/agent/jobs/result",
                     json={"job_id": job.id, "ok": True,
                           "result": {"v": 1}},
                     headers=dheaders(dev, key))
    assert r2.status_code == 200, r2.text
    assert stack["jobs"].get(job.id).state == "done"


def test_C14_revoke_between_auth_and_poll_hands_no_payload():
    stack = make_stack()
    client = TestClient(create_app(stack))
    dev, key = enroll_claim(stack, "d1")
    register(stack, "d1")
    job = stack["jobs"].enqueue("d1", "t", {})
    # Simulate revoke landing after auth verify: revoke directly, then
    # poll with the now-dead key must fail closed (403), never a job.
    stack["device_auth"].revoke(dev)
    r = client.post("/v1/agent/jobs/poll", headers=dheaders(dev, key))
    assert r.status_code == 403, r.text
    assert "job" not in (r.json() if r.headers.get("content-type", "")
                         .startswith("application/json") else {})


def test_C15_disconnect_leaves_jobs_owner_can_still_complete():
    stack = make_stack()
    client = TestClient(create_app(stack))
    dev, key = enroll_claim(stack, "d1")
    register(stack, "d1")
    job = stack["jobs"].enqueue("d1", "t", {})
    assert stack["jobs"].poll("d1") is not None
    r = client.post("/v1/agent/disconnect", headers=dheaders(dev, key))
    assert r.status_code == 200, r.text
    # Disconnect is presence-only: the claimed job is untouched...
    assert stack["jobs"].get(job.id).state == "claimed"
    # ...and the (still credentialed) device result is accepted.
    r2 = client.post("/v1/agent/jobs/result",
                     json={"job_id": job.id, "ok": True,
                           "result": {"v": 1}},
                     headers=dheaders(dev, key))
    assert r2.status_code == 200, r2.text
    assert stack["jobs"].get(job.id).state == "done"


def test_C16_revoked_result_rejected_recorded_outcome_kept():
    stack = make_stack()
    client = TestClient(create_app(stack))
    dev, key = enroll_claim(stack, "d1")
    register(stack, "d1")
    job = stack["jobs"].enqueue("d1", "t", {})
    assert stack["jobs"].poll("d1") is not None
    stack["device_auth"].revoke(dev)
    r = client.post("/v1/agent/jobs/result",
                    json={"job_id": job.id, "ok": True, "result": {}},
                    headers=dheaders(dev, key))
    assert r.status_code == 403, r.text
    assert stack["jobs"].get(job.id).state == "claimed"  # no forgery


def test_C17_no_new_job_endpoints_no_gui_privilege_markers():
    import pathlib
    stack = make_stack()
    client = TestClient(create_app(stack))
    paths = sorted({r.path for r in client.app.routes
                    if hasattr(r, "path") and "/jobs/" in r.path})
    # Task 11 mission: additive owner-only state read (device cancel
    # awareness). Poll/result/cancel shapes unchanged.
    assert paths == ["/v1/agent/jobs/poll",
                     "/v1/agent/jobs/result",
                     "/v1/agent/jobs/{job_id}/cancel",
                     "/v1/agent/jobs/{job_id}/state"], paths
    for name in ("android/lib/main.dart", "android/lib/job_runner.dart"):
        text = pathlib.Path(name).read_text()
        assert "Runtime.getRuntime" not in text
        assert "dart:mirrors" not in text
    assert "MethodChannel('zara/device')" in pathlib.Path(
        "android/lib/device_bridge.dart").read_text()  # sanctioned bridge
