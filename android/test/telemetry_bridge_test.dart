import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/device_bridge.dart';

/// Telemetry bridge contract tests (mock MethodChannel 'zara/device').
///
/// Field contract (Kotlin DeviceBridge.deviceTelem -> Flutter
/// bridge.telemetry() -> heartbeat mem/storage/temp/thermal):
///   mem_avail_mb / mem_total_mb : int MB
///   storage_free_mb / storage_total_mb : int MB
///   battery_temp_c : double C
///   thermal_state : string enum (Core allowlists)
///   power_save : bool
///   cpu_count : int, capacity only
/// Missing keys = unavailable, never zero.
///
/// DeviceBridge.telemetry() returns the RAW native map (passthrough, never
/// throws: channel failure yields {}). The heartbeat narrowing in
/// lib/main.dart (_tick: _asInt/_asDouble/_memPct + `is bool`/`is String`
/// guards) then coerces to validated scalars, null = unknown. The private
/// helpers in main.dart cannot be imported, so this file mirrors their
/// exact semantics (see _hbAsInt/_hbAsDouble/_hbMemPct below, copied from
/// main.dart lines 93-107) to drive the heartbeat-builder path assertions.
void main() {
  const channel = MethodChannel('zara/device');

  TestWidgetsFlutterBinding.ensureInitialized();

  tearDown(() {
    TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null);
  });

  void mockTelem(Future<dynamic> Function(MethodCall call) handler) {
    TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async => handler(call));
  }

  Future<Map<String, dynamic>> driveTelemetry(
      Map<String, dynamic>? fixture) async {
    mockTelem((call) async {
      if (call.method != 'getDeviceTelem') return null;
      return fixture;
    });
    return DeviceBridge().telemetry();
  }

  group('telemetry bridge (zara/device getDeviceTelem)', () {
    test('1. full fixture -> all fields pass through with types/units',
        () async {
      final fixture = <String, dynamic>{
        'mem_avail_mb': 1024,
        'mem_total_mb': 4096,
        'storage_free_mb': 20480,
        'storage_total_mb': 65536,
        'battery_temp_c': 31.5,
        'thermal_state': 'moderate',
        'power_save': false,
        'cpu_count': 8,
      };
      final telem = await driveTelemetry(fixture);

      // Raw bridge passthrough: map contents identical.
      expect(telem, equals(fixture));
      expect(telem['mem_avail_mb'], isA<int>());
      expect(telem['mem_total_mb'], isA<int>());
      expect(telem['storage_free_mb'], isA<int>());
      expect(telem['storage_total_mb'], isA<int>());
      expect(telem['battery_temp_c'], isA<double>());
      expect(telem['thermal_state'], isA<String>());
      expect(telem['power_save'], isA<bool>());
      expect(telem['cpu_count'], isA<int>());

      // Heartbeat-builder path (mirrors main.dart _tick narrowing).
      expect(_hbAsInt(telem['mem_avail_mb']), 1024);
      expect(_hbAsInt(telem['mem_total_mb']), 4096);
      expect(_hbAsInt(telem['storage_free_mb']), 20480);
      expect(_hbAsInt(telem['storage_total_mb']), 65536);
      expect(_hbAsDouble(telem['battery_temp_c']), 31.5);
      expect(_hbPowerSave(telem), isFalse);
      expect(_hbThermal(telem), 'moderate');
      // mem_pct derived device-side reading: (4096-1024)*100/4096 = 75.0.
      expect(_hbMemPct(telem), closeTo(75.0, 1e-9));
      expect(telem['cpu_count'], 8); // capacity only, never a load metric
    });

    test('2. partial map (only mem keys) -> others absent, not zero',
        () async {
      final telem = await driveTelemetry(<String, dynamic>{
        'mem_avail_mb': 512,
        'mem_total_mb': 2048,
      });

      expect(telem['mem_avail_mb'], 512);
      expect(telem['mem_total_mb'], 2048);
      for (final k in [
        'storage_free_mb',
        'storage_total_mb',
        'battery_temp_c',
        'thermal_state',
        'power_save',
        'cpu_count',
      ]) {
        expect(telem.containsKey(k), isFalse, reason: 'missing key: $k');
        expect(telem[k], isNull);
      }
      // Heartbeat narrowing yields null (= unknown), never zero.
      expect(_hbAsInt(telem['storage_free_mb']), isNull);
      expect(_hbAsInt(telem['storage_total_mb']), isNull);
      expect(_hbAsDouble(telem['battery_temp_c']), isNull);
      expect(_hbThermal(telem), isNull);
      expect(_hbPowerSave(telem), isNull);
      // mem_pct still derivable from the mem pair alone.
      expect(_hbMemPct(telem), closeTo(75.0, 1e-9));
    });

    test('3. PlatformException -> {} (heartbeat proceeds, no throw)',
        () async {
      mockTelem((_) async {
        throw PlatformException(code: 'UNAVAILABLE');
      });
      final telem = await DeviceBridge().telemetry();
      expect(telem, equals(<String, dynamic>{}));
    });

    test('4. null map -> {}', () async {
      final telem = await driveTelemetry(null);
      expect(telem, equals(<String, dynamic>{}));
    });

    test('5. garbage types -> no throw; kept-as-is at bridge, '
        'dropped (null) at heartbeat narrowing', () async {
      final telem = await driveTelemetry(<String, dynamic>{
        'mem_avail_mb': 'lots',
        'mem_total_mb': 4096,
        'battery_temp_c': 'warm',
        'power_save': 'yes',
        'thermal_state': 42,
      });

      // DOCUMENTED bridge behavior: kept-as-is (raw passthrough, no
      // coercion, no throw). Assert actual:
      expect(telem['mem_avail_mb'], equals('lots'));
      expect(telem['battery_temp_c'], equals('warm'));
      expect(telem['power_save'], equals('yes'));
      expect(telem['thermal_state'], equals(42));

      // DOCUMENTED heartbeat behavior: narrowing drops garbage to null
      // (never zero, never throw).
      expect(_hbAsInt(telem['mem_avail_mb']), isNull);
      expect(_hbAsDouble(telem['battery_temp_c']), isNull);
      expect(_hbPowerSave(telem), isNull);
      expect(_hbThermal(telem), isNull);
      expect(_hbMemPct(telem), isNull);
    });

    test('6. thermal_state strings pass through verbatim', () async {
      const states = [
        'none',
        'light',
        'moderate',
        'severe',
        'critical',
        'shutdown',
        'unknown-future-value',
      ];
      for (final s in states) {
        final telem = await driveTelemetry(<String, dynamic>{
          'thermal_state': s,
        });
        expect(telem['thermal_state'], equals(s));
        expect(_hbThermal(telem), equals(s),
            reason: 'bridge must not remap Core-allowlisted value: $s');
      }
    });
  });
}

// --- Heartbeat-narrowing mirrors (exact semantics of lib/main.dart) ---
// _asDouble/_asInt/_memPct + the `is bool` / `is String` guards in _tick.
// Kept local because the main.dart originals are library-private.

double? _hbAsDouble(Object? v) =>
    v is num && v.isFinite ? v.toDouble() : null;

int? _hbAsInt(Object? v) => v is num && v.isFinite ? v.toInt() : null;

double? _hbMemPct(Map<String, dynamic> telem) {
  final avail = _hbAsInt(telem['mem_avail_mb']);
  final total = _hbAsInt(telem['mem_total_mb']);
  if (avail == null || total == null || total <= 0 || avail > total) {
    return null;
  }
  return ((total - avail) * 100.0 / total).clamp(0.0, 100.0);
}

bool? _hbPowerSave(Map<String, dynamic> telem) =>
    telem['power_save'] is bool ? telem['power_save'] as bool : null;

String? _hbThermal(Map<String, dynamic> telem) =>
    telem['thermal_state'] is String ? telem['thermal_state'] as String : null;
