"""Long-poll job claim tests: bounded server-side wait_for_job + HTTP
wait_secs opt-in on /v1/agent/jobs/poll.

Goal (dispatch-latency mission): a queued job must be claimable by a
CONNECTED device within milliseconds of enqueue instead of waiting for
the device's next periodic tick, while idle request rate stays ~one poll
per wait window (battery-neutral) and every safety property of the old
poll() claim path is preserved unchanged:

- claim is atomic under the queue lock (two pollers never split a job);
- a cancelled pending job is never handed to any device;
- the HTTP route re-validates revocation/cancellation after the claim
  exactly as in the instant path;
- wait_secs=0 keeps the previous always-instant contract (default), and
  the route bounds the hold (le) so a client can never tie a worker
  thread indefinitely.

Scaled windows (0.3-3 s) model production's 25 s holds 1:20ish without
slowing the suite.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from core.app import AGENT_JOB_LONGPOLL_MAX_S, build_stack, create_app
from core.device_tools import DEVICE_TOOL_DEFS
from core.jobs import JobQueue
from core.models import DeviceKind, DeviceState

TOOL = DEVICE_TOOL_DEFS[0].name


# ---------- queue-level wait_for_job ----------

def test_existing_pending_claimed_instantly():
    q = JobQueue()
    q.enqueue("d1", TOOL, {}, timeout_s=300)
    t0 = time.monotonic()
    job = q.wait_for_job("d1", 5.0)
    elapsed = time.monotonic() - t0
    assert job is not None and job.state == "claimed"
    assert elapsed < 0.5, f"existing pending job must claim instantly, took {elapsed:.2f}s"


def test_enqueued_during_hold_wakes_waiter_fast():
    q = JobQueue()

    def later():
        time.sleep(0.3)
        q.enqueue("d1", TOOL, {}, timeout_s=300)

    threading.Thread(target=later, daemon=True).start()
    t0 = time.monotonic()
    job = q.wait_for_job("d1", 5.0)
    elapsed = time.monotonic() - t0
    assert job is not None and job.state == "claimed"
    assert elapsed < 1.5, (
        f"Condition wake must deliver the claim within ~enqueue time, "
        f"took {elapsed:.2f}s (busy polling would also pass this but is "
        f"prohibited by the 1.0s wait quantum design)")
    assert elapsed > 0.2  # actually waited for the enqueue event


def test_timeout_returns_none_bounded():
    q = JobQueue()
    t0 = time.monotonic()
    job = q.wait_for_job("d1", 0.3)
    elapsed = time.monotonic() - t0
    assert job is None
    assert 0.3 <= elapsed < 1.2, "empty hold must respect its deadline"


def test_cancelled_pending_never_claimed():
    q = JobQueue()
    j = q.enqueue("d1", TOOL, {}, timeout_s=300)
    assert q.cancel(j.id) is True
    job = q.wait_for_job("d1", 0.5)
    assert job is None, "a cancelled pending job must never be handed out"


def test_hold_survives_unrelated_device_enqueue():
    """Another device's enqueue must not hand foreign work to this waiter:
    it may wake the Condition, but the loop re-checks and keeps waiting."""
    q = JobQueue()

    def later():
        time.sleep(0.2)
        q.enqueue("other", TOOL, {}, timeout_s=300)

    threading.Thread(target=later, daemon=True).start()
    t0 = time.monotonic()
    job = q.wait_for_job("d1", 0.6)
    assert job is None
    assert 0.6 <= time.monotonic() - t0 < 1.5


def test_concurrent_longpolls_never_split_one_job():
    q = JobQueue()
    q.enqueue("d1", TOOL, {}, timeout_s=300)
    claimed = []
    lock = threading.Lock()

    def w():
        j = q.wait_for_job("d1", 2.0)
        if j is not None:
            with lock:
                claimed.append(j.id)

    ts = [threading.Thread(target=w, daemon=True) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=4)
    assert len(claimed) == 1, (
        f"exactly one poller may claim the job, saw {len(claimed)}")


def test_two_jobs_two_pollers():
    q = JobQueue()
    for _ in range(2):
        q.enqueue("d1", TOOL, {}, timeout_s=300)
    got = []
    lock = threading.Lock()

    def w():
        j = q.wait_for_job("d1", 2.0)
        if j is not None:
            with lock:
                got.append(j.id)

    ts = [threading.Thread(target=w, daemon=True) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=4)
    assert len(got) == 2 and len(set(got)) == 2


# ---------- HTTP route contract ----------

def _stack_with_device():
    stack = build_stack()
    code = stack["device_auth"].enroll("emu-x", DeviceKind.ANDROID)
    _, key = stack["device_auth"].claim(code)
    stack["devices"].register(DeviceState(
        device_id="emu-x", kind=DeviceKind.ANDROID, capabilities=[TOOL],
        online=True, status="online"))
    return stack, "emu-x", key


def test_http_default_poll_stays_instant():
    stack, dev, key = _stack_with_device()
    client = TestClient(create_app(stack))
    t0 = time.monotonic()
    r = client.post("/v1/agent/jobs/poll",
                    headers={"X-Device-Id": dev, "X-Device-Key": key})
    assert r.status_code == 200 and r.json() == {"job": None}
    assert time.monotonic() - t0 < 0.5, "default contract: instant empty"


def test_http_longpoll_returns_job_enqueued_mid_hold():
    stack, dev, key = _stack_with_device()
    client = TestClient(create_app(stack))

    def later():
        time.sleep(0.25)
        stack["jobs"].enqueue(dev, TOOL, {"x": 1}, timeout_s=300)

    threading.Thread(target=later, daemon=True).start()
    t0 = time.monotonic()
    r = client.post("/v1/agent/jobs/poll?wait_secs=5",
                    headers={"X-Device-Id": dev, "X-Device-Key": key})
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    body = r.json()["job"]
    assert body is not None and body["tool"] == TOOL
    assert elapsed < 2.0, f"wake delivered the claim fast, took {elapsed:.2f}s"
    assert elapsed > 0.2  # genuinely held until the enqueue event


def test_http_longpoll_unauth_fails_closed():
    stack, dev, key = _stack_with_device()
    client = TestClient(create_app(stack))
    r = client.post("/v1/agent/jobs/poll?wait_secs=5",
                    headers={"X-Device-Id": dev, "X-Device-Key": "bogus"})
    assert r.status_code in (401, 403)


def test_http_wait_secs_bounded():
    """The route must refuse unbounded holds (422) — no worker-thread
    tie-up, no silent clamp."""
    stack, dev, key = _stack_with_device()
    client = TestClient(create_app(stack))
    r = client.post(
        f"/v1/agent/jobs/poll?wait_secs={AGENT_JOB_LONGPOLL_MAX_S + 0.1}",
        headers={"X-Device-Id": dev, "X-Device-Key": key})
    assert r.status_code == 422


def test_http_longpoll_revoked_mid_hold_never_gets_job():
    stack, dev, key = _stack_with_device()
    client = TestClient(create_app(stack))

    def later():
        time.sleep(0.2)
        # revoked device's queued work is swept; revocation wins
        stack["device_auth"].revoke(dev)
        stack["jobs"].enqueue(dev, TOOL, {"x": 1}, timeout_s=300)

    threading.Thread(target=later, daemon=True).start()
    r = client.post("/v1/agent/jobs/poll?wait_secs=3",
                    headers={"X-Device-Id": dev, "X-Device-Key": key})
    # Revoked identity: never a payload — fail closed 403 either at auth
    # verify or at the post-claim re-check.
    assert r.status_code == 403
    assert (r.json().get("job") or {}).get("tool") != TOOL \
        if isinstance(r.json().get("job"), dict) else True
    assert not r.json().get("job"), "revoked device must receive no payload"


def test_http_misc_envelope_shape_unchanged():
    """The response envelope for claim+instant path must be byte-shape
    identical to the old contract (job fields exactly)."""
    stack, dev, key = _stack_with_device()
    client = TestClient(create_app(stack))
    j = stack["jobs"].enqueue(dev, TOOL, {"a": 1}, timeout_s=300)
    r = client.post("/v1/agent/jobs/poll",
                    headers={"X-Device-Id": dev, "X-Device-Key": key})
    body = r.json()["job"]
    assert set(body) == {"job_id", "tool", "inputs", "timeout_s",
                         "execution_id", "mission_id"}
    assert body["job_id"] == j.id
    assert body["timeout_s"] == 300.0
