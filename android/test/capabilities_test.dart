import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/capabilities.dart';

void main() {
  test('wake word capability is reserved as Hey Zara and not advertised', () {
    final wake = androidCapabilities.firstWhere((c) => c.name == 'voice.wake_word');
    expect(wake.supported, isFalse);
    expect(wake.note, contains('Hey Zara'));
    expect(advertisedCapabilities(), isNot(contains('voice.wake_word')));
  });

  test('physically verified capabilities are advertised with evidence notes', () {
    for (final name in ['voice.input', 'voice.output', 'assistant.role']) {
      final c = androidCapabilities.firstWhere((c) => c.name == name);
      expect(c.supported, isTrue, reason: name);
      expect(c.note, contains('PHYSICAL_VERIFIED'), reason: name);
    }
    final names = advertisedCapabilities();
    expect(names, containsAll(['voice.input', 'voice.output', 'assistant.role']));
  });

  test('by-design absences stay unadvertised with honest notes', () {
    for (final name in [
      'camera.available',
      'screen.capture',
      'location.available',
      'bluetooth.available',
      'accessibility.automation'
    ]) {
      final c = androidCapabilities.firstWhere((c) => c.name == name);
      expect(c.supported, isFalse, reason: name);
      expect(c.note, contains('by design'), reason: name);
    }
  });

  test('advertised capabilities are all supported=true', () {
    final names = advertisedCapabilities();
    expect(names, isNotEmpty);
    for (final c in androidCapabilities.where((c) => c.supported)) {
      expect(names, contains(c.name));
    }
  });

  test('no fake capabilities: unsupported ones stay out of advertisements', () {
    for (final c in androidCapabilities.where((c) => !c.supported)) {
      expect(advertisedCapabilities(), isNot(contains(c.name)));
    }
  });
}
