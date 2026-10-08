import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

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

  CoreClient(this.baseUrl, {this.timeout = const Duration(seconds: 15)});

  Map<String, String> get _deviceHeaders {
    final h = <String, String>{'Content-Type': 'application/json'};
    final id = deviceId, key = deviceKey;
    if (id != null) h['X-Device-Id'] = id;
    if (key != null) h['X-Device-Key'] = key;
    return h;
  }

  Never _raise(String path, int status, String text) {
    final msg = 'POST $path -> $status: ${text.take(300)}';
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
      CancelScope? cancel}) async {
    return _post('/v1/agent/heartbeat', {
      'battery_pct': batteryPct,
      'charging': charging,
      'network': network,
      'online': true,
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
      {Map<String, String> query = const {}, CancelScope? cancel}) async {
    if (cancel?.isCancelled ?? false) {
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
}

extension _Take on String {
  String take(int n) => length <= n ? this : substring(0, n);
}
