"""Execution engine — the execution contract. Policy-gated, verified, retried."""
from __future__ import annotations
import hashlib
import json
import threading
import uuid
from datetime import timedelta
from typing import Callable, Optional
from .models import ExecutionRecord, ExecutionState, PolicyDecision, utcnow
from .policy import PolicyEngine
from .tools import ToolRegistry

class PolicyDenied(Exception):
    pass

# Approval validity window (Part B): an approval authorizes the operation
# as it looked at park time. Trust, presence, descriptors, and policy all
# move, so WAITING_FOR_PERMISSION records expire after 5 minutes (matches
# PolicyEngine grant TTL) and must be explicitly approved again. Never
# auto-extended; expiry re-parks, never dispatches.
APPROVAL_TTL_S = 300.0

# Task 9 retention contract (see docs/research/EXECUTION_RETENTION_TTL_AUDIT.md).
# The engine is an execution ledger with a bounded tail, not an archive:
# terminal records are retained for polling/idempotency, then reaped by
# count and age. Non-terminal records (PENDING/RUNNING/WAITING) are NEVER
# reaped — deleting an active execution would strand its worker thread
# and falsify the approval queue. Evicted ids read back as unknown
# (KeyError → 404), exactly like never-created ids.
TERMINAL_EXEC_STATES = frozenset({
    ExecutionState.SUCCEEDED, ExecutionState.FAILED,
    ExecutionState.TIMED_OUT, ExecutionState.DENIED,
    ExecutionState.CANCELLED})
TERMINAL_RETENTION_MAX = 500
TERMINAL_RETENTION_S = 24 * 3600.0


def approval_fingerprint(tool: str, device_id: str, inputs: dict,
                         risk: str, capability: Optional[str],
                         tool_version: str) -> str:
    """Deterministic integrity digest of the exact approved operation.

    Exact canonical form (sort_keys + compact separators, ensure_ascii) —
    NOT lossy-normalized: any meaningful argument distinction changes the
    digest. For equality/integrity, not authentication. Secrets are hashed,
    never logged (callers must only persist/compare the hex digest).
    """
    payload = {"capability": capability, "device": device_id, "tool": tool,
               "tool_version": tool_version, "risk": risk, "args": inputs}
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, default=str)
    if len(canon) > 65536:
        raise ValueError("approval fingerprint input exceeds 64 KiB")
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]

class ExecutionEngine:
    def __init__(self, registry: ToolRegistry, policy: PolicyEngine,
                 on_event=None, jobs=None,
                 revalidate: Optional[Callable] = None,
                 max_terminal_records: int = TERMINAL_RETENTION_MAX,
                 terminal_retention_s: float = TERMINAL_RETENTION_S) -> None:
        self.registry = registry
        self.policy = policy
        self._records: dict[str, ExecutionRecord] = {}
        self._idem: dict[str, str] = {}
        self._lock = threading.Lock()
        # Duplicate-submission guard: client idempotency key -> exec id.
        # Bounded (FIFO eviction); keys to reaped records are swept in
        # prune. A replayed key returns the SAME record (no re-execution);
        # a key whose record was reaped starts a new execution honestly.
        self._on_event = on_event
        self._max_terminal = max_terminal_records
        self._terminal_retention_s = terminal_retention_s
        # Optional JobQueue handle so operator cancellation reaches queued
        # device jobs (wakes the proxy waiter instead of stranding it
        # until timeout). None in unit contexts that never enqueue.
        self._jobs = jobs
        # Optional revalidation hook: revalidate(rec) -> (ok, reason).
        # Called at approve time and pre-dispatch for HIGH_RISK tools to
        # re-derive trust/presence/policy/capability freshness. Wired by
        # build_stack to the fabric; None in unit contexts (approve path
        # then enforces fingerprint + TTL only). Never authority by
        # itself — failure denies, never allows.
        self._revalidate = revalidate

    def set_revalidate(self, fn) -> None:
        """Attach the fabric revalidation hook after construction
        (fabric is built after the engine in build_stack)."""
        self._revalidate = fn

    def _emit(self, type: str, **payload) -> None:
        if self._on_event:
            self._on_event(type, payload)

    def submit(self, tool: str, inputs: dict, device_id: str = "cloud",
               who: str = "user", auth: Optional[PolicyDecision] = None,
               mission_id: Optional[str] = None,
               max_retries: int = 1, capability: Optional[str] = None,
               binding: Optional[dict] = None) -> ExecutionRecord:
        """LLM-proposed actions arrive here. Without auth.allow, no execution.

        Synchronous: blocks until terminal (or parked/denied). Preserved
        byte-for-byte for existing callers; async uses submit_async.
        """
        rec = self._prepare(tool, inputs, device_id, who, auth,
                            mission_id, max_retries, capability, binding)
        if rec.state != ExecutionState.RUNNING:
            return rec  # parked for approval, or DENIED raised inside
        self._run(rec)
        return rec

    def submit_async(self, tool: str, inputs: dict, device_id: str = "cloud",
                     who: str = "user", auth: Optional[PolicyDecision] = None,
                     mission_id: Optional[str] = None,
                     max_retries: int = 1, capability: Optional[str] = None,
                     binding: Optional[dict] = None) -> ExecutionRecord:
        """Async twin of submit: same gates, but RUNNING records execute on
        a daemon thread and the record returns immediately. WAITING/DENIED
        behave identically (no thread, no dispatch before approval)."""
        rec = self._prepare(tool, inputs, device_id, who, auth,
                            mission_id, max_retries, capability, binding)
        if rec.state == ExecutionState.RUNNING:
            self._spawn_background(self._run, rec)
        return rec

    IDEM_KEY_RE = None  # compiled lazily (re import at module top is fine)
    IDEM_KEY_MAX = 128
    IDEM_MAP_MAX = 500

    @staticmethod
    def check_idempotency_key(key: str) -> str:
        """Validate a client idempotency key. Returns the stripped key
        ("" = absent, no dedupe). Raises ValueError on malformed keys."""
        import re as _re
        key = (key or "").strip()
        if not key:
            return ""
        if len(key) > ExecutionEngine.IDEM_KEY_MAX:
            raise ValueError("idempotency_key exceeds 128 chars")
        if not _re.fullmatch(r"[A-Za-z0-9_.:\-]{1,128}", key):
            raise ValueError("idempotency_key must match "
                             "[A-Za-z0-9_.:-]{1,128}")
        return key

    def submit_async_idem(self, tool: str, inputs: dict,
                          device_id: str = "cloud", who: str = "user",
                          auth: Optional[PolicyDecision] = None,
                          mission_id: Optional[str] = None,
                          max_retries: int = 1,
                          capability: Optional[str] = None,
                          binding: Optional[dict] = None,
                          idempotency_key: str = "") -> ExecutionRecord:
        """Idempotent async submit: a replayed key returns the SAME record
        without re-executing. Keys are opaque client tokens (never secrets,
        never authority); mismatched tool/inputs on the same key still
        return the original record with its original parameters."""
        key = self.check_idempotency_key(idempotency_key)
        if key:
            with self._lock:
                prior = self._records.get(self._idem.get(key, ""))
                if prior is not None:
                    return prior
        rec = self.submit_async(tool, inputs, device_id, who, auth,
                                mission_id, max_retries, capability, binding)
        if key:
            with self._lock:
                self._idem[key] = rec.id
                while len(self._idem) > self.IDEM_MAP_MAX:
                    self._idem.pop(next(iter(self._idem)))
        return rec

    def approve(self, execution_id: str, approver: str = "user") -> ExecutionRecord:
        """Synchronous approval: grants then runs inline to terminal."""
        rec = self._grant(execution_id, approver)
        self._run(rec)
        return rec

    def approve_async(self, execution_id: str,
                      approver: str = "user") -> ExecutionRecord:
        """Async twin of approve: grants then runs on a daemon thread,
        returning the RUNNING record immediately."""
        rec = self._grant(execution_id, approver)
        self._spawn_background(self._run, rec)
        return rec

    def cancel(self, execution_id: str) -> ExecutionRecord:
        with self._lock:
            rec = self._records[execution_id]
            # Task 9 (T6), corrected: cancel_requested is the _run loop's
            # cross-thread signal, so every NON-terminal record (including
            # RUNNING, whose waiter must wake) is flagged. Already-terminal
            # records are left byte-identical — flagging them dirtied stored
            # history and could mislead future readers. Terminal cancel
            # stays a no-op at the HTTP layer (409).
            if rec.state not in TERMINAL_EXEC_STATES:
                rec.cancel_requested = True
                if rec.state in (ExecutionState.PENDING,
                                 ExecutionState.WAITING_FOR_PERMISSION):
                    rec.state = ExecutionState.CANCELLED
                    rec.ended_at = utcnow()
        # Reach the queued device job, if the execution already dispatched
        # past approval: wakes the proxy waiter promptly (cancelled, never
        # success) instead of holding it until the proxy timeout.
        try:
            if self._jobs is not None:
                self._jobs.cancel_by_execution(execution_id)
        except Exception:
            pass
        return rec

    def get(self, execution_id: str) -> ExecutionRecord:
        return self._records[execution_id]

    def prune(self, now=None, max_terminal: Optional[int] = None,
              retention_s: Optional[float] = None) -> int:
        """Reap terminal execution records boundedly. Returns count removed.

        Contract (Task 9): only records in TERMINAL_EXEC_STATES are
        eligible, and only when older than retention_s (by ended_at) or
        beyond the newest max_terminal (by ended_at). PENDING / RUNNING /
        WAITING_FOR_PERMISSION are never touched — the approval queue and
        in-flight workers must survive retention. Terminal records with
        missing or unparseable ended_at are retained (fail-closed: never
        delete what cannot be aged). Idempotent and lock-guarded; evicted
        ids read back as unknown (KeyError → 404). now is injectable for
        deterministic tests (no sleeps).
        """
        if now is None:
            now = utcnow()
        if max_terminal is None:
            max_terminal = self._max_terminal
        if retention_s is None:
            retention_s = self._terminal_retention_s
        with self._lock:
            kill: set[str] = set()
            ageable: list[tuple[str, object]] = []
            for rid, rec in self._records.items():
                if rec.state not in TERMINAL_EXEC_STATES:
                    continue
                if rec.ended_at is None:
                    continue  # unageable: retain
                try:
                    age = (now - rec.ended_at).total_seconds()
                except Exception:  # noqa: BLE001 — unparseable: retain
                    continue
                if age > retention_s:
                    kill.add(rid)
                else:
                    ageable.append((rid, rec.ended_at))
            if len(ageable) > max_terminal:
                try:
                    ageable.sort(key=lambda item: item[1])
                except Exception:  # noqa: BLE001 — incomparable: retain
                    ageable = []
                for rid, _ in ageable[:len(ageable) - max_terminal]:
                    kill.add(rid)
            for rid in kill:
                del self._records[rid]
                for k, v in list(self._idem.items()):
                    if v == rid:
                        del self._idem[k]
            return len(kill)

    def _prepare(self, tool: str, inputs: dict, device_id: str = "cloud",
                 who: str = "user", auth: Optional[PolicyDecision] = None,
                 mission_id: Optional[str] = None,
                 max_retries: int = 1, capability: Optional[str] = None,
                 binding: Optional[dict] = None) -> ExecutionRecord:
        """Shared submit preamble: policy-gate, record, park-or-ready.

        Returns the record WITHOUT running it. Sync submit/approve call
        _run inline afterwards; async variants hand it to a background
        thread. Single funnel: no path enqueues work before allow/approve.
        capability/binding are advisory records of the resolver decision;
        _prepare always re-decides policy itself (binding is never trust).
        """
        definition = self.registry.get(tool)
        decision = auth or self.policy.decide(
            who, capability=f"tool:{tool}", device_id=device_id,
            resource=tool, risk=definition.risk, context={"command": str(inputs)})
        # Task 9 (T2): enforce the tool's own input envelope BEFORE the
        # record exists. validate_against_schema re-checks at dispatch, but
        # a rejected submit must not retain a full inputs copy (up to the
        # per-tool max, 1 MiB) plus a second copy in the queued DeviceJob.
        # Same measure as tools.validate_against_schema (len of json.dumps).
        try:
            input_bytes = len(json.dumps(inputs))
        except Exception:  # noqa: BLE001 — unserializable is itself invalid
            raise ValueError(f"inputs for {tool} are not serializable")
        if input_bytes > definition.max_input_bytes:
            raise ValueError(
                f"inputs for {tool} exceed {definition.max_input_bytes} "
                f"bytes ({input_bytes})")
        rec = ExecutionRecord(id=f"exec-{uuid.uuid4().hex[:12]}", tool=tool,
                              inputs=dict(inputs), device_id=device_id,
                              mission_id=mission_id, auth=decision,
                              max_retries=max_retries, capability=capability,
                              binding=dict(binding or {}))
        with self._lock:
            self._records[rec.id] = rec
            over = len(self._records) > 2 * self._max_terminal
        if over:
            # Lazy bounded retention: amortized reap of terminal tail only.
            # No background thread, no periodic polling; active records are
            # never eligible (see prune).
            self.prune()
        if not decision.allow:
            if decision.requires_approval and not decision.hard_deny:
                rec.state = ExecutionState.WAITING_FOR_PERMISSION
                rec.error = decision.reason
                rec.parked_at = utcnow()
                rec.approval_fingerprint = approval_fingerprint(
                    tool, device_id, rec.inputs,
                    definition.risk.value
                    if hasattr(definition.risk, "value")
                    else str(definition.risk),
                    capability, definition.version)
                self._emit("permission_required", execution_id=rec.id, tool=tool,
                           reason=decision.reason)
                return rec
            rec.state = ExecutionState.DENIED
            rec.error = decision.reason
            self._emit("permission_required", execution_id=rec.id, tool=tool,
                       reason=decision.reason)
            raise PolicyDenied(decision.reason)
        rec.state = ExecutionState.RUNNING
        rec.started_at = utcnow()
        return rec

    def _grant(self, execution_id: str,
               approver: str = "user") -> ExecutionRecord:
        """Shared approval transition: re-validate, grant, mark running.

        Approval authorizes EXACTLY the parked operation:
        1. state must be WAITING_FOR_PERMISSION (else ValueError→409);
        2. park must be within APPROVAL_TTL_S (else re-park + ValueError —
           stays approvable, never dispatches stale);
        3. recomputed fingerprint must match the parked one (else DENIED —
           tool/device/args/risk/capability drifted);
        4. revalidate hook (trust/presence/policy/capability freshness)
           must pass (else DENIED).
        Returns the record WITHOUT running it.
        """
        with self._lock:
            rec = self._records[execution_id]
        if rec.state != ExecutionState.WAITING_FOR_PERMISSION:
            raise ValueError(f"execution {execution_id} is not awaiting approval")
        now = utcnow()
        parked = rec.parked_at or rec.requested_at
        try:
            age = (now - parked).total_seconds()
        except Exception:
            # Task 9: missing/malformed park time fails CLOSED. Approving a
            # record whose window cannot be verified would bypass the TTL;
            # take the expiry path instead (re-park + refuse, self-healing:
            # the fresh parked_at makes the next attempt verifiable).
            age = None
        if age is None or age > APPROVAL_TTL_S:
            # Expired: re-park (fresh window + fingerprint), refuse this
            # attempt. The user must explicitly approve again.
            rec.parked_at = now
            rec.requested_at = now
            definition = self.registry.get(rec.tool)
            rec.approval_fingerprint = approval_fingerprint(
                rec.tool, rec.device_id, rec.inputs,
                definition.risk.value
                if hasattr(definition.risk, "value")
                else str(definition.risk),
                rec.capability, definition.version)
            rec.error = (f"approval expired after {age:.0f}s "
                         f"(window {APPROVAL_TTL_S:.0f}s); re-parked"
                         if age is not None
                         else "approval park time unverifiable; re-parked "
                         f"(window {APPROVAL_TTL_S:.0f}s)")
            self._emit("approval_expired", execution_id=rec.id, tool=rec.tool,
                       reason=rec.error)
            raise ValueError(rec.error)
        definition = self.registry.get(rec.tool)
        current = approval_fingerprint(
            rec.tool, rec.device_id, rec.inputs,
            definition.risk.value
            if hasattr(definition.risk, "value") else str(definition.risk),
            rec.capability, definition.version)
        if current != rec.approval_fingerprint:
            rec.state = ExecutionState.DENIED
            rec.error = ("approval invalidated: operation changed since park "
                         "(tool/device/args/risk/capability drift)")
            rec.ended_at = now
            self._emit("approval_invalidated", execution_id=rec.id,
                       tool=rec.tool, reason=rec.error)
            raise ValueError(rec.error)
        if self._revalidate is not None:
            try:
                ok, reason = self._revalidate(rec)
            except Exception as e:  # fail closed on hook errors
                ok, reason = False, f"revalidate error: {e}"
            if not ok:
                rec.state = ExecutionState.DENIED
                rec.error = f"approval invalidated at re-check: {reason}"[:300]
                rec.ended_at = now
                self._emit("approval_invalidated", execution_id=rec.id,
                           tool=rec.tool, reason=rec.error)
                raise ValueError(rec.error)
        gid = self.policy.grant(approver, f"tool:{rec.tool}",
                                rec.device_id, rec.tool)
        rec.auth = PolicyDecision(allow=True, grant_id=gid,
                                  scope=f"tool:{rec.tool}@{rec.device_id}",
                                  reason=f"approved by {approver}")
        self._emit("permission_granted", execution_id=rec.id, tool=rec.tool)
        rec.state = ExecutionState.RUNNING
        rec.started_at = utcnow()
        rec.approved_at = rec.started_at
        return rec

    @staticmethod
    def _spawn_background(target, rec: ExecutionRecord) -> None:
        t = threading.Thread(target=target, args=(rec,), daemon=True)
        t.start()

    def _run(self, rec: ExecutionRecord) -> None:
        tool = self.registry.get(rec.tool)
        # Pre-dispatch re-check for HIGH_RISK: the approve→dispatch window
        # can span trust/presence/policy changes. Non-high-risk tools rely
        # on the approve-time gate only (documented, keeps hot paths hot).
        try:
            risk_val = tool.risk.value if hasattr(tool.risk, "value") \
                else str(tool.risk)
        except Exception:
            risk_val = "unknown"
        if risk_val == "high_risk" and self._revalidate is not None:
            try:
                ok, reason = self._revalidate(rec)
            except Exception as e:  # fail closed on hook errors
                ok, reason = False, f"revalidate error: {e}"
            if not ok:
                rec.state = ExecutionState.DENIED
                rec.error = f"dispatch refused at re-check: {reason}"[:300]
                rec.ended_at = utcnow()
                self._emit("execution_finished", execution_id=rec.id,
                           tool=rec.tool, ok=False, error=rec.error)
                return
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
                if rec.cancel_requested:
                    rec.error = f"cancelled: {e}"
                    rec.state = ExecutionState.CANCELLED
                else:
                    rec.error = str(e)
                    rec.state = ExecutionState.TIMED_OUT
                rec.ended_at = utcnow()
                self._emit("execution_finished", execution_id=rec.id,
                           tool=rec.tool, ok=False, error=rec.error)
                break
            except Exception as e:  # noqa: BLE001
                # A cancel wakeup surfaces here as InterruptedError from the
                # proxy waiter: operator cancel wins over failure recording.
                if rec.cancel_requested or isinstance(e, InterruptedError):
                    rec.error = f"cancelled: {e}"
                    rec.state = ExecutionState.CANCELLED
                    rec.ended_at = utcnow()
                    self._emit("execution_finished", execution_id=rec.id,
                               tool=rec.tool, ok=False, error=rec.error)
                    break
                rec.error = f"{type(e).__name__}: {e}"
                if tool.failure_behavior == "retry" and attempts <= rec.max_retries:
                    continue
                rec.state = ExecutionState.FAILED
                rec.ended_at = utcnow()
                self._emit("execution_finished", execution_id=rec.id,
                           tool=rec.tool, ok=False, error=rec.error)
                break
