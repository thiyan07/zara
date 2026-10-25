import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/job_runner.dart';

/// Minimal stand-in for a coded bridge-channel failure (mirrors
/// A11yBridgeException's {code, message} shape without importing the
/// Flutter-dependent bridge into runner tests).
class _FakeBridgeError implements Exception {
  final String code;
  final String message;
  final Map<String, dynamic> details;
  _FakeBridgeError(this.code, this.message, [this.details = const {}]);
  @override
  String toString() => '_FakeBridgeError($code): $message';
}

DeviceJob job(String tool, [String id = 'job-1']) => DeviceJob(
    jobId: id, tool: tool, inputs: {}, timeoutS: 20, executionId: 'exec-1');

DeviceJob forceJob(String pkg, [String id = 'job-fs']) => DeviceJob(
    jobId: id,
    tool: 'android.app.force_stop',
    inputs: {'target_package': pkg},
    timeoutS: 20,
    executionId: 'exec-fs');

DeviceJob inspectJob([String id = 'job-insp']) => DeviceJob(
    jobId: id,
    tool: 'gui.screen.inspect',
    inputs: {},
    timeoutS: 20,
    executionId: 'exec-insp');

DeviceJob inspectJobWithParams(Map<String, dynamic> inputs, [String id = 'job-insp-params']) => DeviceJob(
    jobId: id,
    tool: 'gui.screen.inspect',
    inputs: inputs,
    timeoutS: 20,
    executionId: 'exec-insp-params');

DeviceJob tapJob(String pkg, dynamic nodeId, [String id = 'job-tap']) =>
    DeviceJob(
        jobId: id,
        tool: 'gui.tap',
        inputs: {'package': pkg, 'node_id': nodeId},
        timeoutS: 20,
        executionId: 'exec-tap');

AndroidJobRunner runner() => AndroidJobRunner(
    battery: () => {'battery_pct': 77, 'charging': false, 'power_save': false},
    network: () => {'network': 'wifi', 'metered': false});

AndroidJobRunner runnerWithGateway() => AndroidJobRunner(
    battery: () => {'battery_pct': 77, 'charging': false, 'power_save': false},
    network: () => {'network': 'wifi', 'metered': false},
    forceStop: (pkg) async => {
          'package': pkg,
          'stopped': true,
          'was_running': true,
          'verify_state': 'stopped',
        });

void main() {
  test('allowlisted battery/network jobs return device data', () async {
    final r = runner();
    final b = await r.run(job('system.battery', 'job-b'));
    expect(b.ok, isTrue);
    expect(b.result['battery_pct'], equals(77));
    final n = await r.run(job('system.network', 'job-n'));
    expect(n.ok, isTrue);
    expect(n.result['network'], equals('wifi'));
  });

  test('unknown tools refused, never executed (incl. shell/URL shapes)',
      () async {
    final r = runner();
    for (final t in [
      'shell.safe_readonly',
      'filesystem.read',
      'http://evil.example.com/x',
      'rm -rf /',
      '..',
      ''
    ]) {
      final out = await r.run(DeviceJob(
          jobId: 'job-$t', tool: t, inputs: {}, timeoutS: 5, executionId: 'e'));
      expect(out.ok, isFalse, reason: t);
      expect(out.error, contains('refused'));
    }
  });

  test('malformed jobs throw before any execution', () {
    expect(() => DeviceJob.fromJson({'tool': 'system.battery', 'inputs': {}}),
        throwsFormatException);
    expect(() => DeviceJob.fromJson({'job_id': 'j', 'inputs': {}}),
        throwsFormatException);
    expect(() => DeviceJob.fromJson({'job_id': 'j', 'tool': 't'}),
        throwsFormatException);
  });

  test('duplicate job IDs refused; cancellation honored', () async {
    final r = runner();
    expect((await r.run(job('system.battery'))).ok, isTrue);
    final dup = await r.run(job('system.battery'));
    expect(dup.ok, isFalse);
    expect(dup.error, contains('duplicate'));
    final cancel = CancelToken()..cancel();
    final c =
        await r.run(job('system.network', 'job-2'), cancel: cancel);
    expect(c.ok, isFalse);
    expect(c.error, contains('cancelled'));
  });

  test('execution IDs flow through the parsed job', () {
    final j = DeviceJob.fromJson({
      'job_id': 'job-9',
      'tool': 'system.battery',
      'inputs': {},
      'timeout_s': 20,
      'execution_id': 'exec-9',
      'mission_id': 'm-1'
    });
    expect(j.executionId, equals('exec-9'));
    expect(j.missionId, equals('m-1'));
  });

  group('Rung-1 force_stop lab allowlist (device mirror)', () {
    test('allowlisted package executes via gateway', () async {
      final r = runnerWithGateway();
      final out = await r.run(forceJob('dev.zara.lab.privtest', 'job-ok'));
      expect(out.ok, isTrue);
      expect(out.result['package'], equals('dev.zara.lab.privtest'));
      expect(out.result['stopped'], isTrue);
      expect(out.result['verify_state'], equals('stopped'));
    });

    test('non-allowlisted packages refused without touching gateway',
        () async {
      var called = false;
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        forceStop: (pkg) async {
          called = true;
          return {'stopped': true};
        },
      );
      for (final pkg in [
        'com.android.systemui',
        'android',
        'dev.zara.zara_android',
        'com.zara.lab.victim.evil',
        '',
        '../escape',
      ]) {
        final out = await r.run(forceJob(pkg, 'job-$pkg'));
        expect(out.ok, isFalse, reason: pkg);
        expect(out.error, contains('allowlist'));
      }
      expect(called, isFalse);
    });

    test('missing gateway reports unavailable, never executes', () async {
      final r = runner(); // forceStop: null
      final out = await r.run(forceJob('dev.zara.lab.privtest', 'job-nogw'));
      expect(out.ok, isFalse);
      expect(out.error, contains('unavailable'));
    });

    test('gateway denial surfaces as failed result', () async {
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        forceStop: (_) async =>
            throw Exception('PrivBridgeException(denied): refused'),
      );
      final out = await r.run(forceJob('dev.zara.lab.privtest', 'job-deny'));
      expect(out.ok, isFalse);
      expect(out.error, contains('runner fault'));
    });
  });

  group('Rung-2 a11y lab pilot (device mirror)', () {    AndroidJobRunner runnerWithA11y() => AndroidJobRunner(
          battery: () => {},
          network: () => {},
          a11yInspect: () async => {
                'supported': true,
                'node_count': 1,
                'nodes': [
                  {
                    'node_id': 0,
                    'class': 'android.widget.Button',
                    'text': 'OK',
                    'bounds': [0, 0, 100, 50],
                  }
                ],
              },
          a11yInspectWithParams: (params) async {
                final expectedPkg = params['expected_package'] as String?;
                const actualPkg = 'com.example';
                if (expectedPkg != null && expectedPkg != actualPkg) {
                  return {
                    'supported': false,
                    'reason': 'UNEXPECTED_PACKAGE',
                    'expected_package': expectedPkg,
                    'actual_package': actualPkg,
                  };
                }
                return {
                  'supported': true,
                  'node_count': 1,
                  'nodes': [
                    {
                      'node_id': 0,
                      'class': 'android.widget.Button',
                      'text': 'OK',
                      'bounds': [0, 0, 100, 50],
                      'resource_id': 'com.example:id/button',
                      'content_desc': 'OK button',
                      'package': 'com.example',
                      'clickable': true,
                      'long_clickable': false,
                      'scrollable': false,
                      'editable': false,
                      'enabled': true,
                      'selected': false,
                      'checked': false,
                      'focused': false,
                      'visible': true,
                      'sensitive': false,
                    }
                  ],
                  'activity': 'com.example.MainActivity',
                  'screen_w': 1080,
                  'screen_h': 1920,
                  'focused_id': 0,
                  'keyboard_visible': false,
                  'screenshot_available': false,
                  'scrollable_regions': [],
                  'truncated': false,
                  'package': 'com.example',
                };
              },
          a11yTap: (pkg, nodeId, [options]) async => {
                'package': pkg,
                'stopped': 'n/a',
                'applied': true,
                'verify_state': 'clicked',
              },
        );

    test('inspect passes the snapshot shape through', () async {
      final r = runnerWithA11y();
      final out = await r.run(inspectJob('job-insp-ok'));
      expect(out.ok, isTrue);
      expect(out.result['supported'], isTrue);
      expect(out.result['node_count'], equals(1));
      expect((out.result['nodes'] as List).first['node_id'], equals(0));
    });

    test('inspect without gateway reports unavailable', () async {
      final r = runner(); // a11yInspect: null
      final out = await r.run(inspectJob('job-insp-nogw'));
      expect(out.ok, isFalse);
      expect(out.error, contains('unavailable'));
    });

    test('tap allowlisted package executes via gateway', () async {
      final r = runnerWithA11y();
      for (final pkg in [
        'dev.zara.zara_android',
        'dev.zara.lab.privtest',
      ]) {
        final out = await r.run(tapJob(pkg, 0, 'job-tap-$pkg'));
        expect(out.ok, isTrue, reason: pkg);
        expect(out.result['package'], equals(pkg));
        expect(out.result['stopped'], equals('n/a'));
        expect(out.result['applied'], isTrue);
        expect(out.result['verify_state'], equals('clicked'));
      }
    });

    test('non-allowlisted packages refused without touching gateway',
        () async {
      var called = false;
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        a11yTap: (pkg, nodeId, [options]) async {
          called = true;
          return {'applied': true};
        },
      );
      for (final pkg in [
        'com.android.systemui',
        'android',
        'com.zara.lab.victim.evil',
        '',
        '../escape',
      ]) {
        final out = await r.run(tapJob(pkg, 0, 'job-tap-$pkg'));
        expect(out.ok, isFalse, reason: pkg);
        expect(out.error, contains('allowlist'));
      }
      expect(called, isFalse);
    });

    test('tap without gateway reports unavailable, never executes', () async {
      final r = runnerWithGateway(); // a11yTap: null (forceStop only)
      final out = await r.run(
          tapJob('dev.zara.lab.privtest', 0, 'job-tap-nogw'));
      expect(out.ok, isFalse);
      expect(out.error, contains('unavailable'));
    });

    test('tap with bad node_id refused without touching gateway', () async {
      var called = false;
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        a11yTap: (pkg, nodeId, [options]) async {
          called = true;
          return {'applied': true};
        },
      );
      for (final bad in ['0', -1, 1.5, 1.0]) {
        final out = await r.run(
            tapJob('dev.zara.lab.privtest', bad, 'job-tap-bad-$bad'));
        // Contract v1: node_id is an exact non-negative integer DFS index.
        // Strings, negatives, and doubles (even integral ones like 1.0 —
        // truncation could click the wrong node) are refused before the
        // gateway.
        expect(out.ok, isFalse, reason: '$bad');
        expect(out.error, contains('node_id'));
      }
      // Nothing reached the gateway.
      expect(called, isFalse);
    });

    test('unknown gui.* tools refused, never executed', () async {
      final r = runnerWithA11y();
      for (final t in ['gui.swipe', 'gui.foo', 'gui.screen.record']) {
        final out = await r.run(DeviceJob(
            jobId: 'job-$t', tool: t, inputs: {}, timeoutS: 5, executionId: 'e'));
        expect(out.ok, isFalse, reason: t);
        expect(out.error, contains('refused'));
      }
    });

    group('contract v2 set_text/scroll', () {
      AndroidJobRunner textRunner(
              Future<Map<String, dynamic>> Function(
                      String, int, String, Map<String, dynamic>?)
                  fn) =>
          AndroidJobRunner(
            battery: () => {},
            network: () => {},
            a11ySetText: (pkg, nodeId, text, [options]) =>
                fn(pkg, nodeId, text, options ?? const {}),
          );

      AndroidJobRunner scrollRunner(
              Future<Map<String, dynamic>> Function(
                      String, int, bool, Map<String, dynamic>?)
                  fn) =>
          AndroidJobRunner(
            battery: () => {},
            network: () => {},
            a11yScroll: (pkg, nodeId, fwd, [options]) =>
                fn(pkg, nodeId, fwd, options ?? const {}),
          );

      var textSeq = 0;
      DeviceJob textJob(Map<String, dynamic> extra) => DeviceJob(
          jobId: 'job-text-${textSeq++}',
          tool: 'gui.set_text',
          inputs: {
            'target_package': 'dev.zara.zara_android',
            'node_id': 4,
            'text': 'hello lab',
            ...extra,
          },
          timeoutS: 20,
          executionId: 'exec-text');

      var scrollSeq = 0;
      DeviceJob scrollJob(Map<String, dynamic> extra) => DeviceJob(
          jobId: 'job-scroll-${scrollSeq++}',
          tool: 'gui.scroll',
          inputs: {
            'target_package': 'dev.zara.zara_android',
            'node_id': 9,
            ...extra,
          },
          timeoutS: 20,
          executionId: 'exec-scroll');

      test('set_text forwards text plus snapshot bindings', () async {
        String? seenText;
        Map? seenOpts;
        final r = textRunner((pkg, nodeId, text, opts) async {
          seenText = text;
          seenOpts = opts;
          return {
            'applied': true,
            'verify_state': 'text_matched',
            'snapshot_id': 'eeee5678:6',
            'contract_version': 2,
          };
        });
        final out = await r.run(textJob({
          'expected_snapshot_id': 'abcd1234:5',
        }));
        expect(out.ok, isTrue);
        expect(seenText, equals('hello lab'));
        expect(seenOpts!['expected_snapshot_id'], equals('abcd1234:5'));
        expect(out.result['verify_state'], equals('text_matched'));
        // Typed text never echoes back through the result.
        expect('${out.result}', isNot(contains('hello lab')));
      });

      test('empty/oversize text refused without touching gateway',
          () async {
        var called = false;
        final r = textRunner((pkg, nodeId, text, opts) async {
          called = true;
          return {'applied': true};
        });
        for (final bad in ['', 'x' * 501]) {
          final out = await r.run(textJob({'text': bad}));
          expect(out.ok, isFalse, reason: 'len=${bad.length}');
          expect(out.error, contains('bad text'));
        }
        expect(called, isFalse);
      });

      test('non-editable target fails with evidence', () async {
        final r = textRunner((pkg, nodeId, text, opts) async => {
              'supported': false,
              'applied': false,
              'error': 'NOT_EDITABLE',
              'snapshot_id': 'eeee5678:6',
            });
        final out = await r.run(textJob({}));
        expect(out.ok, isFalse);
        expect(out.error, contains('not editable'));
        expect(out.result['snapshot_id'], equals('eeee5678:6'));
      });

      test('scroll forwards direction and bindings', () async {
        bool? seenFwd;
        final r = scrollRunner((pkg, nodeId, fwd, opts) async {
          seenFwd = fwd;
          return {
            'applied': true,
            'verify_state': 'requested (compare snapshots for movement)',
            'snapshot_id': 'ffff1111:9',
            'contract_version': 2,
          };
        });
        var out = await r.run(scrollJob({}));
        expect(out.ok, isTrue);
        expect(seenFwd, isTrue);
        out = await r.run(scrollJob({'direction': 'backward'}));
        expect(out.ok, isTrue);
        expect(seenFwd, isFalse);
      });

      test('scroll on non-scrollable node fails', () async {
        final r = scrollRunner((pkg, nodeId, fwd, opts) async => {
              'supported': false,
              'applied': false,
              'error': 'NOT_SCROLLABLE',
              'snapshot_id': 'ffff1111:9',
            });
        final out = await r.run(scrollJob({}));
        expect(out.ok, isFalse);
        expect(out.error, contains('not scrollable'));
      });
    });

    test('inspect with params returns Rung-3 snapshot shape', () async {
      final r = runnerWithA11y();
      final out = await r.run(inspectJobWithParams({
        'expected_package': 'com.example',
        'max_elements': 25,
        'include_text': true,
        'include_content_description': true,
      }));
      expect(out.ok, isTrue);
      expect(out.result['supported'], isTrue);
      expect(out.result['activity'], equals('com.example.MainActivity'));
      expect(out.result['screen_w'], equals(1080));
      expect(out.result['screen_h'], equals(1920));
      expect(out.result['focused_id'], equals(0));
      expect(out.result['keyboard_visible'], isFalse);
      expect(out.result['screenshot_available'], isFalse);
      expect(out.result['scrollable_regions'], isA<List>());
      expect(out.result['truncated'], isFalse);
      final nodes = out.result['nodes'] as List;
      expect(nodes.length, equals(1));
      final n = nodes.first as Map<String, dynamic>;
      expect(n['resource_id'], equals('com.example:id/button'));
      expect(n['content_desc'], equals('OK button'));
      expect(n['long_clickable'], isFalse);
      expect(n['scrollable'], isFalse);
      expect(n['editable'], isFalse);
      expect(n['enabled'], isTrue);
      expect(n['selected'], isFalse);
      expect(n['checked'], isFalse);
      expect(n['visible'], isTrue);
      expect(n['sensitive'], isFalse);
    });

    test('inspect with expected_package mismatch returns UNEXPECTED_PACKAGE', () async {
      final r = runnerWithA11y();
      final out = await r.run(inspectJobWithParams({
        'expected_package': 'wrong.package',
      }));
      expect(out.ok, isFalse);
      expect(out.result['supported'], isFalse);
      expect(out.result['reason'], equals('UNEXPECTED_PACKAGE'));
      expect(out.result['expected_package'], equals('wrong.package'));
      expect(out.result['actual_package'], equals('com.example'));
    });

    group('contract v1 snapshot-bound tap', () {
      AndroidJobRunner tapRunner(
          Future<Map<String, dynamic>> Function(
                  String, int, Map<String, dynamic>?)
              tap) =>
          AndroidJobRunner(
            battery: () => {},
            network: () => {},
            a11yTap: (pkg, nodeId, [options]) =>
                tap(pkg, nodeId, options ?? const {}),
          );

      DeviceJob tapBoundJob(Map<String, dynamic> extra) => DeviceJob(
          jobId: 'job-tap-bound',
          tool: 'gui.tap',
          inputs: {
            'target_package': 'dev.zara.zara_android',
            'node_id': 3,
            ...extra,
          },
          timeoutS: 20,
          executionId: 'exec-tap-bound');

      test('snapshot bindings forwarded to the gateway', () async {
        Map<String, dynamic>? seen;
        final r = tapRunner((pkg, nodeId, opts) async {
          seen = {'pkg': pkg, 'nodeId': nodeId, 'opts': opts};
          return {'applied': true, 'package': pkg, 'verify_state': 'clicked'};
        });
        final out = await r.run(tapBoundJob({
          'expected_snapshot_id': 'abcd1234:5',
          'expected_package': 'dev.zara.zara_android',
          'max_elements': 25,
        }));
        expect(out.ok, isTrue);
        expect(seen!['pkg'], equals('dev.zara.zara_android'));
        expect(seen!['nodeId'], equals(3));
        final opts = seen!['opts'] as Map;
        expect(opts['expected_snapshot_id'], equals('abcd1234:5'));
        expect(opts['expected_package'],
            equals('dev.zara.zara_android'));
        expect(opts['max_elements'], equals(25));
      });

      test('stale snapshot refusal is a failure with evidence', () async {
        final r = tapRunner((pkg, nodeId, opts) async => {
              'supported': false,
              'applied': false,
              'error': 'STALE_SNAPSHOT',
              'reason': 'screen structure changed since snapshot',
              'snapshot_id': 'eeee5678:6',
              'contract_version': 1,
            });
        final out = await r.run(tapBoundJob({
          'expected_snapshot_id': 'abcd1234:5',
        }));
        expect(out.ok, isFalse);
        expect(out.error, contains('stale snapshot'));
        expect(out.error, contains('never executed'));
        expect(out.result['error'], equals('STALE_SNAPSHOT'));
        expect(out.result['snapshot_id'], equals('eeee5678:6'));
      });

      test('package-mismatch refusal is a failure, never success', () async {
        final r = tapRunner((pkg, nodeId, opts) async => {
              'supported': false,
              'applied': false,
              'error': 'UNEXPECTED_PACKAGE',
              'expected_package': 'dev.zara.zara_android',
              'actual_package': 'com.other',
            });
        final out = await r.run(tapBoundJob({
          'expected_package': 'dev.zara.zara_android',
        }));
        expect(out.ok, isFalse);
        expect(out.error, contains('foreground package changed'));
      });

      test('bridge channel error maps to structured refusal', () async {
        final r = AndroidJobRunner(
          battery: () => {},
          network: () => {},
          a11yTap: (pkg, nodeId, [options]) async =>
              throw _FakeBridgeError('stale', 'STALE_SNAPSHOT: moved'),
        );
        final out = await r.run(tapBoundJob({
          'expected_snapshot_id': 'abcd1234:5',
        }));
        expect(out.ok, isFalse);
        expect(out.error, contains('stale snapshot'));
        expect(out.result['bridge_code'], equals('stale'));
      });

      test('bridge error details merge into refusal evidence', () async {
        final r = AndroidJobRunner(
          battery: () => {},
          network: () => {},
          a11yTap: (pkg, nodeId, [options]) async =>
              throw _FakeBridgeError('stale', 'STALE_SNAPSHOT: moved', {
                'error': 'STALE_SNAPSHOT',
                'expected_snapshot_id': 'abcd1234:5',
                'snapshot_id': 'eeee5678:6',
                'package': 'dev.zara.zara_android',
              }),
        );
        final out = await r.run(tapBoundJob({
          'expected_snapshot_id': 'abcd1234:5',
        }));
        expect(out.ok, isFalse);
        // Fresh snapshot_id survives the channel-error path so the
        // caller can re-inspect precisely.
        expect(out.result['snapshot_id'], equals('eeee5678:6'));
        expect(out.result['error'], equals('STALE_SNAPSHOT'));
      });

      test('non-applied tap without error code still fails', () async {
        final r = tapRunner(
            (pkg, nodeId, opts) async => {'applied': false});
        final out = await r.run(tapBoundJob({}));
        expect(out.ok, isFalse);
        expect(out.error, contains('not applied'));
      });

      test('applied tap carries contract evidence through', () async {
        final r = tapRunner((pkg, nodeId, opts) async => {
              'applied': true,
              'package': pkg,
              'verify_state': 'clicked',
              'snapshot_id': 'abcd1234:5',
              'contract_version': 1,
            });
        final out = await r.run(tapBoundJob({}));
        expect(out.ok, isTrue);
        expect(out.result['applied'], isTrue);
        expect(out.result['snapshot_id'], equals('abcd1234:5'));
        expect(out.result['contract_version'], equals(1));
      });
    });

    test('inspect with max_elements caps nodes', () async {
      var capturedParams = <String, dynamic>{};
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        a11yInspectWithParams: (params) async {
          capturedParams = Map.from(params);
          return {
            'supported': true,
            'node_count': 10,
            'nodes': List.generate(10, (i) => {
                  'node_id': i,
                  'class': 'View',
                  'bounds': [0, 0, 10, 10],
                }),
            'truncated': true,
            'activity': 'TestActivity',
            'screen_w': 100,
            'screen_h': 100,
            'focused_id': -1,
            'keyboard_visible': false,
            'screenshot_available': false,
            'scrollable_regions': [],
          };
        },
      );
      final out = await r.run(inspectJobWithParams({'max_elements': 10}));
      expect(out.ok, isTrue);
      expect(capturedParams['max_elements'], equals(10));
      expect(out.result['truncated'], isTrue);
      expect(out.result['node_count'], equals(10));
    });

    test('inspect with include_text=false omits text field', () async {
      var capturedParams = <String, dynamic>{};
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        a11yInspectWithParams: (params) async {
          capturedParams = Map.from(params);
          return {
            'supported': true,
            'node_count': 1,
            'nodes': [{
              'node_id': 0,
              'class': 'View',
              'bounds': [0, 0, 10, 10],
            }],
            'truncated': false,
            'activity': 'TestActivity',
            'screen_w': 100,
            'screen_h': 100,
            'focused_id': -1,
            'keyboard_visible': false,
            'screenshot_available': false,
            'scrollable_regions': [],
          };
        },
      );
      final out = await r.run(inspectJobWithParams({'include_text': false}));
      expect(out.ok, isTrue);
      expect(capturedParams['include_text'], isFalse);
    });

    test('inspect with include_content_description=false omits content_desc field', () async {
      var capturedParams = <String, dynamic>{};
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        a11yInspectWithParams: (params) async {
          capturedParams = Map.from(params);
          return {
            'supported': true,
            'node_count': 1,
            'nodes': [{
              'node_id': 0,
              'class': 'View',
              'bounds': [0, 0, 10, 10],
            }],
            'truncated': false,
            'activity': 'TestActivity',
            'screen_w': 100,
            'screen_h': 100,
            'focused_id': -1,
            'keyboard_visible': false,
            'screenshot_available': false,
            'scrollable_regions': [],
          };
        },
      );
      final out = await r.run(inspectJobWithParams({'include_content_description': false}));
      expect(out.ok, isTrue);
      expect(capturedParams['include_content_description'], isFalse);
    });

    test('inspect falls back to legacy probe when params probe unavailable', () async {
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        a11yInspect: () async => {
              'supported': true,
              'node_count': 1,
              'nodes': [{
                'node_id': 0,
                'class': 'LegacyView',
                'bounds': [0, 0, 10, 10],
              }],
            },
        // a11yInspectWithParams: null
      );
      final out = await r.run(inspectJobWithParams({'max_elements': 10}));
      expect(out.ok, isTrue);
      expect(out.result['supported'], isTrue);
      expect(out.result['nodes'].first['class'], equals('LegacyView'));
    });
  });

  group('Phase 1 sys-control (read-only states + settings/app open)', () {
    AndroidJobRunner phase1Runner() => AndroidJobRunner(
          battery: () =>
              {'battery_pct': 55, 'charging': true, 'power_save': false},
          network: () => {'network': 'wifi', 'metered': false},
          wifiState: () async => {'supported': true, 'state': 'enabled'},
          bluetoothState: () async => {'supported': true, 'state': 'disabled'},
          hotspotState: () async => {'supported': false, 'reason': 'no public API'},
          mobileDataState: () async => {'supported': true, 'state': 'enabled'},
          airplaneModeState: () async => {'supported': true, 'state': 'disabled'},
          openSettings: (screen) async =>
              {'supported': true, 'screen': screen},
          openApp: (pkg) async => {'package': pkg, 'launched': true},
        );

    test('state tools pass bridge snapshots through', () async {
      final r = phase1Runner();
      for (final t in [
        'system.wifi.state',
        'system.bluetooth.state',
        'system.mobile_data.state',
        'system.airplane_mode.state',
      ]) {
        final out = await r.run(job(t, 'job-$t'));
        expect(out.ok, isTrue, reason: t);
        expect(out.result['supported'], isTrue, reason: t);
      }
    });

    test('unsupported hotspot surfaces as failure with reason', () async {
      final r = phase1Runner();
      final out =
          await r.run(job('system.hotspot.state', 'job-hotspot'));
      expect(out.ok, isFalse);
      expect(out.error, contains('no public API'));
    });

    test('charging_time never invents a duration', () async {
      final r = phase1Runner();
      final out =
          await r.run(job('system.battery.charging_time', 'job-ct'));
      expect(out.ok, isTrue);
      expect(out.result['time_to_full_minutes'], isNull);
    });

    test('settings tools open exactly the mapped screen', () async {
      final r = phase1Runner();
      for (final t in [
        'system.wifi.settings',
        'system.bluetooth.settings',
        'system.hotspot.settings',
        'system.mobile_data.settings',
        'system.airplane_mode.settings',
      ]) {
        final out = await r.run(job(t, 'job-$t'));
        expect(out.ok, isTrue, reason: t);
      }
    });

    test('app.open allowlisted package launches; others refused', () async {
      var called = false;
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        openApp: (pkg) async {
          called = true;
          return {'package': pkg, 'launched': true};
        },
      );
      final ok = await r.run(DeviceJob(
          jobId: 'job-app-ok',
          tool: 'system.app.open',
          inputs: {'package': 'com.android.settings'},
          timeoutS: 5,
          executionId: 'e'));
      expect(ok.ok, isTrue);
      expect(called, isTrue);
      called = false;
      for (final pkg in [
        'com.android.systemui',
        'android',
        'com.evil.pwn',
        '',
      ]) {
        final out = await r.run(DeviceJob(
            jobId: 'job-app-$pkg',
            tool: 'system.app.open',
            inputs: {'package': pkg},
            timeoutS: 5,
            executionId: 'e'));
        expect(out.ok, isFalse, reason: pkg);
        expect(out.error, contains('allowlist'));
      }
      expect(called, isFalse);
    });

    test('missing gateways report unavailable, never execute', () async {
      final r = runner(); // no phase-1 probes wired
      for (final t in [
        'system.wifi.state',
        'system.app.open',
      ]) {
        final out = await r.run(DeviceJob(
            jobId: 'job-$t',
            tool: t,
            inputs: t == 'system.app.open'
                ? {'package': 'com.android.settings'}
                : {},
            timeoutS: 5,
            executionId: 'e'));
        expect(out.ok, isFalse, reason: t);
        expect(out.error, contains('unavailable'));
      }
    });
  });

  group('Phase 2 sys-control setters (tiered, verified-or-honest)', () {
    DeviceJob setJob(String tool, dynamic enabled, [String id = 'job-set']) =>
        DeviceJob(
            jobId: id,
            tool: tool,
            inputs: {'enabled': enabled},
            timeoutS: 20,
            executionId: 'exec-set');

    AndroidJobRunner setRunner() => AndroidJobRunner(
          battery: () => {},
          network: () => {},
          setWifi: (v) async => {
                'supported': true,
                'applied': false,
                'requested': v,
                'method': 'settings-ui',
                'settings_opened': true,
                'verify': 'system.wifi.state',
              },
          setBluetooth: (v) async => {
                'supported': true,
                'applied': false,
                'requested': v,
                'method': 'consent-dialog',
                'consent_requested': true,
                'verify': 'system.bluetooth.state',
              },
          setHotspot: (v) async => {
                'supported': true,
                'applied': false,
                'requested': v,
                'method': 'settings-ui',
                'deployment': 't3-priv-companion-or-settings-ui',
                'verify': 'system.hotspot.state',
              },
          setMobileData: (v) async => {
                'supported': true,
                'applied': false,
                'requested': v,
                'method': 'settings-ui',
                'verify': 'system.mobile_data.state',
              },
          setAirplaneMode: (v) async => {
                'supported': true,
                'applied': true,
                'requested': v,
                'method': 'direct-adb-grant',
                'verify': 'system.airplane_mode.state',
              },
        );

    test('verified applied:true passes through with method + verify', () async {
      final r = setRunner();
      final out =
          await r.run(setJob('system.airplane_mode.set', true, 'job-ap-on'));
      expect(out.ok, isTrue);
      expect(out.result['applied'], isTrue);
      expect(out.result['method'], equals('direct-adb-grant'));
      expect(out.result['verify'], equals('system.airplane_mode.state'));
    });

    test('applied:false tier outcomes are terminal honesty, not retryable',
        () async {
      final r = setRunner();
      for (final t in [
        'system.wifi.set',
        'system.bluetooth.set',
        'system.hotspot.set',
        'system.mobile_data.set',
      ]) {
        final out = await r.run(setJob(t, true, 'job-$t'));
        expect(out.ok, isTrue, reason: t);
        expect(out.result['applied'], isFalse, reason: t);
        expect(out.result['verify'], isNotNull, reason: t);
      }
    });

    test('non-boolean enabled refused without touching gateway', () async {
      var called = false;
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        setWifi: (v) async {
          called = true;
          return {'applied': true};
        },
      );
      for (final bad in ['on', 1, null, {'enabled': true}]) {
        final out = await r.run(DeviceJob(
            jobId: 'job-bad-$bad',
            tool: 'system.wifi.set',
            inputs: {'enabled': bad},
            timeoutS: 5,
            executionId: 'e'));
        expect(out.ok, isFalse, reason: '$bad');
        expect(out.error, contains('enabled'));
      }
      expect(called, isFalse);
    });

    test('missing setter gateway reports unavailable', () async {
      final r = runner(); // no setters wired
      final out =
          await r.run(setJob('system.wifi.set', true, 'job-nosetter'));
      expect(out.ok, isFalse);
      expect(out.error, contains('unavailable'));
    });

    test('native unsupported surfaces as failure with reason', () async {
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        setHotspot: (v) async => {
          'supported': false,
          'reason': 'no public API',
        },
      );
      final out =
          await r.run(setJob('system.hotspot.set', true, 'job-hs-unsup'));
      expect(out.ok, isFalse);
      expect(out.error, contains('no public API'));
    });

    test('gateway exceptions surface as runner faults', () async {
      final r = AndroidJobRunner(
        battery: () => {},
        network: () => {},
        setWifi: (v) async =>
            throw Exception('SysControlBridgeException(failed): nope'),
      );
      final out = await r.run(setJob('system.wifi.set', true, 'job-fault'));
      expect(out.ok, isFalse);
      expect(out.error, contains('runner fault'));
    });
  });
}
