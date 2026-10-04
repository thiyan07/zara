import 'package:flutter_test/flutter_test.dart';
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
}
