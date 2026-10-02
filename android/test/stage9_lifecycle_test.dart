import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/device_lifecycle.dart';

void main() {
  test('cold start without identity goes to pairing', () {
    final l = DeviceLifecycle();
    final snap = l.restore(null);
    expect(snap.deviceId, isNull);
    expect(l.state, equals(DeviceLifecycleState.uninitialized));
    l.move(DeviceLifecycleState.pairing);
    expect(l.state, equals(DeviceLifecycleState.pairing));
  });

  test('cold start with identity goes to registering', () {
    final l = DeviceLifecycle();
    final snap = l.restore('android-phone');
    expect(snap.deviceId, equals('android-phone'));
    expect(l.state, equals(DeviceLifecycleState.registering));
  });

  test('illegal jumps throw (no state forgery)', () {
    final l = DeviceLifecycle();
    expect(() => l.move(DeviceLifecycleState.online), throwsStateError);
    l.move(DeviceLifecycleState.pairing);
    expect(() => l.move(DeviceLifecycleState.online), throwsStateError);
  });

  test('403 with identity revokes; keys must be wiped by caller', () {
    final l = DeviceLifecycle();
    l.restore('android-phone');
    l.move(DeviceLifecycleState.online);
    expect(l.handleHttp(403, hasIdentity: true), equals('revoked'));
    expect(l.state, equals(DeviceLifecycleState.revoked));
    // revoked devices can only re-pair, never resume
    expect(() => l.move(DeviceLifecycleState.online), throwsStateError);
    l.move(DeviceLifecycleState.pairing);
  });

  test('404 means reregister, 5xx means reconnecting', () {
    final l = DeviceLifecycle();
    l.restore('d');
    l.move(DeviceLifecycleState.online);
    expect(l.handleHttp(404, hasIdentity: true), equals('reregister'));
    expect(l.state, equals(DeviceLifecycleState.online)); // one step only
    expect(l.handleHttp(503, hasIdentity: true), equals('reconnecting'));
    expect(l.state, equals(DeviceLifecycleState.reconnecting));
  });

  test('logout clears identity and returns to pairing path', () {
    final l = DeviceLifecycle();
    l.restore('d');
    l.move(DeviceLifecycleState.online);
    l.logout();
    expect(l.deviceId, isNull);
    expect(l.state, equals(DeviceLifecycleState.loggedOut));
    l.move(DeviceLifecycleState.pairing);
  });

  test('snapshot round-trips device ID, never secrets', () {
    final l = DeviceLifecycle();
    l.restore('android-phone');
    final m = l.snapshot().toMap();
    expect(m['device_id'], equals('android-phone'));
    expect(m.keys.any((k) => k.contains('key') || k.contains('token')),
        isFalse);
    expect(LifecycleSnapshot.fromMap(m).deviceId, equals('android-phone'));
  });
}
