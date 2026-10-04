import 'package:flutter/services.dart';

/// Clean Flutter <-> native boundary. All Android API access goes through
/// this class over the single 'zara/device' MethodChannel.
/// Anything unimplemented returns supported=false, never a fake value.
class DeviceBridge {
  static const MethodChannel _ch = MethodChannel('zara/device');

  Future<Map<String, dynamic>> battery() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('getBattery');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  Future<Map<String, dynamic>> network() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('getNetwork');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  Future<Map<String, dynamic>> permissions() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('getPermissions');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  /// Voice boundary. The native side reports real support flags;
  /// capture/playback API paths land in Stage 9 (signal/audibility still
  /// deferred to physical validation). Anything unimplemented returns
  /// supported=false — never faked.
  Future<Map<String, dynamic>> voiceSupport() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('getVoiceSupport');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  /// Bounded native capture (WAV bytes, ≤30 s). Throws AudioBridgeException
  /// on denied/unavailable/cancelled — never returns fake audio.
  Future<Uint8List?> audioCapture({double seconds = 5.0}) async {
    try {
      final b = await _ch.invokeMethod<Uint8List>(
          'audioCapture', {'seconds': seconds});
      return b;
    } on PlatformException catch (e) {
      throw AudioBridgeException(e.code, e.message ?? 'capture failed');
    } catch (e) {
      throw AudioBridgeException('failed', '$e');
    }
  }

  Future<void> audioStop() async {
    try {
      await _ch.invokeMethod('audioStop');
    } on PlatformException catch (e) {
      throw AudioBridgeException(e.code, e.message ?? 'stop failed');
    } catch (e) {
      throw AudioBridgeException('failed', '$e');
    }
  }

  /// Play TTS WAV bytes from Core. Completion is process-exit, NOT proof
  /// a human heard anything (audibility is manual-only).
  Future<Map<String, dynamic>> audioPlay(Uint8List wav) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'audioPlay', {'wav': wav});
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw AudioBridgeException(e.code, e.message ?? 'play failed');
    } catch (e) {
      throw AudioBridgeException('failed', '$e');
    }
  }

  Future<void> audioPlayStop() async {
    try {
      await _ch.invokeMethod('audioPlayStop');
    } on PlatformException catch (e) {
      throw AudioBridgeException(e.code, e.message ?? 'play-stop failed');
    } catch (e) {
      throw AudioBridgeException('failed', '$e');
    }
  }

  /// Runtime permission requests (Activity-mediated). Returns the
  /// post-request state; denial is a normal outcome, never an error.
  Future<Map<String, dynamic>> requestMicPermission() async {
    try {
      final m =
          await _ch.invokeMapMethod<String, dynamic>('requestMicPermission');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'granted': false};
    } catch (_) {
      return {'granted': false};
    }
  }

  Future<Map<String, dynamic>> requestNotifPermission() async {
    try {
      final m =
          await _ch.invokeMapMethod<String, dynamic>('requestNotifPermission');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'granted': false};
    } catch (_) {
      return {'granted': false};
    }
  }

  Future<void> createNotificationChannels() async {
    try {
      await _ch.invokeMethod('createNotificationChannels');
    } catch (_) {/* channels are best-effort */}
  }

  /// Assistant-role report (Stage 10). Absence of data is never success.
  Future<Map<String, dynamic>> assistantStatus() async {
    try {
      final m =
          await _ch.invokeMapMethod<String, dynamic>('getAssistantStatus');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'service_registered': false};
    } catch (_) {
      return {'service_registered': false};
    }
  }

  /// Debug/emulator self-test: sync mic+speaker API info, no permission
  /// needed, no audio captured. Proves the API path, not signal.
  Future<Map<String, dynamic>> audioSelfTest() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('audioSelfTest');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }
}

/// Structured bridge failure: code is denied|unavailable|cancelled|failed.
class AudioBridgeException implements Exception {
  final String code;
  final String message;
  const AudioBridgeException(this.code, this.message);
  @override
  String toString() => 'AudioBridgeException($code): $message';
}
