# Zara privileged companion (LAB ONLY — never the physical phone)

One narrow priv-app (`dev.zara.privcompanion`) exposing a signature-guarded
`ContentProvider.call()` interface for operations an ordinary app can never
perform: today only Wi-Fi tethering (`TETHER_PRIVILEGED`). No network, no UI,
no other code. The main app binds with a short timeout and treats absence
as `deployment_blocked`.

## Why a second APK

`TETHER_PRIVILEGED` (like `FORCE_STOP_PACKAGES`) is grantable only to APKs
baked into `/system/priv-app`. No manifest entry, runtime prompt, or ADB
grant gives it to `dev.zara.zara_android`. A separate minimal APK keeps the
privilege surface to one provider + one permission instead of widening the
main app.

## Trust model

- Companion and main app must be signed with the SAME lab keystore, or the
  signature permission `dev.zara.permission.SYSCONTROL` is not granted and
  every call returns `applied:false` (`companion permission denied`).
- Provider manifest permission + in-code `checkCallingPermission()`.
- `applied:true` only after bounded post-action verification in the
  companion itself; the main app re-verifies via its own state read.

## Build (needs Android SDK + JDK; NOT run in this task — no SDK on host)

```bash
# 1. one-time lab keystore (keep out of the repo)
keytool -genkeypair -keystore lab.keystore -alias labkey \
  -keyalg RSA -keysize 2048 -validity 3650
# 2. build both APKs (companion module + main app) and sign with lab.keystore
bash scripts/build_privileged_lab.sh --keystore /path/to/lab.keystore
```

## Install (emulator/lab image only)

```bash
bash scripts/setup_privileged_lab.sh --companion /path/to/privcompanion.apk [--yes]
```

The script refuses non-emulator targets and `user` builds, pushes via
`adb root` + `remount` to `/system/priv-app/` **together with
`privapp-permissions-zara.xml` to `/system/etc/permissions/`** (without
the allowlist entry, system_server boot-loops — verified live), reboots,
and verifies the provider placement. Physical-device use is out of scope
by design.
