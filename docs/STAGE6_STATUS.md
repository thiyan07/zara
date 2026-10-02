# Stage 6 Status (emulator-first validation — same Zara)

## Baseline

- Commit `b33f85a`, clean tree; Python 100/100; Flutter 10/10, analyze clean.
- Disk 20 GB free start; 20 GB end. No framework added.

## Emulator (EMULATOR TESTED, not physical)

- Serial `emulator-5554`, Android 16, API 36, x86_64, sdk_gphone64_x86_64,
  1080x2400, Pixel_8 AVD, KVM accel, headless + swiftshader.
- Package `dev.zara.zara_android`, newest debug APK installed, launched,
  MainActivity resumed, no fatal exceptions, UI renders (screenshots kept
  out of repo; verified by inspection).

## Bugs found by emulator validation (all fixed + tested)

1. Missing `INTERNET`/`ACCESS_NETWORK_STATE` — app could never reach the
   backend and network state read failed. Added to manifest (normal perms).
2. Stale battery/network: heartbeat reused launch-time values. `_beat()`
   now re-reads the bridge every 30 s tick.
3. `BatteryManager.getIntProperty(CAPACITY)` ignores emulator overrides;
   switched to sticky `ACTION_BATTERY_CHANGED` extras (also more portable).
4. Kotlin compile error (`applicationContext` out of scope) — fixed.
5. `degraded` presence never cleared on recovery — heartbeat now restores
   `online` at ≥25% (regression test added).
6. App never restored Keystore session after kill — `_restore()` added:
   auto re-register + heartbeat; revoked/restarted backend → honest
   "session expired — pair again", no retry loop.

## Still BLOCKED (no hardware/engine)

Physical phone · real mic/speaker audio · real wake detection · real
battery drain · Bluetooth · hosted LLM · Docker/Oracle. Emulator results
are never presented as any of the above.
