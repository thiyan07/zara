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

  test('loop resumes on foreground when online or degraded only', () {
    expect(shouldResumeLoop(DeviceLifecycleState.online), isTrue);
    expect(shouldResumeLoop(DeviceLifecycleState.degraded), isTrue);
    for (final s in [
      DeviceLifecycleState.pairing,
      DeviceLifecycleState.revoked,
      DeviceLifecycleState.loggedOut,
      DeviceLifecycleState.error,
      DeviceLifecycleState.uninitialized,
    ]) {
      expect(shouldResumeLoop(s), isFalse, reason: s);
    }
  });
}
