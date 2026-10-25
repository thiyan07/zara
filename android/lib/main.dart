import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import 'app_open_consent.dart';
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
import 'device_panel.dart';
import 'history_view.dart';
import 'provider_setup.dart';
import 'job_runner.dart';
import 'messaging_review.dart';
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

/// Maps a pairing-claim failure to an operator-actionable message. Pure
/// for testing. Server truth (core/device_auth.py): codes are single-use,
/// 10-minute TTL, refused when revoked or already consumed. A 403 whose
/// body says "invalid or expired" is genuinely ambiguous server-side
/// (unknown hash), so the message names both causes instead of guessing.
String describeClaimError(Object e) {
  final s = e.toString();
  if (s.contains('404')) {
    return 'Core has no such pairing — wrong code, or Core restarted. '
        'Ask the operator for a fresh code.';
  }
  if (s.contains('403')) {
    // Order matters: the ambiguous server text "invalid or expired
    // pairing code" contains "expired" but NOT "pairing code expired",
    // so the exact TTL phrase must win before the generic contains().
    if (s.contains('pairing code expired')) {
      return 'Pairing code expired (10-minute TTL). '
          'Ask the operator for a fresh code.';
    }
    if (s.contains('revoked')) {
      return 'This device was revoked. '
          'Ask the operator to enroll it again for a fresh code.';
    }
    if (s.contains('already used')) {
      return 'Code already used — each code works once. '
          'Ask the operator for a fresh code.';
    }
    return 'Claim refused — unknown or expired code (codes last '
        '10 minutes and work once). Ask the operator for a fresh code.';
  }
  if (s.contains('401')) {
    return 'Operator auth rejected — check the backend address, then retry.';
  }
  return 'Could not reach Core ($s). Check the backend address and retry.';
}

/// One-line fleet presence/trust badge for the Paired-devices list.
/// Unknown shapes render as unknown — never as online. Pure for testing.
String describeFleetDevice(Map<String, dynamic> d) {
  final id = '${d['device_id'] ?? '?'}';
  final trust = '${d['trust'] ?? 'unknown'}';
  final presence = '${d['presence'] ?? 'unknown'}';
  final caps = d['capabilities'];
  final n = caps is List ? caps.length : 0;
  return '$id — trust:$trust presence:$presence caps:$n';
}

/// One Tasks-target option derived from a fleet view. `id == ''` is the
/// This-device default. Disabled options carry the concise reason.
/// Pure for testing.
class TaskTargetOption {
  final String id;
  final String label;
  final bool enabled;
  final String reason;
  const TaskTargetOption({
    required this.id,
    required this.label,
    required this.enabled,
    this.reason = '',
  });
}

/// Builds the Tasks "Run on" options from fleet discovery. The This-device
/// default is always first and always enabled (current self-targeting
/// behavior preserved). Remote eligibility is a client-side pre-check
/// ONLY — Core re-validates trust/presence/capability authoritatively at
/// submit and approve time, and this function never grants anything:
/// - trust must be trusted/temporarily_trusted (revoked/others disabled)
/// - presence must be online/degraded (offline/stale/unknown disabled;
///   degraded stays eligible: throttled-but-alive, Core gates load)
/// - the device must advertise the task tool, except util.echo which is a
///   local Core tool with no fabric route (self-only).
/// Unknown shapes and duplicates disable safely, never enable blindly.
/// Pure for testing.
List<TaskTargetOption> fleetTargetOptions({
  required List<Map<String, dynamic>> fleet,
  required String selfId,
  required String tool,
}) {
  final out = [
    const TaskTargetOption(
      id: '',
      label: 'This device (default)',
      enabled: true,
    ),
  ];
  final seen = <String>{selfId};
  for (final d in fleet) {
    final id = '${d['device_id'] ?? ''}';
    if (id.isEmpty || !seen.add(id) || id == selfId) continue;
    final trust = '${d['trust'] ?? 'unknown'}';
    final presence = '${d['presence'] ?? 'unknown'}';
    final caps = d['capabilities'];
    final capList = caps is List ? [for (final c in caps) '$c'] : <String>[];
    String? blocked;
    if (tool == 'util.echo') {
      // Local Core tool with no fabric route: pinning it at a device
      // would park an unclaimable job. Self-only, stated plainly.
      blocked = 'local tool — runs on Core (self-only)';
    } else if (trust == 'revoked') {
      blocked = 'revoked — re-enroll to repair';
    } else if (trust != 'trusted' && trust != 'temporarily_trusted') {
      blocked = 'not trusted ($trust)';
    } else if (presence == 'offline') {
      blocked = 'offline';
    } else if (presence == 'stale' || presence == 'unknown') {
      blocked = 'stale — waiting for heartbeat';
    } else if (!capList.contains(tool)) {
      blocked = 'lacks $tool';
    }
    out.add(
      TaskTargetOption(
        id: id,
        label: '$id — $trust/$presence (${capList.length} caps)',
        enabled: blocked == null,
        reason: blocked ?? '',
      ),
    );
  }
  return out;
}

/// Merges a fresh server execution view into the stored task row.
/// The client-side `requested_device` annotation survives refreshes and
/// cancels: server maps overwrite everything else authoritatively, but
/// never carry the request (so a merge must not drop it, and must never
/// promote it into `device_id`). Pure for testing.
Map<String, dynamic> mergeTaskRow(
  Map<String, dynamic> stored,
  Map<String, dynamic> fresh,
) {
  final requested = stored['requested_device'];
  final merged = Map<String, dynamic>.from(fresh);
  if (requested != null) merged['requested_device'] = requested;
  return merged;
}

/// Lines for a task card separating the REQUESTED target (client-side
/// annotation at submit time) from the AUTHORITATIVE executing device
/// (`device_id` in server results) plus substitution/verified status.
/// Never presents the requested device as the executing one: when the
/// server has not confirmed an executor, only the target line appears.
/// Pure for testing.
List<String> describeTaskDevices(Map<String, dynamic> raw) {
  final requested = '${raw['requested_device'] ?? ''}';
  final actual = '${raw['device_id'] ?? ''}';
  final binding = raw['binding'];
  final resolution = raw['resolution'];
  final substituted =
      (binding is Map && binding['substituted'] == true) ||
      (resolution is Map && resolution['substituted'] == true);
  final verified = raw['verified'] == true;
  final lines = <String>[];
  if (requested.isNotEmpty) lines.add('target: $requested');
  if (actual.isNotEmpty) {
    lines.add('runs on: $actual${verified ? ' (verified)' : ''}');
  }
  if (substituted && requested.isNotEmpty && actual.isNotEmpty) {
    lines.add('substituted: Core routed to $actual instead of $requested');
  }
  return lines;
}

/// 404 from Core on decide = execution unknown server-side (cancelled then
/// wiped by a Core restart, expired, or never existed): the card is stale
/// and must be dismissed with an explanation — never presented as pending,
/// never reported as success. Auth failures (401/403) are NOT stale: they
/// stay explicit and fail closed. Pure for testing.
bool isUnknownExecutionApproval(Object e) => e.toString().contains('404');

/// Post-approval poll-nudge guard. Pure for testing: a successful approve
/// triggers one immediate best-effort `_pollJobs` instead of waiting for
/// the next 30/60s tick — but only when the device can actually reach
/// Core right now (online + network, same guard as `_tick`). Denials,
/// failed decisions, and offline/degraded states never nudge (denials
/// dispatch nothing; offline/degraded polling belongs to the loop, which
/// owns backoff). Callers must swallow nudge errors: a failed poll must
/// never turn a successful approval into a reported failure.
bool shouldNudgePollAfterApproval({
  required bool approve,
  required bool succeeded,
  required String lifecycleState,
  required bool networkOnline,
}) =>
    approve &&
    succeeded &&
    networkOnline &&
    lifecycleState == DeviceLifecycleState.online;

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
    List.of(inputs.entries)..sort((a, b) => a.key.compareTo(b.key)),
  );
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
double? _asDouble(Object? v) => v is num && v.isFinite ? v.toDouble() : null;

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

/// R21-#3 contract (2026-10-23): the stale-offline tick's conn mapping
/// after a bridge re-probe. A FRESH read decides — never the cached
/// snapshot. (CoreLinkState members are String consts; conn is String.)
/// Pure for testing.
String coreLinkForNetwork(NetworkSnapshot s) =>
    s.online ? CoreLinkState.reconnecting : CoreLinkState.offline;

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
    defaultValue: 'http://10.0.2.2:8080',
  );
  final client = CoreClient(_coreUrl);
  final secureStore = SecureStore();
  // P6-c26: the phone-local per-app open consent store + surface.
  // Grants persist in SecureStore; the dialog is device-owned UI.
  final appOpenConsents = AppOpenConsents(SecureStore());
  final _appNavKey = GlobalKey<NavigatorState>();
  final lifecycle = DeviceLifecycle();
  final voice = AndroidVoiceSession();
  final wakeOptIn = WakeOptInState();
  final notifCenter = NotificationCenter();
  late final AndroidJobRunner jobs;
  final codeCtrl = TextEditingController();
  final retry = const RetryPolicy();

  String conn = CoreLinkState.disconnected;
  BatteryReport battery = BatteryReport(timestamp: DateTime.now());
  NetworkSnapshot network = NetworkSnapshot(
    'unavailable',
    timestamp: DateTime.now(),
  );
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
  // Stage 20 observed-devices snapshot (manual refresh; no background
  // polling — battery governance applies to discovery too).
  Map<String, dynamic>? observedSnapshot;
  Map<String, dynamic>? transferStatus; // Stage 21 mission status snapshot
  // Device action output: pressing a panel action sends ONE utterance
  // through the existing POST /v1/talk loop (Core routes it through
  // intent/policy/approval like every request — never a side channel).
  String deviceActionLabel = '';
  String deviceActionReply = '';
  // Stale-approval notice (reconciliation/404 dismissals). Display-only:
  // never a success claim, never masking lastError (auth/transport).
  String approvalNotice = '';
  // Background-resume opt-in (default off). Loaded from the native
  // marker at boot; the toggle writes it back through the bridge.
  // Foreground-service evaluation (2026-10): NOT justified. A persistent
  // FGS would need a permanent notification, battery exemption, and would
  // still be killed by battery optimization / background-start limits on
  // the user's Vivo + stock Android; it cannot provide unattended low-
  // latency agent operation. WorkManager is likewise deferrable-only
  // (quotas, no continuity). So recovery = opt-in tap-to-resume prompt
  // only; no FGS, no WorkManager chain, no mic at boot.
  bool _resumeOptIn = false;
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
  static const taskTools = [
    'system.battery',
    'system.network',
    'system.wifi.state',
    'system.bluetooth.state',
    'system.mobile_data.state',
    'system.airplane_mode.state',
    'system.wifi.set',
    'system.bluetooth.set',
    'system.airplane_mode.set',
    'util.echo',
  ];
  String _taskTool = 'system.battery';
  // Cross-device target for task submits: '' = This device (default,
  // preserves self-targeting). A fleet device id pins the submit through
  // the existing resolver device-pinning fields; Core re-validates
  // trust/presence/capability authoritatively and the row shows the
  // authoritative executor separately (see describeTaskDevices).
  String _taskTarget = '';
  // Primary navigation (Chat/Tasks/History/Settings). Conditional tab
  // content: only the active tab is in the tree. Talk panel state is
  // preserved across switches by its GlobalKey; form controllers live on
  // this state object, so drafts survive tab switches.
  int _tab = 0;

  /// Pending approval count for the Chat destination badge.
  int get _pendingApprovalCount =>
      notifCenter.pending.where((x) => x.channel == 'zara_approvals').length;
  final _taskInputsCtrl = TextEditingController(text: '{}');
  final _taskTokenCtrl =
      TextEditingController(); // operator token, Keystore-remembered
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
  int _jobLoopGen = 0;
  bool _jobLoopOwner = false;
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
      a11yTap: (pkg, nodeId, [options]) => bridge.a11yTap(
        pkg,
        nodeId,
        options == null ? null : Map<String, dynamic>.from(options),
      ),
      a11ySetText: (pkg, nodeId, text, [options]) => bridge.a11ySetText(
        pkg,
        nodeId,
        text,
        options == null ? null : Map<String, dynamic>.from(options),
      ),
      a11yScroll: (pkg, nodeId, forward, [options]) => bridge.a11yScroll(
        pkg,
        nodeId,
        forward,
        options == null ? null : Map<String, dynamic>.from(options),
      ),
      a11yNavigate: (dir, pkg, [options]) => bridge.a11yNavigate(
        dir,
        pkg,
        options == null ? null : Map<String, dynamic>.from(options),
      ),
      a11yFind: (pkg, target, [options]) => bridge.a11yFind(
        pkg,
        target,
        options == null ? null : Map<String, dynamic>.from(options),
      ),
      appsList: (limit) => bridge.appList(limit: limit),
      observedDevices: () => bridge.observedDevices(),
      mediaCounts: () => bridge.mediaCounts(),
      notifList: (params) => bridge.notifList(params),
      // Stage 31-P3 phase A quick/one-shot actions.
      torchSet: (on) => bridge.torchSet(on),
      streamVolumeSet: (pct) => bridge.streamVolumeSet(pct),
      findMyPhoneRing: (seconds, bypass) =>
          bridge.findMyPhoneRing(seconds, bypass),
      mediaKeyEvent: (action) => bridge.mediaKeyEvent(action),
      clipboardSet: (text) => bridge.clipboardSet(text),
      openUri: (uri) => bridge.openUri(uri),
      powerSaverSet: (on) => bridge.powerSaverSet(on),
      alarmSet: (hour, minute, label) => bridge.alarmSet(
        hour,
        minute,
        label,
      ),
      timerSet: (minutes, label) => bridge.timerSet(
        minutes,
        label,
      ),
      // P5 feature-hunt ② (2026-10-22): DND state + toggle gateways.
      dndGet: () => bridge.dndGet(),
      dndSet: (enabled) => bridge.dndSet(enabled),
      // Stage 31-P3 phase B (P3d): call with contact resolution.
      contactsLookup: (name, limit) => bridge.contactsLookup(name,
          limit: limit),
      callDirect: (number) => bridge.callDirect(number),
      callScreenPoll: (waitMs) => bridge.callScreenPoll(waitMs: waitMs),
      // Stage 31-P3 phase B (P3e): native carrier SMS.
      smsSend: (number, text) => bridge.smsSend(number, text),
      // Stage 31-P3 phase B (P3f): consented fresh-ring OTP scan.
      otpScan: (maxAgeMs, senderHint) =>
          bridge.otpScan(maxAgeMs, senderHint),
      // Stage 31-P3 phase B (P3a): targeted media control.
      mediaSessionTargeted: (hint, action) =>
          bridge.mediaSessionTargeted(hint, action),
      // Stage 31-P3 phase B (P3c): pull-only photo inventory.
      photosInventory: () => bridge.photosInventory(),
      wifiState: () => bridge.wifiState(),
      bluetoothState: () => bridge.bluetoothState(),
      hotspotState: () => bridge.hotspotState(),
      mobileDataState: () => bridge.mobileDataState(),
      airplaneModeState: () => bridge.airplaneModeState(),
      openSettings: (screen) => bridge.openSettings(screen),
      openApp: (pkg) => bridge.openApp(pkg),
      // P6-c26: the consent SURFACE — a real dialog on this phone.
      // No navigator (app backgrounded/unmounted) resolves honestly
      // to null, which the runner treats as "not granted".
      appConsent: (pkg) async {
        final nav = _appNavKey.currentState;
        if (nav == null) return null;
        return await showDialog<bool>(
          context: nav.context,
          barrierDismissible: false,
          builder: (ctx) => AlertDialog(
            title: const Text('Allow opening this app?'),
            content: Text(
                'Zara asks to open "$pkg" on this phone. The ask was '
                'approved once in Core; this device-side consent '
                'remembers the app for future opens (revocable) — '
                'nothing is being copied, deleted, or messaged.'),
            actions: [
              TextButton(
                onPressed: () => Navigator.of(ctx).pop(false),
                child: const Text('Never'),
              ),
              FilledButton(
                onPressed: () => Navigator.of(ctx).pop(true),
                child: const Text('Allow'),
              ),
            ],
          ),
        );
      },
      appOpenConsents: appOpenConsents,
      setWifi: (enabled) => bridge.setWifi(enabled),
      setBluetooth: (enabled) => bridge.setBluetooth(enabled),
      setHotspot: (enabled) => bridge.setHotspot(enabled),
      setMobileData: (enabled) => bridge.setMobileData(enabled),
      setAirplaneMode: (enabled) => bridge.setAirplaneMode(enabled),
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
    // Detector start check: probe the app-private model dir first so
    // the error names exactly what is missing; without an on-device
    // engine every path still refuses honestly. Runs only on explicit
    // user action (Settings toggle), never at boot, never background.
    wakeOptIn.onStartListening = () async =>
        describeWakeModelError(await bridge.wakeModelStatus());
    _boot();
  }

  void _render() {
    if (mounted) setState(() {});
  }

  Future<void> _boot() async {
    await bridge.createNotificationChannels();
    try {
      _resumeOptIn = await bridge.resumeOptIn();
    } catch (_) {
      /* default stays off */
    }
    try {
      // Restore the persisted wake preference WITHOUT starting anything:
      // listening never auto-starts at boot (explicit foreground action
      // only). Absent/unreadable reads as OFF.
      wakeOptIn.loadPersisted(await bridge.wakeOptIn());
    } catch (_) {
      /* default stays off */
    }
    await _refresh();
    secureBackend = secureStore.backend;
    // ignore: avoid_print
    print('zara:secure-backend=$secureBackend');
    final creds = await secureStore.loadDeviceCredentials();
    // User decision 2026-10-22: the operator token is remembered on
    // device (Keystore) — restored BEFORE the lifecycle branch so it is
    // ready even while pairing (talk works the moment pairing ends).
    final savedToken = await secureStore.loadOperatorToken();
    _taskTokenCtrl.text = savedToken ?? '';
    client.operatorToken = _taskTokenCtrl.text.trim().isEmpty
        ? null
        : _taskTokenCtrl.text.trim();
    if (creds.deviceId == null || creds.deviceKey == null) {
      lifecycle.move(DeviceLifecycleState.pairing);
      _render();
      return;
    }
    client.deviceId = creds.deviceId;
    client.deviceKey = creds.deviceKey;
    client.deviceSecret = creds.deviceSecret; // null => raw-key transport
    lifecycle.restore(creds.deviceId);
    await _register();
  }

  Future<void> _refresh() async {
    final b = await bridge.battery();
    final n = await bridge.network();
    Map<String, dynamic> v = {};
    try {
      v = await bridge.voiceSupport();
    } catch (_) {
      /* keep static table */
    }
    try {
      final obs = await bridge.observedDevices();
      observedSnapshot = obs; // honest snapshot (supported:false included)
    } catch (_) {
      observedSnapshot = {'supported': false};
    }
    try {
      transferStatus = await client.transferMissions();
    } catch (_) {
      transferStatus = null; // honest absence: no mission card rendered
    }
    try {
      final t = await bridge.audioSelfTest();
      // ignore: avoid_print
      print('zara:audio-selftest=$t');
    } catch (_) {
      /* self-test best-effort */
    }
    // Stage 31-P3 phase B (P3a): the labeled self-test media session
    // (Id for the production targeted-control gate). Released on app
    // death; 'release' is one bridge call away. Never claims other
    // apps' sessions.
    try {
      final t = await bridge.mediaSelftest(true);
      // ignore: avoid_print
      print('zara:media-selftest=$t');
    } catch (_) {
      /* selftest best-effort */
    }
    try {
      perms = await bridge.permissions();
    } catch (_) {
      /* keep last */
    }
    try {
      // Resource telemetry (observation only): cheap synchronous getters
      // riding the existing tick. Missing keys = unavailable, never zero.
      telem = await bridge.telemetry();
    } catch (_) {
      /* keep last */
    }
    try {
      a11y = await bridge.a11yProbe();
    } catch (_) {
      /* keep last: not-ready reads as unavailable downstream */
    }
    try {
      final am = await bridge.assistantStatus();
      assistant = AssistantStatus.fromMap(am);
      // ignore: avoid_print
      print(
        'zara:assistant-status'
        ' registered=${assistant.serviceRegistered}'
        ' default=${assistant.zaraIsDefault}'
        ' roleAvail=${assistant.roleAvailable}'
        ' roleHeld=${assistant.roleHeld}'
        ' current=${assistant.currentAssistant}'
        ' infoValid=${am['service_info_valid']}'
        ' supportsAssist=${am['supports_assist']}'
        ' infoError=${am['service_info_error']}',
      );
    } catch (_) {
      /* keep last */
    }
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
      powerSave: telem['power_save'] is bool
          ? telem['power_save'] as bool
          : false,
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
        final desc = await client.describeCapabilities(
          androidCapabilityDocs(
            micPermission: perms['microphone'] == true,
            notifPermission: perms['notifications'] == true,
            forceStopGranted: perms['forceStop'] == true,
            a11yReady: a11yReady,
          ),
        );
        // ignore: avoid_print
        print(
          'zara:describe ok accepted=${desc['accepted']} '
          'rejected=${desc['rejected']}',
        );
      } catch (e) {
        // ignore: avoid_print
        print('zara:describe failed: $e');
      }
      _regAttempts = 0;
      // Physical-device fix: re-register runs on retry loops while the
      // state machine is already online (re-pair, reg retry). Re-asserting
      // the current state is a no-op, never an illegal transition; any
      // *different* illegal target still throws (see
      // stage9_lifecycle_test.dart).
      lifecycle.moveIfChanged(DeviceLifecycleState.online);
      setState(() {
        conn = CoreLinkState.online;
        lastError = '';
        _failures = 0;
      });
      _startLoop();
    } on AuthException catch (e) {
      // Same halt as the tick path: revoked keys must not keep any timer
      // alive. Re-pair (_connect -> _register) restarts the loop.
      _loop?.cancel();
      _stopJobLoop();
      _regRetry?.cancel();
      await wakeOptIn.forceStandby();
      await secureStore.clearDeviceCredentials();
      lifecycle.handleHttp(403, hasIdentity: true);
      setState(() {
        conn = CoreLinkState.error;
        lastError = 'revoked: $e';
        _tab = 0; // recovery action (pairing) lives on Chat
      });
    } catch (e) {
      // Register-while-unreachable (Stage 11 fix): without a retry the
      // lifecycle strands in `registering` forever — no loop ever starts.
      // Timer-driven bounded backoff; cancelled on pause/dispose.
      _regAttempts += 1;
      _regRetry?.cancel();
      // Jittered bounded backoff: equal-jitter over the attempt delay so
      // post-outage reconnects spread out instead of hammering Core.
      final wait = applyJitter(
        registerRetryDelay(_regAttempts),
        Random().nextDouble(),
      );
      _regRetry = Timer(wait, () {
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
    // NOTE: the job loop is intentionally NOT stopped here. _startLoop
    // re-runs from _register() on every tick, and stopping the long-poll
    // hold each tick would serialise job claims at tick boundaries
    // (measured live: bimodal ~1.2 s / ~30 s claim latency). Stopping
    // happens only at the explicit halt sites (_stopJobLoop callers:
    // auth revoke/wipe, logout, app pause, dispose).
    // Battery-critical: no auto loop; user reconnects manually.
    // (Loop may still be restarted later; _tick re-checks every round.)
    if (battery.level() == AndroidBatteryLevel.critical) return;
    final interval = battery.reduceBackgroundWork
        ? const Duration(seconds: 60)
        : const Duration(seconds: 30);
    _loop = Timer.periodic(interval, (_) => _tick());
    // Device jobs no longer wait for the next tick: the long-poll job
    // loop owns claim latency (see _jobLoop). _startJobLoop is a no-op
    // while an owner loop is running; heartbeats, capability refresh,
    // notifications stay on the periodic tick.
    _startJobLoop();
    unawaited(_tick());
  }

  /// Device-job long-poll loop.
  ///
  /// Replaces per-tick job polling. Each round issues ONE bounded
  /// server-held poll (wait_secs below), so the request rate stays equal
  /// to the old tick cadence (battery-neutral: same radio pattern), but a
  /// queued job is claimed within the hold window instead of after the
  /// next 30/60 s tick.
  ///
  /// Battery properties:
  /// - no busy loop, no extra wake-ups when idle (the hold IS the wait);
  /// - battery saver (reduceBackgroundWork) uses a longer hold (55 s) to
  ///   keep the deliberate 60 s cadence;
  /// - critical battery: loop never starts (same rule as _startLoop) and
  ///   exits at the next iteration boundary;
  /// - offline/degraded: bounded idle wait identical to the tick gap,
  ///   then re-check (no hammering);
  /// - old Core that ignores wait_secs: an instant empty response falls
  ///   back to the old tick-gap pacing (graceful degradation), so the
  ///   radio pattern never gets busier than today.
  ///
  /// Safety properties: every claim/post-claim re-check (revocation,
  /// cancellation) runs in Core exactly as before; job execution goes
  /// through the same device runner (`jobs.run`) as the tick path; auth
  /// failures stop the loop (the tick/re-pair path owns recovery);
  /// generation checking makes stop/restart race-free. Overlap note:
  /// during a stop→start transition the OLD loop may finish one more
  /// in-flight poll (≤ hold window) alongside the replacement — claim
  /// atomicity in Core makes that safe (same property as the legacy
  /// tick-poll + approval-nudge overlap), and the old loop exits at the
  /// next generation check without re-entering.
  Future<void> _jobLoop(int gen) async {
    var failures = 0;
    try {
      while (mounted && gen == _jobLoopGen) {
        if (battery.level() == AndroidBatteryLevel.critical) return;
        if (lifecycle.state != DeviceLifecycleState.online || !network.online) {
          // Offline/degraded: bounded idle wait, then re-check. Same
          // cadence as the tick loop; no spinning.
          await Future<void>.delayed(
            battery.reduceBackgroundWork
                ? const Duration(seconds: 60)
                : const Duration(seconds: 30),
          );
          continue;
        }
        try {
          final holdS = battery.reduceBackgroundWork ? 55.0 : 25.0;
          final sw = Stopwatch()..start();
          final ranJob = await _pollJobsOnce(CancelScope(), waitSecs: holdS);
          failures = 0;
          // Held = server consumed about the whole wait window empty.
          // Old servers return instantly; pace those at the old gap.
          final held =
              sw.elapsed >= Duration(milliseconds: (holdS * 800).round());
          if (!ranJob && !held) {
            final gap = battery.reduceBackgroundWork
                ? const Duration(seconds: 60)
                : const Duration(seconds: 30);
            final rest = gap - sw.elapsed;
            if (rest > Duration.zero) await Future<void>.delayed(rest);
          }
          // ranJob or held: the pacing already happened — loop immediately.
        } on AuthException {
          // Revoked/invalid keys: stop; the tick/auth path wipes creds
          // and parks on pairing. No retries against dead keys.
          return;
        } catch (_) {
          // Bounded backoff on transport failures (same helper as
          // registration retries; equal-jitter so reconnects spread out).
          failures += 1;
          final wait = applyJitter(
            registerRetryDelay(failures),
            Random().nextDouble(),
          );
          await Future<void>.delayed(wait);
        }
      }
    } finally {
      // Only the loop owning the CURRENT generation releases ownership;
      // replacement loops (started after a stop) keep theirs.
      if (gen == _jobLoopGen) _jobLoopOwner = false;
    }
  }

  void _stopJobLoop() {
    // Generation bump: any in-flight long poll completes its current
    // request then exits without claiming further work. Ownership clears
    // immediately so the next start can spawn a fresh loop (the old
    // loop's exit must not clear a replacement's ownership).
    _jobLoopGen++;
    _jobLoopOwner = false;
  }

  void _startJobLoop() {
    // _startLoop is re-invoked from _register() on every tick and on each
    // (re)connect/retry: restarting the job loop every time would kill
    // the in-flight long-poll hold and serialise claims at tick
    // boundaries (measured live: bimodal ~1.2 s / ~30 s claim latency).
    // So start only when no owner loop is running; stopping happens at
    // the explicit halt sites (_stopJobLoop callers: auth revoke/wipe,
    // logout, app pause, dispose).
    if (_jobLoopOwner) return;
    if (battery.level() == AndroidBatteryLevel.critical) return;
    _jobLoopOwner = true;
    _jobLoopGen++;
    unawaited(_jobLoop(_jobLoopGen));
  }

  /// One bounded poll round: claim (long-)poll, execute, report.
  /// Returns true when a job ran. Never throws for malformed payloads.
  Future<bool> _pollJobsOnce(CancelScope scope, {double waitSecs = 0}) async {
    final raw = await client.pollJobs(cancel: scope, waitSecs: waitSecs);
    if (raw == null) return false;
    DeviceJob job;
    try {
      job = DeviceJob.fromJson(raw);
    } catch (e) {
      return false; // malformed: never execute; Core expires the claim
    }
    final out = await jobs.run(job);
    await client.reportJobResult(
      job.jobId,
      ok: out.ok,
      result: out.result,
      error: out.error,
    );
    return true;
  }

  Future<void> _tick() async {
    if (_busy || lifecycle.state != DeviceLifecycleState.online) return;
    if (!network.online) {
      // R21-#3 fix (2026-10-23): the boot-time probe can CACHE "offline"
      // behind a screen-off boot. A cached snapshot must never strand
      // the loop (the old guard returned forever here): re-probe the
      // bridge on this tick (cheap, bounded) and only report offline
      // when the FRESH read also says offline. A fresh online read
      // falls through into the normal tick, which settles conn=online.
      try {
        await _refresh();
      } catch (_) {
        /* keep the cached snapshot; the next tick re-probes again */
      }
      if (!network.online) {
        if (mounted) setState(() => conn = coreLinkForNetwork(network));
        return;
      }
    }
    _busy = true;
    final scope = CancelScope();
    try {
      await _refresh();
      // Re-describe if a11y probe state changed (permission-like gating).
      await _register();
      // Job polling lives in _jobLoop (bounded server-side long poll):
      // no per-tick poll here — the long poll claims queued jobs within
      // its hold window instead of waiting for the next tick (see
      // _jobLoop / Core agent_poll wait_secs contract).
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
        // Same guard: repeated low-battery heartbeats must not throw
        // degraded -> degraded on every poll.
        lifecycle.moveIfChanged(DeviceLifecycleState.degraded);
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
      // Revoked/invalid credentials: wipe, halt all retry timers, and park
      // on pairing. The loop restarts only via a successful re-pair
      // (_register -> _startLoop); nothing retries against dead keys.
      _loop?.cancel();
      _stopJobLoop();
      _regRetry?.cancel();
      await wakeOptIn.forceStandby();
      await secureStore.clearDeviceCredentials();
      lifecycle.handleHttp(403, hasIdentity: true);
      setState(() {
        conn = CoreLinkState.error;
        _tab = 0; // revoked: pair again on Chat
      });
    } on NotRegisteredException {
      // Backend restarted: re-register once, never loop.
      try {
        await client.register(advertisedCapabilities(), 'zara-android 9.0.0');
      } catch (_) {
        /* next tick retries with backoff */
      }
    } catch (_) {
      _failures += 1;
      final waitLonger = !retry.shouldRetry(_failures, batteryCritical: false);
      setState(
        () => conn = waitLonger
            ? CoreLinkState.offline
            : CoreLinkState.reconnecting,
      );
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
    await client.reportJobResult(
      job.jobId,
      ok: out.ok,
      result: out.result,
      error: out.error,
    );
  }

  Future<void> _pullNotifications() async {
    final list = await client.fetchNotifications();
    var changed = false;
    for (final raw in list) {
      try {
        if (notifCenter.show(DeviceNotification.fromJson(raw))) {
          changed = true;
        }
      } catch (_) {
        /* skip malformed, keep polling */
      }
    }
    // Reconcile against the authoritative server list (this successful
    // fetch only — failures keep every card, fail-closed). Approval cards
    // the server no longer lists are stale (cancelled/decided/expired, or
    // wiped by a Core restart): dismiss locally with an explanation.
    // Genuine pending approvals are always server-listed, so they stay.
    final dismissed = notifCenter.reconcileApprovals(
      list.map((e) => '${e['id'] ?? ''}').toSet(),
    );
    if (dismissed.isNotEmpty) {
      approvalNotice =
          'stale approval${dismissed.length == 1 ? '' : 's'} removed — '
          'Core no longer lists ${dismissed.map((n) => n.executionId.isEmpty ? n.id : n.executionId).join(', ')} '
          'as pending (cancelled, decided, expired, or Core restarted); nothing was executed';
      changed = true;
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
    setState(() {
      lastError = '';
      approvalNotice = '';
    });
    try {
      await client.claim(codeCtrl.text.trim(), 'android-phone');
      await secureStore.saveDeviceCredentials(
        deviceId: client.deviceId ?? 'android-phone',
        deviceKey: client.deviceKey ?? '',
        deviceSecret: client.deviceSecret ?? '',
      );
      secureBackend = secureStore.backend;
      lifecycle.move(DeviceLifecycleState.enrolled);
      lifecycle.move(DeviceLifecycleState.registering);
      await _register();
    } on CoreException catch (e) {
      lifecycle.move(DeviceLifecycleState.error);
      setState(() {
        lastError = describeClaimError(e);
        _tab = 0; // pairing lives on Chat
      });
    }
  }

  /// Fleet discovery (Settings → Paired devices). Operator token required
  /// (same memory-only field as Tasks); 401/403 means the token is
  /// missing or wrong — never a device-credential problem.
  List<Map<String, dynamic>> _fleet = [];
  String _fleetError = '';
  bool _fleetLoading = false;
  String _enrollCode = '';
  final _enrollIdCtrl = TextEditingController(text: 'laptop-2');

  void _syncOperatorToken() {
    final token = _taskTokenCtrl.text.trim();
    client.operatorToken = token.isEmpty ? null : token;
  }

  /// User decision 2026-10-22: token entered ONCE is persisted to the
  /// Keystore-backed SecureStore (wiped with device credentials on
  /// logout/revocation). Persisting lives on the field's onChanged so
  /// action-time syncs never rewrite storage.
  void _persistOperatorToken() {
    _syncOperatorToken();
    final v = _taskTokenCtrl.text.trim();
    // fire-and-forget write; the restore path re-reads at next boot
    unawaited(secureStore.saveOperatorToken(v));
  }

  Future<void> _refreshFleet() async {
    _syncOperatorToken();
    setState(() {
      _fleetLoading = true;
      _fleetError = '';
    });
    try {
      final list = await client.fleetDevices();
      if (mounted) {
        setState(() {
          _fleet = list;
          _fleetLoading = false;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _fleetLoading = false;
          _fleetError = e is AuthException
              ? 'Operator token missing or rejected — set it on the Tasks tab.'
              : 'Fleet list failed: $e';
        });
      }
    }
  }

  Future<void> _enrollFleetDevice() async {
    _syncOperatorToken();
    final id = _enrollIdCtrl.text.trim();
    if (id.isEmpty) {
      setState(() => _fleetError = 'Enter a device id to enroll first.');
      return;
    }
    setState(() {
      _fleetError = '';
      _enrollCode = '';
    });
    try {
      final out = await client.enrollDevice(id);
      if (mounted) {
        setState(
          () => _enrollCode =
              '${out['pairing_code'] ?? ''} (single use, 10 minutes)',
        );
      }
    } catch (e) {
      if (mounted) {
        setState(
          () => _fleetError = e is AuthException
              ? 'Operator token missing or rejected — set it on the Tasks tab.'
              : 'Enroll failed: $e',
        );
      }
    }
  }

  Future<void> _revokeFleetDevice(String id) async {
    _syncOperatorToken();
    setState(() => _fleetError = '');
    try {
      final out = await client.revokeDevice(id);
      if (mounted) {
        setState(
          () => _fleetError =
              'Revoked $id (queued jobs cancelled: '
              '${out['queued_jobs_cancelled'] ?? 0}). List refreshed.',
        );
        await _refreshFleet();
      }
    } catch (e) {
      if (mounted) {
        setState(
          () => _fleetError = e is AuthException
              ? 'Operator token missing or rejected — set it on the Tasks tab.'
              : 'Revoke failed: $e',
        );
      }
    }
  }

  Future<void> _logout() async {
    _loop?.cancel();
    _stopJobLoop();
    _regRetry?.cancel();
    await wakeOptIn.forceStandby();
    try {
      await client.disconnect();
    } catch (_) {}
    await secureStore.clearDeviceCredentials();
    client.deviceId = null;
    client.deviceKey = null;
    client.deviceSecret = null;
    client.operatorToken = null;
    lifecycle.logout();
    setState(() {
      approvalNotice = '';
      conn = CoreLinkState.disconnected;
    });
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
      voiceCapture = 'mic: ${wav?.length ?? 0}B peak=$peakStr dBFS';
      setState(
        () => voiceDiag =
            'heard: "$voiceTranscript" (peak $peakStr dBFS) — reply spoken via TTS',
      );
      // ignore: avoid_print
      print(
        'zara:voice-turn transcript="${redactSecretsForDisplay(voiceTranscript)}" '
        'reply="${redactSecretsForDisplay(voiceReply)}" status=${res.status}',
      );
    } on VoiceFlowException catch (e) {
      setState(() => voiceDiag = '$e');
    } on AudioBridgeException catch (e) {
      setState(() => voiceDiag = 'capture: $e');
    } on CoreException catch (e) {
      setState(() {
        voiceDiag = 'core: $e';
        lastError = '$e';
      });
    } catch (e) {
      // Transport failures (unreachable/dropped/timed-out) and any other
      // unexpected error must complete the diagnostic explicitly — the UI
      // must never wedge on "recording…" and never imply success.
      setState(() => voiceDiag = 'voice failed: $e');
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
      if (mounted) {
        setState(() => voiceDiag = 'playback exited (software-only)');
      }
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
    } catch (e) {
      if (mounted) setState(() => voiceDiag = 'playback failed: $e');
    } finally {
      if (mounted) setState(() => voiceBusy = false);
    }
  }

  Future<void> _stopVoice() async {
    // Responsive first: the button always clears immediately, even if a
    // wedged platform leg makes interrupt() hang (interrupt legs are all
    // best-effort with their own bounds).
    if (mounted) setState(() => voiceDiag = 'stopped');
    await voiceFlow.interrupt();
  }

  /// Stage 21: mission cancellation — SAME routing rule as every device
  /// card action: one operator utterance through /v1/talk, so policy,
  /// approval (transfer.cancel is CONFIRM-level), and audit live in
  /// Core. Never a direct REST shortcut from the panel.
  Future<void> _cancelTransferMission() async {
    final token = _taskTokenCtrl.text.trim();
    client.operatorToken = token.isEmpty ? null : token;
    setState(() {
      deviceActionLabel = 'current transfer';
      deviceActionReply = 'asking Zara to cancel…';
    });
    try {
      final out = await client.talk(text: 'cancel the current transfer');
      if (!mounted) return;
      setState(() {
        deviceActionReply =
            redactSecretsForDisplay('${out['reply'] ?? out['result'] ?? 'no reply body'}');
      });
      await _refresh(); // truthful status after the cancel attempt
    } on CoreException catch (e) {
      if (!mounted) return;
      setState(() {
        deviceActionReply = '$e';
      });
    }
  }

  /// Device card action: one honest utterance through the talk loop.
  Future<void> _runDeviceAction(Map<String, dynamic> cmd) async {
    final row = Map<String, dynamic>.from(
      (cmd['row'] as Map?)?.cast<String, dynamic>() ?? const {},
    );
    final label = '${row['label'] ?? cmd['device'] ?? 'device'}';
    final String utterance;
    switch (cmd['action'] as String?) {
      case 'storage.volumes':
        utterance = 'how much space is on the drive "$label"';
      case 'storage.stats':
        utterance = 'storage stats on the drive "$label"';
      case 'storage.copy':
        utterance = 'copy a file to the drive "$label"';
      case 'storage.move':
        utterance = 'move a file to the drive "$label"';
      case 'display.report':
        utterance = 'what display devices are connected';
      default:
        utterance = 'what can I do with this device';
    }
    final token = _taskTokenCtrl.text.trim();
    client.operatorToken = token.isEmpty ? null : token;
    setState(() {
      deviceActionLabel = label;
      deviceActionReply = 'asking Zara…';
    });
    try {
      final out = await client.talk(text: utterance);
      if (!mounted) return;
      setState(() {
        deviceActionReply = redactSecretsForDisplay(
          '${out['reply'] ?? out['result'] ?? 'no reply body'}',
        );
      });
    } on CoreException catch (e) {
      if (!mounted) return;
      setState(() {
        deviceActionReply = '$e';
      });
    }
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
        await client.describeCapabilities(
          androidCapabilityDocs(
            micPermission: perms['microphone'] == true,
            notifPermission: perms['notifications'] == true,
            forceStopGranted: perms['forceStop'] == true,
            a11yReady: a11yReady,
          ),
        );
      } catch (_) {
        /* next boot retries */
      }
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
        text: text,
        deviceId: client.deviceId ?? 'android-phone',
      );
      if (!mounted) return;
      setState(() {
        voiceChatReply = '${out['reply'] ?? ''}';
        voiceDiag = 'transcript sent to chat';
      });
      // ignore: avoid_print
      print(
        'zara:voice-to-chat reply="${redactSecretsForDisplay(voiceChatReply)}"',
      );
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
            utf8.encode(stage15Text('vivo-to-laptop')),
          ),
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
          grantId: grant,
        );
        final tid = '${req['transfer_id']}';
        var state = '${req['state']}';
        final wins = chunkWindows(data.length, 64 * 1024);
        for (var seq = 0; seq < wins.length; seq++) {
          if (_xferStop) break;
          final w = wins[seq];
          final out = await client.xferChunk(
            tid,
            seq,
            Uint8List.sublistView(data, w[0], w[0] + w[1]),
          );
          state = '${out['state']}';
          if (mounted) {
            setState(
              () => xferDiag =
                  '$name: ${out['received_bytes']}/${out['size_bytes']}B…',
            );
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
      final pend = await client.xferPending() as Map<String, dynamic>? ?? {};
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
          lines.add(
            '↓ ${m['filename']} id=$tid MISMATCH '
            '(no ack sent)',
          );
          // ignore: avoid_print
          print('zara:xfer-fetch $tid MISMATCH local=$hex want=$want');
          continue;
        }
        final ack = await client.xferAck(tid, hex);
        lines.add(
          '↓ ${m['filename']} id=$tid ${data.length}B '
          'sha=${hex.substring(0, 16)}… verified=${ack['verified']}',
        );
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
    final token = _taskTokenCtrl.text.trim();
    client.operatorToken = token.isEmpty ? null : token;
    final selfId = client.deviceId ?? 'android-phone';
    // Requested target: This-device default preserves self-targeting.
    // A fleet id pins through the resolver's device-pinning fields;
    // Core re-validates authoritatively (approval/policy/risk unchanged).
    final target = _taskTarget.isEmpty ? selfId : _taskTarget;
    // Per tool+inputs+target duplicate guard: a second tap with the
    // identical payload while the first is still in flight is dropped
    // explicitly (never a second execution). Same tool+inputs to a
    // DIFFERENT device is a distinct key and proceeds. Core 409 remains
    // the server-side backstop for races that cross processes.
    final inflightKey = '$target:${taskSubmitKey(_taskTool, inputs)}';
    if (!_taskInflightKeys.add(inflightKey)) {
      setState(
        () => _taskError =
            'already submitting $_taskTool to $target with these inputs — wait for the first request',
      );
      return;
    }
    final useResolve = taskShouldResolve(
      tool: _taskTool,
      resolveEnabled: _taskUseResolve,
    );
    // One key per user action: transport retries and the offline drain
    // reuse it (Core collapses them); a fresh tap gets a fresh key.
    final idemKey = newIdempotencyKey(_taskTool);
    final payload = buildExecutionSubmitBody(
      tool: _taskTool,
      inputs: inputs,
      deviceId: target,
      idempotencyKey: idemKey,
    );
    if (!network.online) {
      // Offline queue stores the direct-submit envelope (resolve needs a
      // live Core resolver); the drain path sends via submitExecution.
      final queued = _taskQueue.add(
        PendingOp('task-submit', Map<String, dynamic>.from(payload)),
      );
      _taskInflightKeys.remove(inflightKey);
      setState(
        () => _taskError = queued
            ? 'offline — submit queued (${_taskQueue.length} pending, sends on next online submit)'
            : 'offline and task queue is full (${_taskQueue.length}) — submit dropped, nothing sent',
      );
      return;
    }
    setState(() => _taskBusy = true);
    try {
      await _flushTaskQueue();
      if (target != selfId) {
        // Recheck the remote target immediately before submission: the
        // fleet snapshot may be stale (device went offline, was revoked,
        // lost trust, or dropped the capability since the list loaded).
        // A dedicated throwing fetch (not _refreshFleet, which reports
        // into state): refresh failure aborts loudly — never submit
        // blind, never silently fall back to another device.
        try {
          final fresh = await client.fleetDevices();
          if (!mounted) return;
          setState(() {
            _fleet = fresh;
            _fleetError = '';
          });
        } catch (e) {
          if (mounted) {
            setState(() {
              _fleetError = 'fleet recheck failed: $e';
              _taskError =
                  'target $target not rechecked (fleet refresh failed) — nothing submitted';
            });
          }
          return;
        }
        final options = fleetTargetOptions(
          fleet: _fleet,
          selfId: selfId,
          tool: _taskTool,
        );
        final match = options.where((o) => o.id == target).toList();
        if (match.isEmpty || !match.first.enabled) {
          if (mounted) {
            setState(
              () => _taskError = match.isEmpty
                  ? 'target $target left the fleet — nothing submitted (refresh the list)'
                  : 'target $target unavailable: ${match.first.reason} — nothing submitted',
            );
          }
          return;
        }
      }
      final out = useResolve
          ? await client.resolveExecution(
              capability: _taskTool,
              inputs: inputs,
              deviceId: target,
              idempotencyKey: idemKey,
            )
          : await client.submitExecution(
              tool: _taskTool,
              inputs: inputs,
              deviceId: target,
              idempotencyKey: idemKey,
            );
      if (!mounted) return;
      setState(() {
        if (out.containsKey('id')) {
          // Client-side annotation only: the REQUESTED target, kept next
          // to (never merged into) the authoritative device_id that later
          // refreshes overwrite from server results.
          out['requested_device'] = target;
          _tasks.insert(0, out);
          if (_tasks.length > 20) _tasks.removeLast();
        } else {
          // Deferred gate (or unexpected envelope): no execution exists.
          _taskError =
              'submit: ${out['status'] ?? 'no id'} — ${out['reason'] ?? out}';
        }
      });
    } on CoreException catch (e) {
      if (mounted) {
        setState(
            () => _taskError =
                'submit ${describeExecutionError(e)}: $e');
      }
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
            : null,
      );
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
          _tasks[i] = mergeTaskRow(_tasks[i], out);
        } else {
          _tasks.insert(0, out);
        }
      });
    } on CoreException catch (e) {
      if (!mounted) return;
      final n = (_taskRefreshFailures[id] ?? 0) + 1;
      _taskRefreshFailures[id] = n;
      final withinBudget = retry.shouldRetry(
        n,
        batteryCritical: battery.level() == AndroidBatteryLevel.critical,
      );
      setState(
        () => _taskError =
            'refresh $id ${describeExecutionError(e)}: $e'
            '${withinBudget ? '' : ' (retry budget exhausted — check Core, then refresh again)'}',
      );
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
          _tasks[i] = mergeTaskRow(_tasks[i], out);
        } else {
          _tasks.insert(0, out);
        }
      });
    } on CoreException catch (e) {
      if (mounted) {
        setState(
          () => _taskError = 'cancel $id ${describeExecutionError(e)}: $e',
        );
      }
    } catch (e) {
      if (mounted) setState(() => _taskError = 'cancel $id transport: $e');
    } finally {
      _cancelling.remove(id);
      if (mounted) setState(() {});
    }
  }

  Future<void> _decide(
    DeviceNotification n,
    String execId,
    bool approve,
  ) async {
    // One in-flight decision per execution: double-taps collapse into the
    // first request. Core 409 remains the real duplicate-execution guard.
    if (!_deciding.add(execId)) return;
    try {
      await client.decideApproval(execId, approve);
      notifCenter.ack(n.id);
      try {
        await client.ackNotification(n.id);
      } catch (_) {}
      // Approval just granted a dispatch: poll jobs immediately instead of
      // waiting for the next 30/60s tick. Guarded by
      // shouldNudgePollAfterApproval (online + network) and best-effort —
      // a poll failure must never mask the approval outcome.
      if (shouldNudgePollAfterApproval(
        approve: approve,
        succeeded: true,
        lifecycleState: lifecycle.state,
        networkOnline: network.online,
      )) {
        try {
          await _pollJobs(CancelScope());
        } catch (_) {}
      }
    } on CoreException catch (e) {
      // 409 = Core already decided (approved/denied/expired elsewhere):
      // the card is stale, so dismiss it locally instead of nagging.
      // 404 = Core never knew it (cancelled + restarted, expired, or
      // missing): dismiss with an explanation — never keep it presented
      // as pending, never report success. Anything else (401/403 auth,
      // transport) keeps the card for retry with an explicit error.
      if (isStaleApproval(e)) {
        notifCenter.ack(n.id);
        approvalNotice =
            'stale approval removed — ${n.executionId.isEmpty ? n.id : n.executionId} '
            'was already decided elsewhere; nothing further to do';
        try {
          await client.ackNotification(n.id);
        } catch (_) {}
        if (mounted) setState(() {});
        return;
      }
      if (isUnknownExecutionApproval(e)) {
        notifCenter.ack(n.id);
        approvalNotice =
            'stale approval removed — ${n.executionId.isEmpty ? n.id : n.executionId} '
            'is unknown on Core (cancelled, expired, or Core restarted); nothing was executed';
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
  void _resumeTalk(String sessionId, List<Map<String, dynamic>> messages) {
    if (sessionId.isEmpty) return;
    final gen = ++_talkResumeGen;
    setState(() => _talkSessionId = sessionId);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || gen != _talkResumeGen) return; // stale: discard
      _talkKey.currentState?.loadHistory(messages, generation: gen);
      final ctx = _talkSectionKey.currentContext;
      if (ctx != null) {
        unawaited(
          Scrollable.ensureVisible(
            ctx,
            duration: const Duration(milliseconds: 300),
          ),
        );
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
    final jobState = raw['job_state'] == null ? '—' : '${raw['job_state']}';
    final error = '${raw['error'] ?? ''}';
    final result = raw['result'];
    final approval = v?.needsApproval ?? false;
    final terminal = v?.isTerminal ?? isExecutionTerminal(status);
    final busy = _refreshing.contains(id) || _cancelling.contains(id);
    final shortId = id.length <= 12 ? id : '${id.substring(0, 12)}…';
    // Accessibility: the card exposes one summary label (tool + status +
    // requested/executor) so screen readers and UI automation identify
    // the row without coordinate taps; buttons carry stable keys.
    return Semantics(
      container: true,
      label:
          'Task $tool, status $status, target '
          '${describeTaskDevices(raw).join('; ')}',
      child: Card(
        key: ValueKey('task-card-$id'),
        child: Padding(
          padding: const EdgeInsets.all(8),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                '$tool · $shortId',
                style: Theme.of(context).textTheme.titleSmall,
              ),
              Text('status: $status (job: $jobState)'),
              // Requested target vs authoritative executor: the executor
              // comes only from server results (device_id); the request is
              // a client annotation. Never conflated (see
              // describeTaskDevices).
              for (final line in describeTaskDevices(raw)) Text(line),
              if (approval)
                const Text(
                  'Approval required — see Approval section above',
                  style: TextStyle(color: Colors.orange),
                ),
              if ((v?.cancelRequested ?? raw['cancel_requested'] == true))
                const Text('cancel requested…'),
              if (result != null) Text('result: $result'),
              if (error.isNotEmpty)
                Text(
                  'error: $error',
                  style: const TextStyle(color: Colors.red),
                ),
              if (v == null) Text('raw: $raw'),
              const SizedBox(height: 4),
              Wrap(
                spacing: 8,
                children: [
                  OutlinedButton(
                    key: ValueKey('task-refresh-$id'),
                    onPressed: busy ? null : () => _refreshTask(id),
                    child: const Text('Refresh'),
                  ),
                  if (!terminal)
                    OutlinedButton(
                      key: ValueKey('task-cancel-$id'),
                      onPressed: busy ? null : () => _cancelTask(id),
                      child: const Text('Cancel'),
                    ),
                ],
              ),
            ],
          ),
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
      _stopJobLoop();
      _regRetry?.cancel();
      // Foreground-only wake detection: backgrounding stops the listener
      // (generation bump drops late events); re-enable explicitly after
      // resume. Never auto-restarts.
      unawaited(wakeOptIn.forceStandby());
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
    _stopJobLoop();
    _regRetry?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    codeCtrl.dispose();
    grantCtrl.dispose();
    _taskInputsCtrl.dispose();
    _taskTokenCtrl.dispose();
    _enrollIdCtrl.dispose();
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
      navigatorKey: _appNavKey,
      theme: ThemeData(useMaterial3: true, colorSchemeSeed: Colors.deepPurple),
      home: Scaffold(
        appBar: AppBar(title: const Text('Zara')),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (_tab == 0) ...[
              Text(
                'Status: ${s.statusLine()}',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const SizedBox(height: 4),
              Text(network.describe()),
              Text(
                'Battery: ${battery.pct?.toStringAsFixed(0) ?? '?'}% '
                '(charging: ${battery.charging}, level: ${battery.level()})',
              ),
              DeviceObservedPanel(
                snapshot: observedSnapshot,
                onRefresh: _refresh,
                onAction: (cmd) =>
                    _runDeviceAction(Map<String, dynamic>.from(cmd)),
              ),
              TransferMissionPanel(
                status: transferStatus,
                onCancel: () => _cancelTransferMission(),
              ),
              if (deviceActionReply.isNotEmpty) ...[
                Text(
                  'Device action ($deviceActionLabel):',
                  style: const TextStyle(fontSize: 12),
                ),
                Text(deviceActionReply),
              ],
              if (lastError.isNotEmpty)
                Text(
                  'Error: $lastError',
                  style: const TextStyle(color: Colors.red),
                ),
              const SizedBox(height: 12),
              if (lifecycle.state == DeviceLifecycleState.pairing ||
                  lifecycle.state == DeviceLifecycleState.error ||
                  lifecycle.state == DeviceLifecycleState.revoked ||
                  lifecycle.state == DeviceLifecycleState.loggedOut) ...[
                TextField(
                  controller: codeCtrl,
                  decoration: const InputDecoration(
                    labelText: 'Pairing code from Zara Core',
                  ),
                ),
                const SizedBox(height: 8),
                FilledButton(
                  onPressed: _connect,
                  child: const Text('Connect “Hey Zara” device'),
                ),
                const SizedBox(height: 8),
              ],
              // Stale-approval explanation lives outside the pending block:
              // after reconciliation dismisses every card there is nothing
              // pending, but the user must still see where the card went.
              if (approvalNotice.isNotEmpty) Text(approvalNotice),
              if (notifCenter.hasPendingApprovals) ...[
                Text(
                  'Approval required',
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                for (final n in notifCenter.pending.where(
                  (x) => x.channel == 'zara_approvals',
                ))
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
                                  key: ValueKey(
                                    'approval-approve-${n.executionId}',
                                  ),
                                  onPressed: () =>
                                      _decide(n, n.executionId, true),
                                  child: const Text('Approve'),
                                ),
                                TextButton(
                                  key: ValueKey(
                                    'approval-deny-${n.executionId}',
                                  ),
                                  onPressed: () =>
                                      _decide(n, n.executionId, false),
                                  child: const Text('Deny'),
                                ),
                              ],
                            ),
                    ),
                  ),
              ],
              for (final n in notifCenter.pending.where(
                (x) => x.channel != 'zara_approvals',
              ))
                ListTile(
                  dense: true,
                  leading: const Icon(Icons.notifications_outlined),
                  title: Text(n.title),
                  subtitle: Text(n.body),
                ),
              const SizedBox(height: 8),
            ], // end Chat tab (status/pairing/approvals)
            if (_tab == 3) ...[
              Text(
                'This device',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              Text('Voice: ${voice.state} — wake phrase "$wakePhrase"'),
              Text('Assistant: ${assistant.describe()}'),
              _AppOpenConsentPanel(
                consents: appOpenConsents,
                onChanged: _render,
              ),
              Text(
                'Mic permission: ${perms['microphone'] ?? '?'} | '
                'Notifications: ${perms['notifications'] ?? '?'}',
              ),
              Text('Keys: $secureBackend'),
              const SizedBox(height: 12),
              Text(
                'Permissions',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  OutlinedButton(
                    onPressed: () => _requestMic(),
                    child: const Text('Enable mic'),
                  ),
                  OutlinedButton(
                    onPressed: () async {
                      final m = await bridge.requestNotifPermission();
                      if (mounted) {
                        setState(() => perms['notifications'] = m['granted']);
                        try {
                          await client.describeCapabilities(
                            androidCapabilityDocs(
                              micPermission: perms['microphone'] == true,
                              notifPermission: perms['notifications'] == true,
                              forceStopGranted: perms['forceStop'] == true,
                              a11yReady: a11yReady,
                            ),
                          );
                        } catch (_) {
                          /* next boot retries */
                        }
                      }
                    },
                    child: const Text('Enable notifications'),
                  ),
                  OutlinedButton(
                    onPressed: _logout,
                    child: const Text('Sign out'),
                  ),
                ],
              ),
              CheckboxListTile(
                value: _resumeOptIn,
                controlAffinity: ListTileControlAffinity.leading,
                contentPadding: EdgeInsets.zero,
                title: const Text('Resume prompt after reboot (opt-in)'),
                subtitle: const Text(
                  'Android cannot keep the agent polling in the background. '
                  'When on, a reboot posts one "tap to resume" notification; '
                  'tapping it reopens Zara, which restores credentials and '
                  'reconnects normally. No background listening, no mic.',
                ),
                onChanged: (v) async {
                  if (v == null) return;
                  await bridge.setResumeOptIn(v);
                  if (mounted) setState(() => _resumeOptIn = v);
                },
              ),
              const SizedBox(height: 12),
              Text(
                'Paired devices',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              Text(
                'Fleet discovery via operator token (Tasks tab). '
                'Revoking here tombstones credentials, sweeps queued jobs, '
                'and marks the device offline immediately.',
              ),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  OutlinedButton(
                    onPressed: _fleetLoading ? null : _refreshFleet,
                    child: Text(_fleetLoading ? 'Refreshing…' : 'Refresh list'),
                  ),
                ],
              ),
              if (_fleetError.isNotEmpty) Text(_fleetError),
              for (final d in _fleet)
                ListTile(
                  dense: true,
                  title: Text(describeFleetDevice(d)),
                  subtitle: Text(
                    'trust:${d['trust'] ?? '?'} '
                    'presence:${d['presence'] ?? '?'} '
                    'seen:${d['last_seen'] ?? '?'}',
                  ),
                  // Run-here preselects the Tasks target and navigates;
                  // it never executes (submit + approval still required,
                  // target rechecked then). Hidden for revoked rows.
                  trailing: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      if ('${d['trust'] ?? ''}' != 'revoked')
                        OutlinedButton(
                          key: ValueKey(
                            'fleet-run-here-${d['device_id'] ?? ''}',
                          ),
                          onPressed: () => setState(() {
                            _taskTarget = '${d['device_id'] ?? ''}';
                            _tab = 1;
                          }),
                          child: const Text('Run here'),
                        ),
                      const SizedBox(width: 8),
                      OutlinedButton(
                        key: ValueKey('fleet-revoke-${d['device_id'] ?? ''}'),
                        onPressed: () =>
                            _revokeFleetDevice('${d['device_id'] ?? ''}'),
                        child: const Text('Revoke'),
                      ),
                    ],
                  ),
                ),
              Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _enrollIdCtrl,
                      decoration: const InputDecoration(
                        labelText: 'New device id to enroll',
                        hintText: 'e.g. laptop-2',
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  OutlinedButton(
                    onPressed: _enrollFleetDevice,
                    child: const Text('Enroll'),
                  ),
                ],
              ),
              if (_enrollCode.isNotEmpty)
                SelectableText('Pairing code: $_enrollCode'),
              const SizedBox(height: 12),
              Text(
                'Voice diagnostic (advanced)',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              Text(voiceDiag, style: Theme.of(context).textTheme.bodyMedium),
              if (voiceCapture.isNotEmpty) Text(voiceCapture),
              if (voiceStt.isNotEmpty) Text(voiceStt),
              if (voiceTranscript.isNotEmpty) Text('Heard: $voiceTranscript'),
              if (voiceReply.isNotEmpty) Text('Zara: $voiceReply'),
              if (voicePlayback.isNotEmpty) Text(voicePlayback),
              if (voiceChatReply.isNotEmpty)
                Text('Chat reply: $voiceChatReply'),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  FilledButton(
                    onPressed:
                        (voiceBusy ||
                            lifecycle.state != DeviceLifecycleState.online)
                        ? null
                        : _recordTest,
                    child: const Text('Record 5s test'),
                  ),
                  OutlinedButton(
                    onPressed: (voiceBusy || voiceReply.isEmpty)
                        ? null
                        : _speakLast,
                    child: const Text('Speak reply'),
                  ),
                  OutlinedButton(
                    onPressed: (voiceBusy || voiceTranscript.isEmpty)
                        ? null
                        : _sendTranscriptToChat,
                    child: const Text('Send transcript to chat'),
                  ),
                  OutlinedButton(
                    onPressed: voiceBusy ? _stopVoice : null,
                    child: const Text('Stop'),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              Text(
                'Wake-word opt-in (foundation)',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const Text(
                'Explicit opt-in only (off by default; preference '
                'persists but listening never auto-starts — enable it in '
                'the foreground after each restart). Needs mic permission '
                'plus a healthy battery; the wake engine flag below decides '
                'what is claimed.',
              ),
              const SizedBox(height: 4),
              WakeOptInPanel(
                state: wakeOptIn,
                onRequestMic: _requestMic,
                onRefresh: _refresh,
                onPersist: (v) => bridge.setWakeOptIn(v),
              ),
              const SizedBox(height: 12),
              Text(
                'Transfer diagnostic (advanced)',
                style: Theme.of(context).textTheme.titleMedium,
              ),
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
                    onPressed:
                        (xferBusy ||
                            lifecycle.state != DeviceLifecycleState.online)
                        ? null
                        : _xferSend,
                    child: const Text('Send test files'),
                  ),
                  OutlinedButton(
                    onPressed:
                        (xferBusy ||
                            lifecycle.state != DeviceLifecycleState.online)
                        ? null
                        : _xferFetch,
                    child: const Text('Fetch pending'),
                  ),
                  OutlinedButton(
                    onPressed: xferBusy ? _xferCancel : null,
                    child: const Text('Stop'),
                  ),
                ],
              ),
            ], // end Settings tab part 1 (device/permissions/voice/wake/transfer)
            if (_tab == 1) ...[
              Text(
                'Tasks (Core executions)',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const Text(
                'Async tools via Core. Approvals appear on the Chat tab '
                '(device-scoped, never local).',
              ),
              const SizedBox(height: 8),
              DropdownButtonFormField<String>(
                initialValue: _taskTool,
                decoration: const InputDecoration(
                  labelText: 'Tool (safe list only)',
                ),
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
                onChanged: (_) => _persistOperatorToken(),
                decoration: const InputDecoration(
                  labelText: 'Operator token (remembered on device)',
                  hintText: 'asked once; Keystore-backed, wiped on logout',
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
                  '(pinned to the Run-on target when set, else Core picks); '
                  'util.echo always uses direct submit (local tool, no '
                  'capability route).',
                ),
                onChanged: (v) {
                  if (v != null) setState(() => _taskUseResolve = v);
                },
              ),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  FilledButton(
                    key: const ValueKey('task-submit'),
                    onPressed: _taskBusy ? null : _submitTask,
                    child: const Text('Submit task'),
                  ),
                  if (_taskQueue.length > 0)
                    Text('Queued offline: ${_taskQueue.length}'),
                ],
              ),
              Builder(
                builder: (context) {
                  // Fleet options recomputed per build so presence/trust/cap
                  // changes (and tool switches) re-gate immediately. A stale
                  // selection (device left the fleet) falls back to This
                  // device for DISPLAY; submit revalidates and aborts loudly.
                  final options = fleetTargetOptions(
                    fleet: _fleet,
                    selfId: client.deviceId ?? 'android-phone',
                    tool: _taskTool,
                  );
                  final value = options.any((o) => o.id == _taskTarget)
                      ? _taskTarget
                      : '';
                  return DropdownButtonFormField<String>(
                    // initialValue + key (not `value`, deprecated): external
                    // preselection (fleet "Run here") rebuilds the field.
                    key: ValueKey('task-target-$_taskTarget'),
                    initialValue: value,
                    decoration: const InputDecoration(
                      labelText: 'Run on (fleet)',
                      helperText: 'This device default. Refresh devices for the latest presence/trust.',
                    ),
                    items: [
                      for (final o in options)
                        DropdownMenuItem(
                          value: o.id,
                          enabled: o.enabled,
                          child: Text(
                            o.enabled ? o.label : '${o.label} — ${o.reason}',
                          ),
                        ),
                    ],
                    onChanged: (v) {
                      if (v != null) setState(() => _taskTarget = v);
                    },
                  );
                },
              ),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  OutlinedButton(
                    onPressed: _fleetLoading ? null : _refreshFleet,
                    child: Text(
                      _fleetLoading ? 'Refreshing devices…' : 'Refresh devices',
                    ),
                  ),
                ],
              ),
              if (_fleetError.isNotEmpty) Text(_fleetError),
              if (_taskError.isNotEmpty)
                Text(
                  'Tasks: $_taskError',
                  style: const TextStyle(color: Colors.red),
                ),
              if (_tasks.isEmpty)
                const Text('No tasks yet — submit one above to track it here.'),
              for (final t in _tasks) _taskCard(context, t),
              const SizedBox(height: 12),
              Text(
                'Messaging approvals (mission-bound)',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const Text(
                'Pending send decisions via the /v1/messaging '
                'contracts (operator token above). Each approval binds ONE '
                'recipient, ONE message, ONE device and its plan — '
                'approving runs exactly that, nothing else. Recipients and '
                'states are shown; message text is never displayed here.',
              ),
              const SizedBox(height: 8),
              MessagingReviewPanel(
                backend: CoreMessagingBackend(client, _taskTokenCtrl),
              ),
            ], // end Tasks tab
            if (_tab == 0) ...[
              Text(
                'Conversation (talk stream)',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const Text(
                'Streaming replies via POST /v1/talk/stream (SSE); '
                '/v1/talk is the fallback. Approvals land in the Approval '
                'section above (device-scoped, never local).',
              ),
              const SizedBox(height: 8),
              KeyedSubtree(
                key: _talkSectionKey,
                child: TalkStreamPanel(
                  key: _talkKey,
                  client: client,
                  tokenCtrl: _taskTokenCtrl,
                  sessionId: _talkSessionId,
                  onApproval: (execId, reason) {
                    notifCenter.show(
                      DeviceNotification(
                        id: 'talk-$execId',
                        title: 'Approval required',
                        body: reason.isEmpty
                            ? 'Streamed turn needs a decision'
                            : reason,
                        executionId: execId,
                      ),
                    );
                    if (mounted) setState(() {});
                  },
                ),
              ),
              const SizedBox(height: 12),
            ], // end Chat tab (conversation)
            if (_tab == 2) ...[
              Text(
                'Memory review',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const Text(
                'Search + forget + purge via the operator memory '
                'contracts (operator token on the Tasks tab). Pending '
                'candidates load on manual Refresh; Accept/Reject confirms '
                'then reloads the list.',
              ),
              const SizedBox(height: 8),
              MemoryReviewPanel(client: client, tokenCtrl: _taskTokenCtrl),
              const SizedBox(height: 12),
              Text('History', style: Theme.of(context).textTheme.titleMedium),
              const Text(
                'Past conversations via the operator session '
                'contracts (operator token on the Tasks tab). Read-only '
                'browsing plus explicit delete; opening history never sends '
                'new prompts. Continue resumes a session in the Conversation '
                'Chat tab (same session id, transcript read-only).',
              ),
              const SizedBox(height: 8),
              HistoryPanel(
                client: client,
                tokenCtrl: _taskTokenCtrl,
                onResume: _resumeTalk,
              ),
              const SizedBox(height: 12),
            ], // end History tab (memory + past conversations)
            if (_tab == 3) ...[
              Text(
                'Diagnostics (advanced)',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const Text(
                'Connection + provider (display only) + last error + '
                'governor flags + the explicit one-tap live provider check '
                '(operator token from Tasks section). Only new calls are '
                'the public GET /v1/ready and the explicit POST '
                '/v1/diagnostics/provider-check (+ cancel).',
              ),
              const SizedBox(height: 8),
              DiagnosticsPanel(
                client: client,
                connection: conn,
                lastError: lastError,
                tokenCtrl: _taskTokenCtrl,
              ),
              const SizedBox(height: 12),
              Text(
                'Provider setup',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const Text(
                'Which Core model provider is active and how the '
                'operator configures it. Read-only: no key entry here.',
              ),
              const SizedBox(height: 8),
              ProviderSetupPanel(client: client),
              const SizedBox(height: 12),
              Text(
                'Capabilities',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              for (final c in androidCapabilities)
                ListTile(
                  dense: true,
                  leading: Icon(
                    c.supported ? Icons.check_circle : Icons.circle_outlined,
                  ),
                  title: Text(c.name),
                  subtitle: Text(c.note),
                ),
            ], // end Settings tab part 2 (diagnostics/provider/capabilities)
          ],
        ),
        bottomNavigationBar: NavigationBar(
          selectedIndex: _tab,
          onDestinationSelected: (i) {
            if (mounted) setState(() => _tab = i);
          },
          destinations: [
            NavigationDestination(
              icon: _pendingApprovalCount > 0
                  ? Badge(
                      label: Text('$_pendingApprovalCount'),
                      child: const Icon(Icons.chat_bubble_outline),
                    )
                  : const Icon(Icons.chat_bubble_outline),
              selectedIcon: const Icon(Icons.chat_bubble),
              label: 'Chat',
            ),
            const NavigationDestination(
              icon: Icon(Icons.task_outlined),
              selectedIcon: Icon(Icons.task),
              label: 'Tasks',
            ),
            const NavigationDestination(
              icon: Icon(Icons.history_outlined),
              selectedIcon: Icon(Icons.history),
              label: 'History',
            ),
            const NavigationDestination(
              icon: Icon(Icons.settings_outlined),
              selectedIcon: Icon(Icons.settings),
              label: 'Settings',
            ),
          ],
        ),
      ),
    );
  }
}

/// P6-c26 (2026-10-24): the device-side surface for the per-app OPEN
/// consents — a list with per-app Revoke, nothing more. It makes the
/// grant promise on the dialog real: a consent given here can always
/// be undone HERE. Core has no API into this store; the panel reads
/// the device-local list only. Unavailable grants show the honest
/// empty line — absence is named, never invented.
class _AppOpenConsentPanel extends StatefulWidget {
  final AppOpenConsents consents;
  final VoidCallback onChanged;
  const _AppOpenConsentPanel({required this.consents, required this.onChanged});

  @override
  State<_AppOpenConsentPanel> createState() => _AppOpenConsentPanelState();
}

class _AppOpenConsentPanelState extends State<_AppOpenConsentPanel> {
  List<String>? _rows;

  @override
  void initState() {
    super.initState();
    _reload();
  }

  Future<void> _reload() async {
    try {
      final rows = await widget.consents.list();
      if (!mounted) return;
      setState(() => _rows = rows);
    } catch (_) {
      if (!mounted) return;
      setState(() => _rows = const <String>[]);
    }
  }

  @override
  Widget build(BuildContext context) {
    final rows = _rows;
    if (rows == null) return const SizedBox.shrink();
    if (rows.isEmpty) {
      return const Text('App-open consents: none (out-of-allowlist '
          'app opens ask on the phone)');
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('App-open consents (phone-local):',
            style: Theme.of(context).textTheme.titleSmall),
        for (final pkg in rows)
          Row(
            children: [
              Expanded(child: Text(pkg, overflow: TextOverflow.ellipsis)),
              TextButton(
                onPressed: () async {
                  await widget.consents.revoke(pkg);
                  await _reload();
                  widget.onChanged();
                },
                child: const Text('Revoke'),
              ),
            ],
          ),
      ],
    );
  }
}
