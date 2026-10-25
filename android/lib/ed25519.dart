/// Ed25519 signing (RFC 8032) — vendored minimal implementation.
///
library;

/// Zara uses Ed25519 ONLY for challenge-proof device authentication:
/// the device signs an ASCII message "METHOD path nonce" with its secret
/// seed; Core verifies against the registered public key (Python
/// `cryptography`). The exposure is tiny (one short message per
/// request), so a small vector-tested implementation is preferable to
/// expanding the dependency tree for one primitive.
///
/// Scope (deliberate): seed -> public key, signing. Point DECODING is
/// not implemented (the sign path never decodes points; Core is the
/// verifier). Pure-Dart BigInt math is slower than native (~ms/sign) —
/// acceptable at Zara's per-request cadence.
///
/// Correctness anchor: RFC 8032 test vectors (test/ed25519_test.dart).
import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:crypto/crypto.dart' as crypto;

const String _pHex =
    '7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffed';
const String _dHex =
    '52036cee2b6ffe738cc740797779e89800700a4d4141d8ab75eb4dca135978a3';
const String _lHex =
    '1000000000000000000000000000000014def9dea2f79cd65812631a5cf5d3ed';
const String _bxHex =
    '216936d3cd6e53fec0a4e231fdd6dc5c692cc7609525a7b2c9562d608f25d51a';
const String _byHex =
    '6666666666666666666666666666666666666666666666666666666666666658';

final BigInt _p = BigInt.parse(_pHex, radix: 16);
final BigInt _d = BigInt.parse(_dHex, radix: 16);
final BigInt _l = BigInt.parse(_lHex, radix: 16);
final BigInt _mask8 = BigInt.from(0xff);

BigInt _mod(BigInt x) => x % _p;

BigInt _powMod(BigInt base, BigInt exp) {
  var r = BigInt.one;
  var b = _mod(base);
  var e = exp;
  while (e > BigInt.zero) {
    if (e.isOdd) r = _mod(r * b);
    b = _mod(b * b);
    e = e >> 1;
  }
  return r;
}

BigInt _inv(BigInt x) => _powMod(x, _p - BigInt.two);

class _Pt {
  final BigInt x, y, z, t;
  const _Pt(this.x, this.y, this.z, this.t);
  static final _Pt identity =
      _Pt(BigInt.zero, BigInt.one, BigInt.one, BigInt.zero);
  static final _Pt base = _Pt(
      BigInt.parse(_bxHex, radix: 16),
      BigInt.parse(_byHex, radix: 16),
      BigInt.one,
      BigInt.parse(_bxHex, radix: 16) * BigInt.parse(_byHex, radix: 16) % _p);
}

_Pt _add(_Pt P, _Pt Q) {
  // Unified twisted-Edwards (a=-1) addition — also correct for doubling.
  final a = _mod((P.y - P.x) * (Q.y - Q.x));
  final b = _mod((P.y + P.x) * (Q.y + Q.x));
  final c = _mod(P.t * BigInt.two * _d * Q.t);
  final dd = _mod(P.z * BigInt.two * Q.z);
  final e = _mod(b - a);
  final f = _mod(dd - c);
  final g = _mod(dd + c);
  final h = _mod(b + a);
  return _Pt(_mod(e * f), _mod(g * h), _mod(f * g), _mod(e * h));
}

_Pt _scalarMul(BigInt k, _Pt p) {
  // LSB-first double-and-add: r accumulates k*q with q = 2^i*B.
  // (timing is not a Zara concern: the secret operand is per-request
  // local data on a single-user laptop/phone)
  if (k == BigInt.zero) return _Pt.identity;
  var r = _Pt.identity;
  var q = p;
  var v = k;
  while (v > BigInt.zero) {
    if (v.isOdd) r = _add(r, q);
    q = _add(q, q);
    v = v >> 1;
  }
  return r;
}

Uint8List _encodePoint(_Pt pt) {
  final zi = _inv(pt.z);
  final x = _mod(pt.x * zi);
  final y = _mod(pt.y * zi);
  final out = Uint8List(32);
  var yi = y;
  for (var i = 0; i < 32; i++) {
    out[i] = (yi & _mask8).toInt();
    yi = yi >> 8;
  }
  out[31] |= (x.isOdd ? 0x80 : 0x00);
  return out;
}

Uint8List _scalarLE(BigInt s) {
  final out = Uint8List(32);
  var v = s;
  for (var i = 0; i < 32; i++) {
    out[i] = (v & _mask8).toInt();
    v = v >> 8;
  }
  return out;
}

BigInt _leInt(Uint8List b) {
  var r = BigInt.zero;
  for (var i = b.length - 1; i >= 0; i--) {
    r = (r << 8) | BigInt.from(b[i]);
  }
  return r;
}

Uint8List _sha512(List<int> data) =>
    Uint8List.fromList(crypto.sha512.convert(data).bytes);

Uint8List _clamp(Uint8List h) {
  final a = Uint8List.fromList(h.sublist(0, 32));
  a[0] &= 248;
  a[31] &= 127;
  a[31] |= 64;
  return a;
}

String _hex(List<int> b) =>
    b.map((e) => e.toRadixString(16).padLeft(2, '0')).join();

Uint8List _unhex(String s) {
  final out = Uint8List(s.length ~/ 2);
  for (var i = 0; i < out.length; i++) {
    out[i] = int.parse(s.substring(2 * i, 2 * i + 2), radix: 16);
  }
  return out;
}

/// A device signing identity: 32-byte SEED (kept secret) and the
/// matching public key (registered with Core).
class Ed25519KeyPair {
  final String publicKeyHex; // 64 hex chars
  final String secretHex; // 64-hex seed — never leaves the device
  const Ed25519KeyPair({required this.publicKeyHex, required this.secretHex});
}

/// Fresh keypair from OS-secure randomness (32 bytes).
Ed25519KeyPair ed25519Generate() {
  final rng = math.Random.secure();
  final seed = Uint8List(32);
  for (var i = 0; i < 32; i++) {
    seed[i] = rng.nextInt(256);
  }
  return Ed25519KeyPair(
    publicKeyHex: _hex(ed25519PublicFromSeed(seed)),
    secretHex: _hex(seed),
  );
}

Ed25519KeyPair ed25519FromSeedHex(String seedHex) {
  final seed = _unhex(seedHex);
  return Ed25519KeyPair(
    publicKeyHex: _hex(ed25519PublicFromSeed(seed)),
    secretHex: seedHex,
  );
}

Uint8List ed25519PublicFromSeed(List<int> seed32) {
  final h = _sha512(seed32.sublist(0, 32));
  final a = _leInt(_clamp(h));
  return _encodePoint(_scalarMul(a, _Pt.base));
}

/// Sign an ASCII message ("METHOD path nonce") with a hex seed.
/// Returns the base64-encoded 64-byte signature Core expects.
String ed25519SignB64(String secretHex, String messageAscii) =>
    ed25519SignBytesB64(secretHex, ascii.encode(messageAscii));

/// Sign arbitrary bytes with a hex seed (test vectors use raw bytes).
String ed25519SignBytesB64(String secretHex, List<int> message) {
  final seed = _unhex(secretHex);
  final h = _sha512(seed);
  final a = _leInt(_clamp(h));
  final prefix = h.sublist(32, 64);
  final r = _leInt(_sha512(<int>[...prefix, ...message])) % _l;
  final renc = _encodePoint(_scalarMul(r, _Pt.base));
  final aenc = _encodePoint(_scalarMul(a, _Pt.base));
  final k = _leInt(_sha512(<int>[...renc, ...aenc, ...message])) % _l;
  final s = (r + k * a) % _l;
  final out = Uint8List(64);
  out.setRange(0, 32, renc);
  out.setRange(32, 64, _scalarLE(s));
  return base64.encode(out);
}
