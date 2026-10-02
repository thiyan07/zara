import 'dart:async';
import 'dart:convert';
import 'dart:io';

/// Minimal Zara Core REST client (dart:io only, no third-party deps).
/// Device key is kept in memory in Stage 2; secure storage lands in Stage 3.
class CoreClient {
  final String baseUrl;
  String? deviceId;
  String? deviceKey;
  String? devToken; // local development only

  CoreClient(this.baseUrl);

  Map<String, String> get _deviceHeaders {
    final h = <String, String>{'Content-Type': 'application/json'};
    final id = deviceId, key = deviceKey;
    if (id != null) h['X-Device-Id'] = id;
    if (key != null) h['X-Device-Key'] = key;
    return h;
  }

  Future<Map<String, dynamic>> _post(
      String path, Map<String, dynamic> body,
      {bool deviceAuth = true}) async {
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
      final resp = await req.close().timeout(const Duration(seconds: 15));
      final text = await resp.transform(utf8.decoder).join();
      if (resp.statusCode >= 400) {
        throw HttpException('POST $path -> ${resp.statusCode}: $text');
      }
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
      {double? batteryPct, required bool charging, required String network}) async {
    return _post('/v1/agent/heartbeat', {
      'battery_pct': batteryPct,
      'charging': charging,
      'network': network,
      'online': true,
    });
  }
}
