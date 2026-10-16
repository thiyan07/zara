// Wake-word opt-in foundation — honest, push-to-talk only.
//
// Always-on listening is OUT OF SCOPE on this build: the device bridge
// reports wake_word_engine=false, core's WakeWordEngine/MockWakeEngine are
// deterministic stand-ins (physically_validated:false), and
// VADSpotterBackend is a single-shot spotter (always_on_dsp:false).
// This file adds NO background microphone services, permissions, timers,
// polling loops, FCM, or notification listeners. Refresh is manual only:
// the host pushes its existing snapshots in (updateFlags/updatePermission/
// updateBattery) or the user taps "Refresh wake state". There is no
// Timer.periodic, no Future.delayed loop, no background isolate here.
//
// Opt-in is in-memory only (default OFF): android has no existing prefs
// mechanism for UI toggles — SecureStore holds device keys only, and
// provider_setup.dart explicitly documents "no shared prefs writes".
// No new storage dependency is added.
import 'package:flutter/material.dart';
import 'voice.dart' show WakeBatteryPolicy;

/// Visible wake opt-in states. Every gate has its own state; nothing is
/// silent and nothing claims to listen when it cannot.
enum WakeOptInStatus {
  disabled,
  enabled,
  pausedBattery,
  permissionRequired,
  unsupported,
}

/// Detector phases for the persisted ON/OFF setting. `off` is the only
/// resting state without capture; `starting` covers async init;
/// `listening` means exactly one active listener was confirmed;
/// `error` carries an actionable message and holds no resources.
enum WakeListenPhase { off, starting, listening, error }

/// Pure opt-in state (no Flutter, no timers). The host wires:
///   * [onRelease] to the existing stop path (VoiceFlowController.interrupt)
///   * [voiceFlags] from DeviceBridge.voiceSupport()
///   * [engineAvailability] from GET /v1/wake `availability` (optional;
///     unknown until queried — never assumed).
class WakeOptInState extends ChangeNotifier {
  bool _optedIn = false;
  bool micGranted = false;
  double? batteryPct;
  bool charging = false;
  bool powerSave = false;
  Map<String, dynamic> voiceFlags = {};
  Map<String, dynamic>? engineAvailability;
  final WakeBatteryPolicy batteryPolicy;

  /// Existing stop path invoked on disable and on mid-flow revocation.
  /// Exceptions are swallowed: release must never crash the UI.
  Future<void> Function()? onRelease;

  /// Detector start attempt, injected by the host. Returns null when
  /// exactly one listener is confirmed live, or an actionable error
  /// string (permission/model/hardware) when it cannot start. Null
  /// host (tests default) means no engine: start always reports the
  /// missing-engine error instead of pretending to listen.
  Future<String?> Function()? onStartListening;

  /// Count of release invocations (tests assert disable really releases).
  int releaseCalls = 0;

  /// Detector phase + last error. Phase is `off` unless a start was
  /// confirmed; late callbacks carry a stale generation and are dropped.
  WakeListenPhase phase = WakeListenPhase.off;
  String listenError = '';
  int _listenGeneration = 0;
  int get listenGeneration => _listenGeneration;

  /// True when nothing is held (fresh, parked, or just released).
  /// Makes standby idempotent: repeated pause/logout calls never
  /// stack releases. Cleared whenever gating re-arms.
  bool _stoodBy = true;

  /// Duplicate-event suppression: minimum gap between accepted wake
  /// events. Conservative default; host may override per model card.
  Duration wakeCooldown = const Duration(seconds: 2);
  DateTime? _lastWakeFire;

  WakeOptInState({
    this.batteryPolicy = const WakeBatteryPolicy(),
    this.onRelease,
    Map<String, dynamic>? initialFlags,
  }) {
    if (initialFlags != null) {
      voiceFlags = Map<String, dynamic>.from(initialFlags);
    }
  }

  /// Explicit opt-in. In-memory only, default OFF.
  bool get optedIn => _optedIn;

  /// Battery gate per the shared [WakeBatteryPolicy] thresholds
  /// ('listening' or the pause reason).
  String get batteryGate => batteryPolicy.evaluate(
      batteryPct: batteryPct, charging: charging, powerSave: powerSave);

  /// Non-null when the device cannot do wake at all, per the
  /// voiceSupport flags (never a hardcoded device list).
  String? get unsupportedReason {
    if (voiceFlags.isEmpty) return 'voice support not yet checked';
    if (voiceFlags['supported'] != true) {
      return 'voice support unavailable (supported=false)';
    }
    if (voiceFlags['microphone_hardware'] == false) {
      return 'no microphone hardware';
    }
    return null;
  }

  bool get deviceSupported => unsupportedReason == null;

  /// Enable requires opt-in AND mic permission AND battery-ok AND a
  /// supported device. Order matters: unsupported > opt-in > permission
  /// > battery, so each failure surfaces its own explicit state.
  WakeOptInStatus get status {
    if (!deviceSupported) return WakeOptInStatus.unsupported;
    if (!_optedIn) return WakeOptInStatus.disabled;
    if (!micGranted) return WakeOptInStatus.permissionRequired;
    if (batteryGate != 'listening') return WakeOptInStatus.pausedBattery;
    return WakeOptInStatus.enabled;
  }

  /// True only when fully gated on. There is no silent-listening state:
  /// anything less than [WakeOptInStatus.enabled] must not capture.
  bool get effectivelyArmed => status == WakeOptInStatus.enabled;

  String get statusLabel {
    switch (status) {
      case WakeOptInStatus.disabled:
        return 'wake: disabled (opt-in off)';
      case WakeOptInStatus.enabled:
        return 'wake: enabled (push-to-talk gated, no always-on loop)';
      case WakeOptInStatus.pausedBattery:
        return 'wake: paused ($batteryGate; resume ≥ ${batteryPolicy.resumeAbovePct}%)';
      case WakeOptInStatus.permissionRequired:
        return 'wake: permission required (mic not granted)';
      case WakeOptInStatus.unsupported:
        return 'wake: unsupported (${unsupportedReason ?? 'unknown'})';
    }
  }

  /// Honest availability caption, built ONLY from reported flags — never
  /// hardcoded marketing. Tests assert these technical tokens
  /// (wake_word_engine=…, push-to-talk, always-on … not available).
  String get caption {
    final engine = voiceFlags['wake_word_engine'];
    final signal = voiceFlags['microphone_signal'];
    final note = voiceFlags['note'];
    final dsp = engineAvailability?['always_on_dsp'];
    final phys = engineAvailability?['physically_validated'];
    final buf = StringBuffer('wake_word_engine=$engine');
    if (dsp != null) buf.write('; always_on_dsp=$dsp');
    if (phys != null) buf.write('; physically_validated=$phys');
    if (signal != null) buf.write('; microphone_signal=$signal');
    if (note is String && note.isNotEmpty) buf.write(' — $note');
    if (engine == true) {
      buf.write('; availability changed — revalidate on hardware '
          'before claiming always-on');
    } else {
      buf.write('; always-on listening is not available on this build '
          '— push-to-talk only');
    }
    return buf.toString();
  }

  /// User flips the opt-in switch. Turning OFF from armed releases
  /// resources via the existing stop path. The persisted preference is
  /// written by the HOST (it owns the bridge); this object never does
  /// I/O, so preference writes stay best-effort and testable.
  /// Release everything held (listener and/or gated resources) and park
  /// the phase at OFF without touching the persisted preference. Used
  /// by pause, logout, and revocation: the user may re-enable later,
  /// but nothing keeps running meanwhile. Idempotent: a second call
  /// with nothing held does nothing (no extra release, no repaint).
  Future<void> forceStandby() async {
    if (phase == WakeListenPhase.off &&
        (_stoodBy || !effectivelyArmed)) {
      return;
    }
    await stopListening();
  }

  Future<void> setOptIn(bool value) async {
    if (!value) {
      // Standby BEFORE flipping: it must still see the armed state to
      // release gated resources (then the OFF repaint below confirms).
      await forceStandby();
    } else {
      _stoodBy = false; // re-armed gating may hold resources again
    }
    _optedIn = value;
    notifyListeners();
  }

  /// Restores a persisted preference WITHOUT starting anything: no
  /// auto-listen at boot. The user re-enables listening explicitly in
  /// the foreground after gates re-validate.
  void loadPersisted(bool savedOptIn) {
    _optedIn = savedOptIn;
    phase = WakeListenPhase.off;
    listenError = '';
    _stoodBy = true;
    _listenGeneration++; // ancient callbacks from a past life are stale
    _lastWakeFire = null;
    notifyListeners();
  }

  /// Starts exactly one listener when all prerequisites pass. Repeated
  /// calls while starting/listening are no-ops (no duplicate mic
  /// sessions). Returns true only when listening was confirmed.
  Future<bool> startListening() async {
    if (phase == WakeListenPhase.starting ||
        phase == WakeListenPhase.listening) {
      return phase == WakeListenPhase.listening; // single-listener guard
    }
    if (!effectivelyArmed) {
      phase = WakeListenPhase.error;
      listenError = _prerequisiteError() ??
          'enable the opt-in with mic permission and battery-ok first';
      notifyListeners();
      return false;
    }
    final gen = ++_listenGeneration;
    phase = WakeListenPhase.starting;
    listenError = '';
    notifyListeners();
    String? err;
    if (onStartListening != null) {
      try {
        err = await onStartListening!();
      } catch (e) {
        err = '$e';
      }
    } else {
      // Null host (tests default) means no engine: report the missing
      // engine instead of pretending to listen.
      err = 'no wake engine on this build (model unavailable)';
    }
    if (gen != _listenGeneration) return false; // stopped while starting
    if (err == null) {
      phase = WakeListenPhase.listening;
      listenError = '';
      _stoodBy = false; // a live listener is now held
    } else {
      phase = WakeListenPhase.error;
      listenError = err;
      await _release();
      _stoodBy = true;
    }
    notifyListeners();
    return err == null;
  }

  /// Stops capture and inference promptly, releases audio resources,
  /// cancels retries (via the shared stop path), and invalidates queued
  /// wake events by bumping the generation. The OFF phase is shown only
  /// after release completes (confirmed stop).
  Future<void> stopListening() async {
    _listenGeneration++; // late callbacks from here on are stale
    if (phase == WakeListenPhase.off && _stoodBy) {
      listenError = '';
      return; // fully parked already: no work, no repaint storm
    }
    if (!_stoodBy) {
      await _release();
      _stoodBy = true;
    }
    phase = WakeListenPhase.off;
    listenError = '';
    notifyListeners();
  }

  /// Accepts a wake event only from the live generation while listening
  /// and outside the cooldown window. Late, duplicate, or post-disable
  /// events return false and must never open a session.
  bool noteWakeEvent({int? generation}) {
    if (generation != null && generation != _listenGeneration) return false;
    if (phase != WakeListenPhase.listening) return false;
    final now = DateTime.now();
    if (_lastWakeFire != null &&
        now.difference(_lastWakeFire!) < wakeCooldown) {
      return false;
    }
    _lastWakeFire = now;
    return true;
  }

  String? _prerequisiteError() {
    if (!deviceSupported) return unsupportedReason;
    if (!_optedIn) return 'wake opt-in is off';
    if (!micGranted) return 'microphone permission revoked or denied';
    if (batteryGate != 'listening') return 'paused: $batteryGate';
    return null;
  }

  /// Host pushes mic permission state (including mid-flow revocation).
  /// Revocation while armed stops everything: explicit
  /// permission-required state, no crash, no silent listening.
  Future<void> updatePermission(bool granted) async {
    if (!granted) {
      // Standby BEFORE flipping: same armed-visibility rule as setOptIn.
      await forceStandby(); // pref never overrides revocation
    } else {
      _stoodBy = false; // re-grant re-arms the gate
    }
    micGranted = granted;
    notifyListeners();
  }

  /// Host pushes its existing battery/telemetry snapshot.
  void updateBattery(
      {required double? pct,
      required bool charging,
      required bool powerSave}) {
    batteryPct = pct;
    this.charging = charging;
    this.powerSave = powerSave;
    notifyListeners();
  }

  /// Host pushes DeviceBridge.voiceSupport().
  void updateFlags(Map<String, dynamic> flags) {
    voiceFlags = Map<String, dynamic>.from(flags);
    notifyListeners();
  }

  /// Host pushes GET /v1/wake `availability` (null/empty = unknown).
  void updateEngineAvailability(Map<String, dynamic>? availability) {
    engineAvailability =
        availability == null ? null : Map<String, dynamic>.from(availability);
    notifyListeners();
  }

  /// Detector phase line for the Settings toggle area: Off, Starting,
  /// Listening, or Error with the actionable message. Never claims
  /// listening unless a start was confirmed.
  String get phaseLabel {
    switch (phase) {
      case WakeListenPhase.off:
        return 'detection: off';
      case WakeListenPhase.starting:
        return 'detection: starting…';
      case WakeListenPhase.listening:
        return 'detection: listening (foreground only)';
      case WakeListenPhase.error:
        return 'detection: error — ${listenError.isEmpty ? 'could not start' : listenError}';
    }
  }

  Future<void> _release() async {
    releaseCalls += 1;
    try {
      await onRelease?.call();
    } catch (_) {/* release is best-effort, never a crash */}
  }
}

/// Maps a wake-model status probe to the actionable start error shown
/// in Settings. Pure for testing. Never returns null (there is always
/// something honest to say); never claims readiness.
String describeWakeModelError(Map<String, dynamic> status) {
  if (status['ready'] == true) {
    // No engine ships, so a ready=true probe is untrusted: say so
    // instead of starting capture on an unverified claim.
    return 'wake engine reported ready but this build has no verified '
        'wake engine — not starting (see WAKE_ONNX_SETUP.md)';
  }
  final missing = <String>[];
  if (status['model_info'] != true) missing.add('model_info.json');
  if (status['keyword_model'] != true) missing.add('keyword model');
  if (status['mel_model'] != true) missing.add('mel frontend');
  if (missing.isEmpty) {
    return 'wake model files present but no on-device wake engine on '
        'this build (see WAKE_ONNX_SETUP.md; nothing is downloaded '
        'automatically)';
  }
  return 'no wake-word model on this build (missing: ${missing.join(', ')}; '
      'see WAKE_ONNX_SETUP.md for the training + install path; nothing '
      'is downloaded automatically)';
}

/// Wake opt-in panel: switch + mic status/request + battery gate +
/// honest caption. Manual buttons only — no timers, no polling loops.
/// State object is host-owned so the host's existing refresh path stays
/// the single source of snapshots.
class WakeOptInPanel extends StatefulWidget {
  final WakeOptInState state;

  /// Reused mic permission flow (must return the post-request state map
  /// with a `granted` key, as DeviceBridge.requestMicPermission does).
  final Future<Map<String, dynamic>> Function()? onRequestMic;

  /// Manual refresh (host's existing refresh; never called on a timer).
  final Future<void> Function()? onRefresh;

  /// Persists the preference (host's bridge write; best-effort). The
  /// host also wires `state.onStartListening` to its engine check; the
  /// panel only drives pref + start/stop calls.
  final Future<void> Function(bool value)? onPersist;

  const WakeOptInPanel(
      {super.key,
      required this.state,
      this.onRequestMic,
      this.onRefresh,
      this.onPersist});

  @override
  State<WakeOptInPanel> createState() => _WakeOptInPanelState();
}

class _WakeOptInPanelState extends State<WakeOptInPanel> {
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    // Host snapshot pushes (_refresh) repaint the panel with no polling.
    widget.state.addListener(_onState);
  }

  @override
  void didUpdateWidget(WakeOptInPanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.state != widget.state) {
      oldWidget.state.removeListener(_onState);
      widget.state.addListener(_onState);
    }
  }

  @override
  void dispose() {
    widget.state.removeListener(_onState);
    super.dispose();
  }

  void _onState() {
    if (mounted) setState(() {});
  }

  Future<void> _flip(bool value) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await widget.state.setOptIn(value);
      await widget.onPersist?.call(value);
      if (value) await widget.state.startListening();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _stopNow() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      // Immediate disable without leaving the screen: preference off,
      // persisted off, detector confirmed stopped.
      await widget.state.setOptIn(false);
      await widget.onPersist?.call(false);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _mic() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      final m = await widget.onRequestMic?.call();
      if (m != null) {
        await widget.state.updatePermission(m['granted'] == true);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _refresh() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await widget.onRefresh?.call();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.state;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SwitchListTile(
          title: const Text('Wake-word opt-in ("Hey Zara")'),
          subtitle: Text(s.statusLabel),
          value: s.optedIn,
          onChanged: _busy ? null : _flip,
          contentPadding: EdgeInsets.zero,
        ),
        Text('mic: ${s.micGranted ? 'granted' : 'not granted'}'),
        Text('battery: ${s.batteryPct?.toStringAsFixed(0) ?? '?'}% '
            '(gate: ${s.batteryGate}; '
            'pause < ${s.batteryPolicy.pauseBelowPct}%'
            '${s.charging ? ', charging' : ''}'
            '${s.powerSave ? ', power-save' : ''})'),
        Text(s.caption),
        Text(s.phaseLabel,
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                color: s.phase == WakeListenPhase.error
                    ? Theme.of(context).colorScheme.error
                    : null)),
        const Text('Listening runs only in the foreground and may '
            'increase battery usage. No audio leaves the device for '
            'detection; nothing is recorded.'),
        const SizedBox(height: 4),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            OutlinedButton(
                onPressed: _busy ? null : _mic,
                child: const Text('Enable mic')),
            OutlinedButton(
                onPressed: _busy ? null : _refresh,
                child: const Text('Refresh wake state')),
            OutlinedButton(
                onPressed: (_busy || !s.optedIn) ? null : _stopNow,
                child: const Text('Stop listening now')),
          ],
        ),
      ],
    );
  }
}
