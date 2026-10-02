// Push abstraction — registration/token lifecycle + fallback contract.
//
// Real FCM delivery needs operator credentials that are NOT in this repo,
// so the bundled transport is a mock that records intent. The CONTRACT is
// what matters: token register/rotate/invalidate, invalid-token handling,
// dedup by event, and poll fallback (jobs/poll + notifications pull) as the
// reliable channel. Status: FCM_PHYSICAL/PROVIDER_VALIDATION_DEFERRED.
abstract class PushTransport {
  Future<void> deliver(Map<String, dynamic> payload);
  Future<String?> currentToken();
}

class MockPushTransport implements PushTransport {
  final List<Map<String, dynamic>> delivered = [];
  String? token;
  @override
  Future<void> deliver(Map<String, dynamic> payload) async {
    delivered.add(Map<String, dynamic>.from(payload));
  }

  @override
  Future<String?> currentToken() async => token;
}

class PushProvider {
  final PushTransport transport;
  String? _token;
  String? _deviceId;
  final Set<String> _seenEvents = {};

  PushProvider(this.transport);

  String? get token => _token;

  /// Token rules mirror the server (non-empty, <=512 chars).
  Future<void> register(String deviceId, String token) async {
    if (token.isEmpty || token.length > 512) {
      throw ArgumentError('invalid push token');
    }
    _deviceId = deviceId;
    _token = token;
  }

  /// Rotation: old token is forgotten the moment the new one lands.
  Future<void> rotate(String newToken) async {
    if (_deviceId == null) throw StateError('no device registered');
    _token = null;
    await register(_deviceId!, newToken);
  }

  Future<void> logout() async {
    _token = null;
    _deviceId = null;
    _seenEvents.clear();
  }

  /// Inbound message: dedup by event_id, strip to safe fields, hand the
  /// APP a refresh hint (the app pulls real state from Core — push
  /// payloads are never trusted content).
  bool onMessage(Map<String, dynamic> m) {
    final eventId = '${m['event_id'] ?? ''}';
    if (eventId.isEmpty || _seenEvents.contains(eventId)) return false;
    _seenEvents.add(eventId);
    if (_seenEvents.length > 500) {
      _seenEvents.remove(_seenEvents.first);
    }
    return true; // true => caller should refresh from Core
  }
}
