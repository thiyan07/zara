# Zara System-Control Support Matrix (Android + Linux)

Branch: `stage15-real-transfer`. Rule: a state change is reported `applied:true`
only after post-action verification re-reads the state. Opening a settings
screen is never success — results carry `settings_opened:true` plus a
`verify` pointer to the read tool that confirms the outcome.

## Deployment tiers (Android)

| Tier | Name | How obtained | Persists | Scope |
|------|------|--------------|----------|-------|
| T0 | Ordinary app | Play/sideload install | yes | Reads only (normal permissions). No silent toggles on API 29+. |
| T1 | ADB-granted dev | `adb shell pm grant <pkg> android.permission.WRITE_SECURE_SETTINGS` | across reboots, lost on uninstall | Direct airplane-mode toggle. No root, no unlock, no flash. |
| T2 | User-consent UI | OS dialog / Settings screen, same foreground session | per-action | Bluetooth enable dialog, Settings screens. Result verified after. |
| T3 | Privileged companion (lab) | Separate `dev.zara.privcompanion` priv-app on emulator/lab image (`android/privileged/` + `scripts/setup_privileged_lab.sh`); BOTH APKs signed with the SAME lab keystore or the signature permission is silently not granted | lab image only | Hotspot tether via a signature-guarded `ContentProvider.call()` interface (contract: `android/privileged/PROTOCOL.md`; no AIDL, no network). Never the physical phone. |
| T4 | Device Owner | Factory-reset QR provisioning | — | NOT implemented (invasive enrollment; user must opt in explicitly). |

## Per-action matrix (Android)

| Action | Read API (T0) | Direct change | Tier | Change mechanism |
|--------|---------------|---------------|------|------------------|
| `system.wifi.state` | `WifiManager` + `ConnectivityManager` (`ACCESS_WIFI_STATE`) | no (API 29+: `setWifiEnabled` deprecated, returns false) | T2 | `ACTION_WIFI_SETTINGS` + verify via `system.wifi.state`. API <29: direct attempt + verify. |
| `system.bluetooth.state` | `BluetoothManager`/`BluetoothAdapter` (needs `BLUETOOTH_CONNECT` on API 31+, else honest unavailable) | enable: no silent path | T2 | `ACTION_REQUEST_ENABLE` dialog / `ACTION_BLUETOOTH_SETTINGS` + verify. Disable: direct `disable()` attempt + verify where permission allows. |
| `system.hotspot.state` | no public API — honest `supported:false` + tether hint | no | T3/T2 | T3 companion `startTetheredHotspot` (lab only); otherwise tether-settings UI + verify. |
| `system.mobile_data.state` | `TelephonyManager.isDataEnabled` + cellular transport | no (`MODIFY_PHONE_STATE` is system-only) | T2 | Data-settings UI + verify. |
| `system.airplane_mode.state` | `Settings.Global.AIRPLANE_MODE_ON` (no permission) | yes, iff T1 granted | T1/T2 | T1: `Settings.Global.putInt` + `AIRPLANE_MODE_CHANGED` broadcast + verify. Otherwise settings UI + verify. |
| `system.app.open` | `PackageManager.getLaunchIntentForPackage` | n/a (launch, not a toggle) | T0 | Allowlist-mirrored (`APP_OPEN_TARGETS`); refusal otherwise. Needs `<queries>` entries, not `QUERY_ALL_PACKAGES`. |

## Per-action matrix (Linux agent)

| Tool | Read | Change mechanism | Verify |
|------|------|------------------|--------|
| `system.wifi.set` | `nmcli radio wifi` | `nmcli radio wifi on\|off` | re-read `nmcli radio wifi` |
| `system.bluetooth.set` | `bluetoothctl show` (`Powered:`) | `bluetoothctl power on\|off` | re-read `Powered:` |
| `system.hotspot.set` | active-conn scan for ap-mode profile | up/down existing hotspot profile only; refuses when none configured (never creates networks silently) | re-read active conns |
| `system.mobile_data.set` | `mmcli` modem list | enable/disable via ModemManager; `deployment_blocked` when no modem/`mmcli` absent | re-read modem state |
| `system.airplane_mode.set` | `rfkill list` | `rfkill block wifi bluetooth wwan` / `unblock`; permission errors reported, never raised | re-read `rfkill` |

All set tools: `CONFIRM` permission + `HIGH_RISK` risk, `{enabled: bool}`
schema, post-action verification, and an explicit self-stranding warning —
disabling wifi/airplane mode on the executing device can sever its Core
channel, so the approval card names that hazard.

## Why a separate privileged APK (T3) exists at all

`TETHER_PRIVILEGED` (and `FORCE_STOP_PACKAGES` before it) is grantable only
to priv-apps baked into the system image. No manifest entry, runtime prompt,
or ADB grant gives it to an ordinary app — hence the narrow companion
(`android/privileged/`, one provider, one signature permission, no network, no
other code) installable only on lab images via the setup script. The main app
binds with a short timeout and treats absence as `deployment_blocked`, never
as failure of the request itself.
