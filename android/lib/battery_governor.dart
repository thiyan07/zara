// Android battery governor mapping — advisory ONLY.
//
// Levels are derived from the SAME thresholds Core uses (governor
// LOW_BATTERY 15, CAUTION 30; server degraded <15, resume >=25), so the
// phone and Core agree. This file never ALLOWS or DENIES anything; Core
// policy/governor remain the sole authority. Pure Dart.
class AndroidBatteryLevel {
  static const healthy = 'healthy';
  static const caution = 'caution';
  static const low = 'low';
  static const critical = 'critical';

  static const criticalBelow = 15.0;
  static const resumeAbove = 25.0;
  static const cautionBelow = 30.0;
}

/// Full telemetry report. Extra fields (source/health/timestamp) ride
/// along for the UI; heartbeat sends the Core-schema subset.
class BatteryReport {
  final double? pct;
  final bool charging;
  final String source; // 'battery' | 'ac' | 'usb' | 'wireless' | 'unknown'
  final String health; // platform string or 'unknown'
  final DateTime timestamp;
  final bool powerSave;
  const BatteryReport({
    this.pct,
    this.charging = false,
    this.source = 'unknown',
    this.health = 'unknown',
    required this.timestamp,
    this.powerSave = false,
  });

  static BatteryReport fromBridge(Map<String, dynamic> m) {
    final v = m['battery_pct'];
    return BatteryReport(
      pct: v is num ? v.toDouble() : null,
      charging: m['charging'] == true,
      source: '${m['source'] ?? 'unknown'}',
      health: '${m['health'] ?? 'unknown'}',
      timestamp: DateTime.now(),
      powerSave: m['power_save'] == true,
    );
  }

  /// Core heartbeat subset (HeartbeatPayload schema).
  Map<String, dynamic> toHeartbeat() =>
      {'battery_pct': pct, 'charging': charging};

  String level() {
    if (pct == null) return AndroidBatteryLevel.healthy;
    if (charging) return AndroidBatteryLevel.healthy;
    if (pct! < AndroidBatteryLevel.criticalBelow) {
      return AndroidBatteryLevel.critical;
    }
    if (pct! < AndroidBatteryLevel.resumeAbove) return AndroidBatteryLevel.low;
    if (pct! < AndroidBatteryLevel.cautionBelow) {
      return AndroidBatteryLevel.caution;
    }
    return AndroidBatteryLevel.healthy;
  }

  /// Advisory hints for LOCAL thrift (fewer polls/wake). Core decides.
  bool get reduceBackgroundWork {
    final l = level();
    return l == AndroidBatteryLevel.low ||
        l == AndroidBatteryLevel.critical ||
        powerSave;
  }

  bool get wakeAllowed {
    final l = level();
    return l != AndroidBatteryLevel.critical && !powerSave;
  }
}
