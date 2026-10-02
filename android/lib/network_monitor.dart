// Android network awareness — truthful connectivity states.
//
// States: connected | metered | disconnected | reconnecting | unavailable.
// Offline behavior: UI shows it, retries stop, hosted paths fail loudly,
// local paths (Piper, queued heartbeats) continue. Pure Dart.
class NetworkState {
  static const connected = 'connected';
  static const metered = 'metered';
  static const disconnected = 'disconnected';
  static const reconnecting = 'reconnecting';
  static const unavailable = 'unavailable';
}

class NetworkSnapshot {
  final String state;
  final bool metered;
  final DateTime timestamp;
  const NetworkSnapshot(this.state,
      {this.metered = false, required this.timestamp});

  static NetworkSnapshot fromBridge(Map<String, dynamic> m) {
    final net = '${m['network'] ?? 'unknown'}';
    final met = m['metered'] == true;
    final state = switch (net) {
      'offline' => NetworkState.disconnected,
      'metered' => NetworkState.metered,
      'wifi' || 'online' => met ? NetworkState.metered : NetworkState.connected,
      _ => NetworkState.unavailable,
    };
    return NetworkSnapshot(state, metered: met, timestamp: DateTime.now());
  }

  bool get online =>
      state == NetworkState.connected || state == NetworkState.metered;
  bool get offline => !online;

  /// Truthful user-facing line. Hosted failures must read like this, never
  /// like success.
  String describe() => switch (state) {
        NetworkState.connected => 'Online',
        NetworkState.metered => 'Online (metered — heavy work reduced)',
        NetworkState.disconnected => 'Offline — Zara Core unreachable',
        NetworkState.reconnecting => 'Reconnecting…',
        _ => 'Network unavailable',
      };
}
