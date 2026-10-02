import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/capabilities.dart';

void main() {
  test('wake word capability is reserved as Hey Zara and not advertised', () {
    final wake = androidCapabilities.firstWhere((c) => c.name == 'voice.wake_word');
    expect(wake.supported, isFalse);
    expect(wake.note, contains('Hey Zara'));
    expect(advertisedCapabilities(), isNot(contains('voice.wake_word')));
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
