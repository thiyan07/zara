import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/job_runner.dart';

DeviceJob job(String tool, [String id = 'job-1']) => DeviceJob(
    jobId: id, tool: tool, inputs: {}, timeoutS: 20, executionId: 'exec-1');

AndroidJobRunner runner() => AndroidJobRunner(
    battery: () => {'battery_pct': 77, 'charging': false, 'power_save': false},
    network: () => {'network': 'wifi', 'metered': false});

void main() {
  test('allowlisted battery/network jobs return device data', () {
    final r = runner();
    final b = r.run(job('system.battery', 'job-b'));
    expect(b.ok, isTrue);
    expect(b.result['battery_pct'], equals(77));
    final n = r.run(job('system.network', 'job-n'));
    expect(n.ok, isTrue);
    expect(n.result['network'], equals('wifi'));
  });

  test('unknown tools refused, never executed (incl. shell/URL shapes)', () {
    final r = runner();
    for (final t in [
      'shell.safe_readonly',
      'filesystem.read',
      'http://evil.example.com/x',
      'rm -rf /',
      '..',
      ''
    ]) {
      final out = r.run(DeviceJob(
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

  test('duplicate job IDs refused; cancellation honored', () {
    final r = runner();
    expect(r.run(job('system.battery')).ok, isTrue);
    final dup = r.run(job('system.battery'));
    expect(dup.ok, isFalse);
    expect(dup.error, contains('duplicate'));
    final cancel = CancelToken()..cancel();
    final c = r.run(job('system.network', 'job-2'), cancel: cancel);
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
}
