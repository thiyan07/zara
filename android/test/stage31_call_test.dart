// Stage 31-P3 phase B — call with contact resolution at the job runner
// (android.contacts.lookup / android.call.place).
//
// Runner-seam discipline (fixture-only; fake gateways, no device):
// 1. No contacts grant / refused lookup → ok:false with the honest
//    error text; a zero-match SUCCESS is ok:true with count 0 + the
//    matches list (not-found is an answer, never a failure).
// 2. The call result names the number's TAIL only — the full number
//    never enters results (it lives in the approval prompt).
// 3. A fired call is NOT verified by the firing: the runner's verified
//    flag is the a11y poll's verdict (confirmed), never the dispatch;
//    absence is honest data riding ok:true.
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/job_runner.dart';

DeviceJob job(String tool, Map<String, dynamic> inputs,
        [String? id]) =>
    DeviceJob(
        jobId: id ?? 'job-${DateTime.now().microsecondsSinceEpoch}',
        tool: tool,
        inputs: inputs,
        timeoutS: 20,
        executionId: 'exec-${id ?? DateTime.now().microsecondsSinceEpoch}');

AndroidJobRunner runner({
  ContactLookupProbe? contactsLookup,
  CallDirectProbe? callDirect,
  CallScreenPollProbe? callScreenPoll,
  SmsSendProbe? smsSend,
  OtpProbe? otpScan,
  MediaSessionProbe? mediaSessionTargeted,
  PhotosInventoryProbe? photosInventory,
}) =>
    AndroidJobRunner(
      battery: () => {},
      network: () => {},
      contactsLookup: contactsLookup,
      callDirect: callDirect,
      callScreenPoll: callScreenPoll,
      smsSend: smsSend,
      otpScan: otpScan,
      mediaSessionTargeted: mediaSessionTargeted,
      photosInventory: photosInventory,
    );

void main() {
  // ---------------- contacts lookup ----------------
  test('contacts lookup returns the bounded matches verbatim', () async {
    final r = runner(
        contactsLookup: (name, limit) async =>
            {'supported': true, 'ok': true, 'query': name, 'count': 1,
             'matches': [
               {'contact_id': 'c1', 'display_name': 'Sarah Chen',
                'number': '+15551230100', 'starred': false},
             ]});
    final out =
        await r.run(job('android.contacts.lookup', {'name': 'sarah'}));
    expect(out.ok, isTrue);
    expect(out.result['count'], equals(1));
    expect(out.result['matches'], isA<List<dynamic>>());
    final row = (out.result['matches'] as List).first as Map;
    expect(row['display_name'], equals('Sarah Chen'));
  });

  test('a zero-match read is an ANSWER: ok:true, count 0', () async {
    final r = runner(
        contactsLookup: (name, limit) async =>
            {'supported': true, 'ok': true, 'query': name, 'count': 0,
             'matches': <dynamic>[]});
    final out =
        await r.run(job('android.contacts.lookup', {'name': 'priya'}));
    expect(out.ok, isTrue);
    expect(out.result['count'], equals(0));
  });

  test('missing READ_CONTACTS grant reports honestly as ok:false',
      () async {
    final r = runner(
        contactsLookup: (name, limit) async => {
              'supported': true,
              'available': false,
              'detail': 'READ_CONTACTS not granted',
            });
    final out =
        await r.run(job('android.contacts.lookup', {'name': 'x'}));
    expect(out.ok, isFalse);
    expect(out.error, contains('READ_CONTACTS not granted'));
  });

  test('missing gateways report honestly per tool', () async {
    final r1 = runner();
    final out1 = await r1
        .run(job('android.contacts.lookup', {'name': 'x'}));
    expect(out1.ok, isFalse);
    expect(out1.error, contains('gateway unavailable'));
    final out2 = await r1.run(job('android.call.place', {'number': '1'}));
    expect(out2.ok, isFalse);
    expect(out2.error, contains('gateway unavailable'));
  });

  // ---------------- the call ----------------
  test('confirmed call screen sets verified — a fired intent is not proof',
      () async {
    final r = runner(
        callDirect: (number) async => {'ok': true, 'verdict': 'fired'},
        callScreenPoll: (waitMs) async =>
            {'verdict': 'confirmed', 'package': 'com.android.dialer',
             'verification': 'focused-window a11y re-read'});
    final out = await r.run(
        job('android.call.place', {'number': '+15551230100'}));
    expect(out.ok, isTrue);
    expect(out.result['fired'], isTrue);
    expect(out.result['verdict'], equals('confirmed'));
    expect(out.result['verified'], isTrue);
    // the FULL number never leaves the approval context.
    final blob = out.result.toString();
    expect(blob.contains('15551230100'), isFalse);
    expect(blob.contains('*0100'), isTrue);
  });

  test('fired but no call screen → honest unverified, never "placed"',
      () async {
    final r = runner(
        callDirect: (number) async => {'ok': true, 'verdict': 'fired'},
        callScreenPoll: (waitMs) async => {
              'verdict': 'absent',
              'verification': 'no call screen within the bounded window',
            });
    final out = await r.run(
        job('android.call.place', {'number': '+15551230100'}));
    expect(out.ok, isTrue);
    expect(out.result['verified'], isFalse);
    expect(out.result['verdict'], equals('absent'));
  });

  test('missing CALL_PHONE grant / dial-grammar refuse are ok:false',
      () async {
    String mode = 'grant';
    final r = runner(
        callDirect: (number) async => mode == 'dismissal'
            ? {'supported': true, 'available': false,
               'detail': 'CALL_PHONE not granted'}
            : {'supported': true, 'ok': false,
               'error': 'number outside dial grammar — refused'},
        callScreenPoll: (w) async => {'verdict': 'absent'});
    var out = await r.run(job('android.call.place', {'number': 'x'}));
    expect(out.ok, isFalse);
    expect(out.error, contains('dial grammar'));
    mode = 'dismissal';
    out = await r.run(job('android.call.place', {'number': 'x'}));
    expect(out.ok, isFalse);
    expect(out.error, contains('CALL_PHONE not granted'));
  });

  // ---------------- P3e: native carrier SMS ----------------
  test('sms send: honest SENT verdict, text length only, tail only',
      () async {
    final r = runner(
        smsSend: (number, text) async => {
              'supported': true,
              'ok': true,
              'verdict': 'sent',
              'text_len': 27,
              'verification': 'SmsManager accepted the send',
            });
    final out = await r.run(job(
        'android.sms.send',
        {'number': '+15551230100',
         'text': 'I\'m running 20 minutes late'}));
    expect(out.ok, isTrue);
    expect(out.result['verdict'], equals('sent'));
    // delivery is NOT confirmable — honest false.
    expect(out.result['verified'], isFalse);
    expect(out.result['text_len'], equals(27));
    final blob = out.result.toString();
    // the DRAFT never rides the result; nor the full number.
    expect(blob.contains('running 20 minutes'), isFalse);
    expect(blob.contains('15551230100'), isFalse);
    expect(blob.contains('*0100'), isTrue);
  });

  test('sms gateways/grants/caps refuse honestly before any send',
      () async {
    final r = runner();
    final out = await r.run(
        job('android.sms.send', {'number': '1', 'text': 'x'}));
    expect(out.ok, isFalse);
    expect(out.error, contains('gateway unavailable'));
    final r2 = runner(
        smsSend: (number, text) async => {
              'supported': true,
              'available': false,
              'detail': 'SEND_SMS not granted',
            });
    final out2 = await r2.run(
        job('android.sms.send', {'number': '1', 'text': 'x'}));
    expect(out2.ok, isFalse);
    expect(out2.error, contains('SEND_SMS not granted'));
  });

  // ---------------- P3f: per-use consented OTP read ----------------
  test('otp read: candidates pass, zero-fresh is honest data', () async {
    final r = runner(
        otpScan: (maxAgeMs, hint) async => {
              'supported': true,
              'count': 1,
              'fresh_count': 2,
              'candidates': [
                {'posted_at_ms': 0, 'age_ms': 3000,
                 'package': 'com.google.android.gms',
                 'title': 'Google Verify', 'code': '483920'},
              ],
              'verification': 'fresh-window ring scan',
            });
    final out = await r.run(
        job('android.otp.read', {'max_age_ms': 300000}));
    expect(out.ok, isTrue);
    expect(out.result['count'], equals(1));
    final candidates = out.result['candidates'] as List;
    expect((candidates.first as Map)['code'], equals('483920'));
  });

  test('otp gateway unavailable + unsupported refuse honestly', () async {
    final r = runner();
    final out = await r.run(job('android.otp.read', {}));
    expect(out.ok, isFalse);
    expect(out.error, contains('gateway unavailable'));
    final absent = runner(
        otpScan: (maxAgeMs, hint) async =>
            {'supported': false, 'detail': 'listener unavailable'});
    final out2 = await absent.run(job('android.otp.read', {}));
    expect(out2.ok, isFalse);
    expect(out2.error, contains('listener unavailable'));
  });

  // ---------------- P3a: targeted media control ----------------
  test('media control: targeted verdict with state re-read', () async {
    final r = runner(
        mediaSessionTargeted: (hint, action) async => {
              'supported': true, 'ok': true, 'count': 1,
              'package': 'org.videolan.vlc', 'action': action,
              'state_before': 'playing', 'state_after': 'paused',
              'verification': 'transportControls dispatched',
            });
    final out = await r.run(
        job('android.media.control', {'hint': 'vlc', 'action': 'pause'}));
    expect(out.ok, isTrue);
    expect(out.result['state_before'], equals('playing'));
    expect(out.result['state_after'], equals('paused'));
    expect(out.result['package'], equals('org.videolan.vlc'));
  });

  test('media control: honest zero/multi-match answers, refused hintless',
      () async {
    final zero = runner(
        mediaSessionTargeted: (hint, action) async => {
              'supported': true, 'ok': true, 'count': 0,
              'active': ['youtube', 'chrome'],
              'verification': 'no session matches the hint',
            });
    final out = await zero
        .run(job('android.media.control', {'hint': 'vlc', 'action': 'pause'}));
    expect(out.ok, isTrue);
    expect(out.result['count'], equals(0));
    final active = out.result['active'] as List;
    expect(active.length, equals(2));
    final refused = runner(
        mediaSessionTargeted: (hint, action) async => {
              'supported': true, 'ok': false,
              'error': 'target app hint required',
            });
    final out2 = await refused.run(
        job('android.media.control', {'action': 'pause'}));
    expect(out2.ok, isFalse);
    expect(out2.error, contains('hint required'));
    final unsupported = runner(
        mediaSessionTargeted: (hint, action) async =>
            {'supported': false, 'detail': 'controller unavailable'});
    final out3 = await unsupported
        .run(job('android.media.control', {'hint': 'x', 'action': 'play'}));
    expect(out3.ok, isFalse);
    expect(out3.error, contains('controller unavailable'));
  });

  // ---------------- P3c: pull-only photo inventory ----------------
  test('photo inventory: counts-only verdict rides folders, never names',
      () async {
    final r = runner(
        photosInventory: () async => {
              'supported': true,
              'count': 812,
              'total_bytes': 4120000000,
              'newest_ms': 1730000000000,
              'oldest_ms': 1600000000000,
              'folders': ['DCIM/Camera'],
              'verification': 'counts only',
            });
    final out = await r.run(job('android.photos.inventory', {}));
    expect(out.ok, isTrue);
    expect(out.result['count'], equals(812));
    expect((out.result['folders'] as List).first,
        equals('DCIM/Camera'));
  });

  test('photo inventory: empty library + unavailable gateway honest',
      () async {
    final zero = runner(
        photosInventory: () async => {
              'supported': true,
              'count': 0,
              'total_bytes': 0,
              'newest_ms': -1,
              'oldest_ms': -1,
              'folders': <String>[],
              'verification': 'counts only',
            });
    final out = await zero.run(job('android.photos.inventory', {}));
    expect(out.ok, isTrue);
    expect(out.result['count'], equals(0));
    final r2 = runner();
    final out2 = await r2.run(job('android.photos.inventory', {}));
    expect(out2.ok, isFalse);
    expect(out2.error, contains('gateway unavailable'));
  });
}
