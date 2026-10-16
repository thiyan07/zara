import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/main.dart';

// Cross-device targeting: fleet-driven device choice, pinned submit,
// requested-vs-authoritative display. Client wire shapes run against a
// loopback HttpServer (same convention as stage15_tasks_test.dart).

Map<String, dynamic> _dev(String id,
    {String trust = 'trusted',
    String presence = 'online',
    List<String> caps = const ['system.battery']}) {
  return {
    'device_id': id,
    'trust': trust,
    'presence': presence,
    'capabilities': caps,
    'last_seen': '2026-10-13T00:00:00Z',
  };
}

void main() {
  group('fleetTargetOptions (device selector model)', () {
    test('default self-target always first and enabled', () {
      final opts = fleetTargetOptions(
          fleet: [], selfId: 'android-phone', tool: 'system.battery');
      expect(opts.length, 1);
      expect(opts.first.id, '');
      expect(opts.first.label, contains('This device'));
      expect(opts.first.enabled, isTrue);
    });

    test('healthy remote enabled with identity summary', () {
      final opts = fleetTargetOptions(
          fleet: [_dev('laptop-1')], selfId: 'android-phone',
          tool: 'system.battery');
      expect(opts.length, 2);
      expect(opts[1].id, 'laptop-1');
      expect(opts[1].enabled, isTrue);
      expect(opts[1].reason, isEmpty);
    });

    test('offline/stale/unknown/revoked/untrusted/cap-missing disabled', () {
      final cases = {
        'offline laptop':
            _dev('d-off', presence: 'offline'),
        'stale laptop':
            _dev('d-stale', presence: 'stale'),
        'unknown presence':
            _dev('d-unk', presence: 'unknown'),
        'revoked device':
            _dev('d-rev', trust: 'revoked'),
        'pairing device':
            _dev('d-pair', trust: 'pairing'),
        'unknown trust':
            _dev('d-utrust', trust: 'mystery'),
        'capability mismatch':
            _dev('d-nocap', caps: ['system.network']),
      };
      for (final entry in cases.entries) {
        final opts = fleetTargetOptions(
            fleet: [entry.value], selfId: 'android-phone',
            tool: 'system.battery');
        expect(opts.length, 2, reason: entry.key);
        expect(opts[1].enabled, isFalse, reason: entry.key);
        expect(opts[1].reason, isNotEmpty, reason: entry.key);
      }
      expect(
          fleetTargetOptions(
              fleet: [cases['revoked device']!], selfId: 'android-phone',
              tool: 'system.battery')[1].reason,
          contains('revoked'));
    });

    test('degraded and temporarily-trusted stay eligible (Core gates load)',
        () {
      final opts = fleetTargetOptions(
          fleet: [
            _dev('d-deg', presence: 'degraded'),
            _dev('d-tmp', trust: 'temporarily_trusted'),
          ],
          selfId: 'android-phone',
          tool: 'system.battery');
      expect(opts[1].enabled, isTrue);
      expect(opts[2].enabled, isTrue);
    });

    test('util.echo lists remotes disabled (local tool, self-only)', () {
      final opts = fleetTargetOptions(
          fleet: [_dev('laptop-1')], selfId: 'android-phone',
          tool: 'util.echo');
      expect(opts.length, 2);
      expect(opts[1].enabled, isFalse);
      expect(opts[1].reason, contains('self-only'));
    });

    test('self excluded from remotes; dup ids and unknown shapes safe', () {
      final opts = fleetTargetOptions(
          fleet: [
            _dev('android-phone'),
            _dev('laptop-1'),
            _dev('laptop-1'),
            {'weird': 'shape'},
            {'device_id': '', 'trust': 'trusted', 'presence': 'online'},
          ],
          selfId: 'android-phone',
          tool: 'system.battery');
      expect(opts.where((o) => o.id == 'android-phone'), isEmpty);
      expect(opts.where((o) => o.id == 'laptop-1').length, 1);
      // Unknown shapes render disabled, never enabled blindly.
      for (final o in opts.skip(2)) {
        expect(o.enabled, isFalse, reason: o.id);
      }
    });
  });

  group('describeTaskDevices (requested vs authoritative)', () {
    test('full combo: target, verified executor, substitution', () {
      expect(
          describeTaskDevices({
            'requested_device': 'laptop-1',
            'device_id': 'laptop-2',
            'verified': true,
            'binding': {'substituted': true},
          }),
          equals([
            'target: laptop-1',
            'runs on: laptop-2 (verified)',
            'substituted: Core routed to laptop-2 instead of laptop-1',
          ]));
    });

    test('parked row shows target only, never invented executor', () {
      expect(
          describeTaskDevices(
              {'requested_device': 'laptop-1', 'device_id': ''}),
          equals(['target: laptop-1']));
    });

    test('executor without request shows runs-on only', () {
      expect(
          describeTaskDevices({'device_id': 'android-phone'}),
          equals(['runs on: android-phone']));
    });

    test('substitution needs both sides; unverified has no badge', () {
      expect(
          describeTaskDevices({
            'requested_device': 'a',
            'device_id': 'a',
            'binding': {'substituted': false},
          }),
          equals(['target: a', 'runs on: a']));
      // Server-flagged substitution without a request annotation:
      // show executor, claim no substitution story.
      expect(
          describeTaskDevices(
              {'device_id': 'b', 'binding': {'substituted': true}}),
          equals(['runs on: b']));
    });

    test('empty row renders nothing', () {
      expect(describeTaskDevices({}), isEmpty);
    });
  });

  group('mergeTaskRow (refresh/cancel row merge)', () {
    test('requested target survives, authoritative fields win', () {
      final merged = mergeTaskRow(
        {
          'id': 'ex-1',
          'device_id': 'old-device',
          'status': 'running',
          'requested_device': 'laptop-1',
        },
        {
          'id': 'ex-1',
          'device_id': 'laptop-1',
          'status': 'succeeded',
          'verified': true,
        },
      );
      expect(merged['requested_device'], 'laptop-1');
      expect(merged['device_id'], 'laptop-1');
      expect(merged['status'], 'succeeded');
      expect(merged['verified'], isTrue);
    });

    test('annotation never promoted into device_id', () {
      final merged = mergeTaskRow(
        {'id': 'ex-2', 'requested_device': 'laptop-1'},
        {'id': 'ex-2', 'status': 'waiting_for_permission'},
      );
      expect(merged['requested_device'], 'laptop-1');
      expect(merged.containsKey('device_id'), isFalse);
    });

    test('rows without annotation pass through untouched', () {
      const fresh = {'id': 'ex-3', 'device_id': 'android-phone'};
      expect(mergeTaskRow(const {'id': 'ex-3'}, fresh),
          equals({'id': 'ex-3', 'device_id': 'android-phone'}));
    });
  });

  group('Tasks tab target selector (widget)', () {
    const deviceChannel = MethodChannel('zara/device');
    const secureChannel =
        MethodChannel('plugins.it_nomads.com/flutter_secure_storage');

    setUp(() {
      TestWidgetsFlutterBinding.ensureInitialized();
      TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(
              deviceChannel, (call) async => <String, dynamic>{});
      TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(secureChannel, (call) async => null);
    });

    tearDown(() {
      TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(deviceChannel, null);
      TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(secureChannel, null);
    });

    testWidgets('Run-on dropdown defaults to This device', (t) async {
      await t.pumpWidget(const ZaraApp());
      await t.pumpAndSettle();
      await t.tap(find.text('Tasks'));
      await t.pumpAndSettle();
      // Target selector sits below Submit: scroll into view first.
      await t.scrollUntilVisible(find.text('Run on (fleet)'), 400,
          scrollable: find.byType(Scrollable).first);
      await t.pumpAndSettle();
      expect(find.text('Run on (fleet)'), findsOneWidget);
      expect(find.text('This device (default)'), findsWidgets);
      // No fleet loaded: no remote options, Refresh control present.
      expect(find.text('Refresh devices'), findsOneWidget);
    });
  });
}
