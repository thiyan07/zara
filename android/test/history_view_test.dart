// Conversation history: client shapes (list/get/delete incl. 404/401)
// against a loopback stub (no client mocks) + pure shape helpers + the
// history panel (fake backend, socket-free).
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';
import 'package:zara_android/history_view.dart';

/// Scripted loopback stub for the operator session contracts.
class _SessionStub {
  final List<String> methods = [];
  final List<String> paths = [];
  final List<Map<String, String>> queries = [];
  final List<String> authHeaders = [];
  int status = 200;
  dynamic reply = const [];
}

Future<HttpServer> _serveSessions(_SessionStub stub) async {
  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
  server.listen((req) async {
    try {
      stub.methods.add(req.method);
      stub.paths.add(req.uri.path);
      stub.queries.add(Map<String, String>.from(req.uri.queryParameters));
      stub.authHeaders.add(req.headers.value('authorization') ?? '');
      await utf8.decoder.bind(req).join(); // drain body (DELETE has none)
      req.response.statusCode = stub.status;
      req.response.headers.contentType = ContentType.json;
      req.response.write(jsonEncode(stub.reply));
      await req.response.close();
    } catch (_) {}
  });
  return server;
}

void _useRealHttp() {
  // flutter_test hijacks HttpClient (400 for every request) once the
  // widgets binding initializes; wire tests need REAL loopback HTTP.
  HttpOverrides.global = null;
}

Widget _panel(HistoryBackend backend) => MaterialApp(
      home: Scaffold(
        body: SingleChildScrollView(
          child: HistoryPanel(
            client: CoreClient('http://127.0.0.1:1'),
            tokenCtrl: TextEditingController(),
            backend: backend,
          ),
        ),
      ),
    );

void main() {
  setUp(_useRealHttp);
  group('session wire shapes', () {
    test('list query + detail path are exact', () {
      expect(sessionListQuery(limit: 7), equals({'limit': '7'}));
      expect(sessionDetailPath('ses-abc'), equals('/v1/sessions/ses-abc'));
      expect(() => sessionDetailPath(''), throwsArgumentError);
    });

    test('list sends operator Bearer + limit, parses rows', () async {
      final stub = _SessionStub()
        ..reply = [
          {
            'id': 'ses-1',
            'updated_at': '2026-01-02T03:04:05',
            'message_count': 4,
            'last_text': 'hello there'
          },
          {
            'id': 'ses-2',
            'updated_at': '2026-01-01T00:00:00',
            'message_count': 0,
            'last_text': ''
          },
        ];
      _useRealHttp();
      final server = await _serveSessions(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.operatorToken = 'op-token';
        final rows = await c.sessionList(limit: 20);
        expect(stub.methods.single, equals('GET'));
        expect(stub.paths.single, equals('/v1/sessions'));
        expect(stub.queries.single, equals({'limit': '20'}));
        expect(stub.authHeaders.single, equals('Bearer op-token'));
        final parsed = parseHistoryList(rows);
        expect(parsed.length, equals(2));
        expect(parsed[0].id, equals('ses-1'));
        expect(parsed[0].messageCount, equals(4));
        expect(parsed[0].lastText, equals('hello there'));
        expect(parsed[1].lastText, isEmpty);
      } finally {
        await server.close(force: true);
      }
    });

    test('get returns full session incl. messages; never POSTs', () async {
      final stub = _SessionStub()
        ..reply = {
          'id': 'ses-1',
          'messages': [
            {
              'role': 'user',
              'text': 'hi',
              'tool': '',
              'created_at': '2026-01-02T03:04:00'
            },
            {
              'role': 'assistant',
              'text': 'hello',
              'tool': 'util.echo',
              'created_at': '2026-01-02T03:04:05'
            },
          ],
        };
      _useRealHttp();
      final server = await _serveSessions(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.operatorToken = 'op-token';
        final m = await c.sessionGet('ses-1');
        expect(stub.methods.single, equals('GET')); // read-only, no submit
        expect(stub.paths.single, equals('/v1/sessions/ses-1'));
        final detail = parseHistoryDetail(m);
        expect(detail.messages.length, equals(2));
        expect(detail.messages[0].role, equals('user'));
        expect(detail.messages[1].tool, equals('util.echo'));
        expect(detail.awaitingReply, isFalse);
      } finally {
        await server.close(force: true);
      }
    });

    test('delete sends DELETE; 404 is removed-elsewhere', () async {
      final stub = _SessionStub()..reply = {'deleted': 'ses-9'};
      _useRealHttp();
      final server = await _serveSessions(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.operatorToken = 'op-token';
        final out = await c.sessionDelete('ses-9');
        expect(stub.methods.single, equals('DELETE'));
        expect(stub.paths.single, equals('/v1/sessions/ses-9'));
        expect(out['deleted'], equals('ses-9'));
      } finally {
        await server.close(force: true);
      }
      final gone = _SessionStub()
        ..status = 404
        ..reply = {'detail': 'unknown session'};
      _useRealHttp();
      final server2 = await _serveSessions(gone);
      try {
        final c = CoreClient('http://127.0.0.1:${server2.port}');
        c.operatorToken = 'op-token';
        Object? err;
        try {
          await c.sessionDelete('ses-missing');
        } catch (e) {
          err = e;
        }
        expect(err, isNotNull);
        expect(isSessionNotFound(err ?? 'no error'), isTrue);
        expect(describeHistoryError(err ?? 'no error'),
            contains('removed elsewhere'));
      } finally {
        await server2.close(force: true);
      }
    });

    test('401 classifies as token problem', () {
      expect(historyErrorKind(const CoreException(401, 'GET -> 401 x')),
          equals('auth'));
      expect(describeHistoryError(const CoreException(403, 'GET -> 403 x')),
          contains('operator token'));
    });
  });

  group('history pure helpers', () {
    test('parseHistoryList tolerates non-list + id-less rows', () {
      expect(parseHistoryList({'no': 'list'}), isEmpty);
      expect(parseHistoryList(null), isEmpty);
      final rows = parseHistoryList([
        {'id': 'ses-1', 'message_count': 2, 'last_text': 'hi'},
        {'message_count': 1}, // no id: undisplayable, dropped
      ]);
      expect(rows.length, equals(1));
      expect(rows.single.id, equals('ses-1'));
    });

    test('awaitingReply only when last message is user-role', () {
      final waiting = parseHistoryDetail({
        'id': 's',
        'messages': [
          {'role': 'assistant', 'text': 'a'},
          {'role': 'user', 'text': 'q'},
        ],
      });
      expect(waiting.awaitingReply, isTrue);
      final done = parseHistoryDetail({
        'id': 's',
        'messages': [
          {'role': 'user', 'text': 'q'},
          {'role': 'assistant', 'text': 'a'},
        ],
      });
      expect(done.awaitingReply, isFalse);
      expect(parseHistoryDetail({'id': 's'}).awaitingReply, isFalse);
      expect(parseHistoryDetail({'id': 's', 'messages': 'junk'})
          .messages, isEmpty);
    });

    test('redaction kills Bearer and sk- secrets', () {
      expect(redactHistoryText('token Bearer abcDEF123-_.9 ok'),
          equals('token Bearer [redacted] ok'));
      expect(redactHistoryText('key sk-abc123XYZ_-z here'),
          equals('key [redacted] here'));
      expect(redactHistoryText('plain text'), equals('plain text'));
      // Preview truncation keeps rows small-screen safe.
      final long = 'x' * 200;
      expect(historyPreview(long).length, lessThan(130));
      expect(historyPreview('short'), equals('short'));
    });

    test('historyErrorKind buckets transport as offline', () {
      expect(historyErrorKind(const SocketException('Failed host lookup')),
          equals('offline'));
      expect(
          historyErrorKind(const CoreException(-1, 'transport down')),
          equals('offline'));
      expect(historyErrorKind(const CoreException(500, 'boom')),
          equals('server'));
      expect(historyErrorKind(const CoreException(404, 'gone')),
          equals('notFound'));
    });
  });

  group('history panel', () {
    testWidgets('list rows show preview + count + timestamp',
        (tester) async {
      final backend = _FakeHistory()
        ..summaries = [
          const HistorySummary(
              id: 'ses-1',
              updatedAt: '2026-01-02',
              messageCount: 3,
              lastText: 'hello world'),
        ];
      await tester.pumpWidget(_panel(backend));
      await tester.pumpAndSettle();
      expect(find.text('hello world'), findsOneWidget);
      expect(find.textContaining('3 messages'), findsOneWidget);
      expect(find.textContaining('2026-01-02'), findsOneWidget);
      expect(backend.posts, equals(0)); // listing never submits prompts
    });

    testWidgets('empty list states "no conversations yet"', (tester) async {
      await tester.pumpWidget(_panel(_FakeHistory()));
      await tester.pumpAndSettle();
      expect(find.text('no conversations yet'), findsOneWidget);
    });

    testWidgets('error state: 401/403 points at the operator token',
        (tester) async {
      final backend = _FakeHistory()
        ..listError = const CoreException(401, 'GET -> 401 invalid token');
      await tester.pumpWidget(_panel(backend));
      await tester.pumpAndSettle();
      expect(find.textContaining('operator token'), findsWidgets);
    });

    testWidgets('error state: transport failure reads offline',
        (tester) async {
      final backend = _FakeHistory()
        ..listError = const SocketException('Failed host lookup');
      await tester.pumpWidget(_panel(backend));
      await tester.pumpAndSettle();
      expect(find.textContaining('offline'), findsWidgets);
    });

    testWidgets('error state: server failure is explicit', (tester) async {
      final backend = _FakeHistory()
        ..listError = const CoreException(500, 'GET -> 500 boom');
      await tester.pumpWidget(_panel(backend));
      await tester.pumpAndSettle();
      expect(find.textContaining('core error'), findsWidgets);
    });

    testWidgets('open shows messages + tool marker + incomplete state',
        (tester) async {
      final backend = _FakeHistory()
        ..summaries = [
          const HistorySummary(
              id: 'ses-1', messageCount: 2, lastText: 'q'),
          const HistorySummary(
              id: 'ses-2', messageCount: 2, lastText: 'done-q'),
        ]
        ..details = {
          'ses-1': const HistoryDetail(id: 'ses-1', messages: [
            HistoryMessage(role: 'user', text: 'question here'),
            HistoryMessage(role: 'user', text: 'still waiting'),
          ]),
          'ses-2': const HistoryDetail(id: 'ses-2', messages: [
            HistoryMessage(role: 'user', text: 'q'),
            HistoryMessage(
                role: 'assistant', text: 'a', tool: 'util.echo'),
          ]),
        };
      await tester.pumpWidget(_panel(backend));
      await tester.pumpAndSettle();
      // Open the user-last session: explicit incomplete state, never success.
      await tester.tap(find.byKey(const Key('history-row-ses-1')));
      await tester.pumpAndSettle();
      expect(find.text('question here'), findsOneWidget);
      expect(find.text('still waiting'), findsOneWidget);
      expect(find.textContaining('awaiting reply — never marked complete'),
          findsOneWidget);
      expect(backend.opens, equals(['ses-1']));
      expect(backend.posts, equals(0)); // opening never submits prompts
      // Back, then open the assistant-last session: tool marker, no banner.
      await tester.tap(find.byKey(const Key('history-back')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('history-row-ses-2')));
      await tester.pumpAndSettle();
      expect(find.textContaining('[tool: util.echo]'), findsOneWidget);
      expect(find.textContaining('awaiting reply'), findsNothing);
    });

    testWidgets('rapid double-tap opens once', (tester) async {
      final backend = _FakeHistory()
        ..summaries = [
          const HistorySummary(id: 'ses-1', messageCount: 1, lastText: 'q'),
        ]
        ..openGate = Completer<HistoryDetail>();
      await tester.pumpWidget(_panel(backend));
      await tester.pumpAndSettle();
      // Two taps before the first open resolves: second collapses.
      await tester.tap(find.byKey(const Key('history-row-ses-1')));
      await tester.tap(find.byKey(const Key('history-row-ses-1')));
      backend.openGate!.complete(const HistoryDetail(id: 'ses-1', messages: [
        HistoryMessage(role: 'user', text: 'q'),
      ]));
      await tester.pumpAndSettle();
      expect(backend.opens, equals(['ses-1']));
    });

    testWidgets('delete asks first; cancel keeps, confirm deletes',
        (tester) async {
      final backend = _FakeHistory()
        ..summaries = [
          const HistorySummary(id: 'ses-1', messageCount: 1, lastText: 'q'),
        ];
      await tester.pumpWidget(_panel(backend));
      await tester.pumpAndSettle();
      // Cancel path: nothing sent, row stays.
      await tester
          .tap(find.byKey(const Key('history-delete-ses-1')));
      await tester.pumpAndSettle();
      expect(find.text('Delete conversation?'), findsOneWidget);
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(backend.deletes, isEmpty);
      expect(find.byKey(const Key('history-row-ses-1')), findsOneWidget);
      // Confirm path: DELETE sent, row dropped, list refreshed.
      await tester
          .tap(find.byKey(const Key('history-delete-ses-1')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('history-confirm-delete')));
      await tester.pumpAndSettle();
      expect(backend.deletes, equals(['ses-1']));
      expect(backend.lists, greaterThanOrEqualTo(2)); // refreshed after
      expect(find.byKey(const Key('history-row-ses-1')), findsNothing);
      expect(find.textContaining('deleted ses-1'), findsOneWidget);
    });

    testWidgets('stale list response cannot overwrite a newer list',
        (tester) async {
      final backend = _FakeHistory()..manualLists = true;
      await tester.pumpWidget(_panel(backend));
      await tester.pump(); // initState refresh #1 starts, gate held
      expect(backend.lists, equals(1));
      await tester.tap(find.byKey(const Key('history-refresh')));
      await tester.pump(); // manual refresh #2 starts (newer generation)
      expect(backend.lists, equals(2));
      // Newer (#2) resolves first with list B, then stale (#1) with list A.
      backend.gates[1].complete([
        const HistorySummary(id: 'ses-new', lastText: 'new list'),
      ]);
      await tester.pump();
      backend.gates[0].complete([
        const HistorySummary(id: 'ses-stale', lastText: 'stale list'),
      ]);
      await tester.pumpAndSettle();
      expect(find.text('new list'), findsOneWidget);
      expect(find.text('stale list'), findsNothing);
    });

    testWidgets('displayed text redacts secrets', (tester) async {
      final backend = _FakeHistory()
        ..summaries = [
          const HistorySummary(
              id: 'ses-1',
              messageCount: 1,
              lastText: 'Bearer hunter2-token here'),
        ]
        ..details = {
          'ses-1': const HistoryDetail(id: 'ses-1', messages: [
            HistoryMessage(role: 'user', text: 'my key sk-abc123XYZ'),
          ]),
        };
      await tester.pumpWidget(_panel(backend));
      await tester.pumpAndSettle();
      expect(find.textContaining('Bearer [redacted]'), findsOneWidget);
      expect(find.textContaining('hunter2'), findsNothing);
      await tester.tap(find.byKey(const Key('history-row-ses-1')));
      await tester.pumpAndSettle();
      expect(find.textContaining('[redacted]'), findsWidgets);
      expect(find.textContaining('sk-abc123XYZ'), findsNothing);
    });
  });
}

/// Socket-free fake: no HTTP, deterministic. [posts] counts prompt-
/// submitting calls — the interface has none, so it must stay 0.
class _FakeHistory implements HistoryBackend {
  List<HistorySummary> summaries = [];
  Map<String, HistoryDetail> details = {};
  Object? listError;
  Completer<HistoryDetail>? openGate;
  int lists = 0;
  final List<String> opens = [];
  final List<String> deletes = [];
  int posts = 0;

  /// When true, each list() call parks on a fresh completer in [gates]
  /// so tests can resolve generations out of order.
  bool manualLists = false;
  final List<Completer<List<HistorySummary>>> gates = [];

  @override
  Future<List<HistorySummary>> list({int limit = 20}) async {
    lists += 1;
    final e = listError;
    if (e != null) throw e;
    if (manualLists) {
      final gate = Completer<List<HistorySummary>>();
      gates.add(gate);
      return gate.future;
    }
    return summaries
        .where((r) => !deletedIds.contains(r.id))
        .toList();
  }

  final Set<String> deletedIds = {};

  @override
  Future<HistoryDetail> open(String id) async {
    opens.add(id);
    final gate = openGate;
    if (gate != null) return gate.future;
    final d = details[id];
    if (d == null) throw const CoreException(404, 'GET -> 404 unknown');
    return d;
  }

  @override
  Future<void> remove(String id) async {
    deletes.add(id);
    deletedIds.add(id);
    summaries = summaries.where((r) => r.id != id).toList();
  }
}
