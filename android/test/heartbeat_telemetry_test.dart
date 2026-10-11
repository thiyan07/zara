// Heartbeat telemetry contract (observation only, never authority).
//
// Verified live against:
//   android/lib/main.dart:93-107  (_asDouble/_asInt/_memPct + _tick builder)
//   android/lib/core_client.dart:124-149 (heartbeat wire body)
//   android/lib/device_bridge.dart:31-43 (telemetry() never throws, {} on fail)
//
// Honest reachability note (deliverable 1):
//   _memPct/_asInt/_asDouble are top-level PRIVATE functions in main.dart
//   (leading underscore = library-private in Dart). A test in test/ is a
//   different library and CANNOT import them — `import main.dart` would not
//   expose them. The real builder path (_ZaraAppState._tick, main.dart:419)
//   additionally needs platform channels (DeviceBridge MethodChannel
//   'zara/device'), SecureStore, and lifecycle state, i.e. emulator territory.
//   So this file tests (a) an exact formula COPY of _memPct/_asInt/_asDouble
//   (kept byte-faithful to main.dart:93-107; divergence risk documented
//   below) and (b) the real CoreClient.heartbeat() wire path via loopback
//   HttpServer asserting the RAW JSON body. No production files modified.
//
//   DIVERGENCE RISK: if main.dart:100-107 changes, the local _memPctCopy must
//   be updated by hand; the heartbeat body-capture tests below still guard
//   the wire contract regardless.
//
// cpu_pct is deliberately omitted client-side (main.dart:419-451 builds the
// heartbeat without it; core_client.dart:140-141 documents why). Asserted.

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';

// --- Exact copies of main.dart:93-107 (see header note) ---
double? asDoubleCopy(Object? v) =>
    v is num && v.isFinite ? v.toDouble() : null;

int? asIntCopy(Object? v) => v is num && v.isFinite ? v.toInt() : null;

double? memPctCopy(Map<String, dynamic> telem) {
  final avail = asIntCopy(telem['mem_avail_mb']);
  final total = asIntCopy(telem['mem_total_mb']);
  if (avail == null || total == null || total <= 0 || avail > total) {
    return null;
  }
  return ((total - avail) * 100.0 / total).clamp(0.0, 100.0);
}

/// Narrowing as _tick does (main.dart:440-449): validated scalars or null.
Map<String, dynamic> narrowTelemCopy(Map<String, dynamic> telem) => {
      'low_power_mode':
          telem['power_save'] is bool ? telem['power_save'] as bool : null,
      'mem_pct': memPctCopy(telem),
      'storage_free_mb': asIntCopy(telem['storage_free_mb']),
      'storage_total_mb': asIntCopy(telem['storage_total_mb']),
      'battery_temp_c': asDoubleCopy(telem['battery_temp_c']),
      'thermal_state':
          telem['thermal_state'] is String ? telem['thermal_state'] as String : null,
    };

class _HbCap {
  String method = '';
  String path = '';
  final headers = <String, String>{};
  String rawBody = '';
  Map<String, dynamic> body = {};
  int status = 200;
  Map<String, dynamic> reply = {'ok': true};
}

Future<HttpServer> _serve(_HbCap cap) async {
  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
  server.listen((req) async {
    cap.method = req.method;
    cap.path = req.uri.path;
    req.headers.forEach((k, v) => cap.headers[k] = v.join(','));
    cap.rawBody = await utf8.decoder.bind(req).join();
    cap.body = cap.rawBody.isEmpty
        ? <String, dynamic>{}
        : (jsonDecode(cap.rawBody) as Map<String, dynamic>);
    req.response.statusCode = cap.status;
    req.response.headers.contentType = ContentType.json;
    req.response.write(jsonEncode(cap.reply));
    await req.response.close();
  });
  return server;
}

CoreClient _client(HttpServer s) {
  final c = CoreClient('http://127.0.0.1:${s.port}',
      timeout: const Duration(seconds: 5));
  c.deviceId = 'android-phone';
  c.deviceKey = 'k1';
  return c;
}

void main() {
  group('_memPct formula copy (main.dart:100-107)', () {
    test('computes avail/total correctly', () {
      // (2000-1500)*100/2000 = 25.0
      expect(memPctCopy({'mem_avail_mb': 1500, 'mem_total_mb': 2000}), 25.0);
      expect(memPctCopy({'mem_avail_mb': 0, 'mem_total_mb': 2000}), 100.0);
      expect(memPctCopy({'mem_avail_mb': 2000, 'mem_total_mb': 2000}), 0.0);
    });

    test('null unless both present and sane', () {
      expect(memPctCopy({}), isNull);
      expect(memPctCopy({'mem_avail_mb': 500}), isNull);
      expect(memPctCopy({'mem_total_mb': 2000}), isNull);
      expect(memPctCopy({'mem_avail_mb': 500, 'mem_total_mb': 0}), isNull);
      expect(memPctCopy({'mem_avail_mb': 500, 'mem_total_mb': -8}), isNull);
      expect(memPctCopy({'mem_avail_mb': 3000, 'mem_total_mb': 2000}),
          isNull); // avail > total
      expect(
          memPctCopy({'mem_avail_mb': 'lots', 'mem_total_mb': 2000}), isNull);
      expect(memPctCopy(
          {'mem_avail_mb': double.nan, 'mem_total_mb': 2000}), isNull);
    });

    test('narrowing helpers: finite nums pass, else null (never zero)', () {
      expect(asIntCopy(42), 42);
      expect(asIntCopy(42.9), 42);
      expect(asDoubleCopy(36.6), 36.6);
      expect(asIntCopy(double.nan), isNull);
      expect(asIntCopy(double.infinity), isNull);
      expect(asDoubleCopy(double.infinity), isNull);
      expect(asIntCopy('x'), isNull);
      expect(asDoubleCopy(null), isNull);
    });
  });

  group('heartbeat wire body (loopback, raw JSON)', () {
    test('exact snake_case telemetry keys present, nulls preserved as null',
        () async {
      final cap = _HbCap();
      final server = await _serve(cap);
      try {
        await _client(server).heartbeat(
          batteryPct: 55.0,
          charging: false,
          network: 'wifi',
          lowPowerMode: null,
          memPct: null,
          storageFreeMb: null,
          storageTotalMb: null,
          batteryTempC: null,
          thermalState: null,
        );
        expect(cap.method, 'POST');
        expect(cap.path, '/v1/agent/heartbeat');
        for (final k in [
          'mem_pct',
          'storage_free_mb',
          'storage_total_mb',
          'battery_temp_c',
          'thermal_state',
          'low_power_mode',
        ]) {
          expect(cap.body.containsKey(k), isTrue, reason: k);
          expect(cap.body[k], isNull, reason: k);
          expect(cap.rawBody, contains('"$k":null'), reason: k);
        }
        // Presence fields ride along.
        expect(cap.body['battery_pct'], 55.0);
        expect(cap.body['charging'], isFalse);
        expect(cap.body['network'], 'wifi');
        expect(cap.body['online'], isTrue);
        // cpu_pct deliberately omitted client-side.
        expect(cap.body.containsKey('cpu_pct'), isFalse);
        expect(cap.rawBody, isNot(contains('cpu_pct')));
      } finally {
        await server.close(force: true);
      }
    });

    test('populated telemetry values arrive under the same keys', () async {
      final cap = _HbCap();
      final server = await _serve(cap);
      try {
        // Mirror of _tick narrowing for a healthy snapshot.
        final telem = {
          'power_save': true,
          'mem_avail_mb': 1500,
          'mem_total_mb': 2000,
          'storage_free_mb': 12345,
          'storage_total_mb': 65536,
          'battery_temp_c': 31.5,
          'thermal_state': 'moderate',
        };
        final n = narrowTelemCopy(telem);
        await _client(server).heartbeat(
          batteryPct: 80.0,
          charging: true,
          network: 'cellular',
          lowPowerMode: n['low_power_mode'] as bool?,
          memPct: n['mem_pct'] as double?,
          storageFreeMb: n['storage_free_mb'] as int?,
          storageTotalMb: n['storage_total_mb'] as int?,
          batteryTempC: n['battery_temp_c'] as double?,
          thermalState: n['thermal_state'] as String?,
        );
        expect(cap.body['mem_pct'], 25.0);
        expect(cap.body['storage_free_mb'], 12345);
        expect(cap.body['storage_total_mb'], 65536);
        expect(cap.body['battery_temp_c'], 31.5);
        expect(cap.body['thermal_state'], 'moderate');
        expect(cap.body['low_power_mode'], isTrue);
      } finally {
        await server.close(force: true);
      }
    });
  });

  group('null telem map never breaks heartbeat delivery', () {
    test('empty telem -> presence fields still POST, telemetry null', () async {
      final cap = _HbCap();
      final server = await _serve(cap);
      try {
        // DeviceBridge.telemetry() yields {} on channel failure
        // (device_bridge.dart:38-42); _tick narrows that to all-nulls.
        final n = narrowTelemCopy({});
        expect(n.values.every((v) => v == null), isTrue);
        final out = await _client(server).heartbeat(
          batteryPct: 10.0,
          charging: false,
          network: 'unavailable',
          lowPowerMode: n['low_power_mode'] as bool?,
          memPct: n['mem_pct'] as double?,
          storageFreeMb: n['storage_free_mb'] as int?,
          storageTotalMb: n['storage_total_mb'] as int?,
          batteryTempC: n['battery_temp_c'] as double?,
          thermalState: n['thermal_state'] as String?,
        );
        expect(out['ok'], isTrue); // delivery succeeded
        expect(cap.body['battery_pct'], 10.0);
        expect(cap.body['charging'], isFalse);
        expect(cap.body['network'], 'unavailable');
        expect(cap.body['online'], isTrue);
      } finally {
        await server.close(force: true);
      }
    });
  });

  group('out-of-range passthrough (Core validates; client never clamps)', () {
    test('heartbeat() forwards extreme values untouched', () async {
      final cap = _HbCap();
      final server = await _serve(cap);
      try {
        await _client(server).heartbeat(
          batteryPct: -50.0,
          charging: false,
          network: 'wifi',
          lowPowerMode: false,
          memPct: 250.0,
          storageFreeMb: -1,
          storageTotalMb: -99,
          batteryTempC: 999.9,
          thermalState: 'bogus-state-never-emitted',
        );
        // No clamping, no validation, no omission client-side:
        // core_client.dart:135-148 builds the map verbatim.
        expect(cap.body['battery_pct'], -50.0);
        expect(cap.body['mem_pct'], 250.0);
        expect(cap.body['storage_free_mb'], -1);
        expect(cap.body['storage_total_mb'], -99);
        expect(cap.body['battery_temp_c'], 999.9);
        expect(cap.body['thermal_state'], 'bogus-state-never-emitted');
        expect(cap.rawBody, contains('"mem_pct":250.0'));
        expect(cap.rawBody, contains('"storage_free_mb":-1'));
      } finally {
        await server.close(force: true);
      }
    });

    test('narrowing helpers do not clamp finite extremes either', () {
      // _asInt/_asDouble pass any finite num through (main.dart:93-96).
      expect(asIntCopy(-1), -1);
      expect(asDoubleCopy(999.9), 999.9);
      // NOTE / honest nuance: _memPct itself clamps its DERIVED percentage
      // to 0-100 (main.dart:106) and returns null for insane avail/total,
      // so a derived mem_pct can never be out of range by construction.
      // The no-clamp guarantee above covers CoreClient.heartbeat(), which is
      // the actual wire path and forwards whatever it is given.
    });
  });

  group('heartbeat auth headers', () {
    test('X-Device-Id/Key present', () async {
      final cap = _HbCap();
      final server = await _serve(cap);
      try {
        await _client(server).heartbeat(
          batteryPct: 50.0,
          charging: true,
          network: 'wifi',
        );
        expect(cap.headers['x-device-id'], 'android-phone');
        expect(cap.headers['x-device-key'], 'k1');
      } finally {
        await server.close(force: true);
      }
    });
  });
}
