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

/// Stage 9 Android body capabilities.
///
/// Implemented now: notification/battery/network/permission reporting,
/// device lifecycle, notification display, bounded audio capture/playback
/// API paths, mock push. Voice engines (wake/STT/TTS) stay false until
/// provider-backed builds land WITH physical validation — an API path is
/// not a validated voice. Nothing is faked.
const List<ZaraCapability> androidCapabilities = [
  ZaraCapability('notification.receive', true, 'FCM/system notifications (foundation)'),
  ZaraCapability('notification.display', true, 'Local display of Core-pulled notifications (Stage 9)'),
  ZaraCapability('battery.report', true, 'BatteryManager via native bridge'),
  ZaraCapability('network.report', true, 'ConnectivityManager via native bridge'),
  ZaraCapability('system.battery', true, 'Serves Core system.battery jobs from bridge snapshots (Stage 9)'),
  ZaraCapability('system.network', true, 'Serves Core system.network jobs from bridge snapshots (Stage 9)'),
  ZaraCapability('permissions.report', true, 'Runtime permission states via bridge'),
  ZaraCapability('android.lifecycle', true, 'Pair/restore/reconnect/revoke lifecycle (Stage 9)'),
  ZaraCapability('audio.capture.api', true, 'Bounded native capture API path (signal: PHYSICAL_VALIDATION_PENDING)'),
  ZaraCapability('audio.playback.api', true, 'Native playback API path (audibility: manual-only)'),
  ZaraCapability('push.mock', true, 'Mock push transport; poll fallback is the reliable channel'),
  // ---- reserved ----
  ZaraCapability('voice.wake_word', false, 'Reserved wake phrase: "Hey Zara" (engine: PHYSICAL_VALIDATION_PENDING)'),
  ZaraCapability('voice.input', false, 'Microphone speech input (STT path deferred)'),
  ZaraCapability('voice.output', false, 'Speech synthesis output (audibility deferred)'),
  ZaraCapability('camera.available', false, 'Camera (Stage 3)'),
  ZaraCapability('screen.capture', false, 'Screen context (Stage 3)'),
  ZaraCapability('location.available', false, 'Location (Stage 3+)'),
  ZaraCapability('bluetooth.available', false, 'Bluetooth (Stage 3+)'),
  ZaraCapability('accessibility.automation', false, 'UI automation (Stage 3+)'),
  ZaraCapability('assistant.role', false, 'Assistant-role integration (Stage 3+)'),
];

/// Names the core device registry receives (supported ones only).
List<String> advertisedCapabilities() =>
    androidCapabilities.where((c) => c.supported).map((c) => c.name).toList();
