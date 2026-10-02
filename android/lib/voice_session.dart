// Android voice session — mirrors core VoiceStateMachine + Stage-8 budgets.
//
// Adds the two device states core lacks: paused_battery and offline.
// Turn limits are IDENTICAL to core (5 / 300 s / 60 s) and enforced on
// BOTH sides; the client never extends them. Approval holds surface as
// notifications and resolve ONLY through Core approve/deny endpoints.
// The LLM never touches mic, provider, permissions, or devices.
class AndroidVoiceState {
  static const idle = 'idle';
  static const listening = 'listening';
  static const transcribing = 'transcribing';
  static const thinking = 'thinking';
  static const executing = 'executing';
  static const speaking = 'speaking';
  static const interrupted = 'interrupted';
  static const pausedBattery = 'paused_battery';
  static const offline = 'offline';
  static const error = 'error';

  static const allowed = {
    idle: {listening, pausedBattery, offline},
    listening: {transcribing, idle, interrupted, pausedBattery, offline},
    transcribing: {thinking, error, idle, offline},
    thinking: {executing, speaking, idle, error, offline},
    executing: {speaking, idle, error, offline},
    speaking: {idle, interrupted, listening, error, offline},
    interrupted: {listening, idle},
    pausedBattery: {idle, listening},
    offline: {idle, listening},
    error: {idle, listening},
  };

  // 'offline' resumes through listening; the connection layer owns
  // reconnect, so voice has no separate reconnecting state.
  static const offlineTargets = {idle, listening};
}

typedef VoiceClock = DateTime Function();

class AndroidVoiceSession {
  final int maxTurns;
  final Duration maxDuration;
  final Duration idleTimeout;
  final VoiceClock clock;

  String state = AndroidVoiceState.idle;
  int turns = 0;
  DateTime? _started;
  DateTime? _lastActivity;
  String pendingApproval = '';
  String endReason = '';
  bool get ended => endReason.isNotEmpty;

  AndroidVoiceSession({
    this.maxTurns = 5,
    this.maxDuration = const Duration(seconds: 300),
    this.idleTimeout = const Duration(seconds: 60),
    VoiceClock? clock,
  }) : clock = clock ?? DateTime.now;

  void move(String to) {
    if (!(AndroidVoiceState.allowed[state]?.contains(to) ?? false)) {
      throw StateError('illegal android voice transition $state -> $to');
    }
    state = to;
  }

  void reset() {
    state = AndroidVoiceState.idle;
    turns = 0;
    _started = null;
    _lastActivity = null;
    pendingApproval = '';
    endReason = '';
  }

  /// Gate before a turn: limits, idle, battery, network. Returns null when
  /// the turn may proceed, else the end reason (and ends the session).
  String? gate({required bool batteryOk, required bool online}) {
    final now = clock();
    _started ??= now;
    if (ended) return endReason;
    if (turns >= maxTurns) return _end('turn-limit');
    if (now.difference(_started!).compareTo(maxDuration) > 0) {
      return _end('max-duration');
    }
    if (_lastActivity != null &&
        now.difference(_lastActivity!).compareTo(idleTimeout) > 0) {
      return _end('idle-timeout');
    }
    if (!batteryOk) {
      if (state != AndroidVoiceState.pausedBattery) {
        try {
          move(AndroidVoiceState.pausedBattery);
        } catch (_) {/* illegal from here: end instead */}
      }
      return _end('battery-paused');
    }
    if (!online) return _end('offline');
    return null;
  }

  String _end(String reason) {
    endReason = reason;
    return reason;
  }

  /// Called after a successful Core turn (reply received, TTS handed off).
  void recordTurn() {
    turns += 1;
    _lastActivity = clock();
    if (turns >= maxTurns) _end('turn-limit');
  }

  /// Core event payload for sync — voice state only, never audio bytes.
  Map<String, dynamic> syncEvent() => {
        'type': 'voice_state',
        'state': state,
        'turns': turns,
        'pending_approval': pendingApproval.isNotEmpty,
      };
}
