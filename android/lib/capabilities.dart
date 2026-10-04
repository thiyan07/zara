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
