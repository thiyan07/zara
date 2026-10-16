// Voice glue: transcript->submit wiring, interrupt propagation, and
// no-TTS-overlap (second reply cancels first playback). Controller-level
// with injected fakes — no audio devices or sockets in widget tests.
import 'dart:async';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/voice_flow.dart';

class _VoiceFake {
  Uint8List? captureOut = Uint8List.fromList([1, 2, 3]);
  Map<String, dynamic> turnOut = const {
    'transcript': 'hello',
    'reply': 'hi there',
    'status': 'ok',
    'stt_provider': 'mock',
    'stt_model': 'm',
  };
  final List<Uint8List> plays = [];
  final List<String> ttsTexts = [];
  int stopCaptures = 0;
  int stopPlaybacks = 0;
  int serverInterrupts = 0;
  final List<String> transcripts = [];
  Completer<Uint8List>? ttsGate;
  String? gatedText; // when set, only this text waits on [ttsGate]

  VoiceFlowController controller() => VoiceFlowController(
        capture: () async => captureOut,
        turn: (_) async => turnOut,
        fetchTts: (t) async {
          ttsTexts.add(t);
          final g = ttsGate;
          if (g != null && t == gatedText) return g.future;
          return Uint8List.fromList(t.codeUnits);
        },
        play: (w) async {
          plays.add(w);
        },
        stopCapture: () async {
          stopCaptures += 1;
        },
        stopPlayback: () async {
          stopPlaybacks += 1;
        },
        serverInterrupt: () async {
          serverInterrupts += 1;
        },
        onTranscript: transcripts.add,
      );
}

void main() {
  test('turn result parses transcript/reply/stt incl fallback', () {
    final r = VoiceTurnResult.fromTurn({
      'transcript': 'hi',
      'reply': 'hello',
      'status': 'ok',
      'stt_provider': 'p',
      'stt_model': 'm',
      'stt_fallback_used': true,
      'stt_fallback_reason': 'noisy',
    });
    expect(r.transcript, equals('hi'));
    expect(r.reply, equals('hello'));
    expect(r.stt, contains('fallback:noisy'));
    expect(VoiceTurnResult.fromTurn({}).transcript, isEmpty);
  });

  test('runTurn wires capture->turn->transcript->tts->play end to end',
      () async {
    final f = _VoiceFake();
    final c = f.controller();
    final res = await c.runTurn();
    expect(res.transcript, equals('hello'));
    expect(res.reply, equals('hi there'));
    // Transcript submitted to the conversation sink.
    expect(f.transcripts, equals(['hello']));
    // Reply routed to TTS playback.
    expect(f.ttsTexts, equals(['hi there']));
    expect(f.plays.length, equals(1));
    expect(c.busy, isFalse);
  });

  test('empty capture is explicit, never silent', () async {
    final f = _VoiceFake()..captureOut = Uint8List(0);
    await expectLater(
        f.controller().runTurn(), throwsA(isA<VoiceFlowException>()));
  });

  test('concurrent turn is rejected explicitly', () async {
    final f = _VoiceFake();
    final c = f.controller();
    final gate = Completer<Map<String, dynamic>>();
    final c2 = VoiceFlowController(
      capture: () async => Uint8List.fromList([1]),
      turn: (_) => gate.future,
      fetchTts: (_) async => Uint8List(0),
      play: (_) async {},
      stopCapture: () async {},
      stopPlayback: () async {},
      serverInterrupt: () async {},
    );
    final first = c2.runTurn();
    await expectLater(c2.runTurn(), throwsA(isA<VoiceFlowException>()));
    gate.complete(f.turnOut);
    await first;
    expect(c.busy, isFalse);
  });

  test('no TTS overlap: second reply cancels first playback', () async {
    final f = _VoiceFake()
      ..ttsGate = Completer<Uint8List>()
      ..gatedText = 'first reply';
    final c = f.controller();
    final first = c.speak('first reply');
    // Let the first speak reach the gated fetch.
    await Future<void>.delayed(Duration.zero);
    expect(f.ttsTexts, equals(['first reply']));
    final second = c.speak('second reply');
    await second; // second fetch is immediate: stops first, plays second
    // Prior playback stopped for the newer reply.
    expect(f.stopPlaybacks, equals(2));
    // Stale audio for the first reply is dropped, never played.
    f.ttsGate!.complete(Uint8List.fromList([9, 9]));
    await first;
    expect(f.ttsTexts, equals(['first reply', 'second reply']));
    expect(f.plays.length, equals(1));
    expect(String.fromCharCodes(f.plays.single), equals('second reply'));
  });

  test('interrupt propagates to capture + playback + server', () async {
    final f = _VoiceFake();
    final c = f.controller();
    await c.interrupt();
    expect(f.stopCaptures, equals(1));
    expect(f.stopPlaybacks, equals(1));
    expect(f.serverInterrupts, equals(1));
  });

  test('interrupt invalidates in-flight TTS audio', () async {
    final f = _VoiceFake()
      ..ttsGate = Completer<Uint8List>()
      ..gatedText = 'stale';
    final c = f.controller();
    final speaking = c.speak('stale');
    await Future<void>.delayed(Duration.zero);
    await c.interrupt();
    f.ttsGate!.complete(Uint8List.fromList([1]));
    await speaking;
    expect(f.plays, isEmpty); // stale audio dropped after interrupt
    expect(f.serverInterrupts, equals(1));
  });

  group('platform-leg bounds (Vivo audioPlay wedge)', () {
    test('wedged capture surfaces VoiceFlowException, never hangs', () async {
      final c = VoiceFlowController(
        capture: () => Completer<Uint8List?>().future, // never replies
        turn: (_) async => <String, dynamic>{},
        fetchTts: (_) async => Uint8List(0),
        play: (_) async {},
        stopCapture: () async {},
        stopPlayback: () async {},
        serverInterrupt: () async {},
        captureTimeout: const Duration(milliseconds: 50),
      );
      await expectLater(c.runTurn(),
          throwsA(isA<VoiceFlowException>().having((e) => '$e', 'msg',
              contains('capture timed out'))));
      expect(c.busy, isFalse); // turn released, UI can continue
    });

    test('wedged playback surfaces VoiceFlowException, never hangs',
        () async {
      final c = VoiceFlowController(
        capture: () async => Uint8List.fromList([1]),
        turn: (_) async => <String, dynamic>{},
        fetchTts: (_) async => Uint8List.fromList([2]),
        play: (_) => Completer<void>().future, // never replies
        stopCapture: () async {},
        stopPlayback: () async {},
        serverInterrupt: () async {},
        playTimeout: const Duration(milliseconds: 50),
      );
      await expectLater(c.speak('hi'),
          throwsA(isA<VoiceFlowException>().having((e) => '$e', 'msg',
              contains('playback timed out'))));
    });

    test('wedged stopPlayback does not block speak past 10 s', () async {
      var played = false;
      final c = VoiceFlowController(
        capture: () async => Uint8List.fromList([1]),
        turn: (_) async => <String, dynamic>{},
        fetchTts: (_) async => Uint8List.fromList([2]),
        play: (_) async {
          played = true;
        },
        stopCapture: () async {},
        stopPlayback: () => Completer<void>().future, // wedged stop
        serverInterrupt: () async {},
      );
      await c.speak('hi').timeout(const Duration(seconds: 15));
      expect(played, isTrue);
    });
  });
}
