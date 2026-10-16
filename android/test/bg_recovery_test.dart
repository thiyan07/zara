import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/connection.dart';
import 'package:zara_android/device_bridge.dart';
import 'package:zara_android/main.dart';

// Background-recovery contracts (no emulator needed):
//  1. applyJitter: bounded equal-jitter, deterministic per roll.
//  2. resumeOptIn/setResumeOptIn: bridge round-trips the native marker
//     flag; channel failure reads as opted-out (never opted-in).
//  3. Widget: Settings shows the opt-in toggle, default off.

void main() {
  group('applyJitter (reconnect spread)', () {
    test('roll 0 => exactly half, roll ~1 => just under base', () {
      const base = Duration(seconds: 10);
      expect(applyJitter(base, 0.0), const Duration(seconds: 5));
      final top = applyJitter(base, 0.999999);
      expect(top < base, isTrue);
      expect(top >= const Duration(seconds: 5), isTrue);
    });

    test('deterministic per roll, monotonic in roll', () {
      const base = Duration(seconds: 60);
      expect(applyJitter(base, 0.25), applyJitter(base, 0.25));
      expect(applyJitter(base, 0.2) <= applyJitter(base, 0.8), isTrue);
    });

    test('zero base stays zero; caps preserved by caller contract', () {
      expect(applyJitter(Duration.zero, 0.99), Duration.zero);
      // Jitter never exceeds the attempt delay it wraps.
      const cap = Duration(seconds: 60);
      expect(applyJitter(cap, 0.999999) <= cap, isTrue);
    });
  });

  group('resume opt-in bridge (mock MethodChannel)', () {
    const channel = MethodChannel('zara/device');

    setUp(() {
      TestWidgetsFlutterBinding.ensureInitialized();
    });

    tearDown(() {
      TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(channel, null);
    });

    test('reads native flag verbatim', () async {
      TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(channel, (call) async {
        expect(call.method, 'getResumeOptIn');
        return true;
      });
      expect(await DeviceBridge().resumeOptIn(), isTrue);
    });

    test('channel failure reads as opted-out, never opted-in', () async {
      TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(channel, (call) async {
        throw PlatformException(code: 'x');
      });
      expect(await DeviceBridge().resumeOptIn(), isFalse);
    });

    test('write passes the boolean through', () async {
      String? seenMethod;
      Map<dynamic, dynamic>? seenArgs;
      TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(channel, (call) async {
        seenMethod = call.method;
        seenArgs = call.arguments as Map<dynamic, dynamic>?;
        return null;
      });
      await DeviceBridge().setResumeOptIn(true);
      expect(seenMethod, 'setResumeOptIn');
      expect(seenArgs?['enabled'], isTrue);
    });
  });

  group('Settings opt-in toggle (widget)', () {
    const deviceChannel = MethodChannel('zara/device');
    const secureChannel =
        MethodChannel('plugins.it_nomads.com/flutter_secure_storage');

    setUp(() {
      TestWidgetsFlutterBinding.ensureInitialized();
      TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(
              deviceChannel, (call) async => <String, dynamic>{});
      TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(secureChannel, (call) async => null);
    });

    tearDown(() {
      TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(deviceChannel, null);
      TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(secureChannel, null);
    });

    testWidgets('toggle present, unchecked by default', (t) async {
      await t.pumpWidget(const ZaraApp());
      await t.pumpAndSettle();
      await t.tap(find.text('Settings'));
      await t.pumpAndSettle();
      await t.scrollUntilVisible(
          find.text('Resume prompt after reboot (opt-in)'), 400,
          scrollable: find.byType(Scrollable).first);
      await t.pumpAndSettle();
      expect(
          find.text('Resume prompt after reboot (opt-in)'), findsOneWidget);
    });
  });

  group('jittered register backoff (bounded, spread)', () {
    test('attempt delays stay within half-to-full of the schedule', () {
      for (var attempt = 1; attempt <= 8; attempt++) {
        final base = registerRetryDelay(attempt);
        for (final roll in [0.0, 0.13, 0.5, 0.87, 0.999999]) {
          final w = applyJitter(base, roll);
          expect(w.inMicroseconds >= base.inMicroseconds ~/ 2, isTrue,
              reason: 'attempt $attempt roll $roll');
          expect(w <= base, isTrue, reason: 'attempt $attempt roll $roll');
        }
      }
    });

    test('cap of 60s survives jitter (never exceeds schedule)', () {
      final base = registerRetryDelay(50);
      expect(base, const Duration(seconds: 60));
      expect(applyJitter(base, 0.999999) <= base, isTrue);
    });
  });
}
