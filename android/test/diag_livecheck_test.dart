// Live provider check: wire shapes (operator Bearer, exact paths) against
// a loopback stub + pure result/classify/describe helpers + the panel
// section (fake backend, socket-free): explicit Check only (never
// auto-run, never auto-retry), Cancel wiring, concurrent-tap guard,
// per-status messages, and no-secret rendering.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';
import 'package:zara_android/diagnostics_panel.dart';

/// Scripted loopback stub for the operator diagnostics contracts.
class _DiagStub {
  final List<String> methods = [];
  final List<String> paths = [];
  final List<String> authHeaders = [];
  final List<Map<String, dynamic>> bodies = [];
  int status = 200;
  dynamic reply = const {'ok': true};
}

Future<HttpServer> _serveDiag(_DiagStub stub) async {
  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
  server.listen((req) async {
    try {
      stub.methods.add(req.method);
      stub.paths.add(req.uri.path);
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

Widget _panel(_FakeLive live, {Key? key}) => MaterialApp(
      home: Scaffold(
        body: DiagnosticsPanel(
          key: key,
          client: CoreClient('http://127.0.0.1:1'),
          connection: 'online',
          lastError: '',
          backend: _FakeReady(),
          liveBackend: live,
        ),
      ),
    );

void main() {
  setUp(_useRealHttp);
  group('live-check wire shapes', () {
    test('check POSTs the check path with operator Bearer', () async {
      final stub = _DiagStub()
        ..reply = {
          'ok': true,
          'provider': 'acme',
          'model': 'acme-large',
          'elapsed_s': 1.25,
        };
      _useRealHttp();
      final server = await _serveDiag(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.operatorToken = 'op-token';
        final out = await c.providerCheck();
        expect(stub.methods.single, equals('POST'));
        expect(stub.paths.single,
            equals('/v1/diagnostics/provider-check'));
        expect(stub.authHeaders.single, equals('Bearer op-token'));
        expect(stub.bodies.single, equals({}));
        final r = LiveCheckResult.fromJson(out);
        expect(r.provider, equals('acme'));
        expect(r.model, equals('acme-large'));
        expect(r.elapsedS, closeTo(1.25, 1e-9));
      } finally {
        await server.close(force: true);
      }
    });

    test('cancel POSTs the cancel path -> cancelled', () async {
      final stub = _DiagStub()..reply = {'cancelled': true};
      _useRealHttp();
      final server = await _serveDiag(stub);
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.operatorToken = 'op-token';
        final out = await c.cancelProviderCheck();
        expect(stub.methods.single, equals('POST'));
        expect(stub.paths.single,
            equals('/v1/diagnostics/provider-check/cancel'));
        expect(stub.authHeaders.single, equals('Bearer op-token'));
        expect(out['cancelled'], isTrue);
      } finally {
        await server.close(force: true);
      }
    });

    test('409/422/504/502 surface with status in the message', () async {
      for (final status in [409, 422, 504, 502]) {
        final stub = _DiagStub()
          ..status = status
          ..reply = {'detail': 'safe detail'};
        _useRealHttp();
        final server = await _serveDiag(stub);
        try {
          final c = CoreClient('http://127.0.0.1:${server.port}');
          c.operatorToken = 'op-token';
          Object? err;
          try {
            await c.providerCheck();
          } catch (e) {
            err = e;
          }
          expect(err, isNotNull, reason: 'status $status');
          expect(err.toString(), contains('$status'));
          expect(classifyLiveCheckError(err ?? 'no error'),
              isNot(LiveCheckState.error));
        } finally {
          await server.close(force: true);
        }
      }
    });
  });

  group('live-check pure helpers', () {
    test('result parses provider/model/elapsed; missing keys are unknown',
        () {
      final r = LiveCheckResult.fromJson({
        'ok': true,
        'provider': 'acme',
        'model': 'acme-large',
        'elapsed_s': 2.5,
      });
      expect(r.provider, equals('acme'));
      expect(r.model, equals('acme-large'));
      expect(r.elapsedS, closeTo(2.5, 1e-9));
      final empty = LiveCheckResult.fromJson({});
      expect(empty.provider, equals('?'));
      expect(empty.model, equals('?'));
      expect(empty.elapsedS, isNull);
      expect(LiveCheckResult.fromJson({'elapsed_s': 'n/a'}).elapsedS,
          isNull);
    });

    test('409/422/504/502 map to distinct states with distinct messages',
        () {
      final e409 = const CoreException(409, 'POST x -> 409: busy');
      final e422 = const CoreException(422, 'POST x -> 422: not live');
      final e504 = const CoreException(504, 'POST x -> 504: timeout');
      final e502 =
          const CoreException(502, 'POST x -> 502: provider broke');
      expect(classifyLiveCheckError(e409), equals(LiveCheckState.busy));
      expect(
          classifyLiveCheckError(e422), equals(LiveCheckState.notLive));
      expect(
          classifyLiveCheckError(e504), equals(LiveCheckState.timeout));
      expect(classifyLiveCheckError(e502),
          equals(LiveCheckState.providerError));
      final messages = {
        describeLiveCheckError(e409),
        describeLiveCheckError(e422),
        describeLiveCheckError(e504),
        describeLiveCheckError(e502),
      };
      expect(messages.length, equals(4));
      expect(describeLiveCheckError(e409), contains('already running'));
      expect(describeLiveCheckError(e422), contains('not a live provider'));
      expect(describeLiveCheckError(e504), contains('timed out'));
      expect(describeLiveCheckError(e502), contains('provider error'));
    });

    test('401 maps to the token message; unknown maps to core error', () {
      expect(describeLiveCheckError(const CoreException(401, 'x')),
          contains('operator token'));
      expect(describeLiveCheckError(const CoreException(-1, 'socket')),
          equals('core error'));
    });
  });

  group('live-check panel', () {
    testWidgets('never auto-runs: no call on open', (tester) async {
      final live = _FakeLive();
      await tester.pumpWidget(_panel(live));
      await tester.pumpAndSettle();
      expect(live.checks, equals(0));
      expect(find.textContaining('not checked'), findsOneWidget);
      expect(find.text('Check live provider'), findsOneWidget);
    });

    testWidgets('success renders provider/model/elapsed', (tester) async {
      final live = _FakeLive()
        ..result = const LiveCheckResult(
            provider: 'acme', model: 'acme-large', elapsedS: 1.5);
      await tester.pumpWidget(_panel(live));
      await tester.tap(find.text('Check live provider'));
      await tester.pumpAndSettle();
      expect(live.checks, equals(1));
      expect(find.textContaining('Live provider: acme'), findsOneWidget);
      expect(find.textContaining('acme-large'), findsOneWidget);
      expect(find.textContaining('1.5s'), findsOneWidget);
      // Terminal state offers an explicit re-check, never auto-retry.
      expect(find.text('Retry check'), findsOneWidget);
    });

    testWidgets('busy-409 shows explicit state; Retry is manual',
        (tester) async {
      final live = _FakeLive()
        ..error = const CoreException(409, 'POST x -> 409: busy');
      await tester.pumpWidget(_panel(live));
      await tester.tap(find.text('Check live provider'));
      await tester.pumpAndSettle();
      expect(find.textContaining('already running'), findsOneWidget);
      expect(live.checks, equals(1));
      // Settling never retries on its own.
      await tester.pump(const Duration(seconds: 5));
      expect(live.checks, equals(1));
      // Manual Retry issues exactly one more call.
      await tester.tap(find.text('Retry check'));
      await tester.pumpAndSettle();
      expect(live.checks, equals(2));
    });

    testWidgets('not-live/timeout/provider-error each render distinctly',
        (tester) async {
      final cases = {
        const CoreException(422, 'POST x -> 422: nope'):
            'not a live provider',
        const CoreException(504, 'POST x -> 504: slow'):
            'timed out',
        const CoreException(502, 'POST x -> 502: broke'):
            'provider error',
      };
      for (final entry in cases.entries) {
        final live = _FakeLive()..error = entry.key;
        await tester.pumpWidget(_panel(live, key: UniqueKey()));
        await tester.pumpAndSettle();
        await tester.tap(find.text('Check live provider'));
        await tester.pumpAndSettle();
        expect(find.textContaining(entry.value), findsOneWidget,
            reason: 'for ${entry.key}');
      }
    });

    testWidgets('checking shows Cancel; Cancel wires the endpoint',
        (tester) async {
      final live = _FakeLive()..gate = Completer<LiveCheckResult>();
      await tester.pumpWidget(_panel(live));
      await tester.tap(find.text('Check live provider'));
      await tester.pump();
      expect(find.textContaining('checking live provider'), findsOneWidget);
      expect(find.text('Cancel check'), findsOneWidget);
      await tester.tap(find.text('Cancel check'));
      await tester.pump();
      expect(live.cancels, equals(1));
      expect(find.textContaining('cancellation requested'), findsOneWidget);
      // The in-flight check still settles on its own afterwards.
      live.gate!.complete(const LiveCheckResult(
          provider: 'acme', model: 'm', elapsedS: 0.5));
      await tester.pumpAndSettle();
      expect(find.textContaining('Live provider: acme'), findsOneWidget);
    });

    testWidgets('concurrent second tap is dropped with notice',
        (tester) async {
      final live = _FakeLive()..gate = Completer<LiveCheckResult>();
      await tester.pumpWidget(_panel(live));
      await tester.tap(find.text('Check live provider'));
      await tester.pump();
      await tester.tap(find.text('Check live provider'));
      await tester.pump();
      expect(live.checks, equals(1));
      expect(find.textContaining('second tap dropped'), findsOneWidget);
      live.gate!.complete(const LiveCheckResult(provider: 'p'));
      await tester.pumpAndSettle();
    });

    testWidgets('never renders bodies, URLs, keys, or tokens',
        (tester) async {
      const secret = 'hunter2-secret-token';
      const key = 'sk-live-abcdef123456';
      final live = _FakeLive()
        ..error = CoreException(502,
            'POST http://secret-host/x -> 502: Bearer $secret saw $key');
      await tester.pumpWidget(_panel(live));
      await tester.tap(find.text('Check live provider'));
      await tester.pumpAndSettle();
      expect(find.textContaining(secret), findsNothing);
      expect(find.textContaining(key), findsNothing);
      expect(find.textContaining('abcdef123456'), findsNothing);
      expect(find.textContaining('secret-host'), findsNothing);
      expect(find.textContaining('provider error'), findsOneWidget);
    });

    testWidgets('secret-looking provider/model strings are redacted',
        (tester) async {
      final live = _FakeLive()
        ..result = const LiveCheckResult(
            provider: 'sk-live-abcdef123456', model: 'm');
      await tester.pumpWidget(_panel(live));
      await tester.tap(find.text('Check live provider'));
      await tester.pumpAndSettle();
      expect(find.textContaining('abcdef123456'), findsNothing);
      expect(find.textContaining('[redacted]'), findsWidgets);
    });
  });
}

class _FakeReady implements ReadyBackend {
  @override
  Future<ReadyInfo> fetch() async => const ReadyInfo();
}

class _FakeLive implements LiveCheckBackend {
  int checks = 0;
  int cancels = 0;
  Object? error;
  LiveCheckResult result = const LiveCheckResult(provider: 'p');
  Completer<LiveCheckResult>? gate;

  @override
  Future<LiveCheckResult> check() async {
    checks += 1;
    final e = error;
    if (e != null) throw e;
    final g = gate;
    if (g != null) return g.future;
    return result;
  }

  @override
  Future<bool> cancel() async {
    cancels += 1;
    return true;
  }
}
