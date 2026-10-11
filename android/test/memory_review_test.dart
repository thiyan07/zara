// Memory review: client shapes (search/forget/confirm/purge/candidates
// incl. 404/422) against a loopback stub (no client mocks) + pure shape
// helpers + the review panel (fake backend, socket-free).
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';
import 'package:zara_android/memory_review.dart';

/// Scripted loopback stub for the operator memory contracts.
class _MemStub {
  final List<String> methods = [];
  final List<String> paths = [];
  final List<Map<String, String>> queries = [];
  final List<Map<String, dynamic>> bodies = [];
  final List<String> authHeaders = [];
  int status = 200;
  dynamic reply = const [];
}

Future<HttpServer> _serveMem(_MemStub stub) async {
  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
  server.listen((req) async {
    try {
      stub.methods.add(req.method);
      stub.paths.add(req.uri.path);
      stub.queries.add(Map<String, String>.from(req.uri.queryParameters));
      stub.authHeaders.add(req.headers.value('authorization') ?? '');
      final text = await utf8.decoder.bind(req).join();
      stub.bodies.add(text.isEmpty ? {} : jsonDecode(text));
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

void main() {
  setUp(_useRealHttp);
  group('memory wire shapes', () {
    test('search query + forget path + confirm body are exact', () {
      expect(memorySearchQuery('hello', topK: 3),
          equals({'q': 'hello', 'top_k': '3'}));
      expect(memoryForgetPath('abc'), equals('/v1/memory/abc'));
      expect(
          buildMemoryConfirmBody('cand-1', ok: false),
          equals({'candidate_id': 'cand-1', 'ok': false}));
      expect(() => memoryForgetPath(''), throwsArgumentError);
      expect(() => buildMemoryConfirmBody(''), throwsArgumentError);
    });

    test('search sends operator Bearer + query, parses rows', () async {
      final stub = _MemStub()
        ..reply = [
          {'text': 'likes tea', 'category': 'preferences', 'score': 0.9},
          {'text': 'no id row', 'category': 'episodic'},
        ];
      _useRealHttp();
      final server = await _serveMem(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.operatorToken = 'op-token';
        final rows = await c.memorySearch('tea', topK: 5);
        expect(stub.methods.single, equals('GET'));
        expect(stub.paths.single, equals('/v1/memory/search'));
        expect(stub.queries.single,
            equals({'q': 'tea', 'top_k': '5'}));
        expect(stub.authHeaders.single, equals('Bearer op-token'));
        final hits = parseMemorySearchResults(rows);
        expect(hits.length, equals(2));
        expect(hits[0].text, equals('likes tea'));
        expect(hits[0].category, equals('preferences'));
        expect(hits[0].score, closeTo(0.9, 1e-9));
        expect(hits[1].id, isEmpty); // Core omits ids: explicit, never guessed
      } finally {
        await server.close(force: true);
      }
    });

    test('forget DELETEs the id path; 404 is removed-elsewhere', () async {
      final stub = _MemStub()
        ..status = 404
        ..reply = {'detail': 'unknown memory'};
      _useRealHttp();
      final server = await _serveMem(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.operatorToken = 'op-token';
        Object? err;
        try {
          await c.memoryForget('mem-1');
        } catch (e) {
          err = e;
        }
        expect(err, isNotNull);
        expect(stub.methods.single, equals('DELETE'));
        expect(stub.paths.single, equals('/v1/memory/mem-1'));
        expect(isMemoryNotFound(err ?? 'no error'), isTrue);
        expect(describeMemoryError(err ?? 'no error'), contains('removed elsewhere'));
      } finally {
        await server.close(force: true);
      }
    });

    test('confirm POSTs candidate_id+ok; purge POSTs {} -> purged',
        () async {
      final stub = _MemStub()..reply = {'stored': true, 'id': 'm-1'};
      _useRealHttp();
      final server = await _serveMem(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.operatorToken = 'op-token';
        final out = await c.memoryConfirmCandidate('cand-1', ok: true);
        expect(stub.methods.single, equals('POST'));
        expect(stub.paths.single,
            equals('/v1/memory/candidates/confirm'));
        expect(stub.bodies.single,
            equals({'candidate_id': 'cand-1', 'ok': true}));
        expect(out['stored'], isTrue);
      } finally {
        await server.close(force: true);
      }
    });

    test('purge returns purged count', () async {
      final stub = _MemStub()..reply = {'purged': 2};
      _useRealHttp();
      final server = await _serveMem(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.operatorToken = 'op-token';
        final out = await c.memoryPurge();
        expect(stub.paths.single, equals('/v1/memory/purge'));
        expect(out['purged'], equals(2));
      } finally {
        await server.close(force: true);
      }
    });

    test('candidates GETs the list path with operator Bearer', () async {
      final stub = _MemStub()
        ..reply = [
          {
            'candidate_id': 'cand-1',
            'text': '[redacted]',
            'category': 'preferences',
            'source': 'consider',
          },
        ];
      _useRealHttp();
      final server = await _serveMem(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.operatorToken = 'op-token';
        final rows = await c.memoryCandidates();
        expect(stub.methods.single, equals('GET'));
        expect(stub.paths.single, equals('/v1/memory/candidates'));
        expect(stub.authHeaders.single, equals('Bearer op-token'));
        final parsed = parseMemoryCandidates(rows);
        expect(parsed.single.candidateId, equals('cand-1'));
        expect(parsed.single.text, equals('[redacted]'));
        expect(parsed.single.category, equals('preferences'));
        expect(parsed.single.source, equals('consider'));
      } finally {
        await server.close(force: true);
      }
    });

    test('parseMemoryCandidates tolerates missing keys + non-lists', () {
      expect(parseMemoryCandidates('nope'), isEmpty);
      expect(parseMemoryCandidates(null), isEmpty);
      final rows = parseMemoryCandidates([
        {'candidate_id': 'cand-9'},
        {'text': 'x', 'category': 'c', 'source': 's'},
      ]);
      expect(rows.length, equals(2));
      expect(rows[0].candidateId, equals('cand-9'));
      expect(rows[0].text, isEmpty);
      expect(rows[1].text, equals('x'));
    });

    test('422 classifies as secret-refusal; 401 as token problem',
        () async {
      final stub = _MemStub()
        ..status = 422
        ..reply = {'detail': 'secret material'};
      _useRealHttp();
      final server = await _serveMem(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.operatorToken = 'op-token';
        Object? err;
        try {
          await c.memorySearch('password hunter2');
        } catch (e) {
          err = e;
        }
        expect(err, isNotNull);
        expect(isMemorySecretRejected(err ?? 'no error'), isTrue);
        expect(describeMemoryError(err ?? 'no error'), contains('never stored'));
      } finally {
        await server.close(force: true);
      }
      expect(describeMemoryError(const CoreException(401, 'x')),
          contains('operator token'));
    });
  });

  group('memory panel', () {
    testWidgets('search renders text/category/score + per-row Forget',
        (tester) async {
      final backend = _FakeMemory()
        ..hits = const [
          MemoryHit(id: '', text: 'likes tea', category: 'preferences',
              score: 0.9),
        ];
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: MemoryReviewPanel(
            client: CoreClient('http://127.0.0.1:1'),
            tokenCtrl: TextEditingController(),
            backend: backend,
          ),
        ),
      ));
      await tester.enterText(find.byType(TextField).first, 'tea');
      await tester.tap(find.text('Search'));
      await tester.pumpAndSettle();
      expect(find.text('likes tea'), findsOneWidget);
      expect(find.textContaining('preferences'), findsOneWidget);
      expect(find.textContaining('0.900'), findsOneWidget);
      // Row without id: Forget explains instead of sending a guessed DELETE.
      await tester.tap(find.text('Forget').first);
      await tester.pumpAndSettle();
      expect(backend.forgets, isEmpty);
      expect(find.textContaining('carry no id'), findsOneWidget);
    });

    testWidgets('forget 404 shows removed-elsewhere, not a retry',
        (tester) async {
      final backend = _FakeMemory()
        ..hits = const [
          MemoryHit(id: 'mem-9', text: 'old', category: 'episodic'),
        ]
        ..forgetError = const CoreException(404, 'DELETE -> 404 unknown');
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: MemoryReviewPanel(
            client: CoreClient('http://127.0.0.1:1'),
            tokenCtrl: TextEditingController(),
            backend: backend,
          ),
        ),
      ));
      await tester.enterText(find.byType(TextField).first, 'old');
      await tester.tap(find.text('Search'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Forget').first);
      await tester.pumpAndSettle();
      expect(backend.forgets, equals(['mem-9']));
      expect(find.textContaining('removed elsewhere'), findsWidgets);
    });

    testWidgets('candidates load only on manual Refresh, never on open',
        (tester) async {
      final backend = _FakeMemory()
        ..pending = const [
          MemoryCandidate(
              candidateId: 'cand-1',
              text: '[redacted]',
              category: 'preferences',
              source: 'consider'),
        ];
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: MemoryReviewPanel(
            client: CoreClient('http://127.0.0.1:1'),
            tokenCtrl: TextEditingController(),
            backend: backend,
          ),
        ),
      ));
      await tester.pumpAndSettle();
      expect(backend.candidateFetches, equals(0));
      expect(find.textContaining('not loaded'), findsOneWidget);
      expect(find.text('[redacted]'), findsNothing);
      await tester.tap(find.text('Refresh candidates'));
      await tester.pumpAndSettle();
      expect(backend.candidateFetches, equals(1));
      expect(find.text('[redacted]'), findsOneWidget);
      expect(find.textContaining('preferences'), findsOneWidget);
      expect(find.textContaining('cand-1'), findsOneWidget);
    });

    testWidgets('empty candidates render the explicit empty state',
        (tester) async {
      final backend = _FakeMemory()..pending = const [];
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: MemoryReviewPanel(
            client: CoreClient('http://127.0.0.1:1'),
            tokenCtrl: TextEditingController(),
            backend: backend,
          ),
        ),
      ));
      await tester.tap(find.text('Refresh candidates'));
      await tester.pumpAndSettle();
      expect(find.textContaining('no pending candidates'), findsWidgets);
    });

    testWidgets('Accept confirms then refreshes the list', (tester) async {
      final backend = _FakeMemory()
        ..pending = const [
          MemoryCandidate(
              candidateId: 'cand-1', text: 't1', category: 'c'),
        ];
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: MemoryReviewPanel(
            client: CoreClient('http://127.0.0.1:1'),
            tokenCtrl: TextEditingController(),
            backend: backend,
          ),
        ),
      ));
      await tester.tap(find.text('Refresh candidates'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Accept').first);
      await tester.pumpAndSettle();
      expect(
          backend.confirms.single, equals({'id': 'cand-1', 'ok': true}));
      // Decision triggers a re-fetch so the list is current.
      expect(backend.candidateFetches, equals(2));
      expect(find.textContaining('confirmed'), findsOneWidget);
    });

    testWidgets('Reject confirms with ok=false then refreshes',
        (tester) async {
      final backend = _FakeMemory()
        ..pending = const [
          MemoryCandidate(
              candidateId: 'cand-7', text: 't7', category: 'c'),
        ];
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: MemoryReviewPanel(
            client: CoreClient('http://127.0.0.1:1'),
            tokenCtrl: TextEditingController(),
            backend: backend,
          ),
        ),
      ));
      await tester.tap(find.text('Refresh candidates'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Reject').first);
      await tester.pumpAndSettle();
      expect(
          backend.confirms.single, equals({'id': 'cand-7', 'ok': false}));
      expect(backend.candidateFetches, equals(2));
      expect(find.textContaining('rejected'), findsOneWidget);
    });

    testWidgets('confirm 404 renders the gone state, then refreshes',
        (tester) async {
      final backend = _FakeMemory()
        ..pending = const [
          MemoryCandidate(
              candidateId: 'cand-gone', text: 'tg', category: 'c'),
        ]
        ..confirmError =
            const CoreException(404, 'POST x -> 404 unknown candidate');
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: MemoryReviewPanel(
            client: CoreClient('http://127.0.0.1:1'),
            tokenCtrl: TextEditingController(),
            backend: backend,
          ),
        ),
      ));
      await tester.tap(find.text('Refresh candidates'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Accept').first);
      await tester.pumpAndSettle();
      expect(find.textContaining('gone (removed elsewhere)'), findsWidgets);
      expect(backend.candidateFetches, equals(2));
    });
  });
}

class _FakeMemory implements MemoryBackend {
  List<MemoryHit> hits = const [];
  final List<String> forgets = [];
  final List<Map<String, dynamic>> confirms = [];
  Object? forgetError;
  Object? confirmError;
  List<MemoryCandidate> pending = const [];
  int candidateFetches = 0;
  int purges = 0;

  @override
  Future<List<MemoryHit>> search(String q, {int topK = 5}) async => hits;

  @override
  Future<Map<String, dynamic>> forget(String id) async {
    forgets.add(id);
    final e = forgetError;
    if (e != null) throw e;
    return {'forgotten': id};
  }

  @override
  Future<List<MemoryCandidate>> candidates() async {
    candidateFetches += 1;
    return pending;
  }

  @override
  Future<Map<String, dynamic>> confirm(String candidateId, bool ok) async {
    confirms.add({'id': candidateId, 'ok': ok});
    final e = confirmError;
    if (e != null) throw e;
    return {'stored': ok};
  }

  @override
  Future<Map<String, dynamic>> purge() async {
    purges += 1;
    return {'purged': 0};
  }
}
