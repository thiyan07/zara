# Zara Android Integration — Current-Implementation Audit

> READ-ONLY audit of the repository as found. Every claim is grounded in a
> `file:line` reference. No code was changed. No device was touched.
> Scope: `android/lib/` (Flutter/Dart), `android/android/app/src/main/` (Kotlin +
> manifest), and the Core counterparts the app actually calls (`core/`).
> Absence claims (e.g. "no Accessibility code") were verified by grep, not assumed.

## 1. Entry points and app structure (`android/lib/`)

| File | Role |
|---|---|
| `android/lib/main.dart:23` | `void main() => runApp(const ZaraApp())` — sole entry point. |
| `android/lib/main.dart:51-55` | `ZaraApp` StatefulWidget: "owns NO authority … presentation + device I/O only". |
| `android/lib/main.dart:57-102` | `_ZaraAppState` owns: `DeviceBridge`, `CoreClient(_coreUrl)`, `SecureStore`, `DeviceLifecycle`, `AndroidVoiceSession`, `NotificationCenter`, `AndroidJobRunner`, plus diagnostic controllers/timers (`_loop`, `_regRetry`, `_deciding`). |
| `android/lib/main.dart:61-63` | Core URL from `--dart-define=ZARA_CORE_URL`, default `http://10.0.2.2:8080` (emulator loopback). |
| `android/lib/main.dart:690-886` | `build()`: single-screen diagnostic UI — status, pairing field, approval cards, mic/notif buttons, voice diagnostic, transfer diagnostic, capability list. No navigation, no background service, no isolate. |
| `android/lib/app_state.dart:7-32` | `ZaraUiState`: presentation-only snapshot (connection, lifecycle, deviceId, battery, network, voice, approvalPending, lastError). Holds no credentials by construction. |
| `android/pubspec.yaml:1-20` | Package `zara_android`; deps are only `flutter` + `flutter_secure_storage`. No FCM, no STT/TTS, no accessibility, no telephony plugin. |
| `android/lib/assistant.dart:5-40` | `AssistantStatus` pure-Dart parse of the native assistant report; defaults to not-registered/unknown. |

Supporting pure-Dart modules (no platform calls except via `DeviceBridge`):

- `android/lib/connection.dart:6-24` — `CoreLinkState` machine; `android/lib/connection.dart:28-49` `RetryPolicy` (bounded, max 5); `android/lib/connection.dart:69-92` `PendingQueue` (bounded 50, heartbeats coalesce); `android/lib/connection.dart:97-101` `ResponseGuard` (stale-response drop).
- `android/lib/battery_governor.dart:1-77` — advisory-only mapping; thresholds mirror Core (`criticalBelow 15.0`, `resumeAbove 25.0`, `cautionBelow 30.0` at `android/lib/battery_governor.dart:13-15`).
- `android/lib/network_monitor.dart:1-46` — `NetworkSnapshot.fromBridge` (`android/lib/network_monitor.dart:21-31`), `online` = connected|metered (`android/lib/network_monitor.dart:33-34`).
- `android/lib/secure_store.dart:1-66` — see §6.
- `android/lib/device_lifecycle.dart:1-116` — see §7.
- `android/lib/job_runner.dart:1-106` — see §9.
- `android/lib/zara_notifications.dart:1-103`, `android/lib/push.dart:1-69` — see §8.
- `android/lib/voice.dart:1-67`, `android/lib/voice_session.dart:1-122`, `android/lib/wake.dart:1-53`, `android/lib/audio_io.dart:1-146` — see §10.
- `android/lib/transfer.dart:1-131` — local SHA-256 + chunking helpers; see §6/§11.
- `android/lib/capabilities.dart:1-52`, `android/lib/capability_descriptors.dart:1-138` — see §5.

## 2. CoreClient — how the app talks to Zara Core

File: `android/lib/core_client.dart:1-301`. `dart:io` HttpClient only, no third-party HTTP.

- Identity: in-memory `deviceId`/`deviceKey` (`android/lib/core_client.dart:38-39`); headers `X-Device-Id` / `X-Device-Key` (`android/lib/core_client.dart:44-50`). Long-term storage is `SecureStore`'s job (§6).
- Errors: `_raise` maps 401/403 → `AuthException`, 404 → `NotRegisteredException` (`android/lib/core_client.dart:52-57`); every call has a 15 s bounded timeout (`android/lib/core_client.dart:42`); cancellation is cooperative `CancelScope` (`android/lib/core_client.dart:29-33`).
- Endpoints actually used (Dart → Core `core/app.py` handler):

| Dart method | HTTP | Core handler |
|---|---|---|
| `claim` `core_client.dart:89-94` | `POST /v1/agent/claim` | `core/app.py:1045` — exchanges one-time pairing code for device key |
| `register` `core_client.dart:96-103` | `POST /v1/agent/register` (`kind: android`) | `core/app.py:1074` — registers presence + string caps, marks descriptor snapshot stale |
| `describeCapabilities` `core_client.dart:136-140` | `POST /v1/agent/capabilities/describe` | `core/app.py:1196` — strict Stage-16 validation, identity from auth only |
| `updateCapabilities` `core_client.dart:125-131` | `POST /v1/agent/capabilities` | `core/app.py:1111` (defined; not called on the current boot path — `describeCapabilities` is used instead) |
| `heartbeat` `core_client.dart:105-116` | `POST /v1/agent/heartbeat` | `core/app.py:1092` — battery/charging/network/online; Core flips degraded <15 / recovers ≥25 |
| `rotateKey` `core_client.dart:118-123` | `POST /v1/agent/rotate` | `core/app.py:1062` (defined in client; no call site in `main.dart` today) |
| `pollJobs` `core_client.dart:142-145` | `POST /v1/agent/jobs/poll` | `core/app.py:1122` — oldest pending job for this device or null |
| `reportJobResult` `core_client.dart:147-157` | `POST /v1/agent/jobs/result` | `core/app.py:1138` — completes job, publishes `execution_finished`, audits |
| `fetchNotifications` `core_client.dart:159-164` | `POST /v1/agent/notifications` | `core/app.py:1332` — pending for this device (broadcast + addressed) |
| `ackNotification` `core_client.dart:166-168` | `POST /v1/agent/notifications/ack` | `core/app.py:1347` — own-or-broadcast only |
| `decideApproval` `core_client.dart:172-177` | `POST /v1/agent/approvals/$executionId` approve\|deny | `core/app.py:1387` — device-scoped: only executions targeted at this device; 409 on double-decide |
| `voiceTurn` `core_client.dart:200-208` | `POST /v1/agent/voice/turn` | `core/app.py:1289` — bounded WAV up, transcript+reply back (no audio down) |
| `tts` `core_client.dart:212-219` | `POST /v1/agent/tts` | `core/app.py:1313` — provider WAV down, ≤2000 chars, audited |
| `interruptVoice` `core_client.dart:221-225` | `POST /v1/agent/voice/interrupt` | `core/app.py:1304` |
| `xferRequest` `core_client.dart:231-247` | `POST /v1/agent/xfer/request` | `core/app.py:1213` — sender/recipient binding server-side, grant checked |
| `xferChunk` `core_client.dart:249-255` | `POST /v1/agent/xfer/$id/chunk` | `core/app.py:1227` — strict order, bounded |
| `xferPending` `core_client.dart:277-279` | `GET /v1/agent/xfer/pending` | `core/app.py:1244` |
| `xferBytes` `core_client.dart:281-285` | `GET /v1/agent/xfer/$id/bytes?offset&length` | `core/app.py:1248` |
| `xferAck` `core_client.dart:287-291` | `POST /v1/agent/xfer/$id/ack` | `core/app.py:1264` — recipient hash verification |
| `xferCancel` `core_client.dart:293-296` | `POST /v1/agent/xfer/$id/cancel` | `core/app.py:1275` (defined; transfer diagnostics use local stop-flag instead) |
| `registerPush` / `invalidatePush` `core_client.dart:179-186` | `POST /v1/agent/push/register` \| `/invalidate` | `core/app.py:1364` / `core/app.py:1374` (defined; no call site in `main.dart` — push is mock/fallback, §8) |
| `syncEvents` `core_client.dart:188-190` | `POST /v1/agent/events/sync` | `core/app.py:1156` (defined; voice `syncEvent` in `android/lib/voice_session.dart:116-122` builds the payload but `main.dart` never sends it today) |
| `disconnect` `core_client.dart:192-196` | `POST /v1/agent/disconnect` | `core/app.py:1163` — best-effort goodbye on logout (`android/lib/main.dart:362-373`) |

Core protocol schemas live in `core/protocol.py:27-52` (`HeartbeatPayload`, `CapabilityUpdate`, `ExecutionRequest/Result`); device auth in `core/device_auth.py:40-102` (enroll/claim/verify/revoke/rotate).

## 3. MethodChannels / platform bridges — native APIs actually called today

Single channel. Dart side `android/lib/device_bridge.dart:7`:
`static const MethodChannel _ch = MethodChannel('zara/device')`.
Kotlin side `android/android/app/src/main/kotlin/dev/zara/zara_android/DeviceBridge.kt:49`
`const val CHANNEL = "zara/device"`, handler `android/.../DeviceBridge.kt:60-91`.
Wiring: `MainActivity.configureFlutterEngine` attaches the bridge
(`android/.../MainActivity.kt:35-39`); runtime permission requests are
Activity-mediated via `requestZaraPermission` (`android/.../MainActivity.kt:41-53`,
request codes `REQ_MIC 9001` / `REQ_NOTIF 9002` at `MainActivity.kt:31-33`).

Complete method inventory (nothing else exists on the channel; unknown → `notImplemented` at `DeviceBridge.kt:89`):

| Channel method | Dart caller | Native implementation | Android API touched |
|---|---|---|---|
| `getBattery` | `device_bridge.dart:9-18` | `DeviceBridge.kt:442-483` `battery()` | Sticky `ACTION_BATTERY_CHANGED` (`IntentFilter`, `BatteryManager.EXTRA_*`), `PowerManager.isPowerSaveMode`; caches `lastBatteryPct` (`DeviceBridge.kt:53`) |
| `getNetwork` | `device_bridge.dart:20-29` | `DeviceBridge.kt:485-501` `network()` | `ConnectivityManager`, `NetworkCapabilities` (wifi/cellular), `isActiveNetworkMetered` |
| `getPermissions` | `device_bridge.dart:31-40` | `DeviceBridge.kt:503-519` `permissions()` | **Read-only** `checkSelfPermission` for `RECORD_AUDIO`, `POST_NOTIFICATIONS` (API 33+, else true), `CAMERA`, `ACCESS_FINE_LOCATION` — state report only, never requests |
| `getVoiceSupport` | `device_bridge.dart:46-55` | `DeviceBridge.kt:528-566` `voiceSupport()` | `FEATURE_MICROPHONE`, `FEATURE_AUDIO_OUTPUT`, `RECORD_AUDIO` state, `SpeechRecognizer.isRecognitionAvailable`, `TextToSpeech.engines` presence. Note: `audio_capture`/`stt`/`tts`/`wake_word_engine` are hardcoded `false` here (`DeviceBridge.kt:560-563`) — stale relative to the Stage-10 verified path; the real capture/playback state is proven per-call, not via this table |
| `audioCapture` | `device_bridge.dart:59-69` (≤30 s, throws `AudioBridgeException`) | `DeviceBridge.kt:96-146` | `AudioRecord` (`VOICE_RECOGNITION`, 16 kHz mono 16-bit, `coerceIn(0.5, 30.0)` at `DeviceBridge.kt:103`), manual WAV wrap (`DeviceBridge.kt:156-178`) |
| `audioStop` | `device_bridge.dart:71-79` | `DeviceBridge.kt:69`, `148-154` | `AudioRecord.stop/release` |
| `audioPlay` | `device_bridge.dart:83-93` | `DeviceBridge.kt:182-294` | `AudioTrack` `MODE_STREAM` + `AudioManager` focus (`USAGE_MEDIA`/`CONTENT_TYPE_SPEECH`; `USAGE_ASSISTANT` rejected on OriginOS per `DeviceBridge.kt:187-189`); honors WAV's own rate 8–48 kHz (`DeviceBridge.kt:221-225`); completion = clean exit, `audibility: manual-only` (`DeviceBridge.kt:273-275`) |
| `audioPlayStop` | `device_bridge.dart:95-103` | `DeviceBridge.kt:72`, `296-302` | `AudioTrack.stop/release` |
| `requestMicPermission` | `device_bridge.dart:107-117` | `DeviceBridge.kt:73-74` → `MainActivity` | `RECORD_AUDIO` runtime request |
| `requestNotifPermission` | `device_bridge.dart:119-129` | `DeviceBridge.kt:75-83` | `POST_NOTIFICATIONS` on API 33+ (`TIRAMISU`), auto-true below |
| `createNotificationChannels` | `device_bridge.dart:131-135` (called at boot `main.dart:121`) | `DeviceBridge.kt:84-86`, `306-319` | `NotificationManager.createNotificationChannel` ×3: `zara_approvals` HIGH, `zara_missions` DEFAULT, `zara_status` LOW |
| `audioSelfTest` | `device_bridge.dart:152-161` (called at every `_refresh`, `main.dart:143-149`) | `DeviceBridge.kt:418-440` | `getMinBufferSize` in/out + `FEATURE_MICROPHONE`/`FEATURE_AUDIO_OUTPUT`; API path only, no permission, no audio |
| `getAssistantStatus` | `device_bridge.dart:138-148` (read at every `_refresh`, `main.dart:153-166`) | `DeviceBridge.kt:329-414` `assistantStatus()` | `Settings.Secure "assistant"`, `RoleManager` (available/held), own `VoiceInteractionService` descriptor XML parse (`service_info_valid`, `supports_assist`, `session_service`); `role_request_possible: false` always (`DeviceBridge.kt:407`) |

What is deliberately NOT on the channel: no SMS/telephone, no contacts, no camera capture, no location fix, no Bluetooth, no notification-listener, no device-admin, no accessibility (see §4).

## 4. Accessibility-related code — confirmed ABSENT

- Grep `accessib|AccessibilityService|BIND_ACCESS|NotificationListener|DeviceAdmin` over `android/`: zero hits in Kotlin, manifest, or Dart outside the two explicit refusals below and one status-report line.
- `android/lib/capabilities.dart:47`: `ZaraCapability('accessibility.automation', false, 'UI automation: never — not a fallback for assistant integration (by design)')`.
- `android/lib/capabilities.dart:21-22` header: "accessibility never a fallback. Nothing is faked."
- `android/test/capabilities_test.dart:28` and `android/test/capability_descriptors_test.dart:32` pin `accessibility.automation` as not-advertised / never-described.
- Manifest (`android/.../AndroidManifest.xml:1-89`): no `BIND_ACCESSIBILITY_SERVICE`, no `<accessibility-service>`; the only services are `ZaraVoiceInteractionService` + `ZaraVoiceInteractionSessionService` (`BIND_VOICE_INTERACTION`, `AndroidManifest.xml:43-61`).
- Native session (`ZaraVoiceInteractionSession.kt:80-91`): `onHandleAssist` reads `AssistStructure.activityComponent` for the *underlying-app label only*; screen content/screenshot path is unused (`SHOW_WITH_SCREENSHOT` is only logged at `ZaraVoiceInteractionSession.kt:67-71`). The reserved `screen.capture` capability is `false` (`capabilities.dart:44`).
- Conclusion: there is no UI-automation path to repurpose. Any privileged capability that needs to act on another app's UI has **no existing bridge, service, or permission** — it would be greenfield native code plus Core authorization (§12).

## 5. Capability descriptors — what is advertised

Source of truth: `android/lib/capabilities.dart:23-48` (`androidCapabilities`), advertised via `advertisedCapabilities()` (`capabilities.dart:50-52`, supported-only).

Supported=true today (15): `notification.receive`, `notification.display`, `battery.report`, `network.report`, `system.battery`, `system.network`, `permissions.report`, `android.lifecycle`, `audio.capture.api`, `audio.playback.api`, `push.mock`, `voice.input`, `voice.output`, `assistant.role`, `files.transfer`.
Reserved false by design (7): `voice.wake_word` (`capabilities.dart:42` — no always-on DSP loop), `camera.available`, `screen.capture` (assist screenshot toggle OFF, never read), `location.available`, `bluetooth.available`, `accessibility.automation` (`capabilities.dart:43-47`).

Descriptor build: `android/lib/capability_descriptors.dart:48-97` `androidCapabilityDocs({micPermission, notifPermission})` — one doc per supported capability; permission-gated availability (`os_denied` + reason when mic/notif denied, `capability_descriptors.dart:58-66`); advisory risk map `capability_descriptors.dart:15-31` (`audio.capture.api`/`audio.playback.api`/`voice.input`/`voice.output`/`files.transfer` = `confirm`, rest `safe`); permission map `capability_descriptors.dart:34-38` (`RECORD_AUDIO`, `POST_NOTIFICATIONS`); foreground set `capability_descriptors.dart:40-43`; `files.transfer` is the only `core-mediated` execution (`capability_descriptors.dart:78-79`), everything else `on-device`. Wire text is ASCII-sanitized (`capability_descriptors.dart:103-117`).

Notable specifics:

- `files.transfer`: note at `capabilities.dart:40` documents the Stage-15 physical proof (3 uploads + 4 fetches, SHA-256 verified); descriptor aliases `send file / share file` (`capability_descriptors.dart:129-130`). Core counterpart: `TRANSFER_CAPABILITY = "files.transfer"`, `TRANSFER_RISK = "confirm"` (`core/transfer.py:44-45`); canonical descriptor `core/capabilities.py:278-324` (consistency-tested against transfer limits).
- `voice.input`/`voice.output` (`capabilities.dart:36-37`): `PHYSICAL_VERIFIED` capture→STT→turn and Piper TTS→AudioTrack→heard; aliases `listen/hear`, `speak/say` (`capability_descriptors.dart:125-128`).
- `assistant.role` (`capabilities.dart:38`): default-assistant role via ASSIST proxy `PHYSICAL_VERIFIED`; session path `NOT_SUPPORTED` on OriginOS.
- `voice.wake_word` stays `false` even though the phrase `"Hey Zara"` is exact and single-sourced (`android/lib/voice.dart:29`); verification is manual-capture only (§10).

Core side validation: `core/capabilities.py:202-215` `validate_descriptor_doc` (bounded, forbidden-key scan `capabilities.py:56-62`, no authority fields), batch `capabilities.py:218-243`, fingerprint/diff `capabilities.py:246-275`. `core/app.py:1196-1208` rejects device-supplied identity; `core/fabric.py:747-...` `advertise` stores the snapshot; re-register marks stale (`core/app.py:1081-1087`, `core/fabric.py:856`).

## 6. Device protocol and authentication (key storage, pairing)

Pairing (claim): UI field + Connect (`main.dart:719-726`, `_connect` at `main.dart:337-360`) → `client.claim(code, 'android-phone')` (`core_client.dart:89-94`) → `POST /v1/agent/claim` (`core/app.py:1045-1060`; one-time code, `DeviceAuthStore.claim` at `core/device_auth.py:55-69`) → `secureStore.saveDeviceCredentials` (`main.dart:348-351`).

Key storage: `android/lib/secure_store.dart:10-66` — slots `zara_device_id` / `zara_device_key` (`secure_store.dart:11-12`); real path `flutter_secure_storage` → Keystore-backed EncryptedSharedPreferences; in-memory fallback only if platform storage fails, reported via `backend` (`secure_store.dart:19`); round-trip verified on save (`secure_store.dart:32-35`); never plaintext/logs/git (`secure_store.dart:3-9`). In-memory use in `CoreClient` (`core_client.dart:38-39`); wiped on 401/403 (`main.dart:210`, `main.dart:290`) and logout (`main.dart:368`).

Registration: `_register` (`main.dart:177-240`) → `register(advertisedCapabilities(), 'zara-android 9.0.0')` (`main.dart:184`, string caps at `capabilities.dart:50-52`) → `describeCapabilities(androidCapabilityDocs(...))` (`main.dart:189-195`; retried on permission flip at `main.dart:774-780`, `791-797`). 401/403 → clear keys + `lifecycle.handleHttp(403)` → revoked (`main.dart:208-215`); unreachable → bounded backoff 5/10/20/40/60 s (`main.dart:40-44`, `220-236`).

Ongoing auth: every request carries `X-Device-Id/Key` (`core_client.dart:44-50`); Core `agent_auth` verifies via `DeviceAuthStore.verify` (`core/device_auth.py:71-78`); revoke path `core/app.py:1172-1191` (auth revoke + fabric tombstone `core/fabric.py:719` + transfer revoke); rotate defined both sides (`core_client.dart:118-123`, `core/device_auth.py:88-102`, `core/app.py:1062-1072`) but has no UI call site today.

## 7. Lifecycle / pairing / reconnect handling

- States: `android/lib/device_lifecycle.dart:8-18` (uninitialized/pairing/enrolled/registering/online/degraded/reconnecting/revoked/logged-out/error); legal transitions `device_lifecycle.dart:20-31` (illegal throws, `device_lifecycle.dart:53-60`).
- Boot: `_boot` (`main.dart:120-136`) → channels + `_refresh`, load creds → pairing if absent (`main.dart:127-131`), else `lifecycle.restore(id)` → registering (`device_lifecycle.dart:64-72`) → `_register`.
- Connect: pairing → enrolled → registering → online (`main.dart:353-355`, `201`).
- Tick loop: `_startLoop` (`main.dart:242-252`) — refused when battery-critical; 30 s healthy / 60 s `reduceBackgroundWork` (`battery_governor.dart:66-71`); `_tick` (`main.dart:254-308`) skips unless online + network.online, then refresh → heartbeat → degraded/low-battery handling (Core thresholds mirrored at `main.dart:270-280`, `core/app.py:1100-1108`) → `_pollJobs` → `_pullNotifications`; failures counted, `RetryPolicy.shouldRetry` decides reconnecting vs offline (`main.dart:299-304`, `connection.dart:45-49`); 401/403 → wipe + revoked; 404 → single re-register (`main.dart:293-297`).
- Register retry: `registerRetryDelay` (`main.dart:40-44`); timer cancelled on pause/dispose (`main.dart:222`, `652-653`, `668`).
- Foreground/background: `didChangeAppLifecycleState` (`main.dart:648-664`) — pause cancels loop+retry; resume restarts only via `shouldResumeLoop` (`main.dart:34-36`: online|degraded) or retries registering (`main.dart:659-663`).
- Logout: `_logout` (`main.dart:362-373`) → cancel timers, best-effort `disconnect`, clear keys + null client identity, `lifecycle.logout()` → logged-out (`device_lifecycle.dart:105-112`), UI disconnected.
- `handleHttp` mapping (`device_lifecycle.dart:78-100`): 401/403 + identity → revoked; 404 → reregister; 5xx online → reconnecting; at most one step per call.
- Stale-approval 409 helper: `isStaleApproval` (`main.dart:28`); double-decide guard `_deciding` set (`main.dart:99`, `617-645`); Core approve path raises 409 on non-waiting execution (`core/execution.py:56-60`, surfaced at `core/app.py:1403-1408`).

## 8. Notification system

- Core → device is **pull**: `_pullNotifications` (`main.dart:324-335`) → `fetchNotifications` (`core_client.dart:159-164`) → `POST /v1/agent/notifications` (`core/app.py:1332-1345`, titles ≤120, bodies ≤500) → `NotificationCenter.show` dedups by ID (`zara_notifications.dart:87-94`, cap 200 at `zara_notifications.dart:82`).
- Model: `DeviceNotification.fromJson` (`zara_notifications.dart:30-46`) — title containing "approv" → `zara_approvals` channel, else mission→`zara_missions`, else `zara_status`; secrets scrubbed (`zara_notifications.dart:50-64`, mirrors `core/tracing.py`); `NotificationAction` (`zara_notifications.dart:73-79`) carries IDs only, never executes locally.
- Approval cards: `_decide` (`main.dart:617-645`) → `decideApproval(execId, approve)` (`core_client.dart:172-177`) → `POST /v1/agent/approvals/{exec_id}` (`core/app.py:1387-1411`: device-scoped — `rec.device_id != device_id` → 403 + `approval_scope_denied` audit; approve→`ExecutionEngine.approve` / deny→`cancel` at `core/execution.py:56-77`; 409 → local dismiss as stale). On success: local `notifCenter.ack` + `ackNotification` (`main.dart:624-627`).
- Channels exist natively (`DeviceBridge.kt:306-319`, created at boot `main.dart:121`) but display today is in-app list (`main.dart:727-760`); no `NotificationManager.notify` call exists — system-tray posting is not implemented.
- Push is mock + poll-fallback by design: Dart `MockPushTransport`/`PushProvider` (`android/lib/push.dart:13-69`; token rules `push.dart:36-40`, dedup `push.dart:60-68`); Core `PushRegistry` (`core/push.py:25-91`; mock outbox, FCM deferred at `core/push.py:83-86`); client `registerPush/invalidatePush` (`core_client.dart:179-186`) have no `main.dart` call sites; Core `send` is best-effort nudge only (`core/push.py:63-86`).

## 9. Jobs / polling

- Core queue: `core/jobs.py:31-113` — `enqueue` (cap 100/device, `jobs.py:34-58`), `poll` oldest-pending→claimed (`jobs.py:60-68`), `complete` idempotent (`jobs.py:70-81`), `wait_for_result` server-side Condition (`jobs.py:98-110`).
- Device poll: `_pollJobs` (`main.dart:310-322`) → `pollJobs` (`core_client.dart:142-145`) → `DeviceJob.fromJson` strict parse (`job_runner.dart:26-41`; malformed → return, never execute, `main.dart:313-318`) → `AndroidJobRunner.run` (`job_runner.dart:70-106`) → `reportJobResult` (`core_client.dart:147-157`).
- Allowlist today: exactly `{'system.battery', 'system.network'}` (`job_runner.dart:68`); anything else → structured refusal, never exec/shell/fetch (`job_runner.dart:95-101`). Snapshots injected from bridge (`main.dart:108-111`; `battery()` → `toHeartbeat()` at `battery_governor.dart:49-50`; network map at `main.dart:110`).
- Core dispatch path (for context): `POST /v1/dispatch` (`core/app.py:1413-1432`) → router → `ExecutionEngine.submit` (`core/execution.py:26-54`, policy-gated, approval→`WAITING_FOR_PERMISSION`) → device-proxy handler blocks on queue (`core/device_tools.py:59-77`); registered device tools all declare `supported_devices=["linux","android"]` (`core/device_tools.py:21`) with defs at `core/device_tools.py:25-56`.
- Poll cadence is the `_tick` loop (§7); server `poll` also counts as presence heartbeat (`core/app.py:1122-1127`).

## 10. Voice system (capture/playback, STT/TTS wiring, wake phrase)

- Phrase: exactly `"Hey Zara"`, single constant (`android/lib/voice.dart:29`); UI echoes it (`main.dart:706`).
- Wake evaluation (NOT wired to any loop today): `WakeController.evaluate` (`android/lib/wake.dart:33-52`) — battery gate first (`voice.dart:32-54`), then DSP signal OR (`vadVoice && transcript.contains(wakePhrase)`); DSP source defaults to null (`wake.dart:31`: "Null DSP => always null"); `WakeSignal` typedef documents hardware DSP as future plug-in (`wake.dart:11-14`). `voice.wake_word` capability stays false by design (`capabilities.dart:42`). No always-on listener, no service, no worker exists in `main.dart` or Kotlin.
- Capture: diagnostic `_recordTest` (`main.dart:378-423`) → `bridge.audioCapture(seconds: 5.0)` (`device_bridge.dart:59-69`) → native `AudioRecord` (§3); peak evidence `wavPeakDbfs` (`audio_io.dart:19-32`; digital zeros → null = silence, never faked); bounds `AudioBounds.maxSeconds 30` (`audio_io.dart:10-14`); mocks `MockMicCapture`/`MockSpeakerOutput` (`audio_io.dart:79-146`) are test-only.
- STT/TTS are Core-side: `client.voiceTurn(wav)` (`core_client.dart:200-208`, `main.dart:398`) → `POST /v1/agent/voice/turn` (`core/app.py:1289-1302`, ≤2 MiB, 422/413 guards) → `VoicePipeline.handle_audio` (`core/voice.py:578-619`: LISTENING→TRANSCRIBING→THINKING→EXECUTING/SPEAKING→IDLE; STT transcribe → core loop → TTS stream; provenance `stt_provider/model/fallback_used` returned). Reply voiced via `client.tts(reply)` (`core_client.dart:212-219`, ≤2000 chars) → `POST /v1/agent/tts` (`core/app.py:1313-1328`, unprivileged synthesis, audited as `agent_tts`).
- Playback: `_speakLast` (`main.dart:425-444`) → `bridge.audioPlay(wav)` (`device_bridge.dart:83-93`) → native `AudioTrack` (§3); completion is process-exit, "software-only, NOT audibility" (`main.dart:433-435`, `DeviceBridge.kt:272-275`). Stop: `_stopVoice` (`main.dart:446-457`) → `audioStop` + `audioPlayStop` + `interruptVoice` (all best-effort).
- Session mirror: `AndroidVoiceSession` (`voice_session.dart:40-122`) — device states `paused_battery`/`offline` added (`voice_session.dart:15-17`); turn budgets identical to Core 5/300 s/60 s (`voice_session.dart:55-57`); `gate({batteryOk, online})` (`voice_session.dart:79-101`); `recordTurn` (`voice_session.dart:109-113`); `syncEvent` builds voice-state-only payload (`voice_session.dart:116-122`) but has no sender wired today (§2). State enum shared with `voice.dart:6-26` (`VoiceState` + transitions).
- Assistant-role path (voice-adjacent, not STT): ASSIST proxy activity opens the normal app surface only (`ZaraAssistProxyActivity.kt:18-28`); `VoiceInteractionService` is readiness/diagnostic only, no tools/policy/secrets (`ZaraVoiceInteractionService.kt:9-16`); session surface is a stateless diagnostic view with no Core calls/audio (`ZaraVoiceInteractionSession.kt:27-34`); OS candidacy reported honestly via `getAssistantStatus` (§3). OriginOS session path `NOT_SUPPORTED` per `capabilities.dart:38`.

## 11. AndroidManifest permissions actually declared

File: `android/android/app/src/main/AndroidManifest.xml:1-89`.

- Requested: `RECORD_AUDIO` (`:3`, runtime, Activity-mediated), `POST_NOTIFICATIONS` (`:5`, runtime on API 33+), `INTERNET` (`:7`, normal), `ACCESS_NETWORK_STATE` (`:8`, normal).
- NOT requested: no `CAMERA`, no `ACCESS_FINE_LOCATION`/`COARSE_LOCATION`, no `READ/RECEIVE/SEND_SMS`, no `CALL_PHONE`/`READ_PHONE_STATE`, no `READ_CONTACTS`, no `SYSTEM_ALERT_WINDOW`, no `BIND_ACCESSIBILITY_SERVICE`, no `BIND_NOTIFICATION_LISTENER_SERVICE`, no `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`, no `FOREGROUND_SERVICE_*`. (`CAMERA`/`ACCESS_FINE_LOCATION` appear only as read-only status checks in `DeviceBridge.kt:516-517`, never as `<uses-permission>`.)
- Components: launcher `MainActivity` (`:13-34`); `VoiceInteractionService` + `SessionService` with `BIND_VOICE_INTERACTION` (`:43-61`, diagnostic per §10); `ZaraAssistProxyActivity` for `ACTION_ASSIST` (`:65-76`); `queries ACTION_PROCESS_TEXT` (`:83-88`, Flutter engine text plugin).

## 12. Privileged-capability plug-in map — where new work would land

"Privileged" here means anything beyond today's 15 supported caps that touches
another app's data, device-external effects, or OS-guarded APIs (e.g. SMS,
phone calls, contacts, notification content, screen/UI automation, camera,
location, system settings). **None of this exists today** (§4, §5, §11).
The map below says exactly which existing module would grow, which Core gate
still applies, and what stays untouched — following the repo's own patterns.

### 12.1 The non-negotiable gate chain (stays unchanged)

Any privileged capability must flow through the existing Core authorities;
no device-side addition bypasses them:

1. **Advertisement**: new descriptor via `POST /v1/agent/capabilities/describe`
   (`core/app.py:1196`) → strict validation (`core/capabilities.py:202-243`,
   forbidden authority keys `core/capabilities.py:56-62`) → `FabricRegistry.advertise`
   (`core/fabric.py:747`). Devices document; Core never trusts (`core/capabilities.py:1-16`).
2. **Resolution**: `CapabilityResolver.resolve` (`core/resolver.py:392-476`) →
   per-candidate `FabricRegistry.authorize_capability` 13-step pipeline
   (`core/fabric.py:955-1021`: exists → trust → presence → OS-usable →
   `PolicyEngine.decide` → `ResourceGovernor.check`).
3. **Policy**: `PolicyEngine.decide` (`core/policy.py:48-76`) — privileged risk
   must be `confirm`/`high_risk` (grant-consumed or approval-held; high-risk
   never fails over, `core/resolver.py:143-146`). Deny-patterns at
   `core/policy.py:10-18` still apply to request text.
4. **Governor**: `ResourceGovernor.check` (`core/governor.py:30-46`) — battery/
   metered/offline defers; android + cost>0.6 reroutes (`governor.py:44-45`).
5. **Approval**: `ExecutionEngine.submit` → `WAITING_FOR_PERMISSION`
   (`core/execution.py:41-47`) → human decides on the phone pull path
   (`core/app.py:1387-1411`, `_decide` at `android/lib/main.dart:617-645`);
   409 is the duplicate guard (`core/execution.py:56-60`).
6. **Audit**: every leg recorded — register/heartbeat/capability/job/approval/
   transfer/TTS at `core/app.py:1088, 1118, 1148, 1409, 1324` via
   `AuditLog.record` (`core/audit.py:17-25`); device auth events at
   `core/app.py:1042, 1059, 1071`.
7. **Transport**: privileged work executes as a Core-dispatched `DeviceJob`
   (`core/jobs.py:41-68`) claimed via poll (`core/app.py:1122`), result via
   (`core/app.py:1138`); or as Core-mediated relay like transfer
   (`core/transfer.py:1-19`, endpoints `core/app.py:1213-1287`).

### 12.2 Plug-in points per privileged class

| Privileged class | Dart caller (new) | Channel/service (new native) | Core registration (new) | Gate notes |
|---|---|---|---|---|
| SMS read/send, call log, phone call, contacts | New method(s) on `DeviceBridge` (`android/lib/device_bridge.dart:6-162` pattern: `invokeMapMethod` + `AudioBridgeException`-style typed error, `supported:false` fallback) + new `AndroidJobRunner` allowlist arm(s) beside `job_runner.dart:68-101` | New `when` arm(s) in `DeviceBridge.kt:60-91` + manifest `<uses-permission>` (none today, §11) + Activity runtime-request path in `MainActivity.kt:41-53` | New `DEVICE_TOOL_DEFS` entries (`core/device_tools.py:25-56`) with `risk=CONFIRM/HIGH_RISK`; descriptors flow through `androidCapabilityDocs` + `_riskOf/_requiresOf` (`capability_descriptors.dart:15-38`) | `confirm` needs live scoped grant (`policy.py:64-73`, transfer-style `grant_id` at `core/transfer.py:44-45`); `high_risk` needs explicit human approval, no fallback (`resolver.py:143-146`) |
| Notification-listener content | Same Dart/channel pattern as above; status-only `permissions()` read (`DeviceBridge.kt:503-519`) would gain a real listener-state bit | New `NotificationListenerService` subclass + `BIND_NOTIFICATION_LISTENER_SERVICE` + user opt-in Settings screen (no such service today, §4) | Same as above; availability `os_denied` until the listener is enabled (`capability_descriptors.dart:58-66` pattern) | Content is untrusted input: scrub before display/transport like `zara_notifications.dart:50-64`; push payloads stay IDs-only (`core/push.py:63-86`) |
| Screen/UI automation | **No reuse possible**: `accessibility.automation` is `false` by design (`capabilities.dart:47`); `screen.capture` is `false` (`capabilities.dart:44`); no `AccessibilityService` exists (§4) | Greenfield `AccessibilityService` + declaration + user binding — contradicts the current by-design refusal; resolver ladder marks GUI as represented-NEVER-executable (`core/resolver.py:59-66`, `EscalationLevel.GUI`) | Would resolve at best to `REQUIRES_ESCALATION`/HUMAN (`resolver.py:56-57, 572-576`); fallback registry forbids destructive failover (`resolver.py:102-146`) | Expect `policy_blocked`/`governor_blocked`/HUMAN-terminal, not execution; this is the one class where "plug in" means reversing a documented design decision |
| Camera / location / Bluetooth | New `DeviceBridge` methods + `permissions()`-style state + allowlist arms, same shape as `system.battery`/`system.network` (`job_runner.dart:81-94`) | New channel arms calling CameraX / FusedLocation / BLE + manifest permissions + runtime requests | New tool defs + descriptors with `requires=[CAMERA/ACCESS_FINE_LOCATION/...]` (`capability_descriptors.dart:34-38` pattern), availability via `os_permission_granted` (`capabilities.py:197-199`) | Governor battery/metered defers apply (`governor.py:30-46`); location/camera data stays device-scoped inputs, verified like job results (`device_tools.py:59-77`) |
| Always-on wake (DSP) | `WakeSignal` source injection (`wake.dart:11-14`) + a real loop calling `WakeController.evaluate` (`wake.dart:33-52`) | DSP/hotword native source feeding the typedef; foreground-service + battery exemption permissions (none today) | None (wake is local state-change request only, `wake.dart:1-8` — "no code path" to tools/approve/memory/governor) | Battery policy gates first (`voice.dart:32-54`); `WakeBatteryPolicy` + `reduceBackgroundWork` (`battery_governor.dart:66-71`) + `_startLoop` critical refusal (`main.dart:246`) still throttle it |

### 12.3 What stays unchanged for any privileged addition

- `SecureStore` slots and Keystore path (`secure_store.dart:11-23`); `X-Device-Id/Key` headers (`core_client.dart:44-50`); enroll/claim/verify/revoke/rotate (`device_auth.py:40-102`).
- Lifecycle state machine and transition table (`device_lifecycle.dart:20-31`); bounded retry/backoff (`connection.dart:28-49`, `main.dart:40-44`); foreground/background rules (`main.dart:648-664`).
- `AndroidJobRunner` refusal default (`job_runner.dart:95-101`): unknown tools are refused, never executed — new tools are allowlist additions, not a new runner.
- Notification pull/ack/decide flow (`main.dart:324-335, 617-645`; `core/app.py:1332-1361, 1387-1411`); in-app approval cards remain the human surface.
- Voice capture→STT→turn→TTS→playback chain (§10); privileged work never rides the audio bytes — it rides jobs/grants.
- `files.transfer` relay semantics (sender/recipient binding, chunk order, SHA-256 both sides: `transfer.dart:99-131`, `main.dart:464-615`, `core/transfer.py:1-19`).
- `assistant.role` stays report-only (`DeviceBridge.kt:329-414`); ASSIST proxy stays open-app-and-finish (`ZaraAssistProxyActivity.kt:18-28`).

### 12.4 Minimal end-to-end shape of one privileged addition (example: `sms.send`)

1. Dart: `ZaraCapability('sms.send', true, …)` in `capabilities.dart:23-48` (flips from absent → supported with hardware evidence, per the file's own rule at `capabilities.dart:18`); risk `confirm` + `requires: [SEND_SMS]` in `capability_descriptors.dart:15-38`; `DeviceBridge.smsSend()` mirroring `audioCapture` (`device_bridge.dart:59-69`); `AndroidJobRunner` `case 'sms.send'` beside `job_runner.dart:81-94`.
2. Native: channel arm beside `DeviceBridge.kt:62-90` using `SmsManager`; manifest permission + `MainActivity` runtime request (`MainActivity.kt:41-53`); denial → typed error, availability `os_denied` (`capability_descriptors.dart:63-65`).
3. Core: `DEVICE_TOOL_DEFS` entry with `risk=CONFIRM` (`device_tools.py:25-56`); nothing else new — `authorize_capability` (`fabric.py:955`), `decide` (`policy.py:48`), governor (`governor.py:30`), `submit→approve` (`execution.py:26-68`), job poll/result (`app.py:1122-1150`), audit (`audit.py:17`) already gate it; without a live grant the resolver returns `requires_approval` and the phone shows the existing approval card (`main.dart:727-753`).
