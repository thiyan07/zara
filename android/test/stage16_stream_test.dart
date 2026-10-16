// Stage 16: streaming talk (POST /v1/talk/stream SSE) + /v1/talk fallback.
//
// Covers the frozen event contract (core/talk_stream.py + core/app.py,
// read-only): framing/parse incl. split-chunk + split-multibyte +
// malformed (strict, never silent), done-dedup by turn_id, cancel wiring
// and fallback against a loopback HttpServer (no mocks of the client),
// plus the streaming panel (incremental render, tool rows, approval
// pointer, done state, auto-fallback).
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';
import 'package:zara_android/stream_client.dart';
import 'package:zara_android/talk_stream_view.dart';

String _frame(String name, Map<String, dynamic> payload) =>
    'event: $name\ndata: ${jsonEncode(payload)}\n\n';

List<String> _names(List<TalkStreamEvent> events) =>
    events.map((e) => e.name).toList();

/// Captured wire state for the loopback talk stub.
class _TalkStub {
  String streamMethod = '';
  String streamPath = '';
  final streamHeaders = <String, String>{};
  Map<String, dynamic> streamBody = {};
  int streamStatus = 200;
  List<String> earlyFrames = [];
  List<String> lateFrames = [];
  final turnSent = Completer<void>();
  final release = Completer<void>();
  String cancelBody = '';
  final cancelHeaders = <String, String>{};
  String talkMethod = '';
  Map<String, dynamic> talkBody = {};
  int talkStatus = 200;
  Map<String, dynamic> talkReply = {};
}

Future<HttpServer> _serveTalk(_TalkStub stub) async {
  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
  server.listen((req) async {
    try {
      if (req.uri.path == '/v1/talk/stream') {
        stub.streamMethod = req.method;
        stub.streamPath = req.uri.path;
        req.headers.forEach((k, v) => stub.streamHeaders[k] = v.join(','));
        final text = await utf8.decoder.bind(req).join();
        stub.streamBody = jsonDecode(text) as Map<String, dynamic>;
        if (stub.streamStatus != 200) {
          req.response.statusCode = stub.streamStatus;
          req.response.headers.contentType = ContentType.json;
          req.response.write(jsonEncode({'detail': 'boom'}));
          await req.response.close();
          return;
        }
        req.response.statusCode = 200;
        req.response.headers.contentType =
            ContentType('text', 'event-stream', charset: 'utf-8');
        // HttpResponse buffers body chunks until close() by default, which
        // would collapse the whole stream into one blob. Real SSE servers
        // (Core's StreamingResponse) push each frame; disable buffering so
        // the stub streams frame-by-frame like production.
        req.response.bufferOutput = false;
        for (final f in stub.earlyFrames) {
          req.response.write(f);
        }
        await req.response.flush();
        if (!stub.turnSent.isCompleted) stub.turnSent.complete();
        await stub.release.future.timeout(const Duration(seconds: 10));
        for (final f in stub.lateFrames) {
          req.response.write(f);
        }
        await req.response.flush();
        await req.response.close();
      } else if (req.uri.path == '/v1/talk/cancel') {
        final text = await utf8.decoder.bind(req).join();
        stub.cancelBody = text;
        req.headers.forEach((k, v) => stub.cancelHeaders[k] = v.join(','));
        req.response.statusCode = 200;
        req.response.headers.contentType = ContentType.json;
        req.response.write(jsonEncode({'cancelled': true}));
        await req.response.close();
      } else if (req.uri.path == '/v1/talk') {
        stub.talkMethod = req.method;
        final text = await utf8.decoder.bind(req).join();
        stub.talkBody = jsonDecode(text) as Map<String, dynamic>;
        req.response.statusCode = stub.talkStatus;
        req.response.headers.contentType = ContentType.json;
        req.response.write(jsonEncode(stub.talkReply));
        await req.response.close();
      } else {
        req.response.statusCode = 404;
        await req.response.close();
      }
    } catch (_) {
      try {
        await req.response.close();
      } catch (_) {}
    }
  });
  return server;
}

CoreClient _client(HttpServer s) {
  final c = CoreClient('http://127.0.0.1:${s.port}',
      timeout: const Duration(seconds: 5));
  c.deviceId = 'android-phone';
  c.deviceKey = 'k1';
  c.operatorToken = 'op-token';
  return c;
}

/// flutter_test installs a global mock HttpClient (empty 400 for every
/// request) once the widgets binding initializes. The wire tests below
/// need REAL loopback HTTP, so opt this file's isolate back out to the
/// default client before every test. Test-local only; other files run in
/// their own isolates and are unaffected.
void _useRealHttp() {
  HttpOverrides.global = null;
}

Future<void> _waitFor(bool Function() cond, [String what = 'condition']) async {
  for (var i = 0; i < 100; i++) {
    if (cond()) return;
    await Future.delayed(const Duration(milliseconds: 50));
  }
  throw StateError('timed out waiting for $what');
}

/// Socket-free fake backend for panel tests: the flutter_tester cannot hold
/// loopback servers open (a bound HttpServer wedges test completion), so
/// panel tests drive [TalkStreamHandle.fake] event streams instead. Wire
/// behavior stays covered by the loopback tests above.
class _FakeBackend extends TalkBackend {
  _FakeBackend() : super(CoreClient('http://127.0.0.1:1'));
  StreamController<TalkStreamEvent> ctrl =
      StreamController<TalkStreamEvent>.broadcast();
  Object? openError;
  Map<String, dynamic>? fallbackReply;
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
    // Fire-and-forget: awaiting controller teardown futures inside
    // testWidgets can stall (FakeAsync never releases them mid-test), and
    // the panel must show Cancelled immediately, not after teardown.
    unawaited(handle.close());
    return {'cancelled': true};
  }

  /// End the current stream without a `done` (disconnect drill) and swap
  /// in a fresh controller so a later resend gets its own event feed.
  /// Synchronous for the same FakeAsync reason as [cancelStream]: the done
  /// event still propagates on subsequent pumps.
  void endStream() {
    final current = ctrl;
    ctrl = StreamController<TalkStreamEvent>.broadcast();
    unawaited(current.close());
  }
}

TalkStreamEvent _ev(String name, Map<String, dynamic> payload) =>
    TalkStreamEvent(name, Map<String, dynamic>.from(payload));

/// Pump frames until [cond] holds (bounded). Stream teardown chains
/// (source done -> handle close -> panel onDone -> setState) span async
/// gaps that include zero-duration timers, which only fire when fake time
/// advances — so pump WITH a duration (a bare pump() can stall forever).
Future<void> _settle(WidgetTester t, bool Function() cond) async {
  for (var i = 0; i < 20; i++) {
    await t.pump(const Duration(milliseconds: 50));
    if (cond()) return;
  }
}

Future<void> _pumpPanel(WidgetTester t, _FakeBackend backend,
    {void Function(String, String)? onApproval}) {
  return t.pumpWidget(MaterialApp(
    home: Scaffold(
      body: TalkStreamPanel(
        client: CoreClient('http://127.0.0.1:1'),
        tokenCtrl: TextEditingController(),
        onApproval: onApproval ?? (id, reason) {},
        backend: backend,
      ),
    ),
  ));
}

void main() {
  setUp(_useRealHttp);
  group('SseParser framing', () {
    test('whole frames parse incl :ping keepalives + CRLF', () {
      final text = ': ping\n\n'
          'event: turn\r\ndata: {"turn_id":"t-1"}\r\n\r\n'
          ': ping\n\n'
          'event: delta\ndata: {"text":"Hi"}\n\n'
          'event: done\ndata: {"reply":"Hi","status":"ok","turn_id":"t-1","mission_id":"","execution_id":""}\n\n';
      final parser = SseParser();
      final events = parser.addBytes(utf8.encode(text));
      expect(_names(events), ['turn', 'delta', 'done']);
      expect(events[0].payload['turn_id'], 't-1');
      expect(events[1].payload['text'], 'Hi');
      expect(events[2].payload['reply'], 'Hi');
      expect(parser.flush(), isEmpty); // clean end: nothing left over
    });

    test('id:/retry: fields ignored per SSE tolerance', () {
      final parser = SseParser();
      final events = parser.addBytes(utf8.encode(
          'id: 7\nevent: delta\nretry: 100\ndata: {"text":"x"}\n\n'));
      expect(_names(events), ['delta']);
      expect(events.single.payload['text'], 'x');
    });

    test('split chunks reassemble identically', () {
      final full = utf8.encode(
          ': ping\n\n${_frame('turn', {'turn_id': 't-2'})}'
          '${_frame('delta', {'text': 'Hello '})}'
          '${_frame('tool', {
            'tool': 'system.battery',
            'phase': 'ok',
            'execution_id': 'ex-1',
            'device_id': 'android-phone',
            'message': 'done'
          })}'
          '${_frame('done', {
            'reply': 'Hello',
            'status': 'ok',
            'turn_id': 't-2',
            'mission_id': '',
            'execution_id': ''
          })}');
      final want = SseParser().addBytes(full);
      for (final split in [1, 7, full.length ~/ 2, full.length - 1]) {
        final p = SseParser();
        final got = <TalkStreamEvent>[
          ...p.addBytes(full.sublist(0, split)),
          ...p.addBytes(full.sublist(split)),
        ];
        expect(_names(got), _names(want), reason: 'split at $split');
        expect(
            got.map((e) => jsonEncode(e.payload)).toList(),
            want.map((e) => jsonEncode(e.payload)).toList(),
            reason: 'split at $split');
        expect(p.flush(), isEmpty, reason: 'split at $split');
      }
    });

    test('multibyte chars split across byte chunks decode cleanly', () {
      const text = 'naïve café 🌍✓ — zara';
      final frame = utf8.encode(_frame('delta', {'text': text}));
      for (var split = 0; split <= frame.length; split++) {
        final p = SseParser();
        final got = <TalkStreamEvent>[
          ...p.addBytes(frame.sublist(0, split)),
          ...p.addBytes(frame.sublist(split)),
        ];
        expect(got, hasLength(1), reason: 'split at $split');
        expect(got.single.payload['text'], text, reason: 'split at $split');
      }
    });

    test('malformed data lines throw, never silent', () {
      // Not JSON.
      expect(() => SseParser().addBytes(
          utf8.encode('event: delta\ndata: not-json\n\n')),
          throwsFormatException);
      // data without a preceding event:.
      expect(() => SseParser().addBytes(utf8.encode('data: {"a":1}\n\n')),
          throwsFormatException);
      // Unknown event name (contract is frozen).
      expect(
          () => SseParser()
              .addBytes(utf8.encode('event: bogus\ndata: {}\n\n')),
          throwsFormatException);
      // Non-object payload.
      expect(
          () => SseParser().addBytes(
              utf8.encode('event: delta\ndata: [1,2]\n\n')),
          throwsFormatException);
      expect(
          () => SseParser()
              .addBytes(utf8.encode('event: delta\ndata: null\n\n')),
          throwsFormatException);
      // event: with no data (blank dispatch).
      expect(() => SseParser().addBytes(utf8.encode('event: delta\n\n')),
          throwsFormatException);
      // Truncated event at stream end (disconnect evidence).
      final p = SseParser();
      expect(p.addBytes(utf8.encode('event: delta\ndata: {"text":"hi"}')),
          isEmpty);
      expect(p.flush, throwsFormatException);
      // Invalid UTF-8 bytes.
      expect(() => SseParser().addBytes([0xff, 0xfe]),
          throwsFormatException);
    });
  });

  group('TalkConversation reducer', () {
    TalkConversation seeded() {
      final c = TalkConversation();
      c.apply(const TalkStreamEvent('turn', {'turn_id': 't-1'}));
      c.apply(const TalkStreamEvent('delta', {'text': 'Hel'}));
      c.apply(const TalkStreamEvent('delta', {'text': 'lo'}));
      c.apply(const TalkStreamEvent('tool', {
        'tool': 'system.battery',
        'phase': 'started',
        'execution_id': 'ex-1',
        'device_id': 'd',
        'message': 'running'
      }));
      c.apply(const TalkStreamEvent('approval', {
        'execution_id': 'ex-9',
        'reason': 'needs a human'
      }));
      return c;
    }

    test('incremental deltas + tool + approval accumulate', () {
      final c = seeded();
      expect(c.turnId, 't-1');
      expect(c.reply, 'Hello');
      expect(c.tools.single.phase, 'started');
      expect(c.approvalExecutionId, 'ex-9');
      expect(c.approvalReason, 'needs a human');
      expect(c.isTerminal, isFalse);
    });

    test('done applies once per turn_id; late/duplicate done ignored', () {
      final c = seeded();
      c.apply(const TalkStreamEvent('done', {
        'reply': 'Hello',
        'status': 'ok',
        'turn_id': 't-1',
        'mission_id': 'm-1',
        'execution_id': 'ex-1'
      }));
      expect(c.isDone, isTrue);
      expect(c.doneCount, 1);
      expect(c.reply, 'Hello');
      expect(c.doneStatus, 'ok');
      // Exact duplicate: ignored.
      c.apply(const TalkStreamEvent('done', {
        'reply': 'OTHER',
        'status': 'ok',
        'turn_id': 't-1',
        'mission_id': '',
        'execution_id': ''
      }));
      expect(c.doneCount, 1);
      expect(c.reply, 'Hello');
      // Late done for another turn: ignored (first terminal done wins).
      c.apply(const TalkStreamEvent('done', {
        'reply': 'OTHER',
        'status': 'ok',
        'turn_id': 't-2',
        'mission_id': '',
        'execution_id': ''
      }));
      expect(c.doneCount, 1);
      expect(c.reply, 'Hello');
      // Post-done delta/tool/approval: ignored (no duplicate text).
      c.apply(const TalkStreamEvent('delta', {'text': '!!!'}));
      c.apply(const TalkStreamEvent('tool', {
        'tool': 'x',
        'phase': 'ok',
        'execution_id': '',
        'device_id': '',
        'message': ''
      }));
      expect(c.reply, 'Hello');
      expect(c.tools, hasLength(1));
    });

    test('disconnect is explicit and non-terminal; done wins over error', () {
      final c = seeded();
      c.markDisconnected('socket closed');
      expect(c.status, 'disconnected');
      expect(c.isTerminal, isFalse);
      expect(c.error, 'socket closed');
      c.markDisconnected(); // stays, never throws
      expect(c.status, 'disconnected');

      final d = seeded();
      d.apply(const TalkStreamEvent('done', {
        'reply': 'r',
        'status': 'ok',
        'turn_id': 't-1',
        'mission_id': '',
        'execution_id': ''
      }));
      d.markDisconnected('late');
      d.markError('late');
      d.markCancelled();
      expect(d.isDone, isTrue); // terminal done is sticky
      expect(d.reply, 'r');
    });
  });

  group('wire: stream open + events (loopback)', () {
    test('POST /v1/talk/stream shape, auth, ordered events, turnId',
        () async {
      final stub = _TalkStub()
        ..earlyFrames = [
          _frame('turn', {'turn_id': 't-1'}),
          ': ping\n\n',
          _frame('delta', {'text': 'Hello '}),
          _frame('delta', {'text': 'world'}),
          _frame('tool', {
            'tool': 'system.battery',
            'phase': 'ok',
            'execution_id': 'ex-1',
            'device_id': 'android-phone',
            'message': 'read ok'
          }),
          _frame('approval',
              {'execution_id': 'ex-9', 'reason': 'grant files?'}),
          _frame('done', {
            'reply': 'Hello world',
            'status': 'ok',
            'turn_id': 't-1',
            'mission_id': '',
            'execution_id': ''
          }),
        ]
        ..release.complete();
      final server = await _serveTalk(stub);
      try {
        final handle = await _client(server).startTalkStream(
          text: 'hi',
          deviceId: 'android-phone',
        );
        final events = await handle.events.toList();
        expect(
            events.map((e) => e.name).toList(),
            ['turn', 'delta', 'delta', 'tool', 'approval', 'done']);
        expect(handle.turnId, 't-1');
        expect(stub.streamMethod, 'POST');
        expect(stub.streamPath, '/v1/talk/stream');
        expect(stub.streamBody, {
          'text': 'hi',
          'session_id': '',
          'device_id': 'android-phone',
          'who': 'user',
        });
        expect(stub.streamHeaders['authorization'], 'Bearer op-token');
        expect(stub.streamHeaders['x-device-id'], 'android-phone');
        expect(stub.streamHeaders['x-device-key'], 'k1');
        expect(stub.streamHeaders['accept'], contains('text/event-stream'));
      } finally {
        await server.close(force: true);
      }
    });

    test('cancel wiring: close + POST /v1/talk/cancel {turn_id}', () async {
      final stub = _TalkStub()
        ..earlyFrames = [_frame('turn', {'turn_id': 't-9'})]
        ..lateFrames = [
          _frame('delta', {'text': 'late'}),
          _frame('done', {
            'reply': 'late',
            'status': 'ok',
            'turn_id': 't-9',
            'mission_id': '',
            'execution_id': ''
          }),
        ];
      final server = await _serveTalk(stub);
      try {
        final client = _client(server);
        final handle = await client.startTalkStream(
          text: 'hi',
          deviceId: 'android-phone',
        );
        final got = <TalkStreamEvent>[];
        final errs = <Object>[];
        final done = Completer<void>();
        final sub = handle.events.listen(
          got.add,
          onError: errs.add,
          onDone: () {
            if (!done.isCompleted) done.complete();
          },
        );
        await _waitFor(() => handle.turnId == 't-9', 'turn event');
        final out = await handle.cancel(client);
        expect(out, {'cancelled': true});
        await _waitFor(
            () => stub.cancelBody.isNotEmpty, 'cancel POST');
        expect(jsonDecode(stub.cancelBody), {'turn_id': 't-9'});
        expect(stub.cancelHeaders['authorization'], 'Bearer op-token');
        expect(got.map((e) => e.name), contains('turn'));
        expect(errs, isEmpty);
        stub.release.complete(); // let the stub handler finish
        await sub.cancel();
      } finally {
        await server.close(force: true);
      }
    });

    test('empty text fails fast (never sent)', () async {
      final stub = _TalkStub()..release.complete();
      final server = await _serveTalk(stub);
      try {
        final c = _client(server);
        expect(() => c.startTalkStream(text: ''),
            throwsArgumentError);
        expect(() => c.talk(text: ''), throwsArgumentError);
        expect(() => c.cancelTalk(''), throwsArgumentError);
        expect(stub.streamMethod, isEmpty);
      } finally {
        await server.close(force: true);
      }
    });
  });

  group('fallback path: non-streaming /v1/talk', () {
    test('talk() posts TalkIn envelope + auth, returns reply map', () async {
      final stub = _TalkStub()
        ..release.complete()
        ..talkReply = {
          'reply': 'hi back',
          'status': 'ok',
          'mission_id': '',
          'execution_id': '',
          'turn_id': 'ft-1',
        };
      final server = await _serveTalk(stub);
      try {
        final out = await _client(server)
            .talk(text: 'hi', deviceId: 'android-phone');
        expect(out['reply'], 'hi back');
        expect(out['turn_id'], 'ft-1');
        expect(stub.talkMethod, 'POST');
        expect(stub.talkBody, {
          'text': 'hi',
          'session_id': '',
          'device_id': 'android-phone',
          'who': 'user',
        });
      } finally {
        await server.close(force: true);
      }
    });

    test('HTTP errors are explicit typed errors (stream + fallback)',
        () async {
      final stub = _TalkStub()
        ..release.complete()
        ..streamStatus = 500;
      final server = await _serveTalk(stub);
      try {
        final c = _client(server);
        try {
          await c.startTalkStream(text: 'hi');
          fail('expected CoreException(500)');
        } on CoreException catch (e) {
          expect(e.status, 500);
          expect(describeTalkStreamError(e), isNotEmpty);
        }
        stub.talkStatus = 401;
        try {
          await c.talk(text: 'hi');
          fail('expected AuthException');
        } on CoreException catch (e) {
          expect(e, isA<AuthException>());
          expect(describeTalkStreamError(e), contains('unauthorized'));
        }
      } finally {
        await server.close(force: true);
      }
    });

    test('describeTalkStreamError states are distinct, never empty', () {
      expect(describeTalkStreamError(const CoreException(404, 'x')),
          contains('not found'));
      expect(describeTalkStreamError(const CoreException(500, 'x')),
          isNotEmpty);
      expect(
          describeTalkStreamError(
              const FormatException('malformed SSE data')),
          contains('parse error'));
      expect(describeTalkStreamError(const CoreException(409, 'x')),
          contains('conflict'));
      // Mapped transport failures render as actionable guidance.
      expect(
          describeTalkStreamError(const CoreException(
              -1, 'unreachable POST /v1/talk/stream: SocketException')),
          contains('unreachable'));
      expect(
          describeTalkStreamError(const CoreException(-1,
              'connection dropped STREAM /v1/talk/stream: HttpException')),
          contains('dropped'));
      expect(
          describeTalkStreamError(
              const CoreException(-1, 'timed out after 15s STREAM x')),
          contains('timed out'));
    });
  });

  group('TalkStreamPanel widget (socket-free fakes)', () {
    testWidgets('streams incrementally: tools, approval pointer, done',
        (t) async {
      final backend = _FakeBackend();
      String? approvedId;
      String? approvedReason;
      await _pumpPanel(t, backend, onApproval: (id, reason) {
        approvedId = id;
        approvedReason = reason;
      });
      await t.enterText(find.byType(TextField), 'hello');
      await t.tap(find.text('Send'));
      await t.pump();
      await t.pump();
      expect(backend.openCalls, 1);
      backend.ctrl.add(_ev('turn', {'turn_id': 't-5'}));
      await t.pump();
      expect(find.textContaining('turn: t-5'), findsOneWidget);
      backend.ctrl.add(_ev('delta', {'text': 'Hello '}));
      backend.ctrl.add(_ev('delta', {'text': 'world'}));
      await t.pump();
      expect(find.textContaining('Hello world'), findsOneWidget);
      backend.ctrl.add(_ev('tool', {
        'tool': 'system.battery',
        'phase': 'ok',
        'execution_id': 'ex-1',
        'device_id': 'android-phone',
        'message': 'read ok'
      }));
      await t.pump();
      expect(find.textContaining('system.battery · ok'), findsOneWidget);
      backend.ctrl.add(
          _ev('approval', {'execution_id': 'ex-9', 'reason': 'grant files?'}));
      await t.pump();
      expect(find.textContaining('Approval required'), findsOneWidget);
      expect(approvedId, 'ex-9');
      expect(approvedReason, 'grant files?');
      backend.ctrl.add(_ev('done', {
        'reply': 'Hello world',
        'status': 'ok',
        'turn_id': 't-5',
        'mission_id': '',
        'execution_id': ''
      }));
      await t.pump();
      await t.pump();
      expect(find.textContaining('Done (ok)'), findsOneWidget);
      expect(backend.openCalls, 1); // terminal done: never re-sends
    });

    testWidgets('stream open failure falls back to /v1/talk once',
        (t) async {
      final backend = _FakeBackend()
        ..openError =
            const CoreException(500, 'POST /v1/talk/stream -> 500: boom')
        ..fallbackReply = {
          'reply': 'fallback reply',
          'status': 'ok',
          'mission_id': '',
          'execution_id': '',
          'turn_id': 'ft-7',
        };
      await _pumpPanel(t, backend);
      await t.enterText(find.byType(TextField), 'hello');
      await t.tap(find.text('Send'));
      await t.pump();
      await t.pump();
      await t.pump();
      expect(backend.openCalls, 1);
      expect(backend.fallbackCalls, 1);
      expect(find.textContaining('fallback reply'), findsOneWidget);
      expect(find.textContaining('used /v1/talk fallback'), findsOneWidget);
    });

    testWidgets('disconnect offers resend-as-new-turn or discard',
        (t) async {
      final backend = _FakeBackend();
      await _pumpPanel(t, backend);
      await t.enterText(find.byType(TextField), 'hello');
      await t.tap(find.text('Send'));
      await t.pump();
      await t.pump();
      backend.ctrl.add(_ev('turn', {'turn_id': 't-6'}));
      backend.ctrl.add(_ev('delta', {'text': 'Hi'}));
      await t.pump();
      expect(find.textContaining('Hi'), findsOneWidget);
      backend.endStream(); // clean end, no done: disconnect
      await _settle(
          t,
          () =>
              find.textContaining('Disconnected').evaluate().isNotEmpty);
      expect(find.textContaining('Disconnected'), findsOneWidget);
      expect(find.text('Resend as new turn'), findsOneWidget);
      expect(find.text('Discard'), findsOneWidget);
      await t.pump();
      await t.pump();
      expect(backend.openCalls, 1); // no silent retry loop
      await t.tap(find.text('Discard'));
      await t.pump();
      expect(find.textContaining('Disconnected'), findsNothing);
      expect(find.text('Resend as new turn'), findsNothing);
    });

    testWidgets('resend opens a new turn with the same text', (t) async {
      final backend = _FakeBackend();
      await _pumpPanel(t, backend);
      await t.enterText(find.byType(TextField), 'hello');
      await t.tap(find.text('Send'));
      await t.pump();
      await t.pump();
      backend.ctrl.add(_ev('turn', {'turn_id': 't-7'}));
      await t.pump();
      backend.endStream();
      await _settle(
          t, () => find.text('Resend as new turn').evaluate().isNotEmpty);
      expect(find.text('Resend as new turn'), findsOneWidget);
      await t.tap(find.text('Resend as new turn'));
      await _settle(t, () => backend.openCalls == 2);
      expect(backend.openCalls, 2);
      expect(backend.lastText, 'hello');
      backend.ctrl.add(_ev('turn', {'turn_id': 't-8'}));
      backend.ctrl.add(_ev('done', {
        'reply': 'again',
        'status': 'ok',
        'turn_id': 't-8',
        'mission_id': '',
        'execution_id': ''
      }));
      await t.pump();
      await t.pump();
      expect(find.textContaining('Done (ok)'), findsOneWidget);
    });

    testWidgets('cancel button cancels the turn and shows state',
        (t) async {
      final backend = _FakeBackend();
      await _pumpPanel(t, backend);
      await t.enterText(find.byType(TextField), 'hello');
      await t.tap(find.text('Send'));
      await t.pump();
      await t.pump();
      backend.ctrl.add(_ev('turn', {'turn_id': 't-9'}));
      await t.pump();
      expect(find.text('Cancel'), findsOneWidget);
      await t.tap(find.text('Cancel'));
      await _settle(
          t,
          () => find.textContaining('Cancelled').evaluate().isNotEmpty);
      expect(backend.cancelCalled, isTrue);
      expect(find.textContaining('Cancelled'), findsOneWidget);
    });
  });
}
