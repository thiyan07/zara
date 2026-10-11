// Stream reliability edges (Stage 16 follow-up): parser + reducer only,
// no sockets. Covers: split-chunk at every byte boundary incl. multibyte
// splits, :ping keepalives during tool-wait, late done-after-cancel,
// done-after-done, disconnect mid-delta (partial text kept, explicit
// interrupted state), and resend-as-new-turn (never reuses the cancelled
// id). Resend-as-new-turn is the documented disconnect semantic — mid-
// stream resume is never expected.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/stream_client.dart';

String _frame(String name, Map<String, dynamic> payload) =>
    'event: $name\ndata: ${jsonEncode(payload)}\n\n';

TalkStreamEvent _ev(String name, Map<String, dynamic> payload) =>
    TalkStreamEvent(name, Map<String, dynamic>.from(payload));

void main() {
  group('split-chunk reliability (every boundary)', () {
    test('split at every byte offset reassembles identically', () {
      final full = utf8.encode(
        '${_frame('turn', {'turn_id': 't-1'})}'
        ': ping\n\n'
        '${_frame('delta', {'text': 'Hello '})}'
        '${_frame('delta', {'text': 'world'})}'
        '${_frame('tool', {
          'tool': 'system.battery',
          'phase': 'started',
          'execution_id': '',
          'device_id': 'android-phone',
          'message': ''
        })}'
        '${_frame('tool', {
          'tool': 'system.battery',
          'phase': 'ok',
          'execution_id': 'ex-1',
          'device_id': 'android-phone',
          'message': 'read ok'
        })}'
        '${_frame('done', {
          'reply': 'Hello world',
          'status': 'ok',
          'turn_id': 't-1',
          'mission_id': '',
          'execution_id': ''
        })}',
      );
      final wantNames = SseParser()
          .addBytes(full)
          .map((e) => e.name)
          .toList(growable: false);
      expect(wantNames,
          ['turn', 'delta', 'delta', 'tool', 'tool', 'done']);
      for (var split = 0; split <= full.length; split++) {
        final p = SseParser();
        final got = <TalkStreamEvent>[
          ...p.addBytes(full.sublist(0, split)),
          ...p.addBytes(full.sublist(split)),
        ];
        expect(got.map((e) => e.name).toList(), wantNames,
            reason: 'split at $split');
        expect(got.map((e) => jsonEncode(e.payload)).toList(),
            SseParser().addBytes(full).map((e) => jsonEncode(e.payload)),
            reason: 'split at $split');
        expect(p.flush(), isEmpty, reason: 'split at $split');
      }
    });

    test('multibyte split at every byte offset decodes cleanly', () {
      // Multibyte chars (2-, 3-, 4-byte) spread across the frame so that
      // nearly every split point lands inside a character.
      const text = 'héllo wörld 🌍✓ — naïve café 💬';
      final frame = utf8.encode(_frame('delta', {'text': text}));
      for (var split = 0; split <= frame.length; split++) {
        final p = SseParser();
        final got = <TalkStreamEvent>[
          ...p.addBytes(frame.sublist(0, split)),
          ...p.addBytes(frame.sublist(split)),
        ];
        expect(got, hasLength(1), reason: 'split at $split');
        expect(got.single.payload['text'], text, reason: 'split at $split');
        expect(p.flush(), isEmpty, reason: 'split at $split');
      }
    });

    test('byte-at-a-time feed reassembles identically', () {
      final full = utf8.encode(
          '${_frame('turn', {'turn_id': 't-9'})}'
          '${_frame('delta', {'text': 'x 🌍 y'})}'
          '${_frame('done', {
            'reply': 'x',
            'status': 'ok',
            'turn_id': 't-9',
            'mission_id': '',
            'execution_id': ''
          })}');
      final p = SseParser();
      final got = <TalkStreamEvent>[];
      for (final b in full) {
        got.addAll(p.addBytes([b]));
      }
      expect(got.map((e) => e.name).toList(), ['turn', 'delta', 'done']);
      expect(got[1].payload['text'], 'x 🌍 y');
      expect(p.flush(), isEmpty);
    });
  });

  group(':ping during tool-wait is ignored', () {
    test('keepalives interleaved with tool frames change nothing', () {
      final parser = SseParser();
      final events = parser.addBytes(utf8.encode(
        '${_frame('turn', {'turn_id': 't-2'})}'
        ': ping\n\n'
        '${_frame('tool', {
          'tool': 'system.battery',
          'phase': 'started',
          'execution_id': '',
          'device_id': 'd',
          'message': ''
        })}'
        ': ping\n'
        ': ping\n\n'
        '${_frame('tool', {
          'tool': 'system.battery',
          'phase': 'ok',
          'execution_id': 'ex-1',
          'device_id': 'd',
          'message': 'done'
        })}'
        ': ping\n\n',
      ));
      expect(events.map((e) => e.name).toList(),
          ['turn', 'tool', 'tool']);

      final c = TalkConversation();
      for (final e in events) {
        c.apply(e);
      }
      // Tool-wait state is pure narration: two rows, still streaming,
      // no reply text fabricated by the keepalives.
      expect(c.tools, hasLength(2));
      expect(c.reply, isEmpty);
      expect(c.status, 'streaming');
      expect(c.isTerminal, isFalse);
      expect(parser.flush(), isEmpty);
    });
  });

  group('terminal stickiness', () {
    TalkConversation streaming() {
      final c = TalkConversation();
      c.apply(_ev('turn', {'turn_id': 't-3'}));
      c.apply(_ev('delta', {'text': 'partial'}));
      return c;
    }

    Map<String, dynamic> done(String reply, String tid) => {
          'reply': reply,
          'status': 'ok',
          'turn_id': tid,
          'mission_id': '',
          'execution_id': '',
        };

    test('late done-after-cancel is ignored', () {
      final c = streaming();
      c.markCancelled();
      expect(c.status, 'cancelled');
      c.apply(_ev('done', done('late reply', 't-3')));
      expect(c.status, 'cancelled');
      expect(c.doneCount, 0);
      expect(c.reply, 'partial'); // pre-cancel text kept, never overwritten
      expect(c.isDone, isFalse);
      // Further frames stay dead too: cancel can never be revived.
      c.apply(_ev('delta', {'text': '!!!'}));
      expect(c.reply, 'partial');
    });

    test('done-after-done is ignored (exactly-once reply)', () {
      final c = streaming();
      c.apply(_ev('done', done('first', 't-3')));
      expect(c.isDone, isTrue);
      expect(c.doneCount, 1);
      expect(c.reply, 'first');
      c.apply(_ev('done', done('second', 't-3')));
      c.apply(_ev('done', done('other turn', 't-4')));
      expect(c.doneCount, 1);
      expect(c.reply, 'first');
      c.apply(_ev('delta', {'text': '!!!'}));
      expect(c.reply, 'first');
    });
  });

  group('disconnect keeps partial text with explicit state', () {
    test('disconnect mid-delta: partial text kept, interrupted, resendable',
        () {
      final c = TalkConversation();
      c.apply(_ev('turn', {'turn_id': 't-5'}));
      c.apply(_ev('delta', {'text': 'Hel'}));
      c.apply(_ev('delta', {'text': 'lo '}));
      c.markDisconnected('stream ended before done (disconnect?)');
      expect(c.status, 'disconnected');
      expect(c.isTerminal, isFalse); // NOT terminal: user must choose
      expect(c.isDone, isFalse);
      expect(c.reply, 'Hello '); // partial text preserved, not dropped
      // Late terminal frames after a disconnect but before resend still
      // settle normally on this object; the resend path below uses a
      // fresh turn, so this can never double-execute.
      expect(c.turnId, 't-5');
    });
  });

  group('resend creates a new turn id', () {
    test('resend never reuses the cancelled/interrupted id', () {
      final old = TalkConversation();
      old.apply(_ev('turn', {'turn_id': 't-6'}));
      old.apply(_ev('delta', {'text': 'half'}));
      old.markDisconnected('socket closed');
      expect(old.status, 'disconnected');
      final oldId = old.turnId;
      expect(oldId, 't-6');

      // Resend-as-new-turn: a fresh reducer + fresh server turn_id.
      final fresh = TalkConversation();
      fresh.apply(_ev('turn', {'turn_id': 't-7'}));
      expect(fresh.turnId, isNotNull);
      expect(fresh.turnId, isNot(equals(oldId)));
      expect(fresh.reply, isEmpty); // no stale text leaks into the new turn
      expect(fresh.status, 'streaming');
      fresh.apply(_ev('delta', {'text': 'new reply'}));
      fresh.apply(_ev('done', {
        'reply': 'new reply',
        'status': 'ok',
        'turn_id': 't-7',
        'mission_id': '',
        'execution_id': ''
      }));
      expect(fresh.isDone, isTrue);
      expect(fresh.doneCount, 1);
      // The old turn object is untouched by the new turn's frames.
      expect(old.status, 'disconnected');
      expect(old.reply, 'half');
    });

    test('cancelled turn resends as a new turn, never the cancelled id', () {
      final cancelled = TalkConversation();
      cancelled.apply(_ev('turn', {'turn_id': 't-8'}));
      cancelled.markCancelled();
      final fresh = TalkConversation();
      fresh.apply(_ev('turn', {'turn_id': 't-9'}));
      expect(fresh.turnId, isNot(equals(cancelled.turnId)));
      expect(fresh.reply, isEmpty);
      final freshDone = _ev('done', {
        'reply': 'new reply',
        'status': 'ok',
        'turn_id': 't-9',
        'mission_id': '',
        'execution_id': ''
      });
      fresh.apply(freshDone);
      expect(fresh.isDone, isTrue);
      expect(fresh.reply, 'new reply');
      // Cross-direction: the fresh turn's done cannot revive/settle the
      // discarded cancelled turn object either.
      cancelled.apply(freshDone);
      expect(cancelled.status, 'cancelled');
      expect(cancelled.doneCount, 0);
    });
  });
}
