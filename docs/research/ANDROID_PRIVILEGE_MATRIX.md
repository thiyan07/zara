# Android Privilege Matrix — AI-Assistant Body App

**Scope:** What each Android privilege tier can / cannot do for an on-device
AI-assistant "body" app (launch apps, see UI, hear/see, act on notifications,
manage device). Read-only research; no device modification performed.

**Tiers compared:**

1. Normal third-party app (Play-installed, sandboxed UID)
2. AccessibilityService-enabled app (user explicitly enables in Settings)
3. Device Owner / Profile Owner — Device Policy Controller via `DevicePolicyManager`
4. Privileged system app (`/system/priv-app`, non-platform signature)
5. Platform-signed system app (signed with OS platform key)
6. Custom system service in the system image (AOSP build, `system_server` / system UID)
7. Root (UID 0 via `su` / Magisk / engineering build)

**Capability verdicts:** `AVAILABLE` / `PARTIAL` / `CONDITIONAL` / `NOT AVAILABLE`.
`CONDITIONAL` names the condition (user consent, provisioning method, API level, signature).

**Epistemic labels used below:** `FACT` = documented AOSP / developer.android.com
behavior, verifiable in source. `INFERENCE` = reasonable synthesis, not a quoted
API guarantee. `UNKNOWN` = version/OEM-dependent, do not rely on without testing.

> Android version drift matters. Unless stated otherwise, statements target
> Android 10–15 (API 29–35). OEMs (Samsung, Xiaomi, etc.) add further restrictions
> (background kill, extra permission prompts).

---

## 1. Master matrix

| Capability | (1) Normal app | (2) +Accessibility | (3) Device/Profile Owner | (4) priv-app | (5) platform-signed | (6) custom system service | (7) root |
|---|---|---|---|---|---|---|---|
| Launch other apps | CONDITIONAL (explicit/implicit `Intent`, no force) | CONDITIONAL (same as (1) + global actions / gestures) | CONDITIONAL (same as (1); + `setPackagesSuspended`, `setApplicationHidden`, lock-task allowlist) | Same as (1) | Same as (1) + some signature-only starters | AVAILABLE (ActivityManagerInternal, can force-launch / move tasks) | AVAILABLE (am CLI / direct AM calls) |
| Force-stop / kill other apps | NOT AVAILABLE (`KILL_BACKGROUND_PROCESSES` kills only own/background; `FORCE_STOP_PACKAGES` is signature-only) | NOT AVAILABLE (no kill API; gestures can swipe-away recents only) | PARTIAL (cannot `forceStopPackage` directly; CAN suspend, hide, block-uninstall, `clearApplicationUserData` on managed profile, `wipeData`) | CONDITIONAL (only if whitelisted privileged perm; still not arbitrary force-stop) | CONDITIONAL (with signature perms e.g. `FORCE_STOP_PACKAGES` — platform-signed gets it) | AVAILABLE (ActivityManagerService internals) | AVAILABLE (`am force-stop`, `kill`, `/proc`) |
| UI inspection (other apps' view hierarchy / text) | NOT AVAILABLE (own windows only) | AVAILABLE, CONDITIONAL on user enablement + service config (AccessibilityEvents + `AccessibilityNodeInfo` tree) | NOT AVAILABLE (DPC grants no view-hierarchy access by itself) | NOT AVAILABLE | NOT AVAILABLE (except `DUMP`/`RETRIEVE_WINDOW_CONTENT` privileged paths, still gated) | AVAILABLE (WindowManager / Accessibility internals) | AVAILABLE (read framebuffer, `dumpsys`, input inspection) |
| UI injection (tap / swipe / type / global back-home-recents) | NOT AVAILABLE (only own Views; `dispatchGesture` denied) | AVAILABLE, CONDITIONAL on user enablement (`performAction`, `dispatchGesture` API 24+, `performGlobalAction`) | NOT AVAILABLE (DPC alone injects nothing) | NOT AVAILABLE | CONDITIONAL (`INJECT_EVENTS` is signature-only; platform-signed can hold it) | AVAILABLE (`INJECT_EVENTS` / InputManager internal) | AVAILABLE (`/dev/input`, `input` CLI, `sendevent`) |
| Notification read | CONDITIONAL (`NotificationListenerService` + explicit user grant in Settings) | PARTIAL (receives notification *events* with limited extras; full content needs `NotificationListenerService`) | NOT AVAILABLE (DPC adds no notification-read; still needs NLS grant) | NOT AVAILABLE (still needs NLS) | CONDITIONAL (signature NLS variants / `STATUS_BAR_SERVICE` paths exist but still gated) | AVAILABLE (NotificationManagerService internal) | AVAILABLE (read NLS DB / dumpsys / hook service) |
| Notification action (dismiss, reply, tap) | CONDITIONAL (via NLS: `cancelNotification`, `RemoteInput`, full-screen intent) | PARTIAL (global actions + node clicks can approximate; structured reply needs NLS/RemoteInput) | NOT AVAILABLE | NOT AVAILABLE | PARTIAL (same as (1) + status-bar signature paths) | AVAILABLE | AVAILABLE |
| Files outside sandbox | NOT AVAILABLE (own dir + scoped MediaStore/SAF grants only; no `/data/data/<other>` read) | NOT AVAILABLE (no file-sandbox escape) | NOT AVAILABLE (`DevicePolicyManager` grants no file read; `clearApplicationUserData` ≠ read) | NOT AVAILABLE (no direct escape; a few privileged media/stats APIs only) | PARTIAL (signature perms open some providers — e.g. `INTERACT_ACROSS_USERS`, `READ_LOGS` historically — but NOT arbitrary private dirs; SELinux still applies) | PARTIAL (system UID reaches far more, but SELinux + FBE still bound it; not kernel-bypass) | AVAILABLE, CONDITIONAL on decryption (FBE-unlocked; hardware-backed keys / Keystore still not extractable) |
| Microphone background use | CONDITIONAL (foreground service + `RECORD_AUDIO` runtime grant + mic indicator; OEMs kill background; Android 12+ privacy indicators) | CONDITIONAL (same as (1); Accessibility adds NO mic privilege) | CONDITIONAL (same as (1); DPC can `setCameraDisabled`/`setScreenCaptureDisabled` and mute, not grant itself mic) | CONDITIONAL (same as (1); still needs runtime grant) | CONDITIONAL (same + `CAPTURE_AUDIO_OUTPUT` signature path can capture playback, not covert mic) | CONDITIONAL (can host privileged audio paths, still audited; covert capture is policy/malware, not API) | AVAILABLE (ALSA / audio HAL / hook; still detectable) |
| Camera background use | CONDITIONAL, effectively PARTIAL (foreground service + runtime grant; background camera heavily restricted since Android 12 — background start blocked) | CONDITIONAL (same as (1); no camera privilege added) | CONDITIONAL (same; DPC can *disable* camera via `setCameraDisabled`, not enable covertly) | CONDITIONAL (same; still needs runtime) | CONDITIONAL (same) | CONDITIONAL (can bind camera service internals, but framework policy still applies) | AVAILABLE (V4L2 / HAL bypass; indicator bypass possible = malware behavior) |
| Screen capture / screenshot / screenrecord | CONDITIONAL (`MediaProjection` + per-session system consent dialog + foreground service; `takeScreenshot` in Accessibility API 30+ with enablement) | CONDITIONAL (`AccessibilityService.takeScreenshot()` API 30+, user-enabled; otherwise MediaProjection path) | CONDITIONAL (DPC can *prohibit* via `setScreenCaptureDisabled`; cannot grant itself silent capture — still needs MediaProjection/consent) | CONDITIONAL (same as (1)) | CONDITIONAL (signature `READ_FRAME_BUFFER` / `CAPTURE_SECURE_VIDEO_OUTPUT` paths exist but gated to system) | AVAILABLE (SurfaceFlinger screenshot internal) | AVAILABLE (framebuffer / `screencap`) |
| Audio routing (force speaker/BT, volume, ducking) | PARTIAL (own streams: `AudioManager`, focus request, volume; cannot globally force-route other apps) | PARTIAL (same + global volume keys via global actions) | PARTIAL (restrictions: `DISALLOW_ADJUST_VOLUME`, mute; not fine-grained routing) | PARTIAL (same as (1)) | PARTIAL (+ `MODIFY_AUDIO_ROUTING` / `CAPTURE_AUDIO_OUTPUT` signature paths) | AVAILABLE (AudioService policy internal) | AVAILABLE (tinymix / HAL / policy file) |
| Network / WiFi / Bluetooth control | PARTIAL (request networks, `ConnectivityManager`, BT connect with runtime perms; CANNOT toggle WiFi/BT/Data on Android 10+ — Settings Panels / `CompanionDeviceManager` / user toggle only) | PARTIAL (same; gestures could tap Settings toggles — fragile, user-visible) | PARTIAL (`addUserRestriction`: `DISALLOW_CONFIG_WIFI/BT`, `DISALLOW_DATA_ROAMING`, always-on VPN; cannot silently toggle radios on modern Android) | PARTIAL (same as (1); a few privileged wifi/BT perms with whitelist) | PARTIAL (+ signature `CONNECTIVITY_INTERNAL`, `BLUETOOTH_PRIVILEGED`, `WRITE_SECURE_SETTINGS`-backed toggles where still honored) | AVAILABLE (ConnectivityService / WifiService internal) | AVAILABLE (`svc`, `cmd wifi`, netfilter, HCI) |
| Battery / power APIs | PARTIAL (query `BatteryManager`; request ignore-optimizations via Settings intent; no force-doze control) | PARTIAL (same) | PARTIAL (`reboot()` API 24+ for device owner; stay-on-while-plugged; max screen-off timeout; cannot read privileged power internals) | PARTIAL (same + whitelisted `BATTERY_STATS`, `DUMP`) | PARTIAL (+ `DEVICE_POWER`, `REBOOT` signature perms) | AVAILABLE (PowerManagerService internal; suspend blockers, shutdown) | AVAILABLE (sysfs, power HAL, `reboot`, kernel wakelocks) |
| Package install | CONDITIONAL (user-confirmed `PackageInstaller` session or Play; `REQUEST_INSTALL_PACKAGES` + consent dialog) | CONDITIONAL (same; gestures cannot legally bypass installer consent) | AVAILABLE, CONDITIONAL on managed-device provisioning (DPC + `PackageInstaller` = silent install without user dialog; `setUninstallBlocked`, `setApplicationHidden`, `enableSystemApp`) | CONDITIONAL (same as (1) unless also DPC; priv-app alone ≠ silent install) | CONDITIONAL (same as (1) unless also DPC; `INSTALL_PACKAGES` is signature|privileged but silent path still needs DPC/owner context) | AVAILABLE (PackageManagerService internal silent install) | AVAILABLE (`pm install`, direct `/data/app` write) |
| Package uninstall / remove | CONDITIONAL (own package self-uninstall via intent; others need user confirm or DPC) | CONDITIONAL (same) | AVAILABLE, CONDITIONAL (silent uninstall via DPC `PackageInstaller` delegation / `setUninstallBlocked(false)` + hidden-suspend; profile-owner scoped to profile) | CONDITIONAL (same as (1)) | CONDITIONAL (`DELETE_PACKAGES` signature path; silent still needs owner/system context) | AVAILABLE | AVAILABLE (`pm uninstall`, delete APK) |
| System settings write (`Settings.Secure/Global`) | CONDITIONAL (`WRITE_SETTINGS` for `System` + user grant screen; `WRITE_SECURE_SETTINGS` NOT available) | CONDITIONAL (same; no settings privilege added) | PARTIAL (dedicated DPC setters — time, locale, restrictions — but NOT arbitrary `Secure/Global` writes) | CONDITIONAL (`WRITE_SECURE_SETTINGS` allowlisted ONLY if manifest + `privapp-permissions` XML whitelists it) | AVAILABLE, CONDITIONAL on holding signature perms (`WRITE_SECURE_SETTINGS`, `WRITE_APN_SETTINGS`, etc.) | AVAILABLE | AVAILABLE (`settings put`, direct provider write) |
| Runtime permission grants (grant dangerous perms to self/others) | NOT AVAILABLE (must show system dialog; cannot self-grant) | NOT AVAILABLE (Accessibility cannot click its own permission dialogs into granted state as a supported path; Play policy forbids) | AVAILABLE, CONDITIONAL, scoped (DPC `setPermissionGrantState` / `setPermissionPolicy(PERMISSION_POLICY_AUTO_GRANT/AUTO_DENY/ASK)` for managed apps — the one tier that can auto-grant WITHOUT being a system app) | NOT AVAILABLE (priv-app status alone grants nothing at runtime; dangerous still need user or DPC auto-grant) | PARTIAL (pre-granted via manifest + image defaults possible; still not arbitrary third-party grants without DPC/system-service) | AVAILABLE (permission policy internal) | AVAILABLE (`pm grant`, `appops set`) |
| Lock-screen / biometric / password control | PARTIAL (`KeyguardManager` / `BiometricPrompt` for own auth; `DeviceAdmin` lock/wipe deprecated Android 9–10+) | PARTIAL (can `lockNow` only if ALSO device admin/DPC; gestures can press power — fragile) | AVAILABLE, CONDITIONAL (`lockNow`, `setPasswordQuality`, `resetPassword` with restrictions on modern Android, `setKeyguardDisabledFeatures`, `wipeData`) | NOT AVAILABLE | CONDITIONAL (signature device-admin paths) | AVAILABLE (Keyguard / LockSettings internal) | AVAILABLE (locksettings CLI, Gatekeeper bypass with unlocked FBE — forensic, not API) |
| Background execution exemptions (Doze / App Standby / FGS / exact alarms) | CONDITIONAL (user must grant ignore-optimizations, `SCHEDULE_EXACT_ALARM`, FGS types; OEM task-killers still apply) | CONDITIONAL (bound service is *less* likely killed but NOT exempt; still needs same grants) | PARTIAL (DPC can set always-on VPN, `setPackagesSuspended` others, stay-on; cannot exempt arbitrary third-party from Doze — still needs user grant) | PARTIAL (allowlisted `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`? still user-confirmed; persistent priv-apps less likely killed) | PARTIAL (persistent system apps genuinely survive better; still policy-bound) | AVAILABLE (can whitelist / run as persistent system service) | AVAILABLE (disable doze, cgroup/oom tweaks, run as daemon) |
| Sensors (motion, location, body, BT scan) | CONDITIONAL (runtime perms: `ACCESS_FINE_LOCATION`, `BODY_SENSORS`, `ACTIVITY_RECOGNITION`, nearby-devices; background location needs separate grant + Settings) | CONDITIONAL (same; no sensor privilege added) | CONDITIONAL (same; DPC can force location mode / disallow share-location user restriction, not self-grant sensor reads) | CONDITIONAL (same; still needs runtime) | CONDITIONAL (same + signature background-location pre-grants possible in image) | CONDITIONAL (internal sensor access, still permission/SELinux-audited) | AVAILABLE (sensor HAL /): hardware-direct read bypasses framework checks) |

---

## 2. Per-tier honest assessment

### Tier 1 — Normal third-party app — FACT

- Sandboxed UID, SELinux-isolated; can only touch own `/data/data/<pkg>` plus
  explicitly granted MediaStore / Storage Access Framework handles. `FACT`.
- Can start other apps only by cooperative `Intent` (launcher intent, VIEW, share);
  callee or system may refuse. Cannot stop/kill others. `FACT`.
- Notifications require `NotificationListenerService` + user toggling access in
  Settings; access is revocable. `FACT`.
- Mic/camera need dangerous runtime grants + visible foreground-service + (Android
  12+) privacy dot/indicator; background camera launch is blocked. `FACT`.
- Screen capture needs `MediaProjection` + per-capture system dialog. `FACT`.
- Radio toggles (WiFi/BT/mobile-data) were removed for third-party apps (Android
  10+); only Settings Panels / user action remain. `FACT`.
- Package install/update goes through user-confirmed `PackageInstaller` UI or
  Play; silent install is not available. `FACT`.
- `WRITE_SECURE_SETTINGS`, `FORCE_STOP_PACKAGES`, `INJECT_EVENTS`,
  `INSTALL_PACKAGES`, `MANAGE_USERS` are not grantable. `FACT`.
- Background work is throttled by Doze / App Standby / background-service limits /
  exact-alarm gates; "ignore battery optimizations" needs a Settings intent.
  `FACT`.

### Tier 2 — AccessibilityService-enabled app — FACT vs myth

Genuinely granted (with explicit user enablement in Settings + declared
`accessibilityservice` XML config), `FACT`:

- Receives `AccessibilityEvent`s (window state/content changes, notification
  events, view text where `importantForAccessibility` allows).
- Queries and acts on `AccessibilityNodeInfo` trees (`click`, `scroll`, `setText`
  on API 26+), `performGlobalAction` (back/home/recents/notifications/quick-settings),
  `dispatchGesture` (API 24+), `takeScreenshot` (API 30+).
- `canRetrieveWindowContent`, `canPerformGestures` are config-gated capabilities,
  not blanket powers.

Does NOT grant (common myths), `FACT`:

- **Myth: "Accessibility = root / full control."** It observes + injects *UI
  actions a user could perform*, only while enabled; it confers no file-sandbox
  escape, no silent install, no permission self-grant, no radio/power control.
- **Myth: "It can read passwords / 2FA silently."** Password fields can set
  `importantForAccessibility="no"`; keyboards/flags may withhold content; Play
  policy + on-device warnings treat abuse as malware. Capability is fragile, not
  guaranteed.
- **Myth: "It bypasses consent dialogs."** It cannot grant itself dangerous
  permissions, NLS access, or device-admin; each still needs its own user toggle.
  Google Play's Accessibility API policy additionally restricts use to
  accessibility purposes and requires prominent disclosure.
- **Myth: "It runs exempt forever."** A bound accessibility service is
  higher-priority but still killable; battery-optimization and OEM killers still
  apply; the user can disable it at any time with one toggle.
- **INFERENCE:** For an assistant body, Accessibility is the strongest *UI-level*
  automation available without re-imaging — good for "tap as the user would" —
  but it is user-visible, brittle across app redesigns, and unsuitable as a silent
  security boundary.

### Tier 3 — Device Owner / Profile Owner (DPC) — what it CAN do WITHOUT being a system app

Provisioning `FACT`: set at setup time via QR/NFC/cloud/`dpm` (test only via
`adb shell dpm set-device-owner …` on an account-free device); Device Owner =
one fully-managed device; Profile Owner = work profile on personal device.
APIs surface through `DevicePolicyManager` + `DeviceAdminReceiver`.

WITHOUT any system-app signature, a correctly provisioned owner CAN, `FACT`
(documented `DevicePolicyManager` methods):

- **Silent install / uninstall:** install via `PackageInstaller` without user
  confirmation in managed context; `setUninstallBlocked`, `setApplicationHidden`,
  `enableSystemApp`, `setPackagesSuspended`. (Unmanaged-context silent install
  remains unavailable.)
- **Permission policy:** `setPermissionGrantState` (grant/deny/default per
  permission per managed app) + `setPermissionPolicy`
  (`PERMISSION_POLICY_AUTO_GRANT` / `AUTO_DENY` / `PROMPT`). This is the *only*
  non-system tier that can auto-grant dangerous permissions — scoped to managed
  packages, not arbitrary self-escalation.
- **Lock-task mode / kiosk:** `setLockTaskPackages` + `setLockTaskFeatures`
  (home/overview blocking, keyguard/status-bar control in task).
- **Screen-capture policy:** `setScreenCaptureDisabled(admin, true/false)` —
  can *prohibit* screenshots/recording on managed user; cannot conjure a silent
  capture capability for itself (still needs MediaProjection/NLS path).
- **System-update policy:** `setSystemUpdatePolicy` (automatic / windowed /
  postponed updates).
- **Always-on VPN:** `setAlwaysOnVpnPackage` (+ lockdown flag).
- **USB / data / config restrictions:** `addUserRestriction` —
  `DISALLOW_USB_FILE_TRANSFER`, `DISALLOW_CONFIG_BLUETOOTH/WIFI`,
  `DISALLOW_SHARE_LOCATION`, `DISALLOW_ADJUST_VOLUME`, `DISALLOW_FACTORY_RESET`,
  etc.; plus `setCameraDisabled`, `setKeyguardDisabledFeatures`,
  `setStatusBarDisabled`, `setScreenOffTimeout`/`setMaximumTimeToLock`,
  `lockNow`, `wipeData`, `reboot` (device-owner, API 24+).

CANNOT, even as owner, `FACT`:

- Read other apps' private files, view hierarchies, or notification content
  without the separate Accessibility/NLS grants.
- Inject taps/gestures, capture mic/camera covertly, toggle radios silently on
  modern Android, or write arbitrary `Settings.Secure/Global` keys (only the
  dedicated DPC setters).
- Manage a personal user's private profile beyond the work profile (Profile
  Owner is profile-scoped; Device Owner expects a fully-managed device — trying
  to "DPC a daily-driver phone" breaks personal accounts/Play expectations).

`INFERENCE`: DPC is the correct answer to "fleet/kiosk management without
building an OS image" (silent deploy, permission auto-grant, kiosk, update/VPN
policy). It is the wrong answer to "invisible personal assistant with full
UI/file/sensor access" — it adds management policy, not sensing/actuation.

### Tier 4 — Privileged system app (`priv-app`) — FACT

- Location: `/system/priv-app/` (or vendor/product equivalents); signature need
  NOT be platform, but permissions must be allowlisted in
  `etc/permissions/privapp-permissions-<oem>.xml`. `FACT`.
- **Why priv-app status alone grants nothing at runtime:** Android grants at
  install time only permissions that are (a) declared in the manifest AND (b)
  whose protection level the app qualifies for AND (c) allowlisted if
  `privileged`. Dangerous (runtime) permissions (`CAMERA`, `RECORD_AUDIO`,
  location, …) still require a runtime grant (user dialog or DPC auto-grant);
  being in `priv-app` skips neither the dialog nor the grant state. `FACT`.
- What it *does* unlock: `signature|privileged` ("privileged") permissions such
  as `WRITE_SECURE_SETTINGS`, `DUMP`, `PACKAGE_USAGE_STATS`-adjacent privileged
  paths, `CONTROL_VPN` (version-dependent) — each still needs manifest +
  whitelist entry; missing whitelist = silent non-grant (logged, install still
  succeeds). `FACT`.
- What it does NOT unlock: `signature`-only (platform) permissions
  (`FORCE_STOP_PACKAGES`, `INJECT_EVENTS`, `INSTALL_PACKAGES` in most builds,
  `MANAGE_USERS`, `STATUS_BAR`, …) unless also platform-signed; no sandbox
  escape, no silent capture, no radio toggles beyond tier-1. `FACT`.
- `INFERENCE`: priv-app is a *packaging + whitelist* step for an OEM/image
  builder, not a privilege escalation an app can self-promote into (requires
  system-image signing / OTA).

### Tier 5 — Platform-signed system app — FACT

- Signed with the same platform key that signed the framework
  (`platform.pk8`); therefore qualifies for `signature`-protectionLevel
  permissions declared in its manifest. `FACT`.
- Examples opened (version-dependent): `INTERACT_ACROSS_USERS`,
  `STATUS_BAR`, `FORCE_STOP_PACKAGES`, `INSTALL_PACKAGES` / `DELETE_PACKAGES`,
  `INJECT_EVENTS`, `READ_FRAME_BUFFER` (where still present),
  `WRITE_SECURE_SETTINGS`, `CONNECTIVITY_INTERNAL`-family,
  `BLUETOOTH_PRIVILEGED`, `CAPTURE_AUDIO_OUTPUT`. Each still must be declared;
  some additionally need privileged-whitelist or explicit user/role grant.
  `FACT`.
- Still bound: own UID sandbox + SELinux + runtime-permission model for
  dangerous permissions (pre-grantable in the image via default grants, but the
  grant *exists* and is auditable/revocable where applicable); no kernel bypass;
  no Keystore exfiltration; no bypass of FBE. `FACT`.
- `INFERENCE`: the first tier where "assistant can quietly manage packages,
  inject input, read privileged state" becomes architecturally honest — at the
  cost of requiring the platform key (OEM/ROM builder cooperation). Losing the
  key = losing updatability.

### Tier 6 — Custom system service in the system image — FACT

- Code shipped in the image running as system UID (1000) or inside
  `system_server`: can call `@hide` / internal managers
  (`ActivityManagerInternal`, `WindowManagerInternal`, `PackageManagerInternal`,
  `AudioService`, `PowerManagerService`, `NotificationManagerService`,
  `SensorService`) subject to SELinux + caller checks. `FACT`.
- Can therefore: force-start/stop tasks, move stacks, screenshot via
  SurfaceFlinger, route audio, manage power/vibration, silent-install via PMS,
  expose a custom Binder API to a companion app with signature checks. `FACT`.
- Still bound: SELinux policy (system_server is one of the most constrained
  domains), permissions checks on internal calls, Verified Boot / AVB (image
  tampering breaks boot on locked bootloaders), no kernel-bypass, no
  hardware-key extraction. Requires AOSP fork + signing + OTA infra. `FACT`.
- `UNKNOWN` without a concrete base (Pixel AOSP vs Samsung vs custom SoC):
  exact internal API names/behaviors shift per release; treat any internal call
  as version-pinned.
- `INFERENCE`: the architecturally clean home for a "body" that must *enforce*
  policy (kiosk, fleet, embodied device) rather than merely *request* it.

### Tier 7 — Root — FACT

- UID 0 bypasses Android framework permission checks (`PermissionChecker`,
  `AppOps`) for most paths: arbitrary `pm`/`am`/`settings`/`dumpsys` commands,
  read/write app-private dirs (once FBE-unlocked), `/dev/input` injection,
  framebuffer capture, netfilter/HCI control, sysfs/power control. `FACT`.
- Does NOT automatically bypass: SELinux enforcing (unless set permissive /
  policy patched — noisy and version-fragile), Verified Boot (modifications
  detected; locked bootloader won't boot tampered images), file-based
  encryption at rest (needs unlocked user), hardware-backed Keystore/StrongBox
  (keys non-extractable by design), Play Integrity / SafetyNet (root is
  detectable; banking/DRM break). `FACT`.
- Costs: breaks OTA assumptions, voids warranties/policies, expands attack
  surface (every assistant bug becomes a root bug), fragile across updates and
  per-device. `FACT`.
- `INFERENCE`: root answers "can it technically do X today on this bench
  device" but is not a shippable privilege tier for a consumer assistant;
  anything depending on root should be re-scoped to tiers 2/3/5/6 before
  productization.

---

## 3. Capability notes (per requested axis)

- **App launch/stop:** (1) intent-only launch, no stop; (2) + human-like
  navigation but no API kill; (3) suspend/hide/lock-task instead of kill; (4)
  still no arbitrary kill; (5) `FORCE_STOP_PACKAGES` yes; (6–7) yes. `FACT`.
- **UI inspection/injection:** only (2) user-enabled accessibility, (6) window
  internals, (7) input/framebuffer bypass, and (5)-with-`INJECT_EVENTS` do this;
  (1)/(3)/(4) do not. `FACT`.
- **Notification read/action:** NLS user grant is the only non-system path;
  accessibility events are a lossy substitute; DPC/priv-app add nothing here.
  `FACT`.
- **Files outside sandbox:** no tier below (7) reads arbitrary private dirs;
  (6) reaches furthest within policy; (5) opens providers, not private dirs.
  `FACT`.
- **Mic/camera background:** all framework tiers need runtime grants + foreground
  + indicators; only (6)/(7) host alternate capture paths, and covert use is
  malware-class, not API. `FACT` (+ Play policy).
- **Screen capture:** MediaProjection consent or API-30 accessibility screenshot
  are the only store-safe paths; DPC can only *deny*; silent capture needs
  (6)/(7). `FACT`.
- **Audio routing:** global routing is (6)/(7) (and narrow (5) signature
  perms); others manage own streams/focus. `FACT`.
- **Network/WiFi/BT:** direct radio toggles are gone for (1)/(2)/(4); (3) uses
  restrictions + always-on VPN; (5) keeps some signature toggles where the build
  honors them; (6)/(7) own the services. `FACT` (API 29+ behavior).
- **Battery/power:** query everywhere; control (doze exemption, reboot, power
  internals) only (3)-reboot, (5)-signature, (6)/(7). `FACT`.
- **Install/remove:** silent = (3)-managed, (6)/(7), or (5)+owner context; (1)/(2)/(4)
  are user-confirmed. `FACT`.
- **Settings write:** `WRITE_SECURE_SETTINGS` = (4)-whitelisted, (5), (6), (7);
  (3) uses dedicated setters, not raw writes. `FACT`.
- **Permission grants:** auto-grant without system signature exists ONLY at (3)
  (`setPermissionGrantState`/`setPermissionPolicy`, managed scope) and above.
  `FACT` — this is the most commonly misunderstood DPC power.
- **Lock-screen/biometric:** `lockNow`/password/wipe policy = (3); raw
  LockSettings/Keyguard internals = (6)/(7); (1)/(2)/(4) only authenticate
  themselves. `FACT` (`DeviceAdmin` legacy lock/wipe deprecated).
- **Background exemptions:** no silent exemption below (6)/(7); persistent
  system apps survive better but are still policy-bound; (2) binding helps
  priority, not exemption. `FACT`.
- **Sensors:** runtime-perm model applies through (6); (7) alone bypasses via
  HAL. `FACT`.

---

## 4. FACT vs INFERENCE vs UNKNOWN

**FACT (do not re-litigate without AOSP citation):**

- Sandboxing, runtime-permission, NLS-gate, MediaProjection-dialog,
  background-camera/radio-toggle restrictions as above.
- Accessibility = observe + human-like actuation only while user-enabled; zero
  sandbox/install/permission/file escalation.
- DPC powers listed in §2-Tier-3 exist WITHOUT system-app status once provisioned
  as owner (silent managed install, permission policy, lock-task, capture/VPN/
  update/USB restrictions); DPC adds no UI/file/sensor/mic powers.
- priv-app ≠ runtime grant; whitelist + manifest + install-time grant model.
- Platform signature unlocks `signature` perms but not kernel/SELinux/Keystore/FBE bypass.
- Root bypasses framework checks but not (by itself) SELinux-enforcing, AVB,
  FBE-at-rest, StrongBox, or attestation detectability.

**INFERENCE (best synthesis; validate before building on it):**

- For a *store-distributable* assistant body, practical ceiling is
  Tier 1 + Tier 2 (Accessibility) + `NotificationListenerService` + foreground
  services — i.e. "cooperative + human-like UI automation," brittle and
  user-visible by design.
- For *fleet / kiosk / embodied* devices, the honest stack is Tier 3 (DPC) for
  deployment + policy, optionally Tier 5/6 (platform signature / system service)
  for actuation — one AOSP image you control beats five escalations you don't.
- Anything prototyped at Tier 7 (root) must be re-mapped to a shippable tier
  before release; root-only features do not transfer to consumer devices.

**UNKNOWN (must test per device / OS version; do not assert):**

- Exact OEM background-killer behavior (Samsung / Xiaomi / Oppo / Vivo),
  exact `privapp-permissions` whitelist contents per build, exact internal
  (`@hide`) API availability per API level, and exact Play-policy enforcement
  timing for Accessibility/NLS declarations.
- Whether a given `signature`-permission name still exists / is enforceable on
  a specific build (names migrate: `READ_FRAME_BUFFER`, wifi/BT internals, power
  perms).
- Forensic-grade questions (locked-device FBE access, Gatekeeper/Weaver bypass,
  StrongBox extraction) — out of scope; treat as not available.

---

## 5. Bottom line for the assistant body

| Goal | Minimum honest tier |
|---|---|
| Launch apps cooperatively, speak/hear with consent, handle own notifications | (1) Normal app |
| "See screen, tap as user would," read/act on notifications | (1) + (2) Accessibility + NLS (all user-enabled, revocable) |
| Fleet silent-deploy, auto-grant runtime perms, kiosk lock-task, VPN/update/USB policy | (3) Device/Profile Owner (provisioned; no system signature needed) |
| Write secure settings, hold privileged (not platform) perms | (4) priv-app in image + whitelist |
| Force-stop, inject input silently, manage packages/signals broadly | (5) Platform-signed (needs OEM/ROM key) or (6) system service |
| Full device actuation without framework consent gates | (6) Custom system service in owned image; (7) root only for bench/dev, not shippable |

*Rule of thumb:* management policy → Tier 3; UI automation with consent →
Tier 2; silent system-level actuation → Tier 5/6 on an image you sign; Tier 7
proves feasibility but ships nothing.

## 6. Primary-agent verification appendix (2026-10-06, on-device API 36)

Measured via `adb shell pm list permissions -f` (parsed with `-A6`;
naive adjacent-line pairing misattributes levels) on the ZaraLab
emulator, plus grant/deny POCs. These override the corresponding claims
above:

- **Tier 4 correction:** `INSTALL_PACKAGES` and `FORCE_STOP_PACKAGES`
  are `signature|privileged` (NOT pure-`signature`), so a priv-app with
  an allowlist entry CAN hold them (proven end-to-end: both granted).
  §2 Tier 4 lines 180–183 and master-matrix row 35/47 wordings that say
  otherwise are superseded.
- **`STATUS_BAR`** is `signature|privileged|recents` (not pure-`signature`).
- **Tier 4 as stated stands for:** `GRANT_RUNTIME_PERMISSIONS`
  (`signature|installer|verifier` — priv-app cannot self-grant, §2 row
  line 50 remains correct), `INJECT_EVENTS` (`signature`),
  `READ_FRAME_BUFFER` (`signature|recents`), `MANAGE_HOTWORD_DETECTION`
  (`internal|preinstalled`).
- **Mic-row nuance:** the claim "only tiers (6)/(7) host alternate
  capture paths" needs the hotword qualification — `MANAGE_VOICE_KEYPHRASES`
  and `CAPTURE_AUDIO_HOTWORD` carry the `privileged` flag (priv-app-holdable
  with allowlist); background-mic/AppOps/indicator gates still apply to
  all app tiers.
- Full adjudication: `PRIVILEGED_ANDROID_FINAL_REPORT.md`.
