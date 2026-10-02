// Zara Android device lifecycle — the BODY's onboarding/presence story.
//
// States mirror the Core presence model (registered|online|offline|
// degraded|reconnecting) plus onboarding (pairing/enrolled/registering)
// and terminal-ish (revoked/logged-out/error). Pure Dart: persistence and
// network are injected so every path is unit-testable. Only the device ID
// (public) is persisted here; the device KEY lives in SecureStore.
class DeviceLifecycleState {
  static const uninitialized = 'uninitialized';
  static const pairing = 'pairing';
  static const enrolled = 'enrolled';
  static const registering = 'registering';
  static const online = 'online';
  static const degraded = 'degraded';
  static const reconnecting = 'reconnecting';
  static const revoked = 'revoked';
  static const loggedOut = 'logged-out';
  static const error = 'error';

  static const allowed = {
    uninitialized: {pairing, registering},
    pairing: {enrolled, error, uninitialized},
    enrolled: {registering, pairing, error},
    registering: {online, error, pairing, revoked},
    online: {degraded, reconnecting, loggedOut, error, revoked},
    degraded: {online, reconnecting, loggedOut, error, revoked},
    reconnecting: {online, degraded, registering, error, revoked},
    revoked: {pairing},
    loggedOut: {pairing},
    error: {pairing, registering, reconnecting, uninitialized},
  };
}

/// Minimal durable record: device ID only. No keys, no tokens, no audio.
class LifecycleSnapshot {
  final String? deviceId;
  final String state;
  const LifecycleSnapshot({this.deviceId, this.state = DeviceLifecycleState.uninitialized});

  Map<String, dynamic> toMap() => {'device_id': deviceId, 'state': state};
  static LifecycleSnapshot fromMap(Map<String, dynamic> m) => LifecycleSnapshot(
      deviceId: m['device_id'] as String?, state: '${m['state']}');
}

class DeviceLifecycle {
  String state = DeviceLifecycleState.uninitialized;
  String? deviceId;
  String lastError = '';
  final List<String> transitions = [];

  void Function(String state)? onChange;

  void move(String to) {
    if (!(DeviceLifecycleState.allowed[state]?.contains(to) ?? false)) {
      throw StateError('illegal lifecycle transition $state -> $to');
    }
    state = to;
    transitions.add(to);
    onChange?.call(to);
  }

  /// Cold start: stored ID exists -> try registering; else -> pairing.
  /// Returns the snapshot the app should persist (ID only).
  LifecycleSnapshot restore(String? storedDeviceId) {
    if (storedDeviceId == null || storedDeviceId.isEmpty) {
      deviceId = null;
      return const LifecycleSnapshot();
    }
    deviceId = storedDeviceId;
    move(DeviceLifecycleState.registering);
    return LifecycleSnapshot(deviceId: deviceId, state: state);
  }

  /// Maps an HTTP outcome to the next lifecycle step. 401/403 with a
  /// stored identity means revoked server-side -> safe state, wipe keys
  /// (caller clears SecureStore). 404 means unknown -> re-register.
  /// Never loops: each call moves at most one step.
  String handleHttp(int statusCode, {required bool hasIdentity}) {
    if (statusCode == 401 || statusCode == 403) {
      if (hasIdentity && (state == DeviceLifecycleState.online ||
          state == DeviceLifecycleState.degraded ||
          state == DeviceLifecycleState.reconnecting ||
          state == DeviceLifecycleState.registering)) {
        move(DeviceLifecycleState.revoked);
        return 'revoked';
      }
      move(DeviceLifecycleState.error);
      lastError = 'auth $statusCode';
      return 'error';
    }
    if (statusCode == 404) return 'reregister';
    if (statusCode >= 500) {
      if (state == DeviceLifecycleState.online) {
        move(DeviceLifecycleState.reconnecting);
        return 'reconnecting';
      }
      return state;
    }
    return state;
  }

  /// Explicit user logout: keys wiped by caller, back to pairing.
  /// User intent is the one escape hatch that bypasses the table: from any
  /// state the user can always return to pairing. Recorded, never silent.
  void logout() {
    deviceId = null;
    lastError = '';
    // From any live state the user can always return to pairing.
    state = DeviceLifecycleState.loggedOut;
    transitions.add(DeviceLifecycleState.loggedOut);
    onChange?.call(state);
  }

  LifecycleSnapshot snapshot() =>
      LifecycleSnapshot(deviceId: deviceId, state: state);
}
