# Android Status (truth source for the mobile body)

Labels: VERIFIED (unit/integration) · EMULATOR_VERIFIED · LIVE_SERVER_VERIFIED
(live Core over real HTTP) · PHYSICAL_VALIDATION_PENDING · DEFERRED.

## Lifecycle / identity

| Item | Status |
|---|---|
| enroll→claim→register→heartbeat→disconnect | LIVE_SERVER_VERIFIED (real HTTP + TestClient) |
| Real app pairing on emulator (`emu-body2` → online) | EMULATOR_VERIFIED 2026-10-03 |
| Keystore restore after force-stop (no re-pair) | EMULATOR_VERIFIED |
| Revoke → 403 → keys wiped → "Revoked — pair again" | EMULATOR_VERIFIED (screenshot) |
| Rotation kills old key; replay of pairing code refused | VERIFIED |
| Backend restart → 404 → re-register | VERIFIED |
| Logout clears creds | VERIFIED |

## Connection / jobs / approvals / notifications / push

| Item | Status |
|---|---|
| Bounded retry (5, then offline), battery-critical no auto-retry | VERIFIED |
| Bounded offline queue, heartbeat coalescing, stale-response drops | VERIFIED |
| End-to-end device job: Core dispatch → emulator `system.battery` → verified result (100%, charging) | EMULATOR_VERIFIED |
| Unknown tools refused (incl. shell/URL shapes); dup jobs refused | VERIFIED |
| Approval hold auto-notifies target device; device approves/denies own executions; foreign approval 403 | VERIFIED |
| Notification pull/ack scoping; secret scrub; dedup | VERIFIED |
| Push register/rotate/invalidate/dedup; payload has IDs only | VERIFIED |
| Real FCM delivery | DEFERRED (no credentials; mock transport + poll fallback) |

## Voice runtime

| Item | Status |
|---|---|
| Session states + limits 5/300 s/60 s, battery/offline gating | VERIFIED |
| "Hey Zara" exact; VAD-gated; battery pause; no authority | VERIFIED |
| Native capture/playback API paths (`audioSelfTest`: both true) | EMULATOR_VERIFIED |
| Mic signal (peak dBFS + live STT, incl. "Hey Zara" phrase) / speaker software playback + human audibility | PHYSICAL_VERIFIED 2026-10-04 (vivo V2338; user heard Piper reply; screenshot h1) |
| Barge-in/cancel paths (mock audio, Magpie RPC in Stage 8) | VERIFIED (software) |

## Battery / network / UI / permissions

Battery levels follow Core thresholds; charging override; background
thrift advisory-only — VERIFIED. Network states + truthful offline UX —
VERIFIED. UI reflects connection/lifecycle/battery/voice/approval/error,
Wrap-fixed overflow, survives recreation via Core restore —
EMULATOR_VERIFIED. Permissions: INTERNET/ACCESS_NETWORK_STATE (install),
RECORD_AUDIO + POST_NOTIFICATIONS (runtime, denial = normal state) —
manifest + flow VERIFIED, grant-dialog UX PHYSICAL_VALIDATION_PENDING.

## Blocked / deferred

Physical Android device (`adb` had none): mic signal, audibility, DSP
wake, grant dialogs, FCM delivery, rotation sensor, radio behavior —
all PHYSICAL_VALIDATION_PENDING. Nothing in this file claims them.

## Stage 10 physical validation (vivo V2338, Android 16, API 36, 2026-10-04)

Device: vivo V2338 (manufacturer vivo, OriginOS), Android 16 / SDK 36,
1080x2408, 440dpi (override 396). Baseline screen_off_timeout raised to
10 min for testing only. Prior default assistant:
`com.google.android.googlequicksearchbox/...GsaVoiceInteractionService`.

| Item | Status |
|---|---|
| Assistant service registered (PackageManager resolves both services; own descriptor parses: infoValid, supportsAssist) | PHYSICAL_VERIFIED |
| ACTION_ASSIST proxy activity → Zara listed in OS default-assistant chooser | PHYSICAL_VERIFIED (screenshot da1 + OS setting) |
| Zara selected as default assistant via Settings UI (`settings get secure assistant` = `dev.zara.zara_android/.ZaraAssistProxyActivity`; role holder = `dev.zara.zara_android`) | PHYSICAL_VERIFIED |
| HOME long-press from Chrome → Zara MainActivity (ASSIST intent via proxy) | PHYSICAL_VERIFIED |
| Corner-swipe gesture → Zara | NOT_SUPPORTED on this OEM (Chrome stayed focused; Jovi owns corner swipe). No workaround attempted. |
| VoiceInteractionSession show / SHOW_SOURCE_ASSIST_GESTURE | NOT_SUPPORTED on this OEM (`dumpsys voiceinteraction` = "No active implementation"; OriginOS dispatches assist via ASSIST activity, never binds third-party VoiceInteractionService) |
| Fresh install → enroll → claim → register → heartbeat → online (`vivo-real`) | PHYSICAL_VERIFIED |
| Keystore restore after force-stop AND after `install -r` reinstall (no re-pair) | PHYSICAL_VERIFIED |
| Revoke → 403 → "Revoked — pair again" safe state | PHYSICAL_VERIFIED |
| Core restart (fresh registry) → 403 → pair-again (no crash, no bypass) | PHYSICAL_VERIFIED |
| Re-pair after restart (revoke stale enroll → enroll → claim → online) | PHYSICAL_VERIFIED |
| Mic RECORD_AUDIO granted (permission true); 5 s bounded capture 160044 B; peak −13.7/−18.1/−19.6 dBFS (non-zero signal); faster-whisper STT transcribed live speech ("Hello, how are you?"); Core voice turn + Piper TTS fetch (520–528 kB) | PHYSICAL_VERIFIED |
| Speaker: AudioTrack `USAGE_ASSISTANT` + `MODE_STATIC` init FAILED on OriginOS → fixed to `USAGE_MEDIA` + `MODE_STREAM`; playback exit `{played: true, 524288 B, 22050 Hz, audibility: manual-only}` | PHYSICAL_VERIFIED (software leg + human audibility: user heard Piper reply 2026-10-04, screenshot h1) |
| Speaker human audibility | PHYSICAL_HUMAN_AUDIBILITY_PENDING (needs ears on device) |
| Approval card on phone for `shell.safe_readonly` confirm-gate; Approve → Core `permission_granted` → proxy job → device allowlist refused (never executed); double-decide → honest 409 | PHYSICAL_VERIFIED |
| Deny on phone → Core execution cancelled, nothing ran | PHYSICAL_VERIFIED |
| Stale-card auto-dismiss on 409 (`isStaleApproval` + test) | PHYSICAL_VERIFIED (screenshot stale2) |
| Device without credentials calling decide → rejected | PHYSICAL_VERIFIED |
| Core down → truthful "Reconnecting…" (no crash, no fake online) | PHYSICAL_VERIFIED |
| Rotation portrait→landscape→portrait: state + connection preserved, no crash | PHYSICAL_VERIFIED (screenshot land) |
| "Hey Zara" live wake phrase / DSP / acoustic barge-in / low-battery governor / FCM delivery | WAKE_PHRASE PHYSICAL_VERIFIED ("Heard: Hey Zara, how are you?", screenshot h1; wake evaluator emits state only by design); DSP NOT_SUPPORTED; acoustic barge-in + low-battery + FCM PENDING (battery stayed 73–83% healthy; no FCM credentials — poll fallback is the channel) |
| Appium | AVAILABLE (binary present) but NOT_USED — adb + screenshots sufficed; nothing claimed via Appium |
