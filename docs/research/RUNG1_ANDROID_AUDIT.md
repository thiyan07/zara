# Rung 1 Android Audit — Privileged Gateway Placement

> READ-ONLY audit. No existing file modified, no code changed, no device touched.
> Context read first: `docs/research/ZARA_ANDROID_INTEGRATION.md`,
> `docs/research/PRIVILEGED_ANDROID_FINAL_REPORT.md`.
> Every claim grounded in `file:line`. Scope: `android/lib/*.dart`,
> `android/android/app/src/main/` (esp. `DeviceBridge.kt`, `MainActivity.kt`,
> `AndroidManifest.xml`), plus the Core counterparts the app actually calls.

## 0. Baseline (what exists today)

- Single channel only: Dart `android/lib/device_bridge.dart:7`
  (`MethodChannel('zara/device')`) ↔ Kotlin
  `android/android/app/src/main/kotlin/dev/zara/zara_android/DeviceBridge.kt:49`
  (`CHANNEL = "zara/device"`), dispatched in `DeviceBridge.kt:60-91`,
  unknown → `notImplemented` (`DeviceBridge.kt:89`). Wired once in
  `MainActivity.kt:35-39`; runtime permission requests are Activity-mediated
  (`MainActivity.kt:41-53`, codes `REQ_MIC 9001` / `REQ_NOTIF 9002` at
  `MainActivity.kt:31-33`).
- Channel inventory is closed: `getBattery` / `getNetwork` / `getPermissions` /
  `getVoiceSupport` / `audioCapture` / `audioStop` / `audioPlay` /
  `audioPlayStop` / `requestMicPermission` / `requestNotifPermission` /
  `createNotificationChannels` / `audioSelfTest` / `getAssistantStatus`
  (`DeviceBridge.kt:62-88`). No force-stop, no package-manager, no second
  channel, no service binding — verified by grep (zero hits for
  `force_stop|FORCE_STOP|privileg|zara/priv` under `android/`).
- Auth: in-memory `deviceId`/`deviceKey` (`core_client.dart:38-39`), headers
  `X-Device-Id` / `X-Device-Key` (`core_client.dart:44-50`), 401/403 →
  `AuthException`, 404 → `NotRegisteredException` (`core_client.dart:52-57`),
  15 s bounded timeout (`core_client.dart:42`). Keys live in `SecureStore`
  slots `zara_device_id` / `zara_device_key` (`secure_store.dart:11-12`),
  Keystore-backed with round-trip verify (`secure_store.dart:32-35`).
- Jobs: poll `core_client.dart:142-145` → strict parse
  `DeviceJob.fromJson` (`job_runner.dart:26-41`, malformed → never executed)
  → `AndroidJobRunner.run` (`job_runner.dart:70-106`) → result
  `core_client.dart:147-157`. Allowlist is exactly
  `{'system.battery', 'system.network'}` (`job_runner.dart:68`); default arm
  is structured refusal, never exec (`job_runner.dart:95-101`). Dedup
  (`job_runner.dart:71-76`), cancel-before-start (`job_runner.dart:77-79`).
- Core proxy side: `core/device_tools.py:59-77` (`make_proxy_handler`:
  enqueue → `wait_for_result`, malformed result rejected); defs at
  `core/device_tools.py:25-56`, all `supported_devices=["linux","android"]`
  (`core/device_tools.py:21`).
- Priv-app facts (FINAL_REPORT, DEVICE-proven): same-APK placement in
  `/system/priv-app` honors `PRIVILEGED` flag, grants allowlisted
  `signature|privileged` permissions including `FORCE_STOP_PACKAGES`
  (FINAL_REPORT §A.1–A.2); taxonomy confirms `FORCE_STOP_PACKAGES` is
  priv-app-holdable while `INJECT_EVENTS` / `GRANT_RUNTIME_PERMISSIONS` are
  not (FINAL_REPORT §A, `pm list permissions -f` table).

## 1. Where the privileged gateway should live

**Recommendation: same APK, new `when` arm(s) on the existing `zara/device`
channel (or one second channel `zara/privileged` owned by the same
`DeviceBridge` class) — NOT a new APK, NOT a separate service, NOT a custom
system service.**

| Option | Verdict | Why (grounded) |
|---|---|---|
| New `when` arm in `DeviceBridge.kt:60-91` (same channel) | **Preferred** | Smallest delta: follows the exact `audioCapture` precedent (`DeviceBridge.kt:96-146`, denial → typed error `DeviceBridge.kt:97-101`). No new manifest component, no new IPC, no new identity. `else → notImplemented` (`DeviceBridge.kt:89`) keeps backward compat with old Core. |
| Second channel (e.g. `zara/privileged`), same class/process | Acceptable alternative | Isolates audit/review surface; same trust domain (same UID, same Keystore, same `X-Device` identity). Cost: second `MethodChannel` attach beside `MainActivity.kt:35-39`, parallel `invokeMapMethod` wrapper mirroring `device_bridge.dart:59-69`. Justifiable only if reviewer wants privileged calls greppable in one file. |
| Separate APK (new package) | **Reject** | New package = new Keystore namespace (`secure_store.dart:21-22` is per-app), new `deviceId`/`deviceKey` enrollment (`core_client.dart:89-103`), new pairing UX (`main.dart:337-360`), new lifecycle instance (`device_lifecycle.dart:45-60`). All new trust assumptions for zero capability gain — the privilege comes from priv-app placement + allowlist XML (FINAL_REPORT §A), which applies per-package and would have to be duplicated. |
| Separate bound/exported service | **Reject** | Every component in `AndroidManifest.xml:15,46,57,67` already sets `exported=true` only where the OS requires it (launcher, voice-interaction bind, ASSIST). A new exported service is a confused-deputy surface (FINAL_REPORT §J: per-method caller checks required, `exported=false` mitigation). Same-process method call needs none of this. |
| Custom system service (AIDL + `SystemServer`) | **Reject for Rung 1** | Explicitly deferred: needs full AOSP sync/build, `service_contexts`, `.te` policy, platform keys, own OTA stream (FINAL_REPORT §D, §I.4–5 NOT BUILT). `FORCE_STOP_PACKAGES` does not need it — it is priv-app-holdable (FINAL_REPORT §A table). Revisit only on a written per-primitive gap (FINAL_REPORT §H rung order). |

Placement note: the APK binary is unchanged in kind; privilege comes from
image placement (`/system/priv-app` + allowlist XML entry), which is
boot-fatal if missing (FINAL_REPORT §A.4, §J). That XML lives in the
**image, not the repo** — so the repo delta stays pure app code.

## 2. How the current agent talks to it with no new trust assumptions

No new keys, no new identity, no direct Binder auth, no second enrollment.
The privileged call rides the exact existing job loop:

1. **Dispatch (Core → device, unchanged transport):** Core proxy
   (`core/device_tools.py:59-77`) enqueues a `DeviceJob`; device claims it
   in `_pollJobs` (`android/lib/main.dart:310-322`) via
   `pollJobs` (`core_client.dart:142-145`), on the existing `_tick` cadence
   (`main.dart:254-308`, 30 s healthy / 60 s `reduceBackgroundWork`).
2. **Auth (unchanged):** poll and result both carry `X-Device-Id/Key`
   (`core_client.dart:44-50`); 401/403 → wipe + revoked
   (`main.dart:289-292`, `device_lifecycle.dart:78-90`); 404 → single
   re-register (`main.dart:293-297`). `SecureStore` slots untouched
   (`secure_store.dart:11-12`).
3. **Execution (one new allowlist arm):** `DeviceJob.fromJson` strict parse
   (`job_runner.dart:26-41`) → new `case` beside `job_runner.dart:81-94` →
   synchronous `DeviceBridge` call with bounded params → `JobResult`.
   Unknown/refused tools keep the current refusal shape
   (`job_runner.dart:95-101`); Core treats refusal as done, never retries
   blindly.
4. **Result (unchanged):** `reportJobResult(job.jobId, ok, result, error)`
   (`core_client.dart:147-157`), error truncated to 300 chars
   (`core_client.dart:155`). Malformed jobs return before execution
   (`main.dart:313-318`).
5. **Human gate (unchanged):** privileged risk forces `confirm`/`high_risk`
   → `WAITING_FOR_PERMISSION` → existing approval card + `_decide`
   → `decideApproval` (`core_client.dart:172-177`); 409 duplicate guard
   stays. No approval UI is built — the pull path
   (`main.dart:324-335`) already is the human surface.
6. **What is explicitly NOT introduced:** no second header/token
   (cf. `devToken` is local-dev-only, `core_client.dart:40`), no
   `exported` service intent, no shell/exec path (the runner has none —
   `job_runner.dart:1-7` documents never-exec/never-shell/never-fetch).

## 3. Capability abstractions reusable verbatim

| Abstraction | Reuse | Reference |
|---|---|---|
| Descriptor doc builder | Verbatim shape: `id`, `descriptor_version`, `risk`, `requires`, `platforms`, `execution`, `requires_foreground`, `availability` / `availability_reason`, `os_permission_granted`, `aliases` | `capability_descriptors.dart:67-94` (`androidCapabilityDocs`) |
| Risk / permission maps | Add one row each, same const-map pattern; privileged entry takes `confirm` or `high_risk` (Core decides, advisory here) | `_riskOf` at `capability_descriptors.dart:15-31`; `_requiresOf` at `capability_descriptors.dart:34-38`; `_foregroundOf` at `capability_descriptors.dart:40-43` |
| Honest availability | `os_denied` + reason when the OS gate is off; permission-gated branch pattern | `capability_descriptors.dart:55-66` |
| Wire-text sanitizer | ASCII-sanitize any new description/alias (latin-1 `HttpClientRequest.write` constraint) | `_ascii` at `capability_descriptors.dart:103-117` |
| Alias table | One new alias row (e.g. force-stop verbs) | `_aliasesOf` at `capability_descriptors.dart:119-134` |
| Allowlist + refusal | New `case` beside existing arms; default refusal text unchanged | `allowlisted` at `job_runner.dart:68`; arms at `job_runner.dart:81-94`; refusal at `job_runner.dart:95-101` |
| Dedup / cancel | Job-ID dedup cap 200, cancel-before-start — no change needed | `job_runner.dart:71-79` |
| Key custody | Slots, Keystore path, round-trip verify, wipe-on-401/403 and logout | `secure_store.dart:11-12,19,32-35`; callers at `main.dart:210,290,368` |
| Lifecycle machine | States, allowed-transition table (illegal throws), `restore`, `handleHttp` single-step, logout-to-pairing | `device_lifecycle.dart:8-31,53-72,78-100,105-112`; driven at `main.dart:120-136,242-308,648-664` |
| Bridge error contract | `supported:false` fallback for state reads; typed `AudioBridgeException(denied\|unavailable\|cancelled\|failed)` for actions; `notImplemented` for unknown | `device_bridge.dart:9-18,57-69,164-171`; `DeviceBridge.kt:89-91,97-101,108-110` |
| Registration flow | `register` string caps + `describeCapabilities` docs, retried on permission flip; register-retry backoff | `main.dart:177-240` (`register` at `:184`, `describeCapabilities` at `:189-195`); `main.dart:40-44` |
| Capability table rule | Flip to `supported=true` ONLY with hardware/lab evidence (file's own rule); reserved entries document the refusal | `capabilities.dart:16-22` + entries at `capabilities.dart:23-48`; `advertisedCapabilities` at `capability_descriptors.dart:53-54` via `capabilities.dart:50-52` |

## 4. Files that MUST NOT be modified (and why)

1. **`android/lib/secure_store.dart`** — key custody. Slot names
   (`:11-12`), Keystore-vs-fallback reporting (`:19`), round-trip verify
   (`:32-35`) are load-bearing for claim/register/rotate and the wipe paths.
   Touching this risks silent credential divergence (fallback vs keystore).
2. **`android/lib/core_client.dart` auth core (`:38-57`)** — `_deviceHeaders`
   (`:44-50`) and `_raise` (`:52-57`) map every transport outcome to the
   lifecycle. Adding a second auth scheme here would fork the trust root;
   §2 shows none is needed.
3. **`android/lib/device_lifecycle.dart` transition table (`:20-31`) and
   `move`/`handleHttp` (`:53-100`)** — illegal transitions throw by design;
   the tick loop, boot, pause/resume, and revoke paths all assume this
   table. Privilege adds no new lifecycle state.
4. **`android/lib/job_runner.dart` refusal default (`:95-101`)** — the
   never-exec guarantee. A privileged addition is an allowlist *arm*, never
   a softened default.
5. **Existing `when` arms + audio paths in `DeviceBridge.kt:62-88,96-302`**
   — capture/playback honesty contracts (typed errors, `audibility:
   manual-only` at `:273-275`, `USAGE_MEDIA` OriginOS workaround at
   `:187-189`). Force-stop must not alter audio behavior or focus handling.
6. **`AndroidManifest.xml` existing components (`:13-76`)** — launcher,
   voice-interaction services, ASSIST proxy. No attribute tightening or
   loosening; the only manifest delta is one additive `<uses-permission>`
   (see §5), plus image-side allowlist XML that is not in the repo.
7. **`android/lib/capabilities.dart` existing 15 supported rows + 7 reserved
   rows (`:23-48`)** — their notes record physical-verification evidence
   (voice Stage 10, transfer Stage 15) and by-design refusals
   (`accessibility.automation` at `:47`). Editing them invalidates the
   Stage-16 descriptor diff contract (`main.dart:188-199`).
8. **`MainActivity.kt` permission plumbing (`:41-65`)** — single-pending-result
   mediation, denial-is-normal. A new runtime permission (none needed for
   force-stop) would extend, not rewrite, this.
9. **Voice chain (`voice.dart`, `wake.dart`, `voice_session.dart`,
   `audio_io.dart`, `ZaraVoiceInteraction*.kt`, `ZaraAssistProxyActivity.kt`)**
   — privileged work rides jobs/grants, never audio bytes (§10 of the
   integration audit). No wake/assistant change is in scope.
10. **Notification pull/ack/decide (`main.dart:324-335,617-645`,
    `zara_notifications.dart`, `push.dart`)** — the approval human surface.
    Privilege consumes it; it must not fork it.

## 5. Smallest integration point: ONE capability (`android.app.force_stop`, lab allowlist target)

Target semantic: force-stop one lab-owned test package (hardcoded allowlist,
e.g. a single `labTargetPackage`), gated `high_risk` (explicit human
approval, no fallback). Non-allowlisted package → structured refusal.

1. **Image side (not the repo, lab emulator only):** APK placed in
   `/system/priv-app` + allowlist XML granting `FORCE_STOP_PACKAGES`
   (mechanics DEVICE-proven; missing entry boot-crashes — FINAL_REPORT
   §A.4). No repo file records this; lab build notes live outside the tree.
2. **Manifest (additive, one line):**
   `<uses-permission android:name="android.permission.FORCE_STOP_PACKAGES" />`
   beside `AndroidManifest.xml:3-8`. Nothing else in the manifest changes.
3. **Kotlin — one new `when` arm** in `DeviceBridge.kt:60-91` (e.g.
   `"privForceStop"`), implemented beside `battery()`/`network()`
   (`DeviceBridge.kt:442-501` pattern): read `targetPackage` arg → check
   against hardcoded lab allowlist set → call
   `ActivityManager.forceStopPackage` (holder-only API; throws
   `SecurityException` without the priv-app grant → map to `denied` error
   like `DeviceBridge.kt:97-101`) → return `{supported, stopped, package}`.
   Denial/unlisted package = typed error, never an exception leak.
4. **Dart bridge — one new method** on `DeviceBridge`
   (`device_bridge.dart:57-69` `audioCapture` pattern):
   `privForceStop(String packageName)` via `invokeMapMethod`, throwing
   `AudioBridgeException`-shaped typed error (or a sibling
   `PrivBridgeException` reusing `:164-171`), `supported:false` on
   `PlatformException`.
5. **Runner — one new `case`** beside `job_runner.dart:81-94`
   (e.g. `case 'android.app.force_stop':`), validating `inputs['package']`
   against the same lab allowlist before touching the bridge; anything else
   falls into the unchanged refusal (`job_runner.dart:95-101`).
6. **Capability table — one new row** in `capabilities.dart:23-48`
   (`ZaraCapability('android.app.force_stop', …)`), flipped to `true` only
   with lab evidence per the file's rule (`capabilities.dart:18`); until
   then `false` + note, which keeps it out of `advertisedCapabilities()`
   (`capabilities.dart:50-52`) and out of descriptors
   (`capability_descriptors.dart:54`).
7. **Descriptors — two one-row additions:** `_riskOf['android.app.force_stop']
   = 'high_risk'` (`capability_descriptors.dart:15-31` pattern),
   `_requiresOf` entry (priv-app grant, not a runtime permission —
   availability still flows through `os_denied` at
   `capability_descriptors.dart:63-66` when the grant self-check fails),
   plus one alias row in `_aliasesOf` (`capability_descriptors.dart:119-134`).
8. **Core — one `DEVICE_TOOL_DEFS` entry** (`core/device_tools.py:25-56`
   `_def` pattern): `risk=HIGH_RISK`, `required_capabilities=
   ["android.app.force_stop"]`, bounded input schema (`package` string,
   allowlist enforced device-side too), `supported_devices` unchanged
   (`["linux","android"]` at `device_tools.py:21` — linux agents will refuse
   via their own allowlist, same as today). Policy/governor/approval/audit
   paths need zero changes.

Explicit non-goals for this slice: no new channel, no new service, no new
permission-request UX (force-stop needs no runtime grant), no lifecycle /
key / notification / voice changes, no Vivo involvement.
