// RFC 8032 test vectors (ed25519.cr.yp.to/sign.input) for the vendored
// Ed25519 implementation, plus the challenge-proof wire shape Core
// (Python `cryptography`) verifies. These MUST run in CI: a broken or
// drifted signer silently locks the device out of its Core.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/ed25519.dart';

void main() {
  // (seed, public key, message, signature) — all hex.
  const vectors = <List<String>>[
    [
      '9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60',
      'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a',
      '',
      'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b',
    ],
    [
      '4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb',
      '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c',
      '72',
      '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00',
    ],
    [
      'c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7',
      'fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025',
      'af82',
      '6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a',
    ],
  ];

  test('RFC 8032 vectors: public key derivation', () {
    for (final v in vectors) {
      final kp = ed25519FromSeedHex(v[0]);
      expect(kp.publicKeyHex, v[1], reason: 'pubkey derivation for ${v[0]}');
    }
  });

  test('RFC 8032 vectors: signatures', () {
    for (final v in vectors) {
      final sig = ed25519SignBytesB64(v[0], hexToBytes(v[2]));
      expect(base64.decode(sig), hexToBytes(v[3]),
          reason: 'signature for message ${v[2]}');
    }
  });

  test('challenge-proof message shape is stable', () {
    // Core signs/verifies f"{METHOD} {path} {nonce}" — byte-exact.
    final kp = ed25519Generate();
    expect(kp.publicKeyHex.length, 64);
    expect(kp.secretHex.length, 64);
    final msg = 'POST /v1/agent/heartbeat N-O-N-C-E';
    final sig = ed25519SignB64(kp.secretHex, msg);
    expect(base64.decode(sig).length, 64);
    // determinism: RFC 8032 signatures are deterministic
    expect(ed25519SignB64(kp.secretHex, msg), sig);
  });
}

List<int> hexToBytes(String hex) {
  final out = List<int>.filled(hex.length ~/ 2, 0);
  for (var i = 0; i < out.length; i++) {
    out[i] = int.parse(hex.substring(2 * i, 2 * i + 2), radix: 16);
  }
  return out;
}
