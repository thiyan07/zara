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

class AndroidJobRunner {
  final BatteryProbe battery;
  final NetworkProbe network;
  final Set<String> _seen = {};
  static const int maxSeen = 200;

  AndroidJobRunner({required this.battery, required this.network});

  static const allowlisted = {'system.battery', 'system.network'};

  JobResult run(DeviceJob job, {CancelToken? cancel}) {
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
