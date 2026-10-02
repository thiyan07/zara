import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/app_state.dart';
import 'package:zara_android/battery_governor.dart';
import 'package:zara_android/network_monitor.dart';
import 'package:zara_android/push.dart';
import 'package:zara_android/zara_notifications.dart';

void main() {
  test('battery levels follow Core thresholds (15/25/30)', () {
    BatteryReport r(double? pct, {bool charging = false, bool ps = false}) =>
        BatteryReport(
            pct: pct, charging: charging, timestamp: DateTime.now(),
            powerSave: ps);
    expect(r(5).level(), equals('critical'));
    expect(r(20).level(), equals('low'));
    expect(r(27).level(), equals('caution'));
    expect(r(80).level(), equals('healthy'));
    expect(r(5, charging: true).level(), equals('healthy'));
    expect(r(null).level(), equals('healthy'));
    expect(r(50, ps: true).wakeAllowed, isFalse);
    expect(r(5).reduceBackgroundWork, isTrue);
    expect(r(80).reduceBackgroundWork, isFalse);
    // heartbeat subset matches Core schema
    expect(r(77).toHeartbeat(),
        equals({'battery_pct': 77.0, 'charging': false}));
  });

  test('network states map truthfully; offline never claims success', () {
    NetworkSnapshot snap(Map<String, dynamic> m) =>
        NetworkSnapshot.fromBridge(m);
    expect(snap({'network': 'wifi'}).state, equals('connected'));
    expect(snap({'network': 'metered'}).state, equals('metered'));
    expect(snap({'network': 'wifi', 'metered': true}).state,
        equals('metered'));
    expect(snap({'network': 'offline'}).offline, isTrue);
    expect(snap({}).state, equals('unavailable'));
    expect(snap({'network': 'offline'}).describe(), contains('Offline'));
  });

  test('notifications scrub secrets, dedup, channel approvals', () {
    final center = NotificationCenter();
    final n = DeviceNotification.fromJson({
      'id': 'notif-1',
      'title': 'Approval required nvapi-SECRETKEY12345678',
      'body': 'run X with sk-ant-SECRETKEY12345678',
      'mission_id': 'm1',
    });
    expect(n.title, isNot(contains('SECRETKEY')));
    expect(n.body, isNot(contains('SECRETKEY')));
    expect(n.channel, equals('zara_approvals'));
    expect(center.show(n), isTrue);
    expect(center.show(n), isFalse); // duplicate dropped
    expect(center.hasPendingApprovals, isTrue);
    center.ack('notif-1');
    expect(center.pending, isEmpty);
    expect(() => DeviceNotification.fromJson({'title': 'x'}),
        throwsFormatException);
  });

  test('notification actions carry IDs only — no local execution path', () {
    const a = NotificationAction('approve', 'notif-1', 'exec-1');
    expect(a.kind, equals('approve'));
    expect(a.executionId, equals('exec-1'));
  });

  test('push tokens validated, rotated, deduped; fallback is poll', () async {
    final t = MockPushTransport();
    final p = PushProvider(t);
    expect(() => p.register('d', ''), throwsArgumentError);
    await p.register('d', 'tok-1');
    expect(p.token, equals('tok-1'));
    await p.rotate('tok-2');
    expect(p.token, equals('tok-2'));
    expect(p.onMessage({'event_id': 'e1'}), isTrue);
    expect(p.onMessage({'event_id': 'e1'}), isFalse); // dup
    expect(p.onMessage({}), isFalse); // no id: ignore
    await p.logout();
    expect(p.token, isNull);
  });

  test('UI state lines are truthful; restore map has no secrets', () {
    final off = ZaraUiState(
        connection: 'offline', updatedAt: DateTime.now());
    expect(off.statusLine(), contains('Offline'));
    final rev =
        ZaraUiState(lifecycle: 'revoked', updatedAt: DateTime.now());
    expect(rev.statusLine(), contains('Revoked'));
    final appr = ZaraUiState(
        connection: 'online',
        lifecycle: 'online',
        deviceId: 'android-phone',
        approvalPending: true,
        updatedAt: DateTime.now());
    expect(appr.statusLine(), equals('Approval required'));
    final m = appr.toRestoreMap();
    expect(m['device_id'], equals('android-phone'));
    expect(m.keys.any((k) => k.contains('key') || k.contains('token')),
        isFalse);
    expect(ZaraUiState.fromRestoreMap(m).deviceId,
        equals('android-phone'));
  });
}
