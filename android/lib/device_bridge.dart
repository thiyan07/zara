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

  /// Phase 1 read-only states. Each returns the native map verbatim;
  /// {supported:false} means unavailable — never a fake state.
  Future<Map<String, dynamic>> wifiState() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('getWifiState');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  Future<Map<String, dynamic>> bluetoothState() async {
    try {
      final m =
          await _ch.invokeMapMethod<String, dynamic>('getBluetoothState');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  Future<Map<String, dynamic>> hotspotState() async {
    try {
      final m =
          await _ch.invokeMapMethod<String, dynamic>('getHotspotState');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  Future<Map<String, dynamic>> mobileDataState() async {
    try {
      final m =
          await _ch.invokeMapMethod<String, dynamic>('getMobileDataState');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  Future<Map<String, dynamic>> airplaneModeState() async {
    try {
      final m =
          await _ch.invokeMapMethod<String, dynamic>('getAirplaneModeState');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  /// Phase 1 settings/app open (user-confirmed OS UI, never silent toggles).
  Future<Map<String, dynamic>> openSettings(String screen) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'openSettings', {'screen': screen});
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'open failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  Future<Map<String, dynamic>> openApp(String package) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'openApp', {'package': package});
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'open failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  /// Phase 2 tiered state changes. Each returns the native result map
  /// ({supported, applied, requested, method, verify, ...}) verbatim;
  /// applied:true appears only after native post-action verification.
  /// Channel failures throw SysControlBridgeException (never fake data).
  Future<Map<String, dynamic>> setWifi(bool enabled) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'setWifi', {'enabled': enabled});
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'set failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  Future<Map<String, dynamic>> setBluetooth(bool enabled) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'setBluetooth', {'enabled': enabled});
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'set failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  Future<Map<String, dynamic>> setHotspot(bool enabled) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'setHotspot', {'enabled': enabled});
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'set failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  Future<Map<String, dynamic>> setMobileData(bool enabled) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'setMobileData', {'enabled': enabled});
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'set failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  Future<Map<String, dynamic>> setAirplaneMode(bool enabled) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'setAirplaneMode', {'enabled': enabled});
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'set failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  /// Tier-3 probe: lab privileged companion installed + reachable?
  /// Ordinary builds (and the physical phone) report present:false —
  /// deployment_blocked, never an error.
  Future<Map<String, dynamic>> privCompanionProbe() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'privCompanionProbe');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  /// Resource telemetry snapshot (observation only, never authority).
  /// Returns raw native map; missing keys mean unavailable (never zero).
  /// Never throws — channel failure yields {} (heartbeat omits it).
  Future<Map<String, dynamic>> telemetry() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('getDeviceTelem');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {};
    } catch (_) {
      return {};
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

  /// Rung-1 privileged gateway: force-stop one lab-allowlisted package.
  /// The device-side allowlist mirror lives in native code; Core holds the
  /// same set. Throws PrivBridgeException on denied/unavailable/failed —
  /// never a fake success.
  Future<Map<String, dynamic>> privForceStop(String packageName) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'privForceStop', {'package': packageName});
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw PrivBridgeException(e.code, e.message ?? 'force-stop failed');
    } catch (e) {
      throw PrivBridgeException('failed', '$e');
    }
  }

  /// Rung-2 lab-pilot gateway: bounded screen snapshot via the optional
  /// AccessibilityService. Unbound/disabled is reported as
  /// {supported:false} by the native side — never a fake tree. Throws
  /// A11yBridgeException on channel failure.
  Future<Map<String, dynamic>> a11yInspect() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('a11yInspect');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw A11yBridgeException(e.code, e.message ?? 'inspect failed');
    } catch (e) {
      throw A11yBridgeException('failed', '$e');
    }
  }

  /// Rung-3: a11y snapshot with parameters (expected_package, max_elements,
  /// include_text, include_content_description, expected_snapshot_id). Passes
  /// params to native snapshot for validation and filtering. Throws
  /// A11yBridgeException on channel failure.
  Future<Map<String, dynamic>> a11yInspectWithParams(Map<String, dynamic> params) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'a11yInspectWithParams', params);
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw A11yBridgeException(e.code, e.message ?? 'inspect failed');
    } catch (e) {
      throw A11yBridgeException('failed', '$e');
    }
  }

  /// Rung-2 lab-pilot gateway: tap one snapshot node in an allowlisted lab
  /// package. The device-side allowlist mirror lives in native code; Core
  /// holds the same set. Throws A11yBridgeException on denied/unavailable/
  /// failed — never a fake success.
  Future<Map<String, dynamic>> a11yTap(String package, int nodeId) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'a11yTap', {'package': package, 'node_id': nodeId});
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw A11yBridgeException(e.code, e.message ?? 'tap failed');
    } catch (e) {
      throw A11yBridgeException('failed', '$e');
    }
  }

  /// Rung-2 registration-guard probe: is the AccessibilityService
  /// enabled in Settings AND bound AND seeing a window? Read-only, zero
  /// interaction. Returns {supported, ready, service_enabled, bound,
  /// root_present, reason}. Never throws a fake ready state.
  Future<Map<String, dynamic>> a11yProbe() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('a11yProbe');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw A11yBridgeException(e.code, e.message ?? 'probe failed');
    } catch (e) {
      throw A11yBridgeException('failed', '$e');
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

  /// Background-resume opt-in flag (plain internal marker file, no
  /// secrets). The boot receiver posts a "tap to resume" notification
  /// only when this is true; the app never polls in the background on
  /// its own. Defaults to false (opt-in).
  Future<bool> resumeOptIn() async {
    try {
      final v = await _ch.invokeMethod<bool>('getResumeOptIn');
      return v == true;
    } catch (_) {
      return false;
    }
  }

  Future<void> setResumeOptIn(bool enabled) async {
    try {
      await _ch.invokeMethod('setResumeOptIn', {'enabled': enabled});
    } catch (_) {/* flag write is best-effort; UI shows last-known */}
  }

  /// Wake-word detection preference (plain internal marker file, no
  /// secrets). Absent/unreadable reads as OFF — the safe default on
  /// first install. Persisting the preference never overrides a
  /// revoked mic permission, a missing model, or a failed init: those
  /// re-validate on every enable.
  Future<bool> wakeOptIn() async {
    try {
      final v = await _ch.invokeMethod<bool>('getWakeOptIn');
      return v == true;
    } catch (_) {
      return false;
    }
  }

  Future<void> setWakeOptIn(bool enabled) async {
    try {
      await _ch.invokeMethod('setWakeOptIn', {'enabled': enabled});
    } catch (_) {/* flag write is best-effort; UI shows last-known */}
  }

  /// Wake-model presence probe (report only). Always treats missing or
  /// unreadable data as all-absent; `ready` comes from native (always
  /// false until an engine ships) and is never upgraded here.
  Future<Map<String, dynamic>> wakeModelStatus() async {
    try {
      final m =
          await _ch.invokeMapMethod<String, dynamic>('getWakeModelStatus');
      final out = Map<String, dynamic>.from(m ?? {});
      out['model_info'] = out['model_info'] == true;
      out['keyword_model'] = out['keyword_model'] == true;
      out['mel_model'] = out['mel_model'] == true;
      out['ready'] = out['ready'] == true;
      return out;
    } catch (_) {
      return {
        'model_info': false,
        'keyword_model': false,
        'mel_model': false,
        'ready': false,
        'note': 'probe failed'
      };
    }
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

/// Structured privileged-gateway failure (Rung-1): same code contract as
/// AudioBridgeException, separate type so privileged denials stay greppable.
class PrivBridgeException implements Exception {
  final String code;
  final String message;
  const PrivBridgeException(this.code, this.message);
  @override
  String toString() => 'PrivBridgeException($code): $message';
}

/// Structured a11y-gateway failure (Rung-2 lab pilot): same code contract
/// as AudioBridgeException, separate type so a11y denials stay greppable.
class A11yBridgeException implements Exception {
  final String code;
  final String message;
  const A11yBridgeException(this.code, this.message);
  @override
  String toString() => 'A11yBridgeException($code): $message';
}

/// Structured system-control failure (Phase 1 settings/app open):
/// denied|unavailable|failed — never a fake success.
class SysControlBridgeException implements Exception {
  final String code;
  final String message;
  const SysControlBridgeException(this.code, this.message);
  @override
  String toString() => 'SysControlBridgeException($code): $message';
}
