// Android job runner — the ONLY code that acts on Core-dispatched jobs.
//
// Authority rule: Core decides WHAT runs; this runner only executes tools
// on a hard allowlist with device-local data. Unknown tool names are
// REFUSED with a structured error — never exec'd, never shelled, never
// fetched from a URL. Pure Dart; bridge snapshots are injected.
import 'app_open_consent.dart';

/// Strict job shape from /v1/agent/jobs/poll. Malformed jobs throw and are
/// reported as failed — never executed.
class DeviceJob {
  final String jobId;
  final String tool;
  final Map<String, dynamic> inputs;
  final double timeoutS;
  final String executionId;
  final String? missionId;
  const DeviceJob({
    required this.jobId,
    required this.tool,
    required this.inputs,
    required this.timeoutS,
    required this.executionId,
    this.missionId,
  });

  static DeviceJob fromJson(Map<String, dynamic> m) {
    final id = m['job_id'], tool = m['tool'], inputs = m['inputs'];
    if (id is! String || id.isEmpty) throw FormatException('bad job_id');
    if (tool is! String || tool.isEmpty) throw FormatException('bad tool');
    if (inputs is! Map) throw FormatException('bad inputs');
    final t = m['timeout_s'];
    return DeviceJob(
      jobId: id,
      tool: tool,
      inputs: Map<String, dynamic>.from(inputs),
      timeoutS: t is num ? t.toDouble() : 30.0,
      executionId: '${m['execution_id'] ?? ''}',
      missionId: m['mission_id'] as String?,
    );
  }
}

class CancelToken {
  bool _cancelled = false;
  void cancel() => _cancelled = true;
  bool get isCancelled => _cancelled;
}

class JobResult {
  final bool ok;
  final Map<String, dynamic> result;
  final String error;
  const JobResult({required this.ok, this.result = const {}, this.error = ''});
}

/// Snapshot providers (wired to DeviceBridge in main.dart, faked in tests).
typedef BatteryProbe = Map<String, dynamic> Function();
typedef NetworkProbe = Map<String, dynamic> Function();

/// Phase 1 state probes (wired to DeviceBridge wifi/bluetooth/... states).
/// Async: DeviceBridge platform-channel calls always return Futures.
typedef SysStateProbe = Future<Map<String, dynamic>> Function();

/// Phase 1 executors (wired to DeviceBridge.openSettings/openApp).
typedef SettingsOpener = Future<Map<String, dynamic>> Function(String screen);
typedef AppOpener = Future<Map<String, dynamic>> Function(String package);

/// P6-c26: the phone-side consent surface for OUT-of-allowlist app
/// opens. main.dart wires this to a real dialog; null (builds without
/// the surface, or no UI context) keeps the byte-equal refusal. The
/// Future resolves true (tapped allow), false (tapped deny), or null
/// (dismissed/not showable) — all three are distinct, none invented.
typedef AppConsentPrompt = Future<bool?> Function(String package);

/// Phase 2 tiered setters (wired to DeviceBridge.setWifi/... in main.dart,
/// faked in tests). Each returns the native result map; applied:true only
/// after native post-action verification. Null = gateway unavailable.
typedef SysSetter = Future<Map<String, dynamic>> Function(bool enabled);

/// Stage 20 observed-device discovery probes (wired to
/// DeviceBridge.observedDevices / mediaCounts in main.dart, faked in
/// tests). Read-only; detection is never trust.
typedef ObservedDevicesProbe = Future<Map<String, dynamic>> Function();
typedef MediaCountsProbe = Future<Map<String, dynamic>> Function();
// Stage 31-P3 phase A: quick/one-shot phone actions. String single-arg
// (media key action, clipboard text, open-uri) share one typedef; the
// torch/power-saver setters reuse SysSetter(bool).
typedef TextActionProbe = Future<Map<String, dynamic>> Function(String s);
typedef PctActionProbe = Future<Map<String, dynamic>> Function(int pct);
typedef RingProbe = Future<Map<String, dynamic>> Function(
    int seconds, bool allowDndBypass);
typedef AlarmProbe = Future<Map<String, dynamic>> Function(
    int? hour, int? minute, String? label);
/// P5 feature-hunt (2026-10-22): countdown timer hand-off.
typedef TimerProbe = Future<Map<String, dynamic>> Function(
    int? minutes, String? label);
/// P5 feature-hunt ② (2026-10-22): DND state read + verified toggle.
typedef DndGetProbe = Future<Map<String, dynamic>> Function();
typedef DndSetProbe = Future<Map<String, dynamic>> Function(bool? enabled);

/// Stage 31-P3 phase B (P3d): bounded contacts lookup + the confirmed
/// direct call, faked in tests, wired to DeviceBridge in main.dart.
typedef ContactLookupProbe = Future<Map<String, dynamic>> Function(
    String? name, int limit);
typedef CallDirectProbe = Future<Map<String, dynamic>> Function(
    String? number);
typedef CallScreenPollProbe = Future<Map<String, dynamic>> Function(
    int waitMs);

/// Stage 31-P3 phase B (P3e): ONE carrier SMS through the confirmed
/// recipient. Never throws.
typedef SmsSendProbe = Future<Map<String, dynamic>> Function(
    String? number, String? text);

/// Stage 31-P3 phase B (P3f): bounded OTP scan over the fresh ring.
/// Never throws. Codes ride the LIVE result (the turn surfaces them);
/// memory + audit rows never receive them (core-side fixture-pinned).
typedef OtpProbe = Future<Map<String, dynamic>> Function(
    int maxAgeMs, String? senderHint);

/// Stage 31-P3 phase B (P3a): targeted media control of ONE app's
/// session. Never throws.
typedef MediaSessionProbe = Future<Map<String, dynamic>> Function(
    String? hint, String? action);

/// Stage 31-P3 phase B (P3c): photo inventory probe (counts only).
typedef PhotosInventoryProbe = Future<Map<String, dynamic>> Function();

/// Privacy: call results carry the number's TAIL only (last 4 digits);
/// the full number lives in the approval prompt — never in results or
/// the audit trail they land in.
String _tail(String number) {
  final digits = number.trim();
  if (digits.length <= 4) return '*' * digits.length;
  final t = digits.substring(digits.length - 4);
  return '${'*' * (digits.length - 4)}$t';
}

/// Privileged executor (wired to DeviceBridge.privForceStop in main.dart,
/// faked in tests). Null means the gateway is unavailable on this build.
typedef ForceStopProbe = Future<Map<String, dynamic>> Function(String package);

/// A11y snapshot provider (wired to DeviceBridge.a11yInspect in main.dart,
/// faked in tests). Null means the gateway is unavailable on this build.
typedef A11yInspectProbe = Future<Map<String, dynamic>> Function();

/// A11y snapshot provider with params (Rung-3): expected_package,
/// max_elements, include_text, include_content_description.
typedef A11yInspectWithParamsProbe = Future<Map<String, dynamic>> Function(
  Map<String, dynamic> params,
);

/// A11y tap executor (wired to DeviceBridge.a11yTap in main.dart, faked in
/// tests). Null means the gateway is unavailable on this build.
/// Contract v1: optional [options] carries snapshot bindings
/// (expected_package, expected_snapshot_id, max_elements) forwarded to
/// the native snapshot-bound tap; fakes may ignore the third argument.
typedef A11yTapProbe = Future<Map<String, dynamic>> Function(
  String package,
  int nodeId, [
  Map<String, dynamic>? options,
]);

/// Contract-v2 text-entry executor (wired to DeviceBridge.a11ySetText).
/// [text] is never logged or echoed by the runner. Null = unavailable.
typedef A11ySetTextProbe = Future<Map<String, dynamic>> Function(
  String package,
  int nodeId,
  String text, [
  Map<String, dynamic>? options,
]);

/// Contract-v2 scroll executor (wired to DeviceBridge.a11yScroll).
/// Null = unavailable.
typedef A11yScrollProbe = Future<Map<String, dynamic>> Function(
  String package,
  int nodeId,
  bool forward, [
  Map<String, dynamic>? options,
]);

/// Contract-v3 read-only semantic lookup (wired to DeviceBridge.a11yFind in
/// main.dart, faked in tests). A MATCH returns element facts (observation
/// only); not-found / ambiguous arrive as coded bridge errors carrying
/// evidence. Null = unavailable on this build.
typedef A11yFindProbe = Future<Map<String, dynamic>> Function(
  String package,
  Map<String, dynamic> target, [
  Map<String, dynamic>? options,
]);

/// Global navigation executor (wired to DeviceBridge.a11yNavigate in
/// main.dart, faked in tests). [direction] is 'back' or 'home'; [options]
/// carries the foreground guard (expected_package). Null = unavailable.
typedef A11yNavigateProbe = Future<Map<String, dynamic>> Function(
  String direction,
  String package, [
  Map<String, dynamic>? options,
]);

/// Launchable-app discovery (wired to DeviceBridge.appList in main.dart,
/// faked in tests). [limit] is 1-200. Null = unavailable on this build.
typedef AppsListProbe = Future<Map<String, dynamic>> Function(int limit);

/// Passive notification read (wired to DeviceBridge.notifList in
/// main.dart, faked in tests). [params] carries since_ms/limit; the
/// native grant check returns granted:false until the user opted in.
/// Null = unavailable on this build.
typedef NotifListProbe = Future<Map<String, dynamic>> Function(
  Map<String, dynamic> params,
);

/// Contract-v3 semantic target selector (docs/UI_SNAPSHOT_CONTRACT.md
/// §8). Mirrors the native SemTarget.parse discipline exactly: known
/// selector fields only (resource_id, content_desc, text, role), bounded
/// lengths, secret-shaped values refused fail-closed, optional
/// non-negative integer index_hint, and at least one selector. The tap /
/// set_text executors resolve targets natively against the FRESH tree at
/// action time — the runner only validates shape and forwards evidence.
class SemTarget {
  final Map<String, dynamic>? arg;
  final String? refuse;
  const SemTarget._(this.arg, this.refuse);

  static const _limits = {
    'resource_id': 128,
    'content_desc': 120,
    'text': 120,
    'role': 40,
  };
  static final _secretRe = RegExp(
    'password|passwd|token|api[_-]?key|apikey|secret|credential'
    '|private[_-]?key',
    caseSensitive: false,
  );
  static const _selectorKeys = [
    'resource_id',
    'content_desc',
    'text',
    'role',
    'index_hint',
  ];

  /// Absent target → [arg] null + [refuse] null. A malformed target always
  /// returns a refusal; the runner never silently drops it.
  static SemTarget parse(Object? raw) {
    if (raw == null) return const SemTarget._(null, null);
    if (raw is! Map) {
      return const SemTarget._(null, 'bad target (need selector object)');
    }
    for (final k in raw.keys) {
      if (!_selectorKeys.contains(k)) {
        return SemTarget._(null, 'bad target: unknown selector field $k');
      }
    }
    final out = <String, dynamic>{};
    var selCount = 0;
    for (final k in _limits.keys) {
      final v = raw[k];
      if (v == null) continue;
      if (v is! String || v.isEmpty) {
        return SemTarget._(null, 'bad target: $k must be a non-empty string');
      }
      if (v.length > _limits[k]!) {
        return SemTarget._(
          null,
          'bad target: $k too long (limit ${_limits[k]} chars)',
        );
      }
      if (_secretRe.hasMatch(v)) {
        return SemTarget._(
          null,
          'insecure target: $k looks secret-shaped (refused, never '
          'matched)',
        );
      }
      out[k] = v;
      selCount++;
    }
    if (selCount == 0) {
      return const SemTarget._(
        null,
        'bad target: need at least one selector (resource_id, '
        'content_desc, text, or role)',
      );
    }
    final hint = raw['index_hint'];
    if (hint != null) {
      if (hint is! int || hint < 0) {
        return const SemTarget._(
          null,
          'bad target: index_hint must be a non-negative integer',
        );
      }
      out['index_hint'] = hint;
    }
    return SemTarget._(out, null);
  }
}

class AndroidJobRunner {
  final BatteryProbe battery;
  final NetworkProbe network;
  final ForceStopProbe? forceStop;
  final A11yInspectProbe? a11yInspect;
  final A11yInspectWithParamsProbe? a11yInspectWithParams;
  final A11yTapProbe? a11yTap;
  final A11ySetTextProbe? a11ySetText;
  final A11yScrollProbe? a11yScroll;
  final A11yNavigateProbe? a11yNavigate;
  final A11yFindProbe? a11yFind;
  final AppsListProbe? appsList;
  final ObservedDevicesProbe? observedDevices;
  final MediaCountsProbe? mediaCounts;
  final NotifListProbe? notifList;
  // Stage 31-P3 phase A gateways (nulled on builds without them —
  // unavailable is reported honestly, never faked).
  final SysSetter? torchSet;
  final PctActionProbe? streamVolumeSet;
  final RingProbe? findMyPhoneRing;
  final TextActionProbe? mediaKeyEvent;
  final TextActionProbe? clipboardSet;
  final TextActionProbe? openUri;
  final SysSetter? powerSaverSet;
  final AlarmProbe? alarmSet;
  final TimerProbe? timerSet;
  // P5 feature-hunt ② (2026-10-22) gateways (nulled builds report
  // unavailable honestly, never faked).
  final DndGetProbe? dndGet;
  final DndSetProbe? dndSet;
  // Stage 31-P3 phase B (P3d) gateways.
  final ContactLookupProbe? contactsLookup;
  final CallDirectProbe? callDirect;
  final CallScreenPollProbe? callScreenPoll;
  // Stage 31-P3 phase B (P3e) gateway.
  final SmsSendProbe? smsSend;
  // Stage 31-P3 phase B (P3f) gateway.
  final OtpProbe? otpScan;
  // Stage 31-P3 phase B (P3a) gateway.
  final MediaSessionProbe? mediaSessionTargeted;
  // Stage 31-P3 phase B (P3c) gateway.
  final PhotosInventoryProbe? photosInventory;
  final SysStateProbe? wifiState;
  final SysStateProbe? bluetoothState;
  final SysStateProbe? hotspotState;
  final SysStateProbe? mobileDataState;
  final SysStateProbe? airplaneModeState;
  final SettingsOpener? openSettings;
  final AppOpener? openApp;
  final SysSetter? setWifi;
  final SysSetter? setBluetooth;
  final SysSetter? setHotspot;
  final SysSetter? setMobileData;
  final SysSetter? setAirplaneMode;
  // P6-c26: the per-app open consent surface (device-local decision;
  // Core never sees or changes the grant store).
  final AppConsentPrompt? appConsent;
  final AppOpenConsents? appOpenConsents;
  final Set<String> _seen = {};
  static const int maxSeen = 200;

  AndroidJobRunner({
    required this.battery,
    required this.network,
    this.forceStop,
    this.a11yInspect,
    this.a11yInspectWithParams,
    this.a11yTap,
    this.a11ySetText,
    this.a11yScroll,
    this.a11yNavigate,
    this.a11yFind,
    this.appsList,
    this.observedDevices,
    this.mediaCounts,
    this.notifList,
    this.torchSet,
    this.streamVolumeSet,
    this.findMyPhoneRing,
    this.mediaKeyEvent,
    this.clipboardSet,
    this.openUri,
    this.powerSaverSet,
    this.alarmSet,
    this.timerSet,
    this.dndGet,
    this.dndSet,
    this.contactsLookup,
    this.callDirect,
    this.callScreenPoll,
    this.smsSend,
    this.otpScan,
    this.mediaSessionTargeted,
    this.photosInventory,
    this.wifiState,
    this.bluetoothState,
    this.hotspotState,
    this.mobileDataState,
    this.airplaneModeState,
    this.openSettings,
    this.openApp,
    this.setWifi,
    this.setBluetooth,
    this.setHotspot,
    this.setMobileData,
    this.setAirplaneMode,
    this.appConsent,
    this.appOpenConsents,
  });

  static const allowlisted = {
    'system.battery',
    'system.battery.charging_time',
    'system.network',
    'system.wifi.state',
    'system.wifi.settings',
    'system.bluetooth.state',
    'system.bluetooth.settings',
    'system.hotspot.state',
    'system.hotspot.settings',
    'system.mobile_data.state',
    'system.mobile_data.settings',
    'system.airplane_mode.state',
    'system.airplane_mode.settings',
    'system.wifi.set',
    'system.bluetooth.set',
    'system.hotspot.set',
    'system.mobile_data.set',
    'system.airplane_mode.set',
    'system.app.open',
    'gui.screen.inspect',
    'gui.tap',
    'gui.set_text',
    'gui.scroll',
    'gui.find',
    'gui.back',
    'gui.wait',
    'android.apps.list',
    'android.observe',
    'android.storage.stats',
    'android.notifications.read',
    // Stage 31-P3 phase A quick/one-shot actions (P3a media keys,
    // P3b clipboard push, P3h torch/alarm/volume, P3i ring, P3j open
    // uri, P3k battery saver via the T1 adb grant).
    'android.torch.set',
    'android.volume.set',
    'android.ring',
    'android.media.playback',
    'android.clipboard.set',
    'android.open_uri',
    'android.power_saver.set',
    'android.alarm.set',
    'android.timer.set',
    // P5 feature-hunt ② (2026-10-22): DND state + verified toggle.
    'android.dnd.get',
    'android.dnd.set',
    // Stage 31-P3 phase B (P3d): the call flow. The lookup reads the
    // contacts database (bounded columns); the call only fires on the
    // CONFIRMED number after Core's per-call approval.
    'android.contacts.lookup',
    'android.call.place',
    // Stage 31-P3 phase B (P3e): ONE carrier SMS on the confirmed
    // recipient; the draft lives in the approval inputs only.
    'android.sms.send',
    // Stage 31-P3 phase B (P3f): per-use consented OTP read.
    'android.otp.read',
    // Stage 31-P3 phase B (P3a): targeted media session control.
    'android.media.control',
    // Stage 31-P3 phase B (P3c): pull-only photo inventory gate.
    'android.photos.inventory',
  };

  /// Rung-1 lab allowlist mirror — byte-equal to Core's
  /// FORCE_STOP_LAB_TARGETS. Refusals happen here even if a job arrived.
  static const forceStopLabTargets = {'dev.zara.lab.privtest'};

  /// Rung-2 lab allowlist mirror — byte-equal to Core's A11Y_TAP_TARGETS.
  /// Refusals happen here even if a job arrived.
  static const a11yTapTargets = {
    'dev.zara.zara_android',
    'dev.zara.lab.privtest',
  };

  /// Shared contract v1/v2 action tail: invoke the gateway with snapshot
  /// bindings, map channel errors and non-applied results to failures
  /// with evidence, and return ok:true ONLY when applied == true.
  /// [defaultVerify] labels success when native omits verify_state;
  /// [extraSuccess] adds action-specific success fields. Typed text must
  /// never appear in [extraSuccess] (no echo, ever).
  ///
  /// Stage 29 (mined hardening, OpenDroid): [verifyMatch] decides the
  /// honest [verified] flag — TRUE only when NATIVE's own re-read
  /// confirmed the declared effect (tap: 'clicked'; set_text:
  /// 'text_matched'). Runner-side defaults never claim verification:
  /// scroll ('requested (compare snapshots…)') and set_text's
  /// non-matching applied-notes report verified=false, never a
  /// runner-invented confirm. Absent [verifyMatch] = the family member
  /// has no automatic native confirm → verified=false (explicitly
  /// carried, never omitted).
  static Future<JobResult> _guardedA11y({
    required String pkg,
    required int nodeId,
    required Map<String, dynamic> options,
    required Future<Map<String, dynamic>> Function() invoke,
    required String defaultVerify,
    Map<String, dynamic> extraSuccess = const {},
    bool Function(String verifyState)? verifyMatch,
  }) async {
    late final Map<String, dynamic> m;
    try {
      m = await invoke();
    } catch (e) {
      // Native refusals arrive as channel errors (stale, mismatch,
      // denied, unavailable): structured failure, never fake success.
      // The bridge exception shape is read structurally (no import:
      // this runner stays dependency-free). Native refusal evidence
      // (fresh snapshot_id, actual package) merges in when the channel
      // carried a details map.
      String code = 'failed';
      Map<String, dynamic> evidence = const {};
      try {
        final dyn = e as dynamic;
        final c = dyn.code;
        if (c != null) code = '$c';
        final det = dyn.details;
        if (det is Map) {
          evidence = Map<String, dynamic>.from(det);
        }
      } catch (_) {
        // Not a coded bridge error; fall through to generic mapping.
      }
      return JobResult(
        ok: false,
        error: _tapCodeMessage(code),
        result: {
          'package': pkg,
          'node_id': nodeId,
          'bridge_code': code,
          ...evidence,
        },
      );
    }
    if (m['applied'] != true) {
      // Native returned (not threw) a non-applied result: the action
      // did not happen. Failure with evidence, never ok:true.
      return JobResult(
        ok: false,
        error: _tapCodeMessage('${m['error'] ?? 'not_applied'}'),
        result: Map<String, dynamic>.from(m),
      );
    }
    return JobResult(
      ok: true,
      result: {
        'package': '${m['package'] ?? pkg}',
        'applied': true,
        'verify_state': '${m['verify_state'] ?? defaultVerify}',
        // Stage 29 honest verification: native re-read confirmation
        // only; runner defaults are labels, never proof (see doc above).
        // Native omitting verify_state → '' → no match → false.
        'verified': verifyMatch != null && verifyMatch('${m['verify_state'] ?? ''}'),
        if (m['snapshot_id'] != null) 'snapshot_id': '${m['snapshot_id']}',
        if (m['contract_version'] != null)
          'contract_version': m['contract_version'],
        if (m['target_matched'] is Map)
          'target_matched': Map<String, dynamic>.from(m['target_matched']),
        ...extraSuccess,
      },
    );
  }

  /// Snapshot bindings forwarded to every guarded action.
  static Map<String, dynamic> _a11yOptions(Map<String, dynamic> inputs) {
    final options = <String, dynamic>{};
    for (final k in [
      'expected_package',
      'expected_snapshot_id',
      'max_elements',
    ]) {
      final v = inputs[k];
      if (v != null) options[k] = v;
    }
    return options;
  }

  /// Shared bridge-error decoder (Stage 28): waits poll through the
  /// normal discover/inspect channel, so coded refusals (NOT_FOUND,
  /// UNEXPECTED_PACKAGE, unavailable) arrive as channel exceptions that
  /// must be decoded structurally — the same way the action cases do
  /// it inline — without importing platform types into the runner.
  static Map<String, dynamic> _decodeBridgeError(Object e) {
    String code = 'failed';
    Map<String, dynamic> evidence = const {};
    try {
      final dyn = e as dynamic;
      final c = dyn.code;
      if (c != null) code = '$c';
      final det = dyn.details;
      if (det is Map) evidence = Map<String, dynamic>.from(det);
    } catch (_) {
      // Not a coded bridge error; caller maps the generic tail.
    }
    return {'code': code, 'evidence': evidence};
  }

  /// Contract native action error codes (docs/UI_SNAPSHOT_CONTRACT.md)
  /// mapped to human-readable refusal reasons. Every code means "the
  /// action did not happen" — callers must treat these as failures,
  /// never retry an action blindly against a changed screen.
  static String _tapCodeMessage(String code) {
    switch (code) {
      case 'stale':
      case 'STALE_SNAPSHOT':
        return 'stale snapshot (refused, never executed)';
      case 'mismatch':
      case 'UNEXPECTED_PACKAGE':
        return 'foreground package changed (refused, never executed)';
      case 'denied':
        return 'action denied by device policy (refused, never executed)';
      case 'unavailable':
      case 'NO_WINDOW':
        return 'a11y service unavailable (never executed)';
      case 'NODE_MISSING':
        return 'action target not present (not applied)';
      case 'NOT_CLICKABLE':
        return 'action target not clickable (not applied)';
      case 'EMPTY_TEXT':
        return 'nothing to type (refused, never executed)';
      case 'NOT_EDITABLE':
        return 'text target not editable (not applied)';
      case 'EDIT_FAILED':
        return 'text entry failed (not applied)';
      case 'NOT_SCROLLABLE':
        return 'scroll target not scrollable (not applied)';
      case 'SCROLL_FAILED':
        return 'scroll failed (not applied)';
      case 'TARGET_NOT_FOUND':
        return 'no element matches the target in the fresh tree (refused, '
            'never guessed)';
      case 'TARGET_AMBIGUOUS':
        return 'several elements match the target; give index_hint or a '
            'more specific selector (refused)';
      case 'TARGET_INDEX_OUT_OF_RANGE':
        return 'index_hint beyond the matching elements (refused, never '
            'guessed)';
      case 'TARGET_INVALID':
        return 'bad target selectors (refused, never guessed)';
      case 'TARGET_CONFLICT':
        return 'give target or node_id, not both (refused)';
      case 'INSECURE_TARGET':
        return 'target selector looks secret-shaped (refused, never '
            'matched)';
      default:
        return 'action not applied: $code';
    }
  }

  /// Phase 1 app-open allowlist mirror — byte-equal to Core's
  /// APP_OPEN_TARGETS. Refusals happen here even if a job arrived.
  static const appOpenTargets = {
    'dev.zara.zara_android',
    'dev.zara.lab.privtest',
    'com.android.settings',
    'com.android.chrome',
    'com.google.android.gm',
    'com.google.android.calendar',
  };

  /// Phase 1 settings screens (must match native settingsActions keys).
  static const settingsScreens = {
    'wifi',
    'bluetooth',
    'hotspot',
    'mobile_data',
    'airplane_mode',
  };

  Future<JobResult> run(DeviceJob job, {CancelToken? cancel}) async {
    if (_seen.contains(job.jobId)) {
      return const JobResult(
        ok: false,
        error: 'duplicate job refused (already handled)',
      );
    }
    _seen.add(job.jobId);
    if (_seen.length > maxSeen) _seen.remove(_seen.first);
    if (cancel?.isCancelled ?? false) {
      return const JobResult(ok: false, error: 'job cancelled before start');
    }
    try {
      switch (job.tool) {
        case 'system.battery':
          final b = battery();
          return JobResult(
            ok: true,
            result: {
              'battery_pct': b['battery_pct'],
              'charging': b['charging'] == true,
              'power_save': b['power_save'] == true,
            },
          );
        case 'system.network':
          final n = network();
          return JobResult(
            ok: true,
            result: {
              'network': n['network'] ?? 'unknown',
              'metered': n['metered'] == true,
            },
          );
        case 'android.app.force_stop':
          final pkg = job.inputs['target_package'];
          if (pkg is! String || !forceStopLabTargets.contains(pkg)) {
            return const JobResult(
              ok: false,
              error: 'target not in lab allowlist (refused, never executed)',
            );
          }
          final fn = forceStop;
          if (fn == null) {
            return const JobResult(
              ok: false,
              error: 'privileged gateway unavailable on this build',
            );
          }
          final m = await fn(pkg);
          return JobResult(
            ok: true,
            result: {
              'package': '${m['package'] ?? pkg}',
              'stopped': m['stopped'] == true,
              'was_running': m['was_running'] == true,
              'verify_state': '${m['verify_state'] ?? 'unknown'}',
            },
          );
        case 'gui.screen.inspect':
          // Prefer the params version if provided; fall back to legacy.
          final inspectParams = a11yInspectWithParams;
          final inspectLegacy = a11yInspect;
          if (inspectParams == null && inspectLegacy == null) {
            return const JobResult(
              ok: false,
              error: 'a11y gateway unavailable on this build',
            );
          }
          // Extract params from job.inputs
          final params = <String, dynamic>{};
          final expectedPkg = job.inputs['expected_package'];
          if (expectedPkg is String && expectedPkg.isNotEmpty) {
            params['expected_package'] = expectedPkg;
          }
          final expectedSnap = job.inputs['expected_snapshot_id'];
          if (expectedSnap is String && expectedSnap.isNotEmpty) {
            params['expected_snapshot_id'] = expectedSnap;
          }
          final maxEl = job.inputs['max_elements'];
          if (maxEl is int && maxEl > 0) {
            params['max_elements'] = maxEl;
          }
          final incText = job.inputs['include_text'];
          if (incText is bool) {
            params['include_text'] = incText;
          }
          final incContentDesc = job.inputs['include_content_description'];
          if (incContentDesc is bool) {
            params['include_content_description'] = incContentDesc;
          }
          final snap = inspectParams != null
              ? await inspectParams(params)
              : await inspectLegacy!();
          // Native side reports unsupported (e.g., UNEXPECTED_PACKAGE) via
          // {supported: false, reason: ...} — treat as job failure.
          if (snap['supported'] == false) {
            return JobResult(
              ok: false,
              error: snap['reason'] ?? 'a11y inspect unsupported',
              result: Map<String, dynamic>.from(snap),
            );
          }
          return JobResult(ok: true, result: Map<String, dynamic>.from(snap));
        case 'gui.tap':
          // Core schema key is `target_package` (device_tools.py); accept
          // legacy `package` too. Allowlist enforced either way.
          // Contract v1 (docs/UI_SNAPSHOT_CONTRACT.md): node_id is an
          // exact non-negative integer DFS index — fractional doubles are
          // refused (truncation could click the wrong node).
          final pkg = job.inputs['target_package'] ?? job.inputs['package'];
          final rawId = job.inputs['node_id'];
          if (pkg is! String || !a11yTapTargets.contains(pkg)) {
            return const JobResult(
              ok: false,
              error: 'target not in lab allowlist (refused, never executed)',
            );
          }
          final nodeId = rawId is int && rawId >= 0 ? rawId : -1;
          final tap = a11yTap;
          if (tap == null) {
            return const JobResult(
              ok: false,
              error: 'a11y gateway unavailable on this build',
            );
          }
          // Contract v3: positional node_id OR semantic target — never
          // both. A valid target lets the tap run with node_id = -1: the
          // native resolver matches against the FRESH tree at action time
          // and refuses NOT_FOUND / AMBIGUOUS instead of guessing.
          final t = SemTarget.parse(job.inputs['target']);
          if (t.refuse != null) {
            return JobResult(ok: false, error: t.refuse!);
          }
          if (t.arg != null && nodeId >= 0) {
            return const JobResult(
              ok: false,
              error: 'give target or node_id, not both',
            );
          }
          if (t.arg == null && nodeId < 0) {
            return const JobResult(
              ok: false,
              error: 'bad node_id (need snapshot node)',
            );
          }
          final options = _a11yOptions(job.inputs);
          if (t.arg != null) options['target'] = t.arg;
          // Snapshot bindings (expected_package, expected_snapshot_id,
          // max_elements) are forwarded to the native snapshot-bound tap,
          // which re-reads state and refuses stale/moved targets instead
          // of clicking them. A non-applied tap is a FAILURE with
          // evidence, never ok:true.
          return await _guardedA11y(
            pkg: pkg,
            nodeId: nodeId,
            options: options,
            invoke: () => tap(pkg, nodeId, options),
            defaultVerify: 'clicked',
            // Native re-verifies via its own state read: 'clicked' is
            // the tap confirm; anything else (or native omitting it)
            // stays honestly unverified.
            verifyMatch: (s) => s == 'clicked',
            extraSuccess: const {'stopped': 'n/a'},
          );
        case 'gui.set_text':
          // Contract v2 text entry. The typed text travels to the gateway
          // but is NEVER logged, echoed, or returned: success carries
          // only verify_state (text_matched / applied-notes), failures
          // carry codes + snapshot evidence.
          final spkg = job.inputs['target_package'] ?? job.inputs['package'];
          final srawId = job.inputs['node_id'];
          if (spkg is! String || !a11yTapTargets.contains(spkg)) {
            return const JobResult(
              ok: false,
              error: 'target not in lab allowlist (refused, never executed)',
            );
          }
          final snodeId = srawId is int && srawId >= 0 ? srawId : -1;
          // Contract v3: semantic target resolves against the fresh tree
          // natively; runner validates shape and forwards it.
          final st = SemTarget.parse(job.inputs['target']);
          if (st.refuse != null) {
            return JobResult(ok: false, error: st.refuse!);
          }
          if (st.arg != null && snodeId >= 0) {
            return const JobResult(
              ok: false,
              error: 'give target or node_id, not both',
            );
          }
          if (st.arg == null && snodeId < 0) {
            return const JobResult(
              ok: false,
              error: 'bad node_id (need snapshot node)',
            );
          }
          final stext = job.inputs['text'];
          if (stext is! String || stext.isEmpty || stext.length > 500) {
            return const JobResult(
              ok: false,
              error: 'bad text (need 1-500 chars, never logged)',
            );
          }
          final setText = a11ySetText;
          if (setText == null) {
            return const JobResult(
              ok: false,
              error: 'a11y gateway unavailable on this build',
            );
          }
          final sOptions = _a11yOptions(job.inputs);
          if (st.arg != null) sOptions['target'] = st.arg;
          return await _guardedA11y(
            pkg: spkg,
            nodeId: snodeId,
            options: sOptions,
            invoke: () => setText(spkg, snodeId, stext, sOptions),
            defaultVerify: 'applied',
            // Confirmed ONLY by the native re-read comparing the field
            // content ('text_matched'). Applied-notes (content changed /
            // sensitive field not re-read) stay verified=false.
            verifyMatch: (s) => s.startsWith('text_matched'),
          );
        case 'gui.scroll':
          final gpkg = job.inputs['target_package'] ?? job.inputs['package'];
          final grawId = job.inputs['node_id'];
          if (gpkg is! String || !a11yTapTargets.contains(gpkg)) {
            return const JobResult(
              ok: false,
              error: 'target not in lab allowlist (refused, never executed)',
            );
          }
          final gnodeId = grawId is int && grawId >= 0 ? grawId : -1;
          if (gnodeId < 0) {
            return const JobResult(
              ok: false,
              error: 'bad node_id (need snapshot node)',
            );
          }
          final scroll = a11yScroll;
          if (scroll == null) {
            return const JobResult(
              ok: false,
              error: 'a11y gateway unavailable on this build',
            );
          }
          final dir = job.inputs['direction'];
          final forward = dir == null || '$dir'.toLowerCase() != 'backward';
          return await _guardedA11y(
            pkg: gpkg,
            nodeId: gnodeId,
            options: _a11yOptions(job.inputs),
            invoke: () =>
                scroll(gpkg, gnodeId, forward, _a11yOptions(job.inputs)),
            defaultVerify: 'requested (compare snapshots for movement)',
            // Scrolling has no automatic native re-read confirm: the
            // family verify is a caller-side snapshot content diff. So
            // scroll NEVER self-reports verified=true (the flag stays
            // false even on success — ack is not proof).
          );
        case 'gui.find':
          // Contract-v3 read-only element lookup (docs/UI_SNAPSHOT_CONTRACT
          // .md §8). Same lab allowlist as the acting family. A MATCH is an
          // honest OBSERVATION (element facts, nothing acted on);
          // not-found / ambiguous / bad-target are failures with
          // evidence — never guesses, never ok:true.
          final fpkg = job.inputs['target_package'] ?? job.inputs['package'];
          if (fpkg is! String || !a11yTapTargets.contains(fpkg)) {
            return const JobResult(
              ok: false,
              error: 'target not in lab allowlist (refused, never executed)',
            );
          }
          final ft = SemTarget.parse(job.inputs['target']);
          if (ft.refuse != null) {
            return JobResult(ok: false, error: ft.refuse!);
          }
          if (ft.arg == null) {
            return const JobResult(
              ok: false,
              error: 'missing target (selectors required)',
            );
          }
          final find = a11yFind;
          if (find == null) {
            return const JobResult(
              ok: false,
              error: 'a11y gateway unavailable on this build',
            );
          }
          final findOptions = _a11yOptions(job.inputs);
          findOptions['target'] = ft.arg;
          late final Map<String, dynamic> fm;
          try {
            fm = await find(fpkg, ft.arg!, findOptions);
          } catch (e) {
            // Coded bridge errors (mismatch, unavailable, denied, failed)
            // carry native evidence in details; merge it like taps do.
            String code = 'failed';
            Map<String, dynamic> evidence = const {};
            try {
              final dyn = e as dynamic;
              final c = dyn.code;
              if (c != null) code = '$c';
              final det = dyn.details;
              if (det is Map) {
                evidence = Map<String, dynamic>.from(det);
              }
            } catch (_) {
              // Not a coded bridge error; generic mapping below.
            }
            // Native target-verdict codes surface in the evidence map;
            // map them to honest refusals instead of the generic tail.
            final evCode = evidence['error'] ?? evidence['status'];
            if (evCode is String &&
                (evCode.startsWith('TARGET') ||
                    evCode.startsWith('INSECURE'))) {
              code = evCode;
            }
            return JobResult(
              ok: false,
              error: _tapCodeMessage(code),
              result: {'package': fpkg, 'bridge_code': code, ...evidence},
            );
          }
          final status = '${fm['status'] ?? 'find failed'}';
          if (status == 'MATCH' && fm['element'] is Map) {
            return JobResult(
              ok: true,
              result: {
                'status': 'MATCH',
                'package': '${fm['package'] ?? fpkg}',
                'element': Map<String, dynamic>.from(fm['element']),
                if (fm['snapshot_id'] != null)
                  'snapshot_id': '${fm['snapshot_id']}',
                if (fm['contract_version'] != null)
                  'contract_version': fm['contract_version'],
                'note':
                    '${fm['note'] ?? 'read-only lookup; no element was acted on'}',
              },
            );
          }
          return JobResult(
            ok: false,
            error: _tapCodeMessage(status),
            result: Map<String, dynamic>.from(fm),
          );
        case 'gui.wait':
          // Stage 28: bounded read-only wait for an element to appear.
          // Reuses the SAME lab allowlist + semantic target contract as
          // gui.find, and polls the fresh accessibility tree with a
          // HARD attempt/total-time bound (no unbounded sleeps, no
          // input injection, nothing acted on). A timeout is an
          // honest matched:false RESULT — the wait did run, the
          // element simply never appeared; never ok:false, never a
          // guess, never an automatic retry of anything.
          final wpkg = job.inputs['target_package'] ?? job.inputs['package'];
          if (wpkg is! String || !a11yTapTargets.contains(wpkg)) {
            return const JobResult(
              ok: false,
              error: 'target not in lab allowlist (refused, never executed)',
            );
          }
          final wt = SemTarget.parse(job.inputs['target']);
          if (wt.refuse != null) {
            return JobResult(ok: false, error: wt.refuse!);
          }
          if (wt.arg == null) {
            return const JobResult(
              ok: false,
              error: 'missing target (selectors required)',
            );
          }
          final waitFind = a11yFind;
          if (waitFind == null) {
            return const JobResult(
              ok: false,
              error: 'a11y gateway unavailable on this build',
            );
          }
          final wOptions = _a11yOptions(job.inputs);
          wOptions['target'] = wt.arg!;
          // Core schema bounds timeout_s to <= 15 s; 5 s is the default
          // and anything larger is clamped. Attempts stay bounded by
          // elapsed time; the find probe is bounded per call natively.
          final rawTimeout = job.inputs['timeout_s'];
          final timeoutMs = rawTimeout is num && rawTimeout > 0
              ? (rawTimeout * 1000).round() > 15000
                  ? 15000
                  : (rawTimeout * 1000).round()
              : 5000;
          final sw = Stopwatch()..start();
          var wAttempts = 0;
          var wFound = false;
          Map<String, dynamic>? wHit;
          String wLastError = '';
          Map<String, dynamic> wEvidence = const {};
          while (sw.elapsedMilliseconds <= timeoutMs) {
            wAttempts += 1;
            try {
              final fm = await waitFind(wpkg, wt.arg!, wOptions);
              if ('${fm['status'] ?? ''}' == 'MATCH' && fm['element'] is Map) {
                // Fresh evidence for the matched condition: element
                // facts come from the same bounded snapshot source as
                // gui.find.
                wHit = Map<String, dynamic>.from(fm['element']);
                wEvidence =
                    fm['snapshot_id'] != null
                        ? {'snapshot_id': '${fm['snapshot_id']}'}
                        : const {};
                wFound = true;
              }
            } catch (e) {
              final dec = _decodeBridgeError(e);
              final dc = '${dec['code']}';
              final dEv = dec['evidence'] as Map<String, dynamic>;
              wLastError = dc;
              if (dEv.isNotEmpty) wEvidence = dEv;
              // Terminal structural conditions stop the wait honestly:
              // the gateway went away or the guard says the screen is
              // somewhere else. Normal not-found codes keep polling.
              if (dc == 'unavailable' ||
                  dc == 'NO_WINDOW' ||
                  dc == 'UNEXPECTED_PACKAGE' ||
                  dc == 'denied') {
                return JobResult(
                  ok: false,
                  error: _tapCodeMessage(dc),
                  result: {
                    'package': wpkg,
                    'matched': false,
                    'attempts': wAttempts,
                    'waited_ms': sw.elapsedMilliseconds,
                    'bridge_code': dc,
                    'verified': false,
                    ...dEv,
                  },
                );
              }
              // Not-found-family / ambiguous: keep waiting within the
              // bound (that is literally the job of this tool).
            }
            if (wFound) break;
            if (sw.elapsedMilliseconds < 500 && timeoutMs >= 500) {
              // Fixed bounded interval between checks (never busy-spin).
              await Future<void>.delayed(
                  const Duration(milliseconds: 500));
            }
          }
          if (wHit != null) {
            return JobResult(
              ok: true,
              result: {
                'package': wpkg,
                'matched': true,
                'status': 'matched',
                'attempts': wAttempts,
                'waited_ms': sw.elapsedMilliseconds,
                'element': wHit,
                'verified': true,
                if (wEvidence['snapshot_id'] != null)
                  'snapshot_id': wEvidence['snapshot_id'],
                'contract_version': '28',
              },
            );
          }
          return JobResult(
            ok: true,
            result: {
              'package': wpkg,
              'matched': false,
              'status': 'timeout',
              'attempts': wAttempts,
              'waited_ms': sw.elapsedMilliseconds,
              'verified': false,
              if (wLastError.isNotEmpty) 'bridge_code': wLastError,
              'note':
                  'element never appeared within the bounded wait '
                      '(read-only observation, nothing acted on)',
              ...wEvidence,
            },
          );
        case 'gui.back':
          final bpkg = job.inputs['target_package'] ?? job.inputs['package'];
          if (bpkg is! String || !a11yTapTargets.contains(bpkg)) {
            return const JobResult(
              ok: false,
              error: 'target not in lab allowlist (refused, never executed)',
            );
          }
          // Only back and home exist as supported global navigation
          // surfaces (recents/global-search/multi-window are refused).
          final rawDir = job.inputs['direction'];
          final dir = rawDir == null
              ? 'back'
              : ('$rawDir'.toLowerCase() == 'home' ? 'home' : 'back');
          final navigate = a11yNavigate;
          if (navigate == null) {
            return const JobResult(
              ok: false,
              error: 'a11y gateway unavailable on this build',
            );
          }
          late final Map<String, dynamic> bm;
          try {
            bm = await navigate(dir, bpkg, _a11yOptions(job.inputs));
          } catch (e) {
            // Same refusals as contract actions: channel error codes map
            // to structured failures, never fake navigation successes.
            String code = 'failed';
            Map<String, dynamic> evidence = const {};
            try {
              final dyn = e as dynamic;
              final c = dyn.code;
              if (c != null) code = '$c';
              final det = dyn.details;
              if (det is Map) evidence = Map<String, dynamic>.from(det);
            } catch (_) {
              // Not a coded bridge error; fall through to generic mapping.
            }
            return JobResult(
              ok: false,
              error: _tapCodeMessage(code),
              result: {
                'package': bpkg,
                'direction': dir,
                'bridge_code': code,
                ...evidence,
              },
            );
          }
          if (bm['applied'] != true) {
            return JobResult(
              ok: false,
              error:
                  'navigation not applied: '
                  '${bm['error'] ?? bm['reason'] ?? 'refused'}',
              result: {'package': bpkg, 'direction': dir, ...bm},
            );
          }
          return JobResult(
            ok: true,
            result: {
              'package': bpkg,
              'direction': dir,
              'package_before': bm['package_before'],
              'package_after': bm['package_after'],
              'fresh_snapshot_id': bm['fresh_snapshot_id'],
              'verify_state': '${bm['verify_state'] ?? 'applied'}',
              'verify': 'gui.screen.inspect',
              // Stage 29 honest verification: native re-reads the
              // foreground package after navigation and attaches a fresh
              // snapshot; the observed package_after IS the confirm.
              // Missing fresh snapshot → honestly unverified.
              'verified':
                  '${bm['fresh_snapshot_id'] ?? ''}'.isNotEmpty &&
                      bm['package_after'] != null,
            },
          );
        case 'android.observe':
          final fn = observedDevices;
          if (fn == null) {
            return const JobResult(
              ok: false,
              error: 'observed-device gateway unavailable on this build',
            );
          }
          final m = await fn();
          if (m['supported'] == false) {
            return const JobResult(
              ok: true,
              result: {
                'supported': false,
                'detail':
                    'device discovery not supported on this Android '
                    'hardware/build (honest abstain)',
              },
            );
          }
          final String trust =
              m['trust_note'] as String? ?? 'detection is not trust';
          final rows = (m['devices'] is List) ? m['devices'] as List : const [];
          final Map<String, int> bySrc = {};
          for (final r in rows) {
            final s = (r is Map) ? '${r['src'] ?? 'unknown'}' : 'unknown';
            bySrc[s] = (bySrc[s] ?? 0) + 1;
          }
          return JobResult(
            ok: true,
            result: {
              'supported': true,
              'count': rows.length,
              'by_source': bySrc,
              'trust_note': trust,
              'privacy_note':
                  'listing only; serials and vendor ids are not reported',
              'devices': rows,
            },
          );

        case 'android.storage.stats':
          final fn = mediaCounts;
          if (fn == null) {
            return const JobResult(
              ok: false,
              error: 'storage-stats gateway unavailable on this build',
            );
          }
          final m = await fn();
          if (m['supported'] == false) {
            return const JobResult(
              ok: true,
              result: {
                'supported': false,
                'detail': 'storage statistics not supported on this build',
              },
            );
          }
          final counts = (m['counts'] is Map)
              ? Map<String, dynamic>.from(m['counts'] as Map)
              : const <String, dynamic>{};
          final storage = (m['storage'] is Map)
              ? Map<String, dynamic>.from(m['storage'] as Map)
              : const <String, dynamic>{};
          final needs = (m['permissions_needed'] is List)
              ? m['permissions_needed'] as List
              : const [];
          return JobResult(
            ok: true,
            result: {
              'supported': true,
              'counts': counts,
              'permissions_needed': needs,
              'storage': storage,
              'disclaimer': 'counts are statistics only; no media read',
            },
          );

        case 'android.apps.list':
          final listing = appsList;
          if (listing == null) {
            return const JobResult(
              ok: false,
              error: 'app-listing gateway unavailable on this build',
            );
          }
          final rawLimit = job.inputs['limit'];
          final limit = rawLimit is int ? rawLimit.clamp(1, 200) : 100;
          try {
            final m = await listing(limit);
            final rows = m['apps'];
            if (rows is! List) {
              return const JobResult(
                ok: false,
                error:
                    'app listing returned an unexpected '
                    'shape (refused, never guessed)',
              );
            }
            return JobResult(
              ok: true,
              result: {
                'requested_limit': limit,
                'count': m['count'] is int ? m['count'] : rows.length,
                'truncated': m['truncated'] == true,
                'apps': [
                  for (final row in rows.whereType<Map>())
                    {
                      'package': '${row['package'] ?? ''}',
                      'label': '${row['label'] ?? ''}',
                    },
                ],
                'note':
                    'discovery metadata only; not an authorization to '
                    'open or automate any app',
              },
            );
          } catch (e) {
            return JobResult(ok: false, error: 'app listing failed: $e');
          }
        case 'android.notifications.read':
          final reader = notifList;
          if (reader == null) {
            return const JobResult(
              ok: false,
              error: 'notification gateway unavailable on this build',
            );
          }
          final rawSince = job.inputs['since_ms'];
          final rawLimit = job.inputs['limit'];
          final params = <String, dynamic>{
            'since_ms': rawSince is int ? rawSince : 0,
            'limit': rawLimit is int ? rawLimit.clamp(1, 50) : 20,
          };
          late final Map<String, dynamic> nm;
          try {
            nm = await reader(params);
          } catch (e) {
            return JobResult(ok: false, error: 'notification read failed: $e');
          }
          if (nm['granted'] != true) {
            // Never fake content; the missing user grant is the truth
            // the job result reports.
            return const JobResult(
              ok: false,
              error:
                  'notification listener access not granted '
                  '(grant in Settings > Special access > Device & app '
                  'notifications; Zara never opens Settings itself)',
            );
          }
          final rows = nm['items'];
          if (rows is! List) {
            return const JobResult(
              ok: false,
              error:
                  'notification read returned an '
                  'unexpected shape (refused, never guessed)',
            );
          }
          return JobResult(
            ok: true,
            result: {
              'count': nm['count'] is int ? nm['count'] : rows.length,
              'truncated': nm['truncated'] == true,
              'items': [
                for (final row in rows.whereType<Map>())
                  {
                    'posted_at_ms': row['posted_at_ms'],
                    'package': '${row['package'] ?? ''}',
                    'title': '${row['title'] ?? ''}',
                    'text': '${row['text'] ?? ''}',
                    'category': '${row['category'] ?? ''}',
                  },
              ],
              'note':
                  'passive listener read; secret-looking material is '
                  'scrubbed at capture; contents stay sensitive — results '
                  'must still pass audit redaction',
            },
          );
        // ---- Stage 31-P3 phase A: quick/one-shot actions ----------
        case 'android.torch.set':
          final fn = torchSet;
          if (fn == null) {
            return const JobResult(
              ok: false, error: 'torch gateway unavailable on this build');
          }
          final m = await fn(job.inputs['on'] == true);
          if (m['available'] == false) {
            return JobResult(
              ok: true,
              result: {
                'supported': true,
                'available': false,
                'detail': '${m['detail'] ?? 'torch unavailable'}',
              },
            );
          }
          // Stage-29-shaped honesty: the native call dispatched; if the
          // torch re-read could not confirm, the verdict 'unverified'
          // rides the result EXPLICITLY (ok:true — the execution ran,
          // verification is honestly absent) while a re-read that
          // REFUTES the request stays a real failure. Neither outcome
          // manufactures an applied claim.
          return JobResult(
            ok: (m['verdict'] ?? 'unverified') != 'not-applied',
            error: m['verdict'] == 'not-applied'
                ? 'torch not-applied: re-read shows the old state'
                : '',
            result: {
              'torch': job.inputs['on'] == true,
              'verdict': '${m['verdict'] ?? 'unverified'}',
              'verification': '${m['verification'] ?? 'none available'}',
              'verified': '${m['verdict'] ?? ''}' == 'applied',
              if (m['detail'] is String) 'detail': m['detail'],
            },
          );
        case 'android.volume.set':
          final fn = streamVolumeSet;
          if (fn == null) {
            return const JobResult(
              ok: false,
              error: 'volume gateway unavailable on this build',
            );
          }
          final raw = job.inputs['pct'];
          if (raw is! int) {
            return const JobResult(
              ok: false, error: 'pct (int 0-100) required');
          }
          final m = await fn(raw.clamp(0, 100));
          if (m['verdict'] != 'applied') {
            return JobResult(
              ok: false,
              error: 'volume ${m['verdict'] ?? 'not-applied'}: '
                  '${m['detail'] ?? 're-read did not confirm'}',
            );
          }
          return JobResult(
            ok: true,
            result: {
              'before_pct': m['before_pct'],
              'after_pct': m['after_pct'],
              'verdict': m['verdict'],
              'verification': m['verification'],
              'verified': m['verdict'] == 'applied',
            },
          );
        case 'android.ring':
          final fn = findMyPhoneRing;
          if (fn == null) {
            return const JobResult(
              ok: false, error: 'ring gateway unavailable on this build');
          }
          final raw = job.inputs['seconds'];
          final m = await fn(
            raw is int ? raw.clamp(3, 30) : 15,
            job.inputs['allow_dnd_bypass'] == true,
          );
          if (m['rang'] != true) {
            return JobResult(
              ok: false,
              error: '${m['detail'] ?? 'ring refused'}',
            );
          }
          return JobResult(
            ok: true,
            result: {
              'rang': true,
              'seconds': m['seconds'],
              'verification': m['verification'],
              'verified': true,
            },
          );
        case 'android.media.playback':
          final fn = mediaKeyEvent;
          if (fn == null) {
            return const JobResult(
              ok: false, error: 'media-key gateway unavailable on this build');
          }
          final action = '${job.inputs['action'] ?? ''}';
          final m = await fn(action);
          if (m['ok'] != true) {
            return JobResult(ok: false, error: '${m['error'] ?? 'refused'}');
          }
          return JobResult(
            ok: true,
            result: {
              'action': action,
              'verdict': m['verdict'],
              'verification': m['verification'],
              'verified': false,
            },
          );
        case 'android.clipboard.set':
          final fn = clipboardSet;
          if (fn == null) {
            return const JobResult(
              ok: false,
              error: 'clipboard gateway unavailable on this build',
            );
          }
          final text = job.inputs['text'];
          if (text is! String || text.isEmpty) {
            return const JobResult(
              ok: false, error: 'text required');
          }
          final m = await fn(text);
          if (m['applied'] != true) {
            return JobResult(
              ok: false,
              error: 'clipboard ${m['verdict'] ?? 'refused'}: '
                  '${m['detail'] ?? 'write not verified'}',
            );
          }
          // NO echo of the text — only length + verdict.
          return JobResult(
            ok: true,
            result: {
              'applied': true,
              'length': m['length'],
              'verification': m['verification'],
              'verified': true,
            },
          );
        case 'android.open_uri':
          final fn = openUri;
          if (fn == null) {
            return const JobResult(
              ok: false, error: 'open-uri gateway unavailable on this build');
          }
          final uri = job.inputs['uri'];
          if (uri is! String || uri.isEmpty) {
            return const JobResult(ok: false, error: 'uri required');
          }
          final m = await fn(uri);
          if (m['fired'] != true) {
            return JobResult(
              ok: false,
              error: '${m['detail'] ?? m['error'] ?? 'view refused'}',
            );
          }
          return JobResult(
            ok: true,
            result: {
              'fired': true,
              'scheme': m['scheme'],
              'verdict': m['verdict'],
              'verification': m['verification'],
              'verified': false,
            },
          );
        case 'android.power_saver.set':
          final fn = powerSaverSet;
          if (fn == null) {
            return const JobResult(
              ok: false,
              error: 'power-saver gateway unavailable on this build',
            );
          }
          final m = await fn(job.inputs['on'] == true);
          if (m['granted'] == false) {
            return JobResult(ok: false, error: '${m['detail']}');
          }
          if (m['verdict'] != 'applied') {
            return JobResult(
              ok: false,
              error: 'power saver ${m['verdict'] ?? 'not-applied'}: '
                  '${m['detail'] ?? 're-reads did not confirm'}',
            );
          }
          return JobResult(
            ok: true,
            result: {
              'power_save': job.inputs['on'] == true,
              'before': m['before'],
              'verdict': m['verdict'],
              'verification': m['verification'],
              'verified': m['verdict'] == 'applied',
            },
          );
        case 'android.alarm.set':
          final fn = alarmSet;
          if (fn == null) {
            return const JobResult(
              ok: false, error: 'alarm gateway unavailable on this build');
          }
          final hour = job.inputs['hour'];
          final minute = job.inputs['minute'];
          final m = await fn(
            hour is int ? hour : null,
            minute is int ? minute : null,
            job.inputs['label'] is String
                ? job.inputs['label'] as String
                : null,
          );
          if (m['fired'] != true) {
            return JobResult(
              ok: false,
              error: '${m['detail'] ?? m['error'] ?? 'alarm refused'}',
            );
          }
          return JobResult(
            ok: true,
            result: {
              'verdict': m['verdict'],
              'hour': m['hour'],
              'minute': m['minute'],
              'verification': m['verification'],
              'verified': false,
            },
          );
        case 'android.timer.set':
          final tfn = timerSet;
          if (tfn == null) {
            return const JobResult(
              ok: false, error: 'timer gateway unavailable on this build');
          }
          final minutes = job.inputs['minutes'];
          final m = await tfn(
            minutes is int ? minutes : null,
            job.inputs['label'] is String
                ? job.inputs['label'] as String
                : null,
          );
          if (m['fired'] != true) {
            return JobResult(
              ok: false,
              error: '${m['detail'] ?? m['error'] ?? 'timer refused'}',
            );
          }
          return JobResult(
            ok: true,
            result: {
              'verdict': m['verdict'],
              'minutes': m['minutes'],
              'verification': m['verification'],
              'verified': false, // hand-off verdict, like the alarm
            },
          );
        // ---- P5 feature-hunt ② (2026-10-22): DND state + toggle
        case 'android.dnd.get':
          final gfn = dndGet;
          if (gfn == null) {
            return const JobResult(ok: false,
                error: 'dnd gateway unavailable on this build');
          }
          final m = await gfn();
          if (m['ok'] != true) {
            return JobResult(ok: false,
                error: '${m['detail'] ?? m['error'] ?? 'dnd read refused'}');
          }
          return JobResult(ok: true, result: {
            'filter': m['filter'],
            'filter_name': m['filter_name'],
            'dnd_on': m['dnd_on'],
            'policy_access': m['policy_access'],
          });
        case 'android.dnd.set':
          final sfn = dndSet;
          if (sfn == null) {
            return const JobResult(ok: false,
                error: 'dnd gateway unavailable on this build');
          }
          final m = await sfn(job.inputs['enabled'] is bool
              ? job.inputs['enabled'] as bool
              : null);
          if (m['fired'] != true || m['ok'] != true) {
            return JobResult(
              ok: false,
              error: '${m['detail'] ?? m['error'] ?? 'dnd refused'}',
            );
          }
          return JobResult(ok: true, result: {
            'verdict': 'verified-by-reread',
            'filter': m['filter'],
            'verification': m['verification'],
            // The setInterruptionFilter result + re-read agree.
            'verified': m['verified'] == true,
          });
        // ---- Stage 31-P3 phase B (P3d): call with contact resolution
        case 'android.contacts.lookup':
          final fn = contactsLookup;
          if (fn == null) {
            return const JobResult(ok: false,
                error: 'contacts gateway unavailable on this build');
          }
          final m = await fn(
            job.inputs['name'] is String
                ? job.inputs['name'] as String
                : null,
            job.inputs['limit'] is int ? job.inputs['limit'] as int : 5,
          );
          if (m['available'] == false) {
            return JobResult(
              ok: false,
              error: '${m['detail'] ?? 'READ_CONTACTS not granted'}',
            );
          }
          if (m['ok'] != true) {
            return JobResult(
              ok: false,
              error: '${m['error'] ?? m['detail'] ?? 'contacts lookup refused'}',
            );
          }
          // A zero-match read is an HONEST ANSWER (the user's standing
          // spec starts the alias flow from it) — ok:true, count 0.
          return JobResult(
            ok: true,
            result: {
              'query': m['query'],
              'count': m['count'] ?? 0,
              'matches': m['matches'] ?? const [],
              'verify_state': 'rows returned from ContactsContract',
            },
          );
        case 'android.call.place':
          final fn = callDirect;
          if (fn == null) {
            return const JobResult(ok: false,
                error: 'call gateway unavailable on this build');
          }
          final m = await fn(
            job.inputs['number'] is String
                ? job.inputs['number'] as String
                : null,
          );
          if (m['available'] == false) {
            return JobResult(
              ok: false,
              error: '${m['detail'] ?? 'CALL_PHONE not granted'}',
            );
          }
          if (m['ok'] != true) {
            return JobResult(
              ok: false,
              error: '${m['error'] ?? m['detail'] ?? 'call refused'}',
            );
          }
          // ACTION_CALL fired: the act is NOT its own proof — the
          // bounded a11y poll (250 ms x wait_ms window) decides
          // confirmed/absent; a confirmed call screen is what "verified"
          // means here, and absence stays honest data.
          final poll = callScreenPoll == null
              ? const <String, dynamic>{'verdict': 'absent',
                  'verification': 'poll gateway unavailable on this build'}
              : await callScreenPoll!(
                  job.inputs['wait_ms'] is int
                      ? job.inputs['wait_ms'] as int
                      : 4000);
          final verdict = '${poll['verdict'] ?? 'absent'}';
          return JobResult(
            ok: true,
            result: {
              'fired': true,
              'number_tail': _tail(job.inputs['number'] is String
                  ? job.inputs['number'] as String
                  : ''),
              'verdict': verdict,
              'verification': poll['verification'],
              if (poll['package'] is String) 'package': poll['package'],
              'verified': verdict == 'confirmed',
            },
          );
        // ---- Stage 31-P3 phase B (P3e): ONE carrier SMS
        case 'android.sms.send':
          final fn = smsSend;
          if (fn == null) {
            return const JobResult(ok: false,
                error: 'sms gateway unavailable on this build');
          }
          final m = await fn(
            job.inputs['number'] is String
                ? job.inputs['number'] as String
                : null,
            job.inputs['text'] is String
                ? job.inputs['text'] as String
                : null,
          );
          if (m['available'] == false) {
            return JobResult(
              ok: false,
              error: '${m['detail'] ?? 'SEND_SMS not granted'}',
            );
          }
          if (m['ok'] != true) {
            return JobResult(
              ok: false,
              error:
                  '${m['error'] ?? m['detail'] ?? 'sms send refused'}',
            );
          }
          // "sent" = the radio accepted the send. Carrier delivery is
          // NOT confirmable by a bounded re-read — verified stays false
          // (honest), exactly like media keys and open_uri. The DRAFT
          // never enters the result: length only (same rule as
          // clipboard); the number enters as its masked tail.
          return JobResult(
            ok: true,
            result: {
              'verdict': 'sent',
              'number_tail': _tail(job.inputs['number'] is String
                  ? job.inputs['number'] as String
                  : ''),
              'text_len': m['text_len'] ?? -1,
              'verification': m['verification'],
              'verified': false,
            },
          );
        // ---- Stage 31-P3 phase B (P3f): consented OTP read
        case 'android.otp.read':
          final fn = otpScan;
          if (fn == null) {
            return const JobResult(ok: false,
                error: 'otp gateway unavailable on this build');
          }
          final m = await fn(
            job.inputs['max_age_ms'] is int
                ? job.inputs['max_age_ms'] as int
                : 300000,
            job.inputs['sender_hint'] is String
                ? job.inputs['sender_hint'] as String
                : null,
          );
          if (m['supported'] != true) {
            return JobResult(
              ok: false,
              error:
                  '${m['detail'] ?? 'notification listener unavailable'}',
            );
          }
          // Candidates pass through (age, package, title, code).
          // Freshness + count are data; a zero-count scan is an HONEST
          // answer (no fresh code), never a retry or a guess.
          final candidates = m['candidates'] is List
              ? List<Map<String, dynamic>>.from(
                  (m['candidates'] as List)
                      .whereType<Map>()
                      .map((c) => Map<String, dynamic>.from(c)))
              : const <Map<String, dynamic>>[];
          return JobResult(
            ok: true,
            result: {
              'count': m['count'] ?? 0,
              'fresh_count': m['fresh_count'] ?? 0,
              'candidates': candidates,
              'verification': m['verification'],
            },
          );
        // ---- Stage 31-P3 phase B (P3a): targeted media control
        case 'android.media.control':
          final fn = mediaSessionTargeted;
          if (fn == null) {
            return const JobResult(ok: false,
                error: 'media session gateway unavailable on this build');
          }
          final m = await fn(
            job.inputs['hint'] is String
                ? job.inputs['hint'] as String
                : null,
            job.inputs['action'] is String
                ? job.inputs['action'] as String
                : null,
          );
          if (m['supported'] != true) {
            return JobResult(
              ok: false,
              error: '${m['detail'] ?? 'media session controller unavailable'}',
            );
          }
          if (m['ok'] != true) {
            return JobResult(
              ok: false,
              error: '${m['error'] ?? 'media control refused'}',
            );
          }
          // Honest verdicts: count 0 / many matches ride ok:true with
          // the bounded listing (an ANSWER); a sent transport command
          // reports the before/after playback states.
          return JobResult(
            ok: true,
            result: {
              'count': m['count'] ?? -1,
              if (m['active'] is List) 'active':
                  List<String>.from(m['active'] as List),
              if (m['package'] is String) 'package': m['package'],
              if (m['action'] is String) 'action': m['action'],
              if (m['state_before'] is String) 'state_before':
                  m['state_before'],
              if (m['state_after'] is String) 'state_after':
                  m['state_after'],
              'verification': m['verification'],
            },
          );
        // ---- Stage 31-P3 phase B (P3c): photo inventory (counts
        // only). A zero library is an honest answer; folders ride as
        // top-level paths (names never leave the device).
        case 'android.photos.inventory':
          final fn = photosInventory;
          if (fn == null) {
            return const JobResult(ok: false,
                error: 'inventory gateway unavailable on this build');
          }
          final m = await fn();
          if (m['supported'] != true) {
            return JobResult(
              ok: false,
              error: '${m['detail'] ?? 'photo inventory unavailable'}',
            );
          }
          return JobResult(
            ok: true,
            result: {
              'count': m['count'] ?? -1,
              'total_bytes': m['total_bytes'] ?? -1,
              'newest_ms': m['newest_ms'] ?? -1,
              'oldest_ms': m['oldest_ms'] ?? -1,
              if (m['folders'] is List) 'folders':
                  List<String>.from(m['folders'] as List),
              'verification': m['verification'],
            },
          );
        case 'system.battery.charging_time':
          final b = battery();
          final charging = b['charging'] == true;
          final pct = b['battery_pct'];
          // Single snapshot cannot estimate charge time: report honestly
          // (null estimate) instead of inventing a duration.
          return JobResult(
            ok: true,
            result: {
              'battery_pct': pct,
              'charging': charging,
              'time_to_full_minutes': null,
              'note': charging
                  ? 'charging; single snapshot cannot estimate time-to-full'
                  : 'not charging; no estimate',
            },
          );
        case 'system.wifi.state':
        case 'system.bluetooth.state':
        case 'system.hotspot.state':
        case 'system.mobile_data.state':
        case 'system.airplane_mode.state':
          final probe = switch (job.tool) {
            'system.wifi.state' => wifiState,
            'system.bluetooth.state' => bluetoothState,
            'system.hotspot.state' => hotspotState,
            'system.mobile_data.state' => mobileDataState,
            _ => airplaneModeState,
          };
          if (probe == null) {
            return const JobResult(
              ok: false,
              error: 'sys-state gateway unavailable on this build',
            );
          }
          final s = await probe();
          if (s['supported'] == false) {
            return JobResult(
              ok: false,
              error: '${s['reason'] ?? s['error'] ?? 'unsupported'}',
              result: Map<String, dynamic>.from(s),
            );
          }
          return JobResult(ok: true, result: Map<String, dynamic>.from(s));
        case 'system.wifi.settings':
        case 'system.bluetooth.settings':
        case 'system.hotspot.settings':
        case 'system.mobile_data.settings':
        case 'system.airplane_mode.settings':
          final screen = job.tool.split('.')[1];
          if (!settingsScreens.contains(screen)) {
            return const JobResult(
              ok: false,
              error: 'unknown settings screen (refused)',
            );
          }
          final opener = openSettings;
          if (opener == null) {
            return const JobResult(
              ok: false,
              error: 'settings gateway unavailable on this build',
            );
          }
          final m = await opener(screen);
          return JobResult(ok: true, result: Map<String, dynamic>.from(m));
        case 'system.app.open':
          final pkg = job.inputs['package'];
          if (pkg is! String || pkg.isEmpty) {
            return const JobResult(
              ok: false,
              error:
                  'package not in app-open allowlist (refused, never executed)',
            );
          }
          // P6-c26: the Phase-1 allowlist stays the built-in trust
          // set. An OUT-of-allowlist package runs only through the
          // phone's OWN consent surface: a stored per-app grant (this
          // human, this phone, this package) or a fresh dialog tap.
          // Deny, dismissal, an absent surface, and no grant store
          // ALL fall to the byte-equal refusal — one honest ladder,
          // never a relaxed runner.
          var consented = false;
          if (!appOpenTargets.contains(pkg)) {
            final consents = appOpenConsents;
            if (consents != null && await consents.allows(pkg)) {
              consented = true;
            } else {
              if (appConsent == null || consents == null) {
                return const JobResult(
                  ok: false,
                  error:
                      'package not in app-open allowlist (refused, never executed)',
                );
              }
              final answer = await appConsent!(pkg);
              if (answer != true) {
                final reason = answer == false
                    ? 'phone consent declined for $pkg'
                    : 'phone consent not granted for $pkg';
                return JobResult(
                  ok: false,
                  error: '$reason (refused, never executed)',
                );
              }
              await consents.grant(pkg);
              if (!await consents.allows(pkg)) {
                return JobResult(
                  ok: false,
                  error: 'phone grant for $pkg could not be stored '
                      '— open refused, never executed',
                );
              }
              consented = true;
            }
          }
          final fn = openApp;
          if (fn == null) {
            return const JobResult(
              ok: false,
              error: 'app-open gateway unavailable on this build',
            );
          }
          final m = await fn(pkg);
          return JobResult(
            ok: true,
            result: {
              'package': '${m['package'] ?? pkg}',
              'launched': m['launched'] == true,
              // Focus verification from the device (may be honest
              // focus_unconfirmed when a11y cannot observe).
              'verify_state': '${m['verify_state'] ?? 'requested'}',
              'verify': '${m['verify'] ?? 'gui.screen.inspect'}',
              // P6-c26: true ONLY when the phone's own consent
              // surface granted this package; allowlisted packages
              // launched without a consent decision report false.
              'consented': consented,
            },
          );
        case 'system.wifi.set':
        case 'system.bluetooth.set':
        case 'system.hotspot.set':
        case 'system.mobile_data.set':
        case 'system.airplane_mode.set':
          final want = job.inputs['enabled'];
          if (want is! bool) {
            // No direction (or a non-boolean): refuse without touching
            // any gateway. The intent layer normally fills `enabled`;
            // a bare capability call clarifies instead of guessing.
            return const JobResult(
              ok: false,
              error: 'bad enabled (need {enabled: boolean}, refused)',
            );
          }
          final setter = switch (job.tool) {
            'system.wifi.set' => setWifi,
            'system.bluetooth.set' => setBluetooth,
            'system.hotspot.set' => setHotspot,
            'system.mobile_data.set' => setMobileData,
            _ => setAirplaneMode,
          };
          if (setter == null) {
            return const JobResult(
              ok: false,
              error: 'sys-set gateway unavailable on this build',
            );
          }
          try {
            final r = await setter(want);
            if (r['supported'] == false) {
              return JobResult(
                ok: false,
                error: '${r['reason'] ?? r['error'] ?? 'unsupported'}',
                result: Map<String, dynamic>.from(r),
              );
            }
            // applied:false (settings-UI / consent / deployment-blocked)
            // is an HONEST terminal outcome, not a failure to retry:
            // the device did exactly what the tier allows and said so.
            return JobResult(ok: true, result: Map<String, dynamic>.from(r));
          } catch (e) {
            return JobResult(ok: false, error: 'runner fault: $e');
          }
        default:
          // NOT an error to retry: structured refusal, Core sees it as done.
          return JobResult(
            ok: false,
            error:
                'tool not allowlisted on android: ${job.tool} (refused, never executed)',
          );
      }
    } catch (e) {
      return JobResult(ok: false, error: 'runner fault: $e');
    }
  }
}
