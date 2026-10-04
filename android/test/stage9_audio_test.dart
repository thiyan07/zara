import 'dart:typed_data';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/audio_io.dart';

void main() {
  test('mic status splits hardware/permission/signal honestly', () {
    final silent = MockMicCapture(); // silence ok-path below
    final voiced = MockMicCapture(scripted: Uint8List.fromList([1, 2, 3]));
    expect(voiced.status.hardware, isTrue);
    expect(voiced.status.permission, isTrue);
    expect(voiced.status.signal, equals('detected'));
    expect(silent.status.signal, equals('silence'));
    final denied = MockMicCapture(denied: true);
    expect(denied.status.permission, isFalse);
    expect(() => denied.capture(), throwsA(isA<AudioException>()));
  });

  test('capture bounds enforced, cancel honored', () async {
    final m = MockMicCapture(scripted: Uint8List.fromList([1]));
    expect(() => m.capture(seconds: 31),
        throwsA(predicate((e) => e is AudioException && e.kind == 'failed')));
    final c = CancelFlag()..set();
    expect(() => m.capture(cancel: c),
        throwsA(predicate((e) => e is AudioException && e.kind == 'cancelled')));
    final out = await m.capture(seconds: 5);
    expect(out, equals(Uint8List.fromList([1])));
    expect(m.captures, equals(1));
    await m.stop();
    expect(m.stopped, isTrue);
  });

  test('speaker plays, cancels mid-play, stops cleanly (no zombie)', () async {
    final s = MockSpeakerOutput();
    await s.play(Uint8List.fromList([1, 2]));
    expect(s.plays, equals(1));
    expect(s.status.playing, isFalse);
    final c = CancelFlag();
    Future<void> run() => s.play(Uint8List.fromList([1, 2, 3]), cancel: c);
    final f = run();
    c.set();
    await expectLater(
        f, throwsA(predicate((e) => e is AudioException && e.kind == 'cancelled')));
    expect(s.status.playing, isFalse); // released despite cancel
    await s.play(Uint8List.fromList([9]));
    await s.stop();
    expect(s.stops, equals(1));
    expect(() => s.play(Uint8List(0)), throwsA(isA<AudioException>()));
  });

  test('audio bounds are sane', () {
    expect(AudioBounds.maxSeconds, equals(30));
    expect(AudioBounds.sampleRate, equals(16000));
  });

  test('wav peak: silence null, tone measured, garbage null', () {
    // 44-byte header + 4 silent samples
    final silent = Uint8List(44 + 8);
    silent[0] = 0x52;
    silent[1] = 0x49;
    expect(wavPeakDbfs(silent), isNull);
    expect(wavPeakDbfs(Uint8List(10)), isNull); // too short
    expect(wavPeakDbfs(Uint8List.fromList(List.filled(60, 7))), isNull);
    // full-scale-ish tone: peak 16384 -> -6.02 dBFS
    final tone = Uint8List(44 + 4);
    tone[0] = 0x52;
    tone[1] = 0x49;
    tone[44] = 0x00;
    tone[45] = 0x40; // 0x4000 = 16384
    final db = wavPeakDbfs(tone);
    expect(db, isNotNull);
    expect((db! + 6.02).abs(), lessThan(0.05));
  });
}
