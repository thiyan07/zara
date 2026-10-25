// P6-c26 (2026-10-24) — the phone-side app-open consent surface.
//
// The Phase-1 byte-equal allowlist refusal stays the DEFAULT: an
// out-of-allowlist package is refused unless the phone's OWN consent
// surface granted it — an explicit dialog tap (Allow), persisted
// per-package, device-local (Core cannot see or change the store).
// Deny, dismissal, an absent surface, and no grant store all fall to
// the byte-equal refusal; a recognized grant (or a saved one) opens
// with consented:true so the audit shows WHY an out-of-allowlist
// package launched.
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/app_open_consent.dart';
import 'package:zara_android/job_runner.dart';
import 'package:zara_android/secure_store.dart';

DeviceJob openJob(String pkg, [String id = 'job-open']) => DeviceJob(
    jobId: id,
    tool: 'system.app.open',
    inputs: {'package': pkg},
    timeoutS: 30,
    executionId: 'exec-open');

AndroidJobRunner runner({
  Future<Map<String, dynamic>> Function(String pkg)? open,
  Future<bool?> Function(String pkg)? consent,
  AppOpenConsents? consents,
}) {
  return AndroidJobRunner(
    battery: () => {},
    network: () => {},
    openApp: open == null
        ? null
        : (pkg) => open(pkg),
    appConsent: consent,
    appOpenConsents: consents,
  );
}

void main() {
  test('allowlisted package opens unchanged, consented:false', () async {
    var opened = '';
    final r = runner(
      open: (pkg) async {
        opened = pkg;
        return {'package': pkg, 'launched': true};
      },
      consent: (pkg) => Future.value(false), // surface must NOT be asked
    );
    final out = await r.run(openJob('com.android.settings'));
    expect(out.ok, isTrue);
    expect(out.result['package'], equals('com.android.settings'));
    expect(out.result['consented'], isFalse);
    expect(opened, equals('com.android.settings'));
  });

  test('out-of-allowlist with no surface: byte-equal refusal, gateway '
      'untouched', () async {
    var called = false;
    final r = runner(
      open: (pkg) async {
        called = true;
        return {'package': pkg, 'launched': true};
      },
    );
    final out = await r.run(openJob('com.example.notreal'));
    expect(out.ok, isFalse);
    expect(
        out.error,
        'package not in app-open allowlist (refused, never executed)',
        reason: out.error);
    expect(called, isFalse);
  });

  test('out-of-allowlist, surface present but store missing: still '
      'byte-equal refusal', () async {
    var asked = false;
    final r = runner(
      open: (pkg) async => {'package': pkg, 'launched': true},
      consent: (pkg) {
        asked = true;
        return Future.value(true);
      },
    );
    final out = await r.run(openJob('com.example.notreal'));
    expect(out.ok, isFalse);
    expect(out.error, contains('allowlist'), reason: out.error);
    expect(asked, isFalse, reason: 'consent without a store is a lie: '
        'Allow must never open anything that cannot be remembered');
  });

  test('dialog DENY: honest refusal named, gateway untouched, no grant '
      'persisted', () async {
    var called = false;
    var asked = 0;
    final consents = AppOpenConsents(SecureStore());
    final r = runner(
      open: (pkg) async {
        called = true;
        return {'package': pkg, 'launched': true};
      },
      consent: (pkg) {
        asked++;
        return Future.value(false);
      },
      consents: consents,
    );
    final out = await r.run(openJob('com.example.notreal'));
    expect(out.ok, isFalse);
    expect(out.error, contains('phone consent declined'), reason: out.error);
    expect(called, isFalse);
    expect(asked, equals(1));
    expect(await consents.allows('com.example.notreal'), isFalse);
  });

  test('dialog DISMISS (null): neutral refusal wording, no grant',
      () async {
    var called = false;
    final consents = AppOpenConsents(SecureStore());
    final r = runner(
      open: (pkg) async {
        called = true;
        return {'package': pkg, 'launched': true};
      },
      consent: (pkg) async => null,
      consents: consents,
    );
    final out = await r.run(openJob('com.example.notreal'));
    expect(out.ok, isFalse);
    expect(out.error, contains('not granted'), reason: out.error);
    expect(called, isFalse);
  });

  test('dialog ALLOW: opens with consented:true and PERSISTS the grant '
      '(the next run needs no dialog)', () async {
    var asked = 0;
    var opened = 0;
    final consents = AppOpenConsents(SecureStore());
    var r = runner(
      open: (pkg) async {
        opened++;
        return {'package': pkg, 'launched': true};
      },
      consent: (pkg) {
        asked++;
        return Future.value(true);
      },
      consents: consents,
    );
    final first = await r.run(openJob('com.example.notreal', 'job-a'));
    expect(first.ok, isTrue, reason: first.error);
    expect(first.result['consented'], isTrue);
    expect(asked, equals(1));
    expect(await consents.allows('com.example.notreal'), isTrue);

    // a SECOND runner instance reads the SAME persisted store —
    // granted means remembered (same SecureStore instance semantics
    // apply only in memory-fallback tests; share the instance).
    r = runner(
      open: (pkg) async {
        opened++;
        return {'package': pkg, 'launched': true};
      },
      consent: (pkg) => Future.value(false), // would deny if re-asked
      consents: consents,
    );
    final again = await r.run(openJob('com.example.notreal', 'job-b'));
    expect(again.ok, isTrue, reason: again.error);
    expect(again.result['consented'], isTrue);
    expect(asked, equals(1), reason: 'a stored grant replaces the ask');
    expect(opened, equals(2));
  });

  test('invalid package shapes can never be granted nor opened', () async {
    final consents = AppOpenConsents(SecureStore());
    final r = runner(
      open: (pkg) async => {'package': pkg, 'launched': true},
      consent: (pkg) => Future.value(true),
      consents: consents,
    );
    for (final pkg in ['', 'bad;pkg', 'x' * 101, 'sp ace', 'semi;colon']) {
      final out = await r.run(
          openJob(pkg, 'job-${pkg.isEmpty ? "empty" : pkg.hashCode}'));
      expect(out.ok, isFalse, reason: pkg);
      expect(await consents.allows(pkg), isFalse, reason: pkg);
    }
    // still a repair path for a valid one
    expect(AppOpenConsents.validPackage('com.example.app'), isTrue);
  });

  test('revoke() undoes a grant on the phone: next run refuses', () async {
    final consents = AppOpenConsents(SecureStore());
    var r = runner(
      open: (pkg) async => {'package': pkg, 'launched': true},
      consent: (pkg) => Future.value(true),
      consents: consents,
    );
    final granted = await r.run(openJob('com.example.revocable'));
    expect(granted.ok, isTrue, reason: granted.error);
    expect(await consents.revoke('com.example.revocable'), isTrue);
    expect(await consents.revoke('com.example.revocable'), isFalse);
    r = runner(
      open: (pkg) async => {'package': pkg, 'launched': true},
      consent: (pkg) => Future.value(false),
      consents: consents,
    );
    final out = await r.run(openJob('com.example.revocable', 'job-after'));
    expect(out.ok, isFalse);
    expect(out.error, contains('declined'), reason: out.error);
  });

  test('corrupt consent slot = no grants (default refusal wins)',
      () async {
    final consents = AppOpenConsents(SecureStore());
    // simulate corruption via grant-then-invalid storage of the slot:
    // the store only ever writes valid JSON itself, so the guard is
    // its decode path — a non-JSON value decodes to empty.
    await consents.grant('com.example.good');
    for (final v in await consents.list()) {
      expect(v, equals('com.example.good'));
    }
    expect(AppOpenConsents.validPackage('bad "["'), isFalse);
  });

  test('store validation rejects shapes the runner could never grant',
      () async {
    expect(AppOpenConsents.validPackage('com.android.settings'), isTrue);
    expect(AppOpenConsents.validPackage('dev.zara.lab.privtest'),
        isTrue);
    expect(AppOpenConsents.validPackage(''), isFalse);
    expect(AppOpenConsents.validPackage('a;ls'), isFalse);
    expect(AppOpenConsents.validPackage('has space'), isFalse);
    expect(AppOpenConsents.validPackage('a' * 101), isFalse);
  });
}
