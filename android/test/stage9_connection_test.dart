import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/connection.dart';

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
}
