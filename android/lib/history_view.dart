// Conversation history panel: browse past sessions + explicit delete.
//
// Frozen Core contracts (core/app.py, read-only):
//   GET    /v1/sessions?limit=N -> [{id,updated_at,message_count,last_text}]
//   GET    /v1/sessions/{id}     -> full session incl.
//                                    messages[{role,text,tool,created_at}]
//   DELETE /v1/sessions/{id}     -> {deleted} (404 unknown session)
//
// Rules (never violated):
// - Opening history never submits prompts: list/open are GET-only. There
//   is no POST /v1/talk (or any other mutating call) anywhere in this file.
// - No unbounded polling: the list loads once on insert + on explicit
//   manual Refresh. No Timer, no periodic loop, no stream subscription.
// - Stale async list results never overwrite a newer list: every refresh
//   bumps [_listGen] and only the newest generation may apply.
// - Rapid taps collapse: one in-flight open per session id, one in-flight
//   delete per session id, one in-flight resume per session id (second tap
//   dropped, never a second request).
// - Stale async loads never win: list refreshes carry [_listGen] and
//   resumes carry a resume generation — only the newest may apply.
// - Continue (explicit per-row button + detail button, only when onResume
//   is wired) delivers the chronological transcript GET-only to the talk
//   panel. A deleted id, or a 404 from Core, is an explicit gone state —
//   the callback never fires for a missing session.
// - Deleted sessions never reappear: the id joins [_deletedIds] on a
//   successful DELETE (or a 404 already-removed-elsewhere) and list
//   results are filtered against it, so a slow pre-delete response that
//   still contains the id is dropped too.
// - Tokens/secrets are never rendered: every displayed string passes
//   through [redactHistoryText] (Bearer/sk- patterns -> [redacted]).
import 'package:flutter/material.dart';

import 'core_client.dart';

/// One row of GET /v1/sessions (core/sessions.py list_recent: newest
/// first, last_text truncated to 120 server-side, redacted).
class HistorySummary {
  final String id;
  final String updatedAt;
  final int messageCount;
  final String lastText;
  const HistorySummary({
    required this.id,
    this.updatedAt = '',
    this.messageCount = 0,
    this.lastText = '',
  });

  static HistorySummary fromJson(Map<String, dynamic> m) {
    final mc = m['message_count'];
    return HistorySummary(
      id: '${m['id'] ?? ''}',
      updatedAt: '${m['updated_at'] ?? ''}',
      messageCount: mc is num ? mc.toInt() : 0,
      lastText: '${m['last_text'] ?? ''}',
    );
  }
}

/// Parse GET /v1/sessions rows. Non-list envelopes yield [] (explicit
/// empty state, never a crash). Rows without an id are dropped — they
/// can neither be opened nor deleted. Pure for testing.
List<HistorySummary> parseHistoryList(dynamic json) {
  if (json is! List) return [];
  return json
      .whereType<Map>()
      .map((e) => HistorySummary.fromJson(Map<String, dynamic>.from(e)))
      .where((h) => h.id.isNotEmpty)
      .toList();
}

/// One message of a full session (core/sessions.py SessionMessage:
/// role/text/tool/created_at).
class HistoryMessage {
  final String role;
  final String text;
  final String tool;
  final String createdAt;
  const HistoryMessage({
    this.role = '',
    this.text = '',
    this.tool = '',
    this.createdAt = '',
  });

  static HistoryMessage fromJson(Map<String, dynamic> m) => HistoryMessage(
        role: '${m['role'] ?? ''}',
        text: '${m['text'] ?? ''}',
        tool: '${m['tool'] ?? ''}',
        createdAt: '${m['created_at'] ?? ''}',
      );
}

/// Full session detail in chronological (stored) order.
class HistoryDetail {
  final String id;
  final List<HistoryMessage> messages;
  const HistoryDetail({required this.id, this.messages = const []});

  /// True only when the last message is user-role with no following
  /// assistant message: the turn was interrupted (e.g. restart) and is
  /// awaiting a reply. Reopenable; never auto-marked successful.
  /// Pure for testing.
  bool get awaitingReply =>
      messages.isNotEmpty && messages.last.role == 'user';
}

/// Parse GET /v1/sessions/{id}. Missing/non-list messages read as empty
/// (explicit "no messages" state, never a crash). Pure for testing.
HistoryDetail parseHistoryDetail(Map<String, dynamic> m) {
  final raw = m['messages'];
  final msgs = raw is List
      ? raw
          .whereType<Map>()
          .map((e) => HistoryMessage.fromJson(Map<String, dynamic>.from(e)))
          .toList()
      : <HistoryMessage>[];
  return HistoryDetail(id: '${m['id'] ?? ''}', messages: msgs);
}

/// Defensive redaction for DISPLAYED text only (list previews + message
/// bodies). Core already redacts at rest, but history text is untrusted
/// operator/user content that may quote a credential back: Bearer tokens
/// and sk-style keys render as [redacted], never verbatim. Pure for
/// testing. Never applied to request paths/ids (ids are server-issued,
// never secrets).
String redactHistoryText(String text) {
  var out = text.replaceAll(
      RegExp(r'Bearer\s+[A-Za-z0-9\-._~+/=]+'), 'Bearer [redacted]');
  out = out.replaceAll(RegExp(r'sk[_-][A-Za-z0-9_\-]+'), '[redacted]');
  return out;
}

/// Client-side preview truncation (server already caps at 120; this is
/// the display backstop for small screens). Pure for testing.
String historyPreview(String text, [int max = 120]) {
  if (text.length <= max) return text;
  return '${text.substring(0, max)}…';
}

/// Error buckets for the history panel. Pure for testing.
/// 'auth' = 401/403 (operator token missing/wrong — never a network
/// issue); 'notFound' = 404 (removed elsewhere); 'offline' = transport
/// failure (no route to Core at all); 'server' = everything else.
String historyErrorKind(Object e) {
  final s = e.toString();
  if (s.contains('401') || s.contains('403')) return 'auth';
  if (s.contains('404')) return 'notFound';
  if (s.contains('SocketException') ||
      s.contains('Failed host lookup') ||
      s.contains('Connection refused') ||
      s.contains('Connection reset') ||
      s.contains('Network is unreachable') ||
      s.contains('No route to host') ||
      s.contains('TimeoutException') ||
      s.contains('timed out') ||
      s.contains('CoreException(-1)')) {
    return 'offline';
  }
  return 'server';
}

/// One-line, explicit rendering of history-endpoint failures. Pure for
/// testing.
String describeHistoryError(Object e) {
  switch (historyErrorKind(e)) {
    case 'auth':
      return 'unauthorized/forbidden (operator token missing/wrong)';
    case 'notFound':
      return 'not found (unknown session — removed elsewhere)';
    case 'offline':
      return 'offline (Core unreachable — check connection, then refresh)';
    default:
      return 'core error';
  }
}

/// Transport behind [HistoryPanel]: list, open, delete. Production runs
/// on the shared [CoreClient] (operator Bearer + X-Device auth, bounded
/// timeouts); widget tests inject a fake so no sockets are needed — wire
/// behavior is covered by loopback tests against [CoreClient] directly.
/// Read-only except [remove], which is DELETE-only: there is deliberately
/// no prompt-submit method on this interface, so the panel cannot send
/// chat traffic even by accident.
abstract class HistoryBackend {
  Future<List<HistorySummary>> list({int limit = 20});
  Future<HistoryDetail> open(String id);
  Future<void> remove(String id);
}

class CoreHistoryBackend implements HistoryBackend {
  final CoreClient client;
  final TextEditingController tokenCtrl; // shared operator token, memory only
  const CoreHistoryBackend(this.client, this.tokenCtrl);

  void _applyToken() {
    final tok = tokenCtrl.text.trim();
    client.operatorToken = tok.isEmpty ? null : tok;
  }

  @override
  Future<List<HistorySummary>> list({int limit = 20}) async {
    _applyToken();
    final rows = await client.sessionList(limit: limit);
    return parseHistoryList(rows);
  }

  @override
  Future<HistoryDetail> open(String id) async {
    _applyToken();
    final m = await client.sessionGet(id);
    return parseHistoryDetail(m);
  }

  @override
  Future<void> remove(String id) async {
    _applyToken();
    await client.sessionDelete(id);
  }
}

class HistoryPanel extends StatefulWidget {
  final CoreClient client;
  final TextEditingController tokenCtrl; // shared operator token, memory only
  final int limit;

  /// Resume seam: invoked with the session id + full transcript
  /// ([{role,text,tool,created_at}] maps, chronological) when the user taps
  /// an explicit Continue button. Read-only fetch (GET-only, never a
  /// prompt submit); main.dart routes it into the talk panel. Null =
  /// no resume affordance (panel is browse/delete only).
  final void Function(String sessionId, List<Map<String, dynamic>> messages)?
      onResume;

  /// Test seam: production passes null (operations run on [client]).
  /// Widget tests inject a fake so no sockets are needed.
  final HistoryBackend? backend;
  const HistoryPanel({
    super.key,
    required this.client,
    required this.tokenCtrl,
    this.limit = 20,
    this.onResume,
    this.backend,
  });

  @override
  State<HistoryPanel> createState() => _HistoryPanelState();
}

class _HistoryPanelState extends State<HistoryPanel> {
  List<HistorySummary> _summaries = [];
  bool _loading = true;
  String _errorKind = '';
  String _errorDetail = '';
  String _notice = '';

  /// Generation counter: every refresh bumps [_listGen]; only the newest
  /// generation may apply its results. A slow earlier response that
  /// arrives after a newer refresh is discarded, never rendered.
  int _listGen = 0;

  /// Locally deleted ids (successful DELETE, or 404 already-removed):
  /// filtered out of every future list result so a deleted session can
  /// never reappear — even inside a stale in-flight response.
  final Set<String> _deletedIds = {};

  /// Rapid-tap guards: one in-flight open/delete per session id. The
  /// membership test runs synchronously before any await, so a second
  /// tap in the same frame still collapses into the first request.
  final Set<String> _opening = {};
  final Set<String> _deleting = {};

  /// Resume guards: one in-flight Continue per session id (rapid taps
  /// collapse), plus a generation counter so a stale resume that resolves
  /// after a newer one is discarded and never delivered to [onResume].
  final Set<String> _resuming = {};
  int _resumeGen = 0;

  String? _selectedId;
  HistoryDetail? _detail;
  bool _detailLoading = false;
  String _detailErrorKind = '';
  String _detailErrorDetail = '';

  HistoryBackend get _backend =>
      widget.backend ?? CoreHistoryBackend(widget.client, widget.tokenCtrl);

  @override
  void initState() {
    super.initState();
    _refresh(); // on-navigate refresh: the list loads when shown.
  }

  /// Manual refresh (+ the single on-navigate load above). No timers, no
  /// polling loops: this is the only list fetch in the panel.
  Future<void> _refresh() async {
    final gen = ++_listGen;
    if (mounted) {
      setState(() {
        _loading = true;
        _errorKind = '';
        _errorDetail = '';
        _notice = '';
      });
    }
    try {
      final rows = await _backend.list(limit: widget.limit);
      if (!mounted || gen != _listGen) return; // stale: discard silently
      setState(() {
        _summaries =
            rows.where((r) => !_deletedIds.contains(r.id)).toList();
        _loading = false;
      });
    } catch (e) {
      if (!mounted || gen != _listGen) return; // stale error: discard
      setState(() {
        _loading = false;
        _errorKind = historyErrorKind(e);
        _errorDetail = '$e';
      });
    }
  }

  /// Open one session (GET-only, chronological read). Rapid double-taps
  /// on the same row collapse into the first request.
  Future<void> _open(String id) async {
    if (!_opening.add(id)) return;
    if (mounted) {
      setState(() {
        _selectedId = id;
        _detail = null;
        _detailLoading = true;
        _detailErrorKind = '';
        _detailErrorDetail = '';
      });
    }
    try {
      final d = await _backend.open(id);
      if (!mounted || _selectedId != id) return; // navigated away: discard
      setState(() => _detail = d);
    } catch (e) {
      if (!mounted || _selectedId != id) return;
      setState(() {
        _detailErrorKind = historyErrorKind(e);
        _detailErrorDetail = '$e';
      });
    } finally {
      _opening.remove(id);
      if (mounted) setState(() => _detailLoading = false);
    }
  }

  void _back() {
    if (_detailLoading) return; // an open is in flight; Back lands after
    setState(() {
      _selectedId = null;
      _detail = null;
      _detailErrorKind = '';
      _detailErrorDetail = '';
    });
  }

  /// Continue one session in the talk panel: reuses the already-loaded
  /// detail when this is the selected session, otherwise fetches it
  /// GET-only (never a prompt submit), then delivers the chronological
  /// transcript to [HistoryPanel.onResume]. Rapid double-taps collapse
  /// into the first request; stale resolutions (a newer resume started
  /// while this one was in flight) are discarded silently. A locally
  /// deleted id, or a 404 from Core, is an explicit gone state — the
  /// callback is never invoked for a session that no longer exists.
  Future<void> _resume(String id) async {
    final cb = widget.onResume;
    if (cb == null) return;
    if (!_resuming.add(id)) return;
    final gen = ++_resumeGen;
    try {
      if (_deletedIds.contains(id)) {
        if (!mounted || gen != _resumeGen) return;
        setState(() => _notice = '$id is gone (deleted)');
        return;
      }
      HistoryDetail? d;
      if (_selectedId == id && _detail != null) {
        d = _detail;
      } else {
        try {
          d = await _backend.open(id);
        } on Object catch (e) {
          if (!mounted || gen != _resumeGen) return; // stale: discard
          if (isSessionNotFound(e)) {
            setState(() {
              _deletedIds.add(id);
              _summaries = _summaries.where((r) => r.id != id).toList();
              if (_selectedId == id) {
                _selectedId = null;
                _detail = null;
              }
              _notice = '$id is gone (deleted elsewhere)';
            });
            return;
          }
          setState(() {
            _errorKind = historyErrorKind(e);
            _errorDetail = 'resume $id ${describeHistoryError(e)}: $e';
          });
          return;
        }
      }
      if (!mounted || gen != _resumeGen) return; // stale: discard
      if (_deletedIds.contains(id)) return; // deleted mid-flight: gone
      final detail = d;
      if (detail == null) return;
      cb(
        id,
        [
          for (final m in detail.messages)
            {
              'role': m.role,
              'text': m.text,
              'tool': m.tool,
              'created_at': m.createdAt,
            },
        ],
      );
      setState(() => _notice = 'resumed $id in conversation');
    } finally {
      _resuming.remove(id);
      if (mounted) setState(() {});
    }
  }

  /// Explicit confirm dialog. Returns true only for the Delete button —
  /// dismissing or Cancel returns false and nothing is sent.
  Future<bool> _confirmDelete(String id) async {
    String? preview;
    for (final r in _summaries) {
      if (r.id == id) {
        preview = r.lastText;
        break;
      }
    }
    final ok = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete conversation?'),
        content: Text(
          'This deletes conversation $id from Core.\n'
          '${preview == null || preview.isEmpty ? '(no preview)' : 'Preview: ${historyPreview(redactHistoryText(preview))}'}\n'
          'This cannot be undone.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Cancel'),
          ),
          TextButton(
            key: const Key('history-confirm-delete'),
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    return ok == true;
  }

  /// Delete one session after explicit confirmation. The local entry is
  /// dropped immediately (never reappears), then the list refreshes from
  /// Core. 404 = already removed elsewhere: same local drop, explicit
  /// notice, never a retry. Rapid double-taps collapse.
  Future<void> _delete(String id) async {
    if (!_deleting.add(id)) return;
    try {
      if (!await _confirmDelete(id)) return;
      try {
        await _backend.remove(id);
      } on Object catch (e) {
        if (!mounted) return;
        if (isSessionNotFound(e)) {
          setState(() {
            _deletedIds.add(id);
            _summaries = _summaries.where((r) => r.id != id).toList();
            if (_selectedId == id) {
              _selectedId = null;
              _detail = null;
            }
            _notice = '$id was already removed elsewhere';
          });
          return;
        }
        setState(() {
          _errorKind = historyErrorKind(e);
          _errorDetail = 'delete $id ${describeHistoryError(e)}: $e';
        });
        return;
      }
      if (!mounted) return;
      setState(() {
        _deletedIds.add(id);
        _summaries = _summaries.where((r) => r.id != id).toList();
        if (_selectedId == id) {
          _selectedId = null;
          _detail = null;
        }
      });
      await _refresh(); // re-list after delete: server state is the truth
      if (!mounted) return;
      setState(() => _notice = 'deleted $id');
    } finally {
      _deleting.remove(id);
      if (mounted) setState(() {});
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_selectedId != null) return _buildDetail(context);
    return _buildList(context);
  }

  Widget _buildList(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            // Always enabled: a newer manual refresh supersedes any
            // in-flight one via the generation counter.
            OutlinedButton(
              key: const Key('history-refresh'),
              onPressed: _refresh,
              child: const Text('Refresh'),
            ),
          ],
        ),
        const SizedBox(height: 4),
        if (_loading) ...[
          const LinearProgressIndicator(),
          const Text('Loading conversations…'),
        ] else if (_errorKind.isNotEmpty) ...[
          Text(
            'History: ${describeHistoryError(_errorDetail)}\n$_errorDetail',
            style: const TextStyle(color: Colors.red),
          ),
          if (_errorKind == 'auth')
            const Text('Uses the operator token from the Tasks section.'),
        ] else if (_summaries.isEmpty) ...[
          const Text('no conversations yet'),
        ] else ...[
          for (final h in _summaries)
            Card(
              key: Key('history-row-${h.id}'),
              child: ListTile(
                dense: true,
                leading: const Icon(Icons.history_outlined),
                title: Text(
                  h.lastText.isEmpty
                      ? '(empty conversation)'
                      : historyPreview(redactHistoryText(h.lastText)),
                  style: theme.textTheme.bodyMedium,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
                subtitle: Text(
                  '${h.messageCount} message${h.messageCount == 1 ? '' : 's'}'
                  '${h.updatedAt.isEmpty ? '' : ' · ${h.updatedAt}'}',
                ),
                trailing: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    // Explicit resume affordance (besides Open-tap/Delete):
                    // continues this session in the talk panel. Hidden when
                    // no onResume is wired (browse/delete only).
                    if (widget.onResume != null)
                      TextButton(
                        key: Key('history-continue-${h.id}'),
                        onPressed: _resuming.contains(h.id)
                            ? null
                            : () => _resume(h.id),
                        child: const Text('Continue'),
                      ),
                    IconButton(
                      key: Key('history-delete-${h.id}'),
                      icon: const Icon(Icons.delete_outline),
                      tooltip: 'Delete',
                      onPressed: _deleting.contains(h.id)
                          ? null
                          : () => _delete(h.id),
                    ),
                  ],
                ),
                onTap:
                    _opening.contains(h.id) ? null : () => _open(h.id),
              ),
            ),
        ],
        if (_notice.isNotEmpty) Text(_notice),
      ],
    );
  }

  Widget _buildDetail(BuildContext context) {
    final theme = Theme.of(context);
    final id = _selectedId!;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            OutlinedButton(
              key: const Key('history-back'),
              onPressed: _detailLoading ? null : _back,
              child: const Text('Back'),
            ),
            OutlinedButton(
              onPressed:
                  _opening.contains(id) ? null : () => _open(id),
              child: const Text('Reload'),
            ),
            // Continues this conversation in the talk panel using the
            // already-loaded transcript (no refetch, still GET-only data).
            if (widget.onResume != null)
              OutlinedButton(
                key: const Key('history-continue-detail'),
                onPressed: (_detailLoading ||
                        _detail == null ||
                        _resuming.contains(id))
                    ? null
                    : () => _resume(id),
                child: const Text('Continue in conversation'),
              ),
          ],
        ),
        const SizedBox(height: 4),
        Text('Conversation $id', style: theme.textTheme.titleSmall),
        if (_detailLoading) ...[
          const LinearProgressIndicator(),
          const Text('Loading conversation…'),
        ] else if (_detailErrorKind.isNotEmpty) ...[
          Text(
            'History: ${describeHistoryError(_detailErrorDetail)}\n$_detailErrorDetail',
            style: const TextStyle(color: Colors.red),
          ),
        ] else if (_detail == null) ...[
          const Text('Loading conversation…'),
        ] else if (_detail!.messages.isEmpty) ...[
          const Text('no messages in this conversation'),
        ] else ...[
          if (_detail!.awaitingReply)
            const Text(
              'awaiting reply — never marked complete',
              style: TextStyle(color: Colors.orange),
            ),
          for (var i = 0; i < _detail!.messages.length; i++)
            _messageTile(context, _detail!.messages[i], i),
        ],
      ],
    );
  }

  /// One chronological message. User and assistant rows are visually
  /// distinct (person vs bot icon + label); assistant rows carry an
  /// explicit tool marker when Core recorded a tool. System/tool roles
  /// render under their own labels — never misattributed.
  Widget _messageTile(BuildContext context, HistoryMessage m, int index) {
    final theme = Theme.of(context);
    final isUser = m.role == 'user';
    final isAssistant = m.role == 'assistant';
    final icon = isUser
        ? Icons.person_outline
        : isAssistant
            ? Icons.smart_toy_outlined
            : Icons.info_outline;
    final label = isUser
        ? 'You'
        : isAssistant
            ? 'Zara'
            : m.role.isEmpty
                ? 'message ${index + 1}'
                : m.role;
    final toolLine =
        (isAssistant && m.tool.isNotEmpty) ? '[tool: ${m.tool}]' : '';
    final body = redactHistoryText(m.text.isEmpty ? '(empty)' : m.text);
    final sub = [
      body,
      if (toolLine.isNotEmpty) toolLine,
      if (m.createdAt.isNotEmpty) m.createdAt,
    ].join('\n');
    return Card(
      key: Key('history-msg-$index'),
      child: ListTile(
        dense: true,
        leading: Icon(icon),
        title: Text(label, style: theme.textTheme.titleSmall),
        subtitle: Text(sub, style: theme.textTheme.bodyMedium),
      ),
    );
  }
}
