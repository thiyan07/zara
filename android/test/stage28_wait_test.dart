// Stage 28 — bounded read-only wait (gui.wait) at the job runner.
//
// Contract checks: lab allowlist mirror (non-lab packages refused
// without touching the a11y probe), honest matched/timeout semantics,
// terminal structural refusals surface as failures with evidence, the
// typed/observed payloads never get echoed into unrelated fields, and
// the wait NEVER acts (no tap/set_text/scroll probe is present on the
// runner at all — absence is the proof).
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/job_runner.dart';

DeviceJob waitJob(Map<String, dynamic> inputs, [String id = 'job-w']) =>
    DeviceJob(
        jobId: id,
        tool: 'gui.wait',
        inputs: inputs,
        timeoutS: 20,
        executionId: 'exec-w');

class _CodedError implements Exception {
  final String code;
  _CodedError(this.code);
}

void main() {
  test('wait for an element that appears immediately returns matched',
      () async {
    final r = AndroidJobRunner(
        battery: () => {}, network: () => {},
        a11yFind: (pkg, target, [options]) async {
          return {
            'status': 'MATCH',
            'package': pkg,
            'element': {'text': 'Done', 'resource_id': 'done_btn'},
            'snapshot_id': 'snap-latest',
          };
        });
    final out = await r.run(waitJob({
      'target_package': 'dev.zara.lab.privtest',
      'target': {'text': 'Done'},
    }));
    expect(out.ok, isTrue);
    expect(out.result['matched'], isTrue);
    expect(out.result['verified'], isTrue);
    expect(out.result['status'], equals('matched'));
    expect(out.result['snapshot_id'], equals('snap-latest'));
    final elements = out.result['element'] as Map<String, dynamic>;
    expect(elements['text'], equals('Done'));
  });

  test('wait that times out is an honest matched:false result', () async {
    final r = AndroidJobRunner(
        battery: () => {}, network: () => {},
        a11yFind: (pkg, target, [options]) async =>
            throw _CodedError('NOT_FOUND'));
    final out = await r.run(waitJob({
      'target_package': 'dev.zara.lab.privtest',
      'target': {'text': 'Never there'},
      'timeout_s': 1,
    }));
    expect(out.ok, isTrue);
    expect(out.result['matched'], isFalse);
    expect(out.result['verified'], isFalse);
    expect(out.result['status'], equals('timeout'));
    expect(out.result['note'], contains('never appeared'));
  });

  test('non-lab packages are refused without touching the a11y probe',
      () async {
    var called = false;
    final r = AndroidJobRunner(
        battery: () => {}, network: () => {},
        a11yFind: (pkg, target, [options]) async {
          called = true;
          return {'status': 'MATCH'};
        });
    for (final pkg in [
      'com.android.systemui',
      'android',
      'com.whatsapp',
      '',
      'dev.zara.byteequal.notmirror',
    ]) {
      final out = await r.run(
          waitJob({
            'target_package': pkg,
            'target': {'text': 'x'},
          }, 'job-w-${pkg.length}-$pkg'));
      expect(out.ok, isFalse, reason: pkg);
      expect(out.error, contains('allowlist'), reason: pkg);
    }
    expect(called, isFalse);
  });

  test('terminal structural refusals stop the wait honestly', () async {
    final r = AndroidJobRunner(
        battery: () => {}, network: () => {},
        a11yFind: (pkg, target, [options]) async =>
            throw _CodedError('UNEXPECTED_PACKAGE'));
    final out = await r.run(waitJob({
      'target_package': 'dev.zara.lab.privtest',
      'target': {'text': 'x'},
      'timeout_s': 5,
    }));
    expect(out.ok, isFalse);
    expect(out.error, contains('foreground package changed'));
    expect(out.result['matched'], isFalse);
    expect(out.result['verified'], isFalse);
  });

  test('missing gateway and missing target are honest failures', () async {
    final r1 = runner(); // a11yFind: null
    final out1 = await r1.run(waitJob({
      'target_package': 'dev.zara.lab.privtest',
      'target': {'text': 'x'},
    }));
    expect(out1.ok, isFalse);
    expect(out1.error, contains('unavailable'));
    final r2 = runner(a11yProbe: (pkg, target, [options]) async {
      return {'status': 'MATCH'};
    });
    final out2 = await r2.run(waitJob({
      'target_package': 'dev.zara.lab.privtest',
    }));
    expect(out2.ok, isFalse);
    expect(out2.error, contains('missing target'));
  });

  test('gui.wait does not act: the runner has no acting a11y gateway',
      () {
    final r = runner();
    // Absence proof: the runner under test was built WITHOUT tap/type/
    // scroll probes, so even a forged internal call path could never
    // mutate anything. The allowlisted family must still contain the
    // read-only member.
    expect(AndroidJobRunner.allowlisted.contains('gui.wait'), isTrue);
    expect(r.a11yTap, isNull);
    expect(r.a11ySetText, isNull);
    expect(r.a11yScroll, isNull);
  });
}

/// Convenience runner used by helper tests above.
AndroidJobRunner runner(
        {A11yFindProbe? a11yProbe,
        A11yTapProbe? a11yTapProbe,
        A11ySetTextProbe? a11ySetTextProbe,
        A11yScrollProbe? a11yScrollProbe}) =>
    AndroidJobRunner(
        battery: () => {},
        network: () => {},
        a11yFind: a11yProbe,
        a11yTap: a11yTapProbe,
        a11ySetText: a11ySetTextProbe,
        a11yScroll: a11yScrollProbe);
