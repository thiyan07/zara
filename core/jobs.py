"""Device job queue — core dispatches, agents claim and report.

Server side blocks on a Condition (no busy poll); agents poll with adaptive
interval + backoff (documented in docs/LINUX_AGENT.md).
"""
from __future__ import annotations
import json
import threading
import time
import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
from .models import utcnow

# Task 9 retention/TTL contract (see docs/research/EXECUTION_RETENTION_TTL_AUDIT.md).
# The queue is an ownership + wakeup mechanism, not an archive: terminal
# jobs are retained boundedly (count + age) for status joins and
# idempotent replays, then reaped. Active jobs (pending/claimed) are
# NEVER reaped: deletion must not strand a waiter or steal an actuation.
TERMINAL_JOB_STATES = ("done", "failed", "cancelled", "expired")
TERMINAL_RETENTION_MAX = 500
TERMINAL_RETENTION_S = 24 * 3600.0
# Device-supplied result envelope: stored verbatim and served on every
# status poll, so it must be bounded at ingest (fail-closed 422/ValueError,
# never silent truncation — truncation would falsify verification).
JOB_RESULT_MAX_BYTES = 65536
JOB_ERROR_MAX_CHARS = 4096


class DeviceJob(BaseModel):
    id: str = ""
    device_id: str = ""
    tool: str = ""
    inputs: dict = Field(default_factory=dict)
    mission_id: Optional[str] = None
    execution_id: Optional[str] = None
    timeout_s: float = 30.0
    state: str = "pending"  # pending|claimed|done|failed|cancelled|expired
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: str = ""
    claimed_at: Optional[str] = None
    finished_at: Optional[str] = None
    # Zombie-claim repair (see JobQueue.CLAIM_ACK_TIMEOUT_S): deliveries
    # counts claims handed out (any claim path); claim_ack flips True on
    # the owner's first state read of the claimed job. Re-parking clears
    # claimed_at and claim_ack but never deliveries.
    deliveries: int = 0
    claim_ack: bool = False


class JobQueue:
    CLAIM_TIMEOUT_S = 300.0
    MAX_PENDING_PER_DEVICE = 100
    # P5 ⑯ (2026-10-23): long-poll-park wakeup granularity. The parked
    # waiter must wake on enqueue/cancel notify_all (immediate serve) and
    # otherwise at most this often to re-check its own deadline — a slice,
    # not a busy poll (each wake re-checks state under the lock).
    JOB_POLL_SLICE_S = 2.0
    # P5 ⑯ zombie-claim repair (live-caught 2026-10-23): a claim handed
    # to a long-poll whose client has vanished is invisible to the
    # queue (the in-memory claim already happened; the response goes to
    # a dead socket). The claimant's immediate owner-scoped state probe
    # (agent is_job_cancelled → /v1/agent/jobs/{id}/state) doubles as
    # the DELIVERY RECEIPT ("claim_ack"): a claim never acked in this
    # window is presumed delivered to a dead connection and re-parked
    # for redelivery; after this many deliveries without a receipt the
    # job terminates expired (never resurrected — Task 8 invariant).
    # Bounded at-least-once: a device may run a re-delivered tool
    # twice; device tools stay honest under re-run (a calendar move's
    # second run refuses at selection: the old start is gone).
    CLAIM_ACK_TIMEOUT_S = 8.0
    MAX_JOB_DELIVERIES = 3

    def __init__(self, max_pending_per_device: int = 100,
                 max_terminal_records: int = TERMINAL_RETENTION_MAX,
                 terminal_retention_s: float = TERMINAL_RETENTION_S) -> None:
        self._jobs: dict[str, DeviceJob] = {}
        self._cond = threading.Condition()
        self._cancelled: set[str] = set()
        self._max_pending = max_pending_per_device
        self._max_terminal = max_terminal_records
        self._terminal_retention_s = terminal_retention_s

    def enqueue(self, device_id: str, tool: str, inputs: dict,
                mission_id: Optional[str] = None,
                execution_id: Optional[str] = None,
                timeout_s: float = 30.0) -> DeviceJob:
        # Single critical section: pending-count check and insert must be
        # atomic (two sections previously allowed overfilling the cap).
        with self._cond:
            pending = sum(1 for j in self._jobs.values()
                          if j.device_id == device_id and j.state == "pending")
            if pending >= self._max_pending:
                raise OverflowError(
                    f"device {device_id} job queue full ({pending})")
            job = DeviceJob(id=f"job-{uuid.uuid4().hex[:12]}", device_id=device_id,
                            tool=tool, inputs=dict(inputs), mission_id=mission_id,
                            execution_id=execution_id, timeout_s=timeout_s,
                            created_at=utcnow().isoformat())
            self._jobs[job.id] = job
            self._cond.notify_all()
            need_prune = len(self._jobs) > 2 * self._max_terminal
            if need_prune:
                self._prune_locked(None, self._max_terminal,
                                   self._terminal_retention_s)
            return job

    def poll(self, device_id: str) -> Optional[DeviceJob]:
        """Agent poll: oldest pending job for this device (or None).
        The handout is recorded as a delivery (zombie-claim repair)."""
        with self._cond:
            for job in self._jobs.values():
                if job.device_id == device_id and job.state == "pending":
                    job.state = "claimed"
                    job.deliveries += 1
                    job.claimed_at = utcnow().isoformat()
                    return job
        return None

    def wait_for_job(self, device_id: str, timeout: float) -> Optional[DeviceJob]:
        """Server-side long poll: claim the oldest pending job for this
        device, or block (Condition wait — no busy poll) until one arrives
        or the bounded timeout expires. Claim semantics are identical to
        poll() and equally atomic (same lock, same first-match loop), so
        two concurrent long polls can never both claim the same job.
        Wakes on enqueue AND on any cancel/complete/prune notification:
        a pending job of this device being cancelled just re-checks and
        keeps waiting until its own deadline — a cancelled job is never
        handed out. Monotonic deadline like wait_for_result (Task 9):
        wall-clock jumps neither strand a waiter nor oversleep.
        Caller (HTTP handler) still re-validates revocation/cancellation
        AFTER the claim, exactly as with poll()."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for job in self._jobs.values():
                    if job.device_id == device_id and job.state == "pending":
                        job.state = "claimed"
                        job.deliveries += 1
                        job.claimed_at = utcnow().isoformat()
                        return job
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                # Park-wait slice: enqueue/cancel notify_all wakes this
                # immediately; only the deadline re-check needs the slice.
                # P5 ⑯ live-caught (2026-10-23): this line referenced a
                # bare JOB_POLL_SLICE_S (class attr is invisible to the
                # bare name) — every parked long poll died on NameError
                # → HTTP 500 → the agent's poll_job swallowed it and
                # re-polled at full speed (196k rounds/80 s, a 429
                # storm). self.<class const> it is.
                self._cond.wait(timeout=min(remaining,
                                            self.JOB_POLL_SLICE_S))

    def complete_once(self, job_id: str, ok: bool,
                      result: Optional[dict] = None,
                      error: str = "") -> tuple:
        """Accept a device result AND atomically report whether this call
        performed the open->terminal transition: (job, True) for the first
        accepted delivery, (job, False) for replays and late results after
        terminal states. The flag is decided under the same Condition lock
        as the mutation, so concurrent duplicate deliveries cannot both
        observe True. Callers must emit first-delivery side effects
        (bus/audit) on True OUTSIDE the lock — never hold queue state
        across I/O. Bounds/validation identical to complete()."""
        if result:
            try:
                blob = json.dumps(result)
            except Exception:  # noqa: BLE001 — unserializable is invalid
                raise ValueError(f"job {job_id} result not serializable")
            if len(blob) > JOB_RESULT_MAX_BYTES:
                raise ValueError(
                    f"job {job_id} result exceeds "
                    f"{JOB_RESULT_MAX_BYTES} bytes")
        if error and len(error) > JOB_ERROR_MAX_CHARS:
            raise ValueError(
                f"job {job_id} error exceeds "
                f"{JOB_ERROR_MAX_CHARS} chars")
        with self._cond:
            job = self._jobs[job_id]
            if job.state in ("done", "failed", "cancelled", "expired"):
                return job, False  # idempotent: replays and late results
                # after expiry return the recorded outcome; Core already
                # failed the execution, and expiry must never resurrect.
            job.state = "done" if ok else "failed"
            job.result = result
            job.error = error
            job.finished_at = utcnow().isoformat()
            self._cond.notify_all()
            return job, True

    def complete(self, job_id: str, ok: bool, result: Optional[dict] = None,
                 error: str = "") -> DeviceJob:
        # Bounded ingest (Task 9, T3): a device payload is stored verbatim
        # and served on every status poll, so oversize input is rejected
        # here for direct callers (HTTP additionally rejects at the
        # JobResultIn boundary). Never silently truncate: truncation would
        # falsify verification and result content.
        job, _ = self.complete_once(job_id, ok, result, error)
        return job

    def cancel(self, job_id: str) -> bool:
        with self._cond:
            job = self._jobs.get(job_id)
            # One shared terminal set: expired is terminal, so cancelling it
            # is a no-op (previously expired→cancelled rewrote history).
            if job is None or job.state in ("done", "failed", "cancelled",
                                            "expired"):
                return False
            job.state = "cancelled"
            job.finished_at = utcnow().isoformat()
            self._cancelled.add(job_id)
            self._cond.notify_all()
            return True

    def is_cancelled(self, job_id: str) -> bool:
        with self._cond:
            return job_id in self._cancelled

    def cancel_by_execution(self, execution_id: str) -> bool:
        """Cancel the still-open job enqueued for an execution, if any.
        Used by engine.cancel so operator cancellation reaches the queue
        instead of stranding the proxy waiter until timeout."""
        with self._cond:
            for job in self._jobs.values():
                if (job.execution_id == execution_id
                        and job.state in ("pending", "claimed")):
                    job.state = "cancelled"
                    job.finished_at = utcnow().isoformat()
                    self._cancelled.add(job.id)
                    self._cond.notify_all()
                    return True
        return False

    def cancel_device_jobs(self, device_id: str) -> int:
        """Cancel all still-pending (never claimed) jobs for a device.
        Called on revocation so revocation wins over stale queued work
        and blocked waiters wake promptly. Claimed jobs are left alone:
        the device may already be actuating, and Core-side fiction must
        not pretend otherwise (device result endpoint still enforces
        auth, so revoked devices fail closed there)."""
        n = 0
        with self._cond:
            for job in self._jobs.values():
                if job.device_id == device_id and job.state == "pending":
                    job.state = "cancelled"
                    job.finished_at = utcnow().isoformat()
                    self._cancelled.add(job.id)
                    n += 1
            if n:
                self._cond.notify_all()
        return n

    def wait_for_result(self, job_id: str, timeout: float) -> DeviceJob:
        """Core-side blocking wait on Condition — no busy polling.

        P5 ⑯ zombie-claim redelivery: the proxy waiter also sweeps its
        own job. A claim older than CLAIM_ACK_TIMEOUT_S with no owner
        receipt (claim_ack) was delivered to a vanished long-poll client
        — re-park it to pending (redelivery, wake any live parked poll)
        unless the delivery cap is exhausted, in which case the job
        terminates expired exactly like the pre-repair contract (Task 8:
        expired stays terminal, never resurrected). Acked claims and
        pending jobs are left alone; the waiter's own deadline expiry
        expires the job unchanged, so all short-timeout pins still hold
        (their timeouts are far below the ack window)."""
        # Monotonic deadline (Task 9): wall-clock jumps must neither strand
        # a waiter nor expire a claim early. Record stamps stay wall-clock.
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                job = self._jobs[job_id]
                if job.state in ("done", "failed", "cancelled", "expired"):
                    return job
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    job.state = "expired"
                    job.finished_at = utcnow().isoformat()
                    self._cond.notify_all()
                    return job
                if (job.state == "claimed" and not job.claim_ack):
                    age = self._claim_age_s(job)
                    if age > self.CLAIM_ACK_TIMEOUT_S:
                        if job.deliveries >= self.MAX_JOB_DELIVERIES:
                            job.state = "expired"
                            job.finished_at = utcnow().isoformat()
                            self._cond.notify_all()
                            return job
                        job.state = "pending"
                        job.claimed_at = None
                        job.claim_ack = False
                        self._cond.notify_all()
                self._cond.wait(timeout=min(remaining, 1.0))

    def _claim_age_s(self, job: DeviceJob) -> float:
        """Seconds since the current claim (claimed_at, wall-clock).
        Unparseable/missing stamps count as maximally stale — a receipt
        that cannot be aged cannot be trusted to arrive."""
        if not job.claimed_at:
            return self.CLAIM_ACK_TIMEOUT_S + 1.0
        try:
            claimed = datetime.fromisoformat(job.claimed_at)
        except (ValueError, TypeError):
            return self.CLAIM_ACK_TIMEOUT_S + 1.0
        try:
            return (utcnow() - claimed).total_seconds()
        except TypeError:  # naive vs aware stamp mix: cannot trust it
            return self.CLAIM_ACK_TIMEOUT_S + 1.0

    def get_by_execution(self, execution_id: str) -> Optional[DeviceJob]:
        """Read-time execution→job join for status views (O(n), fine for
        lab scale). No stored reverse pointer, no second state machine."""
        with self._cond:
            for job in self._jobs.values():
                if job.execution_id == execution_id:
                    return job
        return None

    def get(self, job_id: str) -> DeviceJob:
        with self._cond:
            return self._jobs[job_id]

    def job_state_for(self, device_id: str, job_id: str) -> Optional[str]:
        """Owner-scoped state read for the agent cancel-poll endpoint.

        Returns job.state iff job.device_id == device_id, else None
        (wrong owner, unknown id, or pruned id all read back as None so
        callers cannot probe other devices' jobs). P5 ⑯: the owner's
        read of a CLAIMED job is also its delivery receipt (claim_ack —
        the one liveness signal a live claimant emits within seconds of
        claiming); nothing else mutates here, and no notify fires (the
        receipt never unblocks a waiter on its own)."""
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.device_id != device_id:
                return None
            # P5 ⑯ zombie-claim repair: the owner reading its OWN claimed
            # job's state is the delivery receipt — the live agent's
            # run_one_job probes this endpoint immediately after
            # claiming, so an unacked claim is a claim nobody holds.
            # Other owners' reads stay non-mutating (wrong device_id
            # returned None above without touching state).
            if job.state == "claimed":
                job.claim_ack = True
            return job.state

    def prune(self, now: Optional[datetime] = None,
              max_terminal: Optional[int] = None,
              retention_s: Optional[float] = None) -> int:
        """Reap terminal jobs boundedly. Returns the number removed.

        Contract (Task 9): only jobs in TERMINAL_JOB_STATES are eligible,
        and only when older than retention_s (by finished_at) or beyond
        the newest max_terminal (by finished_at). Active jobs
        (pending/claimed) are never touched; terminal jobs with missing
        or unparseable finished_at are retained (fail-closed: never delete
        what cannot be aged). Stale _cancelled ids for unknown jobs are
        swept. Idempotent, single critical section, never resurrects.
        Evicted ids read back as unknown (KeyError → 404), exactly like
        never-created ids. now is injectable for deterministic tests.
        """
        if now is None:
            now = utcnow()
        if max_terminal is None:
            max_terminal = self._max_terminal
        if retention_s is None:
            retention_s = self._terminal_retention_s
        with self._cond:
            return self._prune_locked(now, max_terminal, retention_s)

    def _prune_locked(self, now: Optional[datetime], max_terminal: int,
                      retention_s: float) -> int:
        """Prune implementation; caller must hold self._cond (used by the
        lazy enqueue trigger to avoid a second acquisition)."""
        if now is None:
            now = utcnow()
        kill: set[str] = set()
        ageable: list[tuple[str, datetime]] = []
        for jid, job in self._jobs.items():
            if job.state not in TERMINAL_JOB_STATES:
                continue
            if not job.finished_at:
                continue  # unageable: retain
            try:
                fin = datetime.fromisoformat(job.finished_at)
                age = (now - fin).total_seconds()
            except Exception:  # noqa: BLE001 — unparseable: retain
                continue
            if age > retention_s:
                kill.add(jid)
            else:
                ageable.append((jid, fin))
        if len(ageable) > max_terminal:
            try:
                ageable.sort(key=lambda item: item[1])
            except Exception:  # noqa: BLE001 — incomparable: retain
                ageable = []
            for jid, _ in ageable[:len(ageable) - max_terminal]:
                kill.add(jid)
        for jid in kill:
            del self._jobs[jid]
        # Sweep cancel markers for jobs that no longer exist; markers for
        # live jobs are kept (poll suppression still applies). Unconditional
        # and idempotent: unknown ids can never collide with future jobs
        # (uuid), so this only bounds set growth.
        self._cancelled = {jid for jid in self._cancelled
                           if jid in self._jobs}
        if kill:
            self._cond.notify_all()
        return len(kill)
