// Zara capability table — pure Dart (no Flutter dependency).
//
// A capability is advertised ONLY when the device explicitly supports it.
// Unsupported capabilities return supported=false; nothing is faked.
//
// Reserved for later stages (NOT implemented in Stage 2):
//   voice.wake_word  ("Hey Zara")
/// A single device capability declaration.
class ZaraCapability {
  final String name;
  final bool supported;
  final String note;
  const ZaraCapability(this.name, this.supported, this.note);
}

/// Stage 9 Android body capabilities + Stage 10 physical marks (vivo V2338).
///
/// A capability flips to supported ONLY with hardware evidence behind it.
/// voice.wake_word stays false: no always-on DSP loop exists by design
/// (the phrase itself is verified through manual capture). Camera, screen,
/// location, bluetooth, accessibility stay false: not requested, never
/// permission-seeking, accessibility never a fallback. Nothing is faked.
const List<ZaraCapability> androidCapabilities = [
  ZaraCapability(
    'notification.receive',
    true,
    'FCM/system notifications (foundation)',
  ),
  ZaraCapability(
    'notification.display',
    true,
    'Local display of Core-pulled notifications (Stage 9)',
  ),
  ZaraCapability('battery.report', true, 'BatteryManager via native bridge'),
  ZaraCapability(
    'network.report',
    true,
    'ConnectivityManager via native bridge',
  ),
  ZaraCapability(
    'system.battery',
    true,
    'Serves Core system.battery jobs from bridge snapshots (Stage 9)',
  ),
  ZaraCapability(
    'system.network',
    true,
    'Serves Core system.network jobs from bridge snapshots (Stage 9)',
  ),
  ZaraCapability(
    'permissions.report',
    true,
    'Runtime permission states via bridge',
  ),
  ZaraCapability(
    'android.lifecycle',
    true,
    'Pair/restore/reconnect/revoke lifecycle (Stage 9)',
  ),
  ZaraCapability(
    'audio.capture.api',
    true,
    'Bounded native capture, signal PHYSICAL_VERIFIED (peak dBFS + live STT)',
  ),
  ZaraCapability(
    'audio.playback.api',
    true,
    'Native playback, software + human audibility PHYSICAL_VERIFIED',
  ),
  ZaraCapability(
    'push.mock',
    true,
    'Mock push transport; poll fallback is the reliable channel',
  ),
  // ---- physically verified in Stage 10 (vivo V2338, 2026-10-04) ----
  ZaraCapability(
    'voice.input',
    true,
    'Mic speech input: capture→STT→Core turn PHYSICAL_VERIFIED',
  ),
  ZaraCapability(
    'voice.output',
    true,
    'Spoken replies: Piper TTS→AudioTrack→heard PHYSICAL_VERIFIED',
  ),
  ZaraCapability(
    'assistant.role',
    true,
    'Default-assistant role via ASSIST proxy PHYSICAL_VERIFIED (session path NOT_SUPPORTED on OriginOS)',
  ),
  // ---- physically verified in Stage 15 (vivo V2338 ↔ laptop-1, 2026-10-04/05) ----
  ZaraCapability(
    'files.transfer',
    true,
    'Chunked byte transfer via Core relay: uploaded 3 payloads (text+64KB+1MB, SHA-256 verified) and fetched 4 (all recipient-verified) PHYSICAL_VERIFIED',
  ),
  // ---- Stage 20: observed-device discovery (detection ≠ trust) ----
  ZaraCapability(
    'android.observe',
    true,
    'Observed devices: StorageManager volumes, UsbManager listing, DisplayManager external displays; read-only, no permissions (detection is never trust)',
  ),
  ZaraCapability(
    'android.storage.stats',
    true,
    'Permission-gated media counts (MediaStore) + primary storage usage; counts only, no media read',
  ),
  // ---- Rung-1 lab pilot (ZaraLab emulator ONLY, 2026-10-06) ----
  // high_risk + human approval + device hardcoded allowlist; never Vivo.
  ZaraCapability(
    'android.app.force_stop',
    true,
    'Force-stop one allowlisted lab package via priv-app FORCE_STOP_PACKAGES (Rung-1 pilot, emulator lab only)',
  ),
  // ---- Rung-2 lab pilot (ZaraLab emulator ONLY, never Vivo) ----
  // confirm/high_risk + human approval + device hardcoded allowlist.
  ZaraCapability(
    'gui.screen.inspect',
    true,
    'Bounded a11y screen snapshot via opt-in AccessibilityService (Rung-2 pilot, emulator lab only)',
  ),
  ZaraCapability(
    'gui.tap',
    true,
    'Tap one snapshot node in an allowlisted lab package via opt-in AccessibilityService (Rung-2 pilot, emulator lab only)',
  ),
  ZaraCapability(
    'gui.set_text',
    true,
    'Type into an editable node in an allowlisted lab package via opt-in AccessibilityService (contract v2, emulator lab only; text never echoed)',
  ),
  ZaraCapability(
    'gui.scroll',
    true,
    'Scroll a scrollable node in an allowlisted lab package via opt-in AccessibilityService (contract v2, emulator lab only)',
  ),
  ZaraCapability(
    'gui.find',
    true,
    'Read-only semantic element lookup (resource_id/content_desc/text/role + index_hint) against the fresh a11y tree in an allowlisted lab package; never acts (contract v3, emulator lab only)',
  ),
  ZaraCapability(
    'gui.back',
    true,
    'Global back/home navigation guarded by the foreground package via opt-in AccessibilityService (universal operator, emulator lab only)',
  ),
  ZaraCapability(
    'gui.wait',
    true,
    'Bounded read-only wait for an element to appear on the fresh a11y tree in an allowlisted lab package (Stage 28, emulator lab only); timeout reports matched:false honestly, nothing is acted on',
  ),
  ZaraCapability(
    'android.apps.list',
    true,
    'Launchable-app discovery (package + label, capped): read-only metadata, never an authorization (universal operator)',
  ),
  ZaraCapability(
    'android.otp.read',
    true,
    'Bounded OTP scan over the FRESH notification ring (<= 5 min by default): per-use consent enforced at Core (HIGH risk approval parks EVERY run — ambient grants never carry it), shape-gated codes only, full bodies never leave the device, and memory + audit rows never store the code (universal operator)',
  ),
  ZaraCapability(
    'android.media.control',
    true,
    'TARGETED media control of ONE app playback session (play/pause/next/previous/stop through MediaSessionManager): the hint is required (no global blast), 0 or many matches are honest answers with a bounded listing, and the verdict reports before/after playback states of the matched session (universal operator)',
  ),
  ZaraCapability(
    'android.photos.inventory',
    true,
    'COUNT-ONLY inventory of camera-like photos (MediaStore metadata: counts, bytes, top-level folders — never file names, paths, or content), read-only by construction; the backup itself is the consented per-run operator PULL and nothing in this surface ever deletes (universal operator)',
  ),
  ZaraCapability(
    'android.notifications.read',
    true,
    'Passive read of captured notifications via opt-in Notification listener access (special access; scrubbed at capture, never polled) (universal operator)',
  ),
  // ---- Stage 31-P3 phase A: quick/one-shot phone actions ------------
  ZaraCapability(
    'android.torch.set',
    true,
    'Torch on/off via CameraManager.setTorchMode (no camera permission by platform design); verified by the torch-mode callback re-read (universal operator)',
  ),
  ZaraCapability(
    'android.volume.set',
    true,
    'Media-stream volume set (bounded 0-100), verified by getStreamVolume re-read; ringer/alarm streams untouched by default (universal operator)',
  ),
  ZaraCapability(
    'android.ring',
    true,
    'Find-my-phone ring: default alarm sound at max on STREAM_ALARM for a bounded 3-30 s; refuses honestly in DND without an explicit per-run user bypass (universal operator)',
  ),
  ZaraCapability(
    'android.media.playback',
    true,
    'Media transport via media key events (play/pause/stop/next/previous); honest sent-only verdict — no per-session verify channel exists (universal operator)',
  ),
  ZaraCapability(
    'android.clipboard.set',
    true,
    'Clipboard PUSH laptop→phone: primary-clip write verified by re-read (text compared verbatim, never echoed); Android 10+ focus rule reported plainly when refused; the phone→laptop direction is restricted by the OS and out of scope (universal operator)',
  ),
  ZaraCapability(
    'android.open_uri',
    true,
    'Open a link on the phone: VIEW intent after handler presence is verified; schemes bounded to ordinary view types; per-link consent lives in Core (universal operator)',
  ),
  ZaraCapability(
    'android.power_saver.set',
    true,
    'Battery-saver toggle via WRITE_SECURE_SETTINGS (one-time T1 adb grant per SYSCONTROL_MATRIX); verified by PowerManager + Settings re-read (universal operator)',
  ),
  ZaraCapability(
    'android.alarm.set',
    true,
    'Alarm through ACTION_SET_ALARM: clock opens pre-filled and the USER completes it (skip_ui refused structurally); honest handed-to-clock verdict (universal operator)',
  ),
  ZaraCapability(
    'android.timer.set',
    true,
    'Countdown timer through ACTION_SET_TIMER: clock app opens pre-filled (minutes required) and the USER completes it (skip_ui refused structurally); honest handed-to-clock verdict (universal operator)',
  ),
  // ---- P5 feature-hunt ② (2026-10-22): DND state + verified toggle ---
  ZaraCapability(
    'android.dnd.get',
    true,
    'Do-Not-Disturb state read via NotificationManager: current interruption filter (named) + whether Zara holds the policy access the setter needs; read-only (universal operator)',
  ),
  ZaraCapability(
    'android.dnd.set',
    true,
    'Do-Not-Disturb toggle via setInterruptionFilter: requires the ACCESS_NOTIFICATION_POLICY special access the user grants ONCE in Settings; honest refusal (nothing changed) without it; enabled/disabled verified by a filter re-read (universal operator)',
  ),
  // ---- Stage 31-P3 phase B (P3d): call with contact resolution -------
  ZaraCapability(
    'android.contacts.lookup',
    true,
    'Bounded contact lookup by display name: returns name+number only, ZERO or MULTIPLE matches are answers (never guesses); READ_CONTACTS requested once at surface enablement (universal operator)',
  ),
  ZaraCapability(
    'android.call.place',
    true,
    'Direct call (ACTION_CALL) of a CONFIRMED contact number only: per-call approval in Core shows name+number, the dial-grammar re-check refuses anything from untrusted text, and the verdict is a bounded call-screen a11y poll — a fired intent is not proof (universal operator)',
  ),
  ZaraCapability(
    'android.sms.send',
    true,
    'ONE carrier SMS through SmsManager on the CONFIRMED recipient only: per-send approval shows the full draft (the Effect line), text length capped at 400, the bridge refuses dial-grammar violations, and the result is length-only — the honest "sent" verdict means the radio accepted, carrier delivery is not confirmable (universal operator)',
  ),
  // ---- Phase 1: system-control (read-only states + settings/app open) ----
  // States are ordinary-permission reads; toggles are Settings-intent only
  // (user confirms in OS UI). Hotspot reports honest unsupported (no public
  // API); charging_time is a null-estimate guard (no invented durations).
  ZaraCapability(
    'system.battery.charging_time',
    true,
    'Charging-time estimate guard: single snapshot cannot estimate; returns null estimate honestly (Phase 1)',
  ),
  ZaraCapability(
    'system.wifi.state',
    true,
    'WiFi state + connected SSID via WifiManager/ConnectivityManager, read-only (Phase 1)',
  ),
  ZaraCapability(
    'system.wifi.settings',
    true,
    'Open system WiFi settings screen, user-confirmed (Phase 1)',
  ),
  ZaraCapability(
    'system.bluetooth.state',
    true,
    'Bluetooth state via BluetoothManager, read-only; honest unavailable without BLUETOOTH_CONNECT (Phase 1)',
  ),
  ZaraCapability(
    'system.bluetooth.settings',
    true,
    'Open system Bluetooth settings screen, user-confirmed (Phase 1)',
  ),
  ZaraCapability(
    'system.hotspot.state',
    true,
    'Hotspot state: honest unsupported, no public SoftAp API (Phase 1)',
  ),
  ZaraCapability(
    'system.hotspot.settings',
    true,
    'Open system hotspot settings screen, user-confirmed (Phase 1)',
  ),
  ZaraCapability(
    'system.mobile_data.state',
    true,
    'Mobile data state via TelephonyManager/ConnectivityManager, read-only (Phase 1)',
  ),
  ZaraCapability(
    'system.mobile_data.settings',
    true,
    'Open system mobile data settings screen, user-confirmed (Phase 1)',
  ),
  ZaraCapability(
    'system.airplane_mode.state',
    true,
    'Airplane mode state via Settings.Global, read-only (Phase 1)',
  ),
  ZaraCapability(
    'system.airplane_mode.settings',
    true,
    'Open system airplane mode settings screen, user-confirmed (Phase 1)',
  ),
  ZaraCapability(
    'system.app.open',
    true,
    'Open one allowlisted package via launch intent (Phase 1)',
  ),
  // ---- Phase 2: tiered state changes (verified or honest) ----
  // applied:true only after native post-action verification; otherwise
  // settings-UI/consent/deployment pointers with applied:false.
  ZaraCapability(
    'system.wifi.set',
    true,
    'Change WiFi state: direct on legacy platform, else WiFi settings UI + verify (Phase 2)',
  ),
  ZaraCapability(
    'system.bluetooth.set',
    true,
    'Change Bluetooth state: consent dialog for enable, direct attempt for disable, else settings UI + verify (Phase 2)',
  ),
  ZaraCapability(
    'system.hotspot.set',
    true,
    'Change hotspot state: lab companion direct path, else tether settings UI + verify (Phase 2)',
  ),
  ZaraCapability(
    'system.mobile_data.set',
    true,
    'Change mobile-data state: settings UI + verify, no silent path by design (Phase 2)',
  ),
  ZaraCapability(
    'system.airplane_mode.set',
    true,
    'Change airplane mode: direct with ADB-granted permission, else settings UI + verify (Phase 2)',
  ),
  // ---- reserved / by design ----
  ZaraCapability(
    'voice.wake_word',
    false,
    'Reserved wake phrase: "Hey Zara" — verified via manual capture only; no always-on DSP loop by design',
  ),
  ZaraCapability(
    'camera.available',
    false,
    'Camera: not requested, no permission sought (by design)',
  ),
  ZaraCapability(
    'screen.capture',
    false,
    'Screen context: assist screenshot toggle OFF; never read (by design)',
  ),
  ZaraCapability(
    'location.available',
    false,
    'Location: not requested, no permission sought (by design)',
  ),
  ZaraCapability('bluetooth.available', false, 'Bluetooth: unused (by design)'),
  ZaraCapability(
    'accessibility.automation',
    false,
    'UI automation: never — not a fallback for assistant integration (by design)',
  ),
];

/// Names the core device registry receives (supported ones only).
List<String> advertisedCapabilities() =>
    androidCapabilities.where((c) => c.supported).map((c) => c.name).toList();
