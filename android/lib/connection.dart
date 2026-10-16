// Zara Android connection layer — robust Core link, governor-aware.
//
// Design: bounded retries (never infinite), battery-critical devices do NOT
// auto-retry, every request carries an ID so stale responses are dropped,
// offline work goes into a BOUNDED queue (heartbeats coalesce). Pure Dart.
class CoreLinkState {
  static const disconnected = 'disconnected';
  static const connecting = 'connecting';
  static const online = 'online';
  static const degraded = 'degraded';
  static const reconnecting = 'reconnecting';
  static const offline = 'offline';
  static const error = 'error';

  static const allowed = {
    disconnected: {connecting, offline},
    connecting: {online, degraded, offline, error},
    online: {degraded, reconnecting, offline, disconnected, error},
    degraded: {online, reconnecting, offline, error},
    reconnecting: {online, degraded, offline, error},
    offline: {connecting, reconnecting},
    error: {connecting, disconnected, offline},
  };
}

/// Bounded exponential backoff. maxAttempts then the caller MUST go offline;
/// there is no infinite loop. Battery-critical disables auto-retry.
class RetryPolicy {
  final int maxAttempts;
  final Duration initialDelay;
  final Duration maxDelay;
  const RetryPolicy({
    this.maxAttempts = 5,
    this.initialDelay = const Duration(seconds: 1),
    this.maxDelay = const Duration(seconds: 60),
  });

  Duration delayFor(int attempt) {
    final shift = attempt.clamp(0, 10); // cap: never shifts into overflow
    final d = initialDelay * (1 << shift);
    return d > maxDelay ? maxDelay : d;
  }

  /// attempt is 0-based count of failures so far.
  bool shouldRetry(int attempt, {required bool batteryCritical}) {
    if (batteryCritical) return false; // manual reconnect only
    return attempt < maxAttempts;
  }
}

/// Equal-jitter backoff: half deterministic, half random, so a fleet of
/// devices reconnecting after the same outage does not thundering-herd
/// Core. Pure for testing: [roll] must be in [0, 1); production passes
/// Random().nextDouble(). Result is always in [base/2, base].
Duration applyJitter(Duration base, double roll) {
  assert(roll >= 0 && roll < 1, 'roll must be in [0, 1)');
  final half = base.inMicroseconds ~/ 2;
  return Duration(microseconds: half + (half * roll).round());
}

int _reqSeq = 0;

/// Opaque request ID: monotonic + timestamp. Stale responses (wrong ID)
/// are dropped, so a slow retry never overwrites fresher state.
String newRequestId() {
  _reqSeq += 1;
  return 'req-${DateTime.now().microsecondsSinceEpoch}-$_reqSeq';
}

/// Idempotency key for execution submits: unique per user action (NOT
/// derived from tool+inputs, so intentional repeats still run twice).
/// The same key is reused across transport retries of one action, so
/// Core collapses them into a single execution. Matches the server's
/// [A-Za-z0-9_.:-]{1,128} envelope.
String newIdempotencyKey(String tool) {
  _reqSeq += 1;
  final safe = tool.replaceAll(RegExp(r'[^A-Za-z0-9_.:-]'), '-');
  final t = safe.length <= 40 ? safe : safe.substring(0, 40);
  return 'z-$t-${DateTime.now().microsecondsSinceEpoch}-$_reqSeq';
}

/// A unit of offline-tolerant work. Heartbeats coalesce (only the newest
/// is kept); everything else is FIFO up to [maxSize], then rejected.
class PendingOp {
  final String id;
  final String kind; // 'heartbeat' | 'events' | 'job-result' | 'ack'
  final Map<String, dynamic> payload;
  PendingOp(this.kind, this.payload) : id = newRequestId();
}

class PendingQueue {
  final int maxSize;
  final List<PendingOp> _ops = [];
  PendingQueue({this.maxSize = 50});

  /// Returns false when full (caller reports truthful offline state).
  bool add(PendingOp op) {
    if (op.kind == 'heartbeat') {
      _ops.removeWhere((o) => o.kind == 'heartbeat');
    }
    if (_ops.length >= maxSize) return false;
    _ops.add(op);
    return true;
  }

  List<PendingOp> drain() {
    final out = List<PendingOp>.from(_ops);
    _ops.clear();
    return out;
  }

  int get length => _ops.length;
  bool get isEmpty => _ops.isEmpty;
}

/// Guards against stale responses: only the newest request ID per slot
/// is current; older arrivals are dropped, never applied.
class ResponseGuard {
  final Map<String, String> _current = {};
  void track(String slot, String requestId) => _current[slot] = requestId;
  bool isCurrent(String slot, String requestId) =>
      _current[slot] == requestId;
}
