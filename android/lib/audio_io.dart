// Android audio I/O abstractions — capability/status split from signal.
//
// Distinguishes: hardware exists / permission granted / stream opened /
// signal detected. Opening a stream NEVER implies voice was detected.
// Bounded capture (<=30 s), cancellation-safe, no persistence by default.
// Pure Dart interfaces + mocks; the native path lives in DeviceBridge.
import 'dart:math' as math;
import 'dart:typed_data';

class AudioBounds {
  static const maxSeconds = 30;
  static const sampleRate = 16000;
  static const channels = 1;
}

/// Peak level of 16-bit WAV bytes in dBFS, or null for silence/invalid.
/// Evidence for "microphone heard something" — never faked: digital zeros
/// return null, and callers must report null as silence.
double? wavPeakDbfs(Uint8List wav) {
  if (wav.length < 44) return null;
  if (wav[0] != 0x52 || wav[1] != 0x49) return null; // 'RI'
  var peak = 0;
  final bodyLen = wav.length - 44;
  for (var i = 0; i + 1 < bodyLen; i += 2) {
    var v = wav[44 + i] | (wav[44 + i + 1] << 8);
    if (v >= 0x8000) v -= 0x10000;
    final a = v.abs();
    if (a > peak) peak = a;
  }
  if (peak == 0) return null;
  return 20 * (math.log(peak / 32768.0) / math.ln10);
}

class MicStatus {
  final bool hardware;
  final bool permission;
  final bool streamOpen;
  final String signal; // 'unknown' | 'detected' | 'silence'
  const MicStatus({
    this.hardware = false,
    this.permission = false,
    this.streamOpen = false,
    this.signal = 'unknown',
  });
}

class SpeakerStatus {
  final bool hardware;
  final bool initialized;
  final bool playing;
  const SpeakerStatus({
    this.hardware = false,
    this.initialized = false,
    this.playing = false,
  });
}

class AudioException implements Exception {
  final String kind; // 'denied' | 'unavailable' | 'cancelled' | 'failed'
  final String message;
  const AudioException(this.kind, this.message);
  @override
  String toString() => 'AudioException($kind): $message';
}

class CancelFlag {
  bool _set = false;
  void set() => _set = true;
  bool get isSet => _set;
}

abstract class MicCapture {
  MicStatus get status;
  Future<Uint8List> capture({double seconds = 5.0, CancelFlag? cancel});
  Future<void> stop();
}

/// Scripted mock: returns fixed WAV (or silence), honors cancel, counts.
class MockMicCapture implements MicCapture {
  final Uint8List scripted;
  final bool denied;
  int captures = 0;
  bool stopped = false;
  MockMicCapture({Uint8List? scripted, this.denied = false})
      : scripted = scripted ?? Uint8List(0);

  @override
  MicStatus get status => MicStatus(
      hardware: true, permission: !denied, streamOpen: false,
      signal: scripted.isEmpty ? 'silence' : 'detected');

  @override
  Future<Uint8List> capture({double seconds = 5.0, CancelFlag? cancel}) async {
    if (denied) throw const AudioException('denied', 'microphone denied');
    if (seconds <= 0 || seconds > AudioBounds.maxSeconds) {
      throw const AudioException('failed', 'capture out of bounds');
    }
    if (cancel?.isSet ?? false) {
      throw const AudioException('cancelled', 'capture cancelled');
    }
    captures += 1; // successful captures only
    return scripted;
  }

  @override
  Future<void> stop() async => stopped = true;
}

abstract class SpeakerOutput {
  SpeakerStatus get status;
  Future<void> play(Uint8List wav, {CancelFlag? cancel});
  Future<void> stop();
}

/// Mock speaker: records plays/stops, honors cancel mid-play, no zombies.
class MockSpeakerOutput implements SpeakerOutput {
  int plays = 0;
  int stops = 0;
  bool _playing = false;
  @override
  SpeakerStatus get status =>
      SpeakerStatus(hardware: true, initialized: true, playing: _playing);

  @override
  Future<void> play(Uint8List wav, {CancelFlag? cancel}) async {
    if (wav.isEmpty) throw const AudioException('failed', 'empty audio');
    plays += 1;
    _playing = true;
    try {
      for (var i = 0; i < 4; i++) {
        await Future<void>.delayed(const Duration(milliseconds: 1));
        if (cancel?.isSet ?? false) {
          throw const AudioException('cancelled', 'playback cancelled');
        }
      }
    } finally {
      _playing = false;
    }
  }

  @override
  Future<void> stop() async {
    stops += 1;
    _playing = false;
  }
}
