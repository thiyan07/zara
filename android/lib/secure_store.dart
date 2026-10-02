import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Secure device-credential storage for Zara Android.
///
/// - Real path: flutter_secure_storage → Android Keystore-backed
///   EncryptedSharedPreferences. Long-lived device keys live ONLY here.
/// - Test/fallback path: in-memory store (used in unit tests and ONLY if
///   platform storage is unavailable; `backend` reports which is active).
/// - NEVER: plaintext SharedPreferences, source, assets, logs, or git.
class SecureStore {
  static const _keySlot = 'zara_device_key';
  static const _idSlot = 'zara_device_id';

  final FlutterSecureStorage _storage;
  final Map<String, String> _fallback = {};
  bool _useFallback = false;

  /// Backend in use: 'keystore' or 'memory-fallback'.
  String get backend => _useFallback ? 'memory-fallback' : 'keystore';

  SecureStore({FlutterSecureStorage? storage})
      : _storage = storage ?? const FlutterSecureStorage();

  Future<void> saveDeviceCredentials({
    required String deviceId,
    required String deviceKey,
  }) async {
    try {
      await _storage.write(key: _idSlot, value: deviceId);
      await _storage.write(key: _keySlot, value: deviceKey);
      // Verify round-trip; fall back only if platform storage fails.
      final check = await _storage.read(key: _keySlot);
      if (check != deviceKey) {
        throw StateError('secure storage round-trip mismatch');
      }
    } catch (_) {
      _useFallback = true;
      _fallback[_idSlot] = deviceId;
      _fallback[_keySlot] = deviceKey;
    }
  }

  Future<({String? deviceId, String? deviceKey})> loadDeviceCredentials() async {
    if (_useFallback) {
      return (deviceId: _fallback[_idSlot], deviceKey: _fallback[_keySlot]);
    }
    try {
      return (
        deviceId: await _storage.read(key: _idSlot),
        deviceKey: await _storage.read(key: _keySlot),
      );
    } catch (_) {
      return (deviceId: null, deviceKey: null);
    }
  }

  Future<void> clearDeviceCredentials() async {
    _fallback.clear();
    try {
      await _storage.delete(key: _idSlot);
      await _storage.delete(key: _keySlot);
    } catch (_) {
      _useFallback = true;
    }
  }
}
