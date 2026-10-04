import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/assistant.dart';

void main() {
  test('empty report is never success', () {
    const s = AssistantStatus();
    expect(s.serviceRegistered, isFalse);
    expect(s.zaraIsDefault, isFalse);
    expect(AssistantStatus.fromMap({}).zaraIsDefault, isFalse);
  });

  test('parses native report; default detected by component match', () {
    final s = AssistantStatus.fromMap({
      'service_registered': true,
      'service_component':
          'dev.zara.zara_android/.ZaraVoiceInteractionService',
      'current_assistant':
          'dev.zara.zara_android/.ZaraVoiceInteractionService',
      'zara_is_default': true,
      'role_available': true,
      'role_held': true,
      'role_request_possible': false,
    });
    expect(s.serviceRegistered, isTrue);
    expect(s.zaraIsDefault, isTrue);
    expect(s.roleRequestPossible, isFalse);
    expect(s.describe(), contains('Zara IS the default'));
  });

  test('foreign default reported truthfully', () {
    final s = AssistantStatus.fromMap({
      'service_registered': true,
      'current_assistant': 'com.google.android.googlequicksearchbox/xyz',
      'zara_is_default': false,
    });
    expect(s.zaraIsDefault, isFalse);
    expect(s.describe(), contains('googlequicksearchbox'));
  });
}
