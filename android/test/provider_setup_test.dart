// Provider setup panel: ready parsing, exact env names, no-secret-input
// hard rules, and connection-check wiring via loopback.
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';
import 'package:zara_android/diagnostics_panel.dart';
import 'package:zara_android/provider_setup.dart';

Future<HttpServer> _serveReady(Map<String, dynamic> reply) async {
  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
  server.listen((req) async {
    try {
      req.response.statusCode = 200;
      req.response.headers.contentType = ContentType.json;
      req.response.write(jsonEncode(reply));
      await req.response.close();
    } catch (_) {}
  });
  return server;
}

void _useRealHttp() {
  // flutter_test hijacks HttpClient once the widgets binding initializes;
  // loopback tests need the real stack.
  HttpOverrides.global = null;
}

void main() {
  setUp(_useRealHttp);

  group('provider ready parsing', () {
    test('echo provider parses + unverified note state', () {
      final r = ReadyInfo.fromJson({
        'ready': true,
        'checks': {'provider': 'echo'},
      });
      expect(r.provider, equals('echo'));
      expect(isBuiltInProvider(r.provider), isTrue);
      expect(describeProviderKind(r.provider),
          equals('echo (built-in, no key)'));
      expect(providerSetupState(r.provider),
          equals('unconfigured for live inference'));
    });

    test('scripted provider parses + unverified note state', () {
      final r = ReadyInfo.fromJson({
        'ready': true,
        'checks': {'provider': 'scripted'},
      });
      expect(r.provider, equals('scripted'));
      expect(isBuiltInProvider(r.provider), isTrue);
      expect(describeProviderKind(r.provider),
          equals('scripted (built-in, no key)'));
      expect(providerSetupState(r.provider),
          equals('unconfigured for live inference'));
    });

    test('openai-compatible parses as configured', () {
      final r = ReadyInfo.fromJson({
        'ready': true,
        'checks': {'provider': 'openai-compatible'},
      });
      expect(r.provider, equals('openai-compatible'));
      expect(isBuiltInProvider(r.provider), isFalse);
      expect(describeProviderKind(r.provider),
          equals('openai-compatible'));
      expect(providerSetupState(r.provider), equals('configured'));
    });

    test('unknown provider reads as unknown state', () {
      expect(providerSetupState('?'), equals('unknown'));
      expect(describeProviderCheckError(const CoreException(-1, 'Socket')),
          contains('unreachable'));
    });
  });

  group('provider setup text', () {
    testWidgets('explains exact env names + local setup steps',
        (tester) async {
      _useRealHttp();
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: ProviderSetupPanel(
            client: CoreClient('http://127.0.0.1:1'),
            backend: _FakeReady(const ReadyInfo(provider: 'echo')),
          ),
        ),
      ));
      // Exact existing Core env names must appear verbatim.
      expect(find.textContaining('LLM_PROVIDER'), findsWidgets);
      expect(find.textContaining('LLM_BASE_URL'), findsWidgets);
      expect(find.textContaining('LLM_MODEL'), findsWidgets);
      expect(find.textContaining('LLM_API_KEY'), findsWidgets);
      // Provider values + path note + local examples.
      expect(find.textContaining('echo'), findsWidgets);
      expect(find.textContaining('scripted'), findsWidgets);
      expect(find.textContaining('openai-compatible'), findsWidgets);
      expect(find.textContaining('/chat/completions'), findsWidgets);
      expect(find.textContaining('Ollama'), findsWidgets);
      expect(find.textContaining('LMStudio'), findsWidgets);
      // Operator flow: env export + Core restart documented.
      expect(find.textContaining('export'), findsWidgets);
      expect(find.textContaining('Core restart by operator'), findsWidgets);
    });

    testWidgets('built-in provider shows unverified note; live does not',
        (tester) async {
      _useRealHttp();
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: ProviderSetupPanel(
            client: CoreClient('http://127.0.0.1:1'),
            backend: _FakeReady(const ReadyInfo(provider: 'echo')),
          ),
        ),
      ));
      await tester.tap(find.text('Check provider connection'));
      await tester.pumpAndSettle();
      expect(
          find.textContaining('live inference unverified without credentials'),
          findsOneWidget);
      expect(find.textContaining('echo (built-in, no key)'), findsWidgets);

      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: ProviderSetupPanel(
            client: CoreClient('http://127.0.0.1:1'),
            backend:
                _FakeReady(const ReadyInfo(provider: 'openai-compatible')),
          ),
        ),
      ));
      await tester.tap(find.text('Check provider connection'));
      await tester.pumpAndSettle();
      expect(
          find.textContaining('live inference unverified without credentials'),
          findsNothing);
      expect(find.textContaining('configured'), findsWidgets);
    });
  });

  group('no-secret hard rules', () {
    testWidgets('panel has no credential inputs and renders no secrets',
        (tester) async {
      _useRealHttp();
      const secretKey = 'sk-live-abcdef123456';
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: ProviderSetupPanel(
            client: CoreClient('http://127.0.0.1:1'),
            backend:
                _FakeReady(const ReadyInfo(provider: 'openai-compatible')),
          ),
        ),
      ));
      await tester.tap(find.text('Check provider connection'));
      await tester.pumpAndSettle();
      // No credential input fields anywhere in this panel.
      expect(find.byType(TextField), findsNothing);
      expect(find.byType(TextFormField), findsNothing);
      expect(find.byType(EditableText), findsNothing);
      // No password/obscureText inputs either (vacuous: none exist).
      final obscured = tester
          .widgetList(find.byType(EditableText))
          .where((w) => (w as EditableText).obscureText);
      expect(obscured, isEmpty);
      // Key-like strings never render (env NAME is fine, values are not).
      expect(find.textContaining(secretKey), findsNothing);
      expect(find.textContaining('abcdef123456'), findsNothing);
      expect(find.textContaining('Bearer hunter2'), findsNothing);
      // Ready output shows provider without secret fields.
      expect(find.textContaining('Provider: openai-compatible'), findsWidgets);
    });
  });

  group('connection check wiring', () {
    // NOTE: widget tests run in FakeAsync, so real loopback I/O must run
    // inside tester.runAsync (real-async zone) — otherwise sockets never
    // complete. Fake backends stay socket-free (no runAsync needed).
    testWidgets('loopback ready reports reachable + provider name',
        (tester) async {
      _useRealHttp();
      late HttpServer server;
      await tester.runAsync(() async {
        server = await _serveReady({
          'ready': true,
          'checks': {'provider': 'scripted'},
          'governor_v2': false,
          'governor_shadow': false,
        });
      });
      try {
        await tester.pumpWidget(MaterialApp(
          home: Scaffold(
            body: ProviderSetupPanel(
              client: CoreClient('http://127.0.0.1:${server.port}'),
            ),
          ),
        ));
        expect(
            find.textContaining('unknown — check connection'), findsOneWidget);
        // The tap starts real socket I/O, so it must run inside the
        // real-async zone with a settle delay; pump after to render.
        await tester.runAsync(() async {
          await tester.tap(find.text('Check provider connection'));
          await Future<void>.delayed(const Duration(seconds: 2));
        });
        await tester.pump();
        expect(find.textContaining('reachable'), findsWidgets);
        expect(
            find.textContaining('scripted (built-in, no key)'), findsWidgets);
      } finally {
        await tester.runAsync(() async {
          await server.close(force: true);
        });
      }
    });

    testWidgets('refused loopback port reports unreachable', (tester) async {
      _useRealHttp();
      // Bind then close: the port is guaranteed refused (no timing guess).
      late int deadPort;
      await tester.runAsync(() async {
        final s = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
        deadPort = s.port;
        await s.close(force: true);
      });
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: ProviderSetupPanel(
            client: CoreClient('http://127.0.0.1:$deadPort'),
          ),
        ),
      ));
      await tester.runAsync(() async {
        await tester.tap(find.text('Check provider connection'));
        await Future<void>.delayed(const Duration(seconds: 2));
      });
      await tester.pump();
      expect(find.textContaining('unreachable'), findsWidgets);
    });

    test('loopback GET /v1/ready carries no auth (public, never secrets)',
        () async {
      _useRealHttp();
      String? auth;
      String? deviceId;
      String path = '';
      final raw =
          await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      raw.listen((req) async {
        try {
          path = req.uri.path;
          auth = req.headers.value('authorization');
          deviceId = req.headers.value('x-device-id');
          req.response.statusCode = 200;
          req.response.headers.contentType = ContentType.json;
          req.response.write(jsonEncode({
            'ready': true,
            'checks': {'provider': 'openai-compatible'},
          }));
          await req.response.close();
        } catch (_) {}
      });
      try {
        final c = CoreClient('http://127.0.0.1:${raw.port}');
        c.deviceId = 'android-phone';
        c.deviceKey = 'should-not-be-sent';
        c.operatorToken = 'should-not-be-sent-either';
        final out = await c.ready();
        expect(path, equals('/v1/ready'));
        expect(auth, isNull);
        expect(deviceId, isNull);
        expect(ReadyInfo.fromJson(out).provider,
            equals('openai-compatible'));
        expect(
            providerSetupState('openai-compatible'), equals('configured'));
      } finally {
        await raw.close(force: true);
      }
    });
  });
}

class _FakeReady implements ReadyBackend {
  final ReadyInfo info;
  const _FakeReady(this.info);
  @override
  Future<ReadyInfo> fetch() async => info;
}
