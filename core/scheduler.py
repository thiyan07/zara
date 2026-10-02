"""Scheduler — foundation for scheduled + event-driven jobs."""
from __future__ import annotations
import threading
import time
import uuid

class Scheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._fired: list[dict] = []

    def schedule_once(self, name: str, delay_s: float, payload: dict | None = None) -> str:
        jid = f"job-{uuid.uuid4().hex[:12]}"
        job = {"id": jid, "name": name, "run_at": time.time() + delay_s,
               "payload": payload or {}, "cancelled": False}
        with self._lock:
            self._jobs[jid] = job
        t = threading.Timer(delay_s, self._fire, args=(jid,))
        t.daemon = True
        job["timer"] = t
        t.start()
        return jid

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job["cancelled"] = True
            timer = job.get("timer")
        if timer:
            timer.cancel()
        return True

    def _fire(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["cancelled"]:
                return
            self._fired.append(job)

    def fired(self) -> list[dict]:
        with self._lock:
            return list(self._fired)

    def pending(self) -> list[dict]:
        with self._lock:
            return [j for j in self._jobs.values() if not j["cancelled"]
                    and j not in self._fired]
