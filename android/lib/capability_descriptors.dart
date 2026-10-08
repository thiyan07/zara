// Zara Android capability descriptors — pure Dart (no Flutter dep).
//
// Stage 16: versioned descriptor docs built ONLY from capabilities the
// device actually implements (androidCapabilities, supported=true).
// Reserved (supported=false) entries never get descriptors — nothing is
// claimed that isn't implemented. Availability is honest: permission- or
// hardware-gated entries report os_denied/unavailable with a reason.
// Descriptions are untrusted metadata Core-side; risks here are
// advisory (Policy + Governor decide at runtime, always).
import 'capabilities.dart';

const _descriptorVersion = '1';

/// Advisory risk per capability (Core policy is authoritative).
const Map<String, String> _riskOf = {
  'notification.receive': 'safe',
  'notification.display': 'safe',
  'battery.report': 'safe',
  'network.report': 'safe',
  'system.battery': 'safe',
  'system.network': 'safe',
  'permissions.report': 'safe',
  'android.lifecycle': 'safe',
  'audio.capture.api': 'confirm',
  'audio.playback.api': 'confirm',
  'push.mock': 'safe',
  'voice.input': 'confirm',
  'voice.output': 'confirm',
  'assistant.role': 'safe',
  'files.transfer': 'confirm',
  // Rung-1 pilot: advisory only (Core policy decides). Lab emulator only.
  'android.app.force_stop': 'high_risk',
  // Rung-2 pilot: advisory only (Core policy decides). Lab emulator only.
  'gui.screen.inspect': 'confirm',
  'gui.tap': 'high_risk',
};

/// Android runtime permissions backing gated capabilities.
const Map<String, List<String>> _requiresOf = {
  'audio.capture.api': ['RECORD_AUDIO'],
  'voice.input': ['RECORD_AUDIO'],
  'notification.display': ['POST_NOTIFICATIONS'],
  // Priv-app grant (image allowlist), not a runtime permission.
  'android.app.force_stop': ['FORCE_STOP_PACKAGES'],
  // Rung-2 pilot: no runtime permission backing; the operator enables the
  // AccessibilityService in Settings and the inspect call reports state.
  'gui.screen.inspect': [],
  'gui.tap': [],
};

const _foregroundOf = {
  'audio.capture.api',
  'voice.input',
};

/// Build describe documents for every truly-supported capability.
/// [micPermission]/[notifPermission] drive honest availability; anything
/// else stays available (no permission model on Core's side to fake).
/// [forceStopGranted] gates the Rung-1 pilot capability truthfully.
/// [a11yReady] gates the Rung-2 pilot capabilities on the real probe
/// (service enabled + bound + root present) — never assumed.
List<Map<String, dynamic>> androidCapabilityDocs({
  required bool micPermission,
  required bool notifPermission,
  bool forceStopGranted = false,
  bool a11yReady = false,
}) {
  final docs = <Map<String, dynamic>>[];
  for (final c in androidCapabilities) {
    if (!c.supported) continue; // reserved: never advertised
    var availability = 'available';
    var reason = '';
    String? permState;
    if (c.name == 'audio.capture.api' || c.name == 'voice.input') {
      permState = micPermission ? null : 'denied';
    } else if (c.name == 'notification.display') {
      permState = notifPermission ? null : 'denied';
    } else if (c.name == 'android.app.force_stop') {
      permState = forceStopGranted ? null : 'denied';
    } else if (c.name == 'gui.screen.inspect' || c.name == 'gui.tap') {
      if (!a11yReady) {
        availability = 'unavailable';
        reason = 'accessibility service not enabled';
      }
    }
    if (permState == 'denied') {
      availability = 'os_denied';
      reason = 'OS permission not granted';
    }
    docs.add({
      'id': c.name,
      'descriptor_version': _descriptorVersion,
      'name': c.name,
      'version': '1.0.0',
      'description': _ascii(c.note.take(500)),
      'risk': _riskOf[c.name] ?? 'safe',
      'requires': _requiresOf[c.name] ?? [],
      'platforms': ['android'],
      // files.transfer moves bytes through the Core relay (Stage 15);
      // everything else executes in the app process itself.
      'execution':
          c.name == 'files.transfer' ? 'core-mediated' : 'on-device',
      'requires_foreground': _foregroundOf.contains(c.name),
      'requires_network': false,
      'battery_sensitive': false,
      'supports_cancellation': false,
      'supports_streaming': false,
      'timeout_s': 30,
      'input_schema': {'type': 'object'},
      'output_schema': {'type': 'object'},
      'availability': availability,
      'availability_reason': reason,
      if (permState != null) 'os_permission_granted': permState != 'denied',
      'aliases': [for (final a in _aliasesOf(c.name)) _ascii(a)],
      'examples': const [],
      'keywords': const [],
    });
  }
  return docs;
}

/// HttpClientRequest.write encodes strings as latin-1: any non-latin1
/// codepoint (e.g. the → in voice notes) throws "Contains invalid
/// characters" client-side and the describe never leaves the phone.
/// Sanitize wire text to printable ASCII (in-app notes stay untouched).
String _ascii(String s) {
  final out = StringBuffer();
  for (final c in s.codeUnits) {
    if (c == 0x2192) {
      out.write('->');
    } else if (c >= 0x20 && c < 0x7F) {
      out.writeCharCode(c);
    } else if (c == 0x2014 || c == 0x2013) {
      out.write('-');
    } else if (c > 0x7F) {
      out.write('?');
    }
  }
  return out.toString();
}

List<String> _aliasesOf(String name) {
  switch (name) {
    case 'system.battery':
      return ['battery level', 'battery percentage'];
    case 'system.network':
      return ['network status', 'connectivity'];
    case 'voice.input':
      return ['listen', 'hear'];
    case 'voice.output':
      return ['speak', 'say'];
    case 'files.transfer':
      return ['send file', 'share file'];
    case 'android.app.force_stop':
      return ['force stop app', 'kill app'];
    case 'gui.screen.inspect':
      return ['inspect screen'];
    case 'gui.tap':
      return ['tap'];
    default:
      return [];
  }
}

extension _TakeStr on String {
  String take(int n) => length <= n ? this : substring(0, n);
}
