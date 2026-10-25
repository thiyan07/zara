// Messaging approvals panel: mission-bound send decisions only.
//
// Frozen Core contracts (core/app.py, read-only besides the two POSTs):
//   GET  /v1/messaging/approvals             -> {approvals:[view]}
//   GET  /v1/messaging/approvals/{id}        -> {state,...}
//   POST /v1/messaging/approvals/{id}/approve -> executes the BOUND plan
//   POST /v1/messaging/approvals/{id}/cancel  -> {state: cancelled}
//
// Views never contain message plaintext or the binding fingerprint; the
// panel renders recipient + device + state + the bound action labels
// only. Approve is a one-time decision on the exact bound plan (which
// Core re-validates); Cancel refuses silently-ended approvals with an
// explicit 409-conflict state. Pending rows load on manual Refresh
// (never polled); an approve/cancel action reloads the list from Core.

import 'package:flutter/material.dart';

import 'core_client.dart';

/// One redacted approval row as Core reports it. `empty id` never
/// happens on a healthy Core; rows without an id are display-only.
class MessagingView {
  final String id;
  final String missionId;
  final String deviceId;
  final String conversation;
  final String state;
  final List<String> actions;
  const MessagingView({
    this.id = '',
    this.missionId = '',
    this.deviceId = '',
    this.conversation = '',
    this.state = '',
    this.actions = const [],
  });

  bool get isDecidable => id.isNotEmpty && state == 'pending';

  static MessagingView fromJson(Map<String, dynamic> m) {
    final acts = m['actions'];
    return MessagingView(
      id: '${m['approval_id'] ?? ''}',
      missionId: '${m['mission_id'] ?? ''}',
      deviceId: '${m['device_id'] ?? ''}',
      conversation: '${m['conversation'] ?? ''}',
      state: '${m['state'] ?? ''}',
      actions: acts is List
          ? acts.map((a) => '$a').where((a) => a.isNotEmpty).toList()
          : const [],
    );
  }
}

/// Parse GET /v1/messaging/approvals rows into redacted [MessagingView]s.
/// Unknown/extra keys are ignored; a row missing its id yields no
/// decision target (state kept for display, id empty). Pure for testing.
List<MessagingView> parseMessagingApprovals(List<Map<String, dynamic>> rows) {
  return rows.map(MessagingView.fromJson).whereType<MessagingView>().toList();
}

/// 409 on a messaging decision = the approval already ended (approved,
/// cancelled, expired, completed). Pure for testing.

/// Backend for the panel. Widget tests inject a fake (socket-free);
/// production uses [CoreMessagingBackend] (operator Bearer + X-Device).
abstract class MessagingBackend {
  Future<List<MessagingView>> pending();
  Future<Map<String, dynamic>> approve(String approvalId);
  Future<Map<String, dynamic>> cancel(String approvalId);
}

class CoreMessagingBackend implements MessagingBackend {
  final CoreClient client;
  final TextEditingController tokenCtrl; // shared operator token, memory only
  const CoreMessagingBackend(this.client, this.tokenCtrl);

  void _applyToken() {
    final tok = tokenCtrl.text.trim();
    client.operatorToken = tok.isEmpty ? null : tok;
  }

  @override
  Future<List<MessagingView>> pending() async {
    _applyToken();
    final rows = await client.messagingListPending();
    return parseMessagingApprovals(rows);
  }

  @override
  Future<Map<String, dynamic>> approve(String approvalId) async {
    _applyToken();
    return client.messagingApprove(approvalId);
  }

  @override
  Future<Map<String, dynamic>> cancel(String approvalId) async {
    _applyToken();
    return client.messagingCancel(approvalId);
  }
}

/// Manual-refresh pending list with an explicit, honest empty state.
/// Approve cancels nothing implicitly: every decision is a separate
/// explicit request, and the list reloads from Core afterwards (Core is
/// the source of truth for one-time consumption).
class MessagingReviewPanel extends StatefulWidget {
  final MessagingBackend backend;
  const MessagingReviewPanel({super.key, required this.backend});

  @override
  State<MessagingReviewPanel> createState() => _MessagingReviewPanelState();
}

class _MessagingReviewPanelState extends State<MessagingReviewPanel> {
  List<MessagingView> _rows = [];
  String _error = '';
  String _notice = '';
  bool _loading = false;
  final Set<String> _busy = {}; // ids being decided right now

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _reload());
  }

  Future<void> _reload() async {
    // mounted guard at entry: the post-frame callback can fire after a
    // fast tab switch disposed this panel (observed as a real
    // setState-after-dispose crash in widget tests)
    if (!mounted || _loading) return;
    setState(() {
      _loading = true;
      _error = '';
    });
    try {
      final rows = await widget.backend.pending();
      if (!mounted) return;
      setState(() {
        _rows = rows;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = '$e';
        _loading = false;
      });
    }
  }

  Future<void> _decide(MessagingView v, bool ok) async {
    if (!v.isDecidable || _busy.contains(v.id)) return;
    setState(() => _busy.add(v.id));
    try {
      final out = ok
          ? await widget.backend.approve(v.id)
          : await widget.backend.cancel(v.id);
      if (!mounted) return;
      final s = '${out['status'] ?? out['state'] ?? 'decided'}';
      setState(() {
        _notice = ok
            ? 'Approval result: $s'
            : 'Cancelled: ${v.conversation.isEmpty ? v.id : v.conversation}';
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = messagingDecisionError(e, approve: ok));
    } finally {
      if (mounted) {
        setState(() => _busy.remove(v.id));
        await _reload();
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            TextButton(
              onPressed: _loading ? null : _reload,
              child: Text(_loading
                  ? 'Loading…'
                  : _rows.isEmpty
                      ? 'Refresh'
                      : 'Refresh (${_rows.length})'),
            ),
            const Spacer(),
            const Text('Never shows message content — decisions only',
                style: TextStyle(fontSize: 11, color: Colors.grey)),
          ],
        ),
        if (_notice.isNotEmpty)
          Text(_notice, style: const TextStyle(color: Colors.green)),
        if (_error.isNotEmpty)
          Text(_error, style: const TextStyle(color: Colors.red)),
        if (!_loading && _rows.isEmpty && _error.isEmpty)
          const Text(
              'Nothing pending. Messaging approvals appear here after a '
              'send is requested ("send hi to X"), never before — '
              'and never without a bound mission.'),
        for (final v in _rows)
          Card(
            child: ListTile(
              title: Text(v.conversation.isEmpty
                  ? '(unknown recipient)'
                  : 'To: ${v.conversation}'),
              subtitle: Text(
                  'Device: ${v.deviceId.isEmpty ? '?' : v.deviceId} • '
                  'State: ${v.state} • '
                  'Plan: ${v.actions.join(' → ')}'),
              trailing: v.isDecidable
                  ? _busy.contains(v.id)
                      ? const Text('Deciding…')
                      : Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            FilledButton(
                              child: const Text('Approve & send'),
                              onPressed: () => _decide(v, true),
                            ),
                            const SizedBox(width: 6),
                            OutlinedButton(
                              child: const Text('Cancel'),
                              onPressed: () => _decide(v, false),
                            ),
                          ],
                        )
                  : Text(v.state, style: const TextStyle(color: Colors.grey)),
            ),
          ),
      ],
    );
  }
}
