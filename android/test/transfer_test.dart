// Transfer helpers: SHA-256 vectors, chunking, deterministic payloads.
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/transfer.dart';

String hexOf(String s) =>
    sha256Hex(s.codeUnits);

void main() {
  test('sha256 NIST vectors', () {
    expect(hexOf(''), // empty
        'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
    expect(hexOf('abc'),
        'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    expect( // 56-byte boundary (two blocks after padding)
        sha256Hex(List<int>.filled(56, 0x61)),
        'b35439a4ac6f0948b6d6f9e3c6af0f5f590ce20f1bde7090ef7970686ec6738a');
    expect( // 64-byte boundary
        sha256Hex(List<int>.filled(64, 0x62)),
        'a0fab1377f49a759b57f63318262ebe89fabfc990e8e93ceac2984561482b9d4');
  });

  test('sha256 matches streaming length encoding (1 MiB of a)', () {
    final digest = sha256Hex(List<int>.filled(1024 * 1024, 0x61));
    expect(digest,
        '9bc1b2a288b26af7257a36277ae3816a7d4f16e89c1e7e77d0a5c48bad62b360');
  });

  test('deterministic bytes formula', () {
    final b = deterministicBytes(300);
    expect(b.length, 300);
    expect(b[0], 7);
    expect(b[249], (7 + 249) & 0xff);
    expect(b[249], 0); // wraps: binary-safety, not text
    expect(deterministicBytes(10, 42)[0], 42);
    expect(() => deterministicBytes(0), throwsArgumentError);
  });

  test('chunk windows cover exactly', () {
    final w = chunkWindows(200 * 1024, 64 * 1024);
    expect(w.length, 4);
    expect(w.last, [3 * 64 * 1024, 8192]);
    var sum = 0;
    for (final seg in w) {
      sum += seg[1];
    }
    expect(sum, 200 * 1024);
    expect(chunkWindows(100, 65536), [
      [0, 100]
    ]);
    expect(() => chunkWindows(0, 10), throwsArgumentError);
  });

  test('stage15 text is deterministic', () {
    expect(stage15Text('a-to-b'), contains('direction=a-to-b'));
    expect(stage15Text('a-to-b'), stage15Text('a-to-b'));
  });
}
