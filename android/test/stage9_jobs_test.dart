import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/job_runner.dart';

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

  group('Rung-2 a11y lab pilot (device mirror)', () {
    AndroidJobRunner runnerWithA11y() => AndroidJobRunner(
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
          a11yTap: (pkg, nodeId) async => {
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
        a11yTap: (pkg, nodeId) async {
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
        a11yTap: (pkg, nodeId) async {
          called = true;
          return {'applied': true};
        },
      );
      for (final bad in ['0', -1, 1.5]) {
        final out = await r.run(
            tapJob('dev.zara.lab.privtest', bad, 'job-tap-bad-$bad'));
        // '0' is a String, -1 is negative: both refused before the gateway.
        // 1.5 truncates to 1 and executes (num input): allowed through.
        if (bad == 1.5) {
          expect(out.ok, isTrue);
        } else {
          expect(out.ok, isFalse, reason: '$bad');
          expect(out.error, contains('node_id'));
        }
      }
      // Only the 1.5 case reached the gateway.
      expect(called, isTrue);
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
}
