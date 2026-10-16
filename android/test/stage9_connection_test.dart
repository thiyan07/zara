import 'dart:async';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/connection.dart';
import 'package:zara_android/core_client.dart';

void main() {
  test('backoff grows then caps, attempts bounded', () {
    const p = RetryPolicy(maxAttempts: 5);
    expect(p.delayFor(0), equals(const Duration(seconds: 1)));
    expect(p.delayFor(1), equals(const Duration(seconds: 2)));
    expect(p.delayFor(2), equals(const Duration(seconds: 4)));
    expect(p.delayFor(100), equals(const Duration(seconds: 60)));
    expect(p.shouldRetry(4, batteryCritical: false), isTrue);
    expect(p.shouldRetry(5, batteryCritical: false), isFalse);
  });

  test('battery-critical never auto-retries', () {
    const p = RetryPolicy();
    expect(p.shouldRetry(0, batteryCritical: true), isFalse);
  });

  test('request IDs are unique and ordered', () {
    final a = newRequestId(), b = newRequestId();
    expect(a == b, isFalse);
    expect(a.startsWith('req-'), isTrue);
  });

  test('heartbeat coalesces, queue bounds, FIFO drain', () {
    final q = PendingQueue(maxSize: 3);
    expect(q.add(PendingOp('heartbeat', {})), isTrue);
    expect(q.add(PendingOp('heartbeat', {})), isTrue);
    expect(q.length, equals(1)); // coalesced
    expect(q.add(PendingOp('events', {})), isTrue);
    expect(q.add(PendingOp('ack', {})), isTrue);
    expect(q.add(PendingOp('ack', {})), isFalse); // full: truthful refusal
    expect(q.drain().length, equals(3));
    expect(q.isEmpty, isTrue);
  });

  test('stale responses dropped by request ID', () {
    final g = ResponseGuard();
    g.track('hb', 'req-1');
    g.track('hb', 'req-2');
    expect(g.isCurrent('hb', 'req-1'), isFalse);
    expect(g.isCurrent('hb', 'req-2'), isTrue);
  });

  test('connection states only move legally', () {
    for (final entry in CoreLinkState.allowed.entries) {
      for (final to in entry.value) {
        expect(CoreLinkState.allowed[to], isNotNull,
            reason: '$to must itself be a known state');
      }
    }
    expect(CoreLinkState.allowed['online']!.contains('offline'), isTrue);
  });

  group('transport mapping (Vivo connection-closed defect)', () {
    test('mid-request kill maps to structured dropped-connection error',
        () {
      // Exact physical repro: Core SIGTERM during POST
      // /v1/agent/register produced this raw exception in the UI banner.
      final e = mapTransportError(
          HttpException(
              'Connection closed before full header was received',
              uri: Uri.parse(
                  'http://127.0.0.1:18204/v1/agent/register')),
          'POST',
          '/v1/agent/register');
      expect(e, isA<CoreException>());
      expect(e.status, equals(-1)); // transport, never a server verdict
      expect('$e', contains('connection dropped'));
      expect('$e', contains('POST /v1/agent/register'));
      expect('$e', contains('HttpException')); // original kept for buckets
    });

    test('refused maps to unreachable, timeouts stay bounded', () {
      final refused = mapTransportError(
          const SocketException('Connection refused'), 'GET', '/v1/ready');
      expect(refused.status, equals(-1));
      expect('$refused', contains('unreachable'));
      expect('$refused', contains('SocketException'));

      final timedOut = mapTransportError(
          TimeoutException('t', const Duration(seconds: 15)),
          'POST',
          '/v1/agent/voice/turn');
      expect('$timedOut', contains('timed out after 15s'));
      expect('$timedOut', contains('POST /v1/agent/voice/turn'));
    });

    test('server verdicts pass through untouched (never masked)', () {
      const decided = CoreException(409, 'already decided');
      expect(identical(mapTransportError(decided, 'POST', '/x'), decided),
          isTrue);
      const auth = AuthException(403, 'revoked');
      expect(mapTransportError(auth, 'POST', '/x'), same(auth));
    });

    test('mapped errors never echo bodies, queries, or secrets', () {
      final e = mapTransportError(
          const SocketException('refused'), 'POST', '/v1/memory/search');
      expect('$e', isNot(contains('Bearer')));
      expect('$e', isNot(contains('zara-dev-')));
      expect('$e', isNot(contains('audio_base64')));
    });
  });
}
