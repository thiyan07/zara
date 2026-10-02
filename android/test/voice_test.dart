import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/voice.dart';

void main() {
  test('wake phrase is exactly Hey Zara', () {
    expect(wakePhrase, equals('Hey Zara'));
  });

  test('voice state machine enforces legal transitions', () {
    final s = VoiceSession();
    s.move(VoiceState.listening);
    s.move(VoiceState.transcribing);
    s.move(VoiceState.thinking);
    s.move(VoiceState.speaking);
    s.move(VoiceState.idle);
    expect(s.state, equals(VoiceState.idle));
    expect(() => s.move(VoiceState.speaking), throwsStateError);
  });

  test('barge-in path is legal: speaking -> interrupted -> listening', () {
    final s = VoiceSession()..move(VoiceState.listening)
      ..move(VoiceState.transcribing)
      ..move(VoiceState.thinking)
      ..move(VoiceState.speaking)
      ..move(VoiceState.interrupted)
      ..move(VoiceState.listening);
    expect(s.state, equals(VoiceState.listening));
  });

  test('battery policy pauses wake at critical, resumes on charge headroom', () {
    const policy = WakeBatteryPolicy();
    expect(
        policy.evaluate(
            batteryPct: 10, charging: false, powerSave: false),
        equals('paused-low-battery'));
    expect(
        policy.evaluate(
            batteryPct: 80, charging: false, powerSave: false),
        equals('listening'));
    expect(
        policy.evaluate(
            batteryPct: 50, charging: false, powerSave: true),
        equals('paused-low-battery'));
  });
}
