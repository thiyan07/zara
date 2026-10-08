# Privileged Capability Matrix — Assistant Body App (Android/AOSP)

- Date (UTC): 2026-10-06
- Scope: read-only research. No code changes, no device modification, no downloads.
- Method: established AOSP behavior only — `DevicePolicyManager` APIs, AppOps,
  permission `protectionLevel`s (`normal` / `dangerous` / `signature` /
  `signature|privileged` / `signature|privileged|development` / `internal` /
  `system-UID-only`), privileged-permission allowlisting
  (`etc/permissions/privapp-permissions-*.xml`), system-UID-only APIs,
  SELinux (never fully bypassed except by custom OS policy).
- How to read: each cell is one of `AVAILABLE` / `PARTIAL (condition)` /
  `NOT AVAILABLE`, followed by a one-line mechanism or restriction.
- Conservatism rule applied: when in doubt the cell is `PARTIAL` or `UNKNOWN`
  with the reason stated; no cell is upgraded without evidence.
- Legend for evidence tags used in §4:
  `FACT` = stable, widely documented AOSP behavior;
  `INFERENCE` = reasoned from protection levels / service gating, version-sensitive;
  `UNKNOWN` = version/OEM-dependent, verify on target build.

## 1. Tier definitions (what each column means)

| Tier | Meaning |
|---|---|
| NORMAL APP | Regular Play-installed app. Sandbox UID, scoped storage, runtime permissions, AppOps-gated sensors/mic/camera, background-start/FGS restrictions. |
| ACCESSIBILITY | Normal app + user-enabled `AccessibilityService` (`BIND_ACCESSIBILITY_SERVICE`, `canRetrieveWindowContent`, `canPerformGestures`). Adds UI tree + gesture injection + a few `GLOBAL_ACTION_*`; adds no file, policy, or signature bypass. |
| DEVICE OWNER (DO) | Device-owner provisioned via QR/NFC/`dpm` during setup (`DevicePolicyManager` active owner). Adds `setPermissionGrantState`, `setPackagesSuspended`, `setApplicationHidden`, delegated install/uninstall scopes, `lockNow`, user restrictions, managed configs. Does NOT grant signature permissions or silent framebuffer/input/file bypass. |
| PRIVILEGED APP | Preinstalled in `/system/priv-app` (or equivalent) + explicitly allowlisted in `privapp-permissions-*.xml`. Can hold `signature|privileged` permissions (e.g. `BATTERY_STATS`, `INSTALL_PACKAGES`, `BLUETOOTH_PRIVILEGED`, `WRITE_SECURE_SETTINGS` where allowlisted). Cannot hold pure-`signature` permissions (e.g. `INJECT_EVENTS`, `READ_FRAME_BUFFER`, `TETHER_PRIVILEGED`, `PREVENT_POWER_KEY`, `DEVICE_POWER`) unless it is also platform-signed. |
| PLATFORM-SIGNED SYSTEM APP | System app signed with the platform certificate (same signer as `android` package / `system_server`). Can hold pure-`signature` permissions (`INJECT_EVENTS`, `READ_FRAME_BUFFER`, `MODIFY_AUDIO_ROUTING`, `CONTROL_KEYGUARD`, `MANAGE_USB` where applicable). Still subject to SELinux domain + API gating; not the same as running as system UID. |
| SYSTEM SERVICE | Code running inside `system_server` (system UID 1000) or as a bound system service (`ActivityManagerService`, `InputManagerService`, `NotificationManagerService`, `DevicePolicyManagerService`, `DisplayManagerService`, `SensorService`, `AudioService`, `UsbService`). Direct service-internal calls; still constrained by SELinux but not by permission-grant UX. |
| ROOT / CUSTOM OS | uid 0 (`su`) and/or custom-built OS image (own platform keys, own SELinux policy, own `system_server` patches). DAC bypass; SELinux bypass only if policy made permissive or rewritten in the custom build. `dpm`/`pm`/`am`/`input`/`screencap` shell paths available. |

## 2. Matrix

| Capability | NORMAL APP | ACCESSIBILITY | DEVICE OWNER | PRIVILEGED APP | PLATFORM-SIGNED SYSTEM APP | SYSTEM SERVICE | ROOT / CUSTOM OS |
|---|---|---|---|---|---|---|---|
| launch apps | AVAILABLE — `startActivity` launcher intent / `PackageManager.getLaunchIntentForPackage`. | AVAILABLE — same intents + can drive launcher via gestures. | AVAILABLE — same intents; `setLockTaskPackages` only affects kiosk launch scope. | AVAILABLE — same intents; no extra gate. | AVAILABLE — same intents + system-context starts. | AVAILABLE — `ActivityTaskManagerInternal` / `startActivityAsUser`. | AVAILABLE — `am start` / same intents. |
| stop / force-stop apps | NOT AVAILABLE — no force-stop API; `killBackgroundProcesses` only kills background processes. | PARTIAL (UI automation only) — can click Settings App-Info Force-Stop button; brittle, no programmatic API. | PARTIAL (no true force-stop API) — `setPackagesSuspended` / `setApplicationHidden` / `clearApplicationUserData`; `ActivityManager.forceStopPackage` is `signature`\|system, not exposed via `DevicePolicyManager`. | AVAILABLE (if allowlisted) — `FORCE_STOP_PACKAGES` (`signature\|privileged`) → `ActivityManager.forceStopPackage`. | AVAILABLE — `FORCE_STOP_PACKAGES` with platform cert → `ActivityManagerService.forceStopPackage`. | AVAILABLE — `ActivityManagerService.forceStopPackage` direct. | AVAILABLE — `am force-stop <pkg>`. |
| inspect UI tree | NOT AVAILABLE — no cross-app view read. | AVAILABLE — `AccessibilityService` + `canRetrieveWindowContent` → `AccessibilityNodeInfo` tree. | NOT AVAILABLE — DO status adds no view-tree bypass. | NOT AVAILABLE — privileged status alone adds no view-tree bypass (still needs accessibility/shell). | NOT AVAILABLE — platform cert alone adds no view-tree bypass (needs accessibility or service internals). | AVAILABLE — `AccessibilityManagerService` / `WindowManager` internals. | AVAILABLE — UiAutomator / `uiautomator dump` / accessibility backdoor via shell. |
| inject UI actions | NOT AVAILABLE — only in-process instrumentation of own app. | AVAILABLE — `performAction` + `dispatchGesture` (API 24+, `canPerformGestures`); rate/gating limits apply. | NOT AVAILABLE — no `DevicePolicyManager` input-injection API. | NOT AVAILABLE — `INJECT_EVENTS` is pure-`signature`; privileged allowlist alone is insufficient. | AVAILABLE — `INJECT_EVENTS` (platform cert) → `InputManager.injectInputEvent`. | AVAILABLE — `InputManagerService.injectInputEvent` direct. | AVAILABLE — `input tap/swipe/keyevent` / `/dev/input` / uinput. |
| notifications read | PARTIAL (user opt-in) — `NotificationListenerService` (`BIND_NOTIFICATION_LISTENER_SERVICE`) requires explicit Settings approval. | PARTIAL (limited) — `TYPE_NOTIFICATION_STATE_CHANGED` exposes partial content; full read still needs approved listener. | PARTIAL (same as normal) — no `DevicePolicyManager` bypass of listener approval. | PARTIAL (same as normal) — still needs user-approved listener binding. | PARTIAL (same gate) — signature does not bypass listener approval (except built-in SystemUI path). | AVAILABLE — `NotificationManagerService` direct. | AVAILABLE — approved listener pre-granted + service state read via shell. |
| notification actions | PARTIAL (gated) — listener can invoke exposed `PendingIntent` / `RemoteInput` only. | PARTIAL (UI click) — can click posted notification node; inline-reply/expanded actions unreliable. | PARTIAL (same as normal) — no `DevicePolicyManager` action-invoke API. | PARTIAL (same as normal) — no privileged bypass of action gating. | PARTIAL (same gate) — except SystemUI/System path which drives `PendingIntent` directly. | AVAILABLE — `NotificationManagerService` / `StatusBarManagerService` invoke path. | AVAILABLE — listener + `cmd statusbar` / shell-driven intents (notification must expose the action). |
| files outside sandbox | PARTIAL (user-mediated) — MediaStore / SAF picker only; no raw `/data/data/<other>` or `/data/system`. | PARTIAL (same as normal) — accessibility adds no file bypass. | PARTIAL (policy, not read) — can enforce encryption/restrictions; cannot read other apps' private dirs. | PARTIAL (still scoped) — scoped-storage + SELinux still apply; no blanket bypass. | PARTIAL (permission-scoped) — broader only via specific system permissions/UIDs/GIDs within SELinux domain. | PARTIAL (domain-scoped) — `system_server` is still SELinux-confined, not universal file read. | AVAILABLE — uid 0 bypasses DAC; full bypass additionally needs custom SELinux policy. |
| shared storage write | PARTIAL (scoped) — MediaStore + `WRITE_EXTERNAL_STORAGE` / `MANAGE_EXTERNAL_STORAGE` all-files opt-in via Settings. | PARTIAL (same as normal) — accessibility adds no storage bypass. | PARTIAL (same as normal) — can restrict via user restrictions, not bypass scoped storage. | PARTIAL (broad but mediated) — `MANAGE_EXTERNAL_STORAGE` / `WRITE_MEDIA_STORAGE` if allowlisted; still via `MediaProvider`. | PARTIAL (broad but mediated) — system permissions widen scope; still via `MediaProvider`/`vold` paths. | AVAILABLE — `MediaProvider` / `vold` / `StorageManagerService` path. | AVAILABLE — direct filesystem write as uid 0 (SELinux policy permitting / custom OS). |
| microphone background | PARTIAL (gated) — `RECORD_AUDIO` runtime + mic-FGS type + background-start limits + mandatory mic indicator (12+). | PARTIAL (same as normal) — accessibility adds no mic bypass. | PARTIAL (same as normal) — can set permission policy, cannot grant silent background mic. | PARTIAL (still AppOps-gated) — `CAPTURE_AUDIO_OUTPUT` (`signature\|privileged`) covers internal audio, not mic bypass. | PARTIAL (still gated) — `CAPTURE_AUDIO_OUTPUT` / `CAPTURE_VOICE_COMMUNICATION_OUTPUT` cover internal/voice paths; mic background still AppOps/FGS-gated. | AVAILABLE — `AudioService` / HotwordDetection privileged path. | AVAILABLE — audio HAL / `AudioService` bypass via root/custom OS (indicator suppression needs custom OS). |
| camera background | PARTIAL (gated) — `CAMERA` runtime + camera-FGS type + background-start block + mandatory camera indicator (12+). | PARTIAL (same as normal) — accessibility adds no camera bypass. | PARTIAL (same as normal) — `setCameraDisabled` can prohibit, not grant silent background camera. | PARTIAL (same as normal) — no privileged bypass of background-camera block. | PARTIAL (same gate) — except built-in system camera / face-auth path. | AVAILABLE — `CameraService` privileged-client path. | AVAILABLE — camera HAL / service bypass via root/custom OS (indicator suppression needs custom OS). |
| screen capture / screenshot | PARTIAL (per-session consent) — MediaProjection requires each-time user dialog + FGS; no silent capture. | PARTIAL (rate-limited) — `AccessibilityService.takeScreenshot` (API 30+) needs enabled service; window-scoped, throttled. | PARTIAL (can only prohibit) — `setScreenCaptureDisabled` blocks capture; does not grant silent capture. | NOT AVAILABLE (silent) — `READ_FRAME_BUFFER` / `CAPTURE_VIDEO_OUTPUT` / `CAPTURE_SECURE_VIDEO_OUTPUT` are pure-`signature`; privileged allowlist insufficient, must use MediaProjection. | AVAILABLE — `READ_FRAME_BUFFER` (platform cert) → `SurfaceControl.screenshot`. | AVAILABLE — `SurfaceFlinger` / `WindowManager` screenshot path. | AVAILABLE — `screencap` / `screenrecord` / framebuffer read. |
| audio routing | PARTIAL (own streams) — `AudioManager` speaker/focus for own playback; no forced global route. | PARTIAL (same as normal) — gestures can open sound settings; no routing API. | PARTIAL (same as normal) — no `DevicePolicyManager` routing API. | PARTIAL (version-dependent) — `MODIFY_AUDIO_ROUTING` allowlist may apply on some builds; not portable, verify per build. | AVAILABLE — `MODIFY_AUDIO_ROUTING` (platform cert) → `AudioService.setPreferredDevice` / routing policy. | AVAILABLE — `AudioService` routing control direct. | AVAILABLE — `AudioService` / HAL routing via shell/config (custom OS for persistent policy). |
| network state | AVAILABLE — `ACCESS_NETWORK_STATE` (`normal`) + `ConnectivityManager` / `NetworkCapabilities`. | AVAILABLE — same `ConnectivityManager` read path. | AVAILABLE — same read path. | AVAILABLE — same read path. | AVAILABLE — same read path + system-network callbacks. | AVAILABLE — `ConnectivityService` direct. | AVAILABLE — same + `dumpsys connectivity` / netd state. |
| Wi-Fi control | PARTIAL (suggest, not toggle) — `WifiNetworkSuggestion` + user approval; `setWifiEnabled` blocked for non-system since 10. | PARTIAL (UI automation) — can toggle Settings Wi-Fi switch via gestures; brittle. | PARTIAL (configure + restrict) — provision Wi-Fi configs on older APIs / `DISALLOW_CONFIG_WIFI`; on 10+ cannot force-toggle radio programmatically. | PARTIAL (allowlist/version-dependent) — `CHANGE_WIFI_STATE` / `NETWORK_SETTINGS` / `OVERRIDE_WIFI_CONFIG` behavior varies by build; no portable silent toggle. | AVAILABLE — system-context `WifiService` calls with `CHANGE_WIFI_STATE` + `OVERRIDE_WIFI_CONFIG` / `NETWORK_SETTINGS`. | AVAILABLE — `WifiService` direct. | AVAILABLE — `cmd wifi set-wifi-enabled` / `wpa_supplicant` control. |
| Bluetooth control | PARTIAL (pairing needs UX) — `BLUETOOTH_CONNECT` runtime + `CompanionDeviceManager`; `BluetoothAdapter.enable()` gated/deprecated. | PARTIAL (UI automation) — can toggle Settings Bluetooth switch; pairing dialogs still need UX. | PARTIAL (restrict, not pair) — `DISALLOW_CONFIG_BLUETOOTH` / `DISALLOW_BLUETOOTH`; no silent-pair API. | AVAILABLE (if allowlisted) — `BLUETOOTH_PRIVILEGED` (`signature\|privileged`) → enable/disable/pair via `BluetoothManagerService`. | AVAILABLE — `BLUETOOTH_PRIVILEGED` with platform cert → full adapter control. | AVAILABLE — `BluetoothManagerService` direct. | AVAILABLE — `service call bluetooth_manager` / `cmd bluetooth_manager` + stack config. |
| tethering | NOT AVAILABLE — `TETHER_PRIVILEGED` gate; no public toggle API. | PARTIAL (UI automation only) — can drive Settings Hotspot toggle; brittle, user-visible. | PARTIAL (restrict only) — `DISALLOW_CONFIG_TETHERING` blocks config; no enable API. | NOT AVAILABLE — `TETHER_PRIVILEGED` is pure-`signature`; privileged allowlist insufficient. | AVAILABLE — `TETHER_PRIVILEGED` (platform cert) → `TetheringManager.startTethering`. | AVAILABLE — `TetheringService` direct. | AVAILABLE — `cmd tethering` / hostapd/dnsmasq control. |
| battery stats | PARTIAL (coarse) — `BatteryManager` broadcasts/level only; per-UID `batterystats` gated. | PARTIAL (same as normal) — accessibility adds no stats bypass. | PARTIAL (same as normal) — no `DevicePolicyManager` stats API. | AVAILABLE (if allowlisted) — `BATTERY_STATS` (`signature\|privileged\|development`) → `dumpsys batterystats` equivalent. | AVAILABLE — `BATTERY_STATS` with platform cert. | AVAILABLE — `BatteryStatsService` direct. | AVAILABLE — `dumpsys batterystats` / `BatteryStatsService` dump. |
| power management (doze exemption) | PARTIAL (user-approved) — `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` needs Settings allowlist intent; cannot self-grant. | PARTIAL (same as normal) — can open battery-optimization UI; user must approve. | PARTIAL (policy-limited) — no direct doze-whitelist API; keep-alive only via lock-task/persistent config side effects. | PARTIAL (no portable bypass) — `DEVICE_POWER` is pure-`signature`; privileged cannot self-exempt Doze portably. | AVAILABLE — `DEVICE_POWER` (platform cert) → `DeviceIdleController` / `PowerManager` whitelist paths. | AVAILABLE — `DeviceIdleController` / `PowerManagerService` direct. | AVAILABLE — `dumpsys deviceidle whitelist` / `cmd deviceidle` + allegedly persistent via custom OS. |
| process management | PARTIAL (own + limited) — own lifecycle + `KILL_BACKGROUND_PROCESSES` for background only. | PARTIAL (same + UI) — can open Running-Services/Apps settings; no signal/process-list API. | PARTIAL (suspend/hide/clear) — `setPackagesSuspended` / `setApplicationHidden` / `clearApplicationUserData`; not arbitrary `kill`/signals. | PARTIAL (force-stop scoped) — `FORCE_STOP_PACKAGES` if allowlisted; no arbitrary signals or full process list (`getRunningAppProcesses` limited/deprecated). | AVAILABLE — `FORCE_STOP_PACKAGES` + `KILL_UID` class paths via `ActivityManagerService`. | AVAILABLE — `ActivityManagerService` process control direct. | AVAILABLE — `am force-stop` / `kill` / `ps` / cgroup control as uid 0. |
| package install | PARTIAL (user-confirmed) — `PackageInstaller.Session` commit requires explicit install-confirm UI. | PARTIAL (UI automation) — can auto-click installer confirm; brittle + Play-Protect/user-visible. | AVAILABLE — delegated `DELEGATION_PACKAGE_INSTALLATION` scope → silent `PackageInstaller` commit without user confirm (managed-device scope). | AVAILABLE (if allowlisted) — `INSTALL_PACKAGES` (`signature\|privileged`) → silent install via `PackageManager`. | AVAILABLE — `INSTALL_PACKAGES` with platform cert → silent install. | AVAILABLE — `PackageManagerService` install path direct. | AVAILABLE — `pm install` / `PackageManagerService` bypass. |
| package removal | PARTIAL (user-confirmed) — `ACTION_UNINSTALL_PACKAGE` requires confirm UI. | PARTIAL (UI automation) — can auto-click uninstall confirm; brittle. | AVAILABLE — delegated install scope → silent `PackageInstaller.uninstall` + `setUninstallBlocked` policy. | AVAILABLE (if allowlisted) — `DELETE_PACKAGES` (`signature\|privileged`) → silent uninstall. | AVAILABLE — `DELETE_PACKAGES` with platform cert → silent uninstall. | AVAILABLE — `PackageManagerService` delete path direct. | AVAILABLE — `pm uninstall` / delete path. |
| system settings write | PARTIAL (user-granted) — `WRITE_SETTINGS` special-access via Settings intent; `Settings.System` only, not `Global`/`Secure`. | PARTIAL (same as normal) — can open write-settings UI; user must grant. | PARTIAL (narrow setters) — `setTime` / `setTimeZone` / location-mode setters; not arbitrary `Settings.Global` writes. | AVAILABLE (if allowlisted) — `WRITE_SETTINGS` + `WRITE_SECURE_SETTINGS` (`signature\|privileged\|development`) → `Settings.System/Global/Secure` writes. | AVAILABLE — `WRITE_SETTINGS` + `WRITE_SECURE_SETTINGS` (platform cert) → full settings writes. | AVAILABLE — `SettingsProvider` direct. | AVAILABLE — `settings put system/global/secure` as shell/root. |
| secure settings write | NOT AVAILABLE — `WRITE_SECURE_SETTINGS` (`signature\|privileged\|development`); no user-grant path. | NOT AVAILABLE — accessibility adds no `Secure` bypass. | PARTIAL (narrow setters only) — `setLocationEnabled` / `setScreenCaptureDisabled` / time setters; not arbitrary `Settings.Secure/Global.putString`. | AVAILABLE (if allowlisted) — `WRITE_SECURE_SETTINGS` in `privapp-permissions-*.xml` → arbitrary `Secure`/`Global` writes. | AVAILABLE — `WRITE_SECURE_SETTINGS` (platform cert) → arbitrary writes. | AVAILABLE — `SettingsProvider` direct. | AVAILABLE — `settings put secure/global` as shell/root. |
| runtime permission grant | NOT AVAILABLE — can only self-request via dialog. | NOT AVAILABLE — cannot programmatically grant; permission dialogs resist automated clicks. | AVAILABLE — `setPermissionGrantState` (grant/deny/default) for other packages without user prompt. | AVAILABLE (if allowlisted) — `GRANT_RUNTIME_PERMISSIONS` (`signature\|privileged`) → `PackageManager.grantRuntimePermission`. | AVAILABLE — `GRANT_RUNTIME_PERMISSIONS` (platform cert) → grant/revoke. | AVAILABLE — `PackageManagerService` grant path direct. | AVAILABLE — `pm grant/revoke` as shell/root. |
| device policy control | NOT AVAILABLE — cannot become admin/owner without provisioning/UX. | NOT AVAILABLE — can open admin settings; activation still needs user/QR consent. | AVAILABLE — IS the controller: password/wipe/restrictions/managed-configs/`lockNow`/delegations via `DevicePolicyManager`. | NOT AVAILABLE — privileged status alone confers no policy-controller role (still needs DO/DA activation). | PARTIAL (implement, not assume) — can implement `DeviceAdminReceiver` but still needs provisioning/activation UX. | AVAILABLE — `DevicePolicyManagerService` enforcement side. | AVAILABLE (provisioning-gated) — `dpm set-device-owner` via shell/root or policy XML; blocked when accounts exist unless reset (verify per build). |
| lock screen control | NOT AVAILABLE — no lock API. | AVAILABLE (lock only) — `GLOBAL_ACTION_LOCK_SCREEN` (API 28+); cannot unlock/disable keyguard. | AVAILABLE — `lockNow` (+ `setKeyguardDisabled` / lock-task scope). | NOT AVAILABLE — privileged status alone adds no keyguard API. | AVAILABLE — `CONTROL_KEYGUARD`-class control + `DevicePolicyManager`/`KeyguardManager` paths with platform cert. | AVAILABLE — `KeyguardManagerService` / `DevicePolicyManagerService` direct. | AVAILABLE — `input keyevent 26` / `lockNow` via shell + keyguard policy edits (custom OS for persistent remap). |
| system UI control | NOT AVAILABLE — overlay (`SYSTEM_ALERT_WINDOW`) only; no status-bar/nav control. | PARTIAL (global actions) — `GLOBAL_ACTION_BACK/HOME/RECENTS/NOTIFICATIONS/QUICK_SETTINGS`; no full status-bar lock. | PARTIAL (kiosk-scoped) — `setStatusBarDisabled` / `setLockTaskPackages` / `setKeyguardDisabled`; not general SystemUI control. | PARTIAL (if allowlisted) — `STATUS_BAR` (`signature\|privileged`) → collapse/expand panels; not full SystemUI policy. | AVAILABLE — `STATUS_BAR` + `StatusBarManager` control with platform cert. | AVAILABLE — `StatusBarManagerService` direct. | AVAILABLE — `cmd statusbar` collapse/expand + SystemUI tuning via shell/custom OS. |
| background execution exemption | PARTIAL (quotas) — `WorkManager`/`JobScheduler`/FGS quotas + background-start restrictions; user whitelist only. | PARTIAL (same as normal) — accessibility adds no scheduler exemption. | PARTIAL (no direct exemption) — restrictions/managed configs do not lift `JobScheduler`/FGS quotas portably. | PARTIAL (no portable exemption) — still subject to background-start/FGS/`JobScheduler` quotas. | PARTIAL (persistent-only) — persistent/system allowlisted apps exempt; ordinary platform-signed apps still gated. | AVAILABLE — `ActivityManager` / `JobSchedulerService` exemption paths. | AVAILABLE — cgroup/`deviceidle`/job whitelists via shell + persistent policy in custom OS. |
| sensors background | PARTIAL (gated) — `BODY_SENSORS` (+ `HIGH_SAMPLING_RATE_SENSORS`) runtime + FGS + background-rate limits. | PARTIAL (same as normal) — accessibility adds no sensor bypass. | PARTIAL (same as normal) — `DISALLOW_SHARE_LOCATION`-class restrictions only prohibit, not grant. | PARTIAL (same gate) — `MANAGE_SENSORS` / `LOCATION_HARDWARE` are `signature`-gated; privileged alone insufficient. | PARTIAL (scoped bypass) — `MANAGE_SENSORS` / `LOCATION_HARDWARE` / context-hub paths with platform cert; continuous background still AppOps-gated. | AVAILABLE — `SensorService` / context-hub direct. | AVAILABLE — HAL/`SensorService` bypass + custom sampling policy in custom OS. |
| USB control | NOT AVAILABLE — `UsbManager` accessory attach needs user dialog; cannot set function/gadget. | PARTIAL (UI automation) — can drive Settings USB-preferences toggle; brittle. | PARTIAL (restrict only) — `DISALLOW_USB_FILE_TRANSFER` / `DISALLOW_MOUNT_PHYSICAL_MEDIA`; cannot set USB function. | AVAILABLE (if allowlisted) — `MANAGE_USB` (`signature\|privileged`) → `UsbManager.setCurrentFunctions` / port control. | AVAILABLE — `MANAGE_USB` (platform cert) → function/gadget/port control. | AVAILABLE — `UsbService` direct. | AVAILABLE — `UsbService` via shell + gadget-configfs (custom OS for persistent gadget policy). |
| external displays | PARTIAL (own content) — `Presentation` API renders own surfaces on secondary display; no system routing control. | PARTIAL (same as normal) — accessibility adds no display-routing API. | PARTIAL (same as normal) — no `DevicePolicyManager` display-routing API. | PARTIAL (same as normal) — no privileged display-routing bypass. | PARTIAL (broader but mediated) — `DisplayManager`/`SurfaceControl` scope wider; still no arbitrary system-compositor control from app context. | AVAILABLE — `DisplayManagerService` / `SurfaceFlinger` routing direct. | AVAILABLE — `dumpsys display` / `SurfaceFlinger` control + custom compositor policy in custom OS. |
| hardware key control | NOT AVAILABLE — in-app `onKeyDown` only; cannot intercept power/system keys globally. | PARTIAL (some keys) — `onKeyEvent` sees subset; power key not delivered; `GLOBAL_ACTION_*` covers back/home/recents/power-dialog only. | PARTIAL (kiosk-scoped) — lock-task/status-bar/keyguard setters affect key behavior; no general remap API. | NOT AVAILABLE — `PREVENT_POWER_KEY` is pure-`signature`; privileged allowlist insufficient. | AVAILABLE — `PREVENT_POWER_KEY` (platform cert) + window-manager policy hooks. | AVAILABLE — `PhoneWindowManager` / `InputDispatcher` policy direct. | AVAILABLE — kernel keylayout (`*.kl`) / `InputDispatcher` interception / custom `PhoneWindowManager` in custom OS. |
| resource scheduling | PARTIAL (quotas) — `JobScheduler` quotas/standby buckets; no priority override. | PARTIAL (same as normal) — accessibility adds no scheduler bypass. | PARTIAL (policy, not priority) — app restrictions/standby policies; no real-time priority API. | PARTIAL (report, not control) — `UPDATE_DEVICE_STATS` permits usage reporting, not priority control. | PARTIAL (scoped) — system-context jobs get wider latitude; still no arbitrary RT/cgroup control from app context. | AVAILABLE — `ActivityManager` oom-adj + `JobSchedulerService` + cgroup control. | AVAILABLE — `nice`/`ionice`/cgroups/sched policies as uid 0 + custom kernel policy in custom OS. |

## 3. Tier capability summary (conservative read)

- NORMAL APP alone: launch, network-state read, and user-consented slices
  (notification listener, MediaProjection, MediaStore/SAF, mic/camera-FGS,
  install/uninstall-with-confirm, `WRITE_SETTINGS`-with-grant) are reachable.
  Silent capture, silent install, secure-settings writes, runtime-grant,
  force-stop, input injection, tethering, USB-function, key-remap, and policy
  control are not.
- Adding ACCESSIBILITY buys exactly two things: cross-app UI-tree read and
  gesture/action injection (+ lock-screen lock + throttled screenshots). It buys
  no file, settings, install, policy, radio, or sensor bypass.
- Adding DEVICE OWNER buys fleet-control primitives: silent install/uninstall via
  delegation, runtime-permission grant state, suspend/hide, `lockNow`, user
  restrictions, and narrow settings setters. It does not buy silent
  screen-capture, input injection, secure-settings free-write, force-stop, Wi-Fi/
  Bluetooth radio override (portably), tethering enable, or sensor/mic bypass.
- Adding PRIVILEGED (preinstalled + allowlisted) buys the
  `signature|privileged` set: `FORCE_STOP_PACKAGES`, `BATTERY_STATS`,
  `INSTALL_PACKAGES`/`DELETE_PACKAGES`, `WRITE_SECURE_SETTINGS` (where
  allowlisted), `GRANT_RUNTIME_PERMISSIONS`, `BLUETOOTH_PRIVILEGED`,
  `MANAGE_USB`, `STATUS_BAR` (collapse/expand). It does not buy pure-`signature`
  items (`INJECT_EVENTS`, `READ_FRAME_BUFFER`, `TETHER_PRIVILEGED`,
  `PREVENT_POWER_KEY`, `DEVICE_POWER`).
- Adding PLATFORM-SIGNED buys the pure-`signature` set on that build:
  `INJECT_EVENTS`, `READ_FRAME_BUFFER`, `TETHER_PRIVILEGED`,
  `PREVENT_POWER_KEY`, `DEVICE_POWER`, `MODIFY_AUDIO_ROUTING`,
  `CONTROL_KEYGUARD`-class control. Still app-context + SELinux confined.
- SYSTEM SERVICE is the first tier with direct manager internals (no UX gates).
- ROOT / CUSTOM OS is the only tier where every row can read `AVAILABLE`, but
  note the two different mechanisms: root gives shell/uid-0 paths on an
  existing build (still SELinux-confined unless permissive), while durable
  guarantees (indicator suppression, persistent gadget/scheduler/compositor
  policy, pre-granted listeners) require a custom OS build.

## 4. Evidence ledger (FACT vs INFERENCE vs UNKNOWN)

### FACT (stable AOSP behavior; safe to design against)

- `FORCE_STOP_PACKAGES`, `INSTALL_PACKAGES`, `DELETE_PACKAGES`,
  `BATTERY_STATS`, `WRITE_SECURE_SETTINGS`, `GRANT_RUNTIME_PERMISSIONS`,
  `BLUETOOTH_PRIVILEGED`, `MANAGE_USB`, `STATUS_BAR` carry (at least)
  `privileged` gating and require `priv-app` + `privapp-permissions-*.xml`
  allowlisting — privileged tier cells above follow directly.
- `INJECT_EVENTS`, `READ_FRAME_BUFFER`, `TETHER_PRIVILEGED`,
  `PREVENT_POWER_KEY`, `DEVICE_POWER` are pure-`signature` — privileged-only
  cells are therefore `NOT AVAILABLE`, platform-signed cells are the first
  `AVAILABLE` tier. `FACT`.
- No cross-app UI-tree read or gesture injection exists for normal apps;
  `AccessibilityService` (`canRetrieveWindowContent`, `performAction`,
  `dispatchGesture`, `GLOBAL_ACTION_LOCK_SCREEN`, API-30 `takeScreenshot`) is
  the documented path. `FACT`.
- `NotificationListenerService` binding always requires explicit user approval;
  no `DevicePolicyManager`/privileged bypass exists. `FACT`.
- MediaProjection always requires per-session user consent; `DevicePolicyManager`
  exposes only `setScreenCaptureDisabled` (prohibit), never grant. `FACT`.
- `PackageInstaller` without delegation/privilege always requires user
  confirmation for install/uninstall; `DELEGATION_PACKAGE_INSTALLATION` is the
  documented DO silent path. `FACT`.
- `WRITE_SECURE_SETTINGS` has no user-grant path; `WRITE_SETTINGS` requires the
  special-access Settings intent and covers `Settings.System` only. `FACT`.
- `setPermissionGrantState` (DO) and `GRANT_RUNTIME_PERMISSIONS`
  (privileged/platform/system) are the documented grant paths; normal +
  accessibility tiers have none. `FACT`.
- Scoped storage (MediaStore/SAF, all-files opt-in) and background-start/FGS +
  mic/camera-indicator gating apply to all app tiers including privileged and
  platform-signed app contexts; only system-service/privileged-system-client or
  custom-OS paths escape them. `FACT`.
- `dpm set-device-owner` requires provisioning conditions (no existing
  accounts / fresh setup on production builds); root/shell does not remove that
  gate, it only provides the shell identity to invoke it. `FACT`.

### INFERENCE (reasoned from gating; verify on target API level / build)

- Wi-Fi toggle row: `setWifiEnabled` blocked for non-system since 10; DO and
  privileged cells marked `PARTIAL` because surviving config/restriction paths
  differ by API level and OEM `WifiService` checks. `INFERENCE` — verify on
  target API (especially 29/30/31+ behavior deltas).
- Audio-routing privileged cell `PARTIAL`: `MODIFY_AUDIO_ROUTING` gating and
  `AudioService.setPreferredDevice` checks vary by build; platform-signed is the
  first portable `AVAILABLE`. `INFERENCE`.
- Power/Doze privileged cell `PARTIAL`: `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`
  UX gate is stable, but OEM `DeviceIdleController` allowlists vary; do not
  assume privileged self-exemption. `INFERENCE`.
- System-settings-write privileged cell `AVAILABLE (if allowlisted)`: assumes
  the build's `privapp-permissions` XML actually grants `WRITE_SECURE_SETTINGS`
  to this package; a missing entry silently demotes the cell to
  `NOT AVAILABLE`. `INFERENCE` — verify with `dumpsys package <pkg>` on the
  target image.
- External-display and resource-scheduling platform cells `PARTIAL`: platform
  cert widens scope but app-context calls into `DisplayManager`/`JobScheduler`
  remain mediated; only `system_server` callers get direct control.
  `INFERENCE`.

### UNKNOWN (must verify per device/build; do not design hard dependencies on these)

- Exact `protectionLevel` strings and allowlist filenames on the target OEM
  image (OEMs occasionally raise `privileged` to `signature` or gate extra
  APIs behind `system`-only checks). `UNKNOWN` — dump target
  `frameworks/base/core/res/AndroidManifest.xml` + `etc/permissions/*.xml`.
- Whether the target build permits `dpm set-device-owner` with existing
  accounts (some enterprise builds add relaxations), and whether `pm grant` /
  `settings put` via shell are SELinux-permitted on a production (`user`)
  build. `UNKNOWN` — probe only via documented read-only dumps where allowed.
- Bluetooth `BLUETOOTH_PRIVILEGED` scope on the target stack (fluoride vs
  Gabeldorsche paths differ for silent pair). `UNKNOWN`.
- `takeScreenshot` (accessibility) throttling/windowing behavior and
  `CAPTURE_SECURE_VIDEO_OUTPUT` enforcement on DRM content per build.
  `UNKNOWN`.
- Sensor `HIGH_SAMPLING_RATE_SENSORS` + background-rate-limit exact thresholds
  per API level/OEM. `UNKNOWN`.

## 5. Recommended minimum tier per goal (conservative)

| Goal | Minimum tier that makes it durable (not brittle UI automation) |
|---|---|
| Reliable app launch + kill + silent install/uninstall + permission grants + lock | DEVICE OWNER with install delegation (no signature needed). |
| + force-stop, battery stats, secure-settings writes, USB functions, Bluetooth privileged control, status-bar collapse | PRIVILEGED APP (preinstalled + allowlisted) on top of DO. |
| + silent screenshots, input injection, tethering enable, power-key prevention, Doze/device-power control | PLATFORM-SIGNED SYSTEM APP (own platform keys ⇒ effectively custom OS signing). |
| + direct service internals, persistent scheduler/compositor/HAL policy, indicator/security-policy changes | SYSTEM SERVICE / CUSTOM OS (custom image; root alone is operational, not durable). |
| UI-tree read + gesture injection without any preinstall/signing | ACCESSIBILITY (user-enabled service; accepts throttling + UX visibility). |

## 6. Notes and non-goals

- This matrix covers mechanism availability, not Play policy, user consent UX,
  or OEM variation. A cell marked `AVAILABLE` can still be rejected by Play
  review, hidden behind an OEM system-only check, or require a provisioning
  flow (QR/`dpm`) that constrains deployment.
- `PARTIAL (UI automation)` cells (accessibility driving Settings/installer/
  hotspot toggles) are listed for completeness but should be treated as
  brittle: layout, locale, timing, and Play-Protect changes break them.
- SELinux note: neither privileged nor platform-signed app tiers escape their
  SELinux domains; only a custom OS with rewritten policy (or a permissive
  build, not expected on production) grants true MAC bypass. Root on a stock
  `user` build bypasses DAC but remains MAC-confined.

## 7. Primary-agent verification appendix (2026-10-06, on-device API 36)

Measured via `adb shell pm list permissions -f` on the ZaraLab emulator
(API 36 userdebug) plus grant/deny POCs. These override the corresponding
cells/claims above where they differ:

- `INSTALL_PACKAGES` = `signature|privileged` → priv-app+allowlist CAN
  hold it. §2 row stands as written.
- `FORCE_STOP_PACKAGES` = `signature|privileged` → priv-app+allowlist CAN
  hold it. §2 row stands as written.
- `GRANT_RUNTIME_PERMISSIONS` = `signature|installer|verifier` (NO
  `privileged` flag) → priv-app CANNOT hold it. **Correction:** any row
  implying priv-app self-grants runtime permissions is wrong; use DO
  delegation or platform signature instead.
- `STATUS_BAR` = `signature|privileged|recents` → §2 row stands.
- `READ_FRAME_BUFFER` = `signature|recents` (not bare `signature`).
- `WRITE_SECURE_SETTINGS` =
  `signature|privileged|development|installer|role` → §2 row stands
  (allowlist path real, still needs the XML entry).
- Voice-hotword nuance missing above: `MANAGE_VOICE_KEYPHRASES` and
  `CAPTURE_AUDIO_HOTWORD` carry the `privileged` flag (priv-app-holdable);
  `MANAGE_HOTWORD_DETECTION` is `internal|preinstalled` (not holdable).
- Method note: `pm list permissions` output requires `-A6` parsing —
  naive adjacent-line pairing misattributes levels (caught during this
  verification). Runtime (`dangerous`) permissions are absent from that
  listing entirely; their gates are unchanged by this appendix.
- See `PRIVILEGED_ANDROID_FINAL_REPORT.md` (Verification appendix) for
  the full adjudication. POC: priv-app grant proven end-to-end on the
  same image (placement + allowlist → `granted=true` by app self-report
  and `dumpsys`; missing entry → `system_server` boot crash, not silent
  deny).
