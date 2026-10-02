# Android Architecture (`android/` — Flutter + Kotlin)

## Layout

- `lib/main.dart` — Zara shell UI: status, pairing-code claim, register,
  battery/network cards, capability list. No voice UI yet.
- `lib/core_client.dart` — core REST (dart:io, zero third-party deps):
  claim/register/heartbeat. Key kept in memory (secure storage: Stage 3).
- `lib/device_bridge.dart` — the ONLY Flutter→native path, one
  `zara/device` MethodChannel.
- `lib/capabilities.dart` — capability table (pure Dart, unit-tested).
- `android/.../DeviceBridge.kt` — all Android API access:
  `getBattery` (BatteryManager + power-save), `getNetwork`
  (ConnectivityManager, metered detection), `getPermissions` (state reporting
  only — nothing requested in Stage 2).
- `MainActivity.kt` — attaches the bridge; nothing else.

## Rules

- No Android code scattered in Flutter outside `device_bridge.dart`.
- Unimplemented → `supported=false`, never faked.
- Advertised to core = supported ones only.
- `voice.wake_word` reserved with the exact phrase **"Hey Zara"**; the wake
  engine itself is NOT built in this checkpoint.
- Battery-first: 30 s heartbeat, no polling loops, no background inference,
  heavy work routes to laptop/cloud via core router.

## Limitations (Stage 2)

No mic/capture in UI, no FCM push yet, `http://10.0.2.2:8080` dev URL for the
emulator (TLS + production URL in later work), key in memory only.

## Verify

`flutter analyze` (clean), `flutter test` (3 capability-contract tests),
`flutter build apk --debug` (validates Kotlin bridge compiles).
No physical-device testing claimed — emulator/physical validation is later.
