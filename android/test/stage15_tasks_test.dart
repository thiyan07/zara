// Stage 15b: task submission + execution status + cancellation, wired to
// the REAL Core REST contracts (core/app.py, read-only):
//   POST /v1/executions {tool,inputs,device_id} -> 201/202 view
//   GET  /v1/executions/{id} -> {status,job_state,result,error,...}
//   POST /v1/executions/{id}/cancel (404 unknown, 409 terminal)
//   POST /v1/executions/resolve {capability,...}
// Client shape/auth tests run against a loopback HttpServer (no mocks of
// the client itself). Pure helpers (JSON validation, view parsing, error
// mapping) are tested directly.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/core_client.dart';
import 'package:zara_android/connection.dart';
import 'package:zara_android/main.dart';

/// Captured wire request + canned response for the loopback stub.
class _Stub {
  String method = '';
  String path = '';
  final headers = <String, String>{};
  Map<String, dynamic> body = {};
  int status = 200;
  Map<String, dynamic> reply = {};
}

Future<HttpServer> _serve(_Stub stub) async {
  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
  server.listen((req) async {
    stub.method = req.method;
    stub.path = req.uri.path;
    req.headers.forEach((k, v) => stub.headers[k] = v.join(','));
    final text = await utf8.decoder.bind(req).join();
    stub.body = text.isEmpty
        ? <String, dynamic>{}
        : (jsonDecode(text) as Map<String, dynamic>);
    req.response.statusCode = stub.status;
    req.response.headers.contentType = ContentType.json;
    req.response.write(jsonEncode(stub.reply));
    await req.response.close();
  });
  return server;
}

CoreClient _client(HttpServer s) {
  final c = CoreClient('http://127.0.0.1:${s.port}',
      timeout: const Duration(seconds: 5));
  c.deviceId = 'android-phone';
  c.deviceKey = 'k1';
  c.operatorToken = 'op-token';
  return c;
}

void main() {
  group('submitExecution wire shape', () {
    test('POST /v1/executions with tool/inputs/device_id + auth headers',
        () async {
      final stub = _Stub()
        ..reply = {
          'id': 'ex-1',
          'tool': 'util.echo',
          'device_id': 'android-phone',
          'status': 'running',
          'job_state': 'pending',
          'result': null,
          'error': '',
          'approval_required': false,
        };
      final server = await _serve(stub);
      try {
        final out = await _client(server).submitExecution(
            tool: 'util.echo',
            inputs: {
              'text': 'hi'
            },
            deviceId: 'android-phone');
        expect(out['id'], 'ex-1');
        expect(stub.method, 'POST');
        expect(stub.path, '/v1/executions');
        expect(stub.body['tool'], 'util.echo');
        expect(stub.body['inputs'], {'text': 'hi'});
        expect(stub.body['device_id'], 'android-phone');
        expect(stub.body['who'], 'user');
        expect(stub.headers['x-device-id'], 'android-phone');
        expect(stub.headers['x-device-key'], 'k1');
        expect(stub.headers['authorization'], 'Bearer op-token');
      } finally {
        await server.close(force: true);
      }
    });

    test('no operator token => no Authorization header (Core 401s, explicit)',
        () async {
      final stub = _Stub()..reply = {'id': 'ex-1', 'status': 'running'};
      final server = await _serve(stub);
      try {
        final c = _client(server)..operatorToken = null;
        await c.submitExecution(
            tool: 'system.battery', inputs: {}, deviceId: 'android-phone');
        expect(stub.headers.containsKey('authorization'), isFalse);
        expect(stub.headers['x-device-id'], 'android-phone');
      } finally {
        await server.close(force: true);
      }
    });

    test('empty tool/deviceId fail fast (never sent)', () async {
      final stub = _Stub()..reply = {};
      final server = await _serve(stub);
      try {
        final c = _client(server);
        expect(
            () => c.submitExecution(
                tool: '', inputs: {}, deviceId: 'android-phone'),
            throwsArgumentError);
        expect(
            () => c.submitExecution(
                tool: 'util.echo', inputs: {}, deviceId: ''),
            throwsArgumentError);
        expect(stub.method, isEmpty); // nothing hit the wire
      } finally {
        await server.close(force: true);
      }
    });
  });

  group('executionStatus + cancelExecution wire shape', () {
    test('GET /v1/executions/{id} carries auth headers', () async {
      final stub = _Stub()
        ..reply = {
          'id': 'ex-9',
          'status': 'succeeded',
          'job_state': 'done',
          'result': {'ok': true},
          'error': '',
          'approval_required': false,
        };
      final server = await _serve(stub);
      try {
        final out = await _client(server).executionStatus('ex-9');
        expect(stub.method, 'GET');
        expect(stub.path, '/v1/executions/ex-9');
        expect(stub.headers['authorization'], 'Bearer op-token');
        expect(out['status'], 'succeeded');
      } finally {
        await server.close(force: true);
      }
    });

    test('cancel POSTs to /v1/executions/{id}/cancel', () async {
      final stub = _Stub()
        ..reply = {
          'id': 'ex-2',
          'status': 'cancelled',
          'job_state': 'cancelled',
          'result': null,
          'error': 'cancelled',
          'approval_required': false,
          'cancel_requested': true,
        };
      final server = await _serve(stub);
      try {
        final out = await _client(server).cancelExecution('ex-2');
        expect(stub.method, 'POST');
        expect(stub.path, '/v1/executions/ex-2/cancel');
        expect(stub.body, isEmpty);
        expect(stub.headers['authorization'], 'Bearer op-token');
        expect(out['status'], 'cancelled');
      } finally {
        await server.close(force: true);
      }
    });

    test('resolveExecution posts capability envelope', () async {
      final stub = _Stub()
        ..reply = {'id': 'ex-3', 'status': 'running'};
      final server = await _serve(stub);
      try {
        await _client(server)
            .resolveExecution(capability: 'system.battery', deviceId: 'd1');
        expect(stub.method, 'POST');
        expect(stub.path, '/v1/executions/resolve');
        expect(stub.body['capability'], 'system.battery');
        expect(stub.body['device_id'], 'd1');
        expect(stub.body['allow_fallback'], isTrue);
      } finally {
        await server.close(force: true);
      }
    });

    test('pre-cancelled scope short-circuits submit/status/cancel',
        () async {
      final stub = _Stub()..reply = {};
      final server = await _serve(stub);
      try {
        final c = _client(server);
        final scope = CancelScope()..cancel();
        for (final f in [
          () => c.submitExecution(
              tool: 'util.echo', inputs: {}, deviceId: 'd', cancel: scope),
          () => c.executionStatus('ex-1', cancel: scope),
          () => c.cancelExecution('ex-1', cancel: scope),
        ]) {
          try {
            await f();
            fail('expected CoreException(-1)');
          } on CoreException catch (e) {
            expect(e.status, -1);
          }
        }
        expect(stub.method, isEmpty);
      } finally {
        await server.close(force: true);
      }
    });
  });

  group('status-code mapping (404/409/422/401)', () {
    Future<CoreException> capture(
        _Stub stub, Future<void> Function(CoreClient) call) async {
      final server = await _serve(stub);
      try {
        await call(_client(server));
      } on CoreException catch (e) {
        return e;
      } finally {
        await server.close(force: true);
      }
      fail('expected an error');
    }

    test('404 unknown execution -> NotRegisteredException + notFound',
        () async {
      final e = await capture(
          _Stub()
            ..status = 404
            ..reply = {'detail': 'unknown execution'},
          (c) => c.executionStatus('nope'));
      expect(e, isA<NotRegisteredException>());
      expect(isExecutionNotFound(e), isTrue);
      expect(isExecutionTerminalConflict(e), isFalse);
      expect(isExecutionRejected(e), isFalse);
    });

    test('409 cancel on terminal -> conflict, kept visible', () async {
      final e = await capture(
          _Stub()
            ..status = 409
            ..reply = {'detail': 'already terminal: succeeded'},
          (c) => c.cancelExecution('ex-done'));
      expect(e, isA<CoreException>());
      expect(isExecutionTerminalConflict(e), isTrue);
      expect(isExecutionNotFound(e), isFalse);
    });

    test('422 bad inputs -> rejected, nothing created', () async {
      final e = await capture(
          _Stub()
            ..status = 422
            ..reply = {'detail': 'inputs too large'},
          (c) => c.submitExecution(
              tool: 'util.echo', inputs: {}, deviceId: 'd'));
      expect(isExecutionRejected(e), isTrue);
      expect(isExecutionNotFound(e), isFalse);
    });

    test('401 bad token -> AuthException', () async {
      final e = await capture(
          _Stub()
            ..status = 401
            ..reply = {'detail': 'invalid token'},
          (c) => c.executionStatus('ex-1'));
      expect(e, isA<AuthException>());
    });
  });

  group('parseTaskInputs JSON validation', () {
    test('blank => {} and valid object passes through', () {
      expect(parseTaskInputs(''), isEmpty);
      expect(parseTaskInputs('   '), isEmpty);
      expect(parseTaskInputs('{}'), isEmpty);
      expect(parseTaskInputs('{"text": "hi", "n": 3}'),
          {'text': 'hi', 'n': 3});
    });

    test('syntax errors and non-objects throw FormatException', () {
      expect(() => parseTaskInputs('{oops'), throwsFormatException);
      expect(() => parseTaskInputs('not json'), throwsFormatException);
      expect(() => parseTaskInputs('[1, 2]'), throwsFormatException);
      expect(() => parseTaskInputs('"str"'), throwsFormatException);
      expect(() => parseTaskInputs('42'), throwsFormatException);
    });
  });

  group('ExecutionView parsing', () {
    test('full status envelope incl. approval flag', () {
      final v = ExecutionView.fromJson({
        'id': 'ex-7',
        'tool': 'system.battery',
        'device_id': 'android-phone',
        'status': 'waiting_for_permission',
        'job_id': 'job-1',
        'job_state': 'pending',
        'result': null,
        'error': '',
        'verified': false,
        'approval_required': true,
        'cancel_requested': false,
      });
      expect(v.id, 'ex-7');
      expect(v.status, 'waiting_for_permission');
      expect(v.jobState, 'pending');
      expect(v.needsApproval, isTrue);
      expect(v.isTerminal, isFalse);
      expect(v.isRunning, isFalse);
    });

    test('minimal envelope defaults sane; terminal matrix', () {
      final v = ExecutionView.fromJson({'id': 'a', 'status': 'running'});
      expect(v.tool, isEmpty);
      expect(v.jobState, isNull);
      expect(v.result, isNull);
      expect(v.error, isEmpty);
      expect(v.approvalRequired, isFalse);
      expect(v.isRunning, isTrue);
      for (final s in [
        'succeeded',
        'failed',
        'timed_out',
        'denied',
        'cancelled'
      ]) {
        expect(isExecutionTerminal(s), isTrue, reason: s);
      }
      for (final s in ['pending', 'running', 'waiting_for_permission']) {
        expect(isExecutionTerminal(s), isFalse, reason: s);
      }
    });

    test('deferred gate reply (no id) is NOT a view', () {
      expect(() => ExecutionView.fromJson({'status': 'deferred'}),
          throwsFormatException);
      expect(() => ExecutionView.fromJson({'id': 'x'}),
          throwsFormatException);
      expect(() => ExecutionView.fromJson({}), throwsFormatException);
    });
  });

  group('request-body builders', () {
    test('submit body matches ExecutionSubmitIn', () {
      expect(
          buildExecutionSubmitBody(
              tool: 'util.echo',
              inputs: {'text': 'hi'},
              deviceId: 'd1'),
          {'tool': 'util.echo', 'inputs': {'text': 'hi'}, 'device_id': 'd1', 'who': 'user'});
      expect(
          buildExecutionSubmitBody(
              tool: 't', inputs: {}, deviceId: 'd', missionId: 'm1')['mission_id'],
          'm1');
      expect(() => buildExecutionSubmitBody(tool: '', deviceId: 'd'),
          throwsArgumentError);
      expect(() => buildExecutionSubmitBody(tool: 't', deviceId: ''),
          throwsArgumentError);
    });

    test('resolve body matches ExecutionResolveIn', () {
      final b = buildExecutionResolveBody(
          capability: 'system.battery', deviceId: 'd1');
      expect(b['capability'], 'system.battery');
      expect(b['allow_fallback'], isTrue);
      expect(
          () => buildExecutionResolveBody(capability: ''),
          throwsArgumentError);
    });

    test('idempotency keys are unique per action, valid envelope', () {
      final a = newIdempotencyKey('util.echo');
      final b = newIdempotencyKey('util.echo');
      expect(a, isNot(equals(b)));
      final re = RegExp(r'^[A-Za-z0-9_.:\-]{1,128}$');
      expect(re.hasMatch(a), isTrue);
      expect(re.hasMatch(b), isTrue);
      final body = buildExecutionSubmitBody(
          tool: 'util.echo', inputs: {}, deviceId: 'd',
          idempotencyKey: a);
      expect(body['idempotency_key'], a);
      final rbody = buildExecutionResolveBody(
          capability: 'system.battery', deviceId: 'd',
          idempotencyKey: b);
      expect(rbody['idempotency_key'], b);
      // Absent by default (server treats as no-dedupe).
      expect(
          buildExecutionSubmitBody(tool: 't', deviceId: 'd')
              .containsKey('idempotency_key'),
          isFalse);
    });
  });

  group('describeExecutionError explicit states', () {
    test('404/409/422/401 each render distinctly, never silent', () {
      expect(describeExecutionError(const CoreException(404, 'x')),
          contains('not found'));
      expect(describeExecutionError(const CoreException(409, 'x')),
          contains('conflict'));
      expect(describeExecutionError(const CoreException(422, 'x')),
          contains('rejected'));
      expect(describeExecutionError(const AuthException(401, 'x')),
          contains('unauthorized'));
      expect(describeExecutionError(const CoreException(403, 'x')),
          contains('unauthorized'));
      expect(describeExecutionError(const CoreException(500, 'x')),
          isNotEmpty);
    });
  });

  group('capability-routed submit decision (taskShouldResolve)', () {
    test('system.* resolve when toggle on, direct when off', () {
      expect(
          taskShouldResolve(
              tool: 'system.battery', resolveEnabled: true),
          isTrue);
      expect(
          taskShouldResolve(
              tool: 'system.network', resolveEnabled: true),
          isTrue);
      expect(
          taskShouldResolve(
              tool: 'system.battery', resolveEnabled: false),
          isFalse);
    });

    test('util.echo never resolves (local tool, no capability route)',
        () {
      expect(taskShouldResolve(tool: 'util.echo', resolveEnabled: true),
          isFalse);
      expect(taskShouldResolve(tool: 'util.echo', resolveEnabled: false),
          isFalse);
    });

    test('unknown tools never resolve', () {
      expect(taskShouldResolve(tool: 'gui.tap', resolveEnabled: true),
          isFalse);
      expect(taskShouldResolve(tool: '', resolveEnabled: true), isFalse);
    });
  });

  group('duplicate-submission guard key (taskSubmitKey)', () {
    test('same tool+inputs share a key regardless of map order', () {
      final a = taskSubmitKey('system.battery', {'b': 2, 'a': 1});
      final b = taskSubmitKey('system.battery', {'a': 1, 'b': 2});
      expect(a, b);
    });

    test('different tools or inputs are distinct keys', () {
      final base = taskSubmitKey('system.battery', {});
      expect(taskSubmitKey('system.network', {}), isNot(base));
      expect(taskSubmitKey('system.battery', {'x': 1}), isNot(base));
      expect(taskSubmitKey('util.echo', {'text': 'hi'}),
          isNot(taskSubmitKey('util.echo', {'text': 'bye'})));
    });

    test('set semantics collapse a rapid double-tap', () {
      final keys = <String>{};
      final key = taskSubmitKey('system.battery', {});
      expect(keys.add(key), isTrue); // first tap proceeds
      expect(keys.add(key), isFalse); // second tap drops
    });
  });
}
