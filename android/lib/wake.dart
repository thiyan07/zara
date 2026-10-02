// Android wake controller — EXACT phrase "Hey Zara", VAD-gated.
//
// Wake detection ONLY requests a state change (idle/paused -> listening).
// It cannot execute tools, approve, mutate memory, or override the
// governor — there is simply no code path from here to any of those.
// Heavy always-on models are refused by construction: evaluation is a
// transcript match behind a voice-activity gate. DSP integration, when
// hardware allows, plugs in as a [WakeSignal] source.
import 'voice.dart' show wakePhrase, WakeBatteryPolicy;

/// A hardware/DSP wake signal source (not yet present on test hardware).
/// Returns the matched phrase or null. Null DSP => always null => the
/// VAD-gated transcript path below is the only trigger.
typedef WakeSignal = String? Function();

class WakeEvaluation {
  final bool detected;
  final String reason; // 'detected' | 'no-voice' | 'no-match' | 'battery-paused'
  const WakeEvaluation(this.detected, this.reason);
}

class WakeController {
  final WakeBatteryPolicy batteryPolicy;
  final WakeSignal dspSignal;
  int evaluations = 0;
  int detections = 0;

  WakeController({
    this.batteryPolicy = const WakeBatteryPolicy(),
    WakeSignal? dsp,
  }) : dspSignal = dsp ?? (() => null);

  /// Pure evaluation: no side effects, returns a state-change request.
  WakeEvaluation evaluate({
    required bool vadVoice,
    required String transcript,
    double? batteryPct,
    bool charging = false,
    bool powerSave = false,
  }) {
    evaluations += 1;
    final gate = batteryPolicy.evaluate(
        batteryPct: batteryPct, charging: charging, powerSave: powerSave);
    if (gate != 'listening') return const WakeEvaluation(false, 'battery-paused');
    final dsp = dspSignal();
    if (dsp == wakePhrase || (vadVoice && transcript.contains(wakePhrase))) {
      detections += 1;
      return const WakeEvaluation(true, 'detected');
    }
    if (!vadVoice && dsp == null) return const WakeEvaluation(false, 'no-voice');
    return const WakeEvaluation(false, 'no-match');
  }
}
