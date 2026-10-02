# Android Test Report (Stage 9)

## Automated

- Python `tests/test_android_stage9.py`: 15 tests — lifecycle, degraded
  thresholds (15/25), 404-reregister, revoke/rotate, pairing replay,
  jobs poll/claim/result/cancel/dup, notifications pull/ack scoping,
  approval approve/deny/scope/auto-notify, push lifecycle/dedup/secrets,
  events bound. All pass (full suite: 157 Python).
- Flutter: 45 tests (10 pre-existing + 35 new: connection, lifecycle,
  jobs, audio mocks, voice session, wake, battery, network, notifications,
  push, app state). `flutter analyze`: clean.

## Emulator (Pixel_8, API 16, 2026-10-03)

- APK builds clean (`app-debug.apk`, 181 MB debug incl. Dart VM).
- Cold start: `Displayed ... +2.9 s / +3.1 s`. Zero FATAL/zero unhandled
  in final build (one pairing→pairing crash found + fixed during testing).
- `getVoiceSupport`: mic-hw true, permission false, signal unknown,
  speaker-hw true, platform STT/TTS true, Zara engines honestly false.
- `audioSelfTest`: capture_api true, playback_api true (API path only).
- Real pairing from app UI (adb-typed code): `emu-body2` → online.
- Force-stop → relaunch: session restored, no re-pair.
- HOME → foreground: still online. No crash.
- `system.battery` dispatched from Core → emulator executed → verified
  result `{100%, charging}` (matched `dumpsys battery`).
- Operator revoke → app showed "Revoked — pair again" within one tick,
  keys wiped. Screenshot in session log.
- Heartbeat RTT (host loopback): ~1.2–1.6 ms.

## Live server (real HTTP, 127.0.0.1:8080)

enroll→claim→register→heartbeat→push→notifications→poll→sync all OK;
approval scoping enforced (403 cross-device).

## Not tested (need physical device)

Mic signal, speaker audibility, always-on DSP, permission dialogs,
FCM delivery, rotation sensor, metered radio, battery drain.

## Reproduce

```
python -m pytest tests/test_android_stage9.py -q
cd android && flutter test && flutter analyze
flutter build apk --debug
# live server: python3 zara_run.py (build_stack + uvicorn :8080)
adb -s emulator-XXXX install -r build/app/outputs/flutter-apk/app-debug.apk
adb -s emulator-XXXX shell am start -n dev.zara.zara_android/.MainActivity
adb -s emulator-XXXX logcat -d | grep -E "zara:voice-support|zara:audio-selftest"
# pair: enroll via POST /v1/agent/enroll, type code into app, tap Connect
```

## Physical-device preparation (PHYSICAL_DEVICE_VALIDATION_PENDING)

```
adb devices                                  # expect a real device, not emulator-XXXX
adb install -r app-debug.apk
adb shell am start -n dev.zara.zara_android/.MainActivity
adb logcat -d | grep -E "zara:"               # flags + self-test + errors
# pairing: same enroll/type/Connect flow; server must be reachable
#   from the phone (host LAN IP, not 10.0.2.2 — rebuild or make configurable)
adb shell dumpsys battery                     # cross-check reported pct
adb shell input keyevent 3                    # HOME: background test
```
Note: `CoreClient` base URL is currently hardcoded to `10.0.2.2`
(emulator→host). Physical testing needs it configurable (setting/flag);
do NOT ship a LAN IP in source.
