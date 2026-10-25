// Stage 31-P3 phase A — quick/one-shot phone actions at the job runner
// (torch / volume / ring / media keys / clipboard push / open-uri /
// power saver / alarm).
//
// Honest-answer discipline at the RUNNER seam (fixture-only; fake
// gateways, no device):
// 1. An unavailable/refused answer from the gateway is HONEST DATA.
//    The runner maps: abstain-backed ("available:false", "no player")
//    → ok:true with the refusal recorded; action failure WITHOUT an
//    abstain reason → ok:false with the honest verdict text.
// 2. Every success carries the gateway's verification text (or the
//    honest sent/handed-to-clock verdict) — runner-side labels never
//    add claims the native re-read could not back.
// 3. clipboard NEVER echoes the text through the result (length only).
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/job_runner.dart';

DeviceJob job(String tool, Map<String, dynamic> inputs,
        [String id = 'job-x']) =>
    DeviceJob(
        jobId: id,
        tool: tool,
        inputs: inputs,
        timeoutS: 20,
        executionId: 'exec-$id');

AndroidJobRunner runner({
  SysSetter? torchSet,
  PctActionProbe? streamVolumeSet,
  RingProbe? findMyPhoneRing,
  TextActionProbe? mediaKeyEvent,
  TextActionProbe? clipboardSet,
  TextActionProbe? openUri,
  SysSetter? powerSaverSet,
  AlarmProbe? alarmSet,
  TimerProbe? timerSet,
  DndGetProbe? dndGet,
  DndSetProbe? dndSet,
}) =>
    AndroidJobRunner(
      battery: () => {},
      network: () => {},
      torchSet: torchSet,
      streamVolumeSet: streamVolumeSet,
      findMyPhoneRing: findMyPhoneRing,
      mediaKeyEvent: mediaKeyEvent,
      clipboardSet: clipboardSet,
      openUri: openUri,
      powerSaverSet: powerSaverSet,
      alarmSet: alarmSet,
      timerSet: timerSet,
      dndGet: dndGet,
      dndSet: dndSet,
    );

void main() {
  // ---------------- torch ----------------
  test('torch applied carries the native re-read verification', () async {
    final r = runner(
        torchSet: (on) async => {
              'available': true,
              'requested': on,
              'after_torch': true,
              'verdict': 'applied',
              'verification': 'torch-callback re-read',
            });
    final out = await r.run(job('android.torch.set', {'on': true}));
    expect(out.ok, isTrue);
    expect(out.result['torch'], isTrue);
    expect(out.result['verdict'], equals('applied'));
    expect(out.result['verification'], equals('torch-callback re-read'));
  });

  test('torch honest abstain (no flash unit) is ok WITH the reason',
      () async {
    final r = runner(
        torchSet: (on) async => {
              'available': false,
              'detail': 'no camera with a flash unit on this device',
            });
    final out = await r.run(job('android.torch.set', {'on': true}));
    expect(out.ok, isTrue);
    expect(out.result['available'], isFalse);
    expect(out.result['detail'], contains('no camera with a flash'));
  });

  test('torch not-applied is an honest failure', () async {
    final r = runner(
        torchSet: (on) async => {
              'available': true,
              'verdict': 'not-applied',
            });
    final out = await r.run(job('android.torch.set', {'on': true}));
    expect(out.ok, isFalse);
    expect(out.error, contains('not-applied'));
  });

  test('torch unverified rides ok:true with the explicit verdict', () async {
    // The native call DISPATCHED but the torch re-read could not
    // confirm (e.g. the emulator's virtual camera): the execution ran,
    // verification is honestly absent — ok:true, verdict 'unverified',
    // never a manufactured applied claim.
    final r = runner(
        torchSet: (on) async => {
              'available': true,
              'verdict': 'unverified',
              'verification': 'torch-callback re-read',
            });
    final out = await r.run(job('android.torch.set', {'on': true}));
    expect(out.ok, isTrue, reason: 'the execution ran; honest abstain');
    expect(out.result['verdict'], equals('unverified'));
    expect(out.result['verification'], contains('re-read'));
  });

  // ---------------- volume ----------------
  test('phone volume applied with re-read pct values', () async {
    final r = runner(
        streamVolumeSet: (pct) async => {
              'ok': true,
              'verdict': 'applied',
              'before_pct': 63,
              'after_pct': pct,
            });
    final out = await r.run(job('android.volume.set', {'pct': 80}));
    expect(out.ok, isTrue);
    expect(out.result['after_pct'], 80);
    expect(out.result['before_pct'], 63);
  });

  test('phone volume non-applied verdict fails honestly', () async {
    final r = runner(
        streamVolumeSet: (pct) async => {
              'ok': false,
              'verdict': 'not-applied',
            });
    final out = await r.run(job('android.volume.set', {'pct': 80}));
    expect(out.ok, isFalse);
    expect(out.error, contains('not-applied'));
  });

  // ---------------- ring ----------------
  test('ring success carries bounded seconds + verification', () async {
    final r = runner(
        findMyPhoneRing: (seconds, bypass) async {
          expect(seconds, 10);
          expect(bypass, isFalse);
          return {
            'rang': true,
            'seconds': seconds,
            'verification': 'alarm-sound bounded ring',
          };
        });
    final out =
        await r.run(job('android.ring', {'seconds': 10}));
    expect(out.ok, isTrue);
    expect(out.result['rang'], isTrue);
    expect(out.result['verification'], equals('alarm-sound bounded ring'));
  });

  test('ring refuses honestly in DND without explicit bypass', () async {
    final r = runner(
        findMyPhoneRing: (seconds, bypass) async => {
              'rang': false,
              'detail': 'phone is in do-not-disturb; Zara does not '
                  'override silence without an explicit user allowance',
            });
    final out = await r.run(job('android.ring', {'seconds': 10}));
    expect(out.ok, isFalse);
    expect(out.error, contains('do-not-disturb'));
  });

  // ---------------- media keys ----------------
  test('media key sent verdict is carried (never upgraded to applied)',
      () async {
    final r = runner(
        mediaKeyEvent: (action) async =>
            {'ok': true, 'verdict': 'sent', 'verification': 'key events'},
        );
    final out = await r.run(job('android.media.playback', {'action': 'pause'}));
    expect(out.ok, isTrue);
    expect(out.result['verdict'], equals('sent'));
    expect(out.result['action'], equals('pause'));
  });

  test('media key bad action fails with the honest refusal', () async {
    final r = runner(
        mediaKeyEvent: (action) async =>
            {'ok': false, 'error': 'action must be play|pause|...'});
    final out = await r.run(job('android.media.playback', {'action': 'rewind'}));
    expect(out.ok, isFalse);
    expect(out.error, contains('action'));
  });

  // ---------------- clipboard ----------------
  test('clipboard applied is length-only (text never echoed)', () async {
    final text = 'secret-draft-9f2b';
    final r = runner(
        clipboardSet: (t) async {
          expect(t, equals(text));
          return {
            'applied': true,
            'length': t.length,
            'verification': 'primary-clip re-read',
          };
        });
    final out = await r.run(job('android.clipboard.set', {'text': text}));
    expect(out.ok, isTrue);
    expect(out.result['applied'], isTrue);
    expect(out.result['length'], text.length);
    expect(out.result.toString(), isNot(contains(text)));
  });

  test('clipboard focus-refusal fails with the honest reason', () async {
    final r = runner(
        clipboardSet: (t) async => {
              'applied': false,
              'verdict': 'focus-refused',
              'detail': 'the app window is not focused (Android 10+ rule)',
            });
    final out =
        await r.run(job('android.clipboard.set', {'text': 'hi'}));
    expect(out.ok, isFalse);
    expect(out.error, contains('focus'));
  });

  // ---------------- open uri ----------------
  test('open-uri success carries scheme + honest sent verdict', () async {
    final r = runner(
        openUri: (uri) async => {
              'fired': true,
              'scheme': 'https',
              'verdict': 'sent',
              'verification': 'VIEW intent fired after resolvable check',
            });
    final out = await r.run(
        job('android.open_uri', {'uri': 'https://example.org/zara'}));
    expect(out.ok, isTrue);
    expect(out.result['scheme'], equals('https'));
    expect(out.result['verdict'], equals('sent'));
  });

  test('open-uri no-handler abstain fails with the honest detail',
      () async {
    final r = runner(
        openUri: (uri) async => {
              'fired': false,
              'detail': 'no app on this device handles this link',
            });
    final out = await r.run(
        job('android.open_uri', {'uri': 'https://example.org/none'}));
    expect(out.ok, isFalse);
    expect(out.error, contains('no app on this device'));
  });

  // ---------------- power saver ----------------
  test('power saver applied carries native dual re-read verification',
      () async {
    final r = runner(
        powerSaverSet: (on) async => {
              'ok': true,
              'before': false,
              'after_mode': true,
              'verdict': 'applied',
              'verification': 'PowerManager + Settings re-read',
            });
    final out = await r.run(job('android.power_saver.set', {'on': true}));
    expect(out.ok, isTrue);
    expect(out.result['power_save'], isTrue);
    expect(out.result['verdict'], equals('applied'));
  });

  test('power saver without the T1 grant fails with the setup path',
      () async {
    final r = runner(
        powerSaverSet: (on) async => {
              'granted': false,
              'detail': 'WRITE_SECURE_SETTINGS not granted (adb shell '
                  'pm grant dev.zara.zara_android ...)',
            });
    final out = await r.run(job('android.power_saver.set', {'on': true}));
    expect(out.ok, isFalse);
    expect(out.error, contains('pm grant'));
  });

  // ---------------- alarm ----------------
  test('alarm handed-to-clock verdict (user completes it)', () async {
    final r = runner(
        alarmSet: (hour, minute, label) async {
          expect(hour, 7);
          expect(minute, 30);
          expect(label, 'zara-test');
          return {
            'fired': true,
            'verdict': 'handed-to-clock',
            'hour': hour,
            'minute': minute,
            'verification': 'clock opens pre-filled',
          };
        });
    final out = await r.run(job('android.alarm.set',
        {'hour': 7, 'minute': 30, 'label': 'zara-test'}));
    expect(out.ok, isTrue);
    expect(out.result['verdict'], equals('handed-to-clock'));
    expect(out.result['hour'], 7);
  });

  test('alarm skip_ui is refused structurally (user completes by design)',
      () async {
    final r = runner(
        alarmSet: (hour, minute, label) async => {
              'fired': false,
              'detail': 'skip_ui refused by design',
            });
    final out = await r.run(
        job('android.alarm.set', {'hour': 7, 'skip_ui': true}));
    expect(out.ok, isFalse);
    expect(out.error, contains('skip_ui'));
  });

  // ---------------- timer (P5 feature-hunt 2026-10-22) ----------------
  test('timer handed-to-clock verdict (user completes it)', () async {
    final r = runner(
        timerSet: (minutes, label) async {
          expect(minutes, 5);
          expect(label, 'eggs');
          return {
            'fired': true,
            'verdict': 'handed-to-clock',
            'minutes': minutes,
            'verification': 'clock opens pre-filled',
          };
        });
    final out = await r.run(
        job('android.timer.set', {'minutes': 5, 'label': 'eggs'}));
    expect(out.ok, isTrue);
    expect(out.result['verdict'], equals('handed-to-clock'));
    expect(out.result['minutes'], 5);
    expect(out.result['verified'], isFalse, reason: 'hand-off verdict');
  });

  test('timer skip_ui is refused structurally (user completes by design)',
      () async {
    final r = runner(
        timerSet: (minutes, label) async => {
              'fired': false,
              'detail': 'skip_ui refused by design',
            });
    final out = await r.run(job('android.timer.set', {'skip_ui': true}));
    expect(out.ok, isFalse);
    expect(out.error, contains('skip_ui'));
  });

  // ---------------- DND (P5 feature-hunt ② 2026-10-22) ----------------
  test('dnd get reports the named filter + policy access read-only',
      () async {
    final r = runner(
        dndGet: () async => {
              'ok': true,
              'filter': 1,
              'filter_name': 'all (DND off)',
              'dnd_on': false,
              'policy_access': true,
            });
    final out = await r.run(job('android.dnd.get', {}));
    expect(out.ok, isTrue);
    expect(out.result['filter'], 1);
    expect(out.result['dnd_on'], isFalse);
    expect(out.result['policy_access'], isTrue);
  });

  test('dnd set carries the honest verified re-read', () async {
    final r = runner(
        dndSet: (enabled) async {
          expect(enabled, true);
          return {
            'ok': true,
            'fired': true,
            'granted': true,
            'filter': 3,
            'verification': 'interruption filter re-read after set',
            'verified': true,
          };
        });
    final out = await r.run(job('android.dnd.set', {'enabled': true}));
    expect(out.ok, isTrue);
    expect(out.result['verdict'], equals('verified-by-reread'));
    expect(out.result['verified'], isTrue);
  });

  test('dnd set without policy access refuses honestly (nothing changed)',
      () async {
    final r = runner(
        dndSet: (enabled) async => {
              'ok': false,
              'fired': false,
              'granted': false,
              'detail': 'Do Not Disturb policy access not granted',
            });
    final out = await r.run(job('android.dnd.set', {'enabled': true}));
    expect(out.ok, isFalse);
    expect(out.error, contains('policy'));
  });

  test('dnd gateways unavailable report honestly per tool', () async {
    final r = runner();
    final g = await r.run(job('android.dnd.get', {}, 'job-dnd-g'));
    expect(g.ok, isFalse);
    expect(g.error, contains('unavailable'));
    final s = await r.run(job('android.dnd.set', {'enabled': true},
        'job-dnd-s'));
    expect(s.ok, isFalse);
    expect(s.error, contains('unavailable'));
  });

  // ---------------- gating ----------------
  test('all eleven quick-action tools are allowlisted on the runner',
      () {
    for (final t in const [
      'android.torch.set',
      'android.volume.set',
      'android.ring',
      'android.media.playback',
      'android.clipboard.set',
      'android.open_uri',
      'android.power_saver.set',
      'android.alarm.set',
      'android.timer.set',
      'android.dnd.get',
      'android.dnd.set',
    ]) {
      expect(AndroidJobRunner.allowlisted.contains(t), isTrue,
          reason: t);
    }
  });

  test('missing gateways report honestly per tool', () async {
    final r = runner();
    final torch = await r.run(job('android.torch.set', {'on': true}));
    expect(torch.ok, isFalse);
    expect(torch.error, contains('unavailable'));
    final ring = await r.run(job('android.ring', {}));
    expect(ring.ok, isFalse);
    final clip = await r.run(job('android.clipboard.set', {'text': 'x'}));
    expect(clip.ok, isFalse);
    final alarm = await r.run(job('android.alarm.set', {}));
    expect(alarm.ok, isFalse);
    final open = await r.run(job('android.open_uri', {'uri': 'x:1'}));
    expect(open.ok, isFalse);
    final vol = await r.run(job('android.volume.set', {'pct': 5}));
    expect(vol.ok, isFalse);
    final media = await r.run(job('android.media.playback', {'action': 'pause'}));
    expect(media.ok, isFalse);
    final pow = await r.run(job('android.power_saver.set', {'on': true}));
    expect(pow.ok, isFalse);
  });
}
