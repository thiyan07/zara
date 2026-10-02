"""Execution engine — the execution contract. Policy-gated, verified, retried."""
from __future__ import annotations
import threading
import uuid
from typing import Optional
from .models import ExecutionRecord, ExecutionState, PolicyDecision, utcnow
from .policy import PolicyEngine
from .tools import ToolRegistry

class PolicyDenied(Exception):
    pass

class ExecutionEngine:
    def __init__(self, registry: ToolRegistry, policy: PolicyEngine,
                 on_event=None) -> None:
        self.registry = registry
        self.policy = policy
        self._records: dict[str, ExecutionRecord] = {}
        self._lock = threading.Lock()
        self._on_event = on_event

    def _emit(self, type: str, **payload) -> None:
        if self._on_event:
            self._on_event(type, payload)

    def submit(self, tool: str, inputs: dict, device_id: str = "cloud",
               who: str = "user", auth: Optional[PolicyDecision] = None,
               mission_id: Optional[str] = None,
               max_retries: int = 1) -> ExecutionRecord:
        """LLM-proposed actions arrive here. Without auth.allow, no execution."""
        definition = self.registry.get(tool)
        decision = auth or self.policy.decide(
            who, capability=f"tool:{tool}", device_id=device_id,
            resource=tool, risk=definition.risk, context={"command": str(inputs)})
        rec = ExecutionRecord(id=f"exec-{uuid.uuid4().hex[:12]}", tool=tool,
                              inputs=dict(inputs), device_id=device_id,
                              mission_id=mission_id, auth=decision,
                              max_retries=max_retries)
        with self._lock:
            self._records[rec.id] = rec
        if not decision.allow:
            if decision.requires_approval and not decision.hard_deny:
                rec.state = ExecutionState.WAITING_FOR_PERMISSION
                rec.error = decision.reason
                self._emit("permission_required", execution_id=rec.id, tool=tool,
                           reason=decision.reason)
                return rec
            rec.state = ExecutionState.DENIED
            rec.error = decision.reason
            self._emit("permission_required", execution_id=rec.id, tool=tool,
                       reason=decision.reason)
            raise PolicyDenied(decision.reason)
        self._run(rec)
        return rec

    def approve(self, execution_id: str, approver: str = "user") -> ExecutionRecord:
        with self._lock:
            rec = self._records[execution_id]
        if rec.state != ExecutionState.WAITING_FOR_PERMISSION:
            raise ValueError(f"execution {execution_id} is not awaiting approval")
        gid = self.policy.grant(approver, f"tool:{rec.tool}",
                                rec.device_id, rec.tool)
        rec.auth = PolicyDecision(allow=True, grant_id=gid,
                                  scope=f"tool:{rec.tool}@{rec.device_id}",
                                  reason=f"approved by {approver}")
        self._emit("permission_granted", execution_id=rec.id, tool=rec.tool)
        self._run(rec)
        return rec

    def cancel(self, execution_id: str) -> ExecutionRecord:
        with self._lock:
            rec = self._records[execution_id]
            rec.cancel_requested = True
            if rec.state in (ExecutionState.PENDING, ExecutionState.WAITING_FOR_PERMISSION):
                rec.state = ExecutionState.CANCELLED
                rec.ended_at = utcnow()
        return rec

    def get(self, execution_id: str) -> ExecutionRecord:
        return self._records[execution_id]

    def _run(self, rec: ExecutionRecord) -> None:
        tool = self.registry.get(rec.tool)
        rec.state = ExecutionState.RUNNING
        rec.started_at = utcnow()
        attempts = 0
        while True:
            if rec.cancel_requested:
                rec.state = ExecutionState.CANCELLED
                rec.ended_at = utcnow()
                break
            attempts += 1
            rec.attempt = attempts
            try:
                out = self.registry.call(rec.tool, rec.inputs,
                                         {"device_id": rec.device_id,
                                          "execution_id": rec.id})
                rec.result = out
                rec.verified = True if tool.verification == "none" else bool(out)
                rec.state = ExecutionState.SUCCEEDED
                rec.ended_at = utcnow()
                self._emit("execution_finished", execution_id=rec.id,
                           tool=rec.tool, ok=True)
                break
            except TimeoutError as e:
                rec.error = str(e)
                rec.state = ExecutionState.TIMED_OUT
                rec.ended_at = utcnow()
                self._emit("execution_finished", execution_id=rec.id,
                           tool=rec.tool, ok=False, error=rec.error)
                break
            except Exception as e:  # noqa: BLE001
                rec.error = f"{type(e).__name__}: {e}"
                if tool.failure_behavior == "retry" and attempts <= rec.max_retries:
                    continue
                rec.state = ExecutionState.FAILED
                rec.ended_at = utcnow()
                self._emit("execution_finished", execution_id=rec.id,
                           tool=rec.tool, ok=False, error=rec.error)
                break
