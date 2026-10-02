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

/// Stage 2 Android foundation capabilities.
///
/// Implemented now: notification, battery/network reporting, permission
/// state reporting. Everything else is reserved with supported=false,
/// including the "Hey Zara" wake word (voice engine is Stage 3+).
const List<ZaraCapability> androidCapabilities = [
  ZaraCapability('notification.receive', true, 'FCM/system notifications (foundation)'),
  ZaraCapability('battery.report', true, 'BatteryManager via native bridge'),
  ZaraCapability('network.report', true, 'ConnectivityManager via native bridge'),
  ZaraCapability('permissions.report', true, 'Runtime permission states via bridge'),
  // ---- reserved (Stage 3+) ----
  ZaraCapability('voice.wake_word', false, 'Reserved wake phrase: "Hey Zara" (not implemented)'),
  ZaraCapability('voice.input', false, 'Microphone speech input (Stage 3)'),
  ZaraCapability('voice.output', false, 'Speech synthesis (Stage 3)'),
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
