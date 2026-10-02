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
    }
  }

  Future<Map<String, dynamic>> network() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('getNetwork');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    }
  }

  Future<Map<String, dynamic>> permissions() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('getPermissions');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    }
  }

  /// Voice boundary (Stage 3 foundation). The native side reports real
  /// support flags; capture/STT/TTS/wake engines land with providers later.
  /// Anything unimplemented returns supported=false — never faked.
  Future<Map<String, dynamic>> voiceSupport() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('getVoiceSupport');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    }
  }
}
