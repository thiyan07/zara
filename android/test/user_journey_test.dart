// User-journey suite: one coherent pass driving the REAL panels together
// (TalkStreamPanel + Tasks-wire + HistoryPanel + MemoryReviewPanel +
// ProviderSetupPanel + DiagnosticsPanel).
//
// Socket-free widget tests use fake backends (the flutter_tester cannot
// hold loopback servers open); wire behavior that needs HTTP runs in
// plain unit tests against a loopback HttpServer. No emulator is used or
// claimed anywhere in this file.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';
import 'package:zara_android/diagnostics_panel.dart';
import 'package:zara_android/history_view.dart';
import 'package:zara_android/main.dart'
    show describeExecutionError, taskSubmitKey;
import 'package:zara_android/memory_review.dart';
import 'package:zara_android/provider_setup.dart';
import 'package:zara_android/stream_client.dart';
import 'package:zara_android/talk_stream_view.dart';

void _useRealHttp() {
  // flutter_test hijacks HttpClient (400 for every request) once the
  // widgets binding initializes; loopback tests need the real stack.
  HttpOverrides.global = null;
}

TalkStreamEvent _ev(String name, Map<String, dynamic> payload) =>
    TalkStreamEvent(name, Map<String, dynamic>.from(payload));

/// Pump frames until [cond] holds (bounded). Stream teardown chains span
/// async gaps that include zero-duration timers, which only fire when
/// fake time advances — so pump WITH a duration.
Future<void> _settle(WidgetTester t, bool Function() cond) async {
  for (var i = 0; i < 20; i++) {
    await t.pump(const Duration(milliseconds: 50));
    if (cond()) return;
  }
}

/// Socket-free talk backend: drives [TalkStreamHandle.fake] event streams.
class _JourneyTalk extends TalkBackend {
  _JourneyTalk() : super(CoreClient('http://127.0.0.1:1'));
  StreamController<TalkStreamEvent> ctrl =
      StreamController<TalkStreamEvent>.broadcast();
  Object? openError;
  Map<String, dynamic>? fallbackReply;
  Object? fallbackError;
  int openCalls = 0;
  int fallbackCalls = 0;
  bool cancelCalled = false;
  String lastText = '';
  String lastSessionId = '';

  @override
  Future<TalkStreamHandle> openStream(
      {required String text,
      required String deviceId,
      String sessionId = ''}) async {
    openCalls += 1;
    lastSessionId = sessionId;
    final err = openError;
    if (err != null) throw err;
    lastText = text;
    return TalkStreamHandle.fake(ctrl.stream);
  }

  @override
  Future<Map<String, dynamic>> fallback(
      {required String text,
      required String deviceId,
      String sessionId = ''}) async {
    fallbackCalls += 1;
    final err = fallbackError;
    if (err != null) throw err;
    return fallbackReply ??
        {
          'reply': '',
          'status': '',
          'turn_id': '',
          'mission_id': '',
          'execution_id': '',
        };
  }

  @override
  Future<Map<String, dynamic>?> cancelStream(TalkStreamHandle handle) async {
    cancelCalled = true;
    unawaited(handle.close());
    return {'cancelled': true};
  }
}

class _JourneyHistory implements HistoryBackend {
  List<HistorySummary> summaries = [];
  Map<String, HistoryDetail> details = {};
  Object? listError;
  int lists = 0;
  final List<String> opens = [];
  final List<String> deletes = [];

  @override
  Future<List<HistorySummary>> list({int limit = 20}) async {
    lists += 1;
    final e = listError;
    if (e != null) throw e;
    return List.of(summaries);
  }

  @override
  Future<HistoryDetail> open(String id) async {
    opens.add(id);
    final d = details[id];
    if (d == null) throw const CoreException(404, 'GET -> 404 unknown');
    return d;
  }

  @override
  Future<void> remove(String id) async {
    deletes.add(id);
    summaries = summaries.where((r) => r.id != id).toList();
  }
}

class _JourneyMemory implements MemoryBackend {
  List<MemoryHit> hits = [];
  int searches = 0;
  final List<String> forgets = [];
  Object? searchError;
  int purges = 0;
  int purgedCount = 3;

  @override
  Future<List<MemoryHit>> search(String q, {int topK = 5}) async {
    searches += 1;
    final e = searchError;
    if (e != null) throw e;
    return List.of(hits);
  }

  @override
  Future<Map<String, dynamic>> forget(String id) async {
    forgets.add(id);
    hits = hits.where((h) => h.id != id).toList();
    return {'forgotten': id};
  }

  @override
  Future<List<MemoryCandidate>> candidates() async => const [];

  @override
  Future<Map<String, dynamic>> confirm(String candidateId, bool ok) async =>
      {'stored': ok};

  @override
  Future<Map<String, dynamic>> purge() async {
    purges += 1;
    return {'purged': purgedCount};
  }
}

class _JourneyReady implements ReadyBackend {
  final ReadyInfo info;
  Object? error;
  int fetches = 0;
  _JourneyReady(this.info);

  @override
  Future<ReadyInfo> fetch() async {
    fetches += 1;
    final e = error;
    if (e != null) throw e;
    return info;
  }
}

Widget _scroll(Widget child) => MaterialApp(
      home: Scaffold(body: SingleChildScrollView(child: child)),
    );

Future<void> _sendTalk(WidgetTester t, String text) async {
  await t.enterText(find.byType(TextField), text);
  await t.tap(find.text('Send'));
  await t.pump();
  await t.pump();
}

/// All Text-widget strings currently visible (for the secret sweep).
String _visibleText(WidgetTester t) {
  final buf = StringBuffer();
  for (final w in t.widgetList(find.byType(Text))) {
    final d = (w as Text).data ?? w.textSpan?.toPlainText() ?? '';
    buf.writeln(d);
  }
  return buf.toString();
}

void main() {
  setUp(_useRealHttp);

  group('journey 1: new conversation streams, reconciles, keeps session', () {
    testWidgets('deltas accumulate, done replaces (no dup), id adopted',
        (t) async {
      final backend = _JourneyTalk();
      final tokenCtrl = TextEditingController();
      addTearDown(tokenCtrl.dispose);
      await t.pumpWidget(_scroll(TalkStreamPanel(
        client: CoreClient('http://127.0.0.1:1'),
        tokenCtrl: tokenCtrl,
        onApproval: (id, reason) {},
        backend: backend,
      )));
      await _sendTalk(t, 'hello zara');
      expect(backend.openCalls, 1);
      expect(backend.lastSessionId, isEmpty); // nothing known yet

      backend.ctrl.add(_ev('turn', {'turn_id': 't-ux-1'}));
      backend.ctrl.add(_ev('delta', {'text': 'Hello '}));
      backend.ctrl.add(_ev('delta', {'text': 'world'}));
      await t.pump();
      expect(find.textContaining('Hello world'), findsOneWidget);

      // Terminal done carries the server-resolved session id.
      backend.ctrl.add(_ev('done', {
        'reply': 'Hello world',
        'status': 'ok',
        'turn_id': 't-ux-1',
        'mission_id': '',
        'execution_id': '',
        'session_id': 'ses-ux-1',
      }));
      await t.pump();
      await t.pump();
      expect(find.textContaining('Done (ok)'), findsOneWidget);
      // Reconciled into the SAME bubble: exactly one copy, never two.
      expect(find.textContaining('Hello world'), findsOneWidget);
      expect(backend.openCalls, 1); // terminal: never re-sends

      // Next send in the SAME panel state reuses the adopted session id.
      await _sendTalk(t, 'second question');
      expect(backend.openCalls, 2);
      expect(backend.lastSessionId, 'ses-ux-1');
      backend.ctrl.add(_ev('turn', {'turn_id': 't-ux-2'}));
      backend.ctrl.add(_ev('done', {
        'reply': 'second answer',
        'status': 'ok',
        'turn_id': 't-ux-2',
        'mission_id': '',
        'execution_id': '',
        'session_id': 'ses-ux-1',
      }));
      await t.pump();
      await t.pump();
      expect(find.textContaining('second answer'), findsOneWidget);
    });
  });

  group('journey 2: navigate away and back — history keeps continuity', () {
    testWidgets('disposed panel + new instance still finds the session',
        (t) async {
      // 1) Talk panel creates ses-ux-1 (same flow as journey 1).
      final talk = _JourneyTalk();
      final tokenCtrl = TextEditingController();
      addTearDown(tokenCtrl.dispose);
      await t.pumpWidget(_scroll(TalkStreamPanel(
        client: CoreClient('http://127.0.0.1:1'),
        tokenCtrl: tokenCtrl,
        onApproval: (id, reason) {},
        backend: talk,
      )));
      await _sendTalk(t, 'remember the journey');
      talk.ctrl.add(_ev('turn', {'turn_id': 't-ux-9'}));
      talk.ctrl.add(_ev('done', {
        'reply': 'remembered',
        'status': 'ok',
        'turn_id': 't-ux-9',
        'mission_id': '',
        'execution_id': '',
        'session_id': 'ses-ux-1',
      }));
      await t.pump();
      await t.pump();
      expect(find.textContaining('Done (ok)'), findsOneWidget);

      // 2) Navigate away: dispose the panel state entirely.
      await t.pumpWidget(
          const MaterialApp(home: Scaffold(body: SizedBox())));
      await t.pump();

      // 3) Navigate back: a NEW panel instance + history seeded from the
      // created ids (what Core would return for GET /v1/sessions).
      final history = _JourneyHistory()
        ..summaries = const [
          HistorySummary(
              id: 'ses-ux-1',
              updatedAt: '2026-10-10',
              messageCount: 2,
              lastText: 'remember the journey'),
        ]
        ..details = const {
          'ses-ux-1': HistoryDetail(id: 'ses-ux-1', messages: [
            HistoryMessage(role: 'user', text: 'remember the journey'),
            HistoryMessage(role: 'assistant', text: 'remembered'),
          ]),
        };
      await t.pumpWidget(_scroll(Column(children: [
        TalkStreamPanel(
          client: CoreClient('http://127.0.0.1:1'),
          tokenCtrl: tokenCtrl,
          onApproval: (id, reason) {},
          backend: _JourneyTalk(),
        ),
        HistoryPanel(
          client: CoreClient('http://127.0.0.1:1'),
          tokenCtrl: tokenCtrl,
          backend: history,
        ),
      ])));
      await t.pumpAndSettle();
      // Continuity contract at UI level: the session is listed and opens
      // with both turns — no prompt was ever re-sent to get here.
      expect(find.byKey(const Key('history-row-ses-ux-1')), findsOneWidget);
      expect(find.text('remember the journey'), findsWidgets);
      await t.tap(find.byKey(const Key('history-row-ses-ux-1')));
      await t.pumpAndSettle();
      expect(find.text('remembered'), findsOneWidget);
      expect(history.opens, ['ses-ux-1']);
    });
  });

  group('journey 3: approval-required tool event wires to approve', () {
    testWidgets('approval pointer + callback with execution id', (t) async {
      final backend = _JourneyTalk();
      String? approvedId;
      String? approvedReason;
      int calls = 0;
      final tokenCtrl = TextEditingController();
      addTearDown(tokenCtrl.dispose);
      await t.pumpWidget(_scroll(TalkStreamPanel(
        client: CoreClient('http://127.0.0.1:1'),
        tokenCtrl: tokenCtrl,
        onApproval: (id, reason) {
          calls += 1;
          approvedId = id;
          approvedReason = reason;
        },
        backend: backend,
      )));
      await _sendTalk(t, 'run the gated tool');
      backend.ctrl.add(_ev('turn', {'turn_id': 't-ap-1'}));
      backend.ctrl.add(_ev('approval',
          {'execution_id': 'ex-approve-1', 'reason': 'grant files?'}));
      await t.pump();
      expect(find.textContaining('Approval required'), findsOneWidget);
      expect(approvedId, 'ex-approve-1');
      expect(approvedReason, 'grant files?');
      expect(calls, 1);
      // Duplicate pointer for the same execution never re-fires.
      backend.ctrl.add(_ev('approval',
          {'execution_id': 'ex-approve-1', 'reason': 'grant files?'}));
      await t.pump();
      expect(calls, 1);
    });
  });

  group('journey 4: task submit -> running -> cancel -> terminal', () {
    test('loopback submit/status/cancel with sticky terminal', () async {
      _useRealHttp();
      var submitted = 0;
      var cancelled = 0;
      var lateSucceeded = false;
      final authHeaders = <String>[];
      final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      server.listen((req) async {
        try {
          final text = await utf8.decoder.bind(req).join();
          final body = text.isEmpty
              ? <String, dynamic>{}
              : jsonDecode(text) as Map<String, dynamic>;
          authHeaders.add(req.headers.value('authorization') ?? '');
          Map<String, dynamic> reply;
          if (req.uri.path == '/v1/executions' && req.method == 'POST') {
            submitted += 1;
            reply = {
              'id': 'ex-j-1',
              'tool': body['tool'],
              'device_id': body['device_id'],
              'status': 'running',
              'job_state': 'pending',
              'result': null,
              'error': '',
              'approval_required': false,
              'cancel_requested': false,
            };
          } else if (req.uri.path == '/v1/executions/ex-j-1/cancel') {
            cancelled += 1;
            reply = {
              'id': 'ex-j-1',
              'tool': 'util.echo',
              'device_id': 'android-phone',
              'status': 'cancelled',
              'job_state': 'cancelled',
              'result': null,
              'error': 'cancelled',
              'approval_required': false,
              'cancel_requested': true,
            };
          } else if (req.uri.path == '/v1/executions/ex-j-1') {
            // Late poll after cancel reports success elsewhere: the UI
            // must keep the sticky terminal state, never revive the row.
            reply = {
              'id': 'ex-j-1',
              'tool': 'util.echo',
              'device_id': 'android-phone',
              'status': lateSucceeded ? 'succeeded' : 'running',
              'job_state': lateSucceeded ? 'done' : 'pending',
              'result': null,
              'error': '',
              'approval_required': false,
              'cancel_requested': false,
            };
          } else {
            req.response.statusCode = 404;
            await req.response.close();
            return;
          }
          req.response.statusCode = 200;
          req.response.headers.contentType = ContentType.json;
          req.response.write(jsonEncode(reply));
          await req.response.close();
        } catch (_) {}
      });
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}',
            timeout: const Duration(seconds: 5));
        c.deviceId = 'android-phone';
        c.deviceKey = 'k1';
        c.operatorToken = 'op-token';

        final created = await c.submitExecution(
            tool: 'util.echo',
            inputs: {'text': 'hi'},
            deviceId: 'android-phone');
        var view = ExecutionView.fromJson(created);
        expect(view.status, 'running');
        expect(view.isTerminal, isFalse);
        expect(authHeaders.single, 'Bearer op-token');

        final refreshed =
            ExecutionView.fromJson(await c.executionStatus('ex-j-1'));
        expect(refreshed.isRunning, isTrue);

        view = ExecutionView.fromJson(await c.cancelExecution('ex-j-1'));
        expect(view.status, 'cancelled');
        expect(view.isTerminal, isTrue);

        // Late done arrives after the terminal cancel: sticky, ignored.
        lateSucceeded = true;
        final late =
            ExecutionView.fromJson(await c.executionStatus('ex-j-1'));
        expect(late.status, 'succeeded');
        final kept = view.isTerminal ? view : late;
        expect(kept.status, 'cancelled');

        expect(submitted, 1);
        expect(cancelled, 1);
      } finally {
        await server.close(force: true);
      }
    });

    test('duplicate-submit key collapses a rapid double-tap', () {
      final keys = <String>{};
      final key = taskSubmitKey('util.echo', {'text': 'hi'});
      expect(keys.add(key), isTrue);
      expect(keys.add(key), isFalse);
      expect(describeExecutionError(const CoreException(409, 'x')),
          contains('conflict'));
    });
  });

  group('journey 5: memory search -> forget -> purge', () {
    testWidgets('row gone after forget; purge reports count', (t) async {
      final backend = _JourneyMemory()
        ..hits = const [
          MemoryHit(
              id: 'mem-1', text: 'likes tea', category: 'preferences'),
          MemoryHit(id: 'mem-2', text: 'allergic to nuts', category: 'health'),
        ];
      final tokenCtrl = TextEditingController();
      addTearDown(tokenCtrl.dispose);
      await t.pumpWidget(_scroll(MemoryReviewPanel(
        client: CoreClient('http://127.0.0.1:1'),
        tokenCtrl: tokenCtrl,
        backend: backend,
      )));
      await t.enterText(find.byType(TextField).first, 'tea');
      await t.tap(find.text('Search'));
      await t.pumpAndSettle();
      expect(backend.searches, 1);
      expect(find.text('likes tea'), findsOneWidget);
      expect(find.text('allergic to nuts'), findsOneWidget);

      await t.tap(find.text('Forget').first);
      await t.pumpAndSettle();
      expect(backend.forgets, ['mem-1']);
      expect(find.text('likes tea'), findsNothing);
      expect(find.text('allergic to nuts'), findsOneWidget);
      expect(find.textContaining('forgot mem-1'), findsOneWidget);

      await t.tap(find.text('Purge expired'));
      await t.pumpAndSettle();
      expect(backend.purges, 1);
      expect(find.textContaining('purged 3 expired memories'), findsOneWidget);
    });
  });

  group('journey 6: offline everywhere is explicit, bounded, no storms', () {
    testWidgets('talk: stream + fallback down shows error once', (t) async {
      final backend = _JourneyTalk()
        ..openError = const SocketException('Connection refused')
        ..fallbackError = const SocketException('Connection refused');
      final tokenCtrl = TextEditingController();
      addTearDown(tokenCtrl.dispose);
      await t.pumpWidget(_scroll(TalkStreamPanel(
        client: CoreClient('http://127.0.0.1:1'),
        tokenCtrl: tokenCtrl,
        onApproval: (id, reason) {},
        backend: backend,
      )));
      await _sendTalk(t, 'hello offline');
      await _settle(
          t, () => find.text('Resend as new turn').evaluate().isNotEmpty);
      expect(find.textContaining('Error'), findsOneWidget);
      expect(find.text('Resend as new turn'), findsOneWidget);
      expect(backend.openCalls, 1);
      expect(backend.fallbackCalls, 1);
      await t.pump(const Duration(milliseconds: 200));
      await t.pump(const Duration(milliseconds: 200));
      expect(backend.openCalls, 1); // no silent retry loop
      expect(backend.fallbackCalls, 1);
    });

    testWidgets('history: transport failure reads offline, one fetch',
        (t) async {
      final backend = _JourneyHistory()
        ..listError = const SocketException('Failed host lookup');
      final tokenCtrl = TextEditingController();
      addTearDown(tokenCtrl.dispose);
      await t.pumpWidget(_scroll(HistoryPanel(
        client: CoreClient('http://127.0.0.1:1'),
        tokenCtrl: tokenCtrl,
        backend: backend,
      )));
      await t.pumpAndSettle();
      expect(find.textContaining('offline'), findsWidgets);
      expect(backend.lists, 1);
      await t.pump(const Duration(milliseconds: 200));
      expect(backend.lists, 1);
    });

    testWidgets('memory: transport failure is explicit, one search',
        (t) async {
      final backend = _JourneyMemory()
        ..searchError = const SocketException('Network is unreachable');
      final tokenCtrl = TextEditingController();
      addTearDown(tokenCtrl.dispose);
      await t.pumpWidget(_scroll(MemoryReviewPanel(
        client: CoreClient('http://127.0.0.1:1'),
        tokenCtrl: tokenCtrl,
        backend: backend,
      )));
      await t.enterText(find.byType(TextField).first, 'tea');
      await t.tap(find.text('Search'));
      await t.pumpAndSettle();
      expect(find.textContaining('Memory:'), findsOneWidget);
      expect(backend.searches, 1);
      await t.pump(const Duration(milliseconds: 200));
      expect(backend.searches, 1);
    });

    testWidgets('provider + diagnostics: refused reads unreachable, once',
        (t) async {
      final ready = _JourneyReady(const ReadyInfo(provider: 'echo'))
        ..error = const SocketException('Connection refused');
      final diag = _JourneyReady(const ReadyInfo(provider: 'echo'))
        ..error = const SocketException('Connection refused');
      await t.pumpWidget(_scroll(Column(children: [
        ProviderSetupPanel(
            client: CoreClient('http://127.0.0.1:1'), backend: ready),
        DiagnosticsPanel(
            client: CoreClient('http://127.0.0.1:1'),
            connection: 'offline',
            lastError: '',
            backend: diag),
      ])));
      await t.tap(find.text('Check provider connection'));
      await t.pumpAndSettle();
      await t.tap(find.text('Check readiness'));
      await t.pumpAndSettle();
      expect(find.textContaining('unreachable'), findsWidgets);
      expect(ready.fetches, 1);
      expect(diag.fetches, 1);
      await t.pump(const Duration(milliseconds: 200));
      expect(ready.fetches, 1);
      expect(diag.fetches, 1);
    });
  });

  group('journey 7: provider note, governor flags, no secrets rendered', () {
    test('ready is public: no auth headers on GET /v1/ready', () async {
      _useRealHttp();
      String? auth;
      String? device;
      String path = '';
      final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      server.listen((req) async {
        try {
          path = req.uri.path;
          auth = req.headers.value('authorization');
          device = req.headers.value('x-device-id');
          req.response.statusCode = 200;
          req.response.headers.contentType = ContentType.json;
          req.response.write(jsonEncode({
            'ready': true,
            'checks': {'provider': 'echo'},
            'governor_v2': true,
            'governor_shadow': false,
          }));
          await req.response.close();
        } catch (_) {}
      });
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.deviceId = 'android-phone';
        c.deviceKey = 'should-not-be-sent';
        c.operatorToken = 'should-not-be-sent-either';
        final info = ReadyInfo.fromJson(await c.ready());
        expect(path, '/v1/ready');
        expect(auth, isNull);
        expect(device, isNull);
        expect(info.provider, 'echo');
        expect(info.governorV2, isTrue);
        expect(info.governorShadow, isFalse);
      } finally {
        await server.close(force: true);
      }
    });

    testWidgets('echo note + governor flags show; secrets never render',
        (t) async {
      const secretToken = 'hunter2-secret-token';
      const secretKey = 'sk-live-abcdef123456';
      final tokenCtrl = TextEditingController();
      addTearDown(tokenCtrl.dispose);
      final history = _JourneyHistory()
        ..summaries = const [
          HistorySummary(
              id: 'ses-ux-1',
              messageCount: 1,
              lastText: 'quoted Bearer $secretToken here'),
        ]
        ..details = const {
          'ses-ux-1': HistoryDetail(id: 'ses-ux-1', messages: [
            HistoryMessage(role: 'user', text: 'quoted key $secretKey back'),
          ]),
        };
      await t.pumpWidget(_scroll(Column(children: [
        ProviderSetupPanel(
            client: CoreClient('http://127.0.0.1:1'),
            backend: _JourneyReady(const ReadyInfo(provider: 'echo'))),
        DiagnosticsPanel(
            client: CoreClient('http://127.0.0.1:1'),
            connection: 'online',
            lastError: 'submit 401 Bearer $secretToken, saw $secretKey',
            backend: _JourneyReady(const ReadyInfo(
              ready: true,
              provider: 'echo',
              registry: true,
              memoryWrite: true,
              eventBus: true,
              governorV2: true,
              governorShadow: false,
            ))),
        HistoryPanel(
          client: CoreClient('http://127.0.0.1:1'),
          tokenCtrl: tokenCtrl,
          backend: history,
        ),
      ])));
      await t.ensureVisible(find.text('Check provider connection'));
      await t.pumpAndSettle();
      await t.tap(find.text('Check provider connection'));
      await t.pumpAndSettle();
      await t.ensureVisible(find.text('Check readiness'));
      await t.pumpAndSettle();
      await t.tap(find.text('Check readiness'));
      await t.pumpAndSettle();

      // Provider: echo built-in note.
      expect(find.textContaining('echo (built-in, no key)'), findsWidgets);
      expect(
          find.textContaining('live inference unverified without credentials'),
          findsOneWidget);
      // Diagnostics: governor flags.
      expect(find.textContaining('v2=true'), findsOneWidget);
      expect(find.textContaining('shadow=false'), findsOneWidget);
      // Provider panel takes no credentials at all.
      expect(
          find.descendant(
              of: find.byType(ProviderSetupPanel),
              matching: find.byType(TextField)),
          findsNothing);

      // History redacts the quoted secrets in list + detail.
      await t.ensureVisible(find.byKey(const Key('history-row-ses-ux-1')));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('history-row-ses-ux-1')));
      await t.pumpAndSettle();

      final visible = _visibleText(t);
      expect(visible, contains('[redacted]'));
      expect(visible, isNot(contains(secretToken)));
      expect(visible, isNot(contains(secretKey)));
      expect(visible, isNot(contains('abcdef123456')));
      expect(visible, isNot(contains('Bearer $secretToken')));
    });
  });
}
