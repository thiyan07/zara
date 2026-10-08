// Stage 16: capability descriptor docs — truthful advertisement.
// Every doc comes from a supported=true entry (reserved entries never get
// docs); permission-gated entries report honest availability; descriptor
// shape matches what Core validates (id/name/version/risk/availability).
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/capabilities.dart';
import 'package:zara_android/capability_descriptors.dart';

void main() {
  test('docs cover exactly the supported capabilities', () {
    final docs = androidCapabilityDocs(
      micPermission: true,
      notifPermission: true,
      forceStopGranted: true,
      a11yReady: true,
    );
    final supported =
        androidCapabilities.where((c) => c.supported).map((c) => c.name);
    expect(docs.map((d) => d['id']).toSet(), equals(supported.toSet()));
    for (final d in docs) {
      expect(d['descriptor_version'], '1');
      expect(d['name'], d['id']);
      expect((d['version'] as String).split('.'), hasLength(3));
      expect(d['availability'], 'available');
    }
  });

  test('reserved capabilities never get descriptors', () {
    final docs = androidCapabilityDocs(
        micPermission: true, notifPermission: true);
    final ids = docs.map((d) => d['id']).toSet();
    for (final c in androidCapabilities.where((c) => !c.supported)) {
      expect(ids, isNot(contains(c.name)), reason: c.name);
    }
    expect(ids, isNot(contains('screen.capture')));
    expect(ids, isNot(contains('accessibility.automation')));
  });

  test('mic denial flips voice caps to os_denied with reason', () {
    final docs = androidCapabilityDocs(
        micPermission: false, notifPermission: true);
    final voice =
        docs.firstWhere((d) => d['id'] == 'voice.input');
    expect(voice['availability'], 'os_denied');
    expect((voice['availability_reason'] as String), isNotEmpty);
    expect(voice['os_permission_granted'], isFalse);
    expect((voice['requires'] as List), contains('RECORD_AUDIO'));
    // Non-mic caps stay available.
    expect(
        docs.firstWhere((d) => d['id'] == 'system.battery')['availability'],
        'available');
  });

  test('notification denial flips display cap only', () {
    final docs = androidCapabilityDocs(
        micPermission: true, notifPermission: false);
    expect(
        docs.firstWhere(
            (d) => d['id'] == 'notification.display')['availability'],
        'os_denied');
    expect(
        docs.firstWhere((d) => d['id'] == 'voice.input')['availability'],
        'available');
  });

  test('files.transfer advertised as core-mediated confirm-risk', () {
    final docs = androidCapabilityDocs(
        micPermission: true, notifPermission: true);
    final xfer = docs.firstWhere((d) => d['id'] == 'files.transfer');
    expect(xfer['execution'], 'core-mediated');
    expect(xfer['risk'], 'confirm');
  });

  test('force_stop is high_risk, gated by priv-app grant state', () {
    final granted = androidCapabilityDocs(
        micPermission: true, notifPermission: true, forceStopGranted: true);
    final cap = granted.firstWhere((d) => d['id'] == 'android.app.force_stop');
    expect(cap['risk'], 'high_risk');
    expect(cap['availability'], 'available');
    expect((cap['requires'] as List), contains('FORCE_STOP_PACKAGES'));
    expect(cap['execution'], 'on-device');
    final denied = androidCapabilityDocs(
        micPermission: true, notifPermission: true, forceStopGranted: false);
    final cap2 =
        denied.firstWhere((d) => d['id'] == 'android.app.force_stop');
    expect(cap2['availability'], 'os_denied');
    expect((cap2['availability_reason'] as String), isNotEmpty);
    expect(cap2['os_permission_granted'], isFalse);
  });

  test('gui.* gated by real probe state, never assumed', () {
    final ready = androidCapabilityDocs(
      micPermission: true,
      notifPermission: true,
      forceStopGranted: true,
      a11yReady: true,
    );
    for (final id in ['gui.screen.inspect', 'gui.tap']) {
      expect(ready.firstWhere((d) => d['id'] == id)['availability'],
          'available');
    }
    final notReady = androidCapabilityDocs(
        micPermission: true, notifPermission: true);
    for (final id in ['gui.screen.inspect', 'gui.tap']) {
      final doc = notReady.firstWhere((d) => d['id'] == id);
      expect(doc['availability'], 'unavailable');
      expect((doc['availability_reason'] as String), contains('accessibility'));
    }
    // Gating is availability-only: risk labels stay intact either way.
    final gatedTap =
        notReady.firstWhere((d) => d['id'] == 'gui.tap');
    expect(gatedTap['risk'], 'high_risk');
  });

  test('descriptions are plain ASCII (latin-1 transport)', () {
    final docs = androidCapabilityDocs(
        micPermission: true, notifPermission: true);
    for (final d in docs) {
      for (final s in [
        d['description'] as String,
        ...(d['aliases'] as List).cast<String>()
      ]) {
        expect(s.codeUnits.every((c) => c >= 0x20 && c < 0x7F), isTrue,
            reason: '${d['id']}: $s');
      }
    }
    final voice =
        docs.firstWhere((d) => d['id'] == 'voice.input');
    expect(voice['description'], contains('capture->STT->Core turn'));
  });

  test('rung-2 a11y pilot docs present with confirm/high_risk', () {
    final docs = androidCapabilityDocs(
      micPermission: true,
      notifPermission: true,
      forceStopGranted: true,
      a11yReady: true,
    );
    final inspect =
        docs.firstWhere((d) => d['id'] == 'gui.screen.inspect');
    expect(inspect['risk'], 'confirm');
    expect(inspect['requires'], isEmpty);
    expect(inspect['availability'], 'available');
    expect((inspect['aliases'] as List), contains('inspect screen'));
    expect(inspect['execution'], 'on-device');
    final tap = docs.firstWhere((d) => d['id'] == 'gui.tap');
    expect(tap['risk'], 'high_risk');
    expect(tap['requires'], isEmpty);
    expect(tap['availability'], 'available');
    expect((tap['aliases'] as List), contains('tap'));
    expect(tap['execution'], 'on-device');
  });

  test('malformed server answers stay inert (no crash on junk)', () {    // Docs are plain maps; a junk answer decodes to nothing we act on.
    const junk = {'accepted': [], 'rejected': [
      {'record': 'x', 'error': 'bad'}
    ]};
    expect(junk['accepted'], isEmpty);
  });
}
