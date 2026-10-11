# EXECUTION RETENTION, TTL, AND EXPIRY AUDIT (Task 9)

## A. Initial state (Phase 0 baseline, verified before any edit)

- Branch `stage15-real-transfer`, HEAD `1c86e3239be5884d637c7a1fda893ea971c9b3ef`.
- `git status --short`: 50 entries (19 modified tracked, incl. all Task 8
  files; 31 untracked). No Task 8 file reverted or normalized.
- Free disk 23 GB (81%) before, during, after; 10 GB floor never approached.
- Calibration v1 hashes intact: jsonl `5498ad8207c4c06a27c9115becee3ae3`,
  summary `9dac4995fb407ebd21def1483f93048a`.
- Task 8 files present with Task 8 behavior intact (`core/jobs.py`,
  `core/device_auth.py`, `core/app.py`, `tests/test_claim_lease.py`,
  `docs/research/DEVICE_CLAIM_LEASE_AUDIT.md`).
- Processes: emulator (pid 13878) + Core `:8080` (pid 59630, started
  18:13, i.e. BEFORE Task 9 edits — see §J) + `adb emulator-5554 device`.
  Nothing stopped or killed.
- Reported 12 pre-existing failures re-checked: 10×
  `test_calibration_campaign.py` still fail (missing
  `calibration_v2.jsonl`, FileNotFound — out of scope, fixtures not
  manufactured). The 2 voice tests PASS in isolation (flaky under
  full-suite load only; they import `core.audio`/`core.voice`, untouched).

## B. Five-agent findings (read-only; disagreements resolved vs code)

**A — Lifecycle/retention.** Records (`execution.py:154-160`) and jobs
(`jobs.py:57`) insert-only; zero `del/pop/clear/evict/purge` in either
file. No list/history/eviction API (admitted in ASYNC_EXECUTION_API.md).
Terminal records retained forever; `_cancelled` only grows. Pending cap
(`jobs.py:48-52`) counts pending only — never bounds totals. Found:
DENIED-on-submit records retained but undiscoverable via API (no id
returned).

**B — In-memory vs persistent.** `build_stack` (`app.py:270-365`):
jobs/auth/engine-records/devices always RAM; only memory/missions/audit/
fabric-tombstones/session/transfer persist iff `ZARA_*_DB` set. Live
launcher sets only memory/audit/fabric (all memory-mode defaults).
Architecture promises NO durability for executions/jobs. Restart →
stale ids 404, orphaned claims, lost approvals/threads.

**C — TTL/clock.** `utcnow()` wall everywhere. Boundary operators
inconsistent: approval/devices/presence `>` (equality live), TempGrant
`>=`, jobs deadline `<=`, device-auth/policy `<`, mixed ISO-string vs
parsed-datetime compares. Dead/fail-open TTLs found: pairing TTL never
enforced in `claim()` ("invalid or expired" message fiction); `_grant`
parse-failure → `age=0.0` (fail-open approve); jobs deadline on wall
`time.time()`. Missing telemetry fails closed; missing `last_seen`
raises; missing auth expiry fails open (pinned by Task 8 C12, kept).

**D — Compatibility fence.** Exact 404/403/409 codes + details, 201/202
split, `{"job":None}` vs 403/404, terminal stickiness, re-park (stay
WAITING + fresh window) vs DENIED, no enumeration, fingerprint never
exposed, device binding, 90 s proxy + cancel-wake, revoke pending-only.
All preserved (§I).

**E — Adversarial.** T1 unbounded accumulation DEMONSTRATED; T2 inputs
stored pre-validation (≤ per-tool max, ×2 copies) DEMONSTRATED; T3
device result/error unbounded at every layer DEMONSTRATED; T4 stale ids
handled; T5 no cleanup to race (hypothetical); T6 job-layer safe, engine
cancel-flag nit DEMONSTRATED low; T7 write-safe, operator-read by design.
Uncertainty resolved: no hidden evictor exists (grep-clean).

## C. Retention/TTL inventory (seven concepts, not one timeout)

| Concept | Bound | Clock | On expiry |
|---|---|---|---|
| Execution lifetime (RUNNING) | `timeout_s` proxy wait (90 s) | monotonic (Task 9 fix) | job `expired` (terminal), engine TIMED_OUT |
| Claim/lease (Task 8) | per-job `timeout_s`, no token | waiter monotonic | terminal, never re-queued |
| HIGH_RISK approval TTL | 300 s, strict `>` | wall parsed | re-park + fresh fingerprint, still approvable |
| Completed-result retention | 500 terminal / 24 h, lazy prune | wall `ended_at` | evicted → unknown 404; returned results unaffected |
| Terminal-job retention | 500 terminal / 24 h + `_cancelled` sweep | wall `finished_at` | same; active never eligible |
| Presence/heartbeat freshness | 120 s stale (unchanged) | wall | STALE/UNKNOWN → deny (unchanged) |
| Process-lifetime state | none (in-memory by design) | n/a | restart drops all (documented, unchanged) |

Expiry removes query visibility of aged-out terminal objects only; it
never resurrects, never overrides cancel/revoke, never deletes actives,
never converts unknown time to zero (unageable terminal retained).

## D. Contract selected and rationale

Bounded-tail ledger, lazy eviction, fail-closed time: count+age prune of
terminal-only objects, triggered amortized on insert (no threads, no
timers, no new endpoints/DB/queue/state machine); ingest envelopes
bounded (inputs pre-check, result/error reject); corrupt time fails
closed; monotonic wait deadlines. Rationale: smallest change that bounds
worst-case memory while preserving every §B-D guarantee; active work is
never auto-deleted (deleting approvals/in-flight executions would
falsify the security model).

## E. Defects confirmed (with reproduction)

1. **T1** — loop submits → `_records`/`_jobs` grow forever (nothing
   deletes). Fixed by `prune()` + lazy trigger.
2. **T3** — multi-MB device result stored whole, served forever (no bound
   at `JobResultIn`/`complete`/record/view). Fixed by 64 KiB/4 KiB
   reject at both boundaries.
3. **T2** — oversize-but-invalid inputs retained (record + job copy).
   Fixed by pre-insert size check vs tool `max_input_bytes` (422,
   no record).
4. **Pairing TTL dead** — stale code mints keys indefinitely. Fixed by
   enforcing `cred.expires_at` in `claim()` (fail-closed).
5. **`_grant` fail-open** — corrupt `parked_at` → `age=0` → approvable.
   Fixed: unverifiable → re-park path (409, self-healing).
6. **Wall-clock deadline** — `wait_for_result` on `time.time()`. Fixed:
   monotonic.
7. **T6 nit** — `cancel()` flagged terminal records. Fixed (flag only
   non-terminal). First attempt broke RUNNING-cancel wake-up (B05 caught
   it); corrected same session — see §G.

## F. Files changed and why

- `core/jobs.py` — retention constants + `prune()`/`_prune_locked()`
  (T1); `complete()` size guards (T3); monotonic deadline (E6); lazy
  prune in `enqueue`; ctor caps. Task 8 lines (claim, terminal sets,
  revoke comment, expired wake) byte-intact.
- `core/execution.py` — retention constants + `prune()` (T1); `_prepare`
  input pre-check (T2) + lazy trigger; `_grant` fail-closed time (E5);
  `cancel()` flag guard (E7/T6). Approval TTL/fingerprint/revalidate
  untouched.
- `core/device_auth.py` — pairing TTL enforcement (E4). Task 8 revoked
  checks intact and ordered before it.
- `core/app.py` — `JobResultIn` bounds → 422 (T3); `ValueError→422` at
  the 4 submit endpoints (T2; also fixes latent 500s); Task 8 poll
  linearization intact.
- `docs/research/ASYNC_EXECUTION_API.md` — synced 3 stale limitation
  lines (was: "no eviction yet", "envelope unbounded").
- New `tests/test_retention_ttl.py` (21 tests). No GUI/scheduler/
  Governor/telemetry/persistence changes.

## G. Tests (exact commands, exit codes)

- `pytest tests/test_retention_ttl.py` → **21 passed** (exit 0). Covers
  R01–R09, R14–R20 (R10–R13 via suites below).
- `pytest test_claim_lease + job_timing + async_exec + approval_staleness
  + resolver_async + resolve_constraints + stage2 + stage4 + fabric` →
  193 passed + **1 regression (mine, B05)** → fixed → **37/37** on
  re-run of retention + staleness (exit 0). Cause: first T6 guard
  skipped RUNNING flagging; existing B05 (cancel-after-approve wakes
  proxy) caught it — approval invariants did their job.
- Full `pytest tests/` → **938 passed, 10 failed** (exit 1): all 10 are
  `test_calibration_campaign.py` FileNotFound (pre-existing §A); voice
  flakes passed this run. No new failures (12 → 10).
- Flutter `test` → **86 passed**; `analyze` → **no issues** (no android
  files changed; run as hygiene).

## H. Concurrency/boundary/restart/adversarial evidence

- R06 (barrier: complete×2 + cancel + prune) — no errors, terminal
  stable, untouched claimed job survives prune.
- R02 boundary (age==retention kept, older reaped, injected now).
- R05 double prune idempotent; R04/R04b evicted → 404 shapes preserved.
- R07 corrupt park time → "unverifiable" re-park (never approve);
  expired/missing pairing → claim refused; expired key → verify False.
- R08 new stack → old id 404 (no-durability guarantee pinned).
- R19 lazy triggers bound totals (engine ≤10 with cap 5 over 20
  submits; queue likewise). R20 flag semantics incl. RUNNING case.

## I. Compatibility/security regression evidence

- All §B-D codes/shapes preserved (full related suites green, incl.
  B01–B16 approval, A01–A21 async, C01–C17 claim/lease, T0–T10 timing).
- Fingerprint/TTL/revalidation/grant tests untouched and passing;
  resolver binding tests passing; sync `/v1/exec` live-proved
  (`util.echo` → `succeeded`); no new routes (3 job paths); device
  binding (R09: device creds ≠ operator auth); no GUI shell/reflection
  markers (R14).

## J. Process/device/disk status

- Emulator `emulator-5554 device` online; Core `:8080` + emulator
  untouched (no kills/restarts). Disk 23 GB free throughout.
- **Caveat (recorded, not hidden): live Core predates Task 9 edits**
  (started 18:13, edits 21:08–21:58; restart forbidden). Live probe
  therefore proves only unchanged paths (sync compat, 404 shapes,
  claim/complete flow, revoke, governor flags) — and live-RETRO-proves
  T2/T3 (oversize inputs/results accepted by old code: 201/200). New
  422/prune/pairing/TTL paths are proven by TestClient tests against
  current code (R04b/R15/R16/R18). Temp probe `task9-probe` revoked;
  fabric shows only `emu-lab trusted`.

## K. Physical Vivo status: UNVERIFIED (no device connected; all live
proof is emulator + Linux host + unit tests).

## L. Remaining risks / next step

1. Active (esp. parked WAITING) records unbounded by design — operators
   must cancel stale approvals; consider a parked-count alert (not
   auto-delete).
2. Worst case still ~500 × (1 MiB inputs + 64 KiB results) per store —
   bounded but not small; per-deployment caps are ctor-configurable.
3. Wall-clock approval/presence/retention ages remain NTP-sensitive
   (only job wait-deadlines are monotonic now).
4. Audit table, `events_log`, notifications still unbounded (out of
   scope; audit is forensic evidence — do not cap silently).
5. Restart still drops everything except DB-backed stores (§B).
6. Same preemption/restart limits as Task 8 §11 persist.
Next: Task 10 (event/push triggers). Do NOT bundle calibration v2 or
Governor audit into it.

## M. No commit, push, reset, rebase, amend, force-push, stash, clean,
or discard occurred. All work uncommitted for review.

## N. `git status --short` change summary for review

Task 9 deltas on top of the Task 8 baseline: `M core/jobs.py`,
`M core/execution.py`, `M core/device_auth.py`, `M core/app.py`,
`M docs/research/ASYNC_EXECUTION_API.md` (3 lines), new
`tests/test_retention_ttl.py`, new
`docs/research/EXECUTION_RETENTION_TTL_AUDIT.md`. Full status (50
entries incl. prior work) verified unchanged except these; HEAD
`1c86e32` intact.
