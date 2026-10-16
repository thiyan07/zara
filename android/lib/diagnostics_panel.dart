// Diagnostics panel: connection + provider + last error + governor flags
// + the explicit one-tap live provider check.
//
// Read-only over existing state plus TWO call families, both explicit-only:
//   GET  /v1/ready (public, no auth, no secrets)
//   POST /v1/diagnostics/provider-check (+ /cancel, operator Bearer auth)
// Nothing runs on open, nothing polls, nothing retries: Check starts one
// call, Retry re-starts one call after a terminal state, Cancel posts the
// cancel endpoint. Provider/model are display-only strings; secret-looking
// substrings anywhere near the error/success lines are redacted before
// render, so tokens/keys can never appear in widgets. Raw request/response
// bodies, URLs, keys, and tokens are never rendered — failure lines use
// fixed safe messages only.
import 'package:flutter/material.dart';

import 'core_client.dart';

/// Parsed GET /v1/ready. Missing keys read as unknown/false, never throw.
/// Pure for testing.
class ReadyInfo {
  final bool ready;
  final bool registry;
  final bool memoryWrite;
  final bool eventBus;
  final String provider;
  final bool governorV2;
  final bool governorShadow;
  const ReadyInfo({
    this.ready = false,
    this.registry = false,
    this.memoryWrite = false,
    this.eventBus = false,
    this.provider = '?',
    this.governorV2 = false,
    this.governorShadow = false,
  });

  static bool _flag(Object? v) => v == true;

  static ReadyInfo fromJson(Map<String, dynamic> m) {
    final checks =
        m['checks'] is Map ? Map<String, dynamic>.from(m['checks'] as Map) : {};
    final provider = checks['provider'];
    return ReadyInfo(
      ready: m['ready'] == true,
      registry: _flag(checks['registry']),
      memoryWrite: _flag(checks['memory_write']),
      eventBus: _flag(checks['event_bus']),
      provider: provider is String && provider.isNotEmpty ? provider : '?',
      governorV2: _flag(m['governor_v2']),
      governorShadow: _flag(m['governor_shadow']),
    );
  }
}

/// Redact secret-looking material before display: Bearer tokens,
/// sk-... keys, api-key/device-key/token/password assignments. Pure for
/// testing. Assignment-shaped only: free-form prose passes through, so
/// spoken free-text secrets outside assignments are a known limitation
/// (voice log lines keep transcripts for debugging; shape-matched
/// secrets do not land verbatim).
String redactSecretsForDisplay(String s) {
  var out = s.replaceAll(
      RegExp(r'Bearer\s+\S+', caseSensitive: false), 'Bearer [redacted]');
  out = out.replaceAll(RegExp(r'sk-[A-Za-z0-9_-]+'), '[redacted]');
  out = out.replaceAll(
      RegExp(r'(api[_-]?key|device[_-]?key|token|password|passwd|passcode|secret)\s*[:=]\s*\S+',
      caseSensitive: false), r'$1=[redacted]');
  return out;
}

/// Explicit one-line rendering of /v1/ready failures. Pure for testing.
String describeReadyError(Object e) {
  final s = e.toString();
  if (s.contains('401') || s.contains('403')) {
    return 'unauthorized (core refused readiness)';
  }
  return 'core unreachable';
}

/// Transport behind the live-check section of [DiagnosticsPanel].
/// Production runs on the shared [CoreClient] (operator Bearer auth);
/// widget tests inject a fake (socket-free).
abstract class LiveCheckBackend {
  /// POST /v1/diagnostics/provider-check. Never retried automatically.
  Future<LiveCheckResult> check();

  /// POST /v1/diagnostics/provider-check/cancel. True when a running
  /// check was signalled; false is an explicit server-side no-op.
  Future<bool> cancel();
}

class CoreLiveCheckBackend implements LiveCheckBackend {
  final CoreClient client;
  final TextEditingController? tokenCtrl; // shared operator token, if any
  const CoreLiveCheckBackend(this.client, [this.tokenCtrl]);

  void _applyToken() {
    final c = tokenCtrl;
    if (c == null) return;
    final tok = c.text.trim();
    client.operatorToken = tok.isEmpty ? null : tok;
  }

  @override
  Future<LiveCheckResult> check() async {
    _applyToken();
    return LiveCheckResult.fromJson(await client.providerCheck());
  }

  @override
  Future<bool> cancel() async {
    _applyToken();
    final out = await client.cancelProviderCheck();
    return out['cancelled'] == true;
  }
}

/// Terminal live-check outcome. `notChecked` is the initial state;
/// `checking` is the only non-terminal state besides it. There is no
/// auto-retry: every transition out of a terminal state needs an
/// explicit Check/Retry tap. Pure for testing.
enum LiveCheckState {
  notChecked,
  checking,
  success,
  busy,
  notLive,
  timeout,
  providerError,
  error,
}

/// Map a check failure to its explicit state. 409/422/504/502 each get
/// their own state (never silent, never retried); everything else —
/// including 401/403 — lands on [LiveCheckState.error] with its own
/// message from [describeLiveCheckError]. Pure for testing.
LiveCheckState classifyLiveCheckError(Object e) {
  final s = e.toString();
  if (s.contains('409')) return LiveCheckState.busy;
  if (s.contains('422')) return LiveCheckState.notLive;
  if (s.contains('504')) return LiveCheckState.timeout;
  if (s.contains('502')) return LiveCheckState.providerError;
  return LiveCheckState.error;
}

/// One-line, fixed safe message per live-check failure. Never embeds
/// the raw error (it may carry URLs, keys, or payload fragments) —
/// callers must render ONLY this string. Pure for testing.
String describeLiveCheckError(Object e) {
  switch (classifyLiveCheckError(e)) {
    case LiveCheckState.busy:
      return 'already running (another check is in progress)';
    case LiveCheckState.notLive:
      return 'not a live provider (mock/echo/scripted cannot be health-checked)';
    case LiveCheckState.timeout:
      return 'timed out (provider did not answer in time)';
    case LiveCheckState.providerError:
      return 'provider error (safe failure, nothing executed)';
    case LiveCheckState.error:
      final s = e.toString();
      if (s.contains('401') || s.contains('403')) {
        return 'unauthorized/forbidden (operator token missing/wrong)';
      }
      return 'core error';
    case LiveCheckState.notChecked:
    case LiveCheckState.checking:
    case LiveCheckState.success:
      return 'core error';
  }
}

/// Parsed 200 from POST /v1/diagnostics/provider-check
/// ({ok,provider,model,elapsed_s}). Missing keys read as unknown,
/// never throw. Pure for testing.
class LiveCheckResult {
  final String provider;
  final String model;
  final double? elapsedS;
  const LiveCheckResult({
    this.provider = '?',
    this.model = '?',
    this.elapsedS,
  });

  static LiveCheckResult fromJson(Map<String, dynamic> m) {
    final el = m['elapsed_s'];
    final provider = m['provider'];
    final model = m['model'];
    return LiveCheckResult(
      provider:
          provider is String && provider.isNotEmpty ? provider : '?',
      model: model is String && model.isNotEmpty ? model : '?',
      elapsedS: el is num ? el.toDouble() : null,
    );
  }
}

/// Transport behind [DiagnosticsPanel]. Production runs on the shared
/// [CoreClient]; widget tests inject a fake (socket-free).
abstract class ReadyBackend {
  Future<ReadyInfo> fetch();
}

class CoreReadyBackend implements ReadyBackend {
  final CoreClient client;
  const CoreReadyBackend(this.client);

  @override
  Future<ReadyInfo> fetch() async =>
      ReadyInfo.fromJson(await client.ready());
}

/// Connection status (existing state) + provider (display only) + last
/// request error (redacted) + governor flags + the explicit one-tap
/// live provider check. Backend calls happen ONLY on explicit button
/// taps — nothing runs on open, nothing polls, nothing retries.
class DiagnosticsPanel extends StatefulWidget {
  final CoreClient client;
  final String connection;
  final String lastError;

  /// Shared operator-token field from the Tasks section (live check is
  /// operator-authed). Null means "use the token already on [client]".
  final TextEditingController? tokenCtrl;

  /// Test seams: production passes null (calls run on [client]).
  final ReadyBackend? backend;
  final LiveCheckBackend? liveBackend;
  const DiagnosticsPanel({
    super.key,
    required this.client,
    required this.connection,
    required this.lastError,
    this.tokenCtrl,
    this.backend,
    this.liveBackend,
  });

  @override
  State<DiagnosticsPanel> createState() => _DiagnosticsPanelState();
}

class _DiagnosticsPanelState extends State<DiagnosticsPanel> {
  ReadyInfo? _ready;
  String _error = '';
  bool _busy = false;

  LiveCheckState _live = LiveCheckState.notChecked;
  LiveCheckResult? _liveResult;
  String _liveError = '';
  String _liveNotice = '';
  bool _liveBusy = false;

  ReadyBackend get _backend => widget.backend ?? CoreReadyBackend(widget.client);
  LiveCheckBackend get _liveBackend =>
      widget.liveBackend ?? CoreLiveCheckBackend(widget.client, widget.tokenCtrl);

  Future<void> _refresh() async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = '';
    });
    try {
      final info = await _backend.fetch();
      if (!mounted) return;
      setState(() => _ready = info);
    } catch (e) {
      if (mounted) setState(() => _error = describeReadyError(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// Explicit Check tap. Never auto-run; never retried automatically.
  /// A second tap while a check is in flight is dropped with a notice
  /// (the button stays enabled so the drop is explicit, never silent).
  Future<void> _liveCheck() async {
    if (_liveBusy) {
      setState(() =>
          _liveNotice = 'check already in progress — second tap dropped');
      return;
    }
    setState(() {
      _liveBusy = true;
      _live = LiveCheckState.checking;
      _liveResult = null;
      _liveError = '';
      _liveNotice = '';
    });
    try {
      final result = await _liveBackend.check();
      if (!mounted) return;
      setState(() {
        _live = LiveCheckState.success;
        _liveResult = result;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _live = classifyLiveCheckError(e);
        _liveError = describeLiveCheckError(e);
      });
    } finally {
      if (mounted) setState(() => _liveBusy = false);
    }
  }

  /// Explicit Cancel tap while checking. Posts the cancel endpoint;
  /// the in-flight check still settles on its own (success or error)
  /// — cancel never fabricates an outcome.
  Future<void> _liveCancel() async {
    try {
      final cancelled = await _liveBackend.cancel();
      if (!mounted) return;
      setState(() => _liveNotice = cancelled
          ? 'cancellation requested'
          : 'nothing to cancel (check already finished)');
    } catch (e) {
      if (mounted) {
        setState(() => _liveNotice = 'cancel failed: ${describeLiveCheckError(e)}');
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final r = _ready;
    final live = _live;
    final liveResult = _liveResult;
    final liveTerminal = live != LiveCheckState.notChecked &&
        live != LiveCheckState.checking;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Core connection: ${widget.connection}'),
        if (r == null)
          const Text('Readiness: unknown — refresh to check'),
        if (r != null) ...[
          Text('Ready: ${r.ready}'),
          Text('Provider: ${redactSecretsForDisplay(r.provider)}'),
          Text('Checks: registry=${r.registry} '
              'memory_write=${r.memoryWrite} event_bus=${r.eventBus}'),
          Text('Governor: v2=${r.governorV2} shadow=${r.governorShadow}'),
        ],
        if (widget.lastError.isNotEmpty)
          Text('Last request error: ${redactSecretsForDisplay(widget.lastError)}'),
        if (_error.isNotEmpty)
          Text('Readiness: $_error',
              style: const TextStyle(color: Colors.red)),
        const SizedBox(height: 8),
        OutlinedButton(
          onPressed: _busy ? null : _refresh,
          child: const Text('Check readiness'),
        ),
        const SizedBox(height: 8),
        const Text('Live provider check'),
        if (live == LiveCheckState.notChecked)
          const Text('not checked — tap Check to run once (never automatic)'),
        if (live == LiveCheckState.checking)
          const Text('checking live provider…'),
        if (live == LiveCheckState.success && liveResult != null)
          Text('Live provider: ${redactSecretsForDisplay(liveResult.provider)}'
              ' · model: ${redactSecretsForDisplay(liveResult.model)}'
              ' · elapsed: ${liveResult.elapsedS == null ? '?' : '${liveResult.elapsedS}s'}'),
        if (liveTerminal && live != LiveCheckState.success && _liveError.isNotEmpty)
          Text('Live check: $_liveError',
              style: const TextStyle(color: Colors.red)),
        if (_liveNotice.isNotEmpty) Text(_liveNotice),
        const SizedBox(height: 4),
        Wrap(
          spacing: 8,
          children: [
            OutlinedButton(
              onPressed: _liveCheck,
              // Retry appears only after a terminal state; while
              // checking the button still says Check (a second tap is
              // dropped with an explicit notice).
              child: Text(liveTerminal
                  ? 'Retry check'
                  : 'Check live provider'),
            ),
            if (live == LiveCheckState.checking)
              OutlinedButton(
                onPressed: _liveCancel,
                child: const Text('Cancel check'),
              ),
          ],
        ),
      ],
    );
  }
}
