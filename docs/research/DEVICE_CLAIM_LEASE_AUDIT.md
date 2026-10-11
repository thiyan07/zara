# DEVICE CLAIM / LEASE AUDIT (Task 8)

## 0. Initial state (recorded before edits)

- Root `/home/thiyan/projects/assistant`, branch `stage15-real-transfer`,
  HEAD `1c86e3239be5884d637c7a1fda893ea971c9b3ef` (all verified, not assumed).
- `git status --short`: 47 entries (19 modified tracked + 28 untracked).
  All pre-existing work preserved; nothing reverted.
- Free disk 23 GB before, during, and after (floor 10 GB never approached).
- Calibration v1 md5 verified intact before/after:
  `5498ad8207c4c06a27c9115becee3ae3` (jsonl),
  `9dac4995fb407ebd21def1483f93048a` (summary).
- Test baseline: 909 collected. No edits to transfer/Stages/Rungs code
  except the three minimal fixes in §5.

## 1. Five-agent investigation (read-only; all citations verified)

**A — Queue/job lifecycle** (`core/jobs.py` + callers). States that exist:
`pending` (enqueue `jobs.py:53-56`), `claimed` (poll `jobs.py:66-67`),
`done`/`failed` (complete `jobs.py:79`), `cancelled` (`jobs.py:94,112,130`),
`expired` (waiter timeout `jobs.py:148-150`). No `running` at job layer.
Single-claim under one `_cond` (`jobs.py:63-68`); cross-device steal
impossible (`job.device_id==device_id`, `jobs.py:65`). No lease/token/
generation object; `CLAIM_TIMEOUT_S=300` dead (only cited by
`docs/research/JOB_TIMING_FIX.md` as deliberately unwired: claimed jobs
may be actuating). `complete()` takes no device identity (HTTP-only gate
at `app.py:1433`). No reclaim path: `poll` serves only `pending`.

**B — Identity/presence/revoke** (`core/fabric.py`, `core/devices.py`,
`core/protocol.py`, `core/app.py`, `core/device_auth.py`). Identity =
`X-Device-Id`+`X-Device-Key` per request (`app.py:1306-1312`); endpoints
never trust body identity. Presence: `last_seen` + 120 s staleness
(`devices.py:165-170`, `fabric.py:111-125`); poll counts as presence
(`app.py:1411`). Revoke order: auth → `cancel_device_jobs` (pending-only,
by design) → offline → fabric tombstone → xfer (`app.py:1467-1495`).
Found: `claim()` never checked `revoked` and `revoke()` never cleared
`_pending` (pre-claim hole, fixed §5). Claimed jobs survive revoke
deliberately; revoked results fail closed at `agent_auth`.

**C — Polling** (Android `main.dart` tick, `job_runner.dart`, Linux
`agent.py`). Android: 30 s tick (60 s power-save), poll-before-heartbeat
(`main.dart:305-308`), `_seen` duplicate set (200), no claim token.
Linux: `HEARTBEAT_S=30`, poll ~5 s idle, no `_seen`, unknown tool still
reported `ok:false`. Neither client holds lease state; identity = headers.

**D — Execution/approval invariants** (`core/execution.py`,
`core/resolver.py`, `core/app.py:682-750`). Binding advisory, policy
re-decided (`execution.py:150-153`); approve binds parked record only
(`execution.py:184-197`); fingerprint + 300 s TTL + re-park
(`execution.py:208-238`); `_revalidate` at approve and pre-dispatch
(`app.py:378-439`, `execution.py:267-288`); cancel wins over outcomes
(`execution.py:310-330`); terminal 409s (`app.py:673-677`).
Claim/lease work must not bypass any of these — it does not (§5 changes
touch none of them).

**E — Adversarial races.** Exactly-once claim satisfied (one `_cond`);
no-resurrection satisfied (`jobs.py:75-78`); cancel-vs-claim satisfied
(state + `notify_all`); revoke-vs-claim ordered but window open
(fixed §5); restart persistence memory-only (documented limit §8);
wall-clock skew noted (no monotonic clock; missing → unknown/deny).
Two liveness gaps found and fixed: second waiter on `expired` blocked
(`jobs.py:144` omitted `expired`); revoked device could receive payload
in the verify→poll window.

## 2. State model

A **claim** is the `pending→claimed` transition performed by `poll()`
for the bound device — ownership of one execution attempt, stamped
(`claimed_at`) but unbounded by itself. A **lease** in the expiring-token
sense **does not exist** and is deliberately not added: the bound is the
job's own `timeout_s` (proxy wait, default 90 s) enforced by the
Core-side waiter, which moves `pending|claimed→expired` terminally.
A **job execution** is the device actuation + result report; a **terminal
outcome** (`done|failed|cancelled|expired`) is immutable and wins over
all late writes.

## 3. Transition table (after fix)

| From | Event | To | Notes |
|---|---|---|---|
| pending | poll by bound device | claimed | atomic under `_cond`; exactly once |
| pending | poll by other device | — (None) | binding filter |
| pending\|claimed | cancel / cancel_by_execution | cancelled | wakes waiters |
| pending | revoke (cancel_device_jobs) | cancelled | claimed deliberately kept |
| pending\|claimed | waiter timeout (`timeout_s`) | expired | terminal; never resurrected |
| claimed | complete by bound device | done\|failed | first write wins |
| done\|failed\|cancelled\|expired | complete / cancel | unchanged | idempotent; `cancel→False` |
| claimed | revoke then poll-window | cancelled + 403 | §5 fix: payload never handed out |

## 4. Lease bounds and justification

- Claim lifetime bound = per-job `timeout_s` (default 90 s, exceeds the
  30 s slowest agent cadence by 3×; proven by `test_job_timing.py` T0–T4).
- NOT derived from the retired 20 s proxy value, from heartbeats, or from
  `CLAIM_TIMEOUT_S` (dead by design: expiry of a possibly-actuating claim
  is unsafe; the waiter expiry covers the liveness need instead).
- Approval TTL (300 s), pairing TTL (600 s), presence staleness (120 s)
  are independent constants; none was changed or conflated.

## 5. Code changes (3 minimal fixes, no new endpoints, no ranking/policy)

1. `core/jobs.py:144` — `wait_for_result` wake set adds `expired`: a
   second concurrent waiter on an already-expired job returns immediately
   instead of blocking to its own deadline. First-waiter behavior
   unchanged; terminal semantics unchanged.
2. `core/device_auth.py:62-63,80-91` — `claim()` refuses revoked
   credentials (`ValueError: device revoked`); `revoke()` drops that
   device's pending pairing entries so no key can be minted post-revoke
   without a fresh operator enroll (which replaces the credential,
   clearing `revoked` — the sanctioned re-pair path).
3. `core/app.py:1419-1429` — `agent_poll` re-checks
   `device_auth.is_revoked()` after `poll()`; on revoked, best-effort
   `cancel()` the just-claimed job (wakes proxy waiter) and returns 403.
   Result-time `verify` remains the backstop. No behavior change for
   non-revoked devices (single boolean read).

Not changed (deliberately): `pending→done` without prior claim stays
legal (job ids unguessable, HTTP binding + auth gate; both agents only
learn ids via poll); revoke stays pending-only (claimed may be
actuating; remote kill does not exist); no generation tokens (ids never
re-issued); `core/scheduler.py` timer class untouched (file not edited).

## 6. Tests — `tests/test_claim_lease.py` (18 tests, all green)

C01 concurrent 8-thread claim → exactly one winner; C02 repeat poll None;
C03 cross-device poll None; C04 expiry → poll None + late complete keeps
`expired`; C05 second waiter on expired returns immediately; C06 complete
after cancel keeps `cancelled`; C07 duplicate complete idempotent; C08
claim-then-`cancel_by_execution` wakes waiter as `cancelled`; C09 revoke
cancels pending-only; C10 revoke drops pairing code (claim → invalid);
C10b revoked credential refuses even with pending; C11 re-enroll re-pairs;
C12 missing expiry never denies; C13 wrong-device result 403, job
untouched, owner completes; C14 revoked poll 403; C15 disconnect leaves
claim, owner result accepted; C16 revoked result 403, no forgery; C17 job
routes unchanged (3 paths), no `Runtime.getRuntime`/`dart:mirrors` in GUI
poll path, sanctioned `zara/device` channel only.

## 7. Validation results (exact commands, exit codes)

- `pytest tests/test_claim_lease.py -q` → **18 passed** (exit 0).
- Related: job_timing + async_exec + approval_staleness + resolver_async
  + resolve_constraints + scheduler + demand_thresholds +
  resource_telemetry + calibration + governor_v2 + canonical_snapshot +
  telemetry_expansion → **219 passed** (exit 0).
- Full `pytest tests/ -q` → **915 passed, 12 failed** (exit 1):
  10× `test_calibration_campaign.py` (all `FileNotFound`/missing
  `calibration_v2.jsonl` — pre-existing untracked file from a prior
  session; dataset never produced; task scope forbids starting that
  campaign; failure mode independent of this change) +
  2× voice (`test_wake_spotter_exact_phrase`,
  `test_wake_output_has_no_authority` — known flakes importing only
  `core.audio`/`core.voice`, untouched by this change, documented in
  prior session summary as unrelated).
- Flutter `flutter test` → **86 passed**; `flutter analyze` → **no
  issues**.
- No test weakened or skipped.

## 8. Live emulator evidence (Core :8080, emulator-5554, temp probe
devices created then revoked; emu-lab pairing untouched)

- enroll→claim→register→heartbeat→describe all 200; `system.battery`
  submit 201 `running` → poll got job → result 200 → execution
  **succeeded** (authorized claim+complete live-proved).
- `gui.tap` submit 202 `waiting_for_permission` + `approval_required`
  (HIGH_RISK parked, never approved/executed — no tap performed).
- Poll with wrong key → **403**; cross-device result → **403**;
  cancel → late owner result does not resurrect (**cancelled**);
  poll after revoke → **403**.
- `/v1/ready`: `governor_v2:false`, `governor_shadow:false` (default-off
  preserved). Probe devices revoked; post-run fabric shows only
  `emu-lab trusted` (presence `stale` — app backgrounded, pre-existing).

## 9. Restart and persistence limitations

Job queue, auth creds (hashes), presence, and execution records are
in-memory; a Core restart loses in-flight jobs (devices retry →
`404 unknown job`) and executions. Revocation tombstones persist only
with `ZARA_FABRIC_DB` set (live server runs memory mode). After restart,
revoked devices fail closed only if re-revoked or DB-backed. No change
made here; production hardening is follow-up work, not this task.

## 10. Compatibility

- All client-visible behavior backward-compatible: poll/result/cancel
  shapes unchanged; no new routes (C17 asserts the 3 job paths); sync
  execution paths untouched; 90 s proxy wait + poll-before-heartbeat
  ordering preserved; HIGH_RISK TTL/fingerprint/revalidation intact
  (219 related tests green); Governor v2 disabled by default.
- Old clients: revoked devices already handled 403s; the new poll-window
  403 is the same code they already handle (`AuthException` → re-pair
  flow, Android `main.dart:344-347`).

## 11. Remaining risks / recommended next task

1. No remote preemption: a device holding a payload across revoke/cancel
   may still actuate once (logic stays correct; physics does not). Needs
   a cancel-push channel (future task, not this one).
2. Wall-clock expiry/presence (no monotonic clock) — skew can shorten or
   stretch trust/presence windows.
3. In-memory-only runtime state (see §9).
4. Compound trust+presence+policy reads are not under one lock (torn
   reads possible; fail-closed direction dominates).
5. Physical Vivo: UNVERIFIED (no device connected; all live proof is
   emulator + Linux host).

Next: Task 9 (execution retention/TTL) — unaffected by and independent
of these fixes.
