// Streaming conversation panel (Stage 16): POST /v1/talk/stream (SSE)
// with the non-streaming /v1/talk fallback.
//
// One assistant flow with the Tasks + Approval sections above: user and
// assistant turns render as matching Card + dense ListTile bubbles
// (titleSmall labels, bodyMedium text), tool rows carry the same
// execution ids the Tasks section tracks, and the approval pointer uses
// the same orange wording. Incremental text renders as `delta` events
// arrive; `done` reconciles into the same bubble (replaces, never
// appends), so streaming text is never duplicated by the done payload.
// `approval` events plant a pointer AND push a DeviceNotification into
// the shared approval flow (approved/denied there, device-scoped through
// Core — never local execution). `done` is terminal and applied once per
// turn_id (late/duplicate `done` ignored; terminal states are sticky, so
// cancel can never be revived by a late frame). Disconnects show
// explicit reconnect-as-new-turn or cancel — no silent retry loops, and
// the client never re-sends on its own (tools run once per server turn).
// Rapid double-taps collapse via talkSendKey (same concept as the Tasks
// taskSubmitKey guard). Teardown is best-effort and never awaited, so
// controls stay responsive.
//
// History resume (HistoryPanel Continue -> main.dart -> here): the panel
// takes an optional initial `sessionId` (first send reuses it, server ids
// adopted afterwards as before) and [TalkStreamPanelState.loadHistory]
// renders the prior transcript read-only above the live turn (marked
// historical, redacted, never resubmitted, never executed — zero backend
// calls). A 404 against a resumed id means the session was deleted
// elsewhere: the panel drops to a fresh session with an explicit
// "previous session deleted, started new" notice (single retry, never a
// loop); the resumed transcript stays visible.
import 'dart:async';

import 'package:flutter/material.dart';

import 'core_client.dart';
import 'history_view.dart' show redactHistoryText;
import 'stream_client.dart';

/// Transport behind [TalkStreamPanel]: stream open, non-streaming fallback,
/// and stream cancel. Production runs everything on the shared [CoreClient]
/// (operator Bearer + X-Device auth, bounded timeouts); widget tests inject
/// a fake (the flutter_tester cannot hold loopback servers open, so panel
/// tests must stay socket-free — wire behavior is covered by loopback
/// tests against [CoreClient] directly).
class TalkBackend {
  final CoreClient client;
  const TalkBackend(this.client);

  Future<TalkStreamHandle> openStream(
          {required String text,
          required String deviceId,
          String sessionId = ''}) =>
      client.startTalkStream(
          text: text, deviceId: deviceId, sessionId: sessionId);

  Future<Map<String, dynamic>> fallback(
          {required String text,
          required String deviceId,
          String sessionId = ''}) =>
      client.talk(text: text, deviceId: deviceId, sessionId: sessionId);

  Future<Map<String, dynamic>?> cancelStream(TalkStreamHandle handle) =>
      handle.cancel(client);
}

/// In-flight key for talk sends: the normalized prompt text. Two rapid
/// taps with the same text collapse to one key (second tap is dropped with
/// an explicit notice); different text is a distinct key. Same concept as
/// [taskSubmitKey] in main.dart (tool + canonical inputs there, prompt
/// text here — the talk path has no tool/inputs envelope). Pure for
/// testing. Never logs the text itself (key is length + hash only, so the
/// prompt never lands in logs via this key).
String talkSendKey(String text) {
  final t = text.trim();
  return 'talk:${t.length}:${t.hashCode}';
}

/// Short execution id for tool-progress rows (matches the Tasks section
/// truncation so rows can be correlated across sections).
String shortExecId(String id) =>
    id.length <= 12 ? id : '${id.substring(0, 12)}…';

/// One read-only historical turn rendered above the live turn after a
/// history resume. Display only: never resubmitted, never executed, never
/// fed to the approval flow. Text is stored already-redacted.
class TalkHistoryRow {
  final String role;
  final String text;
  final String tool;
  const TalkHistoryRow({required this.role, required this.text, this.tool = ''});
}

class TalkStreamPanel extends StatefulWidget {
  final CoreClient client;
  final TextEditingController tokenCtrl; // shared operator token, memory only
  final void Function(String executionId, String reason) onApproval;

  /// Resume seam: when non-empty, the first send reuses this session id
  /// (server-resolved ids are adopted afterwards as before). Wired by
  /// main.dart to the HistoryPanel Continue path via [TalkStreamPanelState.loadHistory].
  final String sessionId;

  /// Test seam: production passes null (operations run on [client]).
  /// Widget tests inject a fake so no sockets are needed.
  final TalkBackend? backend;
  const TalkStreamPanel({
    super.key,
    required this.client,
    required this.tokenCtrl,
    required this.onApproval,
    this.sessionId = '',
    this.backend,
  });

  @override
  State<TalkStreamPanel> createState() => TalkStreamPanelState();
}

/// Public state so the app shell (main.dart) can drive resume through a
/// GlobalKey: set the session via the [TalkStreamPanel.sessionId] widget
/// param, then [loadHistory] the transcript. No new architecture — the
/// same widget-pattern seam as the approval callback above.
class TalkStreamPanelState extends State<TalkStreamPanel> {
  final _inputCtrl = TextEditingController();
  /// Stable conversation identity: created implicitly by the first turn
  /// (server-resolved id adopted below), reused by later turns so history
  /// accumulates in one session instead of fragmenting per turn.
  String _sessionId = '';
  bool _useStream = true;
  bool _busy = false;
  bool _usedFallback = false;
  TalkConversation _conv = TalkConversation();
  final Set<String> _notifiedApprovals = <String>{};
  TalkStreamHandle? _handle;
  StreamSubscription<TalkStreamEvent>? _sub;
  String _lastText = '';
  String _notice = '';
  // Resumed transcript: read-only rows rendered above the live turn.
  // Never resubmitted (sends only use _inputCtrl/_lastText), never
  // executed (no approval callbacks, no tool runs — display only).
  List<TalkHistoryRow> _history = [];
  // Generation counter: every loadHistory bumps [_historyGen] so a stale
  // async resume that resolves after a newer one is discarded, never
  // rendered. Synchronous loads apply immediately (newest wins by order).
  int _historyGen = 0;
  // Per-prompt in-flight guard (same concept as the Tasks section's
  // _taskInflightKeys): rapid double-taps with identical text collapse
  // into the first request. Core turn_ids remain the cross-process
  // backstop for races that cross isolates.
  final Set<String> _sendKeys = <String>{};
  String _activeSendKey = '';

  /// Release the in-flight send guard when a turn settles (done, error,
  /// disconnect-not-terminal still releases: the user must be able to
  /// resend-as-new-turn afterwards). Synchronous: called inside setState
  /// paths, never awaited, so controls stay responsive.
  void _releaseSendKey() {
    if (_activeSendKey.isNotEmpty) {
      _sendKeys.remove(_activeSendKey);
      _activeSendKey = '';
    }
  }

  TalkBackend get _backend => widget.backend ?? TalkBackend(widget.client);

  @override
  void initState() {
    super.initState();
    // Resume seam: a non-empty initial session id is reused by the first
    // send below (adopted server ids still win afterwards).
    _sessionId = widget.sessionId;
  }

  @override
  void didUpdateWidget(TalkStreamPanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    // main.dart sets the session by rebuilding with a new sessionId
    // (HistoryPanel Continue path): adopt non-empty changes so the next
    // send continues that session. Never clears an adopted id on an
    // empty rebuild — only explicit non-empty resumes switch sessions.
    if (widget.sessionId != oldWidget.sessionId &&
        widget.sessionId.isNotEmpty) {
      _sessionId = widget.sessionId;
    }
  }

  /// Load a resumed transcript for read-only display above the live turn.
  /// Each entry is {role, text, tool?, created_at?} (the HistoryPanel
  /// Continue shape). Rows are marked historical, redacted, and never
  /// resubmitted or executed — zero backend calls. Async drivers pass
  /// their [generation] (see [newHistoryGeneration]): a payload whose
  /// generation is not newer than the last applied one is stale and is
  /// discarded silently, so rapid resumes always settle on the newest.
  void loadHistory(List<Map<String, dynamic>> messages, {int? generation}) {
    final gen = generation;
    if (gen != null) {
      if (gen <= _historyGen) return; // stale: discard
      _historyGen = gen;
    } else {
      ++_historyGen;
    }
    if (!mounted) return;
    setState(() {
      _history = [
        for (final m in messages)
          TalkHistoryRow(
            role: '${m['role'] ?? ''}',
            text: redactHistoryText('${m['text'] ?? ''}'),
            tool: '${m['tool'] ?? ''}',
          ),
      ];
    });
  }

  /// Explicit generation bump for async resume drivers (main.dart): the
  /// driver bumps, fetches, then calls [loadHistory] with that generation
  /// — a superseded fetch discards itself above. Returns the generation.
  int newHistoryGeneration() => ++_historyGen;

  @override
  void dispose() {
    unawaited(_sub?.cancel());
    unawaited(_handle?.close());
    _inputCtrl.dispose();
    super.dispose();
  }

  void _applyToken() {
    final tok = widget.tokenCtrl.text.trim();
    widget.client.operatorToken = tok.isEmpty ? null : tok;
  }

  Future<void> _send({bool isResend = false}) async {
    final text = isResend ? _lastText : _inputCtrl.text.trim();
    if (text.isEmpty) return;
    // Rapid-tap guard (synchronous, before any await so a second tap in
    // the same frame still collapses): identical text while the first send
    // is in flight is dropped with an explicit notice, never a second
    // turn. Distinct text while busy is likewise held (one turn at a
    // time; the panel is single-turn by design — _conv holds one turn).
    final key = talkSendKey(text);
    if (_busy || !_sendKeys.add(key)) {
      if (mounted) {
        setState(() => _notice = 'already sending — wait for the reply');
      }
      return;
    }
    _applyToken();
    // Teardown is best-effort and never awaited: dropping the old
    // subscription/handle must not block opening the new turn (and awaiting
    // teardown futures can stall in test FakeAsync zones). The server
    // treats the disconnect as an abort; explicit cancel posts separately.
    unawaited(_sub?.cancel());
    _sub = null;
    unawaited(_handle?.close());
    _handle = null;
    setState(() {
      _busy = true;
      _usedFallback = false;
      _notice = '';
      _activeSendKey = key;
      _conv = TalkConversation();
      _notifiedApprovals.clear();
      _lastText = text;
    });
    if (!_useStream) {
      await _fallback(text);
      return;
    }
    TalkStreamHandle handle;
    try {
      handle = await _backend.openStream(
        text: text,
        deviceId: widget.client.deviceId ?? 'android-phone',
        sessionId: _sessionId,
      );
    } catch (e) {
      // Open failed before any event: one explicit auto-fallback to the
      // non-streaming twin (same TalkIn envelope), then stop.
      if (!mounted) {
        _releaseSendKey();
        return;
      }
      // Gone resumed session: the id this send tried no longer exists
      // server-side (deleted elsewhere). Drop to a fresh session and
      // retry the open ONCE with an empty id — never a retry loop — with
      // an explicit notice. The resumed transcript above stays visible.
      if (isSessionNotFound(e) && _sessionId.isNotEmpty) {
        setState(() {
          _sessionId = '';
          _notice = 'previous session deleted, started new';
        });
        try {
          handle = await _backend.openStream(
            text: text,
            deviceId: widget.client.deviceId ?? 'android-phone',
            sessionId: '',
          );
        } catch (e2) {
          if (!mounted) {
            _releaseSendKey();
            return;
          }
          await _fallback(text, streamError: e2);
          return;
        }
      } else {
        await _fallback(text, streamError: e);
        return;
      }
    }
    _handle = handle;
    _sub = handle.events.listen(
      _onEvent,
      onError: _onStreamError,
      onDone: _onStreamDone,
    );
    if (mounted) setState(() {});
  }

  void _onEvent(TalkStreamEvent e) {
    if (!mounted) return;
    setState(() {
      _conv.apply(e);
      if (e.name == 'done') {
        final sid = e.payload['session_id'];
        if (sid is String && sid.isNotEmpty) _sessionId = sid;
      }
      if (e.name == 'approval') {
        final id = _conv.approvalExecutionId;
        final reason = _conv.approvalReason;
        if (id != null && _notifiedApprovals.add(id)) {
          widget.onApproval(id, reason);
        }
      }
      if (_conv.isDone) {
        _busy = false;
        _releaseSendKey();
        unawaited(_handle?.close());
      }
    });
  }

  void _onStreamError(Object e) {
    if (!mounted || _conv.isDone) return; // terminal done wins over late error
    setState(() {
      _busy = false;
      _releaseSendKey();
      _conv.markError(describeTalkStreamError(e));
    });
  }

  void _onStreamDone() {
    // Clean end of the byte stream. Without a terminal `done` this is a
    // disconnect, not success: explicit reconnect-or-cancel UI.
    if (!mounted || _conv.isTerminal) return;
    setState(() {
      _busy = false;
      _releaseSendKey();
      _conv.markDisconnected('stream ended before done (disconnect?)');
    });
  }

  /// Non-streaming twin (POST /v1/talk): manual fallback when streaming
  /// is toggled off, or one automatic attempt when the stream fails to
  /// open. Same turn_id dedup guard as the stream path. A 404 against a
  /// resumed (non-empty) session id means the session is gone server-side:
  /// drop to fresh and retry ONCE with an explicit notice (never a loop).
  Future<void> _fallback(String text, {Object? streamError}) async {
    setState(() => _usedFallback = true);
    try {
      final out = await _backend.fallback(
        text: text,
        deviceId: widget.client.deviceId ?? 'android-phone',
        sessionId: _sessionId,
      );
      if (!mounted) return;
      setState(() {
        final tid = out['turn_id'];
        final sid = out['session_id'];
        if (sid is String && sid.isNotEmpty) _sessionId = sid;
        _conv.apply(TalkStreamEvent('done', {
          'reply': out['reply'] ?? '',
          'status': out['status'] ?? '',
          'turn_id': tid is String ? tid : '',
          'mission_id': out['mission_id'] ?? '',
          'execution_id': out['execution_id'] ?? '',
        }));
        _busy = false;
        _releaseSendKey();
      });
    } catch (e) {
      if (!mounted) return;
      // Gone resumed session on the fallback path: same fresh-retry once.
      if (isSessionNotFound(e) && _sessionId.isNotEmpty) {
        String? retryError;
        try {
          final out = await _backend.fallback(
            text: text,
            deviceId: widget.client.deviceId ?? 'android-phone',
            sessionId: '',
          );
          if (!mounted) return;
          setState(() {
            final tid = out['turn_id'];
            final sid = out['session_id'];
            // Fresh session resolved server-side; adopt it. The notice
            // still records that the resumed id was gone.
            _sessionId = sid is String ? sid : '';
            _notice = 'previous session deleted, started new';
            _conv.apply(TalkStreamEvent('done', {
              'reply': out['reply'] ?? '',
              'status': out['status'] ?? '',
              'turn_id': tid is String ? tid : '',
              'mission_id': out['mission_id'] ?? '',
              'execution_id': out['execution_id'] ?? '',
            }));
            _busy = false;
            _releaseSendKey();
          });
          return;
        } catch (e2) {
          retryError = describeTalkStreamError(e2);
        }
        if (!mounted) return;
        setState(() {
          _sessionId = '';
          _busy = false;
          _releaseSendKey();
          final detail = streamError == null
              ? (retryError ?? describeTalkStreamError(e))
              : '${describeTalkStreamError(streamError)}; fallback $retryError';
          _notice = 'previous session deleted, started new';
          _conv.markError(detail);
        });
        return;
      }
      setState(() {
        _busy = false;
        _releaseSendKey();
        final detail = streamError == null
            ? describeTalkStreamError(e)
            : '${describeTalkStreamError(streamError)}; fallback ${describeTalkStreamError(e)}';
        _conv.markError(detail);
      });
    }
  }

  Future<void> _cancel() async {
    final handle = _handle;
    if (handle == null) return;
    // Responsive first: terminal state renders immediately; the cancel
    // POST lands afterwards (never awaited before UI updates).
    setState(() {
      _busy = false;
      _releaseSendKey();
    });
    try {
      await _backend.cancelStream(handle);
    } catch (e) {
      if (mounted) {
        setState(() => _conv.markError(describeTalkStreamError(e)));
        return;
      }
    }
    if (mounted) setState(() => _conv.markCancelled());
  }

  void _discard() {
    if (_busy) return;
    setState(() {
      _conv = TalkConversation();
      _notifiedApprovals.clear();
      _lastText = '';
      _notice = '';
      _releaseSendKey();
    });
  }

  /// One resumed historical row. Same Card + dense ListTile shape as the
  /// live bubbles, with an explicit `(history)` label so prior turns are
  /// never mistaken for the live turn. No buttons, no callbacks: tapping
  /// does nothing, approving is impossible — read-only by construction.
  Widget _historyTile(BuildContext context, TalkHistoryRow h, int index) {
    final theme = Theme.of(context);
    final isUser = h.role == 'user';
    final isAssistant = h.role == 'assistant';
    final icon = isUser
        ? Icons.person_outline
        : isAssistant
            ? Icons.smart_toy_outlined
            : Icons.info_outline;
    final label = isUser
        ? 'You (history)'
        : isAssistant
            ? 'Zara (history)'
            : h.role.isEmpty
                ? 'message ${index + 1} (history)'
                : '${h.role} (history)';
    final toolLine =
        (isAssistant && h.tool.isNotEmpty) ? '[tool: ${h.tool}]' : '';
    final body = h.text.isEmpty ? '(empty)' : h.text;
    final sub = [
      body,
      if (toolLine.isNotEmpty) toolLine,
    ].join('\n');
    return Card(
      key: Key('talk-history-$index'),
      child: ListTile(
        dense: true,
        leading: Icon(icon),
        title: Text(label, style: theme.textTheme.titleSmall),
        subtitle: Text(sub, style: theme.textTheme.bodyMedium),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final c = _conv;
    final theme = Theme.of(context);
    final streaming = _busy && c.status == 'streaming';
    // One assistant bubble only: incremental `delta` text and the terminal
    // `done` reply reconcile into c.reply (done replaces, never appends),
    // so the final message can never render twice.
    final showAssistant = c.reply.isNotEmpty || _busy;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        TextField(
          controller: _inputCtrl,
          decoration: const InputDecoration(
            labelText: 'Ask Zara (streaming talk)',
            hintText: 'uses operator token from Tasks section',
          ),
          maxLines: 2,
          onSubmitted: (_) => _send(),
        ),
        Row(
          children: [
            const Text('Streaming'),
            Switch(
              value: _useStream,
              onChanged: _busy
                  ? null
                  : (v) => setState(() => _useStream = v),
            ),
            const SizedBox(width: 8),
            FilledButton(
              onPressed: _busy ? null : () => _send(),
              child: const Text('Send'),
            ),
            const SizedBox(width: 8),
            if (streaming)
              OutlinedButton(
                onPressed: _cancel,
                child: const Text('Cancel'),
              ),
          ],
        ),
        if (_notice.isNotEmpty)
          Text(_notice, style: theme.textTheme.bodyMedium),
        if (_usedFallback)
          const Text('stream unavailable — used /v1/talk fallback'),
        if (c.turnId != null) Text('turn: ${c.turnId}'),
        if (_sessionId.isNotEmpty) Text('session: $_sessionId'),
        // Resumed transcript (HistoryPanel Continue): read-only rows above
        // the live turn, explicitly marked historical. Display only — never
        // resubmitted (sends use _inputCtrl/_lastText alone), never
        // executed (no approval callbacks, no tool runs), secrets already
        // redacted at load time.
        if (_history.isNotEmpty) ...[
          Text('history · read-only — continues in this session',
              style: theme.textTheme.titleSmall),
          for (var i = 0; i < _history.length; i++)
            _historyTile(context, _history[i], i),
        ],
        // Same Card + dense ListTile shape as the Tasks rows above, so the
        // assistant flow reads as one conversation: user and assistant
        // turns share titleSmall labels + bodyMedium text.
        if (_lastText.isNotEmpty)
          Card(
            child: ListTile(
              dense: true,
              leading: const Icon(Icons.person_outline),
              title: Text('You', style: theme.textTheme.titleSmall),
              subtitle:
                  Text(_lastText, style: theme.textTheme.bodyMedium),
            ),
          ),
        if (showAssistant)
          Card(
            child: ListTile(
              dense: true,
              leading: const Icon(Icons.smart_toy_outlined),
              title: Text(streaming ? 'Zara · typing…' : 'Zara',
                  style: theme.textTheme.titleSmall),
              subtitle: Text(
                c.reply.isEmpty ? '…' : c.reply,
                style: theme.textTheme.bodyMedium,
              ),
            ),
          ),
        for (final t in c.tools)
          Builder(builder: (context) {
            final execLine = t.executionId.isNotEmpty
                ? 'exec ${shortExecId(t.executionId)} — same states as Tasks section'
                : '';
            final sub = [
              if (t.message.isNotEmpty) t.message,
              if (execLine.isNotEmpty) execLine,
            ].join('\n');
            return ListTile(
              dense: true,
              leading: const Icon(Icons.build_outlined),
              title: Text('${t.tool} · ${t.phase}'),
              subtitle: sub.isEmpty ? null : Text(sub),
            );
          }),
        if (c.approvalExecutionId != null)
          const Text(
            'Approval required — see Approval section above',
            style: TextStyle(color: Colors.orange),
          ),
        if (c.isDone)
          Text('Done (${c.doneStatus})${c.missionId.isNotEmpty ? ' mission=${c.missionId}' : ''}'),
        if (c.status == 'error' || c.status == 'disconnected')
          Text(
            '${c.status == 'disconnected' ? 'Disconnected' : 'Error'}: ${c.error}',
            style: const TextStyle(color: Colors.red),
          ),
        if ((c.status == 'error' || c.status == 'disconnected') && !_busy)
          Wrap(
            spacing: 8,
            children: [
              // Resend starts a NEW turn (new turn_id server-side): tools
              // execute once per turn, so this can never double-execute.
              OutlinedButton(
                onPressed: () => _send(isResend: true),
                child: const Text('Resend as new turn'),
              ),
              OutlinedButton(
                onPressed: _discard,
                child: const Text('Discard'),
              ),
            ],
          ),
        if (c.status == 'cancelled')
          const Text('Cancelled — send a new message to start over'),
      ],
    );
  }
}
