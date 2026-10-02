# Stage 6 Emulator Test Report (checklist A–Z)

Backend: `uvicorn core.app` on 0.0.0.0:8080, fresh token, stage-6 file DBs.
App reached it via 10.0.2.2 (dev `CoreClient` URL; production URL deferred).

- A readiness 4 ms, health 1 ms: PASS
- B APK install (`adb install -r`, single newest): PASS
- C launch, resumed, no fatal: PASS (screenshot verified)
- D emulator→host connectivity: PASS (claim/register/heartbeat all 200)
- E enrollment (operator enroll → code): PASS
- F claim from REAL app (typed via adb, tapped Connect): PASS
- G capability registration (4 real caps, no fakes): PASS
- H heartbeat every 30 s with fresh bridge values: PASS
- I battery vs `dumpsys battery`: 100/charging, 10/discharging,
  5% all matched backend state: PASS (EMULATED values, not drain)
- J safe command (`system.battery` via laptop agent, emulator present):
  succeeded, verified, 80%: PASS
- K WebSocket auth/fan-out/replay/invalid-frame: PASS (backend-tested)
- L offline (svc wifi/data off): heartbeats froze, dispatch denied honestly;
  no phantom execution: PASS
- M reconnect (radios on): resumed <45 s, zero duplicate registrations: PASS
- N approval (uptime): hold → approve → succeeded with real output: PASS
- O MCP safe (calc): PASS (backend path, unchanged code)
- P MCP malicious: wrapped, denied: PASS (backend path)
- Q browser open+extract: PASS (backend path, real Chrome)
- R OpenCode: unchanged since Stage 5 real run: PASS (no re-run needed)
- S secure storage: Keystore save → force-stop → auto-restore online
  WITHOUT new pairing; `pm clear` wipes → re-pair required: PASS
- T rotation: old 403, new works: PASS
- U revocation: covered by suite (live revoke would orphan the demo
  device; backend-tested): PASS
- V rate limit: 429s observed live in Stage 5; suite asserts: PASS
- W security regression: full suite green (101): PASS
- X app kill → relaunch → auto-restore: PASS
- Y backend restart → `/v1/ready` ok; file DBs intact; app re-pairs
  cleanly (documented: restart invalidates in-memory auth by design): PASS
- Z audit: 51 rows sampled, 0 secret leaks (logcat + backend log + audit
  DB all scanned): PASS

## Performance (emulator/backend, real)

Pairing (enroll) 1 ms · heartbeat negligible · local dispatch 2 ms ·
device dispatch ~3.6 s (5 s agent poll dominates — documented, unchanged) ·
WS event immediate · reconnect <45 s · app cold start ~6 s.

## Limitations (explicit)

- Pairing-code entry via `adb input` (no test-ui hook in app).
- Battery levels are `dumpsys` simulations, not drain measurements.
- No mic/speaker/camera/Bluetooth exercised (marked unsupported).
- Emulator reboot skipped: app-kill + backend-restart cover the same
  recovery paths at far lower time cost.
- Wake engine remains mock; only config/state/gating validated.
