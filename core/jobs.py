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


class JobQueue:
    CLAIM_TIMEOUT_S = 300.0
    MAX_PENDING_PER_DEVICE = 100

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
        """Agent poll: oldest pending job for this device (or None)."""
        with self._cond:
            for job in self._jobs.values():
                if job.device_id == device_id and job.state == "pending":
                    job.state = "claimed"
                    job.claimed_at = utcnow().isoformat()
                    return job
        return None

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
        """Core-side blocking wait on Condition — no busy polling."""
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
                self._cond.wait(timeout=min(remaining, 1.0))

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
        callers cannot probe other devices' jobs). Pure read under the
        queue lock: no state change, no notify, no prune. Does NOT alter
        claim/complete/cancel/prune semantics.
        """
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.device_id != device_id:
                return None
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
