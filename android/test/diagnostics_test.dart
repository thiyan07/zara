// Diagnostics: ready parsing (no secrets), secret redaction, and the
// panel (fake backend, socket-free) — token/key strings never appear.
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';
import 'package:zara_android/diagnostics_panel.dart';

Future<HttpServer> _serveReady(
    Map<String, dynamic> reply, void Function(HttpRequest) onReq) async {
  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
  server.listen((req) async {
    try {
      onReq(req);
      req.response.statusCode = 200;
      req.response.headers.contentType = ContentType.json;
      req.response.write(jsonEncode(reply));
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
  group('ready parsing', () {
    test('parses provider string + checks + governor flags', () {
      final r = ReadyInfo.fromJson({
        'ready': true,
        'checks': {
          'registry': true,
          'memory_write': true,
          'event_bus': true,
          'provider': 'mock',
        },
        'governor_v2': true,
        'governor_shadow': false,
      });
      expect(r.ready, isTrue);
      expect(r.provider, equals('mock'));
      expect(r.registry, isTrue);
      expect(r.memoryWrite, isTrue);
      expect(r.eventBus, isTrue);
      expect(r.governorV2, isTrue);
      expect(r.governorShadow, isFalse);
    });

    test('missing keys read as unknown, never throw', () {
      final r = ReadyInfo.fromJson({});
      expect(r.provider, equals('?'));
      expect(r.ready, isFalse);
      expect(r.governorV2, isFalse);
      expect(ReadyInfo.fromJson({'checks': 'nope'}).provider, equals('?'));
    });

    test('redacts Bearer tokens, sk- keys, key assignments', () {
      expect(
          redactSecretsForDisplay('submit 401 Bearer hunter2 end'),
          equals('submit 401 Bearer [redacted] end'));
      expect(redactSecretsForDisplay('saw sk-abc123XYZ'), isNot(contains('sk-abc123XYZ')));
      expect(
          redactSecretsForDisplay('x-device-key: hunter2!'), isNot(contains('hunter2')));
      expect(redactSecretsForDisplay('api_key=supersecret'),
          isNot(contains('supersecret')));
      // Ordinary display text passes through untouched.
      expect(redactSecretsForDisplay('provider mock, v2=true'),
          equals('provider mock, v2=true'));
    });

    test('spoken transcripts keep words, lose secret shapes', () {
      // Voice-turn log lines reuse this scrubber: ordinary speech passes
      // through (debuggability kept) while assignment-shaped secrets do
      // not land verbatim in logcat.
      expect(redactSecretsForDisplay('password=hunter2 spoken aloud'),
          isNot(contains('hunter2')));
      expect(redactSecretsForDisplay('what is my battery level'),
          equals('what is my battery level'));
    });

    test('ready error describes refusal vs unreachable', () {
      expect(describeReadyError(const CoreException(403, 'x')),
          contains('unauthorized'));
      expect(describeReadyError(const CoreException(-1, 'Socket')),
          contains('unreachable'));
    });
  });

  group('ready client', () {
    test('GET /v1/ready sends no auth headers (public, never secrets)',
        () async {
      String? auth;
      String? deviceId;
      String? deviceKey;
      String path = '';
      _useRealHttp();
      final server = await _serveReady({
        'ready': true,
        'checks': {'provider': 'mock'},
        'governor_v2': false,
        'governor_shadow': false,
      }, (req) {
        path = req.uri.path;
        auth = req.headers.value('authorization');
        deviceId = req.headers.value('x-device-id');
        deviceKey = req.headers.value('x-device-key');
      });
      try {
        final c = CoreClient('http://127.0.0.1:${server.port}');
        c.deviceId = 'android-phone';
        c.deviceKey = 'should-not-be-sent';
        c.operatorToken = 'should-not-be-sent-either';
        final out = await c.ready();
        expect(path, equals('/v1/ready'));
        expect(auth, isNull);
        expect(deviceId, isNull);
        expect(deviceKey, isNull);
        expect(ReadyInfo.fromJson(out).provider, equals('mock'));
      } finally {
        await server.close(force: true);
      }
    });
  });

  group('diagnostics panel', () {
    testWidgets('shows connection + provider + governor flags on refresh',
        (tester) async {
      final backend = _FakeReady(const ReadyInfo(
        ready: true,
        provider: 'mock',
        registry: true,
        memoryWrite: true,
        eventBus: true,
        governorV2: true,
        governorShadow: false,
      ));
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: DiagnosticsPanel(
            client: CoreClient('http://127.0.0.1:1'),
            connection: 'online',
            lastError: '',
            backend: backend,
          ),
        ),
      ));
      expect(find.textContaining('unknown'), findsOneWidget);
      await tester.tap(find.text('Check readiness'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Core connection: online'), findsOneWidget);
      expect(find.textContaining('Provider: mock'), findsOneWidget);
      expect(find.textContaining('v2=true'), findsOneWidget);
      expect(find.textContaining('shadow=false'), findsOneWidget);
    });

    testWidgets('secret strings never appear in widgets', (tester) async {
      const secretToken = 'hunter2-secret-token';
      const secretKey = 'sk-live-abcdef123456';
      final backend = _FakeReady(const ReadyInfo(provider: 'mock'));
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: DiagnosticsPanel(
            client: CoreClient('http://127.0.0.1:1'),
            connection: 'error',
            lastError: 'submit 401 Bearer $secretToken, saw $secretKey',
            backend: backend,
          ),
        ),
      ));
      await tester.tap(find.text('Check readiness'));
      await tester.pumpAndSettle();
      expect(find.textContaining(secretToken), findsNothing);
      expect(find.textContaining(secretKey), findsNothing);
      expect(find.textContaining('abcdef123456'), findsNothing);
      // Redacted marker proves the error line still renders explicitly.
      expect(find.textContaining('[redacted]'), findsWidgets);
      // Panel never carries credential inputs at all.
      expect(find.byType(TextField), findsNothing);
    });
  });
}

class _FakeReady implements ReadyBackend {
  final ReadyInfo info;
  const _FakeReady(this.info);
  @override
  Future<ReadyInfo> fetch() async => info;
}
