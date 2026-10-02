# Zara Stage 6 — Todo (emulator-first validation)

Baseline: HEAD b33f85a clean, 100/100 Python, 10/10 Flutter, analyze clean,
20GB free. APK: single newest debug. Emulator: BOOTED (see below).

Emulator fingerprint:
- serial: emulator-5554
- Android 16, API 36, x86_64, sdk_gphone64_x86_64, 1080x2400
- package: dev.zara.zara_android

- [x] Forensic baseline
- [x] APK located (single newest)
- [x] Emulator booted
- [x] APK install + launch smoke test
- [x] Backend on 0.0.0.0:8080 + emulator→host (10.0.2.2) check
- [x] Enrollment/claim/register/heartbeat from real app flow
- [x] Identity: rotation/revocation live
- [x] Heartbeat/presence + offline/reconnect
- [x] Battery reporting vs dumpsys (NORMAL/LOW/CRITICAL sim)
- [x] Capabilities truthfulness check
- [x] Safe command path (battery query E2E)
- [x] Event/WS path from emulator
- [x] Approval flow via emulator-registered device
- [x] MCP safe + malicious via emulator device context
- [x] Browser/OpenCode regression (backend-level, unchanged code)
- [x] Secure storage lifecycle (reinstall + re-auth)
- [x] Permissions truthfulness
- [x] Voice/wake non-hardware checks (state/UI/gating only)
- [x] Logcat secret audit
- [x] Crash/recovery (app kill, backend restart, emulator reboot if cheap)
- [x] Rate limit + security regression (full suite)
- [x] Performance measures vs Stage 5
- [x] Docs STAGE6_STATUS/EMULATOR_REPORT, README/TODO, cleanup, commit+report
