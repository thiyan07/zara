# Rung 1 Reliability Audit — Privileged Gateway via Core-Dispatched Device Jobs (Emulator)

Scope: READ-ONLY audit. No code changed, no device work. Question: for a
privileged gateway capability executed as a Core-dispatched device job on an
emulator, what is the CORRECT behavior per failure case —
**succeed / retry-with-bound / deny-and-report** — as determined by
mechanisms that already exist in the repo?

Grounding files read: `android/lib/device_lifecycle.dart`,
`android/lib/connection.dart`, `core/jobs.py`, `core/fabric.py`
(presence + revoke paths), `core/capabilities.py` (descriptor versioning),
`core/resolver.py` (status mapping),
`docs/research/PRIVILEGED_ANDROID_FINAL_REPORT.md` (privilege taxonomy).

## Existing mechanisms (the verdict vocabulary)

- **Job claim semantics** (`core/jobs.py`): `poll(device_id)` flips oldest
  `pending → claimed` and timestamps it; `complete(job_id, ok, …)` flips to
  `done|failed` and is **idempotent** — replays on terminal
  (`done|failed|cancelled`) return the recorded outcome without mutation.
  `cancel()` flips to `cancelled` (refused once terminal).
  `wait_for_result(timeout)` blocks on a Condition and flips to `expired` on
  Core-side timeout. Queue is bounded (`MAX_PENDING_PER_DEVICE = 100`,
  `OverflowError` on enqueue when full). Note: `CLAIM_TIMEOUT_S = 300.0` is
  declared but no reclaim sweeper was found in `jobs.py` — a claimed-but-never-
  completed job has no in-file recovery path; Core-side `expired` only fires
  for jobs still watched via `wait_for_result`. Verdicts below assume this and
  say where `cancel` + re-enqueue is the honest recovery.
- **Bounded retry, never infinite** (`android/lib/connection.dart`):
  `RetryPolicy(maxAttempts=5, 1s→60s exponential, capped shift)`;
  `shouldRetry` returns false when `batteryCritical` (manual reconnect only).
  `PendingQueue(maxSize=50)`: heartbeats coalesce, everything else FIFO,
  `add` returns false when full so the caller must report truthful offline
  state. `ResponseGuard`: stale responses (wrong request ID) are dropped, so a
  slow retry never overwrites fresher state.
- **Presence derivation, stale is never healthy** (`core/fabric.py:111-125`,
  `presence_of`, `authorize_capability`, `execute_on_device`): presence is
  derived from manager `status/online/last_seen` with `stale_s=120s` into
  `online|offline|degraded|stale|unknown`. `authorize_capability` denies
  `OFFLINE|UNKNOWN|STALE` with "stale is never healthy";
  `execute_on_device` rejects `offline|stale|unknown` before policy/engine.
  Routing maps presence/governor failures to **defer**, policy/trust failures
  to **deny** (`route_capability`, `resolver._candidate_status` →
  `offline|governor_blocked|policy_blocked|unauthorized|unavailable`).
- **Revocation tombstones, identity gate first** (`core/fabric.py:396-405`,
  `615-654`, `719-744`, `1203-1216`; `core/device_auth.py`; `core/app.py`
  revoke endpoint; `android/lib/device_lifecycle.dart:78-90`): `revoke_device`
  revokes auth creds, adds a persistent tombstone (`fabric_revocations`
  survives restart/rehydrate), revokes in DeviceManager, deletes capability
  snapshot, revokes live grants, audits `device_revoked`. `trust_of` returns
  `REVOKED` for tombstoned or cred-revoked devices. `execute_on_device`
  checks trust **before** presence/policy/engine. Device side maps 401/403
  with stored identity → `revoked` (caller wipes keys).
- **Descriptor versioning + stale snapshots** (`core/capabilities.py:119-157`,
  `core/fabric.py:747-867`): strict `CapabilityDescriptor(extra="forbid")`;
  unknown `descriptor_version` (anything ≠ `"1"`) is rejected, never
  executed. `advertise()` is full-snapshot refresh with add/remove/change
  diff-audit; garbage batches keep the old snapshot. `mark_stale()` flags the
  prior snapshot usable-but-stale after re-register without fresh describe;
  resolver still routes but labels the verdict stale explicitly.
  `availability ∈ available|unavailable|os_denied|unimplemented` with
  `usable() = available ∧ os_permission_granted ≠ False`; non-usable is
  reported truthfully, never hidden, never executed.
- **Audit on every transition** (`fabric._audit`, resolver reasons, job
  `finished_at`): `execution_rejected|execution_finished|verification_failed|
  capability_added|removed|changed|stale|device_revoked|temporary_grant_*`,
  plus bus publish. "Deny-and-report" always means: terminal job state
  (`failed`/`cancelled`) with reason + audit, never silent drop.

Privilege facts assumed from the FINAL REPORT (DEVICE/DOC, not re-verified
here): priv-app can hold `FORCE_STOP_PACKAGES`, `WRITE_SECURE_SETTINGS`,
install/remove, battery-stats/USB/BT holdings; it can **not** self-grant
runtime permissions (`GRANT_RUNTIME_PERMISSIONS` lacks the `privileged`
flag), inject input, screenshot silently, or bypass consent gates (NLS,
MediaProjection, assistant role, runtime dialogs, indicators).

## Verdicts

| # | Case | Verdict | Grounding |
|---|------|---------|-----------|
| 1 | Privileged gateway APK missing or outdated (not in `/system/priv-app`, no allowlist grant) | **Deny-and-report** (do not retry as-is; operator/image fix required) | Capability must advertise truthfully: if the privileged op cannot run, the descriptor is `unavailable`/`unimplemented`/`os_denied` **with reason**, and `usable()` is false → `authorize_capability` denies ("exists but not usable") and `execute_on_device` rejects before the engine. Resolver maps to `unavailable`, route maps to `deny` (not `defer` — retrying the same image cannot succeed). Missing-allowlist is boot-fatal per REPORT §A.4, so a missing/outdated APK is an image-config defect, not a transient: complete the job `failed` with `availability_reason` + `snapshot_software`, audit `execution_rejected`. Re-dispatch only after fresh `describe` changes the fingerprint (`capability_changed`). |
| 2 | Android app force-stopped mid-poll (gateway kills the Zara body, or the body itself is stopped) | **Retry-with-bound on the Core side; deny-and-report only at the bound** | Force-stop is the REPORT-proven priv-app holding — so this is self-action or operator action, not a device lie. Job semantics cover it: if stop happens before `poll`, the job stays `pending` (bounded queue, still claimable after restart; lifecycle `restore` → `registering`). If after `claim` but before `complete`, the job is `claimed` with no result; Core `wait_for_result` expires it (`expired`), and the honest recovery is `cancel` + re-enqueue a fresh job (given no reclaim sweeper for `CLAIM_TIMEOUT_S` in-file). Device-side retries obey `RetryPolicy` (≤5, backoff, no retry when battery-critical) and `PendingQueue` bound; completion replays are idempotent so a duplicate `complete` after restart cannot double-apply. Presence goes `OFFLINE` → route `defer` while down, not `deny`. |
| 3 | Core unreachable (network down / 5xx at dispatch or poll time) | **Retry-with-bound, then truthful offline** | `connection.dart`: bounded exponential retry, then caller MUST go offline; `PendingQueue` coalesces heartbeats and rejects when full (report offline, don't silently drop). `device_lifecycle.handleHttp`: 5xx from `online` → `reconnecting` (one step, no loops). Core side: presence derives `OFFLINE/STALE` → `authorize` denies with defer semantics; `route_capability` returns `defer` for presence/governor causes. Job stays `pending`/`claimed` server-side; `wait_for_result` yields `expired` at its own timeout rather than fake success. No infinite poll loop on either side. |
| 4 | Connection lost mid-execution (job claimed, work started, result not yet reported) | **Retry-with-bound delivery of the result; never re-execute blindly** | Claim is at-most-once dispatch; `complete` is idempotent, so the correct retry unit is **result delivery**, not re-execution. Device holds the result as a `PendingOp(kind=job-result)` in the bounded queue and retries per `RetryPolicy`; `ResponseGuard` drops stale responses so a delayed duplicate cannot overwrite fresher state. Core `wait_for_result` may mark `expired` while the device still holds the result — late `complete` on a terminal job returns the recorded outcome without mutation, so the device must treat "already terminal" as success-of-reporting, not as license to re-run the privileged op. If the queue fills, report offline; do not discard the job-result silently. |
| 5 | Device revoked mid-flight (operator revoke / 401-403 between dispatch and poll/complete) | **Deny-and-report; stop work immediately** | Identity gate is first (`execute_on_device`: revoked never reaches policy/engine). `revoke_device` tombstones persist across restart, kill creds, delete capability snapshot, revoke live grants, audit `device_revoked`. Any in-flight `poll`/`complete` authenticated with the old key gets 401/403 → device lifecycle moves to `revoked`, caller wipes keys. Queued jobs for that device must be `cancel`led (not retried — retrying a revoked identity is wrong), and `route`/`authorize` return `deny`/`unauthorized`. Re-execution requires explicit `clear_revocation` + fresh enroll/claim (new credential, never restored). Late results from the revoked key are rejected, not applied. |
| 6 | Target package missing (the package the privileged op names is not installed) | **Deny-and-report (succeed the job-reporting, fail the op)** | This is a deterministic pre-check failure, not a transient: complete `failed` with `unknown package` + audited `execution_rejected`/`execution_finished(state=failed)`. Do not retry the same inputs — bounded retry cannot install the package unless the job itself is an install job (separate capability, separate policy/approval). Resolver analogue: unknown capability → `deny` "no device advertises X". Report `snapshot_software` + descriptor version so Core can distinguish "image lacks package" from "stale snapshot". |
| 7 | Target already stopped (force-stop / suspend / hide requested, package already in desired state) | **Succeed (idempotent no-op), report `already-stopped`** | Privileged ops must be idempotent like `jobs.complete`: replays return the recorded outcome. "Already in desired state" satisfies the postcondition — return `done` with `result={already:true}` and audit `execution_finished`, so Core verification (`verify_device_result`) passes on state, not on side-effect count. Retrying or failing here would convert a correct converged state into an error. Same logic as transfer/job terminal-state replays. |
| 8 | Target crashes on launch (app starts then dies; op technically "launched") | **Deny-and-report (failed, with evidence), no blind retry** | Launch ≠ success: `verify_device_result` must fail the job (`state≠succeeded` or output-schema missing keys → `verification_failed` audit). A crash is not presence/governor-transient, so route `deny`, not `defer`. Bounded retry is only justified if Core policy explicitly classifies the op retryable AND the crash signature is known-transient (e.g., cold-start race with backoff); otherwise re-dispatch needs new inputs or an updated package. Never report `done` on a crash, and never let the LLM rephrase a crash as success — verdict comes from `verify_device_result`, not prose. |
| 9 | Permission revoked at runtime (OS revokes a dangerous/consent-gated permission the privileged path needs) | **Deny-and-report as `os_denied`** | REPORT §B/G: priv-app and DO cannot bypass consent gates (runtime dialogs, NLS, MediaProjection, indicators). The descriptor must flip to `os_denied`/`unavailable` **with reason** (`validate_record` requires a reason for v1 descriptors); `usable()` goes false → `authorize` denies, resolver maps to `unavailable`, route to `deny`. The device must re-`describe` so the fingerprint/audit trail (`capability_changed`) reflects the revocation; Core must surface the user-consent step, not retry the denied op. No silent fallback to a more invasive path (no escalation primitive — F2 in REPORT appendix). |
| 10 | Emulator reboot between dispatch and poll (clean boot, same image) | **Retry-with-bound (defer, then resume); expire honestly at the bound** | Reboot is transient-down, not distrust: presence derives `OFFLINE`→`STALE` (never reported healthy), route returns `defer`. Lifecycle `restore(storedDeviceId)` → `registering`; `mark_stale` applies if re-register arrives without fresh `describe` (snapshot usable-but-stale, staleness explicit in verdict). Job stays `pending` (or `claimed` if pre-reboot claim; then Core `expired`/`cancel` + re-enqueue per case 2). Bounded by `wait_for_result` timeout + `RetryPolicy`; at the bound, complete `failed/expired` with `reboot-down` reason + audit, never fake success. Tombstones survive reboot — if the device was revoked pre-reboot it stays `REVOKED` (case 5, not this case). |
| 11 | Privileged APK stale relative to Core descriptor version (device advertises old descriptor, Core expects newer) | **Deny-and-report the stale descriptor; require fresh `describe`** | `CapabilityDescriptor.validate_descriptor` rejects unknown `descriptor_version`; legacy `"0"` records are visible but constrained (platforms-empty = unconstrained only for legacy). `advertise()` diffs fingerprints and audits `capability_changed`; resolver surfaces `stale=true` explicitly and `_constraints_match` enforces `min_version` — an old `version` that fails `min_version` does not route. `execute_on_device` must not run a stale privileged op on new semantics: reject (`execution_rejected`, reason cites `descriptor_version` + `snapshot_software` vs expected), ask for fresh `describe`, and only dispatch after the fingerprint updates. Stale snapshots still route in general, but privileged Rung-1 ops are the wrong place for "route anyway" — version mismatch is a deny, not a defer. |

## Cross-case invariants (do not violate for Rung 1)

1. Trust → presence → permission → policy → governor → execute → verify → audit (`execute_on_device`); revoked/unknown never reaches the engine.
2. Retry is always bounded (`maxAttempts=5`, queue `50`/`100`, `wait_for_result` timeout); exhaustion ends in a terminal, audited job state.
3. Completion is idempotent; delivery retries, not execution retries, after claim.
4. Stale is never healthy; stale privileged descriptors deny pending fresh `describe`.
5. Every deny names its reason (`availability_reason`, `descriptor_version`, `snapshot_software`, trust/presence checks) in the job result and audit — silent failure is the only unacceptable outcome.
