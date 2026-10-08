// Zara byte-transfer helpers — pure Dart (no Flutter dependency).
//
// SHA-256 is implemented here (FIPS 180-4) so the app needs no new
// packages: the phone declares content hashes itself and verifies
// downloads locally before acking. Verified against NIST vectors in
// test/transfer_test.dart. The app NEVER trusts Core's hash alone.
import 'dart:typed_data';

const _k = <int>[
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

int _rotr(int x, int n) =>
    (((x & 0xffffffff) >> n) | ((x & 0xffffffff) << (32 - n))) & 0xffffffff;

/// Raw SHA-256 digest (32 bytes) of [message].
Uint8List sha256Bytes(List<int> message) {
  var h0 = 0x6a09e667;
  var h1 = 0xbb67ae85;
  var h2 = 0x3c6ef372;
  var h3 = 0xa54ff53a;
  var h4 = 0x510e527f;
  var h5 = 0x9b05688c;
  var h6 = 0x1f83d9ab;
  var h7 = 0x5be0cd19;

  final padded = <int>[...message, 0x80];
  while (padded.length % 64 != 56) {
    padded.add(0);
  }
  final bitLen = message.length * 8;
  for (var i = 7; i >= 0; i--) {
    padded.add((bitLen >> (8 * i)) & 0xff);
  }

  final w = List<int>.filled(64, 0);
  for (var off = 0; off < padded.length; off += 64) {
    for (var i = 0; i < 16; i++) {
      w[i] = ((padded[off + i * 4] << 24) |
              (padded[off + i * 4 + 1] << 16) |
              (padded[off + i * 4 + 2] << 8) |
              padded[off + i * 4 + 3]) &
          0xffffffff;
    }
    for (var i = 16; i < 64; i++) {
      final s0 = _rotr(w[i - 15], 7) ^ _rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
      final s1 = _rotr(w[i - 2], 17) ^ _rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) & 0xffffffff;
    }
    var a = h0, b = h1, c = h2, d = h3;
    var e = h4, f = h5, g = h6, h = h7;
    for (var i = 0; i < 64; i++) {
      final s1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25);
      final ch = ((e & f) ^ ((~e) & g)) & 0xffffffff;
      final t1 = (h + s1 + ch + _k[i] + w[i]) & 0xffffffff;
      final s0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22);
      final maj = ((a & b) ^ (a & c) ^ (b & c)) & 0xffffffff;
      final t2 = (s0 + maj) & 0xffffffff;
      h = g;
      g = f;
      f = e;
      e = (d + t1) & 0xffffffff;
      d = c;
      c = b;
      b = a;
      a = (t1 + t2) & 0xffffffff;
    }
    h0 = (h0 + a) & 0xffffffff;
    h1 = (h1 + b) & 0xffffffff;
    h2 = (h2 + c) & 0xffffffff;
    h3 = (h3 + d) & 0xffffffff;
    h4 = (h4 + e) & 0xffffffff;
    h5 = (h5 + f) & 0xffffffff;
    h6 = (h6 + g) & 0xffffffff;
    h7 = (h7 + h) & 0xffffffff;
  }

  final out = Uint8List(32);
  final hs = [h0, h1, h2, h3, h4, h5, h6, h7];
  for (var i = 0; i < 8; i++) {
    out[i * 4] = (hs[i] >> 24) & 0xff;
    out[i * 4 + 1] = (hs[i] >> 16) & 0xff;
    out[i * 4 + 2] = (hs[i] >> 8) & 0xff;
    out[i * 4 + 3] = hs[i] & 0xff;
  }
  return out;
}

/// Lowercase hex SHA-256 of [message].
String sha256Hex(List<int> message) =>
    sha256Bytes(message)
        .map((b) => b.toRadixString(16).padLeft(2, '0'))
        .join();

/// Deterministic test payload: byte i is (seed + i) mod 256.
/// Same formula as the Python physical-test drivers (never random,
/// never downloaded). Proves binary-safety, not just UTF-8 text.
Uint8List deterministicBytes(int length, [int seed = 7]) {
  if (length <= 0) throw ArgumentError('length must be positive');
  return Uint8List.fromList(
      List<int>.generate(length, (i) => (seed + i) & 0xff));
}

/// Deterministic text payload for a direction run.
String stage15Text(String direction) =>
    'Zara Stage 15 transfer test\ndirection=$direction\n'
    'sender and recipient are bound by Core authorization.\n';

/// Bounded chunk split for uploads: [offset, length] windows of at
/// most [chunkSize] covering [total]. Pure for testing.
List<List<int>> chunkWindows(int total, int chunkSize) {
  if (total <= 0 || chunkSize <= 0) throw ArgumentError('bad window');
  final out = <List<int>>[];
  var off = 0;
  while (off < total) {
    final len = (total - off) < chunkSize ? total - off : chunkSize;
    out.add([off, len]);
    off += len;
  }
  return out;
}
