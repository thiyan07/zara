import 'package:flutter/services.dart';

/// Deep conversion for maps that crossed StandardMethodCodec: only the
/// top level arrives typed; nested maps stay `Map<Object?, Object?>`
/// unless explicitly rebuilt. Used by bridge reads that return nested
/// structures (observed devices); conversion preserves values
/// verbatim — no truncation, no faking.
Map<String, dynamic> _deepStringMap(Object? input) {
  Object? convert(Object? v) {
    if (v is Map<Object?, Object?>) {
      return {
        for (final e in v.entries) '${e.key}': convert(e.value),
      };
    }
    if (v is List) {
      return [for (final e in v) convert(e)];
    }
    return v;
  }

  final m = input is Map<Object?, Object?> ? input : const <Object?, Object?>{};
  return {
    for (final e in m.entries) '${e.key}': convert(e.value),
  };
}

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
      final m = await _ch.invokeMapMethod<String, dynamic>('getBluetoothState');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  Future<Map<String, dynamic>> hotspotState() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('getHotspotState');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  Future<Map<String, dynamic>> mobileDataState() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
        'getMobileDataState',
      );
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  Future<Map<String, dynamic>> airplaneModeState() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
        'getAirplaneModeState',
      );
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
      final m = await _ch.invokeMapMethod<String, dynamic>('openSettings', {
        'screen': screen,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'open failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  Future<Map<String, dynamic>> openApp(String package) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('openApp', {
        'package': package,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'open failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  /// Universal-operator discovery: launchable apps (package + label),
  /// capped. Read-only metadata; never an authorization.
  Future<Map<String, dynamic>> appList({int limit = 100}) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('appList', {
        'limit': limit,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'listing failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  /// Passive notification read from the in-memory listener ring.
  /// [params] carries since_ms/limit bounds. The native side checks the
  /// listener-access grant and refuses honestly (granted:false) until
  /// the user enabled it in Settings — never opened silently.
  Future<Map<String, dynamic>> notifList(Map<String, dynamic> params) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('notifList', params);
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'read failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  /// Global navigation (back/home) guarded by the foreground package.
  /// [options] may carry expected_package from a fresh snapshot; a
  /// mismatch refuses natively (UNEXPECTED_PACKAGE) instead of navigating
  /// an unexpected app. Throws A11yBridgeException on refusal.
  Future<Map<String, dynamic>> a11yNavigate(
    String direction,
    String package, [
    Map<String, dynamic>? options,
  ]) async {
    try {
      final args = <String, dynamic>{
        'direction': direction,
        'package': package,
        ...?options,
      };
      final m = await _ch.invokeMapMethod<String, dynamic>(
        'a11yNavigate',
        args,
      );
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      final det = e.details;
      throw A11yBridgeException(
        e.code,
        e.message ?? 'navigate failed',
        det is Map ? Map<String, dynamic>.from(det) : const {},
      );
    } catch (e) {
      throw A11yBridgeException('failed', '$e');
    }
  }

  /// Phase 2 tiered state changes. Each returns the native result map
  /// ({supported, applied, requested, method, verify, ...}) verbatim;
  /// applied:true appears only after native post-action verification.
  /// Channel failures throw SysControlBridgeException (never fake data).
  Future<Map<String, dynamic>> setWifi(bool enabled) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('setWifi', {
        'enabled': enabled,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'set failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  Future<Map<String, dynamic>> setBluetooth(bool enabled) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('setBluetooth', {
        'enabled': enabled,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'set failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  Future<Map<String, dynamic>> setHotspot(bool enabled) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('setHotspot', {
        'enabled': enabled,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'set failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  Future<Map<String, dynamic>> setMobileData(bool enabled) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('setMobileData', {
        'enabled': enabled,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'set failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  Future<Map<String, dynamic>> setAirplaneMode(bool enabled) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('setAirplaneMode', {
        'enabled': enabled,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw SysControlBridgeException(e.code, e.message ?? 'set failed');
    } catch (e) {
      throw SysControlBridgeException('failed', '$e');
    }
  }

  // ---- Stage 31-P3 phase A: quick/one-shot actions ------------------
  // These return HONEST RESULT MAPS (never throw): unavailable/refused
  // is a first-class answer (torch without a flash unit, DND ring
  // refusal, clipboard focus rule, missing adb grant), because the
  // refusal is exactly what Core's verdict should record.
  Future<Map<String, dynamic>> torchSet(bool on) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('torchSet', {
        'on': on,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      return {'supported': true, 'ok': false, 'detail': '${e.code}: ${e.message}'};
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  Future<Map<String, dynamic>> streamVolumeSet(int pct) async {
    try {
      final m =
          await _ch.invokeMapMethod<String, dynamic>('streamVolumeSet', {
        'pct': pct,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      return {'supported': true, 'ok': false, 'detail': '${e.code}: ${e.message}'};
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  Future<Map<String, dynamic>> findMyPhoneRing(
      int seconds, bool allowDndBypass) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('findMyPhoneRing', {
        'seconds': seconds,
        'allow_dnd_bypass': allowDndBypass,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      return {'supported': true, 'ok': false, 'detail': '${e.code}: ${e.message}'};
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  Future<Map<String, dynamic>> mediaKeyEvent(String action) async {
    try {
      final m =
          await _ch.invokeMapMethod<String, dynamic>('mediaKeyEvent', {
        'action': action,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      return {'supported': true, 'ok': false, 'detail': '${e.code}: ${e.message}'};
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  Future<Map<String, dynamic>> clipboardSet(String text) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('clipboardSet', {
        'text': text,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      return {'supported': true, 'ok': false, 'detail': '${e.code}: ${e.message}'};
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  Future<Map<String, dynamic>> openUri(String uri) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('openUri', {
        'uri': uri,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      return {'supported': true, 'ok': false, 'detail': '${e.code}: ${e.message}'};
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  Future<Map<String, dynamic>> powerSaverSet(bool on) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('powerSaverSet', {
        'on': on,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      return {'supported': true, 'ok': false, 'detail': '${e.code}: ${e.message}'};
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  Future<Map<String, dynamic>> alarmSet(
      int? hour, int? minute, String? label) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('alarmSet', {
        'hour': hour,
        'minute': minute,
        'label': label,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      return {'supported': true, 'ok': false, 'detail': '${e.code}: ${e.message}'};
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  /// P5 feature-hunt: countdown timer hand-off (ACTION_SET_TIMER).
  Future<Map<String, dynamic>> timerSet(int? minutes, String? label) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('timerSet', {
        'minutes': minutes,
        'label': label,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      return {'supported': true, 'ok': false, 'detail': '${e.code}: ${e.message}'};
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  /// P5 feature-hunt ② (2026-10-22): DND state read — read-only bridge.
  Future<Map<String, dynamic>> dndGet() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('dndGet');
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      return {'supported': true, 'ok': false, 'detail': '${e.code}: ${e.message}'};
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  /// P5 feature-hunt ② (2026-10-22): DND toggle bridge. skip_ui is
  /// refused structurally at the Kotlin layer (no UI to hand off).
  Future<Map<String, dynamic>> dndSet(bool? enabled) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('dndSet', {
        'enabled': enabled,
      });
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      return {'supported': true, 'ok': false, 'detail': '${e.code}: ${e.message}'};
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  /// Tier-3 probe: lab privileged companion installed + reachable?
  /// Ordinary builds (and the physical phone) report present:false —
  /// deployment_blocked, never an error.
  Future<Map<String, dynamic>> privCompanionProbe() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
        'privCompanionProbe',
      );
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

  /// Stage 20: observed-device discovery (detection ≠ trust).
  /// StorageManager volumes + UsbManager listing + DisplayManager
  /// external displays. Read-only; no permission needed for these.
  Future<Map<String, dynamic>> observedDevices() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
        'getObservedDevices',
      );
      // Nested maps that crossed StandardMethodCodec decode as
      // Map<Object?, Object?> (only the TOP level is translated); the
      // observed-device rows and their stat maps must be deep-converted
      // or every generic consumer (List.cast<Map<String, dynamic>>)
      // throws a subtype error at render time.
      return _deepStringMap(m ?? {});
    } on PlatformException {
      return {'supported': false};
    } catch (_) {
      return {'supported': false};
    }
  }

  /// Media-category counts (permission-gated honestly on the native
  /// side) + primary storage volume usage snapshot. Counts only —
  /// never any media content or paths in the response.
  Future<Map<String, dynamic>> mediaCounts() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('getMediaCounts');
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
      final b = await _ch.invokeMethod<Uint8List>('audioCapture', {
        'seconds': seconds,
      });
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
      final m = await _ch.invokeMapMethod<String, dynamic>('audioPlay', {
        'wav': wav,
      });
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
      final m = await _ch.invokeMapMethod<String, dynamic>('privForceStop', {
        'package': packageName,
      });
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
  Future<Map<String, dynamic>> a11yInspectWithParams(
    Map<String, dynamic> params,
  ) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
        'a11yInspectWithParams',
        params,
      );
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      throw A11yBridgeException(e.code, e.message ?? 'inspect failed');
    } catch (e) {
      throw A11yBridgeException('failed', '$e');
    }
  }

  /// Rung-2 lab-pilot gateway: tap one snapshot node in an allowlisted lab
  /// package. The device-side allowlist mirror lives in native code; Core
  /// holds the same set. Contract v1: optional snapshot bindings
  /// (expected_package, expected_snapshot_id, max_elements) are forwarded
  /// to the native snapshot-bound tap. Throws A11yBridgeException on
  /// denied/unavailable/failed/stale/mismatch — never a fake success.
  Future<Map<String, dynamic>> a11yTap(
    String package,
    int nodeId, [
    Map<String, dynamic>? options,
  ]) async {
    try {
      final args = <String, dynamic>{
        'package': package,
        'node_id': nodeId,
        ...?options,
      };
      final m = await _ch.invokeMapMethod<String, dynamic>('a11yTap', args);
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      // Contract v1: native refusals carry the evidence map as channel
      // details (fresh snapshot_id on STALE_SNAPSHOT, actual package on
      // UNEXPECTED_PACKAGE). Preserve it so callers can re-inspect
      // precisely instead of guessing.
      final det = e.details;
      throw A11yBridgeException(
        e.code,
        e.message ?? 'tap failed',
        det is Map ? Map<String, dynamic>.from(det) : const {},
      );
    } catch (e) {
      throw A11yBridgeException('failed', '$e');
    }
  }

  /// Contract v2: snapshot-bound text entry on an editable node.
  /// [text] is capped at 500 chars client-side and never echoed back in
  /// results or errors. Throws A11yBridgeException (with refusal evidence
  /// in details) on denied/unavailable/failed/stale/mismatch.
  Future<Map<String, dynamic>> a11ySetText(
    String package,
    int nodeId,
    String text, [
    Map<String, dynamic>? options,
  ]) async {
    try {
      final args = <String, dynamic>{
        'package': package,
        'node_id': nodeId,
        'text': text,
        ...?options,
      };
      final m = await _ch.invokeMapMethod<String, dynamic>('a11ySetText', args);
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      final det = e.details;
      throw A11yBridgeException(
        e.code,
        e.message ?? 'set-text failed',
        det is Map ? Map<String, dynamic>.from(det) : const {},
      );
    } catch (e) {
      throw A11yBridgeException('failed', '$e');
    }
  }

  /// Contract-v3 read-only semantic element lookup (gui.find). Resolves a
  /// `target` selector against the FRESH accessibility tree WITHOUT
  /// acting. A MATCH returns element facts; not-found / ambiguous /
  /// mismatch arrive as coded bridge errors carrying evidence. Never taps,
  /// types, or focuses anything.
  Future<Map<String, dynamic>> a11yFind(
    String package,
    Map<String, dynamic> target, [
    Map<String, dynamic>? options,
  ]) async {
    try {
      final args = <String, dynamic>{
        'package': package,
        'target': target,
        ...?options,
      };
      final m = await _ch.invokeMapMethod<String, dynamic>('a11yFind', args);
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      final det = e.details;
      throw A11yBridgeException(
        e.code,
        e.message ?? 'find failed',
        det is Map ? Map<String, dynamic>.from(det) : const {},
      );
    } catch (e) {
      throw A11yBridgeException('failed', '$e');
    }
  }

  /// Contract v2: snapshot-bound scroll on a scrollable node. Throws
  /// A11yBridgeException (with refusal evidence in details) on
  /// denied/unavailable/failed/stale/mismatch.
  Future<Map<String, dynamic>> a11yScroll(
    String package,
    int nodeId,
    bool forward, [
    Map<String, dynamic>? options,
  ]) async {
    try {
      final args = <String, dynamic>{
        'package': package,
        'node_id': nodeId,
        'direction': forward ? 'forward' : 'backward',
        ...?options,
      };
      final m = await _ch.invokeMapMethod<String, dynamic>('a11yScroll', args);
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException catch (e) {
      final det = e.details;
      throw A11yBridgeException(
        e.code,
        e.message ?? 'scroll failed',
        det is Map ? Map<String, dynamic>.from(det) : const {},
      );
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
      final m = await _ch.invokeMapMethod<String, dynamic>(
        'requestMicPermission',
      );
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'granted': false};
    } catch (_) {
      return {'granted': false};
    }
  }

  Future<Map<String, dynamic>> requestNotifPermission() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
        'requestNotifPermission',
      );
      return Map<String, dynamic>.from(m ?? {});
    } on PlatformException {
      return {'granted': false};
    } catch (_) {
      return {'granted': false};
    }
  }

  /// Stage 31-P3 phase B (P3d): call with contact resolution. Every
  /// method returns an honest map (never throws); a refusal is data.
  Future<Map<String, dynamic>> contactsLookup(String? name, {int limit = 5}) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('contactsLookup', {
        'name': name,
        'limit': limit,
      });
      return Map<String, dynamic>.from(m ?? {});
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  Future<Map<String, dynamic>> callDirect(String? number) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('callDirect',
          {'number': number});
      return Map<String, dynamic>.from(m ?? {});
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  Future<Map<String, dynamic>> callScreenPoll({int waitMs = 4000}) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('callScreenPoll',
          {'wait_ms': waitMs});
      return Map<String, dynamic>.from(m ?? {});
    } catch (e) {
      return {
        'supported': true,
        'verdict': 'absent',
        'detail': 'poll unavailable: $e',
      };
    }
  }

  /// Surface-enablement prompts — DANGEROUS permissions are requested
  /// here, once, in the asking turn; never silently in a call flow.
  Future<Map<String, dynamic>> requestContactsPermission() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
        'requestContactsPermission',
      );
      return Map<String, dynamic>.from(m ?? {});
    } catch (_) {
      return {'granted': false};
    }
  }

  Future<Map<String, dynamic>> requestCallPermission() async {
    try {
      final m = await _ch
          .invokeMapMethod<String, dynamic>('requestCallPermission');
      return Map<String, dynamic>.from(m ?? {});
    } catch (_) {
      return {'granted': false};
    }
  }

  /// Stage 31-P3 phase B (P3e): ONE carrier SMS after the confirmed
  /// resolution. The bridge re-checks grammar + caps internally; the
  /// honest verdict is "sent" (radio accepted — delivery is NOT
  /// confirmable by a bounded re-read). Never throws.
  Future<Map<String, dynamic>> smsSend(String? number, String? text) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('smsSend', {
        'number': number,
        'text': text,
      });
      return Map<String, dynamic>.from(m ?? {});
    } catch (e) {
      return {'supported': true, 'ok': false, 'detail': 'failed: $e'};
    }
  }

  /// Surface enablement for SEND_SMS (DANGEROUS; asked once, never
  /// silently in a send flow).
  Future<Map<String, dynamic>> requestSmsPermission() async {
    try {
      final m = await _ch
          .invokeMapMethod<String, dynamic>('requestSmsPermission');
      return Map<String, dynamic>.from(m ?? {});
    } catch (_) {
      return {'granted': false};
    }
  }

  /// Stage 31-P3 phase B (P3c): photo INVENTORY — counts only, never
  /// names or content, read-only. The pull itself is the operator's
  /// per-run consented adb seam; this adds zero delete paths.
  Future<Map<String, dynamic>> photosInventory() async {
    try {
      final m = await _ch
          .invokeMapMethod<String, dynamic>('photosInventory');
      return Map<String, dynamic>.from(m ?? {});
    } catch (e) {
      return {'supported': false, 'detail': 'failed: $e'};
    }
  }

  /// Stage 31-P3 phase B (P3a): the labeled self-test session, for
  /// exercising the production targeted-control path end-to-end.
  Future<Map<String, dynamic>> mediaSelftest(bool start) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          start ? 'mediaSelftest' : 'mediaSelftestRelease');
      return Map<String, dynamic>.from(m ?? {});
    } catch (e) {
      return {'supported': false, 'detail': 'failed: $e'};
    }
  }

  /// Stage 31-P3 phase B (P3a): TARGETED media control of ONE app's
  /// session (hint required; 0/many matches are honest answers).
  Future<Map<String, dynamic>> mediaSessionTargeted(
      String? hint, String? action) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
          'mediaSessionTargeted', {'hint': hint, 'action': action});
      return Map<String, dynamic>.from(m ?? {});
    } catch (e) {
      return {'supported': false, 'detail': 'failed: $e'};
    }
  }

  /// Stage 31-P3 phase B (P3f): bounded OTP scan over the FRESH
  /// notification ring. Per-use consent is Core's job (the tool parks
  /// for approval EVERY run); the bridge adds freshness + shape only.
  /// The full notification bodies never leave the device.
  Future<Map<String, dynamic>> otpScan(int maxAgeMs, String? senderHint) async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>('otpScan', {
        'max_age_ms': maxAgeMs,
        'sender_hint': senderHint,
      });
      return Map<String, dynamic>.from(m ?? {});
    } catch (e) {
      return {'supported': false, 'detail': 'failed: $e'};
    }
  }

  Future<void> createNotificationChannels() async {
    try {
      await _ch.invokeMethod('createNotificationChannels');
    } catch (_) {
      /* channels are best-effort */
    }
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
    } catch (_) {
      /* flag write is best-effort; UI shows last-known */
    }
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
    } catch (_) {
      /* flag write is best-effort; UI shows last-known */
    }
  }

  /// Wake-model presence probe (report only). Always treats missing or
  /// unreadable data as all-absent; `ready` comes from native (always
  /// false until an engine ships) and is never upgraded here.
  Future<Map<String, dynamic>> wakeModelStatus() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
        'getWakeModelStatus',
      );
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
        'note': 'probe failed',
      };
    }
  }

  /// Assistant-role report (Stage 10). Absence of data is never success.
  Future<Map<String, dynamic>> assistantStatus() async {
    try {
      final m = await _ch.invokeMapMethod<String, dynamic>(
        'getAssistantStatus',
      );
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

  /// Native refusal evidence (fresh snapshot_id, actual package, ...).
  /// Empty unless the channel error carried a details map.
  final Map<String, dynamic> details;
  const A11yBridgeException(this.code, this.message, [this.details = const {}]);
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
