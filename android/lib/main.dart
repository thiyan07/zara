import 'dart:async';
import 'package:flutter/material.dart';
import 'app_state.dart';
import 'battery_governor.dart';
import 'capabilities.dart';
import 'connection.dart';
import 'core_client.dart';
import 'device_bridge.dart';
import 'device_lifecycle.dart';
import 'job_runner.dart';
import 'network_monitor.dart';
import 'secure_store.dart';
import 'voice.dart' show wakePhrase;
import 'voice_session.dart';
import 'zara_notifications.dart';

void main() => runApp(const ZaraApp());

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
      defaultValue: 'http://10.0.2.2:8080');
  final client = CoreClient(_coreUrl);
  final secureStore = SecureStore();
  final lifecycle = DeviceLifecycle();
  final voice = AndroidVoiceSession();
  final notifCenter = NotificationCenter();
  late final AndroidJobRunner jobs;
  final codeCtrl = TextEditingController();
  final retry = const RetryPolicy();

  String conn = CoreLinkState.disconnected;
  BatteryReport battery = BatteryReport(timestamp: DateTime.now());
  NetworkSnapshot network =
      NetworkSnapshot('unavailable', timestamp: DateTime.now());
  Map<String, dynamic> voiceFlags = {};
  Map<String, dynamic> perms = {};
  String lastError = '';
  int _failures = 0;
  Timer? _loop;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    jobs = AndroidJobRunner(
      battery: () => battery.toHeartbeat(),
      network: () => {'network': network.state, 'metered': network.metered},
    );
    lifecycle.onChange = (_) => _render();
    _boot();
  }

  void _render() {
    if (mounted) setState(() {});
  }

  Future<void> _boot() async {
    await bridge.createNotificationChannels();
    await _refresh();
    final creds = await secureStore.loadDeviceCredentials();
    if (creds.deviceId == null || creds.deviceKey == null) {
      lifecycle.move(DeviceLifecycleState.pairing);
      _render();
      return;
    }
    client.deviceId = creds.deviceId;
    client.deviceKey = creds.deviceKey;
    lifecycle.restore(creds.deviceId);
    await _register();
  }

  Future<void> _refresh() async {
    final b = await bridge.battery();
    final n = await bridge.network();
    Map<String, dynamic> v = {};
    try {
      v = await bridge.voiceSupport();
    } catch (_) {/* keep static table */}
    try {
      final t = await bridge.audioSelfTest();
      // ignore: avoid_print
      print('zara:audio-selftest=$t');
    } catch (_) {/* self-test best-effort */}
    try {
      perms = await bridge.permissions();
    } catch (_) {/* keep last */}
    if (!mounted) return;
    setState(() {
      battery = BatteryReport.fromBridge(b);
      network = NetworkSnapshot.fromBridge(n);
      voiceFlags = v;
    });
    // ignore: avoid_print
    print('zara:voice-support=$v');
  }

  Future<void> _register() async {
    setState(() => conn = CoreLinkState.connecting);
    try {
      await client.register(advertisedCapabilities(), 'zara-android 9.0.0');
      lifecycle.move(DeviceLifecycleState.online);
      setState(() {
        conn = CoreLinkState.online;
        lastError = '';
        _failures = 0;
      });
      _startLoop();
    } on AuthException catch (e) {
      await secureStore.clearDeviceCredentials();
      lifecycle.handleHttp(403, hasIdentity: true);
      setState(() {
        conn = CoreLinkState.error;
        lastError = 'revoked: $e';
      });
    } catch (e) {
      setState(() {
        conn = CoreLinkState.offline;
        lastError = '$e';
      });
    }
  }

  void _startLoop() {
    _loop?.cancel();
    // Battery-critical: no auto loop; user reconnects manually.
    if (battery.level() == AndroidBatteryLevel.critical) return;
    final interval = battery.reduceBackgroundWork
        ? const Duration(seconds: 60)
        : const Duration(seconds: 30);
    _loop = Timer.periodic(interval, (_) => _tick());
    unawaited(_tick());
  }

  Future<void> _tick() async {
    if (_busy || lifecycle.state != DeviceLifecycleState.online) return;
    if (!network.online) {
      setState(() => conn = CoreLinkState.offline);
      return;
    }
    _busy = true;
    final scope = CancelScope();
    try {
      await _refresh();
      await client.heartbeat(
        batteryPct: battery.pct,
        charging: battery.charging,
        network: network.state,
        cancel: scope,
      );
      if (battery.pct != null &&
          battery.pct! < AndroidBatteryLevel.criticalBelow &&
          !battery.charging) {
        lifecycle.move(DeviceLifecycleState.degraded);
        setState(() => conn = CoreLinkState.degraded);
      } else if (lifecycle.state == DeviceLifecycleState.degraded &&
          (battery.pct == null ||
              battery.pct! >= AndroidBatteryLevel.resumeAbove)) {
        lifecycle.move(DeviceLifecycleState.online);
        setState(() => conn = CoreLinkState.online);
      }
      await _pollJobs(scope);
      await _pullNotifications();
      setState(() {
        _failures = 0;
        if (conn != CoreLinkState.degraded) {
          conn = CoreLinkState.online;
        }
      });
    } on AuthException {
      await secureStore.clearDeviceCredentials();
      lifecycle.handleHttp(403, hasIdentity: true);
      setState(() => conn = CoreLinkState.error);
    } on NotRegisteredException {
      // Backend restarted: re-register once, never loop.
      try {
        await client.register(advertisedCapabilities(), 'zara-android 9.0.0');
      } catch (_) {/* next tick retries with backoff */}
    } catch (_) {
      _failures += 1;
      final waitLonger =
          !retry.shouldRetry(_failures, batteryCritical: false);
      setState(() => conn = waitLonger
          ? CoreLinkState.offline
          : CoreLinkState.reconnecting);
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
    final out = jobs.run(job);
    await client.reportJobResult(job.jobId,
        ok: out.ok, result: out.result, error: out.error);
  }

  Future<void> _pullNotifications() async {
    final list = await client.fetchNotifications();
    var changed = false;
    for (final raw in list) {
      try {
        if (notifCenter.show(DeviceNotification.fromJson(raw))) {
          changed = true;
        }
      } catch (_) {/* skip malformed, keep polling */}
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
    setState(() => lastError = '');
    try {
      await client.claim(codeCtrl.text.trim(), 'android-phone');
      await secureStore.saveDeviceCredentials(
        deviceId: client.deviceId ?? 'android-phone',
        deviceKey: client.deviceKey ?? '',
      );
      lifecycle.move(DeviceLifecycleState.enrolled);
      lifecycle.move(DeviceLifecycleState.registering);
      await _register();
    } on CoreException catch (e) {
      lifecycle.move(DeviceLifecycleState.error);
      setState(() => lastError = '$e');
    }
  }

  Future<void> _logout() async {
    _loop?.cancel();
    try {
      await client.disconnect();
    } catch (_) {}
    await secureStore.clearDeviceCredentials();
    client.deviceId = null;
    client.deviceKey = null;
    lifecycle.logout();
    setState(() => conn = CoreLinkState.disconnected);
  }

  Future<void> _decide(
      DeviceNotification n, String execId, bool approve) async {
    try {
      await client.decideApproval(execId, approve);
      notifCenter.ack(n.id);
      try {
        await client.ackNotification(n.id);
      } catch (_) {}
    } on CoreException catch (e) {
      setState(() => lastError = 'approval: $e');
    }
    if (mounted) setState(() {});
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // Foreground/background: pause the loop in background to save radio,
    // resume + refresh on foreground. No state is lost: Core is the source.
    if (state == AppLifecycleState.paused) {
      _loop?.cancel();
    } else if (state == AppLifecycleState.resumed &&
        lifecycle.state == DeviceLifecycleState.online) {
      _startLoop();
    }
  }

  @override
  void dispose() {
    _loop?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    codeCtrl.dispose();
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
      theme: ThemeData(useMaterial3: true, colorSchemeSeed: Colors.deepPurple),
      home: Scaffold(
        appBar: AppBar(title: const Text('Zara')),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Text('Status: ${s.statusLine()}',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 4),
            Text(network.describe()),
            Text('Battery: ${battery.pct?.toStringAsFixed(0) ?? '?'}% '
                '(charging: ${battery.charging}, level: ${battery.level()})'),
            Text('Voice: ${voice.state} — wake phrase "$wakePhrase"'),
            Text('Mic permission: ${perms['microphone'] ?? '?'} | '
                'Notifications: ${perms['notifications'] ?? '?'}'),
            if (lastError.isNotEmpty)
              Text('Error: $lastError',
                  style: const TextStyle(color: Colors.red)),
            const SizedBox(height: 12),
            if (lifecycle.state == DeviceLifecycleState.pairing ||
                lifecycle.state == DeviceLifecycleState.error ||
                lifecycle.state == DeviceLifecycleState.revoked ||
                lifecycle.state == DeviceLifecycleState.loggedOut) ...[
              TextField(controller: codeCtrl,
                  decoration: const InputDecoration(
                      labelText: 'Pairing code from Zara Core')),
              const SizedBox(height: 8),
              FilledButton(
                  onPressed: _connect, child: const Text('Connect “Hey Zara” device')),
              const SizedBox(height: 8),
            ],
            if (notifCenter.hasPendingApprovals) ...[
              Text('Approval required',
                  style: Theme.of(context).textTheme.titleMedium),
              for (final n in notifCenter.pending
                  .where((x) => x.channel == 'zara_approvals'))
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
                                  onPressed: () =>
                                      _decide(n, n.executionId, true),
                                  child: const Text('Approve')),
                              TextButton(
                                  onPressed: () =>
                                      _decide(n, n.executionId, false),
                                  child: const Text('Deny')),
                            ],
                          ),
                  ),
                ),
            ],
            for (final n in notifCenter.pending
                .where((x) => x.channel != 'zara_approvals'))
              ListTile(
                  dense: true,
                  leading: const Icon(Icons.notifications_outlined),
                  title: Text(n.title),
                  subtitle: Text(n.body)),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                OutlinedButton(
                    onPressed: () async {
                      final m = await bridge.requestMicPermission();
                      if (mounted) {
                        setState(() => perms['microphone'] = m['granted']);
                      }
                    },
                    child: const Text('Enable mic')),
                OutlinedButton(
                    onPressed: () async {
                      final m = await bridge.requestNotifPermission();
                      if (mounted) {
                        setState(
                            () => perms['notifications'] = m['granted']);
                      }
                    },
                    child: const Text('Enable notifications')),
                OutlinedButton(
                    onPressed: _logout, child: const Text('Sign out')),
              ],
            ),
            const SizedBox(height: 12),
            Text('Capabilities',
                style: Theme.of(context).textTheme.titleMedium),
            for (final c in androidCapabilities)
              ListTile(
                dense: true,
                leading: Icon(c.supported
                    ? Icons.check_circle
                    : Icons.circle_outlined),
                title: Text(c.name),
                subtitle: Text(c.note),
              ),
          ],
        ),
      ),
    );
  }
}
