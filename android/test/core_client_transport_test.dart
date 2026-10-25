// Transport regression: request bodies with non-ASCII content (em dashes,
// accented text, emoji) must be sent as UTF-8 bytes. The platform HttpClient
// string writer defaults to latin-1 and THROWS 'invalid characters' for
// anything above code point 127 — which turned a valid tool result posting
// (a note containing an em dash) into a hung/failed send. Byte-level
// encoding is the contract; this test pins it end-to-end over a real
// loopback HttpServer (no mocking of the client internals).
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';

void main() {
  _registerTransportPolicyTests();

  test('non-ASCII request bodies are sent as UTF-8 bytes, never latin1-crash',
      () async {
    final server = await HttpServer.bind('127.0.0.1', 0);
    late String received;
    late String receivedContentType;
    server.listen((req) async {
      received = await utf8.decoder.bind(req).join();
      receivedContentType = req.headers.value('content-type') ?? '';
      req.response.statusCode = 200;
      req.response.headers.contentType = ContentType.json;
      req.response.write('{"registered":"ok"}');
      await req.response.close();
    });
    addTearDown(server.close);

    final client = CoreClient('http://127.0.0.1:${server.port}');
    // registerPush is a real posting path (device-scoped, audited).
    await client.registerPush('héllo-töken✓ — em dashes too');

    expect(receivedContentType, contains('json'));
    // The exact JSON must survive at byte level: decode the body with
    // UTF-8 (before the fix this threw on the client, or would mojibake).
    final decoded = jsonDecode(received) as Map<String, dynamic>;
    expect(decoded['token'], equals('héllo-töken✓ — em dashes too'));
  });
}

void _registerTransportPolicyTests() {
  group('transport policy guard (plain HTTP to non-local cores refused)',
      () {
    test('loopback + emulator alias + https are constructible', () {
      expect(
          () => CoreClient('http://127.0.0.1:8080'), returnsNormally);
      expect(() => CoreClient('http://localhost:8080'), returnsNormally);
      expect(() => CoreClient('http://10.0.2.2:18081'), returnsNormally);
      expect(
          () => CoreClient('https://zara.example.org'), returnsNormally);
    });

    test('plain http to a LAN/internet host throws before any request',
        () {
      expect(
          () => CoreClient('http://192.168.1.5:8080'),
          throwsA(isA<CoreException>().having((e) => e.status, 'status', -1)
              .having((e) => e.body, 'body', contains('insecure transport'))));
      expect(() => CoreClient('http://zara.example.org'),
          throwsA(isA<CoreException>()));
    });

    test('explicit allowInsecureTransport is the documented opt-out', () {
      final c = CoreClient('http://192.168.1.5:8080',
          allowInsecureTransport: true);
      expect(c.baseUrl, 'http://192.168.1.5:8080');
    });
  });
}
