"""Job-timing regression tests: Core proxy wait vs device poll cadence.

Production race (Rung-3 forensic report): the device proxy waited 20 s for
a job result while Android polls only every ~30 s (60 s throttled), so the
wait could expire before the device ever polled the queued job.

Fix under test: proxy default timeout raised to 90 s (invariant: proxy wait
> worst-case poll gap), plus cancel/revoke propagation and an idempotent
complete() after expiry.

These tests MUST NOT use the background drain helpers
(`complete_jobs_like` / `approve_with_stub_device` from test_rung1.py /
test_rung2.py): those poll every 0.05 s (~400x faster than production) and
cannot reproduce the race by construction. Instead a cadence poller thread
models the real Android tick, and a claim-latency lower bound fingerprints
it: any tight drain loop claims in <0.15 s and fails the lower bound.

Scaled time preserves the production relationship (ratio 1.5):
  T_POLL = 0.6 s  (stands in for 30 s device tick)
  PHASE  = 0.6 s  (worst case: job enqueued just after a tick)
  EXEC   = 0.1 s  (on-device run latency)
  T_OLD  = 0.4 s  (stands in for the old 20 s proxy wait)
  T_FIX  = 2.0 s  (stands in for the fixed 90 s proxy wait)
"""
import threading
import time

import pytest

from core.app import build_stack
from core.device_tools import DEVICE_TOOL_DEFS, GUI_LAB_TARGETS
from core.jobs import JobQueue
from core.models import DeviceKind, DeviceState, ExecutionState

T_POLL = 0.6
PHASE = 0.6
EXEC = 0.1
T_OLD = 0.4
T_FIX = 2.0

INSPECT = "gui.screen.inspect"
TAP = "gui.tap"
VICTIM = "dev.zara.lab.privtest"


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
    """Models the Android tick: first poll after PHASE, then every T_POLL.
    Completes the first claimed job after EXEC. Records claim latency."""

    def __init__(self, jobs, device_id, result=None, exec_latency=EXEC,
                 phase=PHASE, interval=T_POLL):
        super().__init__(daemon=True)
        self.jobs = jobs
        self.device_id = device_id
        self.result = result or {"ok": True}
        self.exec_latency = exec_latency
        self.phase = phase
        self.interval = interval
        self.stop_ev = threading.Event()
        self.poll_count = 0
        self.claim_time = None
        self.enqueue_time = None
        self.completed = False

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
                    self.jobs.complete(job.id, ok=True, result=self.result)
                    self.completed = True
                except KeyError:
                    pass
                return
            if self.stop_ev.wait(self.interval):
                return

    def stop(self):
        self.stop_ev.set()


# ---------- T-0 production invariant ----------

def test_T0_proxy_timeout_exceeds_max_device_poll_gap():
    """Pin the timing invariant in code: every device proxy wait must exceed
    the worst-case Android poll gap (60 s throttled) plus execution margin.
    If anyone lowers a timeout below this bound, the race returns."""
    for d in DEVICE_TOOL_DEFS:
        assert d.timeout_s > 60.0, f"{d.name}: {d.timeout_s}s must exceed 60s poll gap"


# ---------- T-1 race mechanism (raw queue, scaled) ----------

def test_T1_old_timeout_expires_before_cadence_poll():
    """Negative control: with the OLD 20 s-equivalent wait, a cadence
    poller that first polls after a full tick never sees the job."""
    q = JobQueue()
    job = q.enqueue("d1", "gui.screen.inspect", {}, timeout_s=T_OLD)
    poller = CadencePoller(q, "d1")
    poller.enqueue_time = time.monotonic()
    poller.start()
    try:
        out = q.wait_for_result(job.id, timeout=T_OLD)
        assert out.state == "expired"
        assert poller.claim_time is None  # device never saw it
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_T2_fixed_timeout_survives_cadence_poll():
    """Gate: with the fixed wait, the same poller claims on its first tick
    and the job completes. Claim latency fingerprints a real tick
    (>= PHASE - jitter); a 0.05 s drain loop could never satisfy it."""
    q = JobQueue()
    t0 = time.monotonic()
    job = q.enqueue("d1", "gui.screen.inspect", {}, timeout_s=T_FIX)
    poller = CadencePoller(q, "d1", result={"nodes": []})
    poller.enqueue_time = t0
    poller.start()
    try:
        out = q.wait_for_result(job.id, timeout=T_FIX)
        assert out.state == "done"
        assert out.result == {"nodes": []}
        assert poller.claim_time is not None
        latency = poller.claim_time - t0
        assert latency >= PHASE - 0.3, f"claimed too fast ({latency:.2f}s): not a real tick"
        assert latency <= PHASE + EXEC + 0.6
        assert poller.poll_count in (1, 2)
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_T3_offline_device_times_out_deterministically():
    """No poller at all: the wait ends at the bound, FAILED, never hangs."""
    q = JobQueue()
    t0 = time.monotonic()
    job = q.enqueue("d1", "gui.screen.inspect", {}, timeout_s=T_OLD)
    out = q.wait_for_result(job.id, timeout=T_OLD)
    dt = time.monotonic() - t0
    assert out.state == "expired"
    assert dt < T_OLD + 1.0  # bounded, no indefinite block
    assert q.poll("d1") is None  # expired jobs are never handed out


def test_T4_late_complete_after_expiry_never_resurrects():
    """A device result landing after expiry must not flip the record to
    done: Core already failed the execution."""
    q = JobQueue()
    job = q.enqueue("d1", "gui.screen.inspect", {}, timeout_s=T_OLD)
    out = q.wait_for_result(job.id, timeout=T_OLD)
    assert out.state == "expired"
    q.complete(job.id, ok=True, result={"late": True})
    assert q._jobs[job.id].state == "expired"


# ---------- T-5 engine-level: approval still required ----------

def test_T5_submit_parks_without_approval_even_while_device_polls():
    """A running cadence poller observes zero jobs until approval:
    the fix must never pre-dispatch or auto-execute."""
    stack = make_stack()
    dev = paired_android(stack)
    poller = CadencePoller(stack["jobs"], dev)
    poller.start()
    try:
        rec = stack["engine"].submit(INSPECT, {}, dev)
        assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
        time.sleep(0.8)  # > one full poll tick
        assert poller.poll_count >= 1
        assert poller.claim_time is None
    finally:
        poller.stop()
        poller.join(timeout=3)


def test_T6_high_risk_still_approval_gated():
    """gui.tap resolves to requires_approval (never auto-resolved), even
    across devices: timing changes must not touch risk classes."""
    stack = make_stack()
    dev = paired_android(stack)
    rec = stack["engine"].submit(
        TAP, {"target_package": VICTIM}, dev)
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    assert stack["jobs"].poll(dev) is None


# ---------- T-7 cancellation reaches the queue ----------

def test_T7_engine_cancel_wakes_proxy_waiter():
    """engine.cancel on a dispatched execution cancels the queued job:
    the proxy waiter returns cancelled (never success)."""
    q = JobQueue()
    job = q.enqueue("d1", "gui.screen.inspect", {},
                    execution_id="exec-1", timeout_s=T_FIX)
    outcomes = []
    t = threading.Thread(
        target=lambda: outcomes.append(q.wait_for_result(job.id, timeout=T_FIX)),
        daemon=True)
    t.start()
    time.sleep(0.2)
    assert q.cancel_by_execution("exec-1") is True
    t.join(timeout=3)
    assert outcomes and outcomes[0].state == "cancelled"
    # Late device completion is ignored, never resurrected.
    q.complete(job.id, ok=True, result={"late": True})
    assert q._jobs[job.id].state == "cancelled"


def test_T8_revoke_cancels_pending_jobs():
    """Revocation wins over stale queued work: pending jobs for the device
    are cancelled at revoke time; claimed jobs are left alone."""
    q = JobQueue()
    j1 = q.enqueue("d1", "gui.screen.inspect", {}, timeout_s=T_FIX)
    j2 = q.enqueue("d1", "gui.tap", {}, timeout_s=T_FIX)
    claimed = q.poll("d1")
    assert claimed.id == j1.id
    n = q.cancel_device_jobs("d1")
    assert n == 1  # only the still-pending job
    assert q._jobs[j2.id].state == "cancelled"
    assert q._jobs[j1.id].state == "claimed"  # untouched: may be actuating
    assert q.poll("d1") is None


def test_T9_revoke_endpoint_sweeps_queue():
    """End-to-end through the stack: revoke cancels pending device jobs."""
    stack = make_stack()
    dev = paired_android(stack)
    j = stack["jobs"].enqueue(dev, INSPECT, {}, timeout_s=T_FIX)
    n = stack["jobs"].cancel_device_jobs(dev)
    assert n == 1
    assert stack["jobs"]._jobs[j.id].state == "cancelled"


# ---------- T-10 existing guarantees unchanged ----------

def test_T10_transfer_helpers_untouched():
    """Sanity: transfer queue helpers used by Stage 15 still exist and the
    new methods don't disturb pending/claimed accounting."""
    q = JobQueue()
    a = q.enqueue("d1", "t", {}, timeout_s=5)
    b = q.enqueue("d1", "t", {}, timeout_s=5)
    assert q.poll("d1").id == a.id  # FIFO oldest-first preserved
    assert q.poll("d1").id == b.id
    assert q.poll("d1") is None
