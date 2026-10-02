import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/voice_session.dart';
import 'package:zara_android/wake.dart';
import 'package:zara_android/voice.dart' show wakePhrase;

void main() {
  test('wake phrase is still exactly Hey Zara', () {
    expect(wakePhrase, equals('Hey Zara'));
  });

  test('wake fires only on voiced exact-phrase match', () {
    final w = WakeController();
    expect(w.evaluate(vadVoice: true, transcript: 'Hey Zara').detected, isTrue);
    expect(
        w.evaluate(vadVoice: true, transcript: 'hey zara').detected, isFalse);
    expect(w.evaluate(vadVoice: false, transcript: 'Hey Zara').detected,
        isFalse); // gate first: no voice, no trigger
    expect(w.evaluate(vadVoice: true, transcript: 'hello there').detected,
        isFalse);
    expect(w.evaluations, equals(4));
    expect(w.detections, equals(1));
  });

  test('wake pauses on critical battery, never executes anything', () {
    final w = WakeController();
    final out = w.evaluate(
        vadVoice: true, transcript: 'Hey Zara', batteryPct: 5, charging: false);
    expect(out, equals(const TypeMatcher<WakeEvaluation>()));
    expect(out.detected, isFalse);
    expect(out.reason, equals('battery-paused'));
    // WakeEvaluation has no tool/policy knobs: state request only.
    expect(out.toString(), isNot(contains('tool')));
  });

  test('android voice session enforces limits (turns/idle/duration)', () {
    var now = DateTime(2026, 1, 1);
    final s = AndroidVoiceSession(clock: () => now);
    s.move('listening');
    expect(s.gate(batteryOk: true, online: true), isNull);
    for (var i = 0; i < 5; i++) {
      s.recordTurn();
    }
    expect(s.turns, equals(5));
    expect(s.gate(batteryOk: true, online: true), equals('turn-limit'));
    expect(s.ended, isTrue);
  });

  test('idle timeout and offline end sessions truthfully', () {
    var now = DateTime(2026, 1, 1);
    final s = AndroidVoiceSession(clock: () => now);
    s.move('listening');
    s.recordTurn();
    now = now.add(const Duration(seconds: 61));
    expect(s.gate(batteryOk: true, online: true), equals('idle-timeout'));

    now = DateTime(2026, 1, 1);
    final s2 = AndroidVoiceSession(clock: () => now);
    s2.move('listening');
    expect(s2.gate(batteryOk: true, online: false), equals('offline'));
  });

  test('battery pause moves to paused_battery; illegal jumps throw', () {
    var now = DateTime(2026, 1, 1);
    final s = AndroidVoiceSession(clock: () => now);
    s.move('listening');
    expect(s.gate(batteryOk: false, online: true), equals('battery-paused'));
    expect(s.state, equals('paused_battery'));
    final bad = AndroidVoiceSession();
    expect(() => bad.move('speaking'), throwsStateError);
  });

  test('sync event carries state only, never audio', () {
    final s = AndroidVoiceSession();
    final e = s.syncEvent();
    expect(e['state'], equals('idle'));
    expect(e.keys.any((k) => k.contains('audio') || k.contains('wav')),
        isFalse);
  });
}
