// Streaming talk (Stage 16): SSE framing + client-side conversation reducer.
//
// Pure Dart (dart:convert only, no flutter): incremental UTF-8 decoding that
// tolerates chunk-split lines AND multibyte chars split across byte chunks;
// strict parse (a malformed data line throws, never silent); `: ping`
// keepalives and blank lines ignored.
//
// Event contract mirror of core/talk_stream.py (read-only, frozen):
//   POST /v1/talk/stream {text,session_id,device_id,who} (operator Bearer +
//   X-Device headers) -> text/event-stream frames
//   `event: <name>\ndata: {<single-line-json>}\n\n`
//   turn{turn_id}; delta{text}; tool{tool,phase,execution_id,device_id,
//   message}; approval{execution_id,reason}; error{message};
//   done{reply,status,turn_id,mission_id,execution_id}.
// Tools execute server-side only; this client only narrates.
import 'dart:convert';

/// SSE event names on POST /v1/talk/stream (frozen contract).
const talkStreamEvents = <String>{
  'turn',
  'delta',
  'tool',
  'approval',
  'error',
  'done',
};

/// One parsed SSE frame: event name + single-line JSON object payload.
class TalkStreamEvent {
  final String name;
  final Map<String, dynamic> payload;
  const TalkStreamEvent(this.name, this.payload);
}

/// Exact wire body for POST /v1/talk/stream (core/app.py TalkIn).
/// Pure for testing. Empty text would be a Core 422, so fail fast.
Map<String, dynamic> buildTalkStreamBody({
  required String text,
  String sessionId = '',
  String deviceId = '',
  String who = 'user',
}) {
  if (text.isEmpty) throw ArgumentError('text must not be empty');
  return {
    'text': text,
    'session_id': sessionId,
    'device_id': deviceId,
    'who': who,
  };
}

/// Exact wire body for POST /v1/talk/cancel (core/app.py TalkCancelIn).
/// Pure for testing.
Map<String, dynamic> buildTalkCancelBody(String turnId) {
  if (turnId.isEmpty) throw ArgumentError('turn_id must not be empty');
  return {'turn_id': turnId};
}

/// Incremental SSE parser over raw response bytes.
///
/// Feed network chunks via [addBytes]; each call returns the newly completed
/// events. Partial lines stay buffered; a trailing incomplete UTF-8 sequence
/// stays buffered until the next chunk completes it. Call [flush] at stream
/// end: leftover bytes or a non-blank text remainder throw FormatException
/// (truncated event = disconnect evidence, never silent).
///
/// Strictness (never silent): unknown event names, `data:` without a
/// preceding `event:`, unparseable JSON, non-object payloads, `event:` with
/// no data, and invalid UTF-8 all throw FormatException immediately.
/// `:<comment>` lines (server `: ping` keepalives), blank lines, and
/// unrelated SSE fields (`id:`, `retry:`) are ignored per SSE tolerance.
class SseParser {
  final List<int> _pending = <int>[];
  String _text = '';
  String? _event;
  bool _hasData = false;

  /// Decode [chunk] (holding back a trailing incomplete UTF-8 sequence)
  /// and return newly completed events. Throws FormatException on any
  /// malformed frame.
  List<TalkStreamEvent> addBytes(List<int> chunk) {
    _pending.addAll(chunk);
    final cut = _completePrefixLength(_pending);
    final decodable = _pending.sublist(0, cut);
    _pending.removeRange(0, cut);
    late final String s;
    try {
      s = utf8.decode(decodable, allowMalformed: false);
    } on FormatException catch (e) {
      throw FormatException('invalid UTF-8 in talk stream: $e');
    }
    _text += s;
    return _drainLines();
  }

  /// End-of-stream: emit nothing new (events dispatch on blank lines while
  /// streaming) but fail loudly on leftovers. Returns empty on clean end.
  List<TalkStreamEvent> flush() {
    if (_pending.isNotEmpty) {
      throw const FormatException('truncated UTF-8 at talk-stream end');
    }
    final out = _drainLines();
    if (_event != null || _hasData || _text.trim().isNotEmpty) {
      throw const FormatException(
          'incomplete trailing SSE data (disconnect?)');
    }
    return out;
  }

  List<TalkStreamEvent> _drainLines() {
    final out = <TalkStreamEvent>[];
    while (true) {
      final nl = _text.indexOf('\n');
      if (nl < 0) break;
      var line = _text.substring(0, nl);
      _text = _text.substring(nl + 1);
      if (line.endsWith('\r')) {
        line = line.substring(0, line.length - 1);
      }
      if (line.isEmpty) {
        _dispatchBlank(out);
        continue;
      }
      if (line.startsWith(':')) continue; // `: ping` keepalive / comment
      if (line.startsWith('event:')) {
        final name = line.substring('event:'.length).trim();
        if (!talkStreamEvents.contains(name)) {
          throw FormatException('unknown talk-stream event: $name');
        }
        _event = name;
        _hasData = false;
        continue;
      }
      if (line.startsWith('data:')) {
        final name = _event;
        if (name == null) {
          throw const FormatException('data without preceding event');
        }
        if (_hasData) {
          throw const FormatException('duplicate data line for event');
        }
        final raw = line.substring('data:'.length).trim();
        late final Object? decoded;
        try {
          decoded = jsonDecode(raw);
        } on FormatException catch (e) {
          throw FormatException('malformed SSE data: $e');
        }
        if (decoded is! Map) {
          throw const FormatException('SSE payload must be an object');
        }
        out.add(TalkStreamEvent(name, Map<String, dynamic>.from(decoded)));
        _event = null;
        _hasData = false;
        continue;
      }
      // `id:`, `retry:`, and any other SSE field: ignored per spec.
    }
    return out;
  }

  void _dispatchBlank(List<TalkStreamEvent> out) {
    // Blank line terminates a frame. Frames always carry data here, so a
    // dangling `event:` with nothing after it is malformed, not ignorable.
    if (_event == null && !_hasData) return; // consecutive blank lines
    throw const FormatException('event without data');
  }
}

/// Length of the longest prefix of [bytes] that ends on a UTF-8 character
/// boundary. A trailing incomplete multibyte sequence is held back for the
/// next chunk; definitely-invalid bytes are NOT held back (decode throws
/// now rather than later).
int _completePrefixLength(List<int> bytes) {
  final n = bytes.length;
  var cont = 0;
  var i = n - 1;
  while (i >= 0 && bytes[i] >= 0x80 && bytes[i] <= 0xBF) {
    cont++;
    i--;
  }
  if (cont == 0) {
    if (n == 0) return 0;
    final last = bytes[n - 1];
    if (last < 0x80) return n;
    // A lead byte with no continuation yet: incomplete — unless it can
    // never start a sequence (then fail now, not on flush).
    if ((last >= 0xC0 && last <= 0xC1) || last >= 0xF5) return n;
    return n - 1;
  }
  if (i < 0) return 0; // only continuation bytes so far: hold all
  final lead = bytes[i];
  if (lead < 0x80) return n; // ASCII followed by continuations: invalid
  int expected;
  if (lead >= 0xC2 && lead <= 0xDF) {
    expected = 2;
  } else if (lead >= 0xE0 && lead <= 0xEF) {
    expected = 3;
  } else if (lead >= 0xF0 && lead <= 0xF4) {
    expected = 4;
  } else {
    return n; // invalid lead: let decode throw now
  }
  return (cont + 1 < expected) ? i : n;
}

/// One tool-activity row narrated by a `tool` event. Display only — the
/// tool already ran (or was gated) server-side.
class TalkToolActivity {
  final String tool;
  final String phase;
  final String executionId;
  final String deviceId;
  final String message;
  const TalkToolActivity({
    required this.tool,
    required this.phase,
    required this.executionId,
    required this.deviceId,
    required this.message,
  });

  static TalkToolActivity fromPayload(Map<String, dynamic> p) =>
      TalkToolActivity(
        tool: '${p['tool'] ?? ''}',
        phase: '${p['phase'] ?? ''}',
        executionId: '${p['execution_id'] ?? ''}',
        deviceId: '${p['device_id'] ?? ''}',
        message: '${p['message'] ?? ''}',
      );
}

/// Client-side reducer for one streamed turn. Presentation state only.
///
/// Rules: deltas append incrementally; `done` is terminal and wins exactly
/// once per turn_id (late/duplicate `done` frames are ignored, so a resent
/// or duplicated stream can never double-apply a reply); after `done`,
/// further delta/tool/approval frames are ignored. `disconnected` is NOT
/// terminal — the UI must offer explicit reconnect(new turn)-or-cancel.
/// The client never re-sends on its own: tools execute server-side.
class TalkConversation {
  String? turnId;
  String reply = '';
  final List<TalkToolActivity> tools = [];
  String? approvalExecutionId;
  String approvalReason = '';
  String status = 'streaming'; // streaming|done|error|disconnected|cancelled
  String error = '';
  String doneReply = '';
  String doneStatus = '';
  String missionId = '';
  String executionId = '';
  int doneCount = 0;
  final Set<String> _seenDoneTurns = <String>{};

  bool get isTerminal =>
      status == 'done' || status == 'error' || status == 'cancelled';
  bool get isDone => status == 'done';

  void apply(TalkStreamEvent e) {
    switch (e.name) {
      case 'turn':
        {
          if (isTerminal) return;
          final t = e.payload['turn_id'];
          if (t is String && t.isNotEmpty) turnId ??= t;
          return;
        }
      case 'delta':
        {
          if (isTerminal) return;
          final t = e.payload['text'];
          if (t is String) reply += t;
          return;
        }
      case 'tool':
        {
          if (isTerminal) return;
          tools.add(TalkToolActivity.fromPayload(e.payload));
          return;
        }
      case 'approval':
        {
          if (isTerminal) return;
          final id = e.payload['execution_id'];
          if (id is String && id.isNotEmpty) approvalExecutionId = id;
          final r = e.payload['reason'];
          if (r is String) approvalReason = r;
          return;
        }
      case 'error':
        {
          // Terminal `done`/`cancelled` win over late errors: a settled
          // turn never flips back to an error state.
          if (status == 'done' || status == 'cancelled') return;
          status = 'error';
          final m = e.payload['message'];
          error =
              (m is String && m.isNotEmpty) ? m : 'stream failed (no detail)';
          return;
        }
      case 'done':
        {
          // Terminal states are sticky: once done/error/cancelled, late
          // frames (including a duplicate `done`) are ignored, so a resent
          // or duplicated stream can never double-apply a reply or revive
          // a cancelled turn.
          if (isTerminal) return;
          final t = e.payload['turn_id'];
          final tid = t is String ? t : '';
          if (tid.isNotEmpty) {
            if (_seenDoneTurns.contains(tid)) return; // duplicate: ignore
            _seenDoneTurns.add(tid);
          }
          doneCount += 1;
          status = 'done';
          final r = e.payload['reply'];
          if (r is String && r.isNotEmpty) {
            doneReply = r;
            reply = r;
          }
          final s = e.payload['status'];
          if (s is String) doneStatus = s;
          final m = e.payload['mission_id'];
          if (m is String) missionId = m;
          final x = e.payload['execution_id'];
          if (x is String) executionId = x;
          return;
        }
      default:
        return; // unreachable: parser rejects unknown names
    }
  }

  /// Transport ended before `done`: explicit non-terminal state. The UI
  /// must show reconnect-as-new-turn or cancel — never silent, never auto.
  void markDisconnected([String message = '']) {
    if (isTerminal) return;
    status = 'disconnected';
    if (message.isNotEmpty) error = message;
  }

  void markCancelled() {
    if (status == 'done') return;
    status = 'cancelled';
  }

  void markError(String message) {
    if (status == 'done') return;
    status = 'error';
    error = message;
  }
}

/// One-line, explicit rendering of talk-stream failures. HTTP statuses keep
/// their meaning (401/403 token, 404 unknown, 409 conflict); parse failures
/// and transport errors render distinctly. Never empty, never silent.
String describeTalkStreamError(Object e) {
  final s = e.toString();
  if (s.contains('401') || s.contains('403')) {
    return 'unauthorized/forbidden (operator token missing/wrong)';
  }
  if (s.contains('404')) return 'not found (talk endpoint unknown)';
  if (s.contains('409')) return 'conflict (turn already settled elsewhere)';
  if (s.contains('FormatException')) {
    final oneLine = s.replaceAll('\n', ' ');
    return 'stream parse error: ${oneLine.length <= 160 ? oneLine : oneLine.substring(0, 160)}';
  }
  final oneLine = s.replaceAll('\n', ' ');
  final short = oneLine.length <= 160 ? oneLine : oneLine.substring(0, 160);
  return 'stream transport: $short';
}
