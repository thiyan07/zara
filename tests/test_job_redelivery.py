"""P5 feature-hunt ⑯ (2026-10-23) — zombie-claim redelivery.

Live-caught failure chain (batch 16, Laptop): a postponed move's device
job was CLAIMED seconds after dispatch but no result ever arrived; the
agent had been restarted repeatedly, and a parked long-poll from a
killed daemon (wait_secs request still held server-side) claimed the
fresh job when approve() enqueued it — the claim's response went to a
socket nobody reads. The job expired while the LIVE agent polled on,
blissfully unaware (it never saw an offer).

Contract pinned here:
- the owner's state read of a CLAIMED job is the delivery RECEIPT
  (claim_ack) — the live agent's is_job_cancelled probe emits it
  within milliseconds of claiming;
- an unacked claim older than CLAIM_ACK_TIMEOUT_S is re-parked to
  pending by the proxy waiter itself (redelivery; the parked live
  long-poll claims it and completes normally);
- redelivery is BOUNDED (MAX_JOB_DELIVERIES); the cap exhausted with
  no receipt terminates the job expired — terminal, never resurrected
  (Task 8 invariant intact);
- acked claims are never re-parked (a slow tool is not a zombie);
- a receipt is owner-scoped: another device's read neither acks nor
  reveals state;
- a result post landing on a re-parked (pending) job still completes
  it (the tool DID run; completion beats a superfluous redelivery).
"""
import threading
import time

import pytest

from core.jobs import JobQueue


def _wait_state(q, job_id, want, deadline=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline:
        if q.get(job_id).state == want:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def q():
    queue = JobQueue()
    # shrink the ack window so tests exercise redelivery without real
    # second-scale waits (instance attribute shadows the class const —
    # wait_for_result reads it through self).
    queue.CLAIM_ACK_TIMEOUT_S = 0.4
    return queue


def test_zombie_claim_redelivers_and_completes(q):
    job = q.enqueue("laptop-1", "calendar.event.move",
                    {"summary": "D", "new_start": "20261024T093000Z"})
    zombie = q.poll("laptop-1")
    assert zombie.id == job.id
    assert zombie.deliveries == 1
    assert not zombie.claim_ack

    out = {}
    t = threading.Thread(target=lambda: out.update(
        r=q.wait_for_result(job.id, timeout=30.0)), daemon=True)
    t.start()
    # the waiter re-parks the unacked claim after one ack window
    assert _wait_state(q, job.id, "pending"), out
    live = q.poll("laptop-1")            # the real agent claims
    assert live.deliveries == 2
    assert q.job_state_for("laptop-1", job.id) == "claimed"  # receipt
    assert q.get(job.id).claim_ack is True
    fin = q.complete(job.id, True, {"ok": True})
    t.join(timeout=5)
    assert fin.state == "done"
    assert out["r"].state == "done"
    assert out["r"].result == {"ok": True}


def test_delivery_cap_exhausts_to_expired_never_resurrects(q):
    q.MAX_JOB_DELIVERIES = 3
    job = q.enqueue("d1", "t", {})
    first = q.poll("d1")
    assert first.deliveries == 1
    out = {}
    t = threading.Thread(target=lambda: out.update(
        r=q.wait_for_result(job.id, timeout=30.0)), daemon=True)
    t.start()
    assert _wait_state(q, job.id, "pending")
    assert q.poll("d1").deliveries == 2          # zombie again, no receipt
    assert _wait_state(q, job.id, "pending")
    third = q.poll("d1")
    assert third.deliveries == 3                 # last allowed delivery
    assert _wait_state(q, job.id, "expired", deadline=8.0), out
    t.join(timeout=5)
    assert out["r"].state == "expired"
    # terminal stays terminal: late completion is refused, pending re-issue never happens
    late = q.complete_once(third.id, True, {"ok": True})
    assert late[1] is False
    assert q.poll("d1") is None


def test_acked_claim_is_never_reparked(q):
    job = q.enqueue("d1", "t", {})
    claimed = q.poll("d1")
    # a slow-but-live claimant: its probe acked immediately
    assert q.job_state_for("d1", job.id) == "claimed"
    assert q.get(job.id).claim_ack is True
    out = {}
    t = threading.Thread(target=lambda: out.update(
        r=q.wait_for_result(job.id, timeout=3.0)), daemon=True)
    t.start()
    time.sleep(1.0)  # far past the shrunken ack window
    assert q.get(job.id).state == "claimed"      # untouched
    q.complete(job.id, True, {"ok": 1})
    t.join(timeout=5)
    assert out["r"].state == "done"


def test_receipt_is_owner_scoped(q):
    job = q.enqueue("d1", "t", {})
    q.poll("d1")
    assert q.job_state_for("Other", job.id) is None
    assert q.get(job.id).claim_ack is False      # foreign read: no ack
    q.job_state_for("d1", job.id)
    assert q.get(job.id).claim_ack is True


def test_result_post_on_reparked_job_still_completes(q):
    job = q.enqueue("d1", "t", {})
    q.poll("d1")                                  # zombie holds nothing
    out = {}
    t = threading.Thread(target=lambda: out.update(
        r=q.wait_for_result(job.id, timeout=30.0)), daemon=True)
    t.start()
    assert _wait_state(q, job.id, "pending")
    # the real claimant ran anyway (double-run is the at-least-once
    # caveat) and its completion lands while the job sits pending:
    fin = q.complete(job.id, True, {"ok": "late-but-done"})
    assert fin.state == "done"
    t.join(timeout=5)
    assert out["r"].state == "done"


def test_short_deadline_expires_before_any_redelivery(q):
    # existing pin re-checked on top of the redelivery machinery: the
    # waiter's own deadline expires a claimed job unchanged (Task 8:
    # claimed -> expired stays reachable and terminal).
    job = q.enqueue("d1", "t", {})
    q.poll("d1")
    out = q.wait_for_result(job.id, timeout=0.05)
    assert out.state == "expired"
    assert q.poll("d1") is None
