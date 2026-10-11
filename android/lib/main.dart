import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'app_state.dart';
import 'assistant.dart';
import 'audio_io.dart';
import 'battery_governor.dart';
import 'capabilities.dart';
import 'capability_descriptors.dart';
import 'connection.dart';
import 'core_client.dart';
import 'device_bridge.dart';
import 'device_lifecycle.dart';
import 'diagnostics_panel.dart';
import 'history_view.dart';
import 'provider_setup.dart';
import 'job_runner.dart';
import 'memory_review.dart';
import 'network_monitor.dart';
import 'secure_store.dart';
import 'talk_stream_view.dart';
import 'transfer.dart';
import 'voice.dart' show wakePhrase;
import 'voice_flow.dart';
import 'voice_session.dart';
import 'wake_optin.dart';
import 'zara_notifications.dart';

void main() => runApp(const ZaraApp());

/// 409 from Core on decide = execution already decided elsewhere
/// (approved/denied/expired/cancelled): the approval card is stale and
/// should be dismissed locally, not retried. Pure for testing.
bool isStaleApproval(Object e) => e.toString().contains('409');

/// Parse the Tasks free-text inputs box. Blank = {} (safe tools take no
/// inputs). Must decode to a JSON OBJECT; syntax errors and non-objects
/// (lists/strings/numbers) are FormatExceptions shown inline — never sent.
/// Pure for testing.
Map<String, dynamic> parseTaskInputs(String text) {
  final t = text.trim();
  if (t.isEmpty) return {};
  final decoded = jsonDecode(t);
  if (decoded is! Map) {
    throw FormatException('inputs must be a JSON object, e.g. {"text": "hi"}');
  }
  return Map<String, dynamic>.from(decoded);
}

/// One-line, explicit rendering of execution-endpoint failures for the
/// Tasks section. 404/409/422 each get their own state (never silent);
/// 401/403 point at the operator token or policy, not the network.
String describeExecutionError(Object e) {
  final s = e.toString();
  if (s.contains('401') || s.contains('403')) {
    return 'unauthorized/forbidden (operator token missing/wrong, or policy denied)';
  }
  if (s.contains('404')) return 'not found (unknown tool or execution id)';
  if (s.contains('409')) return 'conflict (already terminal elsewhere)';
  if (s.contains('422')) return 'rejected (bad inputs — nothing was created)';
  return 'core error';
}

/// Canonical in-flight key for task submits: tool + canonical JSON of
/// inputs (key-order insensitive). Two rapid taps with the same tool and
/// logically-equal inputs collapse to one key; different tools or inputs
/// are distinct keys and may proceed in parallel. Pure for testing.
String taskSubmitKey(String tool, Map<String, dynamic> inputs) {
  final sorted = Map.fromEntries(
      List.of(inputs.entries)..sort((a, b) => a.key.compareTo(b.key)));
  return '$tool:${jsonEncode(sorted)}';
}

/// Capabilities the Tasks form can route through POST
/// /v1/executions/resolve. Identity mapping: capability name == tool name
/// for the device-backed safe tools. util.echo is a local Core tool
/// (supported_devices ["any"], never advertised in the fabric), so it has
/// no capability route and always uses direct POST /v1/executions.
const Set<String> taskResolvableCapabilities = {
  'system.battery',
  'system.network',
};

/// Capability-routed submit decision. Pure for testing: resolve only when
/// the toggle is on AND the tool has a capability route; everything else
/// stays on direct submit. Never throws.
bool taskShouldResolve({required String tool, required bool resolveEnabled}) =>
    resolveEnabled && taskResolvableCapabilities.contains(tool);

/// Telemetry narrowing helpers (observation only): coerce native values
/// to validated scalars, else null (= unknown, never zero-as-healthy).
double? _asDouble(Object? v) =>
    v is num && v.isFinite ? v.toDouble() : null;

int? _asInt(Object? v) => v is num && v.isFinite ? v.toInt() : null;

/// mem_pct derived device-side from avail/total (0-100); null unless both
/// present and sane. Core revalidates bounds regardless.
double? _memPct(Map<String, dynamic> telem) {
  final avail = _asInt(telem['mem_avail_mb']);
  final total = _asInt(telem['mem_total_mb']);
  if (avail == null || total == null || total <= 0 || avail > total) {
    return null;
  }
  return ((total - avail) * 100.0 / total).clamp(0.0, 100.0);
}

/// Foreground return restarts polling when the body is live (online) or
/// throttled-but-alive (degraded). Every other state needs explicit user
/// action (pairing) or is terminal (revoked/logged-out). Pure for testing.
/// _startLoop still refuses to schedule while battery-critical.
bool shouldResumeLoop(String lifecycleState) =>
    lifecycleState == DeviceLifecycleState.online ||
    lifecycleState == DeviceLifecycleState.degraded;

/// Re-register backoff: 5s, 10s, 20s, 40s, then 60s cap.
/// Pure for testing. Timer-driven (no busy loop); cancelled on pause.
Duration registerRetryDelay(int attempt) {
  var secs = 5 * (1 << (attempt - 1).clamp(0, 10));
  if (secs > 60) secs = 60;
  return Duration(seconds: secs);
}

/// Zara Android body (Stage 9): lifecycle-driven shell over the SAME Zara.
///
/// This widget owns NO authority: pairing/keys in SecureStore, jobs run
/// only from the allowlist, approvals resolve through Core endpoints,
/// voice state mirrors Core. It is presentation + device I/O only.
class ZaraApp extends StatefulWidget {
  const ZaraApp({super.key});
  @override
  State<ZaraApp> createState() => _ZaraAppState();
}

class _ZaraAppState extends State<ZaraApp> with WidgetsBindingObserver {
  final bridge = DeviceBridge();
  // Emulator default; override for physical devices:
  // flutter build apk --dart-define=ZARA_CORE_URL=http://<host-lan-ip>:8080
  static const _coreUrl = String.fromEnvironment(
      'ZARA_CORE_URL',
      defaultValue: 'http://10.0.2.2:8080');
  final client = CoreClient(_coreUrl);
  final secureStore = SecureStore();
  final lifecycle = DeviceLifecycle();
  final voice = AndroidVoiceSession();
  final wakeOptIn = WakeOptInState();
  final notifCenter = NotificationCenter();
  late final AndroidJobRunner jobs;
  final codeCtrl = TextEditingController();
  final retry = const RetryPolicy();

  String conn = CoreLinkState.disconnected;
  BatteryReport battery = BatteryReport(timestamp: DateTime.now());
  NetworkSnapshot network =
      NetworkSnapshot('unavailable', timestamp: DateTime.now());
  Map<String, dynamic> voiceFlags = {};
  Map<String, dynamic> perms = {};
  Map<String, dynamic> a11y = {};
  Map<String, dynamic> telem = {};

  /// Registration-guard state: gui.* advertise available only when the
  /// probe says the accessibility service is enabled, bound, and seeing
  /// a window. Anything else (including probe failure) reads as not ready.
  bool get a11yReady => a11y['ready'] == true;
  AssistantStatus assistant = const AssistantStatus();
  String lastError = '';
  int _failures = 0;
  String secureBackend = 'keystore';
  String voiceDiag = 'idle';
  String voiceTranscript = '';
  String voiceReply = '';
  String voiceCapture = '';
  String voicePlayback = '';
  String voiceStt = '';
  String voiceChatReply = '';
  bool voiceBusy = false;
  // Voice turn glue (capture -> turn -> transcript -> reply -> TTS):
  // production legs are the existing bridge + client methods; transcript
  // sink feeds the voice section display (sent to chat on demand via the
  // existing POST /v1/talk below), reply audio auto-plays with overlap
  // guard, interrupt stops all three legs. No always-on listening.
  late final VoiceFlowController voiceFlow;
  String xferDiag = 'idle';
  String xferDetail = '';
  bool xferBusy = false;
  bool _xferStop = false;
  final grantCtrl = TextEditingController();
  static const _xferPeer = 'laptop-1'; // diagnostic counterpart (Stage 15)

  // ---- Tasks: async Core executions (submit + status + cancel) ----
  // Small fixed safe-tool list only (read-only probes + echo). Inputs are
  // free-text JSON validated by parseTaskInputs. Approval flow is untouched:
  // waiting_for_permission rows point at the Approval section, which still
  // resolves device-scoped through Core (never local execution).
  static const taskTools = ['system.battery', 'system.network', 'util.echo'];
  String _taskTool = 'system.battery';
  final _taskInputsCtrl = TextEditingController(text: '{}');
  final _taskTokenCtrl = TextEditingController(); // operator token, memory only
  // History -> conversation resume: the talk panel keeps its state across
  // rebuilds via [_talkKey] (same widget-pattern seam as the approval
  // callback); [_talkSessionId] is the resumed session the next send
  // continues, and [_talkResumeGen] discards stale async resumes.
  final _talkKey = GlobalKey<TalkStreamPanelState>();
  final _talkSectionKey = GlobalKey();
  String _talkSessionId = '';
  int _talkResumeGen = 0;
  String _taskFormError = '';
  String _taskError = '';
  bool _taskBusy = false;
  // Capability-routed submit (POST /v1/executions/resolve): on by default
  // for capability-backed tools (system.*). util.echo always uses direct
  // submit (see taskShouldResolve). Toggle is presentation-only.
  bool _taskUseResolve = true;
  // Per tool+inputs in-flight guard: rapid double-taps with identical
  // payloads collapse into the first request. Distinct tools/inputs have
  // distinct keys and are unaffected. Core 409 remains the server-side
  // backstop for races that cross processes.
  final Set<String> _taskInflightKeys = {};
  final List<Map<String, dynamic>> _tasks = [];
  final Set<String> _cancelling = {};
  final Set<String> _refreshing = {};
  final Map<String, int> _taskRefreshFailures = {};
  final PendingQueue _taskQueue = PendingQueue(maxSize: 20);
  Timer? _loop;
  bool _busy = false;
  final Set<String> _deciding = {};
  Timer? _regRetry;
  int _regAttempts = 0;
  bool _registering = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    jobs = AndroidJobRunner(
      battery: () => battery.toHeartbeat(),
      network: () => {'network': network.state, 'metered': network.metered},
      forceStop: (pkg) => bridge.privForceStop(pkg),
      a11yInspect: () => bridge.a11yInspect(),
      a11yInspectWithParams: (params) => bridge.a11yInspectWithParams(params),
      a11yTap: (pkg, nodeId) => bridge.a11yTap(pkg, nodeId),
    );
    lifecycle.onChange = (_) => _render();
    voiceFlow = VoiceFlowController(
      capture: () => bridge.audioCapture(seconds: 5.0),
      turn: (wav) => client.voiceTurn(wav),
      fetchTts: (text) => client.tts(text),
      play: (wav) async {
        final r = await bridge.audioPlay(wav);
        voicePlayback = 'speaker: $r (software-only, NOT audibility)';
        // ignore: avoid_print
        print('zara:voice-playback $r');
        _render();
      },
      stopCapture: () => bridge.audioStop(),
      stopPlayback: () => bridge.audioPlayStop(),
      serverInterrupt: () => client.interruptVoice(),
      onTranscript: (t) {
        voiceTranscript = t;
        _render();
      },
    );
    // Wake opt-in release reuses the existing stop path (all three legs).
    wakeOptIn.onRelease = () => voiceFlow.interrupt();
    _boot();
  }

  void _render() {
    if (mounted) setState(() {});
  }

  Future<void> _boot() async {
    await bridge.createNotificationChannels();
    await _refresh();
    secureBackend = secureStore.backend;
    // ignore: avoid_print
    print('zara:secure-backend=$secureBackend');
    final creds = await secureStore.loadDeviceCredentials();
    if (creds.deviceId == null || creds.deviceKey == null) {
      lifecycle.move(DeviceLifecycleState.pairing);
      _render();
      return;
    }
    client.deviceId = creds.deviceId;
    client.deviceKey = creds.deviceKey;
    lifecycle.restore(creds.deviceId);
    await _register();
  }

  Future<void> _refresh() async {
    final b = await bridge.battery();
    final n = await bridge.network();
    Map<String, dynamic> v = {};
    try {
      v = await bridge.voiceSupport();
    } catch (_) {/* keep static table */}
    try {
      final t = await bridge.audioSelfTest();
      // ignore: avoid_print
      print('zara:audio-selftest=$t');
    } catch (_) {/* self-test best-effort */}
    try {
      perms = await bridge.permissions();
    } catch (_) {/* keep last */}
    try {
      // Resource telemetry (observation only): cheap synchronous getters
      // riding the existing tick. Missing keys = unavailable, never zero.
      telem = await bridge.telemetry();
    } catch (_) {/* keep last */}
    try {
      a11y = await bridge.a11yProbe();
    } catch (_) {/* keep last: not-ready reads as unavailable downstream */}
    try {
      final am = await bridge.assistantStatus();
      assistant = AssistantStatus.fromMap(am);
      // ignore: avoid_print
      print('zara:assistant-status'
          ' registered=${assistant.serviceRegistered}'
          ' default=${assistant.zaraIsDefault}'
          ' roleAvail=${assistant.roleAvailable}'
          ' roleHeld=${assistant.roleHeld}'
          ' current=${assistant.currentAssistant}'
          ' infoValid=${am['service_info_valid']}'
          ' supportsAssist=${am['supports_assist']}'
          ' infoError=${am['service_info_error']}');
    } catch (_) {/* keep last */}
    if (!mounted) return;
    setState(() {
      battery = BatteryReport.fromBridge(b);
      network = NetworkSnapshot.fromBridge(n);
      voiceFlags = v;
    });
    // Wake opt-in mirrors the same snapshots (manual push, no polling):
    // flags + mic permission + battery/telemetry gate.
    wakeOptIn.updateFlags(Map<String, dynamic>.from(v));
    await wakeOptIn.updatePermission(perms['microphone'] == true);
    wakeOptIn.updateBattery(
      pct: battery.pct,
      charging: battery.charging,
      powerSave:
          telem['power_save'] is bool ? telem['power_save'] as bool : false,
    );
    // ignore: avoid_print
    print('zara:voice-support=$v');
  }

  Future<void> _register() async {
    // Re-entrancy: retry timer + foreground resume must not overlap.
    if (_registering) return;
    _registering = true;
    _regRetry?.cancel();
    setState(() => conn = CoreLinkState.connecting);
    try {
      await client.register(advertisedCapabilities(), 'zara-android 9.0.0');
      // Stage 16: capability snapshot follows registration (Core diffs;
      // no re-pair needed when capabilities change). Best-effort: string
      // caps still route even if describe is unreachable right now.
      try {
        final desc = await client.describeCapabilities(androidCapabilityDocs(
          micPermission: perms['microphone'] == true,
          notifPermission: perms['notifications'] == true,
          forceStopGranted: perms['forceStop'] == true,
          a11yReady: a11yReady,
        ));
        // ignore: avoid_print
        print('zara:describe ok accepted=${desc['accepted']} '
            'rejected=${desc['rejected']}');
      } catch (e) {
        // ignore: avoid_print
        print('zara:describe failed: $e');
      }
      _regAttempts = 0;
      lifecycle.move(DeviceLifecycleState.online);
      setState(() {
        conn = CoreLinkState.online;
        lastError = '';
        _failures = 0;
      });
      _startLoop();
    } on AuthException catch (e) {
      _regRetry?.cancel();
      await secureStore.clearDeviceCredentials();
      lifecycle.handleHttp(403, hasIdentity: true);
      setState(() {
        conn = CoreLinkState.error;
        lastError = 'revoked: $e';
      });
    } catch (e) {
      // Register-while-unreachable (Stage 11 fix): without a retry the
      // lifecycle strands in `registering` forever — no loop ever starts.
      // Timer-driven bounded backoff; cancelled on pause/dispose.
      _regAttempts += 1;
      _regRetry?.cancel();
      _regRetry = Timer(registerRetryDelay(_regAttempts), () {
        // Only retry if nobody moved the lifecycle meanwhile
        // (logout/revoke/pairing wins over the timer).
        if (mounted &&
            (lifecycle.state == DeviceLifecycleState.registering ||
                lifecycle.state == DeviceLifecycleState.reconnecting)) {
          _register();
        }
      });
      setState(() {
        conn = _regAttempts <= 2
            ? CoreLinkState.reconnecting
            : CoreLinkState.offline;
        lastError = '$e';
      });
    } finally {
      _registering = false;
    }
  }

  void _startLoop() {
    _loop?.cancel();
    // Battery-critical: no auto loop; user reconnects manually.
    // (Loop may still be restarted later; _tick re-checks every round.)
    if (battery.level() == AndroidBatteryLevel.critical) return;
    final interval = battery.reduceBackgroundWork
        ? const Duration(seconds: 60)
        : const Duration(seconds: 30);
    _loop = Timer.periodic(interval, (_) => _tick());
    unawaited(_tick());
  }

  Future<void> _tick() async {
    if (_busy || lifecycle.state != DeviceLifecycleState.online) return;
    if (!network.online) {
      setState(() => conn = CoreLinkState.offline);
      return;
    }
    _busy = true;
    final scope = CancelScope();
    try {
      await _refresh();
      // Re-describe if a11y probe state changed (permission-like gating).
      await _register();
      // Poll jobs BEFORE heartbeat/notifications: a mid-tick failure must
      // never starve job polling (Core proxy timeout assumes the device
      // gets a poll opportunity every tick — see JOB_TIMING_FIX.md).
      await _pollJobs(scope);
      await client.heartbeat(
        batteryPct: battery.pct,
        charging: battery.charging,
        network: network.state,
        // Telemetry narrowed to validated scalars; nulls mean unknown.
        lowPowerMode: telem['power_save'] is bool
            ? telem['power_save'] as bool
            : null,
        memPct: _memPct(telem),
        storageFreeMb: _asInt(telem['storage_free_mb']),
        storageTotalMb: _asInt(telem['storage_total_mb']),
        batteryTempC: _asDouble(telem['battery_temp_c']),
        thermalState: telem['thermal_state'] is String
            ? (telem['thermal_state'] as String)
            : null,
        cancel: scope,
      );
      if (battery.pct != null &&
          battery.pct! < AndroidBatteryLevel.criticalBelow &&
          !battery.charging) {
        lifecycle.move(DeviceLifecycleState.degraded);
        setState(() => conn = CoreLinkState.degraded);
      } else if (lifecycle.state == DeviceLifecycleState.degraded &&
          (battery.pct == null ||
              battery.pct! >= AndroidBatteryLevel.resumeAbove)) {
        lifecycle.move(DeviceLifecycleState.online);
        setState(() => conn = CoreLinkState.online);
      }
      await _pullNotifications();
      setState(() {
        _failures = 0;
        if (conn != CoreLinkState.degraded) {
          conn = CoreLinkState.online;
        }
      });
    } on AuthException {
      await secureStore.clearDeviceCredentials();
      lifecycle.handleHttp(403, hasIdentity: true);
      setState(() => conn = CoreLinkState.error);
    } on NotRegisteredException {
      // Backend restarted: re-register once, never loop.
      try {
        await client.register(advertisedCapabilities(), 'zara-android 9.0.0');
      } catch (_) {/* next tick retries with backoff */}
    } catch (_) {
      _failures += 1;
      final waitLonger =
          !retry.shouldRetry(_failures, batteryCritical: false);
      setState(() => conn = waitLonger
          ? CoreLinkState.offline
          : CoreLinkState.reconnecting);
    } finally {
      _busy = false;
    }
  }

  Future<void> _pollJobs(CancelScope scope) async {
    final raw = await client.pollJobs(cancel: scope);
    if (raw == null) return;
    DeviceJob job;
    try {
      job = DeviceJob.fromJson(raw);
    } catch (e) {
      return; // malformed: never execute; Core expires the claim
    }
    final out = await jobs.run(job);
    await client.reportJobResult(job.jobId,
        ok: out.ok, result: out.result, error: out.error);
  }

  Future<void> _pullNotifications() async {
    final list = await client.fetchNotifications();
    var changed = false;
    for (final raw in list) {
      try {
        if (notifCenter.show(DeviceNotification.fromJson(raw))) {
          changed = true;
        }
      } catch (_) {/* skip malformed, keep polling */}
    }
    if (changed && mounted) setState(() {});
  }

  Future<void> _connect() async {
    if (lifecycle.state != DeviceLifecycleState.pairing) {
      try {
        lifecycle.move(DeviceLifecycleState.pairing);
      } catch (_) {
        // e.g. revoked/logged-out: fall through to the pairing path anyway
      }
    }
    setState(() => lastError = '');
    try {
      await client.claim(codeCtrl.text.trim(), 'android-phone');
      await secureStore.saveDeviceCredentials(
        deviceId: client.deviceId ?? 'android-phone',
        deviceKey: client.deviceKey ?? '',
      );
      secureBackend = secureStore.backend;
      lifecycle.move(DeviceLifecycleState.enrolled);
      lifecycle.move(DeviceLifecycleState.registering);
      await _register();
    } on CoreException catch (e) {
      lifecycle.move(DeviceLifecycleState.error);
      setState(() => lastError = '$e');
    }
  }

  Future<void> _logout() async {
    _loop?.cancel();
    _regRetry?.cancel();
    try {
      await client.disconnect();
    } catch (_) {}
    await secureStore.clearDeviceCredentials();
    client.deviceId = null;
    client.deviceKey = null;
    lifecycle.logout();
    setState(() => conn = CoreLinkState.disconnected);
  }

  /// Voice turn glue (physical validation): bounded capture -> Core turn
  /// -> transcript sink -> reply auto-routed to TTS playback (overlap
  /// guarded: a second reply cancels the first). Transcript stays on
  /// screen and can be sent to the conversation loop on demand
  /// ([_sendTranscriptToChat]). All legs report separately; playback
  /// completion is logged as software-only, never audibility.
  Future<void> _recordTest() async {
    if (voiceBusy) return;
    setState(() {
      voiceBusy = true;
      voiceDiag = 'recording… (speak now)';
      voiceChatReply = '';
    });
    try {
      final res = await voiceFlow.runTurn();
      final wav = voiceFlow.lastCapture;
      final peak = wav == null ? null : wavPeakDbfs(wav);
      final peakStr = peak?.toStringAsFixed(1) ?? 'silence';
      voiceTranscript = res.transcript;
      voiceReply = res.reply;
      voiceStt = res.stt;
      voiceCapture =
          'mic: ${wav?.length ?? 0}B peak=$peakStr dBFS';
      setState(() => voiceDiag =
          'heard: "$voiceTranscript" (peak $peakStr dBFS) — reply spoken via TTS');
      // ignore: avoid_print
      print('zara:voice-turn transcript="$voiceTranscript" '
          'reply="$voiceReply" status=${res.status}');
    } on VoiceFlowException catch (e) {
      setState(() => voiceDiag = '$e');
    } on AudioBridgeException catch (e) {
      setState(() => voiceDiag = 'capture: $e');
    } on CoreException catch (e) {
      setState(() {
        voiceDiag = 'core: $e';
        lastError = '$e';
      });
    } finally {
      if (mounted) setState(() => voiceBusy = false);
    }
  }

  Future<void> _speakLast() async {
    if (voiceReply.isEmpty || voiceBusy) return;
    setState(() {
      voiceBusy = true;
      voiceDiag = 'speaking…';
    });
    try {
      await voiceFlow.speak(voiceReply);
      if (mounted) setState(() => voiceDiag = 'playback exited (software-only)');
    } on AudioBridgeException catch (e) {
      voicePlayback = 'speaker FAILED: $e';
      if (mounted) setState(() => voiceDiag = 'playback: $e');
    } on CoreException catch (e) {
      if (mounted) {
        setState(() {
          voiceDiag = 'core: $e';
          lastError = '$e';
        });
      }
    } finally {
      if (mounted) setState(() => voiceBusy = false);
    }
  }

  Future<void> _stopVoice() async {
    await voiceFlow.interrupt();
    if (mounted) setState(() => voiceDiag = 'stopped');
  }

  /// Shared mic-permission flow (permission panel button + wake opt-in
  /// panel reuse). Returns the post-request state; denial is a normal
  /// outcome, never an error. Feeds the wake opt-in gate so revocation
  /// surfaces as permission-required instead of silent listening.
  Future<Map<String, dynamic>> _requestMic() async {
    final m = await bridge.requestMicPermission();
    if (mounted) {
      setState(() => perms['microphone'] = m['granted']);
      // Stage 16: permission flips availability
      // (os_denied <-> available) without re-pairing.
      try {
        await client.describeCapabilities(androidCapabilityDocs(
          micPermission: perms['microphone'] == true,
          notifPermission: perms['notifications'] == true,
          forceStopGranted: perms['forceStop'] == true,
          a11yReady: a11yReady,
        ));
      } catch (_) {/* next boot retries */}
    }
    await wakeOptIn.updatePermission(perms['microphone'] == true);
    if (mounted) setState(() {});
    return m;
  }

  /// Transcript -> conversation view: submits the last voice transcript
  /// to the existing POST /v1/talk loop (operator token, same as Tasks)
  /// and shows the reply inline. User-initiated only, never automatic.
  Future<void> _sendTranscriptToChat() async {
    final text = voiceTranscript.trim();
    if (voiceBusy || text.isEmpty) return;
    final token = _taskTokenCtrl.text.trim();
    client.operatorToken = token.isEmpty ? null : token;
    setState(() {
      voiceBusy = true;
      voiceDiag = 'sending transcript to chat…';
    });
    try {
      final out = await client.talk(
          text: text, deviceId: client.deviceId ?? 'android-phone');
      if (!mounted) return;
      setState(() {
        voiceChatReply = '${out['reply'] ?? ''}';
        voiceDiag = 'transcript sent to chat';
      });
      // ignore: avoid_print
      print('zara:voice-to-chat reply="$voiceChatReply"');
    } on CoreException catch (e) {
      if (mounted) {
        setState(() {
          voiceDiag = 'chat: $e';
          lastError = '$e';
        });
      }
    } catch (e) {
      if (mounted) setState(() => voiceDiag = 'chat transport: $e');
    } finally {
      if (mounted) setState(() => voiceBusy = false);
    }
  }

  /// Stage 15 transfer diagnostic: upload deterministic payloads to the
  /// laptop through Core authorization (a live files.transfer grant is
  /// required; paste its ID below). Bytes are hashed locally with
  /// transfer.dart; Core verifies independently. No grant => Core 403,
  /// shown verbatim (never bypassed).
  Future<void> _xferSend() async {
    if (xferBusy || lifecycle.state != DeviceLifecycleState.online) return;
    setState(() {
      xferBusy = true;
      _xferStop = false;
      xferDiag = 'sending…';
    });
    try {
      final grant = grantCtrl.text.trim();
      final jobs = <Map<String, dynamic>>[
        {
          'name': 'stage15-vivo-to-laptop.txt',
          'bytes': Uint8List.fromList(
              utf8.encode(stage15Text('vivo-to-laptop'))),
          'type': 'text/plain',
        },
        {
          'name': 'stage15-vivo-to-laptop.bin',
          'bytes': deterministicBytes(64 * 1024, 42),
          'type': 'application/octet-stream',
        },
        {
          'name': 'stage15-vivo-to-laptop-1M.bin',
          'bytes': deterministicBytes(1024 * 1024, 99),
          'type': 'application/octet-stream',
        },
      ];
      final lines = <String>[];
      for (final job in jobs) {
        if (_xferStop) {
          lines.add('stopped by user');
          break;
        }
        final data = job['bytes'] as Uint8List;
        final name = job['name'] as String;
        final hex = sha256Hex(data);
        setState(() => xferDiag = 'requesting $name (${data.length}B)…');
        final req = await client.xferRequest(
            recipient: _xferPeer,
            filename: name,
            sizeBytes: data.length,
            sha256: hex,
            contentType: job['type'] as String,
            grantId: grant);
        final tid = '${req['transfer_id']}';
        var state = '${req['state']}';
        final wins = chunkWindows(data.length, 64 * 1024);
        for (var seq = 0; seq < wins.length; seq++) {
          if (_xferStop) break;
          final w = wins[seq];
          final out = await client.xferChunk(
              tid, seq, Uint8List.sublistView(data, w[0], w[0] + w[1]));
          state = '${out['state']}';
          if (mounted) {
            setState(() => xferDiag =
                '$name: ${out['received_bytes']}/${out['size_bytes']}B…');
          }
        }
        lines.add('↑ $name id=$tid sha=${hex.substring(0, 16)}… $state');
        // ignore: avoid_print
        print('zara:xfer-send $name id=$tid state=$state sha=$hex');
        if (state != 'succeeded') break;
      }
      setState(() {
        xferDetail = lines.join('\n');
        xferDiag = lines.isEmpty ? 'nothing sent' : 'done';
      });
    } on CoreException catch (e) {
      setState(() => xferDiag = 'core: $e');
    } catch (e) {
      // Network loss, DNS, socket reset: surface verbatim, never stuck.
      setState(() => xferDiag = 'transport: $e');
    } finally {
      if (mounted) setState(() => xferBusy = false);
    }
  }

  /// Stage 15 fetch diagnostic: download everything Core holds for this
  /// device, verify SHA-256 locally, ack ONLY on match. Bytes stay in
  /// memory (sandbox receipt); the ack + Core record are the proof.
  Future<void> _xferFetch() async {
    if (xferBusy || lifecycle.state != DeviceLifecycleState.online) return;
    setState(() {
      xferBusy = true;
      _xferStop = false;
      xferDiag = 'fetching…';
    });
    try {
      final pend =
          await client.xferPending() as Map<String, dynamic>? ?? {};
      final items = (pend['to_download'] as List?) ?? [];
      final lines = <String>[];
      if (items.isEmpty) {
        setState(() => xferDiag = 'nothing pending');
        return;
      }
      for (final item in items) {
        if (_xferStop) {
          lines.add('stopped by user');
          break;
        }
        final m = Map<String, dynamic>.from(item as Map);
        final tid = '${m['transfer_id']}';
        final total = (m['size_bytes'] as num).toInt();
        final want = '${m['sha256']}';
        final buf = BytesBuilder();
        var off = 0;
        while (off < total) {
          if (_xferStop) break;
          final len = (total - off) < 64 * 1024 ? total - off : 64 * 1024;
          final blk = await client.xferBytes(tid, off, len);
          final raw = base64Decode('${blk['data_base64']}');
          buf.add(raw);
          off += (blk['length'] as num).toInt();
          if (mounted) {
            setState(() => xferDiag = '${m['filename']}: $off/$total B…');
          }
          if (raw.isEmpty) break;
        }
        final data = buf.toBytes();
        final hex = sha256Hex(data);
        if (hex != want || data.length != total) {
          lines.add('↓ ${m['filename']} id=$tid MISMATCH '
              '(no ack sent)');
          // ignore: avoid_print
          print('zara:xfer-fetch $tid MISMATCH local=$hex want=$want');
          continue;
        }
        final ack = await client.xferAck(tid, hex);
        lines.add('↓ ${m['filename']} id=$tid ${data.length}B '
            'sha=${hex.substring(0, 16)}… verified=${ack['verified']}');
        // ignore: avoid_print
        print('zara:xfer-fetch $tid verified=${ack['verified']} sha=$hex');
      }
      setState(() {
        xferDetail = lines.join('\n');
        xferDiag = 'done';
      });
    } on CoreException catch (e) {
      setState(() => xferDiag = 'core: $e');
    } catch (e) {
      // Network loss, DNS, socket reset: surface verbatim, never stuck.
      setState(() => xferDiag = 'transport: $e');
    } finally {
      if (mounted) setState(() => xferBusy = false);
    }
  }

  void _xferCancel() {
    _xferStop = true;
    if (mounted) setState(() => xferDiag = 'stopping…');
  }

  /// Submit one async execution to Core. Capability-backed tools
  /// (system.*) route via POST /v1/executions/resolve when the resolve
  /// toggle is on (Core decides the device; echoed in the view's
  /// resolution block); everything else uses direct POST /v1/executions.
  /// Duplicate protection: a per tool+inputs in-flight key
  /// ([taskSubmitKey]) collapses rapid double-taps into the first
  /// request; Core 409 remains the cross-process backstop. Offline
  /// submits go into the BOUNDED [PendingQueue] (connection.dart) with an
  /// explicit "queued" state — never silently dropped, never an unbounded
  /// retry loop. 404/409/422 render as explicit states via
  /// describeExecutionError, never silent.
  Future<void> _submitTask() async {
    if (_taskBusy) return;
    setState(() {
      _taskFormError = '';
      _taskError = '';
    });
    Map<String, dynamic> inputs;
    try {
      inputs = parseTaskInputs(_taskInputsCtrl.text);
    } on FormatException catch (e) {
      setState(() => _taskFormError = '$e');
      return;
    }
    // Per tool+inputs duplicate guard: a second tap with the identical
    // payload while the first is still in flight is dropped explicitly
    // (never a second execution). Distinct payloads proceed.
    final inflightKey = taskSubmitKey(_taskTool, inputs);
    if (!_taskInflightKeys.add(inflightKey)) {
      setState(() => _taskError =
          'already submitting $_taskTool with these inputs — wait for the first request');
      return;
    }
    final token = _taskTokenCtrl.text.trim();
    client.operatorToken = token.isEmpty ? null : token;
    final deviceId = client.deviceId ?? 'android-phone';
    final useResolve =
        taskShouldResolve(tool: _taskTool, resolveEnabled: _taskUseResolve);
    // One key per user action: transport retries and the offline drain
    // reuse it (Core collapses them); a fresh tap gets a fresh key.
    final idemKey = newIdempotencyKey(_taskTool);
    final payload = buildExecutionSubmitBody(
        tool: _taskTool,
        inputs: inputs,
        deviceId: deviceId,
        idempotencyKey: idemKey);
    if (!network.online) {
      // Offline queue stores the direct-submit envelope (resolve needs a
      // live Core resolver); the drain path sends via submitExecution.
      final queued = _taskQueue.add(PendingOp(
          'task-submit', Map<String, dynamic>.from(payload)));
      _taskInflightKeys.remove(inflightKey);
      setState(() => _taskError = queued
          ? 'offline — submit queued (${_taskQueue.length} pending, sends on next online submit)'
          : 'offline and task queue is full (${_taskQueue.length}) — submit dropped, nothing sent');
      return;
    }
    setState(() => _taskBusy = true);
    try {
      await _flushTaskQueue();
      final out = useResolve
          ? await client.resolveExecution(
              capability: _taskTool,
              inputs: inputs,
              deviceId: deviceId,
              idempotencyKey: idemKey)
          : await client.submitExecution(
              tool: _taskTool,
              inputs: inputs,
              deviceId: deviceId,
              idempotencyKey: idemKey);
      if (!mounted) return;
      setState(() {
        if (out.containsKey('id')) {
          _tasks.insert(0, out);
          if (_tasks.length > 20) _tasks.removeLast();
        } else {
          // Deferred gate (or unexpected envelope): no execution exists.
          _taskError =
              'submit: ${out['status'] ?? 'no id'} — ${out['reason'] ?? out}';
        }
      });
    } on CoreException catch (e) {
      if (mounted) setState(() => _taskError = 'submit ${describeExecutionError(e)}: $e');
    } catch (e) {
      // Network loss, DNS, socket reset: verbatim, never stuck.
      if (mounted) setState(() => _taskError = 'submit transport: $e');
    } finally {
      _taskInflightKeys.remove(inflightKey);
      if (mounted) setState(() => _taskBusy = false);
    }
  }

  /// Drain offline-queued submits, oldest first. Single attempt each:
  /// bounded by the queue size, no retry loop. Stops at the first failure
  /// (transport or CoreException) and reports it explicitly.
  Future<void> _flushTaskQueue() async {
    if (_taskQueue.isEmpty || !network.online) return;
    for (final op in _taskQueue.drain()) {
      final p = op.payload;
      await client.submitExecution(
          tool: '${p['tool']}',
          inputs: (p['inputs'] is Map)
              ? Map<String, dynamic>.from(p['inputs'] as Map)
              : {},
          deviceId: '${p['device_id']}',
          idempotencyKey: p['idempotency_key'] is String
              ? p['idempotency_key'] as String
              : null);
    }
  }

  /// Manual refresh of one row (GET /v1/executions/{id}). Consecutive
  /// failures are counted against the shared [RetryPolicy]: past the
  /// budget the message says so explicitly, but manual retry stays
  /// available (budget resets on success). No auto-loop, ever.
  Future<void> _refreshTask(String id) async {
    if (!_refreshing.add(id)) return;
    if (mounted) setState(() => _taskError = '');
    try {
      final out = await client.executionStatus(id);
      if (!mounted) return;
      setState(() {
        _taskRefreshFailures.remove(id);
        final i = _tasks.indexWhere((t) => '${t['id']}' == id);
        if (i >= 0) {
          _tasks[i] = out;
        } else {
          _tasks.insert(0, out);
        }
      });
    } on CoreException catch (e) {
      if (!mounted) return;
      final n = (_taskRefreshFailures[id] ?? 0) + 1;
      _taskRefreshFailures[id] = n;
      final withinBudget = retry.shouldRetry(n,
          batteryCritical:
              battery.level() == AndroidBatteryLevel.critical);
      setState(() => _taskError = 'refresh $id ${describeExecutionError(e)}: $e'
          '${withinBudget ? '' : ' (retry budget exhausted — check Core, then refresh again)'}');
    } catch (e) {
      if (mounted) setState(() => _taskError = 'refresh $id transport: $e');
    } finally {
      _refreshing.remove(id);
      if (mounted) setState(() {});
    }
  }

  /// Cancel one row (POST /v1/executions/{id}/cancel). 409 = already
  /// terminal elsewhere: the row keeps its last known state and the
  /// conflict is shown, never retried, never silently dropped.
  Future<void> _cancelTask(String id) async {
    if (!_cancelling.add(id)) return;
    if (mounted) setState(() => _taskError = '');
    try {
      final out = await client.cancelExecution(id);
      if (!mounted) return;
      setState(() {
        final i = _tasks.indexWhere((t) => '${t['id']}' == id);
        if (i >= 0) {
          _tasks[i] = out;
        } else {
          _tasks.insert(0, out);
        }
      });
    } on CoreException catch (e) {
      if (mounted) setState(() => _taskError = 'cancel $id ${describeExecutionError(e)}: $e');
    } catch (e) {
      if (mounted) setState(() => _taskError = 'cancel $id transport: $e');
    } finally {
      _cancelling.remove(id);
      if (mounted) setState(() {});
    }
  }

  Future<void> _decide(
      DeviceNotification n, String execId, bool approve) async {
    // One in-flight decision per execution: double-taps collapse into the
    // first request. Core 409 remains the real duplicate-execution guard.
    if (!_deciding.add(execId)) return;
    try {
      await client.decideApproval(execId, approve);
      notifCenter.ack(n.id);
      try {
        await client.ackNotification(n.id);
      } catch (_) {}
    } on CoreException catch (e) {
      // 409 = Core already decided (approved/denied/expired elsewhere):
      // the card is stale, so dismiss it locally instead of nagging.
      // Anything else keeps the card for retry.
      if (isStaleApproval(e)) {
        notifCenter.ack(n.id);
        try {
          await client.ackNotification(n.id);
        } catch (_) {}
        if (mounted) setState(() {});
        return;
      }
      setState(() => lastError = 'approval: $e');
    } finally {
      _deciding.remove(execId);
    }
    if (mounted) setState(() {});
  }

  /// History Continue -> conversation: (a) set the talk panel's session
  /// (rebuild with the resumed id; the panel adopts it for its next send),
  /// (b) load the transcript read-only above the live turn, (c) scroll the
  /// talk section into view. Stale resumes (a newer Continue won while this
  /// one was queued) are discarded by [_talkResumeGen]. Synchronous: the
  /// transcript arrives already-fetched from HistoryPanel, so loading the
  /// panel never submits prompts — zero backend calls here.
  void _resumeTalk(
      String sessionId, List<Map<String, dynamic>> messages) {
    if (sessionId.isEmpty) return;
    final gen = ++_talkResumeGen;
    setState(() => _talkSessionId = sessionId);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || gen != _talkResumeGen) return; // stale: discard
      _talkKey.currentState?.loadHistory(messages, generation: gen);
      final ctx = _talkSectionKey.currentContext;
      if (ctx != null) {
        unawaited(Scrollable.ensureVisible(ctx,
            duration: const Duration(milliseconds: 300)));
      }
    });
  }

  /// One execution row: status/job_state/result/error/approval flag with
  /// manual refresh + cancel (cancel hidden once terminal). Unparseable
  /// envelopes render raw (never blank, never silent).
  Widget _taskCard(BuildContext context, Map<String, dynamic> raw) {
    ExecutionView? v;
    try {
      v = ExecutionView.fromJson(raw);
    } catch (_) {
      v = null;
    }
    final id = '${raw['id'] ?? '?'}';
    final tool = '${raw['tool'] ?? _taskTool}';
    final status = '${raw['status'] ?? 'unknown'}';
    final jobState =
        raw['job_state'] == null ? '—' : '${raw['job_state']}';
    final error = '${raw['error'] ?? ''}';
    final result = raw['result'];
    final approval = v?.needsApproval ?? false;
    final terminal = v?.isTerminal ?? isExecutionTerminal(status);
    final busy =
        _refreshing.contains(id) || _cancelling.contains(id);
    final shortId = id.length <= 12 ? id : '${id.substring(0, 12)}…';
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('$tool · $shortId',
                style: Theme.of(context).textTheme.titleSmall),
            Text('status: $status (job: $jobState)'),
            if (approval)
              const Text('Approval required — see Approval section above',
                  style: TextStyle(color: Colors.orange)),
            if ((v?.cancelRequested ?? raw['cancel_requested'] == true))
              const Text('cancel requested…'),
            if (result != null) Text('result: $result'),
            if (error.isNotEmpty)
              Text('error: $error',
                  style: const TextStyle(color: Colors.red)),
            if (v == null) Text('raw: $raw'),
            const SizedBox(height: 4),
            Wrap(
              spacing: 8,
              children: [
                OutlinedButton(
                    onPressed:
                        busy ? null : () => _refreshTask(id),
                    child: const Text('Refresh')),
                if (!terminal)
                  OutlinedButton(
                      onPressed:
                          busy ? null : () => _cancelTask(id),
                      child: const Text('Cancel')),
              ],
            ),
          ],
        ),
      ),
    );
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // Foreground/background: pause the loop in background to save radio,
    // resume + refresh on foreground. No state is lost: Core is the source.
    if (state == AppLifecycleState.paused) {
      _loop?.cancel();
      _regRetry?.cancel();
    } else if (state == AppLifecycleState.resumed &&
        shouldResumeLoop(lifecycle.state)) {
      // Degraded included: backgrounding while degraded must not kill the
      // loop forever (Stage 11). _startLoop still refuses when critical.
      _startLoop();
    } else if (state == AppLifecycleState.resumed &&
        lifecycle.state == DeviceLifecycleState.registering) {
      // Register-while-unreachable: resume retries instead of stranding.
      _register();
    }
  }

  @override
  void dispose() {
    _loop?.cancel();
    _regRetry?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    codeCtrl.dispose();
    grantCtrl.dispose();
    _taskInputsCtrl.dispose();
    _taskTokenCtrl.dispose();
    super.dispose();
  }

  ZaraUiState get ui => ZaraUiState(
        connection: conn,
        lifecycle: lifecycle.state,
        deviceId: client.deviceId,
        batteryPct: battery.pct,
        charging: battery.charging,
        batteryLevel: battery.level(),
        network: network.state,
        voice: voice.state,
        approvalPending: notifCenter.hasPendingApprovals,
        lastError: lastError,
        updatedAt: DateTime.now(),
      );

  @override
  Widget build(BuildContext context) {
    final s = ui;
    return MaterialApp(
      title: 'Zara',
      theme: ThemeData(useMaterial3: true, colorSchemeSeed: Colors.deepPurple),
      home: Scaffold(
        appBar: AppBar(title: const Text('Zara')),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Text('Status: ${s.statusLine()}',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 4),
            Text(network.describe()),
            Text('Battery: ${battery.pct?.toStringAsFixed(0) ?? '?'}% '
                '(charging: ${battery.charging}, level: ${battery.level()})'),
            Text('Voice: ${voice.state} — wake phrase "$wakePhrase"'),
            Text('Assistant: ${assistant.describe()}'),
            Text('Mic permission: ${perms['microphone'] ?? '?'} | '
                'Notifications: ${perms['notifications'] ?? '?'}'),
            Text('Keys: $secureBackend'),
            if (lastError.isNotEmpty)
              Text('Error: $lastError',
                  style: const TextStyle(color: Colors.red)),
            const SizedBox(height: 12),
            if (lifecycle.state == DeviceLifecycleState.pairing ||
                lifecycle.state == DeviceLifecycleState.error ||
                lifecycle.state == DeviceLifecycleState.revoked ||
                lifecycle.state == DeviceLifecycleState.loggedOut) ...[
              TextField(controller: codeCtrl,
                  decoration: const InputDecoration(
                      labelText: 'Pairing code from Zara Core')),
              const SizedBox(height: 8),
              FilledButton(
                  onPressed: _connect, child: const Text('Connect “Hey Zara” device')),
              const SizedBox(height: 8),
            ],
            if (notifCenter.hasPendingApprovals) ...[
              Text('Approval required',
                  style: Theme.of(context).textTheme.titleMedium),
              for (final n in notifCenter.pending
                  .where((x) => x.channel == 'zara_approvals'))
                Card(
                  child: ListTile(
                    title: Text(n.title),
                    subtitle: Text(n.body),
                    trailing: n.executionId.isEmpty
                        ? null
                        : Row(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              TextButton(
                                  onPressed: () =>
                                      _decide(n, n.executionId, true),
                                  child: const Text('Approve')),
                              TextButton(
                                  onPressed: () =>
                                      _decide(n, n.executionId, false),
                                  child: const Text('Deny')),
                            ],
                          ),
                  ),
                ),
            ],
            for (final n in notifCenter.pending
                .where((x) => x.channel != 'zara_approvals'))
              ListTile(
                  dense: true,
                  leading: const Icon(Icons.notifications_outlined),
                  title: Text(n.title),
                  subtitle: Text(n.body)),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                OutlinedButton(
                    onPressed: () => _requestMic(),
                    child: const Text('Enable mic')),
                OutlinedButton(
                    onPressed: () async {
                      final m = await bridge.requestNotifPermission();
                      if (mounted) {
                        setState(
                            () => perms['notifications'] = m['granted']);
                        try {
                          await client.describeCapabilities(
                              androidCapabilityDocs(
                            micPermission: perms['microphone'] == true,
                            notifPermission:
                                perms['notifications'] == true,
                            forceStopGranted:
                                perms['forceStop'] == true,
                            a11yReady: a11yReady,
                          ));
                        } catch (_) {/* next boot retries */}
                      }
                    },
                    child: const Text('Enable notifications')),
                OutlinedButton(
                    onPressed: _logout, child: const Text('Sign out')),
              ],
            ),
            const SizedBox(height: 12),
            Text('Voice diagnostic (physical)',
                style: Theme.of(context).textTheme.titleMedium),
            Text(voiceDiag, style: Theme.of(context).textTheme.bodyMedium),
            if (voiceCapture.isNotEmpty) Text(voiceCapture),
            if (voiceStt.isNotEmpty) Text(voiceStt),
            if (voiceTranscript.isNotEmpty) Text('Heard: $voiceTranscript'),
            if (voiceReply.isNotEmpty) Text('Zara: $voiceReply'),
            if (voicePlayback.isNotEmpty) Text(voicePlayback),
            if (voiceChatReply.isNotEmpty) Text('Chat reply: $voiceChatReply'),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                FilledButton(
                    onPressed: (voiceBusy ||
                            lifecycle.state != DeviceLifecycleState.online)
                        ? null
                        : _recordTest,
                    child: const Text('Record 5s test')),
                OutlinedButton(
                    onPressed: (voiceBusy || voiceReply.isEmpty)
                        ? null
                        : _speakLast,
                    child: const Text('Speak reply')),
                OutlinedButton(
                    onPressed: (voiceBusy || voiceTranscript.isEmpty)
                        ? null
                        : _sendTranscriptToChat,
                    child: const Text('Send transcript to chat')),
                OutlinedButton(
                    onPressed: voiceBusy ? _stopVoice : null,
                    child: const Text('Stop')),
              ],
            ),
            const SizedBox(height: 12),
            Text('Wake-word opt-in (foundation)',
                style: Theme.of(context).textTheme.titleMedium),
            const Text('Explicit opt-in only (off by default, this session '
                'only). Needs mic permission plus a healthy battery; the '
                'wake engine flag below decides what is claimed.'),
            const SizedBox(height: 4),
            WakeOptInPanel(
              state: wakeOptIn,
              onRequestMic: _requestMic,
              onRefresh: _refresh,
            ),
            const SizedBox(height: 12),
            Text('Transfer diagnostic (physical)',
                style: Theme.of(context).textTheme.titleMedium),
            Text(xferDiag, style: Theme.of(context).textTheme.bodyMedium),
            if (xferDetail.isNotEmpty) Text(xferDetail),
            TextField(
              controller: grantCtrl,
              decoration: const InputDecoration(
                labelText: 'files.transfer grant ID (operator-issued)',
                hintText: 'paste live grant or leave empty to see refusal',
              ),
            ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                FilledButton(
                    onPressed: (xferBusy ||
                            lifecycle.state != DeviceLifecycleState.online)
                        ? null
                        : _xferSend,
                    child: const Text('Send test files')),
                OutlinedButton(
                    onPressed: (xferBusy ||
                            lifecycle.state != DeviceLifecycleState.online)
                        ? null
                        : _xferFetch,
                    child: const Text('Fetch pending')),
                OutlinedButton(
                    onPressed: xferBusy ? _xferCancel : null,
                    child: const Text('Stop')),
              ],
            ),
            const SizedBox(height: 12),
            Text('Tasks (Core executions)',
                style: Theme.of(context).textTheme.titleMedium),
            const Text('Async tools via Core. Approvals still resolve in '
                'the Approval section above (device-scoped, never local).'),
            const SizedBox(height: 8),
            DropdownButtonFormField<String>(
              initialValue: _taskTool,
              decoration:
                  const InputDecoration(labelText: 'Tool (safe list only)'),
              items: [
                for (final t in _ZaraAppState.taskTools)
                  DropdownMenuItem(value: t, child: Text(t)),
              ],
              onChanged: (v) {
                if (v != null) setState(() => _taskTool = v);
              },
            ),
            TextField(
              controller: _taskInputsCtrl,
              decoration: InputDecoration(
                labelText: 'Inputs (JSON object)',
                hintText: '{} or {"text": "hi"}',
                errorText: _taskFormError.isEmpty ? null : _taskFormError,
              ),
              maxLines: 3,
            ),
            TextField(
              controller: _taskTokenCtrl,
              obscureText: true,
              decoration: const InputDecoration(
                labelText: 'Operator token (memory only)',
                hintText: 'Bearer for /v1/executions; 401 without it',
              ),
            ),
            const SizedBox(height: 8),
            CheckboxListTile(
              value: _taskUseResolve,
              controlAffinity: ListTileControlAffinity.leading,
              contentPadding: EdgeInsets.zero,
              title: const Text('Route via capability (resolve)'),
              subtitle: const Text(
                  'system.* submits go through POST /v1/executions/resolve '
                  '(Core decides the device); util.echo always uses direct '
                  'submit (local tool, no capability route).'),
              onChanged: (v) {
                if (v != null) setState(() => _taskUseResolve = v);
              },
            ),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                FilledButton(
                    onPressed: _taskBusy ? null : _submitTask,
                    child: const Text('Submit task')),
                if (_taskQueue.length > 0)
                  Text('Queued offline: ${_taskQueue.length}'),
              ],
            ),
            if (_taskError.isNotEmpty)
              Text('Tasks: $_taskError',
                  style: const TextStyle(color: Colors.red)),
            for (final t in _tasks) _taskCard(context, t),
            const SizedBox(height: 12),
            Text('Conversation (talk stream)',
                style: Theme.of(context).textTheme.titleMedium),
            const Text('Streaming replies via POST /v1/talk/stream (SSE); '
                '/v1/talk is the fallback. Approvals land in the Approval '
                'section above (device-scoped, never local).'),
            const SizedBox(height: 8),
            KeyedSubtree(
              key: _talkSectionKey,
              child: TalkStreamPanel(
                key: _talkKey,
                client: client,
                tokenCtrl: _taskTokenCtrl,
                sessionId: _talkSessionId,
                onApproval: (execId, reason) {
                notifCenter.show(DeviceNotification(
                  id: 'talk-$execId',
                  title: 'Approval required',
                  body: reason.isEmpty
                      ? 'Streamed turn needs a decision'
                      : reason,
                  executionId: execId,
                ));
                if (mounted) setState(() {});
              },
            ),
            ),
            const SizedBox(height: 12),
            Text('Memory review',
                style: Theme.of(context).textTheme.titleMedium),
            const Text('Search + forget + purge via the operator memory '
                'contracts (operator token from Tasks section). Pending '
                'candidates load on manual Refresh; Accept/Reject confirms '
                'then reloads the list.'),
            const SizedBox(height: 8),
            MemoryReviewPanel(client: client, tokenCtrl: _taskTokenCtrl),
            const SizedBox(height: 12),
            Text('History',
                style: Theme.of(context).textTheme.titleMedium),
            const Text('Past conversations via the operator session '
                'contracts (operator token from Tasks section). Read-only '
                'browsing plus explicit delete; opening history never sends '
                'new prompts. Continue resumes a session in the Conversation '
                'section above (same session id, transcript read-only).'),
            const SizedBox(height: 8),
            HistoryPanel(
                client: client,
                tokenCtrl: _taskTokenCtrl,
                onResume: _resumeTalk),
            const SizedBox(height: 12),
            Text('Diagnostics',
                style: Theme.of(context).textTheme.titleMedium),
            const Text('Connection + provider (display only) + last error + '
                'governor flags + the explicit one-tap live provider check '
                '(operator token from Tasks section). Only new calls are '
                'the public GET /v1/ready and the explicit POST '
                '/v1/diagnostics/provider-check (+ cancel).'),
            const SizedBox(height: 8),
            DiagnosticsPanel(
                client: client,
                connection: conn,
                lastError: lastError,
                tokenCtrl: _taskTokenCtrl),
            const SizedBox(height: 12),
            Text('Provider setup',
                style: Theme.of(context).textTheme.titleMedium),
            const Text('Which Core model provider is active and how the '
                'operator configures it. Read-only: no key entry here.'),
            const SizedBox(height: 8),
            ProviderSetupPanel(client: client),
            const SizedBox(height: 12),
            Text('Capabilities',
                style: Theme.of(context).textTheme.titleMedium),
            for (final c in androidCapabilities)
              ListTile(
                dense: true,
                leading: Icon(c.supported
                    ? Icons.check_circle
                    : Icons.circle_outlined),
                title: Text(c.name),
                subtitle: Text(c.note),
              ),
          ],
        ),
      ),
    );
  }
}
