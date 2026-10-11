# ASYNC EXECUTION API — submit → id → poll status → result

**Status: IMPLEMENTED. Additive only. Sync paths frozen.**

## Motivation

Every execution held its HTTP worker for the full device round-trip (up to
90 s post race-fix: `HTTP → engine._run → registry thread → proxy Condition
wait → device poll → result`). Offline devices burned a worker for 90 s;
bursts piled threads. The async API detaches the wait from the response.

## Lifecycle

```
POST /v1/executions {tool, inputs, device_id}
  → gates (auth, registry, governor, policy) run SYNCHRONOUSLY
  → DENIED → 403 (no execution, as before)
  → parked (approval required) → 202 + view, NO job, NO thread
  → allowed → RUNNING + daemon-thread _run → 201 + view
GET /v1/executions/{id} → view (record + read-time job join)
POST /v1/executions/{id}/approve → grant + background _run → running view
POST /v1/executions/{id}/cancel → CANCELLED view (409 if already terminal)
```

Old endpoints unchanged: `/v1/exec`, `/v1/exec/{id}/approve`,
`/v1/dispatch`, talk/intent/fabric paths all keep sync semantics.

## State machine

No new states. `ExecutionState` as-is
(`pending|running|waiting_for_permission|succeeded|failed|timed_out|
denied|cancelled`). Job states joined read-only into the view
(`job_id`, `job_state`); mapping `done→succeeded` is by record, never by
renaming job state. Terminal states terminal; late completions never
resurrect (job `complete` guard + record never moves backward).

## Execution ID

`exec-{12hex}` minted server-side per submit, as before. Non-authoritative:
every read/mutation re-derives caller auth + record binding from Core
state. Duplicate submits mint distinct ids (each is separately auditable
and approval-gated; no silent collapse).

## Security model

- Same gates as sync (auth → registry → governor → policy → approval).
  No job enqueued before allow/approve (tested).
- View exposes record fields minus `auth` (no grant internals).
- `agent_result` now binds `job.device_id == caller` (403 + audit
  otherwise) — closes cross-device completion forgery.
- Revocation: queue-cancel linearized first in the revoke path; claimed
  jobs untouched; result endpoint auth-gates stragglers.
- Audit: submit/approve/cancel recorded with metadata only (no
  inputs/results/secrets). Result content stays untrusted data.
- Single-bearer operator plane: no per-user isolation exists (documented,
  unchanged). Cancel-on-terminal → 409.

## Cancellation / revocation

`engine.cancel` → record flag + `cancel_by_execution` (wakes waiter);
`_run` maps cancel-wakeups (`InterruptedError` or `cancel_requested`) to
CANCELLED, never FAILED. Revoke sweeps pending jobs first, then presence /
tombstone / transfers.

## Idempotency

No client key (documented): resubmission is safe (safe tools) or parked
for approval (gated tools); replays after terminal cannot re-execute;
reads are side-effect-free. A `client_key` dedup window is future work if
duplicate-submits ever cause harm.

## Compatibility

Sync `/v1/exec` etc. byte-identical (wire shapes, status strings, audit
actions, approval 409s — plus fixed operator-approve 500s → 404/409).
Android (poll/result/approvals) untouched. `ToolRegistry.call` and proxy
mapping untouched. 90 s proxy invariant untouched.

## Polling

Client→Core status polling (`GET /v1/executions/{id}`) is independent of
Android→Core job polling. Both coexist. `/v1/stream`
`execution_finished` remains the push complement.

## Emulator proof (2026-10-08, ZaraLab API 36)

- `POST /v1/executions {system.battery}` → **0.02 s**, `running`,
  `result: None` (vs ~13–90 s held HTTP before).
- `GET` 10 s later → **`succeeded`, `job: done`,
  `result: {battery_pct: 100.0, ...}** — execution observed through the
  status API after early return. The proof the task demanded.
- `POST {gui.tap}` → **202**, `waiting_for_permission`, `job: None` (no
  pre-approval dispatch) → cancel → **`cancelled`** → unknown id → **404**.

## Known limitations

- Records + jobs in-memory (restart drops them; documented, unchanged).
- No list endpoint (unbounded scan avoided). Terminal retention is bounded
  (Task 9: lazy prune, 500 terminal / 24 h, actives never reaped; evicted
  ids read as unknown 404) — see EXECUTION_RETENTION_TTL_AUDIT.md.
- Result envelope bounded at ingest (Task 9: 64 KiB result / 4 KiB error,
  fail-closed 422/ValueError, never truncated).
- Threads per in-flight exec (existing cost model; pool deferred).
- HIGH_RISK consent staleness now observable via poll loop (re-confirmation
  is the sequenced follow-up, not this task).
