import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/main.dart';

// Stage 11/12: register-while-unreachable must retry with bounded
// backoff instead of stranding the lifecycle in `registering` forever.
void main() {
  test('register retry backoff: 5/10/20/40 then 60s cap', () {
    expect(registerRetryDelay(1), equals(const Duration(seconds: 5)));
    expect(registerRetryDelay(2), equals(const Duration(seconds: 10)));
    expect(registerRetryDelay(3), equals(const Duration(seconds: 20)));
    expect(registerRetryDelay(4), equals(const Duration(seconds: 40)));
    expect(registerRetryDelay(5), equals(const Duration(seconds: 60)));
    expect(registerRetryDelay(50), equals(const Duration(seconds: 60)));
  });
}
