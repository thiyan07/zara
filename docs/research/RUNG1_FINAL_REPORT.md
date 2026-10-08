# Rung 1 Final Report — Privileged Android Capability Gateway
## (`android.app.force_stop`, emulator lab only)

Status: IMPLEMENTED + FUNCTIONALLY PROVEN on emulator. No commit.
Stages 15–19 preserved. Vivo never touched.

Conventions: **DEVICE** = measured on ZaraLab emulator (API 36 userdebug)
· **CORE-UNIT** = pytest, no device · **DOC** = official docs/AOSP.

## 1. Starting git state

HEAD `4f03492`, branch `stage15-real-transfer`; Stages 15–19 uncommitted
(8 modified + 17 new files); baselines 481 Python / 66 Flutter / analyze
clean; ~22 GB free. No Baseline drift found (all expected files present).

## 2. Disk state

22 GB free at end (floor 10 GB never approached). Additions: userdebug
image 4.3 GB (SDK, outside repo), POC sources 172 KB (`/tmp`, outside
repo), one 49.5 MB APK build artifact (gitignored `build/`).

## 3. Subagent findings (all 7 read, none blindly trusted)

- A (audit): gateway = same APK + `when` arm; 10 must-not-modify files;
  8-touch-point slice. Adopted with one change (separate exception type).
- B (probe design): harmless victim, grant-vs-effect split, Core job path.
  Adopted; victim = `dev.zara.lab.privtest`.
- C (security): 1 BLOCKER (arbitrary package targeting) + 10 safeguards.
  Blocker fixed via dual hardcoded allowlists (Core enum + device mirror)
  + high_risk approval gate. All safeguards implemented or tested.
- D (capability design): descriptor + high_risk + schemas + audit shape.
  Adopted almost verbatim; verified every cited invariant in code.
- E (reliability): 11 verdicts; adopted (idempotent stopped, deny paths,
  bounded retry; expiry races observed and explained in §14).
- F (test plan): U/H/N/R/P/E battery; implemented as `tests/test_rung1.py`
  (25 tests) + live E2E instead of a repo emulator test (repo convention:
  physical validation is documented procedure, not committed tests).
- G (scope): 5 KEEP / 22 CUT; obeyed — one capability, one permission,
  no service/Binder/UI/MCP/voice/FCM/Tamil/autonomy work.

## 4. Architecture

Unchanged chain, one new leaf:

```
LLM -> intent/proposal -> resolver -> policy -> grants/approval
  -> /v1/exec submit -> WAITING_FOR_PERMISSION -> operator approve
  -> proxy enqueue (DeviceJob) -> device poll -> job_runner allowlist arm
  -> DeviceBridge.privForceStop (allowlist mirror + self-permission check
     + single reflected ActivityManager call) -> result envelope
  -> reportJobResult -> verification (Core pidof) -> audit
```

New trust assumptions: NONE (existing X-Device auth, job transport,
approval cards). New gates: NONE (existing resolver/policy/governor/
approval/audit). New files of trust: Core `FORCE_STOP_LAB_TARGETS` enum
+ device `forceStopLabTargets` const + human approval of the exact
package.

## 5. Privileged APK design

Same `dev.zara.zara_android` APK (no new package/service/UID): one
manifest line (`FORCE_STOP_PACKAGES`), one Kotlin `when` arm, one Dart
bridge method + runner case + descriptor rows. No `Runtime.exec`,
no `ProcessBuilder`, no shell, no WebView bridge, no dynamic loading
(verified by scan; `grep -rn "Runtime.exec\|ProcessBuilder" android/`
returns only comments/docs). Unknown capability IDs fail closed
device-side (`notImplemented` + runner refusal default untouched).

## 6. Permission used

Exactly one: `android.permission.FORCE_STOP_PACKAGES`
(`signature|privileged`, DEVICE-verified). Declared in manifest, granted
via image allowlist only. No INSTALL/WRITE_SECURE/USB/STATUS_BAR/
AUDIO/GRANT permissions requested (scope review obeyed).

## 7. Allowlist details

- Image: `/system/etc/permissions/privapp-permissions-zara.xml`,
  `privapp-permissions/dev.zara.zara_android → FORCE_STOP_PACKAGES`.
- Placement: `/system/priv-app/ZaraApp/ZaraApp.apk`, 644, userdebug +
  verity-disabled + remount (lab-only procedure, documented, never Vivo).
- Enforcement live: `ro.control_privapp_permissions=enforce`; missing
  entry = `system_server` boot crash loop (observed, §14).
- Core mirror: `FORCE_STOP_LAB_TARGETS = {"dev.zara.lab.privtest"}` enforced
  through `input_schema` `enum` at `registry.call` (pre-handler).
- Device mirror: `forceStopLabTargets` const checked before the bridge.
- Target choice rationale: harmless probe APK we own, relaunchable via
  `am start`, no user data, kills nothing else.

## 8. SELinux evidence

`ps -Z`: our priv-app runs `u:r:priv_app:s0` (upgraded from
`untrusted_app`); UID stays app-range (`u0_a217` — sandbox intact, no
UID change). No policy change, no permissive mode, no AVC denials
attributed to our path.

## 9. Functional force-stop proof (the key result)

Full chain on the lab emulator, each leg evidenced:

1. Victim running: `pidof` returned live PIDs (3987, 3476, 3684…).
2. `resolve android.app.force_stop emu-lab` → `requires_approval`,
   `substituted: False`.
3. `/v1/exec` submit → `waiting_for_permission` (high-risk hold).
4. Operator `approve` → grant minted → proxy enqueued DeviceJob.
5. Device polled (30 s tick), runner allowlist passed, bridge reflected
   `ActivityManager.forceStopPackage(String)` — **no SecurityException,
   no hidden-API block** (single-arg overload discovered by on-device
   method enumeration after an honest first miss on a non-existent
   `(String,int)` overload).
6. **OS-attested kills**: `system_server` log —
   `Force stopping dev.zara.lab.privtest … from pid 3611
   (dev.zara.zara_android)` + `Killing 3476 …` and `Killing 3684 …`.
7. Post-state: `pidof` empty (×3 runs) → `stopped: true`.
8. Relaunch: `am start` → fresh PIDs (3745, 3867, 4040) → app runs again.
9. Execution `succeeded`; audit chain complete (resolve→hold→notify→
   grant→dispatch→result→verify), 200 rows inspected, zero secrets.

Two `succeeded` envelopes reported `was_running: false` (device
`getRunningAppProcesses` under-reports on modern Android — documented
limitation of the device-side flag, predicted in the design doc). The
OS kill lines above are the authoritative proof of live-kill semantics.

## 10. Core→gateway execution path

§4 diagram; every hop is pre-existing code except the new runner case +
bridge arm. ADB used only for setup/inspection/verification (install,
push, dumpsys, pidof, logcat, UI pairing) — never as the kill path.

## 11. Authentication

Existing `X-Device-Id/Key` on poll + result; 401/403 → wipe + revoked
(unchanged). Pairing via one-time claim code typed into the app UI.
No second auth system, no long-lived secrets in logs/UI/LLM/audit
(verified §15).

## 12. Authorization

`high_risk` → `allow=False, requires_approval=True`, no grant path
(`decide()` never consults grants for high_risk). Approval binds one
`(who, capability, device, package)` tuple via single-use engine grant
on approve. Pinned-device no-failover + no-fallback enforced by existing
resolver rules (tested U-4). New tiny improvement (in scope, §19):
approval notifications now include the redacted concrete effect
(`target_package=…`), fixing the observed gap that cards showed only
capability + execution id.

## 13. Verification

Definition of done applied: running (pid) → stopped (pidof empty +
AMS kill lines) → relaunchable (new pid, app functional). Device
`verify_state` is best-effort and labeled as such; Core-side `pidof`
is authoritative. No "API returned" hand-waving anywhere.

## 14. Audit

Core audit chain per execution (resolve/hold/notify/grant/dispatch/
result/verify); device result envelope carries only
package/stopped/was_running/verify_state. Secret scan of 200 rows:
NONE. Observed race (lab timing, not authority): device 30 s poll vs
20 s proxy wait can expire a job the device later completes — the late
completion is still under the genuine approval, but Core records
`expired` while the device reports done. Documented as a lab-timing
artifact; production needs poll/timeout alignment (not changed here —
validated semantics preserved).

## 15. Negative tests (16, CORE-UNIT + DEVICE)

- N-1 normal APK denied: DEVICE — identical code/signature installed via
  `/data` → `BATTERY_STATS=false, FORCE_STOP_PACKAGES=false` (self-report
  + dumpsys). Placement is load-bearing. PROVEN.
- N-2 no-allowlist: DEVICE — `system_server` boot crash loop with the
  exact `not in privapp-permissions allowlist` `IllegalStateException`.
  PROVEN (stronger than silent deny).
- N-3 unknown capability → `no_capability`/HUMAN. N-4 missing auth →
  401/403, no state change. N-5 revoked → unauthorized/None + execute
  rejected. N-6 unadvertised pinned → no substitution. N-7/8/9 malformed/
  empty/oversized → schema/bound rejection pre-handler. N-10 forged
  authority fields → byte-identical verdicts. N-11 screen-text →
  clarification or safe failure, never dispatch. N-12 wrong-scope/
  expired/forged grants → denied. N-13 offline → honest `offline` status.
  N-14 gateway failure → queued/deferred, never blind execution (plus
  observed expiry-race handling). N-15 absent target → contract pinned
  (failed/unknown, never another package). N-16 audit secret-free.
  All in `tests/test_rung1.py` except N-1/N-2 (DEVICE evidence above).

## 16. Revocation test

Live: `revoke emu-lab` → resolve `no_device`/None/0 candidates; app UI
shows "Revoked — pair again" and clears keys on 403; re-pair via fresh
enroll + UI claim → trusted/online, descriptors re-accepted (16/16).
Old keys stay dead. Re-paired to a clean end-state.

## 17. Emulator evidence inventory

- `adb shell pm list permissions -f` protection-level table (API 36).
- `dumpsys package` grant lines (privtest + Zara + normtest).
- `ps -Z` domain lines. `ro.control_privapp_permissions=enforce`.
- Logcat: allowlist boot-crash, `PROBE granted=true/false` rows, method
  enumeration, AMS `Force stopping/Killing` lines with Zara pid + victim pids.
- Core audit rows (resolve/submit/approve/dispatch/result/verify).
- `pidof` before/after/relaunch sequences.
- Build artifacts: `/tmp/opencode/poc/` (172 KB, disposable).

## 18. Physical-device status

BLOCKED / NOT TESTED — Vivo never connected during this task (only
emulators on `adb`); zero privileged components near it; no lock
interaction needed or attempted.

## 19. Limitations (honest)

- Hidden-API reflection is version-fragile (works on API 36 userdebug;
  re-verify per release; platform-signed builds differ).
- Device `was_running`/`verify_state` under-reports (OS pidof rules).
- 30 s poll vs 20 s proxy timeout races on slow emulators (observed,
  audited, no authority impact).
- Victim must be allowlisted + harmless; ANY new target = registry
  change + audit + fresh consent.
- Approval cards now show the effect line (new); human must still read it.
- No functional grant→execution probe for other permissions; installer-
  bypass behavior for INSTALL_PACKAGES still per-flow UNVERIFIED.
- Custom system service (Rung 2) untouched by design.

## 20. Rung-2 requirements (gated, not started)

Written per-primitive gap + approved AOSP sync/build cost + own keys +
OTA stream + rebase process; service class + AIDL + `startOtherServices`
+ `service_contexts` + `.te` + per-method `enforceCallingPermission`;
negative test (unprivileged caller denied); SELinux `avc` audit clean;
Play Integrity loss accepted. Trigger: a concrete primitive SDK+DPM+
priv-app cannot provide.

## 21. Files changed

Core: `core/device_tools.py` (+ allowlist const + tool def),
`core/tools.py` (envelope bounds: ≤16 keys, ≤4 KiB, depth ≤4,
`additionalProperties:false` honored only when declared),
`core/app.py` (approval effect line), `tests/test_stage2.py`
(parity invariant now encodes platform scoping + linux-refusal test).
Android: manifest (+1 permission), `DeviceBridge.kt` (+1 arm + function),
`device_bridge.dart` (+1 method + exception type), `job_runner.dart`
(async run + allowlist case), `main.dart` (wiring ×3),
`capabilities.dart` (+1 lab-noted row), `capability_descriptors.dart`
(risk/requires/alias + grant param). Tests: `tests/test_rung1.py` (25),
`stage9_jobs_test.dart` (async update + 4 new),
`capability_descriptors_test.dart` (+2). Docs: 8 research files
(7 subagent + this report).

## 22. Tests

Python **507 passed** (481 + 25 rung1 + 1 parity addition); Flutter
**71 passed** (66 + 5); `flutter analyze` clean; release APK 49.5 MB.
Two transient voice-synthesis flakes observed once under full-suite load
(files untouched by this diff; 28/28 green in isolation twice) —
recorded, not hidden. No test weakened or deleted (one parity
invariant evolved with documented rationale; async test updates mirror
an intentional signature change).

## 23. Final git state

Branch `stage15-real-transfer`, HEAD `4f03492` (unchanged, verified).
`git diff --check`: only the pre-existing Stage 15 EOF nit. Tree
uncommitted in full.

## 24. Commit status

**No commit was made.**

## Rung-1 success checklist

[x] Stage 15–19 preserved [x] HEAD 4f03492 [x] no commit
[x] disk never ≤10 GB (22 GB) [x] placement proven [x] allowlist proven
(+boot-fatal negative) [x] permission proven (grant=true both sides)
[x] normal APK denied [x] force-stop performed [x] functionally verified
(OS kill lines + pidof + relaunch) [x] Core remains authority
[x] authenticated path [x] unauthorized target rejected (unit + mirror)
[x] revoked rejected (live) [x] unknown capability rejected
[x] malformed input rejected [x] LLM forgery inert [x] audit produced
[x] no secrets [x] regression green [x] analyze clean [x] Vivo untouched
