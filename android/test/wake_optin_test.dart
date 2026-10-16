import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter/services.dart';
import 'package:zara_android/device_bridge.dart';
import 'package:zara_android/wake_optin.dart';

Map<String, dynamic> okFlags() => {
      'supported': true,
      'wake_phrase': 'Hey Zara',
      'microphone_hardware': true,
      'microphone_permission': true,
      'microphone_signal': 'unknown',
      'wake_word_engine': false,
      'note': 'provider-backed voice pending hardware validation',
    };

WakeOptInState armedState({Future<void> Function()? onRelease}) {
  final s = WakeOptInState(initialFlags: okFlags(), onRelease: onRelease);
  s.updateBattery(pct: 80, charging: false, powerSave: false);
  return s;
}

void main() {
  test('opt-in defaults OFF and in-memory (no storage)', () {
    final s = armedState();
    expect(s.optedIn, isFalse);
    expect(s.status, equals(WakeOptInStatus.disabled));
    expect(s.statusLabel, contains('disabled'));
    expect(s.effectivelyArmed, isFalse);
  });

  test('enable gating matrix: permission x battery x support flags', () async {
    final batteries = {
      'ok': [80.0, false, false],
      'low': [10.0, false, false],
      'powersave': [50.0, false, true],
      'charging-low': [10.0, true, false],
    };
    final flagSets = {
      'supported': okFlags(),
      'no-mic-hw': {...okFlags(), 'microphone_hardware': false},
      'bridge-unsupported': {'supported': false},
      'unchecked': <String, dynamic>{},
    };
    for (final fEntry in flagSets.entries) {
      for (final bEntry in batteries.entries) {
        for (final mic in [true, false]) {
          final s = WakeOptInState(initialFlags: fEntry.value);
          final b = bEntry.value;
          s.updateBattery(
              pct: b[0] as double?,
              charging: b[1] as bool,
              powerSave: b[2] as bool);
          await s.updatePermission(mic);
          await s.setOptIn(true);
          final gateLow =
              (b[0] as double) < 15.0 && !(b[1] as bool) || (b[2] as bool);
          WakeOptInStatus want;
          if (fEntry.key != 'supported') {
            want = WakeOptInStatus.unsupported;
          } else if (!mic) {
            want = WakeOptInStatus.permissionRequired;
          } else if (gateLow) {
            want = WakeOptInStatus.pausedBattery;
          } else {
            want = WakeOptInStatus.enabled;
          }
          expect(s.status, equals(want),
              reason:
                  'flags=${fEntry.key} battery=${bEntry.key} mic=$mic');
          expect(s.effectivelyArmed,
              equals(want == WakeOptInStatus.enabled));
          // Opt-out always lands back on disabled (or unsupported when the
          // device itself cannot do wake).
          await s.setOptIn(false);
          expect(
              s.status,
              equals(fEntry.key == 'supported'
                  ? WakeOptInStatus.disabled
                  : WakeOptInStatus.unsupported));
        }
      }
    }
  });

  test('opt-out alone stays disabled even with permission+battery ok', () async {
    final s = armedState();
    await s.updatePermission(true);
    expect(s.status, equals(WakeOptInStatus.disabled));
    expect(s.effectivelyArmed, isFalse);
  });

  test('disable releases resources via the existing stop path', () async {
    var stops = 0;
    final s = armedState(onRelease: () async => stops++);
    await s.updatePermission(true);
    await s.setOptIn(true);
    expect(s.status, equals(WakeOptInStatus.enabled));
    await s.setOptIn(false);
    expect(s.status, equals(WakeOptInStatus.disabled));
    expect(s.releaseCalls, equals(1));
    expect(stops, equals(1));
    // Release failure never crashes the state machine.
    final s2 = armedState(onRelease: () => throw StateError('stop failed'));
    await s2.updatePermission(true);
    await s2.setOptIn(true);
    await s2.setOptIn(false);
    expect(s2.releaseCalls, equals(1));
    expect(s2.status, equals(WakeOptInStatus.disabled));
  });

  test('revoked permission mid-flow stops: explicit state, no crash',
      () async {
    var stops = 0;
    final s = armedState(onRelease: () async => stops++);
    await s.updatePermission(true);
    await s.setOptIn(true);
    expect(s.effectivelyArmed, isTrue);
    await s.updatePermission(false); // OS revoked mid-flow
    expect(s.status, equals(WakeOptInStatus.permissionRequired));
    expect(s.statusLabel, contains('permission required'));
    expect(s.effectivelyArmed, isFalse);
    expect(s.releaseCalls, equals(1));
    expect(stops, equals(1));
    // Re-grant re-arms (opt-in is sticky, permission is the gate).
    await s.updatePermission(true);
    expect(s.status, equals(WakeOptInStatus.enabled));
  });

  test('caption honesty: technical availability strings, never marketing',
      () async {
    final s = armedState();
    expect(s.caption, contains('wake_word_engine=false'));
    expect(s.caption, contains('always-on listening is not available'));
    expect(s.caption, contains('push-to-talk only'));
    s.updateEngineAvailability(
        {'always_on_dsp': false, 'physically_validated': false});
    expect(s.caption, contains('always_on_dsp=false'));
    expect(s.caption, contains('physically_validated=false'));
    for (final hype in [
      'smart',
      'seamless',
      'effortless',
      'AI-powered',
      'just say',
      'always listening',
      'hands-free magic'
    ]) {
      expect(s.caption.toLowerCase(), isNot(contains(hype.toLowerCase())),
          reason: 'caption must not contain marketing "$hype"');
    }
    // Unknown engine availability reports unknown — never claims.
    final u = armedState();
    expect(u.engineAvailability, isNull);
    expect(u.caption, contains('always-on listening is not available'));
  });

  test('no background timers: manual refresh only', () {
    // Code lines only: comments may name the forbidden APIs to document
    // their absence, but no code line may reference them.
    final code = File('lib/wake_optin.dart')
        .readAsLinesSync()
        .where((l) => !l.trimLeft().startsWith('//'))
        .join('\n');
    expect(code, isNot(contains('Timer')));
    expect(code, isNot(contains('periodic')));
    expect(code, isNot(contains('Future.delayed')));
    expect(code, isNot(contains('AlarmManager')));
    expect(code, isNot(contains('dart:async')));
    expect(code, isNot(contains('RECORD_AUDIO')));
    expect(code, isNot(contains('requestPermissions')));
  });

  testWidgets('panel shows switch off + honest caption + manual buttons',
      (tester) async {
    final s = armedState();
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(body: WakeOptInPanel(state: s))));
    expect(find.byType(SwitchListTile), findsOneWidget);
    expect(find.textContaining('disabled'), findsWidgets);
    expect(find.textContaining('wake_word_engine=false'), findsOneWidget);
    expect(
        find.textContaining('always-on listening is not available'),
        findsOneWidget);
    expect(find.text('Enable mic'), findsOneWidget);
    expect(find.text('Refresh wake state'), findsOneWidget);
  });

  testWidgets('panel flip without mic shows permission-required state',
      (tester) async {
    final s = armedState();
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(body: WakeOptInPanel(state: s))));
    await tester.tap(find.byType(SwitchListTile));
    await tester.pumpAndSettle();
    expect(s.optedIn, isTrue);
    expect(find.textContaining('permission required'), findsWidgets);
    expect(s.effectivelyArmed, isFalse);
  });

  testWidgets('panel reflects paused/enabled states after snapshots',
      (tester) async {
    final s = armedState();
    await s.updatePermission(true);
    await s.setOptIn(true);
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(body: WakeOptInPanel(state: s))));
    await tester.pump();
    expect(find.textContaining('no always-on loop'), findsOneWidget);
    s.updateBattery(pct: 5, charging: false, powerSave: false);
    await tester.pump();
    expect(find.textContaining('paused'), findsWidgets);
  });

group('wake preference bridge (mock MethodChannel)', () {
  const channel = MethodChannel('zara/device');

  setUp(() {
    TestWidgetsFlutterBinding.ensureInitialized();
  });

  tearDown(() {
    TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null);
  });

  test('reads native flag verbatim; failure reads OFF, never ON', () async {
    TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      expect(call.method, 'getWakeOptIn');
      return true;
    });
    expect(await DeviceBridge().wakeOptIn(), isTrue);
    TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      throw PlatformException(code: 'x');
    });
    expect(await DeviceBridge().wakeOptIn(), isFalse);
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
    await DeviceBridge().setWakeOptIn(true);
    expect(seenMethod, 'setWakeOptIn');
    expect(seenArgs?['enabled'], isTrue);
  });
});

group('detector phases: persisted ON/OFF, single listener, lifecycle', () {
  WakeOptInState liveState(
      {Future<String?> Function()? onStart, Future<void> Function()? onRelease}) {
    final s = armedState(onRelease: onRelease);
    s.onStartListening = onStart;
    return s;
  }

  Future<WakeOptInState> armedForListen(
      {Future<String?> Function()? onStart,
      Future<void> Function()? onRelease}) async {
    final s = liveState(onStart: onStart ?? () async => null,
        onRelease: onRelease);
    await s.updatePermission(true);
    await s.setOptIn(true);
    return s;
  }

  test('preference defaults OFF; persisted load never auto-starts', () async {
    final s = armedState();
    expect(s.optedIn, isFalse);
    expect(s.phase, equals(WakeListenPhase.off));
    expect(s.phaseLabel, contains('off'));
    s.loadPersisted(true); // reboot restores the toggle, not capture
    expect(s.optedIn, isTrue);
    expect(s.phase, equals(WakeListenPhase.off));
    expect(s.releaseCalls, equals(0));
  });

  test('ON starts one listener when prerequisites pass', () async {
    var starts = 0;
    final s = await armedForListen(onStart: () async {
      starts++;
      return null;
    });
    expect(await s.startListening(), isTrue);
    expect(s.phase, equals(WakeListenPhase.listening));
    expect(s.phaseLabel, contains('listening'));
    expect(starts, equals(1));
  });

  test('repeated starts do not duplicate listeners', () async {
    var starts = 0;
    final s = await armedForListen(onStart: () async {
      starts++;
      return null;
    });
    expect(await s.startListening(), isTrue);
    expect(await s.startListening(), isTrue);
    expect(await s.startListening(), isTrue);
    expect(starts, equals(1));
  });

  test('OFF stops capture and confirms release', () async {
    var stops = 0;
    final s = await armedForListen(onStart: () async => null);
    await s.startListening();
    s.onRelease = () async => stops++;
    await s.setOptIn(false);
    expect(s.phase, equals(WakeListenPhase.off));
    expect(s.phaseLabel, contains('off'));
    expect(stops, equals(1));
  });

  test('revoked permission prevents startup with actionable error',
      () async {
    var starts = 0;
    final s = liveState(onStart: () async {
      starts++;
      return null;
    });
    await s.updatePermission(true);
    await s.setOptIn(true);
    await s.updatePermission(false);
    expect(await s.startListening(), isFalse);
    expect(s.phase, equals(WakeListenPhase.error));
    expect(s.listenError, contains('microphone permission'));
    expect(starts, equals(0)); // engine never touched
  });

  test('missing model is an honest error, never simulated listening',
      () async {
    final s = await armedForListen(
        onStart: () async => 'no wake-word model on this build');
    expect(await s.startListening(), isFalse);
    expect(s.phase, equals(WakeListenPhase.error));
    expect(s.phaseLabel, contains('error'));
    expect(s.listenError, contains('no wake-word model'));
  });

  test('disabling suppresses pending and late wake events', () async {
    final s = await armedForListen(onStart: () async => null);
    await s.startListening();
    final gen = s.listenGeneration;
    expect(s.noteWakeEvent(generation: gen), isTrue); // live event passes
    await s.setOptIn(false);
    expect(s.noteWakeEvent(generation: gen), isFalse); // late: dropped
    expect(s.noteWakeEvent(), isFalse); // phase off: dropped
  });

  test('cooldown prevents repeated wake events', () async {
    final s = await armedForListen(onStart: () async => null);
    s.wakeCooldown = const Duration(seconds: 30);
    await s.startListening();
    expect(s.noteWakeEvent(), isTrue);
    expect(s.noteWakeEvent(), isFalse); // within cooldown
  });

  test('pause/logout/revoke path parks the listener (forceStandby)',
      () async {
    var stops = 0;
    final s = await armedForListen(
        onStart: () async => null, onRelease: () async => stops++);
    await s.startListening();
    await s.forceStandby();
    expect(s.phase, equals(WakeListenPhase.off));
    expect(stops, equals(1));
    // Pref untouched: user may re-enable after resume/re-pair.
    expect(s.optedIn, isTrue);
    await s.forceStandby(); // idempotent, no extra release
    expect(stops, equals(1));
  });

  test('init errors release resources and park at error', () async {
    var stops = 0;
    final s = await armedForListen(
        onStart: () async {
          throw StateError('audio init failed');
        },
        onRelease: () async => stops++);
    expect(await s.startListening(), isFalse);
    expect(s.phase, equals(WakeListenPhase.error));
    expect(s.listenError, contains('audio init failed'));
    expect(stops, equals(1));
  });
});

group('wake model probe and start-error mapping', () {
  const channel = MethodChannel('zara/device');

  setUp(() {
    TestWidgetsFlutterBinding.ensureInitialized();
  });

  tearDown(() {
    TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null);
  });

  test('probe normalizes shapes; failure reads all-absent', () async {
    TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      expect(call.method, 'getWakeModelStatus');
      return <String, dynamic>{
        'model_info': true,
        'keyword_model': false,
        'mel_model': true,
      };
    });
    final m = await DeviceBridge().wakeModelStatus();
    expect(m['model_info'], isTrue);
    expect(m['keyword_model'], isFalse);
    expect(m['ready'], isFalse); // never upgraded client-side
    TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      throw PlatformException(code: 'x');
    });
    final down = await DeviceBridge().wakeModelStatus();
    expect(down['ready'], isFalse);
    expect(down.values.whereType<bool>().every((v) => !v), isTrue);
  });

  test('missing files name exactly what is absent', () {
    final e = describeWakeModelError({
      'model_info': false,
      'keyword_model': false,
      'mel_model': false,
      'ready': false,
    });
    expect(e, contains('model_info.json'));
    expect(e, contains('keyword model'));
    expect(e, contains('mel frontend'));
    expect(e, contains('WAKE_ONNX_SETUP.md'));
  });

  test('present files without engine stay an honest error', () {
    final e = describeWakeModelError({
      'model_info': true,
      'keyword_model': true,
      'mel_model': true,
      'ready': false,
    });
    expect(e, contains('no on-device wake engine'));
    expect(e, isNot(contains('listening')));
  });

  test('untrusted ready=true never starts capture', () {
    final e = describeWakeModelError({
      'model_info': true,
      'keyword_model': true,
      'mel_model': true,
      'ready': true,
    });
    expect(e, contains('not starting'));
  });

  test('host check error surfaces in phase without listening', () async {
    final s = armedState();
    await s.updatePermission(true);
    await s.setOptIn(true);
    s.onStartListening = () async => describeWakeModelError({
          'model_info': false,
          'keyword_model': false,
          'mel_model': false,
          'ready': false,
        });
    expect(await s.startListening(), isFalse);
    expect(s.phase, equals(WakeListenPhase.error));
    expect(s.listenError, contains('model_info.json'));
  });
});
}
