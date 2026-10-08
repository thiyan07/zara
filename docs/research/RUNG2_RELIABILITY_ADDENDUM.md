# Rung 2 — Reliability Addendum (UI-control failure behavior)

Status: SPECIFICATION ONLY. No code changed, no device work.
Evidence conventions: **DEVICE** / **CORE-UNIT** / **DOC** per
`RUNG1_FINAL_REPORT.md`; everything not measured is **NOT YET MEASURED**.

Method (same as `RUNG1_RELIABILITY.md`): for each failure case, the expected
behavior — **succeed / retry-with-bound / deny-and-report** — is derived ONLY
from mechanisms that already exist in the repo. No new mechanism is assumed.
Anything the table states about `gui.*` on-device behavior is NOT YET
MEASURED (no `gui.*` op has run on any device).

## Reused mechanisms (vocabulary; all REPO + CORE-UNIT unless noted)

- **Job expiry** (`core/jobs.py`): `poll` flips oldest `pending → claimed`;
  `complete` to `done|failed` is idempotent (terminal replays return the
  recorded outcome); `wait_for_result(timeout)` flips to `expired` on
  Core-side timeout; queue bounded (`MAX_PENDING_PER_DEVICE = 100`);
  no reclaim sweeper for claimed-but-never-completed — honest recovery is
  `cancel` + re-enqueue. (RUNG1_RELIABILITY §mechanisms.)
- **Presence** (`core/fabric.py`, `presence_of`): derived from
  status/online/last_seen with `stale_s = 120 s`; `authorize_capability`
  denies OFFLINE/UNKNOWN/STALE ("stale is never healthy");
  `execute_on_device` rejects offline/stale/unknown before policy/engine;
  routing maps presence/governor causes to **defer**, policy/trust causes
  to **deny**.
- **Tombstones** (`core/fabric.py` revoke paths; `device_lifecycle.dart`):
  `revoke_device` kills creds, persists the tombstone across restart,
  deletes the capability snapshot, revokes live grants, audits
  `device_revoked`; trust checked BEFORE presence/policy/engine; device
  maps 401/403-with-identity to `revoked` and wipes keys.
- **Descriptor versioning** (`core/capabilities.py`, `fabric.advertise`):
  strict schema (`extra="forbid"`, unknown `descriptor_version` rejected);
  full-snapshot refresh with add/remove/change diff-audit; garbage keeps the
  old snapshot; `mark_stale` on re-register without fresh describe;
  `usable() = available ∧ os_permission_granted ≠ False`; non-usable is
  reported truthfully, never executed.
- **Bounded retry** (`android/lib/connection.dart`): `RetryPolicy`
  maxAttempts=5, 1s→60s exponential; no retry when battery-critical;
  `PendingQueue(maxSize=50)` with truthful-offline on full; `ResponseGuard`
  drops stale responses.
- **UI stuck handling** (`RUNG2_UICONTROL.md` §7): S1 no-progress, S2
  oscillating, S3 blocked-dialog, S4 app-changed, S5 core-offline, S6
  verify-unavailable — all terminal `failed` with reason + audit.

## Behavior table

| # | Case | Expected outcome | Grounding (existing mechanism) |
|---|------|------------------|--------------------------------|
| 1 | Gateway unavailable (device not reachable / `gui.*` descriptor `unavailable` or `unimplemented`) | **Deny-and-report** (`unavailable`; route = deny, not defer, when the descriptor says unusable — retrying the same snapshot cannot succeed) | Descriptor `usable()` false → `authorize_capability` denies ("exists but not usable"); resolver `UNAVAILABLE`; job completes `failed` with `availability_reason` + `snapshot_software` + audit `execution_rejected`. Re-dispatch only after fresh `describe` changes the fingerprint. (RUNG1_RELIABILITY case 1 pattern. On-device `gui.*` unavailability behavior: NOT YET MEASURED.) |
| 2 | App killed mid-flow (target package force-stopped / crashed between inspect and tap) | **Stop (S4 app-changed), then Core decides: deny-and-report the job; re-dispatch is a NEW decision** | Re-inspect sees package/activity mismatch → `failed(reason=target-changed)`; never continue in the new foreground app (cross-package rule). If the kill was the OS reclaiming memory, re-dispatch needs a fresh approval-bound job (new tuple), not a resume — a tap verified against a dead snapshot is void. Idempotent READs (`inspect`/`assert_visible`) may re-run under a new job; non-idempotent gestures never blind-retry. (On-device kill-window rates: NOT YET MEASURED.) |
| 3 | Core offline (network down / 5xx at dispatch, poll, or result time) | **Retry-with-bound delivery, then truthful offline** | Device `RetryPolicy` (≤5, backoff, battery-critical stops) for poll/result delivery; result held as bounded `PendingOp(kind=job-result)`; queue-full → report offline. Core presence derives OFFLINE/STALE → defer. The retry unit after claim is RESULT DELIVERY, never re-execution of the tap (RUNG1_RELIABILITY case 4; UICONTROL S5). `wait_for_result` yields `expired` rather than fake success. |
| 4 | Grant/device revoked mid-flow (operator revoke / 401-403 between inspect and tap) | **Deny-and-report; stop work immediately** | Identity gate first: revoked never reaches policy/engine. Tombstone persists; snapshot deleted; live grants revoked; queued jobs `cancel`led (never retried — retrying a revoked identity is wrong). Device wipes keys on 401/403-with-identity. A tap dispatched pre-revoke but unexecuted must NOT execute post-revoke: the runner re-checks trust at execution time. (Revocation mechanics DEVICE-proven for the Rung-1 path in RUNG1 §16; `gui.*`-specific revocation timing: NOT YET MEASURED.) |
| 5 | Missing package (approval-bound target package not installed) | **Deny-and-report** (deterministic pre-check failure) | Complete `failed(reason=unknown-package)` with `snapshot_software` + descriptor version; no retry of the same inputs (bounded retry cannot install the package — install is a separate capability/approval). Resolver analogue: unknown capability → deny. (Pattern CORE-UNIT-covered via RUNG1 N-15; `gui.*` pre-check path: NOT YET MEASURED.) |
| 6 | Already-stopped / already-in-desired-state (postcondition holds before acting, e.g. toggle already off, element already visible for `assert_visible`) | **Succeed (idempotent no-op)** | Postcondition satisfied → `done` with `result={already: true}` + audit `execution_finished`; verification passes on STATE, not on side-effect count (RUNG1_RELIABILITY case 7; `jobs.complete` idempotency precedent). Retrying or failing here would convert convergence into error. (`gui.*` state-convergence behavior: NOT YET MEASURED.) |
| 7 | Target crashes on launch / during flow (app starts then dies; tap "landed" but app died) | **Deny-and-report (`failed`, with evidence), no blind retry** | Launch ≠ success: `verify_device_result` fails the job (post-inspect predicate unmet → `verification_failed` audit). Crash is not presence/governor-transient → route deny, not defer. Bounded retry only if Core policy classifies the op retryable AND the crash signature is known-transient; otherwise new inputs/updated package required. Never let prose rephrase a crash as success. (Crash-during-UI-flow rates: NOT YET MEASURED.) |
| 8 | Permission change at runtime (user disables the AccessibilityService, revokes a consent the flow needs) | **Deny-and-report as `os_denied`** | Descriptor flips to `os_denied`/`unavailable` WITH reason; `usable()` false → deny; device re-`describe`s so fingerprint/audit (`capability_changed`) reflects the change; Core surfaces the user-consent step (app-hierarchy Level 6), never retries the denied op, never falls back to a more invasive path (never-auto-elevate rule). In-flight tap MUST abort pre-execution if the service unbinds (UICONTROL S6). (FINAL REPORT §B/G consent-gate finding is DOC; `gui.*` permission-flip handling: NOT YET MEASURED.) |
| 9 | Reboot between dispatch and poll (clean boot, same image) | **Retry-with-bound (defer, then resume); expire honestly at the bound** | Transient-down, not distrust: presence OFFLINE→STALE (never healthy), route defer. Lifecycle `restore` → `registering`; `mark_stale` if re-register lacks fresh describe. Job stays `pending` (or `claimed` → Core `expired`/`cancel` + re-enqueue). Tombstones survive reboot (revoked stays revoked — case 4, not this case). At the bound: terminal `failed/expired` with `reboot-down` reason + audit, never fake success. (RUNG1_RELIABILITY case 10 pattern; UI-flow resume-after-reboot: NOT YET MEASURED.) |
| 10 | Stale APK relative to Core descriptor version (device advertises old `gui.*` descriptor; service binary predates the approved spec) | **Deny-and-report the stale descriptor; require fresh `describe`** | Unknown `descriptor_version` rejected; `min_version` mismatch does not route; privileged ops are the wrong place for "route anyway" (RUNG1_RELIABILITY case 11). `execute_on_device` must not run a stale UI op on new semantics: reject with reason citing `descriptor_version` + `snapshot_software` vs expected, await fingerprint update. Stuck-state S6 (`verify-unavailable`) covers the inspect path of the same defect. (Version-skew handling mechanics CORE-UNIT-covered; `gui.*` skew instance: NOT YET MEASURED.) |

## Cross-case invariants (inherited, not re-argued)

1. Trust → presence → permission → policy → governor → execute → verify → audit; revoked/unknown never reaches the engine.
2. Retry always bounded (attempts ≤ 3 UI + 5 transport, queues 50/100, `wait_for_result` timeout); exhaustion ends terminal + audited.
3. Completion idempotent; after claim, delivery retries — never execution retries (critical for taps: a duplicate tap is a duplicate effect).
4. Stale is never healthy; stale privileged/UI descriptors deny pending fresh `describe`.
5. Every deny names its reason in job result + audit. Silent failure is the only unacceptable outcome.
6. No `gui.*` row above upgrades to DEVICE evidence until measured on the ZaraLab emulator with OS-attested state (kill lines / `pidof` / fresh `dumpsys` equivalents for UI: pre/post snapshots + activity records) — the RUNG1 §13 definition-of-done standard applies verbatim.
