"""Device job queue — core dispatches, agents claim and report.

Server side blocks on a Condition (no busy poll); agents poll with adaptive
interval + backoff (documented in docs/LINUX_AGENT.md).
"""
from __future__ import annotations
import threading
import time
import uuid
from typing import Optional
from pydantic import BaseModel, Field
from .models import utcnow


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

    def __init__(self, max_pending_per_device: int = 100) -> None:
        self._jobs: dict[str, DeviceJob] = {}
        self._cond = threading.Condition()
        self._cancelled: set[str] = set()
        self._max_pending = max_pending_per_device

    def enqueue(self, device_id: str, tool: str, inputs: dict,
                mission_id: Optional[str] = None,
                execution_id: Optional[str] = None,
                timeout_s: float = 30.0) -> DeviceJob:
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
        with self._cond:
            self._jobs[job.id] = job
            self._cond.notify_all()
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

    def complete(self, job_id: str, ok: bool, result: Optional[dict] = None,
                 error: str = "") -> DeviceJob:
        with self._cond:
            job = self._jobs[job_id]
            if job.state in ("done", "failed", "cancelled"):
                return job  # idempotent: replays return the recorded outcome
            job.state = "done" if ok else "failed"
            job.result = result
            job.error = error
            job.finished_at = utcnow().isoformat()
            self._cond.notify_all()
            return job

    def cancel(self, job_id: str) -> bool:
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None or job.state in ("done", "failed", "cancelled"):
                return False
            job.state = "cancelled"
            job.finished_at = utcnow().isoformat()
            self._cancelled.add(job_id)
            self._cond.notify_all()
            return True

    def is_cancelled(self, job_id: str) -> bool:
        with self._cond:
            return job_id in self._cancelled

    def wait_for_result(self, job_id: str, timeout: float) -> DeviceJob:
        """Core-side blocking wait on Condition — no busy polling."""
        deadline = time.time() + timeout
        with self._cond:
            while True:
                job = self._jobs[job_id]
                if job.state in ("done", "failed", "cancelled"):
                    return job
                remaining = deadline - time.time()
                if remaining <= 0:
                    job.state = "expired"
                    return job
                self._cond.wait(timeout=min(remaining, 1.0))

    def get(self, job_id: str) -> DeviceJob:
        with self._cond:
            return self._jobs[job_id]
