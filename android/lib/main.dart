import 'dart:async';
import 'package:flutter/material.dart';
import 'capabilities.dart';
import 'core_client.dart';
import 'device_bridge.dart';
import 'secure_store.dart';
import 'voice.dart' show wakePhrase;

void main() => runApp(const ZaraApp());

/// Zara Android shell (Stage 2 foundation): connect, claim pairing code,
/// register capabilities, report battery/network, heartbeat. No voice yet.
class ZaraApp extends StatefulWidget {
  const ZaraApp({super.key});
  @override
  State<ZaraApp> createState() => _ZaraAppState();
}

class _ZaraAppState extends State<ZaraApp> with WidgetsBindingObserver {
  final bridge = DeviceBridge();
  final client = CoreClient('http://10.0.2.2:8080');
  final secureStore = SecureStore();
  final codeCtrl = TextEditingController();
  String status = 'disconnected';
  Map<String, dynamic> battery = {};
  Map<String, dynamic> network = {};
  Map<String, dynamic> voiceFlags = {};
  Timer? _hb;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _refresh().then((_) => _restore());
  }

  /// Restore a previous pairing from secure storage (Keystore survives
  /// force-stop; only uninstall/clear wipes it). Re-registers and resumes
  /// heartbeats without asking for a new pairing code.
  Future<void> _restore() async {
    final creds = await secureStore.loadDeviceCredentials();
    if (creds.deviceId == null || creds.deviceKey == null) return;
    client.deviceId = creds.deviceId;
    client.deviceKey = creds.deviceKey;
    setState(() => status = 'restoring…');
    try {
      await client.register(advertisedCapabilities(), 'zara-android 2.0.0');
      await _beat();
      setState(() => status = 'online as ${client.deviceId}');
      _hb?.cancel();
      _hb = Timer.periodic(const Duration(seconds: 30), (_) => _beat());
    } catch (e) {
      // Revoked/expired server-side (or backend restarted): stay
      // disconnected and require a fresh pairing code. Never retry-loop.
      setState(() => status = 'session expired — pair again');
    }
  }

  Future<void> _refresh() async {
    final b = await bridge.battery();
    final n = await bridge.network();
    // Pull the native audio/voice capability flags so the UI and any
    // reporter always reflects the real bridge, never a stale table.
    Map<String, dynamic> v = {};
    try {
      v = await bridge.voiceSupport();
    } catch (_) {/* bridge unavailable: keep static table */}
    if (mounted) {
      setState(() { battery = b; network = n; voiceFlags = v; });
      // ignore: avoid_print
      print('zara:voice-support=$v');
    }
  }

  Future<void> _connect() async {
    setState(() => status = 'claiming…');
    try {
      await client.claim(codeCtrl.text.trim(), 'android-phone');
      // Device key goes to Keystore-backed storage; never logs, never prefs.
      await secureStore.saveDeviceCredentials(
        deviceId: client.deviceId ?? 'android-phone',
        deviceKey: client.deviceKey ?? '',
      );
      await client.register(advertisedCapabilities(), 'zara-android 2.0.0');
      await _beat();
      setState(() => status = 'online as ${client.deviceId}');
      _hb?.cancel();
      _hb = Timer.periodic(const Duration(seconds: 30), (_) => _beat());
    } catch (e) {
      setState(() => status = 'error: $e');
    }
  }

  Future<void> _beat() async {
    // Re-read platform state every tick: values must never go stale.
    // Two local channel calls per 30 s is negligible next to any radio use.
    try {
      final b = await bridge.battery();
      final n = await bridge.network();
      if (mounted) setState(() { battery = b; network = n; });
    } catch (_) {/* keep last known on bridge failure */}
    try {
      await client.heartbeat(
        batteryPct: (battery['battery_pct'] as num?)?.toDouble(),
        charging: battery['charging'] == true,
        network: network['network'] as String? ?? 'unknown',
      );
    } catch (_) {/* offline: keep local state, retry next tick */}
  }

  @override
  void dispose() {
    _hb?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    codeCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Zara',
      theme: ThemeData(useMaterial3: true, colorSchemeSeed: Colors.deepPurple),
      home: Scaffold(
        appBar: AppBar(title: const Text('Zara')),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Text('Status: $status', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            Text('Battery: ${battery['battery_pct'] ?? '?'}% '
                '(charging: ${battery['charging'] ?? '?'})'),
            Text('Network: ${network['network'] ?? '?'}'),
            const SizedBox(height: 12),
            TextField(controller: codeCtrl,
                decoration: const InputDecoration(labelText: 'Pairing code from Zara Core')),
            const SizedBox(height: 8),
            FilledButton(onPressed: _connect, child: const Text('Connect “Hey Zara” device')),
            const SizedBox(height: 12),
            Text('Voice: wake phrase “$wakePhrase” (engine pending hardware validation)',
                style: Theme.of(context).textTheme.bodyMedium),
            const SizedBox(height: 12),
            Text('Capabilities', style: Theme.of(context).textTheme.titleMedium),
            for (final c in androidCapabilities)
              ListTile(
                dense: true,
                leading: Icon(c.supported ? Icons.check_circle : Icons.circle_outlined),
                title: Text(c.name),
                subtitle: Text(c.note),
              ),
          ],
        ),
      ),
    );
  }
}
