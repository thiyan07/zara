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

~~Mic signal, speaker audibility, always-on DSP, permission dialogs,
FCM delivery, rotation sensor, metered radio, battery drain.~~
See "Physical device (vivo V2338, 2026-10-04)" below. Still open:
human speaker audibility, live "Hey Zara" wake, acoustic barge-in,
low/critical-battery governor, FCM delivery (no credentials).

## Physical device (vivo V2338, Android 16 / API 36, OriginOS, 2026-10-04)

`adb devices` → `10BEC514NJ006PQ device`. Model V2338, vivo, release 16,
SDK 36, 1080x2408. Prior default assistant = Google GSA.

- Assistant: `ZaraVoiceInteractionService` + session service declared with
  `BIND_VOICE_INTERACTION`; own descriptor parses (infoValid/supportsAssist).
  `ZaraAssistProxyActivity` (plain Activity, ACTION_ASSIST) got Zara listed
  in the OS chooser; selected via Settings UI. OS confirms:
  `settings get secure assistant` =
  `dev.zara.zara_android/.ZaraAssistProxyActivity`, role holder =
  `dev.zara.zara_android`. HOME long-press from Chrome → Zara MainActivity
  (PHYSICAL_VERIFIED). Corner swipe: NOT_SUPPORTED (Jovi owns it; Chrome
  stayed focused). VoiceInteractionSession: NOT_SUPPORTED (dumpsys =
  "No active implementation"; OriginOS never binds third-party sessions).
- Auth: fresh install + `install -r` reinstall both restore from Keystore
  (online, no re-pair). Revoke → 403 → pair-again. Core restart → 403 →
  pair-again. Re-pair via revoke-then-enroll (re-enroll while pending
  correctly refuses; claim of stale code correctly 403s).
- Mic: RECORD_AUDIO granted; 5 s captures of exactly 160044 B; peaks
  −13.7/−18.1/−19.6 dBFS; faster-whisper transcribed live speech
  ("Hello, how are you?"); empty-room capture → honest empty transcript.
  Core `/v1/agent/voice/turn` + `/v1/agent/tts` (Piper, 520–528 kB).
- Speaker: `USAGE_ASSISTANT`+`MODE_STATIC` failed init on OriginOS; fixed
  to `USAGE_MEDIA`+`MODE_STREAM` → `{played: true, 524288 B, 22050 Hz,
  audibility: manual-only}`. Software leg VERIFIED; human audibility
  VERIFIED 2026-10-04 (user heard Piper reply; screenshot h1).
- Approvals: `shell.safe_readonly` confirm-gate → card on phone →
  Approve resolved through Core (`permission_granted`; device allowlist
  refused execution — nothing ran; job later expired while app
  backgrounded, execution recorded failure honestly) → second decide
  honestly 409. Deny → execution cancelled, nothing ran. Device without
  credentials → rejected. Stale-card 409 auto-dismiss added
  (`isStaleApproval` + Dart test) and verified on device.
- Resilience: Core down → "Reconnecting…", no fake online; restart →
  re-pair recovery. Rotation both ways: state kept, still online.
- logcat on this OEM returns nothing (even own PID); all device evidence
  came from UI screenshots + Core audit (`/v1/audit`) + OS settings dumps.
- Appium binary present (`~/.npm-global/bin/appium`) but NOT_USED;
  adb + screenshots sufficed.

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
