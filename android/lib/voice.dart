/// Zara voice state + wake configuration (pure Dart, unit-tested).
///
/// Mirrors the core voice state machine. Audio capture/STT/TTS run behind
/// the native bridge; anything unimplemented reports supported=false.
/// Wake phrase is EXACTLY "Hey Zara" — never renamed.
class VoiceState {
  static const idle = 'idle';
  static const listening = 'listening';
  static const transcribing = 'transcribing';
  static const thinking = 'thinking';
  static const executing = 'executing';
  static const speaking = 'speaking';
  static const interrupted = 'interrupted';
  static const error = 'error';

  static const allowed = {
    idle: {listening},
    listening: {transcribing, idle, interrupted},
    transcribing: {thinking, error, idle},
    thinking: {executing, speaking, idle, error},
    executing: {speaking, idle, error},
    speaking: {idle, interrupted, listening, error},
    interrupted: {listening, idle},
    error: {idle, listening},
  };
}

/// Exact wake phrase. A second constant would risk drift — there is one.
const String wakePhrase = 'Hey Zara';

/// Battery-aware wake policy (configurable thresholds, no magic constants).
class WakeBatteryPolicy {
  final double pauseBelowPct;
  final double resumeAbovePct;
  final bool pauseOnPowerSave;
  const WakeBatteryPolicy({
    this.pauseBelowPct = 15.0,
    this.resumeAbovePct = 25.0,
    this.pauseOnPowerSave = true,
  });

  /// Returns 'listening' or the pause reason.
  String evaluate(
      {required double? batteryPct,
      required bool charging,
      required bool powerSave}) {
    if (batteryPct == null) return 'listening';
    if ((batteryPct < pauseBelowPct && !charging) ||
        (powerSave && pauseOnPowerSave)) {
      return 'paused-low-battery';
    }
    return 'listening';
  }
}

/// Minimal client-side voice session: one owner for UI state.
class VoiceSession {
  String state = VoiceState.idle;
  void move(String to) {
    if (!(VoiceState.allowed[state]?.contains(to) ?? false)) {
      throw StateError('illegal voice transition $state -> $to');
    }
    state = to;
  }

  void reset() => state = VoiceState.idle;
}
