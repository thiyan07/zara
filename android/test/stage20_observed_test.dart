import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/job_runner.dart';

DeviceJob obsJob([String id = 'job-obs']) => DeviceJob(
    jobId: id,
    tool: 'android.observe',
    inputs: const {},
    timeoutS: 20,
    executionId: 'exec-obs');

DeviceJob statsJob([String id = 'job-stats']) => DeviceJob(
    jobId: id,
    tool: 'android.storage.stats',
    inputs: const {},
    timeoutS: 20,
    executionId: 'exec-stats');

void main() {
  test('device.observe returns devices with detection-not-trust note',
      () async {
    final r = AndroidJobRunner(
        battery: () => {'battery_pct': 77},
        network: () => {'network': 'wifi', 'metered': false},
        observedDevices: () async => {
              'devices': [
                {'src': 'storage_volume', 'id': 'storage-1234',
                 'label': 'SanDisk USB', 'state': 'mounted',
                 'removable': true},
                {'src': 'display', 'id': 'display-2',
                 'label': 'Cast Display', 'state': 'present'},
              ],
              'trust_note': 'detection is not trust',
            });
    final out = await r.run(obsJob());
    expect(out.ok, isTrue);
    expect(out.result['supported'], isTrue);
    expect(out.result['count'], equals(2));
    expect(out.result['by_source']['storage_volume'], equals(1));
    expect(out.result['trust_note'], equals('detection is not trust'));
    final row = (out.result['devices'] as List).first as Map;
    expect(row['id'], isNotEmpty);
  });

  test('device.observe honest abstain when platform channel absent',
      () async {
    final r = AndroidJobRunner(
        battery: () => const {},
        network: () => const {},
        observedDevices: () async => {'supported': false});
    final out = await r.run(obsJob());
    expect(out.ok, isTrue);
    expect(out.result['supported'], isFalse);
  });

  test('device.observe gateway absent refuses honestly', () async {
    final r = AndroidJobRunner(
        battery: () => const {}, network: () => const {});
    final out = await r.run(obsJob());
    expect(out.ok, isFalse);
    expect(out.error, contains('unavailable'));
  });

  test('device.storage.stats counts + permission-needed honesty',
      () async {
    final r = AndroidJobRunner(
        battery: () => const {},
        network: () => const {},
        mediaCounts: () async => {
              'counts': {'videos': 4, 'audio': null},
              'storage': {'total_bytes': 128000, 'free_bytes': 10},
              'permissions_needed': [
                {'key': 'videos',
                 'permission': 'READ_MEDIA_VIDEO denied — count withheld'}
              ],
            });
    final out = await r.run(statsJob());
    expect(out.ok, isTrue);
    expect(out.result['counts']['videos'], equals(4));
    expect((out.result['permissions_needed'] as List).length, equals(1));
    expect(out.result['disclaimer'], contains('counts are statistics'));
  });
}
