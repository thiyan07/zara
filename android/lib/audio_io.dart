// Android audio I/O abstractions — capability/status split from signal.
//
// Distinguishes: hardware exists / permission granted / stream opened /
// signal detected. Opening a stream NEVER implies voice was detected.
// Bounded capture (<=30 s), cancellation-safe, no persistence by default.
// Pure Dart interfaces + mocks; the native path lives in DeviceBridge.
import 'dart:typed_data';

class AudioBounds {
  static const maxSeconds = 30;
  static const sampleRate = 16000;
  static const channels = 1;
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
