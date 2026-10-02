// Zara notification layer — display + Core-routed actions.
//
// Channels: approvals (high), missions (default), status (low). Bodies are
// truncated; secret patterns are scrubbed before display AND before any
// transport. Actions NEVER execute locally: approve/deny produce Core
// endpoint calls (device-scoped, audited server-side); anything else opens
// the app. Duplicate deliveries are dropped by ID. Pure Dart.
class ZaraChannel {
  static const approvals = 'zara_approvals';
  static const missions = 'zara_missions';
  static const status = 'zara_status';
}

class DeviceNotification {
  final String id;
  final String title;
  final String body;
  final String? missionId;
  final String executionId;
  final String channel;
  const DeviceNotification({
    required this.id,
    required this.title,
    required this.body,
    this.missionId,
    this.executionId = '',
    this.channel = ZaraChannel.status,
  });

  static DeviceNotification fromJson(Map<String, dynamic> m) {
    final id = m['id'];
    if (id is! String || id.isEmpty) throw FormatException('bad notif id');
    final hasMission = m['mission_id'] != null;
    final title = '${m['title'] ?? ''}';
    final needsApproval = title.toLowerCase().contains('approv');
    return DeviceNotification(
      id: id,
      title: scrub(title).take(120),
      body: scrub('${m['body'] ?? ''}').take(500),
      missionId: hasMission ? '${m['mission_id']}' : null,
      executionId: '${m['execution_id'] ?? ''}',
      channel: needsApproval
          ? ZaraChannel.approvals
          : (hasMission ? ZaraChannel.missions : ZaraChannel.status),
    );
  }
}

/// Scrub secret-looking material. Mirrors core/tracing.py patterns.
String scrub(String s) {
  var out = s;
  for (final p in [
    RegExp(r'nvapi-[A-Za-z0-9_\-]{8,}'),
    RegExp(r'sk-ant-[A-Za-z0-9_\-]{8,}'),
    RegExp(r'sk-proj-[A-Za-z0-9_\-]{8,}'),
    RegExp(r'gsk_[A-Za-z0-9]{10,}'),
    RegExp(r'zara-dev-\S+'),
    RegExp(r'zara-pair-\S+'),
    RegExp(r'Bearer \S+'),
  ]) {
    out = out.replaceAll(p, '[REDACTED]');
  }
  return out;
}

extension _Take on String {
  String take(int n) => length <= n ? this : substring(0, n);
}

/// A user tap. approve/deny carry ONLY the execution ID; the Core call
/// happens in main.dart via CoreClient (device-scoped, server-audited).
/// There is no path from a notification to local tool execution.
class NotificationAction {
  final String kind; // 'approve' | 'deny' | 'open' | 'dismiss'
  final String notificationId;
  final String executionId;
  const NotificationAction(this.kind, this.notificationId,
      [this.executionId = '']);
}

class NotificationCenter {
  static const maxSeen = 200;
  final Map<String, DeviceNotification> _shown = {};
  final Set<String> _acked = {};

  /// Returns true when newly shown; false on duplicate.
  bool show(DeviceNotification n) {
    if (_shown.containsKey(n.id)) return false;
    _shown[n.id] = n;
    while (_shown.length > maxSeen) {
      _shown.remove(_shown.keys.first);
    }
    return true;
  }

  List<DeviceNotification> get pending => _shown.values
      .where((n) => !_acked.contains(n.id))
      .toList();

  void ack(String id) => _acked.add(id);
  bool get hasPendingApprovals =>
      pending.any((n) => n.channel == ZaraChannel.approvals);
}
