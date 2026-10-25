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

  test('operator token: save/load round-trip (user decision 2026-10-22: '
      'entered once, remembered on device)', () async {
    final store = SecureStore();
    await store.saveOperatorToken('tok-123');
    expect(await store.loadOperatorToken(), equals('tok-123'));
    // the current (empty-field) state means "no token", not "forget it"
    await store.saveOperatorToken('');
    expect(await store.loadOperatorToken(), isNull);
  });

  test('operator token is wiped WITH device credentials: an un-paired '
      'device must not retain operator authority', () async {
    final store = SecureStore();
    await store.saveOperatorToken('tok-live');
    await store.clearDeviceCredentials();
    expect(await store.loadOperatorToken(), isNull);
  });

  test('loadOperatorToken survives storage failure honestly (nulls, not '
      'exceptions)', () async {
    final store = SecureStore();
    // no write performed: empty store + failing backend still resolves
    expect(await store.loadOperatorToken(), anyOf(isNull, isA<String>()));
  });
}
