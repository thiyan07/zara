import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/device_lifecycle.dart';
import 'package:zara_android/main.dart';

void main() {
  test('409 means stale card (dismiss), other errors keep the card', () {
    expect(
        isStaleApproval(
            'CoreException(409): POST /v1/agent/approvals/exec-1 -> 409'),
        isTrue);
    expect(isStaleApproval('CoreException(403): forbidden'), isFalse);
    expect(isStaleApproval('SocketException: connection refused'), isFalse);
    expect(isStaleApproval(''), isFalse);
  });

  test('404 means unknown execution (dismiss with explanation), auth stays explicit',
      () {
    // Physical Vivo defect: after a Core restart the cancelled card Deny
    // returned 404 but the card stayed presented as pending.
    expect(
        isUnknownExecutionApproval(
            'CoreException(404): POST /v1/agent/approvals/exec-bb8a -> 404: {"detail":"unknown execution"}'),
        isTrue);
    // 404 is not a 409-stale and vice versa: distinct explanations.
    expect(
        isUnknownExecutionApproval(
            'CoreException(409): POST /v1/agent/approvals/exec-1 -> 409'),
        isFalse);
    expect(
        isStaleApproval(
            'CoreException(404): POST /v1/agent/approvals/exec-1 -> 404'),
        isFalse);
    // 401/403 are explicit fail-closed auth failures: card kept, no
    // auto-repair, never treated as stale or unknown.
    for (final e in [
      'CoreException(401): POST /v1/agent/approvals/exec-1 -> 401: {"detail":"invalid token"}',
      'CoreException(403): POST /v1/agent/register -> 403: {"detail":"invalid/revoked device identity"}',
      'SocketException: connection refused',
      '',
    ]) {
      expect(isUnknownExecutionApproval(e), isFalse, reason: e);
      expect(isStaleApproval(e), isFalse, reason: e);
    }
  });

  test('loop resumes on foreground when online or degraded only', () {
    expect(shouldResumeLoop(DeviceLifecycleState.online), isTrue);
    expect(shouldResumeLoop(DeviceLifecycleState.degraded), isTrue);
    for (final s in [
      DeviceLifecycleState.pairing,
      DeviceLifecycleState.enrolled,
      DeviceLifecycleState.registering,
      DeviceLifecycleState.reconnecting,
      DeviceLifecycleState.revoked,
      DeviceLifecycleState.loggedOut,
      DeviceLifecycleState.error,
      DeviceLifecycleState.uninitialized,
    ]) {
      expect(shouldResumeLoop(s), isFalse, reason: s);
    }
  });

  test('revoked/logged-out never resume: re-pair required, no silent retry', () {
    // Duplicate-loop prevention: after revoke/logout the poll loop and
    // register-retry timer are both cancelled in main.dart (_tick +
    // _register AuthException paths, _logout). The pure contract half is
    // that neither state re-arms the loop on foreground resume.
    for (final s in [
      DeviceLifecycleState.revoked,
      DeviceLifecycleState.loggedOut,
    ]) {
      expect(shouldResumeLoop(s), isFalse, reason: s);
    }
    // And the lifecycle table only permits exit via explicit re-pair.
    final l = DeviceLifecycle();
    l.restore('d');
    l.move(DeviceLifecycleState.online);
    expect(l.handleHttp(403, hasIdentity: true), equals('revoked'));
    expect(() => l.move(DeviceLifecycleState.online), throwsStateError);
    l.move(DeviceLifecycleState.pairing);
    expect(l.state, equals(DeviceLifecycleState.pairing));
  });

  group('post-approval poll nudge', () {
    test('successful approve while online nudges exactly once', () {
      expect(
          shouldNudgePollAfterApproval(
              approve: true,
              succeeded: true,
              lifecycleState: DeviceLifecycleState.online,
              networkOnline: true),
          isTrue);
    });

    test('denial never nudges (nothing dispatched)', () {
      expect(
          shouldNudgePollAfterApproval(
              approve: false,
              succeeded: true,
              lifecycleState: DeviceLifecycleState.online,
              networkOnline: true),
          isFalse);
    });

    test('failed decision never nudges (card kept for retry)', () {
      expect(
          shouldNudgePollAfterApproval(
              approve: true,
              succeeded: false,
              lifecycleState: DeviceLifecycleState.online,
              networkOnline: true),
          isFalse);
    });

    test('offline network never nudges (loop owns backoff)', () {
      expect(
          shouldNudgePollAfterApproval(
              approve: true,
              succeeded: true,
              lifecycleState: DeviceLifecycleState.online,
              networkOnline: false),
          isFalse);
    });

    test('non-online lifecycle never nudges (matches _tick guard)', () {
      for (final s in [
        DeviceLifecycleState.degraded,
        DeviceLifecycleState.reconnecting,
        DeviceLifecycleState.pairing,
        DeviceLifecycleState.registering,
        DeviceLifecycleState.revoked,
        DeviceLifecycleState.loggedOut,
        DeviceLifecycleState.error,
        DeviceLifecycleState.uninitialized,
      ]) {
        expect(
            shouldNudgePollAfterApproval(
                approve: true,
                succeeded: true,
                lifecycleState: s,
                networkOnline: true),
            isFalse,
            reason: s);
      }
    });
  });
}
