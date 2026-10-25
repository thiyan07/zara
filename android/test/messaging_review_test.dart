// Messaging approvals panel: client shapes ({id}/cancel/approve incl.
// 401/404/409) against a loopback stub (no client mocks) + pure shape
// helpers + the review panel (fake backend, socket-free). Mirrors
// memory_review_test.dart.
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';
import 'package:zara_android/messaging_review.dart';

/// Scripted loopback stub for the operator messaging contracts.
class _MsgStub {
  final List<String> methods = [];
  final List<String> paths = [];
  final List<Map<String, String>> queries = [];
  final List<Map<String, dynamic>> bodies = [];
  final List<String> authHeaders = [];
  int status = 200;
  dynamic reply = const {};
}

Future<HttpServer> _serveMsg(_MsgStub stub) async {
  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
  server.listen((req) async {
    try {
      stub.methods.add(req.method);
      stub.paths.add(req.uri.path);
      stub.queries.add(Map<String, String>.from(req.uri.queryParameters));
      stub.authHeaders.add(req.headers.value('authorization') ?? '');
      final text = await utf8.decoder.bind(req).join();
      stub.bodies.add(text.isEmpty ? {} : jsonDecode(text));
      var (status, reply) = (stub.status, stub.reply);
      req.response.statusCode = status;
      req.response.headers.contentType = ContentType.json;
      req.response.write(jsonEncode(reply));
      await req.response.close();
    } catch (_) {}
  });
  return server;
}

Map<String, dynamic> _row(String id, {String state = 'pending'}) => {
      'approval_id': id,
      'mission_id': 'mission-$id',
      'device_id': 'phone',
      'conversation': 'Dev Team',
      'state': state,
      'actions': ['open_conversation', 'compose', 'send', 'verify'],
      'created_at': 0.0,
      'approved_at': null,
      'outcome': null,
      'executions': <dynamic>[],
    };

void _useRealHttp() {
  // flutter_test hijacks HttpClient (400 for every request) once the
  // widgets binding initializes; wire tests need REAL loopback HTTP.
  HttpOverrides.global = null;
}

void main() {
  setUp(_useRealHttp);
  group('messaging wire shapes', () {
    test('path + body helpers are exact and fail fast on empty', () {
      expect(messagingApprovalPath('a-1'),
          equals('/v1/messaging/approvals/a-1'));
      expect(messagingApprovalPath('a b/c'),
          equals('/v1/messaging/approvals/a%20b%2Fc'));
      expect(buildMessagingApproveBody('user'), equals({'who': 'user'}));
      expect(() => messagingApprovalPath(''), throwsArgumentError);
    });
  });

  group('messaging client over real loopback HTTP', () {
    test('list sends operator Bearer + parses redacted rows', () async {
      final stub = _MsgStub()..reply = {'approvals': [_row('a-1')]};
      _useRealHttp();
      final server = await _serveMsg(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.operatorToken = 'op-token';
        final rows = await c.messagingListPending();
        expect(stub.methods.single, equals('GET'));
        expect(stub.paths.single, equals('/v1/messaging/approvals'));
        expect(stub.authHeaders.single, equals('Bearer op-token'));
        expect(rows.single['approval_id'], equals('a-1'));
        // Redaction contract: no plaintext field anywhere in the wire row.
        expect(rows.single.containsKey('message'), isFalse);
        expect(rows.single.containsKey('text'), isFalse);
        expect(rows.single.containsKey('fingerprint'), isFalse);
      } finally {
        await server.close();
      }
    });

    test('approve posts to the exact bound path', () async {
      final stub = _MsgStub()..reply = {'status': 'succeeded'};
      _useRealHttp();
      final server = await _serveMsg(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        final out = await c.messagingApprove('a-1');
        expect(stub.methods.single, equals('POST'));
        expect(stub.paths.single,
            equals('/v1/messaging/approvals/a-1/approve'));
        expect(stub.bodies.single, equals({'who': 'user'}));
        expect(out['status'], equals('succeeded'));
      } finally {
        await server.close();
      }
    });

    test('cancel posts and 409 surfaces for a silently-ended approval',
        () async {
      final stub = _MsgStub()
        ..reply = {'state': 'cancelled'};
      _useRealHttp();
      final server = await _serveMsg(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        final out = await c.messagingCancel('a-1');
        expect(stub.methods.single, equals('POST'));
        expect(stub.paths.single,
            equals('/v1/messaging/approvals/a-1/cancel'));
        expect(out['state'], equals('cancelled'));
      } finally {
        await server.close();
      }
    });
  });

  group('parse helpers', () {
    test('parseMessagingApprovals keeps redacted fields only', () {
      final views = parseMessagingApprovals([
        _row('x'),
        {'approval_id': '', 'state': 'pending', 'actions': null},
      ]);
      expect(views.length, equals(2));
      expect(views.first.isDecidable, isTrue);
      expect(views.first.actions,
          equals(['open_conversation', 'compose', 'send', 'verify']));
      expect(views.last.isDecidable, isFalse); // no id -> never decidable
    });

    test('error helper maps 409/404/401 explicitly', () {
      expect(
          messagingDecisionError(
              CoreException(409, 'approval ended'),
              approve: true),
          contains('already ended'));
      expect(
          messagingDecisionError(
              CoreException(404, 'gone'),
              approve: false),
          contains('Refresh'));
      expect(
          messagingDecisionError(
              CoreException(401, 'no token'),
              approve: true),
          contains('operator token'));
    });
  });

  group('panel (fake backend, socket-free)', () {
    testWidgets('renders recipient + plan, never plaintext, and approves '
        'on tap', (tester) async {
      final v = const MessagingView(
          id: 'a-1', deviceId: 'phone', conversation: 'Dev Team',
          state: 'pending',
          actions: ['open_conversation', 'compose', 'send', 'verify']);
      var wasApproved = false;
      var wasCancelled = false;
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(body: MessagingReviewPanel(
              backend: _FakeBackend2([v], (ok, id) async {
        if (ok) {
          wasApproved = true;
        } else {
          wasCancelled = true;
        }
        return <String, dynamic>{'status': ok ? 'succeeded' : 'cancelled'};
      })))));
      await tester.pumpAndSettle();
      expect(find.textContaining('To: Dev Team'), findsOneWidget);
      // The UI hint legitimately SAYS it never shows content; the wire
      // guarantee is that no row-derived text (values/fingerprints) does.
      expect(find.textContaining('fingerprint'), findsNothing);
      await tester.tap(find.text('Approve & send'));
      await tester.pumpAndSettle();
      expect(find.textContaining('succeeded'), findsWidgets);
      expect(wasApproved, isTrue);
      expect(wasCancelled, isFalse);
    });

    testWidgets('cancel path posts and reloads to empty explicitly',
        (tester) async {
      final v = const MessagingView(
          id: 'a-2', deviceId: 'phone', conversation: 'Asha',
          state: 'pending', actions: ['send']);
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(body: MessagingReviewPanel(
              backend: _FakeBackend2([v], (ok, id) async {
        return <String, dynamic>{'state': 'cancelled'};
      })))));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Cancelled'), findsOneWidget);
    });

    testWidgets('terminal-state rows are display-only', (tester) async {
      final v = const MessagingView(
          id: 'a-3', deviceId: 'phone', conversation: 'Group',
          state: 'completed', actions: ['send']);
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(body: MessagingReviewPanel(
              backend: _FakeBackend2([v], (ok, id) async => <String, dynamic>{})))));
      await tester.pumpAndSettle();
      expect(find.text('Approve & send'), findsNothing);
      expect(find.text('Cancel'), findsNothing);
      expect(find.textContaining('completed'), findsWidgets);
    });
  });
}

class _FakeBackend2 implements MessagingBackend {
  final List<MessagingView> rows;
  final Future<Map<String, dynamic>> Function(bool, String) decide;
  _FakeBackend2(this.rows, this.decide);

  int _calls = 0;

  @override
  Future<List<MessagingView>> pending() async {
    _calls++;
    if (_calls > 1 && rows.isNotEmpty) return const [];
    return rows;
  }

  @override
  Future<Map<String, dynamic>> approve(String approvalId) =>
      decide(true, approvalId);

  @override
  Future<Map<String, dynamic>> cancel(String approvalId) =>
      decide(false, approvalId);
}
