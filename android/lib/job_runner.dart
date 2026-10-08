// Android job runner — the ONLY code that acts on Core-dispatched jobs.
//
// Authority rule: Core decides WHAT runs; this runner only executes tools
// on a hard allowlist with device-local data. Unknown tool names are
// REFUSED with a structured error — never exec'd, never shelled, never
// fetched from a URL. Pure Dart; bridge snapshots are injected.

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

/// Privileged executor (wired to DeviceBridge.privForceStop in main.dart,
/// faked in tests). Null means the gateway is unavailable on this build.
typedef ForceStopProbe = Future<Map<String, dynamic>> Function(String package);

/// A11y snapshot provider (wired to DeviceBridge.a11yInspect in main.dart,
/// faked in tests). Null means the gateway is unavailable on this build.
typedef A11yInspectProbe = Future<Map<String, dynamic>> Function();

/// A11y snapshot provider with params (Rung-3): expected_package,
/// max_elements, include_text, include_content_description.
typedef A11yInspectWithParamsProbe = Future<Map<String, dynamic>> Function(
    Map<String, dynamic> params);

/// A11y tap executor (wired to DeviceBridge.a11yTap in main.dart, faked in
/// tests). Null means the gateway is unavailable on this build.
typedef A11yTapProbe = Future<Map<String, dynamic>> Function(
    String package, int nodeId);

class AndroidJobRunner {
  final BatteryProbe battery;
  final NetworkProbe network;
  final ForceStopProbe? forceStop;
  final A11yInspectProbe? a11yInspect;
  final A11yInspectWithParamsProbe? a11yInspectWithParams;
  final A11yTapProbe? a11yTap;
  final Set<String> _seen = {};
  static const int maxSeen = 200;

  AndroidJobRunner(
      {required this.battery,
      required this.network,
      this.forceStop,
      this.a11yInspect,
      this.a11yInspectWithParams,
      this.a11yTap});

  static const allowlisted = {
    'system.battery',
    'system.network',
    'gui.screen.inspect',
    'gui.tap',
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

  Future<JobResult> run(DeviceJob job, {CancelToken? cancel}) async {
    if (_seen.contains(job.jobId)) {
      return const JobResult(
          ok: false, error: 'duplicate job refused (already handled)');
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
          return JobResult(ok: true, result: {
            'battery_pct': b['battery_pct'],
            'charging': b['charging'] == true,
            'power_save': b['power_save'] == true,
          });
        case 'system.network':
          final n = network();
          return JobResult(ok: true, result: {
            'network': n['network'] ?? 'unknown',
            'metered': n['metered'] == true,
          });
        case 'android.app.force_stop':
          final pkg = job.inputs['target_package'];
          if (pkg is! String || !forceStopLabTargets.contains(pkg)) {
            return const JobResult(
                ok: false,
                error:
                    'target not in lab allowlist (refused, never executed)');
          }
          final fn = forceStop;
          if (fn == null) {
            return const JobResult(
                ok: false,
                error: 'privileged gateway unavailable on this build');
          }
          final m = await fn(pkg);
          return JobResult(ok: true, result: {
            'package': '${m['package'] ?? pkg}',
            'stopped': m['stopped'] == true,
            'was_running': m['was_running'] == true,
            'verify_state': '${m['verify_state'] ?? 'unknown'}',
          });
        case 'gui.screen.inspect':
          // Prefer the params version if provided; fall back to legacy.
          final inspectParams = a11yInspectWithParams;
          final inspectLegacy = a11yInspect;
          if (inspectParams == null && inspectLegacy == null) {
            return const JobResult(
                ok: false,
                error: 'a11y gateway unavailable on this build');
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
                result: Map<String, dynamic>.from(snap));
          }
          return JobResult(ok: true, result: Map<String, dynamic>.from(snap));
        case 'gui.tap':
          final pkg = job.inputs['package'];
          final rawId = job.inputs['node_id'];
          if (pkg is! String || !a11yTapTargets.contains(pkg)) {
            return const JobResult(
                ok: false,
                error:
                    'target not in lab allowlist (refused, never executed)');
          }
          final nodeId = rawId is int
              ? rawId
              : rawId is num
                  ? rawId.toInt()
                  : -1;
          if (nodeId < 0) {
            return const JobResult(
                ok: false, error: 'bad node_id (need snapshot node)');
          }
          final tap = a11yTap;
          if (tap == null) {
            return const JobResult(
                ok: false,
                error: 'a11y gateway unavailable on this build');
          }
          final m = await tap(pkg, nodeId);
          return JobResult(ok: true, result: {
            'package': '${m['package'] ?? pkg}',
            'stopped': '${m['stopped'] ?? 'n/a'}',
            'applied': m['applied'] == true,
            'verify_state': '${m['verify_state'] ?? 'unknown'}',
          });
        default:
          // NOT an error to retry: structured refusal, Core sees it as done.
          return JobResult(
              ok: false,
              error:
                  'tool not allowlisted on android: ${job.tool} (refused, never executed)');
      }
    } catch (e) {
      return JobResult(ok: false, error: 'runner fault: $e');
    }
  }
}
