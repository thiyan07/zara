import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/main.dart';

void main() {
  group('describeClaimError (pairing failure mapping)', () {
    test('expired code names the 10-minute TTL', () {
      final m = describeClaimError(
          Exception('POST /v1/agent/claim -> 403: pairing code expired'));
      expect(m, contains('10-minute'));
      expect(m, contains('fresh code'));
    });

    test('revoked device points at re-enrollment', () {
      final m = describeClaimError(
          Exception('POST /v1/agent/claim -> 403: device revoked'));
      expect(m, contains('revoked'));
      expect(m, contains('enroll'));
    });

    test('consumed code says single-use', () {
      final m = describeClaimError(Exception(
          'POST /v1/agent/claim -> 403: pairing code already used'));
      expect(m, contains('once'));
    });

    test('ambiguous invalid-or-expired names both causes, never guesses',
        () {
      final m = describeClaimError(Exception(
          'POST /v1/agent/claim -> 403: invalid or expired pairing code'));
      expect(m, contains('unknown or expired'));
      expect(m, isNot(contains('wrong code')));
    });

    test('exact expiry text keeps the TTL hint', () {
      final m = describeClaimError(Exception(
          'POST /v1/agent/claim -> 403: pairing code expired'));
      expect(m, contains('10-minute'));
    });

    test('404 points at wrong code or Core restart', () {
      final m = describeClaimError(Exception('GET /x -> 404: nope'));
      expect(m, contains('fresh code'));
    });

    test('transport failures stay connectivity errors', () {
      final m = describeClaimError(Exception('unreachable POST /x (down)'));
      expect(m, contains('Could not reach Core'));
    });
  });

  group('describeFleetDevice (presence/trust badges)', () {
    test('normal view renders id, trust, presence, cap count', () {
      expect(
          describeFleetDevice({
            'device_id': 'laptop-1',
            'trust': 'trusted',
            'presence': 'online',
            'capabilities': ['a', 'b'],
          }),
          equals('laptop-1 — trust:trusted presence:online caps:2'));
    });

    test('unknown shapes render unknown, never online', () {
      expect(describeFleetDevice({}), contains('unknown'));
      expect(describeFleetDevice({}), isNot(contains('online')));
      expect(
          describeFleetDevice(
              {'device_id': 'x', 'capabilities': 'not-a-list'}),
          contains('caps:0'));
    });

    test('revoked device badge is explicit', () {
      expect(
          describeFleetDevice({
            'device_id': 'old-phone',
            'trust': 'revoked',
            'presence': 'offline',
            'capabilities': [],
          }),
          contains('trust:revoked'));
    });
  });
}
