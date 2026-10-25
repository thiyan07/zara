// Universal-operator contract tests (fake probes, socket-free):
//
// - 'android.apps.list': launchable-app discovery — limit clamping, row shape,
//   unavailable-gateway refusal, unexpected-shape refusal, and the
//   discovery-is-not-authorization note.
// - 'gui.back': guarded global navigation — lab-target refusal, direction
//   handling, mapped bridge refusals (mismatch/unavailable), and
//   fresh-snapshot passthrough on success.
// - 'system.app.open': focus-verification passthrough (the device may
//   report honest focus_unconfirmed).
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/job_runner.dart';

/// Minimal stand-in for a coded bridge-channel failure (mirrors
/// A11yBridgeException's {code, message, details} shape).
class _FakeBridgeError implements Exception {
  final String code;
  final String message;
  final Map<String, dynamic> details;
  _FakeBridgeError(this.code, this.message, [this.details = const {}]);
  @override
  String toString() => '_FakeBridgeError($code): $message';
}

DeviceJob backJob(Map<String, dynamic> inputs, [String id = 'job-back']) =>
    DeviceJob(
        jobId: id,
        tool: 'gui.back',
        inputs: inputs,
        timeoutS: 20,
        executionId: 'exec-back');

AndroidJobRunner navRunner(
        Future<Map<String, dynamic>> Function(String, String,
                Map<String, dynamic>?)
            fn) =>
    AndroidJobRunner(
      battery: () => {},
      network: () => {},
      a11yNavigate: (dir, pkg, [options]) =>
          fn(dir, pkg, options ?? const {}),
    );

DeviceJob listJob(Map<String, dynamic> inputs, [String id = 'job-list']) =>
    DeviceJob(
        jobId: id,
        tool: 'android.apps.list',
        inputs: inputs,
        timeoutS: 20,
        executionId: 'exec-list');

AndroidJobRunner listRunner(
        Future<Map<String, dynamic>> Function(int) fn) =>
    AndroidJobRunner(
      battery: () => {},
      network: () => {},
      appsList: fn,
    );

void main() {
  group('gui.back (guarded global navigation)', () {
    test('success returns direction + fresh snapshot + verify pointer',
        () async {
      Map<String, dynamic>? seenOptions;
      String? seenDir;
      final r = navRunner((dir, pkg, options) async {
        seenDir = dir;
        seenOptions = options;
        return {
          'supported': true,
          'applied': true,
          'direction': 'back',
          'package_before': 'dev.zara.zara_android',
          'package_after': 'com.android.launcher',
          'fresh_snapshot_id': 'fff012ab:3',
          'verify_state': 'applied (framework accepted)',
        };
      });
      final out = await r.run(backJob({
        'target_package': 'dev.zara.zara_android',
        'expected_package': 'dev.zara.zara_android',
        'expected_snapshot_id': 'abcd1234:5',
      }));
      expect(out.ok, isTrue);
      expect(seenDir, equals('back'));
      expect(seenOptions!['expected_package'],
          equals('dev.zara.zara_android'));
      expect(out.result['fresh_snapshot_id'], equals('fff012ab:3'));
      expect(out.result['package_after'], equals('com.android.launcher'));
      expect(out.result['verify'], equals('gui.screen.inspect'));
    });

    test('direction: home is honored; unknown/default maps to back',
        () async {
      final seen = <String>[];
      final r = navRunner((dir, pkg, options) async {
        seen.add(dir);
        return {'supported': true, 'applied': true, 'verify_state': 'applied'};
      });
      await r.run(backJob({'target_package': 'dev.zara.zara_android',
          'direction': 'home'}, 'job-back-home'));
      await r.run(backJob(
          {'target_package': 'dev.zara.zara_android', 'direction': 'recents'},
          'job-back-unknown'));
      await r.run(backJob(
          {'target_package': 'dev.zara.zara_android'}, 'job-back-default'));
      expect(seen, equals(['home', 'back', 'back']));
    });

    test('out-of-lab target refused without touching the gateway',
        () async {
      var called = false;
      final r = navRunner((dir, pkg, options) async {
        called = true;
        return {'applied': true};
      });
      final out = await r.run(
          backJob({'target_package': 'com.some.thirdparty'}, 'job-out'));
      expect(out.ok, isFalse);
      expect(called, isFalse);
      expect('${out.error}', contains('not in lab allowlist'));
    });

    test('UNEXPECTED_PACKAGE bridge refusal is structured, never applied',
        () async {
      final r = navRunner((dir, pkg, options) async {
        throw _FakeBridgeError('mismatch', 'unexpected package',
            {'actual_package': 'com.other.app'});
      });
      final out = await r.run(backJob({
        'target_package': 'dev.zara.zara_android',
        'expected_package': 'dev.zara.zara_android',
      }));
      expect(out.ok, isFalse);
      expect('${out.error}', contains('foreground package changed'));
      expect(out.result['actual_package'], equals('com.other.app'));
    });

    test('NO_WINDOW refusal (unbound a11y) is honest unavailability',
        () async {
      final r = navRunner((dir, pkg, options) async {
        throw _FakeBridgeError('unavailable', 'no window');
      });
      final out = await r.run(
          backJob({'target_package': 'dev.zara.zara_android'}, 'job-now'));
      expect(out.ok, isFalse);
      expect('${out.error}', contains('a11y service unavailable'));
    });

    test('non-applied native result is a failure with evidence',
        () async {
      final r = navRunner((dir, pkg, options) async => <String, dynamic>{
            'supported': false,
            'applied': false,
            'error': 'NAV_FAILED',
          });
      final out = await r.run(
          backJob({'target_package': 'dev.zara.zara_android'}, 'job-nf'));
      expect(out.ok, isFalse);
      expect('${out.error}', contains('NAV_FAILED'));
    });
  });

  group('apps.list (launchable-app discovery)', () {
    test('default limit 100, rows shaped package+label, never more',
        () async {
      int? seenLimit;
      final r = listRunner((limit) async {
        seenLimit = limit;
        return {
          'supported': true,
          'requested_limit': limit,
          'count': 2,
          'truncated': false,
          'apps': [
            {'package': 'dev.zara.zara_android', 'label': 'Zara'},
            {'package': 'com.android.chrome', 'label': 'Chrome'},
          ],
        };
      });
      final out = await r.run(listJob({}));
      expect(out.ok, isTrue);
      expect(seenLimit, equals(100));
      expect(out.result['count'], equals(2));
      expect(out.result['truncated'], isFalse);
      final rows = out.result['apps'] as List;
      expect(rows.singleWhere((row) => row['package'] ==
          'dev.zara.zara_android')['label'], equals('Zara'));
    });

    test('limit clamped to 1-200 and passthrough of truncation flag',
        () async {
      final seen = <int>[];
      final r = listRunner((limit) async {
        seen.add(limit);
        return {'supported': true, 'truncated': true, 'apps': [
          {'package': 'a.b', 'label': 'x'},
        ]};
      });
      await r.run(listJob({'limit': 500}, 'job-over'));
      await r.run(listJob({'limit': 0}, 'job-under'));
      await r.run(listJob({'limit': 'NaN'}, 'job-bad'));
      expect(seen, equals([200, 1, 100]));
    });

    test('unavailable gateway is an honest refusal', () async {
      final r = AndroidJobRunner(battery: () => {}, network: () => {});
      final out = await r.run(listJob({}, 'job-no'));
      expect(out.ok, isFalse);
      expect('${out.error}', contains('unavailable'));
    });

    test('unexpected native shape refused, never guessed into rows',
        () async {
      final r = listRunner((limit) async => <String, dynamic>{
            'supported': true,
          });
      final out = await r.run(listJob({}, 'job-shape'));
      expect(out.ok, isFalse);
      expect('${out.error}', contains('unexpected shape'));
    });
  });

  group('notifications.read (passive listener read)', () {
    DeviceJob notifJob(Map<String, dynamic> inputs, [String id = 'job-notif']) =>
        DeviceJob(
            jobId: id,
            tool: 'android.notifications.read',
            inputs: inputs,
            timeoutS: 20,
            executionId: 'exec-notif');

    AndroidJobRunner notifRunner(
            Future<Map<String, dynamic>> Function(Map<String, dynamic>)
                fn) =>
        AndroidJobRunner(
          battery: () => {},
          network: () => {},
          notifList: (params) => fn(params),
        );

    test('default bounds, rows shaped, scrubbed-at-capture note',
        () async {
      Map<String, dynamic>? seenParams;
      final r = notifRunner((params) async {
        seenParams = params;
        return {
          'granted': true,
          'count': 1,
          'truncated': false,
          'items': [
            {
              'posted_at_ms': 1700000000000,
              'package': 'com.example.mail',
              'title': 'Mail',
              'text': 'meeting at 4',
              'category': 'email',
            },
          ],
        };
      });
      final out = await r.run(notifJob({}));
      expect(out.ok, isTrue);
      expect(seenParams?['since_ms'], equals(0));
      expect(seenParams?['limit'], equals(20));
      expect(out.result['count'], equals(1));
      final rows = out.result['items'] as List;
      expect((rows.first as Map)['package'], equals('com.example.mail'));
      expect('${out.result['note']}', contains('scrubbed'));
    });

    test('limit clamped to 1-50 and since_ms coerced to 0', () async {
      final seen = <Map<String, dynamic>>[];
      final r = notifRunner((params) async {
        seen.add(params);
        return {'granted': true, 'count': 0, 'truncated': false,
            'items': []};
      });
      await r.run(notifJob({'limit': 99, 'since_ms': 5}, 'job-over'));
      await r.run(notifJob({'limit': 0}, 'job-under'));
      await r.run(notifJob({'limit': 'NaN'}, 'job-bad'));
      expect(seen.map((p) => p['limit']).toList(), equals([50, 1, 20]));
      expect(seen.first['since_ms'], equals(5));
      expect(seen.last['since_ms'], equals(0));
    });

    test('missing user grant is a refusal, never fake content',
        () async {
      final r = notifRunner((params) async =>
          {'supported': true, 'granted': false, 'items': []});
      final out = await r.run(notifJob({}, 'job-denied'));
      expect(out.ok, isFalse);
      expect('${out.error}', contains('not granted'));
      expect('${out.error}', contains('Settings'));
    });

    test('unavailable gateway is an honest refusal', () async {
      final r = AndroidJobRunner(battery: () => {}, network: () => {});
      final out = await r.run(notifJob({}, 'job-none'));
      expect(out.ok, isFalse);
      expect('${out.error}', contains('unavailable'));
    });

    test('unexpected native shape refused, never guessed into rows',
        () async {
      final r = notifRunner(
          (params) async => {'granted': true, 'supported': true});
      final out = await r.run(notifJob({}, 'job-shape'));
      expect(out.ok, isFalse);
      expect('${out.error}', contains('unexpected shape'));
    });
  });

  group('system.app.open verification passthrough', () {
    test('verify_state and verify pointer flow through', () async {
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        openApp: (pkg) async => {
          'package': pkg,
          'launched': true,
          'verify_state': 'focused_verified',
          'verify': 'gui.screen.inspect',
        },
      );
      final out = await r.run(DeviceJob(
        jobId: 'job-open',
        tool: 'system.app.open',
        inputs: {'package': 'dev.zara.zara_android'},
        timeoutS: 20,
        executionId: 'exec-open',
      ));
      expect(out.ok, isTrue);
      expect(out.result['verify_state'], equals('focused_verified'));
      expect(out.result['verify'], equals('gui.screen.inspect'));
    });

    test('out-of-allowlist package refused before any launcher call',
        () async {
      var called = false;
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        openApp: (pkg) async {
          called = true;
          return {};
        },
      );
      final out = await r.run(DeviceJob(
        jobId: 'job-open-bad',
        tool: 'system.app.open',
        inputs: {'package': 'com.random.app'},
        timeoutS: 20,
        executionId: 'exec-open-bad',
      ));
      expect(out.ok, isFalse);
      expect(called, isFalse);
      expect('${out.error}', contains('allowlist'));
    });
  });

  group('gui.find (read-only semantic lookup, contract v3)', () {
    DeviceJob findJob(Map<String, dynamic> inputs,
            [String id = 'job-find']) =>
        DeviceJob(
            jobId: id,
            tool: 'gui.find',
            inputs: inputs,
            timeoutS: 20,
            executionId: 'exec-find');

    AndroidJobRunner findRunner(
            Future<Map<String, dynamic>> Function(String,
                    Map<String, dynamic>, Map<String, dynamic>?)
                fn) =>
        AndroidJobRunner(
          battery: () => {},
          network: () => {},
          a11yFind: (pkg, target, [options]) =>
              fn(pkg, target, options ?? const {}),
        );

    test('MATCH passes element facts through as an observation', () async {
      Map<String, dynamic>? seenTarget, seenOptions;
      String? seenPkg;
      final r = findRunner((pkg, target, options) async {
        seenPkg = pkg;
        seenTarget = target;
        seenOptions = options;
        return {
          'supported': true,
          'applied': false,
          'status': 'MATCH',
          'package': 'dev.zara.zara_android',
          'snapshot_id': 'a1b2c3d4:9',
          'contract_version': 3,
          'element': {
            'node_id': 6,
            'resource_id': 'chat_input',
            'content_desc': '',
            'text': 'Say something',
            'role': 'android.widget.EditText',
            'bounds': [0, 100, 540, 160],
            'clickable': false,
            'editable': true,
          },
          'note': 'read-only lookup; no element was acted on',
        };
      });
      final out = await r.run(findJob({
        'target_package': 'dev.zara.zara_android',
        'target': {'resource_id': 'chat_input'},
      }));
      expect(out.ok, isTrue);
      expect(seenPkg, equals('dev.zara.zara_android'));
      expect(seenTarget!['resource_id'], equals('chat_input'));
      expect(seenOptions!['target'], isNotNull);
      expect(out.result['status'], equals('MATCH'));
      expect(out.result['element']['node_id'], equals(6));
      expect(out.result['element']['editable'], isTrue);
      expect(out.result['snapshot_id'], equals('a1b2c3d4:9'));
      expect(out.result['note'], contains('no element was acted on'));
    });

    test('not-found bridge refusal is a failure with evidence', () async {
      final r = findRunner((pkg, target, options) async {
        throw _FakeBridgeError(
            'failed', 'TARGET_NOT_FOUND: no element matches the target',
            {
              'status': 'TARGET_NOT_FOUND',
              'snapshot_id': 'a1b2c3d4:9',
              'package': 'dev.zara.zara_android',
            });
      });
      final out = await r.run(findJob({
        'target_package': 'dev.zara.zara_android',
        'target': {'text': 'definitely absent'},
      }));
      expect(out.ok, isFalse);
      expect(out.error, contains('no element matches the target'));
      expect(out.result['snapshot_id'], equals('a1b2c3d4:9'));
    });

    test('ambiguous refusal surfaces honestly, never a guessed pick',
        () async {
      final r = findRunner((pkg, target, options) async {
        throw _FakeBridgeError('failed', 'TARGET_AMBIGUOUS: 2 match', {
          'error': 'TARGET_AMBIGUOUS',
          'candidates': [
            {'node_id': 1, 'resource_id': 'title'},
            {'node_id': 3, 'resource_id': 'title'},
          ],
        });
      });
      final out = await r.run(findJob({
        'target_package': 'dev.zara.zara_android',
        'target': {'content_desc': 'inbox'},
      }));
      expect(out.ok, isFalse);
      expect(out.error, contains('several elements match'));
      expect(out.result['candidates'], hasLength(2));
    });

    test('validation refusals happen before any gateway call', () async {
      var called = false;
      final r = findRunner((pkg, target, options) async {
        called = true;
        return {'status': 'MATCH', 'element': {}};
      });
      // Out-of-lab package.
      var out = await r.run(
          findJob({'target_package': 'com.random.app',
              'target': {'text': 'x'}}, 'job-find-lab'));
      expect(out.ok, isFalse);
      expect(out.error, contains('allowlist'));
      // Secret-shaped selector.
      out = await r.run(findJob({
        'target_package': 'dev.zara.zara_android',
        'target': {'resource_id': 'com.app:id/password'},
      }, 'job-find-risk'));
      expect(out.ok, isFalse);
      expect(out.error, contains('secret-shaped'));
      // Unknown selector key is refused, never silently dropped.
      out = await r.run(findJob({
        'target_package': 'dev.zara.zara_android',
        'target': {'text': 'hi', 'color': 'red'},
      }, 'job-find-unknown'));
      expect(out.ok, isFalse);
      expect(out.error, contains('unknown selector'));
      // Selector-less target is refused.
      out = await r.run(findJob({
        'target_package': 'dev.zara.zara_android',
        'target': {'index_hint': 0},
      }, 'job-find-empty'));
      expect(out.ok, isFalse);
      expect(out.error, contains('at least one selector'));
      // Missing target entirely.
      out = await r.run(findJob({
        'target_package': 'dev.zara.zara_android',
      }, 'job-find-none'));
      expect(out.ok, isFalse);
      expect(out.error, contains('missing target'));
      expect(called, isFalse);
    });

    test('unavailable gateway is an honest refusal', () async {
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
      );
      final out = await r.run(findJob({
        'target_package': 'dev.zara.zara_android',
        'target': {'text': 'Send'},
      }));
      expect(out.ok, isFalse);
      expect(out.error, contains('a11y gateway unavailable'));
    });
  });

  group('tap/set_text semantic target forwarding (contract v3)', () {
    test('tap with target forwards it and node_id stays -1', () async {
      int? seenNodeId;
      Map<String, dynamic>? seenOptions;
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        a11yTap: (pkg, nodeId, [options]) async {
          seenNodeId = nodeId;
          seenOptions = options ?? const {};
          return {
            'supported': true,
            'applied': true,
            'package': 'dev.zara.zara_android',
            'snapshot_id': 'b1b2b3b4:7',
            'contract_version': 3,
            'verify_state': 'clicked',
            'target_matched': {
              'node_id': 6,
              'resource_id': 'chat_input',
              'role': 'android.widget.EditText',
            },
          };
        },
      );
      final out = await r.run(DeviceJob(
        jobId: 'job-tapt',
        tool: 'gui.tap',
        inputs: {
          'target_package': 'dev.zara.zara_android',
          'target': {'resource_id': 'chat_input'},
          'expected_snapshot_id': 'b1b2b3b4:7',
        },
        timeoutS: 20,
        executionId: 'exec-tapt',
      ));
      expect(out.ok, isTrue);
      expect(seenNodeId, equals(-1));
      expect(seenOptions!['target'], isNotNull);
      expect(seenOptions!['target']['resource_id'],
          equals('chat_input'));
      expect(seenOptions!['expected_snapshot_id'], equals('b1b2b3b4:7'));
      expect(out.result['target_matched']['node_id'], equals(6));
      expect(out.result['applied'], isTrue);
    });

    test('tap with both target and node_id is refused', () async {
      var called = false;
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        a11yTap: (pkg, nodeId, [options]) async {
          called = true;
          return {'applied': true};
        },
      );
      final out = await r.run(DeviceJob(
        jobId: 'job-tapboth',
        tool: 'gui.tap',
        inputs: {
          'target_package': 'dev.zara.zara_android',
          'node_id': 3,
          'target': {'resource_id': 'chat_input'},
        },
        timeoutS: 20,
        executionId: 'exec-tapboth',
      ));
      expect(out.ok, isFalse);
      expect(out.error, contains('not both'));
      expect(called, isFalse);
    });

    test('set_text with target forwards it; text never echoed', () async {
      Map<String, dynamic>? seenOptions;
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        a11ySetText: (pkg, nodeId, text, [options]) async {
          seenOptions = options ?? const {};
          expect(text, 'secret-typed-value');
          return {
            'supported': true,
            'applied': true,
            'package': 'dev.zara.zara_android',
            'verify_state': 'text_matched',
          };
        },
      );
      final out = await r.run(DeviceJob(
        jobId: 'job-sett',
        tool: 'gui.set_text',
        inputs: {
          'target_package': 'dev.zara.zara_android',
          'target': {'resource_id': 'chat_input'},
          'text': 'secret-typed-value',
        },
        timeoutS: 20,
        executionId: 'exec-sett',
      ));
      expect(out.ok, isTrue);
      expect(seenOptions!['target'], isNotNull);
      
      expect('${out.result}', isNot(contains('secret-typed-value')));
    });
  });
}
