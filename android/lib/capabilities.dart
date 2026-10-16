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
  ZaraCapability('notification.receive', true, 'FCM/system notifications (foundation)'),
  ZaraCapability('notification.display', true, 'Local display of Core-pulled notifications (Stage 9)'),
  ZaraCapability('battery.report', true, 'BatteryManager via native bridge'),
  ZaraCapability('network.report', true, 'ConnectivityManager via native bridge'),
  ZaraCapability('system.battery', true, 'Serves Core system.battery jobs from bridge snapshots (Stage 9)'),
  ZaraCapability('system.network', true, 'Serves Core system.network jobs from bridge snapshots (Stage 9)'),
  ZaraCapability('permissions.report', true, 'Runtime permission states via bridge'),
  ZaraCapability('android.lifecycle', true, 'Pair/restore/reconnect/revoke lifecycle (Stage 9)'),
  ZaraCapability('audio.capture.api', true, 'Bounded native capture, signal PHYSICAL_VERIFIED (peak dBFS + live STT)'),
  ZaraCapability('audio.playback.api', true, 'Native playback, software + human audibility PHYSICAL_VERIFIED'),
  ZaraCapability('push.mock', true, 'Mock push transport; poll fallback is the reliable channel'),
  // ---- physically verified in Stage 10 (vivo V2338, 2026-10-04) ----
  ZaraCapability('voice.input', true, 'Mic speech input: capture→STT→Core turn PHYSICAL_VERIFIED'),
  ZaraCapability('voice.output', true, 'Spoken replies: Piper TTS→AudioTrack→heard PHYSICAL_VERIFIED'),
  ZaraCapability('assistant.role', true, 'Default-assistant role via ASSIST proxy PHYSICAL_VERIFIED (session path NOT_SUPPORTED on OriginOS)'),
  // ---- physically verified in Stage 15 (vivo V2338 ↔ laptop-1, 2026-10-04/05) ----
  ZaraCapability('files.transfer', true, 'Chunked byte transfer via Core relay: uploaded 3 payloads (text+64KB+1MB, SHA-256 verified) and fetched 4 (all recipient-verified) PHYSICAL_VERIFIED'),
  // ---- Rung-1 lab pilot (ZaraLab emulator ONLY, 2026-10-06) ----
  // high_risk + human approval + device hardcoded allowlist; never Vivo.
  ZaraCapability('android.app.force_stop', true, 'Force-stop one allowlisted lab package via priv-app FORCE_STOP_PACKAGES (Rung-1 pilot, emulator lab only)'),
  // ---- Rung-2 lab pilot (ZaraLab emulator ONLY, never Vivo) ----
  // confirm/high_risk + human approval + device hardcoded allowlist.
  ZaraCapability('gui.screen.inspect', true, 'Bounded a11y screen snapshot via opt-in AccessibilityService (Rung-2 pilot, emulator lab only)'),
  ZaraCapability('gui.tap', true, 'Tap one snapshot node in an allowlisted lab package via opt-in AccessibilityService (Rung-2 pilot, emulator lab only)'),
  // ---- Phase 1: system-control (read-only states + settings/app open) ----
  // States are ordinary-permission reads; toggles are Settings-intent only
  // (user confirms in OS UI). Hotspot reports honest unsupported (no public
  // API); charging_time is a null-estimate guard (no invented durations).
  ZaraCapability('system.battery.charging_time', true, 'Charging-time estimate guard: single snapshot cannot estimate; returns null estimate honestly (Phase 1)'),
  ZaraCapability('system.wifi.state', true, 'WiFi state + connected SSID via WifiManager/ConnectivityManager, read-only (Phase 1)'),
  ZaraCapability('system.wifi.settings', true, 'Open system WiFi settings screen, user-confirmed (Phase 1)'),
  ZaraCapability('system.bluetooth.state', true, 'Bluetooth state via BluetoothManager, read-only; honest unavailable without BLUETOOTH_CONNECT (Phase 1)'),
  ZaraCapability('system.bluetooth.settings', true, 'Open system Bluetooth settings screen, user-confirmed (Phase 1)'),
  ZaraCapability('system.hotspot.state', true, 'Hotspot state: honest unsupported, no public SoftAp API (Phase 1)'),
  ZaraCapability('system.hotspot.settings', true, 'Open system hotspot settings screen, user-confirmed (Phase 1)'),
  ZaraCapability('system.mobile_data.state', true, 'Mobile data state via TelephonyManager/ConnectivityManager, read-only (Phase 1)'),
  ZaraCapability('system.mobile_data.settings', true, 'Open system mobile data settings screen, user-confirmed (Phase 1)'),
  ZaraCapability('system.airplane_mode.state', true, 'Airplane mode state via Settings.Global, read-only (Phase 1)'),
  ZaraCapability('system.airplane_mode.settings', true, 'Open system airplane mode settings screen, user-confirmed (Phase 1)'),
  ZaraCapability('system.app.open', true, 'Open one allowlisted package via launch intent (Phase 1)'),
  // ---- Phase 2: tiered state changes (verified or honest) ----
  // applied:true only after native post-action verification; otherwise
  // settings-UI/consent/deployment pointers with applied:false.
  ZaraCapability('system.wifi.set', true, 'Change WiFi state: direct on legacy platform, else WiFi settings UI + verify (Phase 2)'),
  ZaraCapability('system.bluetooth.set', true, 'Change Bluetooth state: consent dialog for enable, direct attempt for disable, else settings UI + verify (Phase 2)'),
  ZaraCapability('system.hotspot.set', true, 'Change hotspot state: lab companion direct path, else tether settings UI + verify (Phase 2)'),
  ZaraCapability('system.mobile_data.set', true, 'Change mobile-data state: settings UI + verify, no silent path by design (Phase 2)'),
  ZaraCapability('system.airplane_mode.set', true, 'Change airplane mode: direct with ADB-granted permission, else settings UI + verify (Phase 2)'),
  // ---- reserved / by design ----
  ZaraCapability('voice.wake_word', false, 'Reserved wake phrase: "Hey Zara" — verified via manual capture only; no always-on DSP loop by design'),
  ZaraCapability('camera.available', false, 'Camera: not requested, no permission sought (by design)'),
  ZaraCapability('screen.capture', false, 'Screen context: assist screenshot toggle OFF; never read (by design)'),
  ZaraCapability('location.available', false, 'Location: not requested, no permission sought (by design)'),
  ZaraCapability('bluetooth.available', false, 'Bluetooth: unused (by design)'),
  ZaraCapability('accessibility.automation', false, 'UI automation: never — not a fallback for assistant integration (by design)'),
];

/// Names the core device registry receives (supported ones only).
List<String> advertisedCapabilities() =>
    androidCapabilities.where((c) => c.supported).map((c) => c.name).toList();
