import 'dart:async';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'app_state.dart';
import 'assistant.dart';
import 'audio_io.dart';
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

/// 409 from Core on decide = execution already decided elsewhere
/// (approved/denied/expired/cancelled): the approval card is stale and
/// should be dismissed locally, not retried. Pure for testing.
bool isStaleApproval(Object e) => e.toString().contains('409');

/// Foreground return restarts polling when the body is live (online) or
/// throttled-but-alive (degraded). Every other state needs explicit user
/// action (pairing) or is terminal (revoked/logged-out). Pure for testing.
/// _startLoop still refuses to schedule while battery-critical.
bool shouldResumeLoop(String lifecycleState) =>
    lifecycleState == DeviceLifecycleState.online ||
    lifecycleState == DeviceLifecycleState.degraded;

/// Re-register backoff: 5s, 10s, 20s, 40s, then 60s cap.
/// Pure for testing. Timer-driven (no busy loop); cancelled on pause.
Duration registerRetryDelay(int attempt) {
  var secs = 5 * (1 << (attempt - 1).clamp(0, 10));
  if (secs > 60) secs = 60;
  return Duration(seconds: secs);
}

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
  AssistantStatus assistant = const AssistantStatus();
  String lastError = '';
  int _failures = 0;
  String secureBackend = 'keystore';
  String voiceDiag = 'idle';
  String voiceTranscript = '';
  String voiceReply = '';
  String voiceCapture = '';
  String voicePlayback = '';
  String voiceStt = '';
  Uint8List? lastReplyAudio;
  bool voiceBusy = false;
  Timer? _loop;
  bool _busy = false;
  final Set<String> _deciding = {};
  Timer? _regRetry;
  int _regAttempts = 0;
  bool _registering = false;

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
    secureBackend = secureStore.backend;
    // ignore: avoid_print
    print('zara:secure-backend=$secureBackend');
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
    try {
      final am = await bridge.assistantStatus();
      assistant = AssistantStatus.fromMap(am);
      // ignore: avoid_print
      print('zara:assistant-status'
          ' registered=${assistant.serviceRegistered}'
          ' default=${assistant.zaraIsDefault}'
          ' roleAvail=${assistant.roleAvailable}'
          ' roleHeld=${assistant.roleHeld}'
          ' current=${assistant.currentAssistant}'
          ' infoValid=${am['service_info_valid']}'
          ' supportsAssist=${am['supports_assist']}'
          ' infoError=${am['service_info_error']}');
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
    // Re-entrancy: retry timer + foreground resume must not overlap.
    if (_registering) return;
    _registering = true;
    _regRetry?.cancel();
    setState(() => conn = CoreLinkState.connecting);
    try {
      await client.register(advertisedCapabilities(), 'zara-android 9.0.0');
      _regAttempts = 0;
      lifecycle.move(DeviceLifecycleState.online);
      setState(() {
        conn = CoreLinkState.online;
        lastError = '';
        _failures = 0;
      });
      _startLoop();
    } on AuthException catch (e) {
      _regRetry?.cancel();
      await secureStore.clearDeviceCredentials();
      lifecycle.handleHttp(403, hasIdentity: true);
      setState(() {
        conn = CoreLinkState.error;
        lastError = 'revoked: $e';
      });
    } catch (e) {
      // Register-while-unreachable (Stage 11 fix): without a retry the
      // lifecycle strands in `registering` forever — no loop ever starts.
      // Timer-driven bounded backoff; cancelled on pause/dispose.
      _regAttempts += 1;
      _regRetry?.cancel();
      _regRetry = Timer(registerRetryDelay(_regAttempts), () {
        // Only retry if nobody moved the lifecycle meanwhile
        // (logout/revoke/pairing wins over the timer).
        if (mounted &&
            (lifecycle.state == DeviceLifecycleState.registering ||
                lifecycle.state == DeviceLifecycleState.reconnecting)) {
          _register();
        }
      });
      setState(() {
        conn = _regAttempts <= 2
            ? CoreLinkState.reconnecting
            : CoreLinkState.offline;
        lastError = '$e';
      });
    } finally {
      _registering = false;
    }
  }

  void _startLoop() {
    _loop?.cancel();
    // Battery-critical: no auto loop; user reconnects manually.
    // (Loop may still be restarted later; _tick re-checks every round.)
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
      secureBackend = secureStore.backend;
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
    _regRetry?.cancel();
    try {
      await client.disconnect();
    } catch (_) {}
    await secureStore.clearDeviceCredentials();
    client.deviceId = null;
    client.deviceKey = null;
    lifecycle.logout();
    setState(() => conn = CoreLinkState.disconnected);
  }

  /// Diagnostic voice loop (physical validation): bounded capture ->
  /// Core turn -> provider TTS -> speaker. All legs report separately;
  /// playback completion is logged as software-only, never audibility.
  Future<void> _recordTest() async {
    if (voiceBusy) return;
    setState(() {
      voiceBusy = true;
      voiceDiag = 'recording… (speak now)';
    });
    try {
      final wav = await bridge.audioCapture(seconds: 5.0);
      if (wav == null || wav.isEmpty) {
        setState(() => voiceDiag = 'capture returned no audio');
        return;
      }
      final peak = wavPeakDbfs(wav);
      voiceCapture =
          'mic: ${wav.length}B peak=${peak?.toStringAsFixed(1) ?? 'silence'} dBFS';
      setState(() => voiceDiag =
          'captured ${wav.length}B peak=${peak?.toStringAsFixed(1) ?? 'silence'} dBFS; thinking…');
      // ignore: avoid_print
      print('zara:voice-capture bytes=${wav.length} '
          'peakDb=${peak?.toStringAsFixed(1) ?? 'silence'}');
      final out = await client.voiceTurn(wav);
      voiceTranscript = '${out['transcript'] ?? ''}';
      voiceReply = '${out['reply'] ?? ''}';
      final fb = out['stt_fallback_used'] == true
          ? ' (fallback:${out['stt_fallback_reason']})'
          : '';
      voiceStt = 'stt: ${out['stt_provider'] ?? '?'}'
          ' ${out['stt_model'] ?? ''}$fb';
      setState(() => voiceDiag =
          'heard: "$voiceTranscript" (peak ${peak?.toStringAsFixed(1) ?? 'silence'} dBFS)');
      // ignore: avoid_print
      print('zara:voice-turn transcript="$voiceTranscript" '
          'reply="$voiceReply" status=${out['status']}');
      if (voiceReply.isNotEmpty) {
        setState(() => voiceDiag += ' — fetching speech…');
        lastReplyAudio = await client.tts(voiceReply);
        setState(() => voiceDiag += ' (${lastReplyAudio!.length}B audio ready)');
      }
    } on AudioBridgeException catch (e) {
      setState(() => voiceDiag = 'capture: $e');
    } on CoreException catch (e) {
      setState(() => voiceDiag = 'core: $e');
    } finally {
      if (mounted) setState(() => voiceBusy = false);
    }
  }

  Future<void> _speakLast() async {
    final audio = lastReplyAudio;
    if (audio == null || voiceBusy) return;
    setState(() {
      voiceBusy = true;
      voiceDiag = 'speaking…';
    });
    try {
      final r = await bridge.audioPlay(audio);
      voicePlayback = 'speaker: $r (software-only, NOT audibility)';
      setState(() => voiceDiag = 'playback exited: $r (software-only)');
      // ignore: avoid_print
      print('zara:voice-playback $r');
    } on AudioBridgeException catch (e) {
      voicePlayback = 'speaker FAILED: $e';
      setState(() => voiceDiag = 'playback: $e');
    } finally {
      if (mounted) setState(() => voiceBusy = false);
    }
  }

  Future<void> _stopVoice() async {
    try {
      await bridge.audioStop();
    } catch (_) {}
    try {
      await bridge.audioPlayStop();
    } catch (_) {}
    try {
      await client.interruptVoice();
    } catch (_) {}
    if (mounted) setState(() => voiceDiag = 'stopped');
  }

  Future<void> _decide(
      DeviceNotification n, String execId, bool approve) async {
    // One in-flight decision per execution: double-taps collapse into the
    // first request. Core 409 remains the real duplicate-execution guard.
    if (!_deciding.add(execId)) return;
    try {
      await client.decideApproval(execId, approve);
      notifCenter.ack(n.id);
      try {
        await client.ackNotification(n.id);
      } catch (_) {}
    } on CoreException catch (e) {
      // 409 = Core already decided (approved/denied/expired elsewhere):
      // the card is stale, so dismiss it locally instead of nagging.
      // Anything else keeps the card for retry.
      if (isStaleApproval(e)) {
        notifCenter.ack(n.id);
        try {
          await client.ackNotification(n.id);
        } catch (_) {}
        if (mounted) setState(() {});
        return;
      }
      setState(() => lastError = 'approval: $e');
    } finally {
      _deciding.remove(execId);
    }
    if (mounted) setState(() {});
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // Foreground/background: pause the loop in background to save radio,
    // resume + refresh on foreground. No state is lost: Core is the source.
    if (state == AppLifecycleState.paused) {
      _loop?.cancel();
      _regRetry?.cancel();
    } else if (state == AppLifecycleState.resumed &&
        shouldResumeLoop(lifecycle.state)) {
      // Degraded included: backgrounding while degraded must not kill the
      // loop forever (Stage 11). _startLoop still refuses when critical.
      _startLoop();
    } else if (state == AppLifecycleState.resumed &&
        lifecycle.state == DeviceLifecycleState.registering) {
      // Register-while-unreachable: resume retries instead of stranding.
      _register();
    }
  }

  @override
  void dispose() {
    _loop?.cancel();
    _regRetry?.cancel();
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
            Text('Assistant: ${assistant.describe()}'),
            Text('Mic permission: ${perms['microphone'] ?? '?'} | '
                'Notifications: ${perms['notifications'] ?? '?'}'),
            Text('Keys: $secureBackend'),
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
            Text('Voice diagnostic (physical)',
                style: Theme.of(context).textTheme.titleMedium),
            Text(voiceDiag, style: Theme.of(context).textTheme.bodyMedium),
            if (voiceCapture.isNotEmpty) Text(voiceCapture),
            if (voiceStt.isNotEmpty) Text(voiceStt),
            if (voiceTranscript.isNotEmpty) Text('Heard: $voiceTranscript'),
            if (voiceReply.isNotEmpty) Text('Zara: $voiceReply'),
            if (voicePlayback.isNotEmpty) Text(voicePlayback),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                FilledButton(
                    onPressed: (voiceBusy ||
                            lifecycle.state != DeviceLifecycleState.online)
                        ? null
                        : _recordTest,
                    child: const Text('Record 5s test')),
                OutlinedButton(
                    onPressed: (voiceBusy || lastReplyAudio == null)
                        ? null
                        : _speakLast,
                    child: const Text('Speak reply')),
                OutlinedButton(
                    onPressed: voiceBusy ? _stopVoice : null,
                    child: const Text('Stop')),
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
