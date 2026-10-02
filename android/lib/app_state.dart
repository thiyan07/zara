// Unified UI state — presentation ONLY, zero authority.
//
// This object answers "what should the screen say?" and nothing else.
// It holds no credentials (compile-time: no key/token fields exist),
// no audio bytes, no transcripts. Restoration round-trips the public
// device ID + display prefs; secrets rehydrate from SecureStore.
class ZaraUiState {
  final String connection; // connection.dart CoreLinkState values
  final String lifecycle; // device_lifecycle.dart values
  final String? deviceId;
  final double? batteryPct;
  final bool charging;
  final String batteryLevel; // battery_governor.dart levels
  final String network; // network_monitor.dart states
  final String voice; // voice_session.dart states
  final bool approvalPending;
  final String lastError;
  final DateTime updatedAt;

  const ZaraUiState({
    this.connection = 'disconnected',
    this.lifecycle = 'uninitialized',
    this.deviceId,
    this.batteryPct,
    this.charging = false,
    this.batteryLevel = 'healthy',
    this.network = 'unavailable',
    this.voice = 'idle',
    this.approvalPending = false,
    this.lastError = '',
    required this.updatedAt,
  });

  /// Truthful one-line status. Offline/degraded always win over detail.
  String statusLine() {
    if (lifecycle == 'revoked') return 'Revoked — pair again';
    if (lifecycle == 'logged-out') return 'Signed out';
    if (connection == 'offline' || network == 'disconnected') {
      return 'Offline — showing last known state';
    }
    if (connection == 'reconnecting') return 'Reconnecting…';
    if (voice == 'paused_battery') return 'Voice paused (low battery)';
    if (approvalPending) return 'Approval required';
    if (lifecycle == 'online' || connection == 'online') {
      return deviceId == null ? 'Online' : 'Online as $deviceId';
    }
    if (lastError.isNotEmpty) return 'Error: $lastError';
    return 'Starting…';
  }

  Map<String, dynamic> toRestoreMap() => {
        'device_id': deviceId,
        'lifecycle': lifecycle,
      };

  static ZaraUiState fromRestoreMap(Map<String, dynamic> m) => ZaraUiState(
        deviceId: m['device_id'] as String?,
        lifecycle: '${m['lifecycle'] ?? 'uninitialized'}',
        updatedAt: DateTime.now(),
      );
}
