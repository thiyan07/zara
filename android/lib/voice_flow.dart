// Voice turn glue: capture -> turn -> transcript -> reply -> TTS playback.
//
// Calls ONLY existing client/bridge methods end-to-end (CoreClient.
// voiceTurn/tts/interruptVoice, DeviceBridge audioCapture/audioPlay/
// audioPlayStop/audioStop). No always-on listening, no new permissions:
// capture is user-initiated through the existing mic flow.
//
// Wiring owned here:
//   * transcript from the turn is forwarded to [onTranscript] (the UI
//     submits it to the conversation view — POST /v1/talk);
//   * reply audio is routed to playback with a generation guard so a
//     second reply cancels the first (no TTS overlap, never stacked);
//   * [interrupt] propagates to capture stop + playback stop + the
//     server-side voice interrupt (existing stop, all three legs).
import 'dart:typed_data';

/// A completed Core voice turn (transcript + reply + STT provenance).
class VoiceTurnResult {
  final String transcript;
  final String reply;
  final String status;
  final String stt;
  const VoiceTurnResult({
    this.transcript = '',
    this.reply = '',
    this.status = '',
    this.stt = '',
  });

  /// Parses POST /v1/agent/voice/turn output
  /// ({transcript,reply,status,stt_provider,stt_model,...}).
  /// Missing keys read as empty, never throw. Pure for testing.
  static VoiceTurnResult fromTurn(Map<String, dynamic> out) {
    final fb = out['stt_fallback_used'] == true
        ? ' (fallback:${out['stt_fallback_reason']})'
        : '';
    return VoiceTurnResult(
      transcript: '${out['transcript'] ?? ''}',
      reply: '${out['reply'] ?? ''}',
      status: '${out['status'] ?? ''}',
      stt: 'stt: ${out['stt_provider'] ?? '?'}'
          ' ${out['stt_model'] ?? ''}$fb',
    );
  }
}

class VoiceFlowException implements Exception {
  final String message;
  const VoiceFlowException(this.message);
  @override
  String toString() => 'VoiceFlowException: $message';
}

/// Owns one voice turn end-to-end over injected capture/turn/tts/play
/// functions (production passes the bridge + client methods; tests pass
/// fakes — the flutter_tester cannot hold audio devices or sockets).
class VoiceFlowController {
  final Future<Uint8List?> Function() capture;
  final Future<Map<String, dynamic>> Function(Uint8List wav) turn;
  final Future<Uint8List> Function(String text) fetchTts;
  final Future<void> Function(Uint8List wav) play;
  final Future<void> Function() stopCapture;
  final Future<void> Function() stopPlayback;
  final Future<void> Function() serverInterrupt;

  /// Transcript sink: the UI submits this to the conversation view
  /// (POST /v1/talk). Never blocks the turn on failure.
  final void Function(String transcript)? onTranscript;

  int _playGeneration = 0;
  bool busy = false;
  Uint8List? lastCapture;

  VoiceFlowController({
    required this.capture,
    required this.turn,
    required this.fetchTts,
    required this.play,
    required this.stopCapture,
    required this.stopPlayback,
    required this.serverInterrupt,
    this.onTranscript,
  });

  /// Generation guard for playback overlap. Tests assert on this.
  int get playGeneration => _playGeneration;

  /// Full turn: bounded capture -> Core turn -> transcript sink ->
  /// reply routed to TTS playback. Empty capture is an explicit
  /// [VoiceFlowException], never a silent no-op.
  Future<VoiceTurnResult> runTurn() async {
    if (busy) throw const VoiceFlowException('voice turn already running');
    busy = true;
    try {
      final wav = await capture();
      lastCapture = wav;
      if (wav == null || wav.isEmpty) {
        throw const VoiceFlowException('capture returned no audio');
      }
      final result = VoiceTurnResult.fromTurn(await turn(wav));
      if (result.transcript.isNotEmpty) {
        try {
          onTranscript?.call(result.transcript);
        } catch (_) {/* sink never breaks the turn */}
      }
      if (result.reply.isNotEmpty) {
        await speak(result.reply);
      }
      return result;
    } finally {
      busy = false;
    }
  }

  /// Route one reply through TTS playback. A newer [speak] (or
  /// [interrupt]) invalidates an older one: prior playback is stopped
  /// first, and audio fetched for a superseded generation is dropped
  /// instead of played — second reply cancels first, never stacked.
  Future<void> speak(String text) async {
    final gen = ++_playGeneration;
    try {
      await stopPlayback();
    } catch (_) {/* best-effort */}
    if (gen != _playGeneration) return; // superseded while stopping
    final audio = await fetchTts(text);
    if (gen != _playGeneration) return; // newer reply won: drop stale audio
    await play(audio);
  }

  /// Interrupt: invalidate in-flight playback, stop capture + speaker,
  /// then tell Core (existing stop legs). Best-effort each, never throws.
  Future<void> interrupt() async {
    _playGeneration++; // stale TTS audio must never play after this
    try {
      await stopCapture();
    } catch (_) {}
    try {
      await stopPlayback();
    } catch (_) {}
    try {
      await serverInterrupt();
    } catch (_) {}
  }
}
