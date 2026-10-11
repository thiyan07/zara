// History -> conversation resume: TalkStreamPanel session reuse +
// read-only transcript, HistoryPanel Continue (rapid-tap, stale, gone),
// and the deleted-session fresh-start path. Socket-free fakes only.
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';
import 'package:zara_android/history_view.dart';
import 'package:zara_android/stream_client.dart';
import 'package:zara_android/talk_stream_view.dart';

/// Socket-free talk backend: scripted open errors per session id so the
/// gone-session path (404 on a resumed id, fresh retry succeeds) is
/// deterministic.
class _ResumeTalkBackend extends TalkBackend {
  _ResumeTalkBackend() : super(CoreClient('http://127.0.0.1:1'));
  StreamController<TalkStreamEvent> ctrl =
      StreamController<TalkStreamEvent>.broadcast();
  final Map<String, Object> openErrorsBySession = {};
  int openCalls = 0;
  int fallbackCalls = 0;
  final List<String> sessionIds = [];
  final List<String> approvals = [];

  @override
  Future<TalkStreamHandle> openStream(
      {required String text,
      required String deviceId,
      String sessionId = ''}) async {
    openCalls += 1;
    sessionIds.add(sessionId);
    final err = openErrorsBySession[sessionId];
    if (err != null) throw err;
    return TalkStreamHandle.fake(ctrl.stream);
  }

  @override
  Future<Map<String, dynamic>> fallback(
      {required String text,
      required String deviceId,
      String sessionId = ''}) async {
    fallbackCalls += 1;
    return {
      'reply': 'fb',
      'status': 'ok',
      'turn_id': 't-fb',
      'mission_id': '',
      'execution_id': '',
      'session_id': 'ses-fresh',
    };
  }

  @override
  Future<Map<String, dynamic>?> cancelStream(TalkStreamHandle handle) async {
    unawaited(handle.close());
    return {'cancelled': true};
  }
}

TalkStreamEvent _ev(String name, Map<String, dynamic> payload) =>
    TalkStreamEvent(name, Map<String, dynamic>.from(payload));

Future<void> _pumpTalk(
  WidgetTester t,
  _ResumeTalkBackend backend, {
  String sessionId = '',
  List<String>? approved,
}) {
  return t.pumpWidget(MaterialApp(
    home: Scaffold(
      body: SingleChildScrollView(
        child: TalkStreamPanel(
          client: CoreClient('http://127.0.0.1:1'),
          tokenCtrl: TextEditingController(),
          sessionId: sessionId,
          onApproval: (id, reason) => approved?.add(id),
          backend: backend,
        ),
      ),
    ),
  ));
}

Future<void> _sendText(WidgetTester t, String text) async {
  await t.enterText(find.byType(TextField), text);
  await t.tap(find.text('Send'));
  await t.pump();
  await t.pump();
}

Future<void> _settle(WidgetTester t, bool Function() cond) async {
  for (var i = 0; i < 20; i++) {
    await t.pump(const Duration(milliseconds: 50));
    if (cond()) return;
  }
}

TalkStreamPanelState _talkState(WidgetTester t) =>
    t.state<TalkStreamPanelState>(find.byType(TalkStreamPanel));

/// Socket-free history backend with scripted details + errors.
class _ResumeHistory implements HistoryBackend {
  List<HistorySummary> summaries = [];
  Map<String, HistoryDetail> details = {};
  Object? openError;
  Completer<HistoryDetail>? openGate;
  final List<String> opens = [];
  final Set<String> deletedIds = {};
  String? resumedId;
  List<Map<String, dynamic>>? resumedMessages;
  int resumeCalls = 0;

  @override
  Future<List<HistorySummary>> list({int limit = 20}) async =>
      summaries.where((r) => !deletedIds.contains(r.id)).toList();

  @override
  Future<HistoryDetail> open(String id) async {
    opens.add(id);
    final gate = openGate;
    if (gate != null) return gate.future;
    final err = openError;
    if (err != null) throw err;
    final d = details[id];
    if (d == null) throw const CoreException(404, 'GET -> 404 unknown');
    return d;
  }

  @override
  Future<void> remove(String id) async {
    deletedIds.add(id);
    summaries = summaries.where((r) => r.id != id).toList();
  }

  void onResume(String id, List<Map<String, dynamic>> messages) {
    resumeCalls += 1;
    resumedId = id;
    resumedMessages = messages;
  }
}

Widget _historyPanel(_ResumeHistory backend) => MaterialApp(
      home: Scaffold(
        body: SingleChildScrollView(
          child: HistoryPanel(
            client: CoreClient('http://127.0.0.1:1'),
            tokenCtrl: TextEditingController(),
            backend: backend,
            onResume: backend.onResume,
          ),
        ),
      ),
    );

void main() {
  group('talk resume: session reuse + read-only history', () {
    testWidgets('initial sessionId reused on first send, server id adopted',
        (t) async {
      final backend = _ResumeTalkBackend();
      await _pumpTalk(t, backend, sessionId: 'ses-resumed');
      await _sendText(t, 'continue here');
      expect(backend.openCalls, 1);
      expect(backend.sessionIds.single, 'ses-resumed');
      backend.ctrl.add(_ev('turn', {'turn_id': 't-r1'}));
      backend.ctrl.add(_ev('done', {
        'reply': 'on it',
        'status': 'ok',
        'turn_id': 't-r1',
        'mission_id': '',
        'execution_id': '',
        'session_id': 'ses-server',
      }));
      await t.pump();
      await t.pump();
      expect(find.textContaining('Done (ok)'), findsOneWidget);
      // Adopted server id wins for the next turn.
      await _sendText(t, 'next');
      expect(backend.sessionIds.last, 'ses-server');
    });

    testWidgets('loadHistory renders marked read-only rows, zero backend calls',
        (t) async {
      final backend = _ResumeTalkBackend();
      final approved = <String>[];
      await _pumpTalk(t, backend, approved: approved);
      _talkState(t).loadHistory([
        {'role': 'user', 'text': 'earlier question'},
        {
          'role': 'assistant',
          'text': 'earlier answer',
          'tool': 'util.echo',
        },
      ]);
      await t.pump();
      expect(find.text('earlier question'), findsOneWidget);
      expect(find.textContaining('earlier answer'), findsOneWidget);
      // Explicit historical markers, never live bubbles.
      expect(find.text('You (history)'), findsOneWidget);
      expect(find.text('Zara (history)'), findsOneWidget);
      expect(find.textContaining('read-only'), findsOneWidget);
      expect(find.textContaining('[tool: util.echo]'), findsOneWidget);
      // No-resubmit: loading never touched the backend; history rows never
      // executed (no approval callbacks, no tool runs).
      expect(backend.openCalls, 0);
      expect(backend.fallbackCalls, 0);
      expect(approved, isEmpty);
    });

    testWidgets('stale generation-tagged loads are discarded', (t) async {
      final backend = _ResumeTalkBackend();
      await _pumpTalk(t, backend);
      final state = _talkState(t);
      final gen = state.newHistoryGeneration();
      // Newer plain load lands first…
      state.loadHistory([
        {'role': 'user', 'text': 'newest question'}
      ]);
      await t.pump();
      // …then the stale tagged load resolves: discarded, never rendered.
      state.loadHistory([
        {'role': 'user', 'text': 'stale question'}
      ], generation: gen);
      await t.pump();
      expect(find.text('newest question'), findsOneWidget);
      expect(find.text('stale question'), findsNothing);
      expect(backend.openCalls, 0);
    });

    testWidgets('history secrets are redacted, never verbatim', (t) async {
      final backend = _ResumeTalkBackend();
      await _pumpTalk(t, backend);
      _talkState(t).loadHistory([
        {'role': 'user', 'text': 'login Bearer hunter2-token-here ok'},
        {'role': 'assistant', 'text': 'key sk-abc123XYZ_-z noted'},
      ]);
      await t.pump();
      expect(find.textContaining('Bearer [redacted]'), findsOneWidget);
      expect(find.textContaining('[redacted]'), findsWidgets);
      expect(find.textContaining('hunter2'), findsNothing);
      expect(find.textContaining('sk-abc123XYZ'), findsNothing);
      expect(backend.openCalls, 0);
    });

    testWidgets('404 on resumed id -> fresh session + explicit notice',
        (t) async {
      final backend = _ResumeTalkBackend()
        ..openErrorsBySession['ses-gone'] =
            const CoreException(404, 'POST /v1/talk -> 404 unknown session');
      await _pumpTalk(t, backend, sessionId: 'ses-gone');
      await _sendText(t, 'hello again');
      // Gone id tried once, then exactly one fresh retry — never a loop.
      await _settle(t, () => backend.openCalls >= 2);
      expect(backend.sessionIds.take(2), ['ses-gone', '']);
      expect(find.textContaining('previous session deleted, started new'),
          findsOneWidget);
      // The retried fresh turn still completes normally.
      backend.ctrl.add(_ev('turn', {'turn_id': 't-fresh'}));
      backend.ctrl.add(_ev('done', {
        'reply': 'fresh answer',
        'status': 'ok',
        'turn_id': 't-fresh',
        'mission_id': '',
        'execution_id': '',
        'session_id': 'ses-new',
      }));
      await t.pump();
      await t.pump();
      expect(find.textContaining('fresh answer'), findsOneWidget);
      // Resumed transcript stays visible alongside the fresh turn.
    });

    testWidgets('rapid double-tap still submits once after resume', (t) async {
      final backend = _ResumeTalkBackend();
      await _pumpTalk(t, backend, sessionId: 'ses-resumed');
      await t.enterText(find.byType(TextField), 'hello');
      await t.tap(find.text('Send'));
      await t.tap(find.text('Send'));
      await t.pump();
      await t.pump();
      expect(backend.openCalls, 1);
      expect(find.textContaining('already sending'), findsOneWidget);
    });
  });

  group('history Continue', () {
    testWidgets('row Continue delivers id + transcript, no prompt submit',
        (t) async {
      final backend = _ResumeHistory()
        ..summaries = const [
          HistorySummary(id: 'ses-1', messageCount: 2, lastText: 'q'),
        ]
        ..details = const {
          'ses-1': HistoryDetail(id: 'ses-1', messages: [
            HistoryMessage(role: 'user', text: 'q'),
            HistoryMessage(
                role: 'assistant', text: 'a', tool: 'util.echo'),
          ]),
        };
      await t.pumpWidget(_historyPanel(backend));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('history-continue-ses-1')), findsOneWidget);
      await t.tap(find.byKey(const Key('history-continue-ses-1')));
      await t.pumpAndSettle();
      expect(backend.resumeCalls, 1);
      expect(backend.resumedId, 'ses-1');
      expect(backend.resumedMessages?.length, 2);
      expect(backend.resumedMessages?[0]['role'], 'user');
      expect(backend.resumedMessages?[0]['text'], 'q');
      expect(backend.resumedMessages?[1]['tool'], 'util.echo');
      expect(backend.opens, ['ses-1']); // GET-only fetch, never a submit
      expect(find.textContaining('resumed ses-1'), findsOneWidget);
    });

    testWidgets('rapid double-tap on Continue opens once', (t) async {
      final backend = _ResumeHistory()
        ..summaries = const [
          HistorySummary(id: 'ses-1', messageCount: 1, lastText: 'q'),
        ]
        ..openGate = Completer<HistoryDetail>();
      await t.pumpWidget(_historyPanel(backend));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('history-continue-ses-1')));
      await t.tap(find.byKey(const Key('history-continue-ses-1')));
      backend.openGate!.complete(const HistoryDetail(id: 'ses-1', messages: [
        HistoryMessage(role: 'user', text: 'q'),
      ]));
      await t.pumpAndSettle();
      expect(backend.opens, ['ses-1']);
      expect(backend.resumeCalls, 1);
    });

    testWidgets('stale resume resolves are discarded', (t) async {
      final backend = _ResumeHistory()
        ..summaries = const [
          HistorySummary(id: 'ses-a', messageCount: 1, lastText: 'a'),
          HistorySummary(id: 'ses-b', messageCount: 1, lastText: 'b'),
        ]
        ..details = const {
          'ses-a': HistoryDetail(id: 'ses-a', messages: [
            HistoryMessage(role: 'user', text: 'stale-a'),
          ]),
          'ses-b': HistoryDetail(id: 'ses-b', messages: [
            HistoryMessage(role: 'user', text: 'newest-b'),
          ]),
        };
      await t.pumpWidget(_historyPanel(backend));
      await t.pumpAndSettle();
      // Slow first resume parks on a gate…
      final gate = Completer<HistoryDetail>();
      backend.openGate = gate;
      await t.tap(find.byKey(const Key('history-continue-ses-a')));
      await t.pump();
      // …newer resume for another session completes first…
      backend.openGate = null;
      await t.tap(find.byKey(const Key('history-continue-ses-b')));
      await t.pumpAndSettle();
      expect(backend.resumedId, 'ses-b');
      // …then the stale one resolves: discarded, never delivered.
      gate.complete(const HistoryDetail(id: 'ses-a', messages: [
        HistoryMessage(role: 'user', text: 'stale-a'),
      ]));
      await t.pumpAndSettle();
      expect(backend.resumeCalls, 1);
      expect(backend.resumedId, 'ses-b');
    });

    testWidgets('resume of a 404 session is an explicit gone state',
        (t) async {
      final backend = _ResumeHistory()
        ..summaries = const [
          HistorySummary(id: 'ses-gone', messageCount: 1, lastText: 'q'),
        ]
        ..openError =
            const CoreException(404, 'GET /v1/sessions/ses-gone -> 404');
      await t.pumpWidget(_historyPanel(backend));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('history-continue-ses-gone')));
      await t.pumpAndSettle();
      expect(backend.resumeCalls, 0); // callback never fires for gone rows
      expect(find.textContaining('gone'), findsOneWidget);
      // The gone row is dropped from the list like a delete.
      expect(find.byKey(const Key('history-row-ses-gone')), findsNothing);
    });

    testWidgets('detail Continue reuses the loaded transcript', (t) async {
      final backend = _ResumeHistory()
        ..summaries = const [
          HistorySummary(id: 'ses-1', messageCount: 2, lastText: 'q'),
        ]
        ..details = const {
          'ses-1': HistoryDetail(id: 'ses-1', messages: [
            HistoryMessage(role: 'user', text: 'detail-q'),
            HistoryMessage(role: 'assistant', text: 'detail-a'),
          ]),
        };
      await t.pumpWidget(_historyPanel(backend));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('history-row-ses-1')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('history-continue-detail')), findsOneWidget);
      await t.tap(find.byKey(const Key('history-continue-detail')));
      await t.pumpAndSettle();
      expect(backend.resumeCalls, 1);
      expect(backend.resumedId, 'ses-1');
      expect(backend.resumedMessages?.length, 2);
      // One open total (the row tap); detail Continue refetches nothing.
      expect(backend.opens, ['ses-1']);
    });
  });
}
