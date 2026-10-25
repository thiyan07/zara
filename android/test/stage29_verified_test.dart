// Stage 29 — honest verified flags on the mutating a11y family
// (gui.tap / gui.set_text / gui.scroll / gui.back) at the job runner.
//
// The OpenDroid-mined discipline: the runner-side default labels are
// NEVER proof. verified=true only when the NATIVE side's own re-read
// confirms the declared effect (tap: 'clicked'; set_text:
// 'text_matched'; back: fresh post-navigation snapshot). Scroll has no
// automatic native confirm and NEVER self-reports verified. The flag is
// carried explicitly in every success result (never omitted), so the
// device core's explicit-verification contract can key on it honestly.
//
// Fixture-only: fake gateways, no device, nothing else wired.
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/job_runner.dart';

DeviceJob tapJob(Map<String, dynamic> inputs, [String id = 'job-tap']) =>
    DeviceJob(
        jobId: id,
        tool: 'gui.tap',
        inputs: inputs,
        timeoutS: 20,
        executionId: 'exec-$id');

DeviceJob setJob(Map<String, dynamic> inputs, [String id = 'job-set']) =>
    DeviceJob(
        jobId: id,
        tool: 'gui.set_text',
        inputs: inputs,
        timeoutS: 20,
        executionId: 'exec-$id');

DeviceJob scrollJob(Map<String, dynamic> inputs, [String id = 'job-scr']) =>
    DeviceJob(
        jobId: id,
        tool: 'gui.scroll',
        inputs: inputs,
        timeoutS: 20,
        executionId: 'exec-$id');

DeviceJob backJob(Map<String, dynamic> inputs, [String id = 'job-back']) =>
    DeviceJob(
        jobId: id,
        tool: 'gui.back',
        inputs: inputs,
        timeoutS: 20,
        executionId: 'exec-$id');

void main() {
  test('tap with native clicked verdict verifies', () async {
    final r = AndroidJobRunner(
        battery: () => {}, network: () => {},
        a11yTap: (pkg, nodeId, [options]) async =>
            {'applied': true, 'verify_state': 'clicked',
             'package': pkg, 'snapshot_id': 'snap-2'});
    final out = await r.run(tapJob({
      'target_package': 'dev.zara.lab.privtest', 'node_id': 3,
    }));
    expect(out.ok, isTrue);
    expect(out.result['applied'], isTrue);
    expect(out.result['verify_state'], equals('clicked'));
    expect(out.result['verified'], isTrue);
  });

  test('tap where native omits verify_state never self-verifies', () async {
    // The runner would label the ack 'clicked' by default — but a label
    // is not proof: verified stays false (explicit, never omitted).
    final r = AndroidJobRunner(
        battery: () => {}, network: () => {},
        a11yTap: (pkg, nodeId, [options]) async => {'applied': true});
    final out = await r.run(tapJob({
      'target_package': 'dev.zara.lab.privtest', 'node_id': 3,
    }));
    expect(out.ok, isTrue);
    expect(out.result['verified'], isFalse);
    expect(out.result.containsKey('verified'), isTrue);
  });

  test('set_text verifies only on the native text_matched re-read',
      () async {
    final matched = AndroidJobRunner(
        battery: () => {}, network: () => {},
        a11ySetText: (pkg, nodeId, text, [options]) async =>
            {'applied': true, 'verify_state': 'text_matched',
             'text_matched': true});
    final a = await matched.run(setJob({
      'target_package': 'dev.zara.lab.privtest', 'node_id': 2,
      'text': 'hello',
    }));
    expect(a.result['verified'], isTrue);

    // Content changed (re-read saw different text) — applied, NOT
    // text_matched: honestly unverified even though ok.
    final appliedOnly = AndroidJobRunner(
        battery: () => {}, network: () => {},
        a11ySetText: (pkg, nodeId, text, [options]) async =>
            {'applied': true, 'verify_state': 'applied (content changed, '
                're-read mismatch)'});
    final b = await appliedOnly.run(setJob({
      'target_package': 'dev.zara.lab.privtest', 'node_id': 2,
      'text': 'hello',
    }));
    expect(b.ok, isTrue);
    expect(b.result['verified'], isFalse);
  });

  test('scroll NEVER self-reports verified (no native confirm)', () async {
    final r = AndroidJobRunner(
        battery: () => {}, network: () => {},
        a11yScroll: (pkg, nodeId, forward, [options]) async =>
            {'applied': true,
             'verify_state': 'requested (compare snapshots for movement)'});
    final out = await r.run(scrollJob({
      'target_package': 'dev.zara.lab.privtest', 'node_id': 1,
    }));
    expect(out.ok, isTrue);
    expect(out.result['applied'], isTrue);
    expect(out.result['verified'], isFalse);
    expect(out.result.containsKey('verified'), isTrue);
  });

  test('back verifies on a fresh post-navigation snapshot', () async {
    final r = AndroidJobRunner(
        battery: () => {}, network: () => {},
        a11yNavigate: (dir, pkg, [options]) async => {
              'applied': true,
              'package_before': 'dev.zara.lab.privtest',
              'package_after': 'dev.zara.zara_android',
              'fresh_snapshot_id': 'snap-after',
              'verify_state': 'applied (framework accepted; confirm the '
                  'new screen via the fresh snapshot)',
            });
    final out = await r.run(backJob({
      'target_package': 'dev.zara.lab.privtest', 'direction': 'back',
    }));
    expect(out.ok, isTrue);
    expect(out.result['verified'], isTrue);
    expect(out.result['package_after'],
        equals('dev.zara.zara_android'));
  });

  test('back with no fresh snapshot stays honestly unverified', () async {
    final r = AndroidJobRunner(
        battery: () => {}, network: () => {},
        a11yNavigate: (dir, pkg, [options]) async =>
            {'applied': true, 'package_before': pkg});
    final out = await r.run(backJob({
      'target_package': 'dev.zara.lab.privtest', 'direction': 'home',
    }));
    expect(out.ok, isTrue);
    expect(out.result['verified'], isFalse);
    expect(out.result.containsKey('verified'), isTrue);
  });
}
