# Android Architecture — Zara's mobile body

The Android app is a **body** of the SAME Zara: one identity, one memory,
one mission state, one permission system, one tool registry, one policy,
one governor, one brain. The phone captures I/O, reports state, and runs
Core-dispatched allowlisted jobs. It never reasons, never authorizes, never
executes anything Core did not dispatch.

```
Android mic ──bounded WAV──▶ Core STT ──▶ Zara Core loop (policy/governor/
  router/approval) ──▶ NVIDIA LLM (proposal only) ──▶ verified result ──▶
  TTS (Piper default / Magpie optional) ──▶ Android speaker
```

## Dart layers (`android/lib/`)

| File | Owns | Authority |
|---|---|---|
| `device_lifecycle.dart` | pairing→enrolled→registering→online→degraded→reconnecting; revoked/logged-out/error | none; transitions only |
| `connection.dart` | `CoreLinkState`, `RetryPolicy` (5 attempts, then offline), `PendingQueue` (bounded 50, heartbeats coalesce), `ResponseGuard` (stale drops) | none |
| `core_client.dart` | REST calls, 15 s timeouts, typed `AuthException`/`NotRegisteredException`, `CancelScope` | none |
| `secure_store.dart` | device key in Keystore-backed storage; in-memory fallback labeled | holds secret, never logs it |
| `job_runner.dart` | allowlist `{system.battery, system.network}`; unknown tools REFUSED; dup IDs refused; cancel honored | executes ONLY allowlisted reads |
| `audio_io.dart` | `MicCapture`/`SpeakerOutput` interfaces + mocks; status split hardware/permission/opened/signal | none |
| `voice_session.dart` | `AndroidVoiceSession`: core states + `paused_battery`/`offline`; limits 5/300 s/60 s (same as Core) | none; mirrors Core |
| `wake.dart` | `WakeController`: exact "Hey Zara" behind VAD gate + battery policy; returns state requests | none — no tool/policy path exists |
| `battery_governor.dart` | levels from Core thresholds (critical<15, low<25, caution<30); charging overrides | advisory only |
| `network_monitor.dart` | connected/metered/disconnected/reconnecting/unavailable; truthful strings | none |
| `zara_notifications.dart` | channels, secret scrub, dedup, approve/deny → Core endpoint calls | none — no local execution path |
| `push.dart` | `PushProvider` + `MockPushTransport`; token rules mirror server; poll is the reliable channel | none |
| `app_state.dart` | `ZaraUiState`: presentation only; restore map holds device ID, never secrets | none |
| `capabilities.dart` | advertised set (see STATUS); voice engines stay `false` | declaration only |

## Native (`DeviceBridge.kt`, `MainActivity.kt`)

Single `zara/device` MethodChannel. Native owns: battery (pct/charging/
power-save/source/health), network (transport/metered), permissions state,
voice-support flags, bounded `AudioRecord` capture (16 kHz mono → WAV,
≤30 s, thread-confined, no persistence), `AudioTrack` playback (focus,
bounded wait, completion ≠ audibility), runtime permission requests
(Activity-mediated, denial is normal state), notification channels,
`audioSelfTest` (API-path proof without permission or audio).

## Core endpoints used (all pre-existing protocol + Stage-9 additions)

enroll / claim / rotate / register / heartbeat / capabilities / jobs
poll+result / events sync / disconnect / revoke — plus Stage 9:
notifications pull+ack, push register+invalidate, device-scoped approvals
(own-device executions only; cross-device stays operator-only), and
approval holds auto-notify the target device with its execution ID.

## What Android never does

Bypass policy/governor/approval; run LLM-proposed commands; pick URLs;
exec anything (`Runtime.exec`/`ProcessBuilder` absent); treat model output
as instructions; persist audio/transcripts; log keys/tokens/secrets.
