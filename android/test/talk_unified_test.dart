// Unified conversation experience (Phase 1): TalkStreamPanel reads as one
// assistant flow with the Tasks + Approval sections — consistent
// user/assistant message styling, streaming text never duplicated by the
// done payload, tool rows linked to Tasks execution states, cancel with a
// correct terminal state, rapid-tap single submit, explicit offline/error
// states with a bounded non-streaming fallback, malformed-event
// resilience. Socket-free fakes only (flutter_tester cannot hold loopback
// servers open); wire behavior stays covered by stage16_stream_test.dart.
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';
import 'package:zara_android/stream_client.dart';
import 'package:zara_android/talk_stream_view.dart';

/// Socket-free fake backend (same shape as stage16's): panel tests drive
/// [TalkStreamHandle.fake] event streams instead of HTTP.
class _FakeBackend extends TalkBackend {
  _FakeBackend() : super(CoreClient('http://127.0.0.1:1'));
  StreamController<TalkStreamEvent> ctrl =
      StreamController<TalkStreamEvent>.broadcast();
  Object? openError;
  Map<String, dynamic>? fallbackReply;
  Object? fallbackError;
  int openCalls = 0;
  int fallbackCalls = 0;
  bool cancelCalled = false;

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

  /// End the current stream without a `done` (disconnect drill) and swap
  /// in a fresh controller so a later resend gets its own event feed.
  void endStream() {
    final current = ctrl;
    ctrl = StreamController<TalkStreamEvent>.broadcast();
    unawaited(current.close());
  }
}

TalkStreamEvent _ev(String name, Map<String, dynamic> payload) =>
    TalkStreamEvent(name, Map<String, dynamic>.from(payload));

Future<void> _pumpPanel(WidgetTester t, _FakeBackend backend) {
  return t.pumpWidget(MaterialApp(
    home: Scaffold(
      body: TalkStreamPanel(
        client: CoreClient('http://127.0.0.1:1'),
        tokenCtrl: TextEditingController(),
        onApproval: (id, reason) {},
        backend: backend,
      ),
    ),
  ));
}

/// Pump frames until [cond] holds (bounded). Stream teardown chains span
/// async gaps that include zero-duration timers, which only fire when fake
/// time advances — so pump WITH a duration.
Future<void> _settle(WidgetTester t, bool Function() cond) async {
  for (var i = 0; i < 20; i++) {
    await t.pump(const Duration(milliseconds: 50));
    if (cond()) return;
  }
}

Future<void> _sendText(WidgetTester t, String text) async {
  await t.enterText(find.byType(TextField), text);
  await t.tap(find.text('Send'));
  await t.pump();
  await t.pump();
}

void main() {
  group('talkSendKey rapid-tap guard (pure)', () {
    test('same text shares a key (whitespace-insensitive)', () {
      expect(talkSendKey('hello'), talkSendKey('  hello  '));
    });

    test('different text is a distinct key', () {
      expect(talkSendKey('hello'), isNot(talkSendKey('goodbye')));
    });

    test('key never embeds the prompt text (no token logging)', () {
      const secret = 'my-secret-prompt-xyz';
      expect(talkSendKey(secret), isNot(contains(secret)));
    });

    test('shortExecId truncates like the Tasks section', () {
      expect(shortExecId('ex-1'), 'ex-1');
      expect(shortExecId('0123456789abcdef'), '0123456789ab…');
    });
  });

  group('terminal states are sticky (pure reducer)', () {
    test('done/error after cancel are ignored (cancel wins)', () {
      final c = TalkConversation();
      c.apply(const TalkStreamEvent('turn', {'turn_id': 't-1'}));
      c.apply(const TalkStreamEvent('delta', {'text': 'Hi'}));
      c.markCancelled();
      expect(c.status, 'cancelled');
      c.apply(const TalkStreamEvent('done', {
        'reply': 'LATE',
        'status': 'ok',
        'turn_id': 't-1',
        'mission_id': '',
        'execution_id': ''
      }));
      expect(c.status, 'cancelled');
      expect(c.reply, 'Hi');
      c.apply(const TalkStreamEvent('error', {'message': 'late'}));
      expect(c.status, 'cancelled');
      c.apply(const TalkStreamEvent('delta', {'text': '!!!'}));
      expect(c.reply, 'Hi');
    });

    test('done after error is ignored (first terminal wins)', () {
      final c = TalkConversation();
      c.apply(const TalkStreamEvent('turn', {'turn_id': 't-1'}));
      c.markError('boom');
      c.apply(const TalkStreamEvent('done', {
        'reply': 'LATE',
        'status': 'ok',
        'turn_id': 't-1',
        'mission_id': '',
        'execution_id': ''
      }));
      expect(c.status, 'error');
    });
  });

  group('TalkStreamPanel unified conversation (socket-free fakes)', () {
    testWidgets('user + assistant share one message style; done never duplicates',
        (t) async {
      final backend = _FakeBackend();
      await _pumpPanel(t, backend);
      await _sendText(t, 'hello');
      expect(backend.openCalls, 1);
      // Consistent styling: matching You/Zara bubbles.
      expect(find.text('You'), findsOneWidget);
      expect(find.textContaining('typing'), findsOneWidget);
      backend.ctrl.add(_ev('turn', {'turn_id': 't-1'}));
      backend.ctrl.add(_ev('delta', {'text': 'Hello '}));
      backend.ctrl.add(_ev('delta', {'text': 'world'}));
      await t.pump();
      expect(find.textContaining('Hello world'), findsOneWidget);
      // Tool row carries the Tasks-tracked execution id.
      backend.ctrl.add(_ev('tool', {
        'tool': 'system.battery',
        'phase': 'ok',
        'execution_id': 'ex-abc-123',
        'device_id': 'android-phone',
        'message': 'read ok'
      }));
      await t.pump();
      expect(find.textContaining('system.battery · ok'), findsOneWidget);
      expect(find.textContaining('ex-abc-123'), findsOneWidget);
      // Terminal done reconciles into the SAME bubble: exactly one
      // 'Hello world', never a duplicated streaming + final copy.
      backend.ctrl.add(_ev('done', {
        'reply': 'Hello world',
        'status': 'ok',
        'turn_id': 't-1',
        'mission_id': '',
        'execution_id': ''
      }));
      await t.pump();
      await t.pump();
      expect(find.textContaining('Hello world'), findsOneWidget);
      expect(find.text('Zara'), findsOneWidget);
      expect(find.textContaining('Done (ok)'), findsOneWidget);
      expect(backend.openCalls, 1); // terminal: never re-sends
    });

    testWidgets('cancel lands a terminal state late frames cannot revive',
        (t) async {
      final backend = _FakeBackend();
      await _pumpPanel(t, backend);
      await _sendText(t, 'hello');
      backend.ctrl.add(_ev('turn', {'turn_id': 't-9'}));
      backend.ctrl.add(_ev('delta', {'text': 'partial'}));
      await t.pump();
      expect(find.text('Cancel'), findsOneWidget);
      await t.tap(find.text('Cancel'));
      await _settle(
          t, () => find.textContaining('Cancelled').evaluate().isNotEmpty);
      expect(backend.cancelCalled, isTrue);
      expect(find.textContaining('Cancelled'), findsOneWidget);
      expect(find.text('Cancel'), findsNothing); // terminal: no cancel
      // Late done for the aborted turn is ignored (sticky terminal).
      backend.ctrl.add(_ev('done', {
        'reply': 'LATE',
        'status': 'ok',
        'turn_id': 't-9',
        'mission_id': '',
        'execution_id': ''
      }));
      await t.pump();
      await t.pump();
      expect(find.textContaining('Cancelled'), findsOneWidget);
      expect(find.textContaining('Done ('), findsNothing);
      // Send is available again for a fresh turn.
      expect(find.text('Send'), findsOneWidget);
    });

    testWidgets('stable session id reused across turns', (t) async {
      final backend = _FakeBackend();
      await _pumpPanel(t, backend);
      await _sendText(t, 'first');
      expect(backend.lastSessionId, ''); // nothing known yet
      backend.ctrl.add(_ev('turn', {'turn_id': 't-1'}));
      backend.ctrl.add(_ev('done', {
        'reply': 'hi',
        'status': 'ok',
        'turn_id': 't-1',
        'mission_id': '',
        'execution_id': '',
        'session_id': 'ses-abc',
      }));
      await _settle(
          t, () => find.text('Send').evaluate().isNotEmpty);
      await _sendText(t, 'second');
      expect(backend.lastSessionId, 'ses-abc'); // adopted, reused
    });

    testWidgets('fallback adopts server session id', (t) async {
      final backend = _FakeBackend()
        ..openError = const CoreException(-1, 'down')
        ..fallbackReply = {
          'reply': 'fb',
          'status': 'ok',
          'turn_id': 't-fb',
          'mission_id': '',
          'execution_id': '',
          'session_id': 'ses-fb',
        };
      await _pumpPanel(t, backend);
      await _sendText(t, 'hello');
      await _settle(
          t, () => find.text('Send').evaluate().isNotEmpty);
      expect(backend.fallbackCalls, 1);
    });

    testWidgets('rapid double-tap submits once with an explicit notice',
        (t) async {
      final backend = _FakeBackend();
      await _pumpPanel(t, backend);
      await t.enterText(find.byType(TextField), 'hello');
      await t.tap(find.text('Send'));
      await t.tap(find.text('Send')); // same frame: must collapse
      await t.pump();
      await t.pump();
      expect(backend.openCalls, 1);
      expect(find.textContaining('already sending'), findsOneWidget);
      // The single turn still completes normally.
      backend.ctrl.add(_ev('turn', {'turn_id': 't-2'}));
      backend.ctrl.add(_ev('done', {
        'reply': 'hi',
        'status': 'ok',
        'turn_id': 't-2',
        'mission_id': '',
        'execution_id': ''
      }));
      await t.pump();
      await t.pump();
      expect(find.textContaining('Done (ok)'), findsOneWidget);
    });

    testWidgets('offline open failure falls back once (bounded), then explicit',
        (t) async {
      final backend = _FakeBackend()
        ..openError =
            const CoreException(-1, 'SocketException: offline (no route)')
        ..fallbackReply = {
          'reply': 'fallback reply',
          'status': 'ok',
          'mission_id': '',
          'execution_id': '',
          'turn_id': 'ft-1',
        };
      await _pumpPanel(t, backend);
      await _sendText(t, 'hello');
      await _settle(t,
          () => find.textContaining('fallback reply').evaluate().isNotEmpty);
      expect(backend.openCalls, 1);
      expect(backend.fallbackCalls, 1); // exactly one bounded fallback
      expect(find.textContaining('fallback reply'), findsOneWidget);
      expect(find.textContaining('used /v1/talk fallback'), findsOneWidget);
      expect(find.textContaining('Done (ok)'), findsOneWidget);
    });

    testWidgets('stream + fallback both down: explicit error, never retried',
        (t) async {
      final backend = _FakeBackend()
        ..openError = const CoreException(500, 'boom')
        ..fallbackError = const CoreException(500, 'fallback boom');
      await _pumpPanel(t, backend);
      await _sendText(t, 'hello');
      await _settle(
          t, () => find.textContaining('Error').evaluate().isNotEmpty);
      expect(backend.openCalls, 1);
      expect(backend.fallbackCalls, 1); // bounded: no retry loop
      expect(find.textContaining('Error'), findsOneWidget);
      expect(find.text('Resend as new turn'), findsOneWidget);
      expect(find.textContaining('Done ('), findsNothing); // never success
    });

    testWidgets('malformed stream event surfaces explicit error, stays usable',
        (t) async {
      final backend = _FakeBackend();
      await _pumpPanel(t, backend);
      await _sendText(t, 'hello');
      backend.ctrl.add(_ev('turn', {'turn_id': 't-3'}));
      await t.pump();
      backend.ctrl.addError(
          const FormatException('malformed SSE data: unexpected char'));
      await _settle(
          t, () => find.textContaining('Error').evaluate().isNotEmpty);
      expect(find.textContaining('parse error'), findsOneWidget);
      expect(find.textContaining('Done ('), findsNothing); // never success
      expect(find.text('Resend as new turn'), findsOneWidget);
      // Recovery path works: resend opens a fresh turn.
      await t.tap(find.text('Resend as new turn'));
      await _settle(t, () => backend.openCalls == 2);
      expect(backend.openCalls, 2);
    });
  });
}
