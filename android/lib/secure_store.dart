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
  static const _skSlot = 'zara_device_sk'; // Ed25519 seed (challenge auth)
  static const _opSlot = 'zara_operator_token'; // operator Bearer, user-asked

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
    String deviceSecret = '',
  }) async {
    try {
      await _storage.write(key: _idSlot, value: deviceId);
      await _storage.write(key: _keySlot, value: deviceKey);
      if (deviceSecret.isNotEmpty) {
        await _storage.write(key: _skSlot, value: deviceSecret);
      }
      // Verify round-trip; fall back only if platform storage fails.
      final check = await _storage.read(key: _keySlot);
      if (check != deviceKey) {
        throw StateError('secure storage round-trip mismatch');
      }
    } catch (_) {
      _useFallback = true;
      _fallback[_idSlot] = deviceId;
      _fallback[_keySlot] = deviceKey;
      if (deviceSecret.isNotEmpty) _fallback[_skSlot] = deviceSecret;
    }
  }

  Future<({String? deviceId, String? deviceKey, String? deviceSecret})>
      loadDeviceCredentials() async {
    if (_useFallback) {
      return (
        deviceId: _fallback[_idSlot],
        deviceKey: _fallback[_keySlot],
        deviceSecret: _fallback[_skSlot],
      );
    }
    try {
      return (
        deviceId: await _storage.read(key: _idSlot),
        deviceKey: await _storage.read(key: _keySlot),
        deviceSecret: await _storage.read(key: _skSlot),
      );
    } catch (_) {
      return (deviceId: null, deviceKey: null, deviceSecret: null);
    }
  }

  Future<void> clearDeviceCredentials() async {
    _fallback.clear();
    try {
      await _storage.delete(key: _idSlot);
      await _storage.delete(key: _keySlot);
      await _storage.delete(key: _skSlot);
      await _storage.delete(key: _opSlot);
    } catch (_) {
      _useFallback = true;
    }
  }

  /// Operator Bearer token (user decision 2026-10-22: remembered on
  /// device so talk/tasks need the token entered ONCE, not per app
  /// start). Lives in the same Keystore-backed store as the device
  /// key — never plaintext SharedPreferences, logs, or source.
  /// Wiped together with device credentials (logout / revoked wipe):
  /// a un-paired device must not retain operator authority.
  Future<void> saveOperatorToken(String token) async {
    try {
      await _storage.write(key: _opSlot, value: token);
      final check = await _storage.read(key: _opSlot);
      if (check != token) {
        throw StateError('operator token round-trip mismatch');
      }
    } catch (_) {
      _useFallback = true;
      if (token.isEmpty) {
        _fallback.remove(_opSlot);
      } else {
        _fallback[_opSlot] = token;
      }
    }
  }

  Future<String?> loadOperatorToken() async {
    if (_useFallback) return _fallback[_opSlot];
    try {
      final v = await _storage.read(key: _opSlot);
      if (v == null || v.isEmpty) return null;
      return v;
    } catch (_) {
      return null;
    }
  }

  // --- P6-c26 (2026-10-24): generic keystore-backed slots for
  // phone-local POLICY state (the per-app open consents). By contract
  // these hold nothing secret-shaped (no keys/tokens — the constant
  // slots above stay the only credential holders) so a code audit
  // can still certify secrets live in exactly four places.

  Future<String?> readValue(String slot) async {
    if (_useFallback) return _fallback[slot];
    try {
      final v = await _storage.read(key: slot);
      if (v == null || v.isEmpty) return null;
      return v;
    } catch (_) {
      return null;
    }
  }

  Future<void> writeValue(String slot, String value) async {
    try {
      await _storage.write(key: slot, value: value);
    } catch (_) {
      _useFallback = true;
      _fallback[slot] = value;
    }
  }
}
