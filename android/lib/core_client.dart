import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'stream_client.dart';

/// Zara Core REST client (dart:io only, no third-party deps).
/// Device key is held in memory; long-term storage is SecureStore's job.
/// Every call has a bounded timeout; 401/403/404 surface as typed errors
/// so the lifecycle layer can react (revoked/reregister) instead of
/// retry-looping. Cancellation is cooperative via [CancelScope].
class CoreException implements Exception {
  final int status;
  final String body;
  const CoreException(this.status, this.body);
  @override
  String toString() => 'CoreException($status): $body';
}

/// 401/403: identity rejected (revoked/rotated/expired) -> safe state.
class AuthException extends CoreException {
  const AuthException(super.status, super.body);
}

/// 404 on heartbeat/poll: backend restarted or never registered -> re-register.
class NotRegisteredException extends CoreException {
  const NotRegisteredException(super.status, super.body);
}

class CancelScope {
  bool _cancelled = false;
  void cancel() => _cancelled = true;
  bool get isCancelled => _cancelled;
}

class CoreClient {
  final String baseUrl;
  final Duration timeout;
  String? deviceId;
  String? deviceKey;
  String? devToken; // local development only
  String? operatorToken; // in-memory operator Bearer token for /v1/executions

  CoreClient(this.baseUrl, {this.timeout = const Duration(seconds: 15)});

  Map<String, String> get _deviceHeaders {
    final h = <String, String>{'Content-Type': 'application/json'};
    final id = deviceId, key = deviceKey;
    if (id != null) h['X-Device-Id'] = id;
    if (key != null) h['X-Device-Key'] = key;
    return h;
  }

  /// Headers for the operator-authed execution contracts
  /// (POST /v1/executions, GET /v1/executions/{id},
  /// POST /v1/executions/{id}/cancel, POST /v1/executions/resolve).
  /// Device headers ride along as provenance; the Bearer token is the
  /// operator credential (explicitly unset => Core 401, surfaced verbatim).
  /// Public (not private) so unit tests can assert the exact wire shape.
  Map<String, String> executionHeaders() {
    final h = Map<String, String>.from(_deviceHeaders);
    final bearer = operatorToken ?? devToken;
    if (bearer != null && bearer.isNotEmpty) {
      h['Authorization'] = 'Bearer $bearer';
    }
    return h;
  }

  Never _raise(String path, int status, String text,
      [String method = 'POST']) {
    final msg = '$method $path -> $status: ${text.take(300)}';
    if (status == 401 || status == 403) throw AuthException(status, msg);
    if (status == 404) throw NotRegisteredException(status, msg);
    throw CoreException(status, msg);
  }

  Future<Map<String, dynamic>> _post(
      String path, Map<String, dynamic> body,
      {bool deviceAuth = true, CancelScope? cancel}) async {
    if (cancel?.isCancelled ?? false) {
      throw const CoreException(-1, 'cancelled before send');
    }
    final client = HttpClient();
    try {
      final req = await client.postUrl(Uri.parse('$baseUrl$path'));
      final headers = deviceAuth
          ? _deviceHeaders
          : {
              'Content-Type': 'application/json',
              if (devToken != null) 'Authorization': 'Bearer $devToken',
            };
      headers.forEach(req.headers.set);
      req.write(jsonEncode(body));
      final resp = await req.close().timeout(timeout);
      final text = await resp.transform(utf8.decoder).join();
      if (cancel?.isCancelled ?? false) {
        throw const CoreException(-1, 'cancelled while waiting');
      }
      if (resp.statusCode >= 400) _raise(path, resp.statusCode, text);
      return jsonDecode(text) as Map<String, dynamic>;
    } finally {
      client.close();
    }
  }

  /// Operator-visible step is done out-of-band; the app claims with the code.
  Future<void> claim(String pairingCode, String id) async {
    final out = await _post('/v1/agent/claim', {'pairing_code': pairingCode},
        deviceAuth: false);
    deviceId = out['device_id'] as String? ?? id;
    deviceKey = out['device_key'] as String?;
  }

  Future<Map<String, dynamic>> register(
      List<String> capabilities, String softwareVersion) async {
    return _post('/v1/agent/register', {
      'capabilities': capabilities,
      'software_version': softwareVersion,
      'kind': 'android',
    });
  }

  Future<Map<String, dynamic>> heartbeat(
      {double? batteryPct,
      required bool charging,
      required String network,
      bool? lowPowerMode,
      double? memPct,
      int? storageFreeMb,
      int? storageTotalMb,
      double? batteryTempC,
      String? thermalState,
      CancelScope? cancel}) async {
    return _post('/v1/agent/heartbeat', {
      'battery_pct': batteryPct,
      'charging': charging,
      'network': network,
      'online': true,
      // cpu_pct deliberately omitted: no reliable public API (see
      // deviceTelem docs). Core must treat it as unknown, never zero.
      'low_power_mode': lowPowerMode,
      'mem_pct': memPct,
      'storage_free_mb': storageFreeMb,
      'storage_total_mb': storageTotalMb,
      'battery_temp_c': batteryTempC,
      'thermal_state': thermalState,
    }, cancel: cancel);
  }

  Future<Map<String, dynamic>> rotateKey({CancelScope? cancel}) async {
    final out =
        await _post('/v1/agent/rotate', {}, cancel: cancel);
    deviceKey = out['device_key'] as String?;
    return out;
  }

  Future<Map<String, dynamic>> updateCapabilities(
      List<String> capabilities) async {
    return _post('/v1/agent/capabilities', {
      'capabilities': capabilities,
      'removed': <String>[],
    });
  }

  /// Stage 16: advertise versioned capability descriptors (built by the
  /// app from actually-implemented capabilities only). Identity comes
  /// from device headers; Core validates strictly and diffs.
  Future<Map<String, dynamic>> describeCapabilities(
      List<Map<String, dynamic>> records, {CancelScope? cancel}) async {
    return _post('/v1/agent/capabilities/describe', {'records': records},
        cancel: cancel);
  }

  Future<Map<String, dynamic>?> pollJobs({CancelScope? cancel}) async {
    final out = await _post('/v1/agent/jobs/poll', {}, cancel: cancel);
    return out['job'] as Map<String, dynamic>?;
  }

  Future<void> reportJobResult(String jobId,
      {required bool ok,
      Map<String, dynamic> result = const {},
      String error = ''}) async {
    await _post('/v1/agent/jobs/result', {
      'job_id': jobId,
      'ok': ok,
      'result': result,
      'error': error.take(300),
    });
  }

  Future<List<Map<String, dynamic>>> fetchNotifications() async {
    final out = await _post('/v1/agent/notifications', {});
    final list = out['notifications'];
    if (list is! List) return [];
    return list.whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
  }

  Future<void> ackNotification(String id) async {
    await _post('/v1/agent/notifications/ack', {'notification_id': id});
  }

  /// Notification approve/deny buttons land here: device-scoped,
  /// server-audited. Never local execution.
  Future<Map<String, dynamic>> decideApproval(
      String executionId, bool approve) async {
    return _post('/v1/agent/approvals/$executionId', {
      'decision': approve ? 'approve' : 'deny',
    });
  }

  Future<void> registerPush(String token, {String provider = 'mock'}) async {
    await _post('/v1/agent/push/register',
        {'token': token, 'provider': provider});
  }

  Future<void> invalidatePush() async {
    await _post('/v1/agent/push/invalidate', {});
  }

  Future<void> syncEvents(List<Map<String, dynamic>> events) async {
    await _post('/v1/agent/events/sync', {'events': events.take(200).toList()});
  }

  Future<void> disconnect() async {
    try {
      await _post('/v1/agent/disconnect', {});
    } catch (_) {/* best-effort goodbye */}
  }

  /// Full voice turn: bounded WAV up, transcript+reply back (no audio down;
  /// use [tts] to voice the reply through the configured provider).
  Future<Map<String, dynamic>> voiceTurn(Uint8List wav,
      {String sessionId = '', CancelScope? cancel}) async {
    return _post('/v1/agent/voice/turn', {
      'audio_base64': base64Encode(wav),
      'session_id': sessionId,
      'device_id': deviceId ?? 'android-phone',
      'who': 'user',
    }, cancel: cancel);
  }

  /// Provider TTS bytes for [text] (Piper default). Unprivileged synthesis.
  Future<Uint8List> tts(String text, {CancelScope? cancel}) async {
    final out = await _post(
        '/v1/agent/tts', {'text': text.take(2000)}, cancel: cancel);
    final b64 = out['audio_base64'];
    if (b64 is! String || b64.isEmpty) {
      throw const CoreException(-1, 'empty tts audio');
    }
    return base64Decode(b64);
  }

  Future<void> interruptVoice() async {
    try {
      await _post('/v1/agent/voice/interrupt', {});
    } catch (_) {/* best-effort */}
  }

  // ---- Memory review + readiness (operator-authed memory, public ready) --
  // Wired to the REAL Core contracts (core/app.py, read-only):
  //   GET    /v1/memory/search?q=&top_k= -> [{text,category,score,...}]
  //   DELETE /v1/memory/{id}            -> {forgotten} (404 unknown memory)
  //   POST   /v1/memory/candidates/confirm {candidate_id,ok} -> verdict
  //   POST   /v1/memory/purge           -> {purged}
  //   GET    /v1/ready                  -> {ready,checks{...provider},
  //                                         governor_v2,governor_shadow}
  // Memory endpoints use operator auth (executionHeaders: Bearer operator
  // token + X-Device provenance), same as the execution contracts. A missing
  // token surfaces as Core 401, verbatim. 404 on forget = unknown memory
  // id (removed elsewhere), never a registration problem — callers must
  // branch with [isMemoryNotFound], not the lifecycle re-register path.
  // /v1/ready is public (no auth headers sent at all, never secrets).
  // NOTE: search results carry whatever keys Core returns; current Core
  // omits `id` per row, so per-row forget needs an id from elsewhere
  // (forward-compatible: parsed when present, empty otherwise).

  /// Operator-authed JSON request returning decoded JSON (Map or List).
  /// 401/403 AuthException, 404 NotRegisteredException, else CoreException
  /// (message always embeds the status code for explicit UI branching).
  Future<dynamic> _memoryRequest(String method, String path,
      {Map<String, String> query = const {},
      Map<String, dynamic>? body,
      CancelScope? cancel}) async {
    if (cancel?.isCancelled ?? false) {
      throw const CoreException(-1, 'cancelled before send');
    }
    final client = HttpClient();
    try {
      final uri = Uri.parse('$baseUrl$path')
          .replace(queryParameters: query.isEmpty ? null : query);
      final req = await client.openUrl(method, uri);
      executionHeaders().forEach(req.headers.set);
      if (body != null) req.write(jsonEncode(body));
      final resp = await req.close().timeout(timeout);
      final text = await resp.transform(utf8.decoder).join();
      if (cancel?.isCancelled ?? false) {
        throw const CoreException(-1, 'cancelled while waiting');
      }
      if (resp.statusCode >= 400) _raise(path, resp.statusCode, text, method);
      if (text.trim().isEmpty) return <String, dynamic>{};
      return jsonDecode(text);
    } finally {
      client.close();
    }
  }

  /// GET /v1/memory/search?q=&top_k=. Returns raw rows
  /// ({text,category,score,...}); empty list when Core returns non-list.
  Future<List<Map<String, dynamic>>> memorySearch(String q,
      {int topK = 5, CancelScope? cancel}) async {
    final out = await _memoryRequest('GET', '/v1/memory/search',
        query: memorySearchQuery(q, topK: topK), cancel: cancel);
    if (out is! List) return [];
    return out
        .whereType<Map>()
        .map((e) => Map<String, dynamic>.from(e))
        .toList();
  }

  /// DELETE /v1/memory/{id}. 404 = unknown id (removed elsewhere).
  Future<Map<String, dynamic>> memoryForget(String id,
      {CancelScope? cancel}) async {
    final out = await _memoryRequest(
        'DELETE', memoryForgetPath(id),
        cancel: cancel);
    if (out is Map) return Map<String, dynamic>.from(out);
    return {'forgotten': id};
  }

  /// POST /v1/memory/candidates/confirm {candidate_id,ok}.
  Future<Map<String, dynamic>> memoryConfirmCandidate(String candidateId,
      {bool ok = true, CancelScope? cancel}) async {
    final out = await _memoryRequest(
        'POST', '/v1/memory/candidates/confirm',
        body: buildMemoryConfirmBody(candidateId, ok: ok), cancel: cancel);
    if (out is Map) return Map<String, dynamic>.from(out);
    return {'stored': false};
  }

  /// POST /v1/memory/purge -> {purged}.
  Future<Map<String, dynamic>> memoryPurge({CancelScope? cancel}) async {
    final out = await _memoryRequest('POST', '/v1/memory/purge',
        body: {}, cancel: cancel);
    if (out is Map) return Map<String, dynamic>.from(out);
    return {'purged': 0};
  }

  /// GET /v1/memory/candidates (operator-authed). Returns raw rows
  /// ({candidate_id,text(redacted server-side),category,source});
  /// empty list when Core returns a non-list envelope.
  Future<List<Map<String, dynamic>>> memoryCandidates(
      {CancelScope? cancel}) async {
    final out = await _memoryRequest('GET', '/v1/memory/candidates',
        cancel: cancel);
    if (out is! List) return [];
    return out
        .whereType<Map>()
        .map((e) => Map<String, dynamic>.from(e))
        .toList();
  }

  /// POST /v1/diagnostics/provider-check (operator-authed, explicit
  /// user action only — never automatic, never polled). 200
  /// {ok,provider,model,elapsed_s}; 409 busy; 422 not-a-live-provider;
  /// 504 timeout; 502 provider error.
  Future<Map<String, dynamic>> providerCheck({CancelScope? cancel}) {
    return _execPost('/v1/diagnostics/provider-check', {}, cancel: cancel);
  }

  /// POST /v1/diagnostics/provider-check/cancel (operator-authed).
  /// Unknown/finished checks are a safe server-side no-op
  /// ({cancelled: false}) — still explicit, never silent.
  Future<Map<String, dynamic>> cancelProviderCheck({CancelScope? cancel}) {
    return _execPost('/v1/diagnostics/provider-check/cancel', {},
        cancel: cancel);
  }

  /// GET /v1/ready (public: no auth headers, never secrets).
  /// Returns {ready,checks{...provider},governor_v2,governor_shadow}.
  Future<Map<String, dynamic>> ready({CancelScope? cancel}) async {
    if (cancel?.isCancelled ?? false) {
      throw const CoreException(-1, 'cancelled before send');
    }
    final client = HttpClient();
    try {
      final req = await client.getUrl(Uri.parse('$baseUrl/v1/ready'));
      req.headers.set(HttpHeaders.acceptHeader, 'application/json');
      final resp = await req.close().timeout(timeout);
      final text = await resp.transform(utf8.decoder).join();
      if (resp.statusCode >= 400) {
        _raise('/v1/ready', resp.statusCode, text, 'GET');
      }
      return jsonDecode(text) as Map<String, dynamic>;
    } finally {
      client.close();
    }
  }

  // ---- Streaming talk (Stage 16): POST /v1/talk/stream (SSE) ----
  // Same operator auth as the execution contracts (executionHeaders:
  // Bearer operator token + X-Device provenance). Non-streaming /v1/talk
  // stays as the fallback path. Tools execute server-side; the stream
  // only narrates. Cancel via [cancelTalk] with the turn_id from the first
  // `turn` event (or by closing the handle: disconnect aborts the turn).

  /// Non-streaming twin: full NL loop in one JSON round-trip. Fallback when
  /// SSE is unavailable; same TalkIn envelope as the stream.
  Future<Map<String, dynamic>> talk(
      {required String text,
      String sessionId = '',
      String deviceId = '',
      String who = 'user',
      CancelScope? cancel}) {
    return _execPost(
        '/v1/talk',
        buildTalkStreamBody(
            text: text,
            sessionId: sessionId,
            deviceId: deviceId,
            who: who),
        cancel: cancel);
  }

  /// Abort an in-flight streamed turn. Unknown/finished ids are a safe
  /// server-side no-op ({cancelled: false}) — still explicit, never silent.
  Future<Map<String, dynamic>> cancelTalk(String turnId,
      {CancelScope? cancel}) {
    return _execPost('/v1/talk/cancel', buildTalkCancelBody(turnId),
        cancel: cancel);
  }

  /// Open a streaming POST with a bounded open [timeout] ([openTimeout] or
  /// the client default). HTTP error statuses throw typed errors via
  /// [_raise] (401/403 AuthException etc.) — the caller renders them
  /// explicitly. The returned handle parses frames incrementally and
  /// surfaces malformed data as stream errors (never silent).
  Future<TalkStreamHandle> startTalkStream(
      {required String text,
      String sessionId = '',
      String deviceId = '',
      String who = 'user',
      Duration? openTimeout,
      CancelScope? cancel}) async {
    final body = buildTalkStreamBody(
        text: text, sessionId: sessionId, deviceId: deviceId, who: who);
    if (cancel?.isCancelled ?? false) {
      throw const CoreException(-1, 'cancelled before send');
    }
    final client = HttpClient();
    try {
      final req = await client.postUrl(Uri.parse('$baseUrl/v1/talk/stream'));
      executionHeaders().forEach(req.headers.set);
      req.headers.set(HttpHeaders.acceptHeader, 'text/event-stream');
      req.write(jsonEncode(body));
      final resp = await req.close().timeout(openTimeout ?? timeout);
      if (resp.statusCode >= 400) {
        final text = await resp.transform(utf8.decoder).join();
        client.close();
        _raise('/v1/talk/stream', resp.statusCode, text);
      }
      return TalkStreamHandle._attach(client, resp);
    } catch (_) {
      client.close();
      rethrow;
    }
  }

  /// Stage 15 byte transfers (Core-mediated chunked relay).
  /// Identity always comes from device headers; sender/recipient binding
  /// is enforced server-side. Hashes are computed by the caller with
  /// transfer.dart (never trusted from Core alone).
  Future<Map<String, dynamic>> xferRequest(
      {required String recipient,
      required String filename,
      required int sizeBytes,
      required String sha256,
      String contentType = 'application/octet-stream',
      String grantId = '',
      CancelScope? cancel}) async {
    return _post('/v1/agent/xfer/request', {
      'recipient_device': recipient,
      'filename': filename,
      'size_bytes': sizeBytes,
      'sha256': sha256,
      'content_type': contentType,
      'grant_id': grantId,
    }, cancel: cancel);
  }

  Future<Map<String, dynamic>> xferChunk(String transferId, int seq,
      Uint8List data, {CancelScope? cancel}) async {
    return _post('/v1/agent/xfer/$transferId/chunk', {
      'seq': seq,
      'data_base64': base64Encode(data),
    }, cancel: cancel);
  }

  Future<Map<String, dynamic>> _get(String path,
      {Map<String, String> query = const {}, CancelScope? cancel}) async {    if (cancel?.isCancelled ?? false) {
      throw const CoreException(-1, 'cancelled before send');
    }
    final client = HttpClient();
    try {
      final uri = Uri.parse('$baseUrl$path')
          .replace(queryParameters: query.isEmpty ? null : query);
      final req = await client.getUrl(uri);
      _deviceHeaders.forEach(req.headers.set);
      final resp = await req.close().timeout(timeout);
      final text = await resp.transform(utf8.decoder).join();
      if (resp.statusCode >= 400) _raise(path, resp.statusCode, text);
      return jsonDecode(text) as Map<String, dynamic>;
    } finally {
      client.close();
    }
  }

  Future<Map<String, dynamic>> xferPending({CancelScope? cancel}) async {
    return _get('/v1/agent/xfer/pending', cancel: cancel);
  }

  Future<Map<String, dynamic>> xferBytes(String transferId, int offset,
      int length, {CancelScope? cancel}) async {
    return _get('/v1/agent/xfer/$transferId/bytes',
        query: {'offset': '$offset', 'length': '$length'}, cancel: cancel);
  }

  Future<Map<String, dynamic>> xferAck(String transferId, String sha256,
      {CancelScope? cancel}) async {
    return _post('/v1/agent/xfer/$transferId/ack', {'sha256': sha256},
        cancel: cancel);
  }

  Future<Map<String, dynamic>> xferCancel(String transferId,
      {CancelScope? cancel}) async {
    return _post('/v1/agent/xfer/$transferId/cancel', {}, cancel: cancel);
  }

  // ---- Task submission + execution status + cancellation ----
  // Wired to the REAL Core contracts (core/app.py, read-only):
  //   POST /v1/executions {tool,inputs,device_id} -> 201/202 execution view
  //   GET  /v1/executions/{id} -> {status,job_state,result,error,...}
  //   POST /v1/executions/{id}/cancel -> view (404 unknown, 409 terminal)
  //   POST /v1/executions/resolve {capability,...} -> 201/202 view
  // Same conventions as the device methods: bounded [timeout], cooperative
  // [CancelScope], typed errors (401/403 AuthException, 404
  // NotRegisteredException, else CoreException incl. 409/422). The
  // device-scoped approval path (decideApproval) is untouched.

  Future<Map<String, dynamic>> _execPost(String path,
      Map<String, dynamic> body, {CancelScope? cancel}) async {
    if (cancel?.isCancelled ?? false) {
      throw const CoreException(-1, 'cancelled before send');
    }
    final client = HttpClient();
    try {
      final req = await client.postUrl(Uri.parse('$baseUrl$path'));
      executionHeaders().forEach(req.headers.set);
      req.write(jsonEncode(body));
      final resp = await req.close().timeout(timeout);
      final text = await resp.transform(utf8.decoder).join();
      if (cancel?.isCancelled ?? false) {
        throw const CoreException(-1, 'cancelled while waiting');
      }
      if (resp.statusCode >= 400) _raise(path, resp.statusCode, text);
      return jsonDecode(text) as Map<String, dynamic>;
    } finally {
      client.close();
    }
  }

  Future<Map<String, dynamic>> _execGet(String path,
      {CancelScope? cancel}) async {
    if (cancel?.isCancelled ?? false) {
      throw const CoreException(-1, 'cancelled before send');
    }
    final client = HttpClient();
    try {
      final req = await client.getUrl(Uri.parse('$baseUrl$path'));
      executionHeaders().forEach(req.headers.set);
      final resp = await req.close().timeout(timeout);
      final text = await resp.transform(utf8.decoder).join();
      if (cancel?.isCancelled ?? false) {
        throw const CoreException(-1, 'cancelled while waiting');
      }
      if (resp.statusCode >= 400) _raise(path, resp.statusCode, text, 'GET');
      return jsonDecode(text) as Map<String, dynamic>;
    } finally {
      client.close();
    }
  }

  /// Async submit: same gates as /v1/exec, id returns immediately.
  /// 201 running, 202 parked-for-approval, 404 unknown tool, 403 policy
  /// deny, 422 input-envelope rejection. A deferred gate returns
  /// {"status":"deferred","reason":...} with NO id (caller must check).
  Future<Map<String, dynamic>> submitExecution(
      {required String tool,
      Map<String, dynamic> inputs = const {},
      required String deviceId,
      String? missionId,
      String who = 'user',
      String? idempotencyKey,
      CancelScope? cancel}) async {
    return _execPost(
        '/v1/executions',
        buildExecutionSubmitBody(
            tool: tool,
            inputs: inputs,
            deviceId: deviceId,
            missionId: missionId,
            who: who,
            idempotencyKey: idempotencyKey),
        cancel: cancel);
  }

  /// Poll one execution: {status,job_state,result,error,approval_required,
  /// cancel_requested,...}. 404 = unknown execution (never a record).
  Future<Map<String, dynamic>> executionStatus(String id,
      {CancelScope? cancel}) async {
    return _execGet('/v1/executions/${Uri.encodeComponent(id)}',
        cancel: cancel);
  }

  /// Cancel one execution. 404 unknown id, 409 already terminal
  /// (succeeded/failed/timed_out/denied/cancelled) — both explicit, never
  /// silent. Terminal records keep their history; the row stays visible.
  Future<Map<String, dynamic>> cancelExecution(String id,
      {CancelScope? cancel}) async {
    return _execPost(
        '/v1/executions/${Uri.encodeComponent(id)}/cancel', {},
        cancel: cancel);
  }

  // ---- Conversation history (read-only browse + delete) ----
  // Wired to the REAL Core contracts (core/app.py, read-only):
  //   GET    /v1/sessions?limit=N -> [{id,updated_at,message_count,
  //                                    last_text}] (newest first, redacted)
  //   GET    /v1/sessions/{id}     -> full session incl.
  //                                    messages[{role,text,tool,created_at}]
  //   DELETE /v1/sessions/{id}     -> {deleted} (404 unknown session)
  // Same operator auth as the execution/memory contracts
  // (executionHeaders: Bearer operator token + X-Device provenance).
  // A missing token surfaces as Core 401, verbatim. Opening history
  // never submits prompts: list/get are GET-only, never POST /v1/talk.

  /// GET /v1/sessions?limit=N. Returns raw rows; empty list when Core
  /// returns a non-list envelope.
  Future<List<Map<String, dynamic>>> sessionList(
      {int limit = 20, CancelScope? cancel}) async {
    final out = await _memoryRequest('GET', '/v1/sessions',
        query: sessionListQuery(limit: limit), cancel: cancel);
    if (out is! List) return [];
    return out
        .whereType<Map>()
        .map((e) => Map<String, dynamic>.from(e))
        .toList();
  }

  /// GET /v1/sessions/{id}. 404 = unknown session (removed elsewhere).
  Future<Map<String, dynamic>> sessionGet(String id,
      {CancelScope? cancel}) async {
    final out = await _memoryRequest('GET', sessionDetailPath(id),
        cancel: cancel);
    if (out is Map) return Map<String, dynamic>.from(out);
    return {'id': id, 'messages': []};
  }

  /// DELETE /v1/sessions/{id}. 404 = unknown session (removed elsewhere).
  Future<Map<String, dynamic>> sessionDelete(String id,
      {CancelScope? cancel}) async {
    final out = await _memoryRequest('DELETE', sessionDetailPath(id),
        cancel: cancel);
    if (out is Map) return Map<String, dynamic>.from(out);
    return {'deleted': id};
  }

  /// Canonical external path: resolve a capability deterministically, then
  /// enter the async lifecycle. 403/409 create NO execution (reason in the
  /// body); 422 input-envelope rejection likewise.
  Future<Map<String, dynamic>> resolveExecution(
      {required String capability,
      Map<String, dynamic> inputs = const {},
      String deviceId = '',
      Map<String, dynamic>? constraints,
      String preferredDevice = '',
      bool allowFallback = true,
      String who = 'user',
      String? missionId,
      String? idempotencyKey,
      CancelScope? cancel}) async {
    return _execPost(
        '/v1/executions/resolve',
        buildExecutionResolveBody(
            capability: capability,
            inputs: inputs,
            deviceId: deviceId,
            constraints: constraints,
            preferredDevice: preferredDevice,
            allowFallback: allowFallback,
            who: who,
            missionId: missionId,
            idempotencyKey: idempotencyKey),
        cancel: cancel);
  }
}

/// Open SSE talk stream: incremental (name, payload) pairs parsed with
/// [SseParser] (chunk-split lines + split multibyte safe, strict, `: ping`
/// tolerant). Single-subscription [events]; malformed frames and transport
/// failures surface as stream errors, never silent.
///
/// Lifecycle: [close] ends local consumption (server treats the disconnect
/// as a best-effort abort). [cancel] closes locally AND posts
/// POST /v1/talk/cancel with the observed turn_id (null when no `turn`
/// event arrived yet — nothing to cancel server-side).
class TalkStreamHandle {
  final HttpClient? _client; // null for [TalkStreamHandle.fake] test handles
  final SseParser _parser = SseParser();
  final StreamController<TalkStreamEvent> _events =
      StreamController<TalkStreamEvent>();
  StreamSubscription<dynamic>? _sub;
  String? turnId;
  bool _closed = false;

  TalkStreamHandle._attach(this._client, HttpClientResponse resp) {
    _sub = resp.listen(
      (chunk) {
        if (_closed) return;
        try {
          for (final e in _parser.addBytes(chunk)) {
            _noteTurn(e);
            if (!_events.isClosed) _events.add(e);
          }
        } catch (e) {
          if (!_events.isClosed) _events.addError(e);
          unawaited(close());
        }
      },
      onError: (Object e) {
        if (!_events.isClosed) _events.addError(e);
        unawaited(close());
      },
      onDone: () {
        if (_closed) return;
        try {
          for (final e in _parser.flush()) {
            _noteTurn(e);
            if (!_events.isClosed) _events.add(e);
          }
        } catch (e) {
          if (!_events.isClosed) _events.addError(e);
        }
        unawaited(close());
      },
      cancelOnError: false,
    );
  }

  TalkStreamHandle._detached() : _client = null;

  Stream<TalkStreamEvent>? _forwarded;

  /// Test seam: a handle fed by [source] instead of a socket (no HTTP, so
  /// widget tests stay hermetic — the flutter_tester cannot hold loopback
  /// servers open). [events] is the source stream itself with turn
  /// sniffing (no intermediate controller), so termination propagates on
  /// pumps without awaiting teardown futures; [close] marks closed and
  /// drops references without awaiting anything. [cancel] still posts
  /// through the given client when a turn_id was observed.
  factory TalkStreamHandle.fake(Stream<TalkStreamEvent> source) {
    final handle = TalkStreamHandle._detached();
    handle._forwarded = source.map((event) {
      handle._noteTurn(event);
      return event;
    });
    return handle;
  }

  void _noteTurn(TalkStreamEvent e) {
    if (e.name != 'turn') return;
    final t = e.payload['turn_id'];
    if (t is String && t.isNotEmpty) turnId ??= t;
  }

  Stream<TalkStreamEvent> get events => _forwarded ?? _events.stream;

  Future<void> close() async {
    if (_closed) return;
    _closed = true;
    if (_forwarded != null) return; // test handle holds no resources
    try {
      await _sub?.cancel();
    } catch (_) {}
    _sub = null;
    if (!_events.isClosed) await _events.close();
    try {
      _client?.close();
    } catch (_) {}
  }

  Future<Map<String, dynamic>?> cancel(CoreClient client) async {
    final tid = turnId;
    await close();
    if (tid == null || tid.isEmpty) return null;
    return client.cancelTalk(tid);
  }
}

extension _Take on String {
  String take(int n) => length <= n ? this : substring(0, n);
}

/// Exact wire body for POST /v1/executions
/// (core/app.py ExecutionSubmitIn: tool/inputs/device_id/mission_id/who).
/// Pure for testing. Throws ArgumentError on empty tool/deviceId — those
/// would be a Core 404/422, so fail fast instead of sending known-bad JSON.
Map<String, dynamic> buildExecutionSubmitBody(
    {required String tool,
    Map<String, dynamic> inputs = const {},
    required String deviceId,
    String? missionId,
    String who = 'user',
    String? idempotencyKey}) {
  if (tool.isEmpty) throw ArgumentError('tool must not be empty');
  if (deviceId.isEmpty) throw ArgumentError('deviceId must not be empty');
  return {
    'tool': tool,
    'inputs': Map<String, dynamic>.from(inputs),
    'device_id': deviceId,
    if (missionId != null && missionId.isNotEmpty) 'mission_id': missionId,
    'who': who,
    if (idempotencyKey != null && idempotencyKey.isNotEmpty)
      'idempotency_key': idempotencyKey,
  };
}

/// Exact wire body for POST /v1/executions/resolve
/// (core/app.py ExecutionResolveIn). Pure for testing.
Map<String, dynamic> buildExecutionResolveBody(
    {required String capability,
    Map<String, dynamic> inputs = const {},
    String deviceId = '',
    Map<String, dynamic>? constraints,
    String preferredDevice = '',
    bool allowFallback = true,
    String who = 'user',
    String? missionId,
    String? idempotencyKey}) {
  if (capability.isEmpty) throw ArgumentError('capability must not be empty');
  final body = <String, dynamic>{
    'capability': capability,
    'inputs': Map<String, dynamic>.from(inputs),
    'device_id': deviceId,
    'preferred_device': preferredDevice,
    'allow_fallback': allowFallback,
    'who': who,
    if (missionId != null && missionId.isNotEmpty) 'mission_id': missionId,
    if (idempotencyKey != null && idempotencyKey.isNotEmpty)
      'idempotency_key': idempotencyKey,
  };
  final c = constraints;
  if (c != null) body['constraints'] = c;
  return body;
}

/// Terminal execution states (core/models.py ExecutionState + the cancel
/// endpoint's terminal set). Pure for testing; the task row's Cancel button
/// is hidden exactly when this is true.
bool isExecutionTerminal(String status) => const {
      'succeeded',
      'failed',
      'timed_out',
      'denied',
      'cancelled',
    }.contains(status);

/// Read-time view of GET /v1/executions/{id} (core/app.py _execution_view).
/// Result content is untrusted device data, never authority.
class ExecutionView {
  final String id;
  final String tool;
  final String deviceId;
  final String status;
  final String? jobId;
  final String? jobState;
  final Map<String, dynamic>? result;
  final String error;
  final bool approvalRequired;
  final bool cancelRequested;
  final bool verified;

  const ExecutionView({
    required this.id,
    required this.tool,
    required this.deviceId,
    required this.status,
    this.jobId,
    this.jobState,
    this.result,
    this.error = '',
    this.approvalRequired = false,
    this.cancelRequested = false,
    this.verified = false,
  });

  /// Throws FormatException when the envelope has no id/status (e.g. a
  /// deferred-gate reply {"status":"deferred","reason":...}, which is NOT
  /// an execution — callers must branch on the raw map first).
  static ExecutionView fromJson(Map<String, dynamic> m) {
    final id = m['id'];
    final status = m['status'];
    if (id is! String || id.isEmpty) {
      throw FormatException('execution view missing id');
    }
    if (status is! String || status.isEmpty) {
      throw FormatException('execution view missing status');
    }
    final rawResult = m['result'];
    return ExecutionView(
      id: id,
      status: status,
      tool: '${m['tool'] ?? ''}',
      deviceId: '${m['device_id'] ?? ''}',
      jobId: m['job_id'] is String ? m['job_id'] as String : null,
      jobState: m['job_state'] is String ? m['job_state'] as String : null,
      result: rawResult is Map ? Map<String, dynamic>.from(rawResult) : null,
      error: '${m['error'] ?? ''}',
      approvalRequired: m['approval_required'] == true,
      cancelRequested: m['cancel_requested'] == true,
      verified: m['verified'] == true,
    );
  }

  bool get isTerminal => isExecutionTerminal(status);
  bool get isRunning => status == 'running' || status == 'pending';
  bool get needsApproval =>
      approvalRequired || status == 'waiting_for_permission';
}

/// 404 on submit/status/cancel = unknown tool or unknown execution id
/// (expired or never created). Never auto-retried, always shown.
bool isExecutionNotFound(Object e) => e.toString().contains('404');
/// 409 on cancel = already terminal elsewhere (or already decided for
/// approvals): stop acting on the row, keep it visible. Never silent.
bool isExecutionTerminalConflict(Object e) => e.toString().contains('409');

/// 422 on submit/resolve = input-envelope rejected before any record
/// existed. Fix the inputs, not the polling.
bool isExecutionRejected(Object e) => e.toString().contains('422');

/// Exact query for GET /v1/memory/search (core/app.py search_memory:
/// q + top_k). Pure for testing.
Map<String, String> memorySearchQuery(String q, {int topK = 5}) =>
    {'q': q, 'top_k': '$topK'};

/// Exact path for DELETE /v1/memory/{id} (core/app.py memory_forget).
/// Pure for testing. Throws ArgumentError on empty id — that would be a
/// Core 404, so fail fast instead of sending known-bad requests.
String memoryForgetPath(String id) {
  if (id.isEmpty) throw ArgumentError('memory id must not be empty');
  return '/v1/memory/${Uri.encodeComponent(id)}';
}

/// Exact wire body for POST /v1/memory/candidates/confirm
/// (core/app.py MemoryConfirmIn: candidate_id/ok). Pure for testing.
Map<String, dynamic> buildMemoryConfirmBody(String candidateId,
    {bool ok = true}) {
  if (candidateId.isEmpty) {
    throw ArgumentError('candidate_id must not be empty');
  }
  return {'candidate_id': candidateId, 'ok': ok};
}

/// 404 on DELETE /v1/memory/{id} = unknown memory id (expired or removed
/// elsewhere). Never a registration problem: do NOT re-register, show the
/// removed-elsewhere state. Pure for testing.
bool isMemoryNotFound(Object e) => e.toString().contains('404');

/// 422 on POST /v1/memory = secret/credential refusal (never stored).
/// Fix the text, not the polling. Pure for testing.
bool isMemorySecretRejected(Object e) => e.toString().contains('422');

/// Exact query for GET /v1/sessions (core/app.py list_sessions: limit,
/// clamped server-side to 1..100). Pure for testing.
Map<String, String> sessionListQuery({int limit = 20}) =>
    {'limit': '$limit'};

/// Exact path for GET/DELETE /v1/sessions/{id} (core/app.py get_session /
/// delete_session). Pure for testing. Throws ArgumentError on empty id —
/// that would be a Core 404, so fail fast instead of sending known-bad
/// requests.
String sessionDetailPath(String id) {
  if (id.isEmpty) throw ArgumentError('session id must not be empty');
  return '/v1/sessions/${Uri.encodeComponent(id)}';
}

/// 404 on GET/DELETE /v1/sessions/{id} = unknown session (expired,
/// deleted, or never created — removed elsewhere). Never a registration
/// problem: do NOT re-register, show the removed-elsewhere state.
/// Pure for testing.
bool isSessionNotFound(Object e) => e.toString().contains('404');

// ---- Live provider check + pending candidates (operator-authed) ----
// Wired to the REAL Core contracts (core/app.py, read-only):
//   POST /v1/diagnostics/provider-check -> 200 {ok,provider,model,
//     elapsed_s} | 409 busy | 422 not-a-live-provider | 504 timeout |
//     502 provider error (detail "provider <kind>: ...")
//   POST /v1/diagnostics/provider-check/cancel -> {cancelled}
//   GET  /v1/memory/candidates -> [{candidate_id,text(redacted
//     server-side),category,source}]
// Same operator auth as the execution/memory contracts
// (executionHeaders: Bearer operator token + X-Device provenance).
// A missing token surfaces as Core 401, verbatim.
