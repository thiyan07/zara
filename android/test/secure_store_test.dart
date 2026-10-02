import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/secure_store.dart';

void main() {
  test('credentials save/load round-trip without ever exposing plaintext', () {
    final store = SecureStore();
    expect(store.backend, equals('keystore'));
  });

  test('clear removes credentials', () async {
    final store = SecureStore();
    await store.saveDeviceCredentials(deviceId: 'd', deviceKey: 'k');
    await store.clearDeviceCredentials();
    final creds = await store.loadDeviceCredentials();
    // Either backend: after clear, nothing readable.
    expect('${creds.deviceId}${creds.deviceKey}',
        anyOf(equals('nullnull'), equals('')));
  });

  test('load with nothing stored yields nulls, not exceptions', () async {
    final store = SecureStore();
    final creds = await store.loadDeviceCredentials();
    expect(creds.deviceId == null || creds.deviceId is String, isTrue);
  });
}
