# Rung 1 Functional Force-Stop Proof — Design Only (Not Executed)

Status: DESIGN ONLY. No code changed. No device command executed.
No file modified except this one.
Emulator-only rule: ZaraLab API 36 `userdebug` emulator only. Vivo (personal device) is NEVER a target, NEVER connected for this probe.

Established facts (from `docs/research/PRIVILEGED_ANDROID_FINAL_REPORT.md` — read first, not re-proven here):

- Priv-app placement honored: APK in `/system/priv-app` → `PRIVILEGED` in `privateFlags`/`pkgFlags`, UID stays app-range sandbox.
- Allowlisted `signature|privileged` permissions actually granted: `BATTERY_STATS` + `FORCE_STOP_PACKAGES` → `granted=true` by both app self-report (`checkSelfPermission`) and `dumpsys package`.
- `FORCE_STOP_PACKAGES` is priv-app-holdable on API 36 (`signature|privileged` per on-device `pm list permissions -f`). Grant proven. **Operation actually working (killing another package via `ActivityManager.forceStopPackage`) is NOT yet proven.** That is exactly what this probe closes.
- Missing allowlist entry is boot-fatal (`system_server` crash loop on `userdebug`) — allowlist state must be verified before and after.
- Grant ≠ effect. This design keeps those two claims strictly separate.

## 1. Harmless lab target package choice (and why it is safe)

Primary choice: a **purpose-built disposable lab target APK** installed on the ZaraLab emulator only (example ID: `dev.zara.labtarget` — any trivial hello-world with one launcher Activity).

Why this is the safe choice:

- It does nothing: no receivers, no sticky/FG service, no device-admin, no accessibility, no account, no data. Force-stopping it cannot lose user data or destabilize the OS.
- It is fully replaceable: if corrupted, uninstall/reinstall; no system state depends on it.
- It is unambiguous in `dumpsys`/`pidof` output (own UID, own process name), unlike shared system processes.
- Relaunch check is trivial (tap icon / start launcher intent) and proves no permanent damage.

Fallback (only if building a trivial APK is impractical): a stock harmless AOSP app with no background role on the emulator, e.g. Calculator. Still safe relative to the forbidden set, but less clean (shared UID / preinstalled assumptions vary by image), so the purpose-built target is preferred.

NEVER targets (explicitly forbidden for this probe):

- `com.android.settings` (Settings) — force-stop/settings-write target confusion; risks breaking the verification path itself.
- `com.android.systemui` (SystemUI) — kills status bar / recents / lock surface; user-visible denial-of-service on the bench.
- Any security, banking, authenticator, or personal app — data-loss / lockout / attestation risk; also violates the emulator-only rule if it implies the Vivo daily driver.
- Any package with a persistent process, device-admin, or active accessibility binding — auto-restart confounds the stopped-state check (see §5).

Target-selection rule for the executor: target package MUST appear on a written single-entry allowlist bound to the probe capability (see §3). Any package name outside that allowlist is refused by default.

## 2. Verification steps — distinguishing "permission held" from "operation worked"

Two separate verdicts, recorded separately. Permission-held passing while operation failing is a valid, reportable outcome (see §5) — it must NOT be re-labeled as success.

### Verdict A — "permission held" (grant state, no behavior implied)

1. `dumpsys package <zara-priv-app>` shows `FORCE_STOP_PACKAGES: granted=true`.
2. In-app self-report (`checkSelfPermission(FORCE_STOP_PACKAGES)`) returns granted (matches the prior session's DEVICE-proven pattern).
3. Record UID (`u0_aXXX`), SELinux context (`u:r:priv_app:s0` expected), placement (`/system/priv-app`), build type (`userdebug`, API 36).

Passing A proves only that the app *may call* the API without a `SecurityException`. It proves nothing about the target's state.

### Verdict B — "operation actually worked" (behavioral, three-phase)

Phase B1 — running-state check (BEFORE):

1. Launch the lab target (launcher tap or start intent — setup step, not the operation under test).
2. Confirm running via inspection only (adb is inspection here, not the force-stop driver): process present (`pidof <target>` non-empty / `dumpsys activity` shows the target process), and record PID.
3. Record stopped-state baseline: `dumpsys package <target>` stopped/force-stop flags as baseline.

Phase B2 — force-stop operation (THROUGH THE JOB PATH, §3):

1. Dispatch the force-stop as a Zara Core device job naming ONLY the lab target (§3). The native call is `ActivityManager.forceStopPackage(<target>)` from the priv-app context.
2. Immediately after job `done`, verify stopped state via inspection:
   - `pidof <target>` empty (process gone),
   - `dumpsys activity` no longer lists the target process,
   - `dumpsys package <target>` shows stopped/force-stopped state.
3. Capture job result payload + Core audit entry for the operation.

Phase B3 — relaunch check (AFTER, proves harmlessness + no wedging):

1. Relaunch the lab target normally.
2. Confirm it runs again (process present, UI launchable).
3. Record that normal user-driven start still works post-force-stop (i.e., the probe did not permanently disable the package — contrast with `suspend`/`hide` semantics, which this probe does NOT use).

Verdict table:

| A (grant) | B (stopped after, relaunch OK) | Report says |
|---|---|---|
| pass | pass | Rung-1 force-stop functionally proven for this target/image/API level |
| pass | fail (process persists / exception) | Grant held, operation failed — see §5 failure-mode analysis; NO success claimed |
| fail | — | Probe invalid (grant regressed); fix grant first, do not interpret B |

No workarounds that weaken security are permitted on failure (no moving the call to shell/UID-0, no broadening the package allowlist, no silently retargeting a weaker API, no skipping approval/audit — see §5).

## 3. Driving it through Zara's Core → device job path (adb is setup/inspection only)

Raw `adb shell am force-stop` MUST NOT be the operation under test: it runs as shell UID, bypasses Core policy, and would prove nothing about the priv-app path. `adb` is allowed only for setup (install lab target, place priv-app image — already done), inspection (dumpsys/pidof/logcat reads), and verification (B1/B2-reads/B3). The force-stop call itself rides the existing Zara chain:

1. **Advertisement**: a new narrow capability descriptor for the probe (e.g. `lab.forcestop.test`), documented via `POST /v1/agent/capabilities/describe` (`core/app.py:1196`), strict validation (`core/capabilities.py:202-243`). Descriptor carries a single bounded param: `package` constrained to the one lab-target name. No free-form package string is ever accepted.
2. **Resolution**: `CapabilityResolver.resolve` (`core/resolver.py:392-476`) → `FabricRegistry.authorize_capability` pipeline (`core/fabric.py:955-1021`).
3. **Policy**: `PolicyEngine.decide` (`core/policy.py:48-76`) — probe risk is `confirm`/`high_risk`, so it consumes a scoped grant or holds for explicit human approval. Deny-patterns still apply.
4. **Governor**: `ResourceGovernor.check` (`core/governor.py:30-46`) — battery/metered/offline defers apply normally.
5. **Approval**: `ExecutionEngine.submit` → `WAITING_FOR_PERMISSION` (`core/execution.py:41-47`) → human decides on the existing phone pull path (`core/app.py:1387-1411`, `android/lib/main.dart:617-645`). The probe does not invent a new approval surface.
6. **Transport/execution**: Core-dispatched `DeviceJob` (`core/jobs.py:41-68`) claimed via `POST /v1/agent/jobs/poll` (`core/app.py:1122`), executed by a future narrow `AndroidJobRunner` allowlist arm beside `android/lib/job_runner.dart:68-101` (today only `system.battery`/`system.network`; anything else is refused — the probe adds exactly one arm), calling a future narrow `DeviceBridge` channel arm (`android/lib/device_bridge.dart` pattern; native `when` arm in `DeviceBridge.kt:60-91`) that invokes `ActivityManager.forceStopPackage()` with the bound package only. Result returns via `POST /v1/agent/jobs/result` (`core/app.py:1138`).
7. **Audit**: every leg recorded via `AuditLog.record` (`core/audit.py:17-25`) — dispatch, approval decision, job completion, native outcome.

Executor note: steps 1/6-native are future code (this file designs; it implements nothing). Until that narrow path exists, there is NO valid functional proof — a shell-driven `am force-stop` is explicitly not accepted as a substitute.

## 4. Evidence to capture

Record verbatim (copy/paste, with timestamps); label DEVICE vs DOC vs INFERENCE per the Final Report convention:

- Target: exact target package name, APK source (lab-built, version/hash), install location, target UID.
- Priv-app: exact package name, APK path (`/system/priv-app/...`), `privateFlags`/`pkgFlags` (`PRIVILEGED`), UID (`u0_aXXX`), SELinux context (`ps -Z`), `checkSelfPermission(FORCE_STOP_PACKAGES)` result.
- Permission state: full `dumpsys package <priv-app>` stanza for `FORCE_STOP_PACKAGES` (`granted=true/false` + flags), `pm list permissions -f` line for `FORCE_STOP_PACKAGES`.
- Build: API level (36), build type (`userdebug`), emulator name (`ZaraLab`), image flavor (`google_apis`).
- Placement: `ls` of priv-app dir entry, allowlist XML path + entry stanza (proves the boot-fatal tripwire is satisfied, not bypassed).
- Allowlist state: allowlist file present with entry (and the known-negative: without entry the image boot-crashes — already DEVICE-proven, do not re-demonstrate destructively).
- Operation: Core job ID, dispatched capability ID + params (package name), approval decision + decider, job `done`/`failed` + result payload, native return/exception text, logcat lines for the force-stop call.
- Verification: B1 PID + running-state dumpsys excerpts, B2 `pidof`/dumpsys excerpts showing stopped state, B3 relaunch confirmation.
- Negatives: any `SecurityException` text in full; any auto-restart PID reappearance with timestamps.

## 5. Failure modes and what each would prove

1. **`SecurityException` despite `granted=true`**: proves grant ≠ capability on this API level (e.g., additional role/caller check, hidden-API/context gate, or `forceStopPackage` requiring a further privileged caller). Report: "permission held, operation denied." No workaround (no shell escalation, no signature spoofing, no broadening manifest). Follow-up is DOC/AOSP analysis of the API's enforcement, not a bypass.
2. **Call returns but target process persists**: proves the API accepted the caller but did not stop this target class (sticky/persistent process, foreground-service restart, or image-specific behavior). Report as operation-failed with process evidence; do NOT relabel "restarted then stopped" as success.
3. **Target auto-restarts (PID reappears seconds later)**: proves force-stop worked transiently but the target (or system) relaunched it — expected for persistent/system packages, which is why §1 forbids them. With the lab target this should not happen; if it does, the target APK design is at fault, not the privilege — fix the target, not the security boundary.
4. **Job-path refusal (`policy_blocked` / `governor_blocked` / `requires_approval` unapproved / unknown-tool refusal)**: proves Core gating works as designed — the probe never reached the native call. Report the refusal verbatim; an unapproved probe is not a failed privilege, it is a correctly held gate.
5. **Allowlist/boot failure during re-imaging**: proves the §J boot-fatal tripwire still holds (already DEVICE-proven; do not intentionally re-trigger on a working image). Report as setup-invalid, not operation evidence.
6. **OEM/image differences (passes on ZaraLab userdebug, fails elsewhere)**: proves the result is scoped to the tested image/API level. Do not generalize; record API + image flavor with the verdict. Production claims require per-image re-verification plus the owned-OTA/key-custody costs in Final Report §J–§K.
7. **Relaunch check fails (target won't start after force-stop)**: proves the probe was NOT harmless — stop, report exactly, reinstall the disposable target, and do not escalate to system packages.

In every failure case the report states the failure exactly, keeps grant and operation verdicts separate, and proposes no change that weakens the Core-decides / narrow-capability / approval / audit chain (§3, Final Report §H).
