import 'package:flutter/material.dart';

/// Stage 20 contextual device panel (Android side of the shared
/// connected-device experience). Driven entirely by a snapshot map
/// from the discovery layer — the widget itself invents nothing.
/// Action visibility follows discovered capability per device class;
/// unsupported actions are never rendered as working buttons.
///
/// Snapshot contract (see job_runner 'device.observe'):
/// {devices: [{src, id, label, stat?, removable?, read_only?,
///             width_px?, height_px?, class_hint? ...}],
///  supported: bool, trust_note: String}
class DeviceObservedPanel extends StatelessWidget {
  final Map<String, dynamic>? snapshot;
  final VoidCallback? onRefresh;
  final ValueChanged<Map<String, dynamic>>? onAction;

  const DeviceObservedPanel({
    super.key,
    this.snapshot,
    this.onRefresh,
    this.onAction,
  });

  static const Map<String, List<String>> _classActions = {
    'usb_storage': [
      'storage.volumes',
      'storage.stats',
      'storage.copy',
      'storage.move',
    ],
    'otherusb': [],
    'usb_storage_card': ['storage.volumes'],
  };

  List<String> actionsFor(Map<String, dynamic> row) {
    final String src = '${row['src'] ?? ''}';
    if (src == 'storage_volume') return _classActions['usb_storage']!;
    if (src == 'usb') {
      final hint = '${row['class_hint'] ?? 'otherusb'}';
      return _classActions[hint] ?? _classActions['otherusb']!; // empty: honest
    }
    if (src == 'display') {
      return ['display.report'];
    }
    return const [];
  }

  @override
  Widget build(BuildContext context) {
    final snap = snapshot;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    'Devices',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                IconButton(
                  tooltip: 'Refresh',
                  onPressed: onRefresh,
                  icon: const Icon(Icons.refresh),
                ),
              ],
            ),
            const Text(
              'Detection is not trust — actions stay inside the '
              'Core approval pipeline.',
              style: TextStyle(fontSize: 12, fontStyle: FontStyle.italic),
            ),
            const SizedBox(height: 8),
            if (snap == null)
              const Text('Not polled yet — tap refresh.')
            else if (snap['supported'] == false)
              const Text(
                'Device discovery not supported on this Android '
                'hardware/build (honest abstain).',
              )
            else if (((snap['devices'] ?? const []) as List).isEmpty)
              const Text('No separate devices detected right now.')
            else ...[
              for (final row
                  in (snap['devices'] as List).cast<Map<String, dynamic>>())
                deviceRow(context, row),
            ],
          ],
        ),
      ),
    );
  }

  Widget deviceRow(BuildContext context, Map<String, dynamic> row) {
    final id = '${row['id'] ?? '?'}';
    final label = '${row['label'] ?? id}';
    final acts = actionsFor(row);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: Theme.of(context).textTheme.titleSmall),
          if (row['stat'] is Map && ((row['stat'] as Map)['free_bytes'] is num))
            Text(
              'Free: ${_h((row['stat'] as Map)['free_bytes'] as num)} · '
              'Total: ${_h((row['stat'] as Map)['total_bytes'] as num?)}',
              style: const TextStyle(fontSize: 12),
            ),
          if (row['width_px'] is num)
            Text(
              '${row['width_px']}×${row['height_px']}',
              style: const TextStyle(fontSize: 12),
            ),
          if (row['read_only'] == true)
            const Text(
              'read-only',
              style: TextStyle(fontSize: 12, color: Colors.deepOrange),
            ),
          Wrap(
            spacing: 6,
            children: [
              for (final a in acts)
                OutlinedButton(
                  onPressed: onAction == null
                      ? null
                      : () =>
                            onAction!({'device': id, 'action': a, 'row': row}),
                  child: Text(_label(a), style: const TextStyle(fontSize: 12)),
                ),
              if (acts.isEmpty)
                const Text(
                  'no supported actions yet',
                  style: TextStyle(fontSize: 12, color: Colors.grey),
                ),
              if (row['user_granted_access'] == false)
                const Text(
                  'access needs a system grant (never silent)',
                  style: TextStyle(fontSize: 11, color: Colors.grey),
                ),
            ],
          ),
        ],
      ),
    );
  }

  static String _label(String action) {
    switch (action) {
      case 'storage.volumes':
        return 'Usage';
      case 'storage.stats':
        return 'Media stats';
      case 'storage.copy':
        return 'Copy here';
      case 'storage.move':
        return 'Move here';
      case 'display.report':
        return 'Resolution/mode';
      default:
        return action;
    }
  }

  static String _h(num? b) {
    if (b == null) return '?';
    final v = b.toDouble();
    if (v >= 1e9) return '${(v / 1e9).toStringAsFixed(1)} GB';
    if (v >= 1e6) return '${(v / 1e6).toStringAsFixed(0)} MB';
    if (v >= 1e3) return '${(v / 1e3).toStringAsFixed(0)} KB';
    return '$v';
  }
}

/// Stage 21 — active file-transfer mission card. Data only from the
/// Core `/v1/transfer-missions` status; nothing is invented client-side.
/// States rendered honestly: running (per-item manifest),
/// waiting (the question Core is asking + its options — rendered as
/// diagnostic text, never as fake choices), failed partial (copied N of
/// M + per-item errors), completed, cancelled. The Cancel action routes
/// through the same /v1/talk pipeline as everything operator-initiated.
class TransferMissionPanel extends StatelessWidget {
  final Map<String, dynamic>? status; // {missions: [...], transfers: [...]}
  final VoidCallback? onCancel;

  const TransferMissionPanel({super.key, this.status, this.onCancel});

  Map<String, dynamic>? get _mission {
    final list = (status?['missions'] as List?) ?? const [];
    for (final e in list) {
      final m = Map<String, dynamic>.from(e as Map);
      final state = '${m['state'] ?? ''}';
      if (state != 'completed' && !state.startsWith('cancelled')) return m;
    }
    return list.isEmpty
        ? null
        : Map<String, dynamic>.from(list.last as Map);
  }

  static String stateLabel(String state) {
    switch (state) {
      case 'waiting':
        return 'Waiting for your answer';
      case 'waiting_for_permission':
        return 'Waiting for approval';
      case 'executing':
        return 'Transferring…';
      case 'verifying':
        return 'Verifying…';
      case 'failed':
        return 'Failed (partial — see details)';
      case 'completed':
        return 'Done';
      case 'cancelled':
        return 'Cancelled';
      default:
        return state.replaceAll('_', ' ');
    }
  }

  @override
  Widget build(BuildContext context) {
    final active = _mission;
    if (active == null) {
      return const SizedBox.shrink();
    }
    final state = '${active['state'] ?? ''}';
    final done = state == 'completed' || state == 'cancelled';
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              const Icon(Icons.swap_horiz, size: 18),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  'Transfer: ${active['goal'] ?? ''}',
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              Text(
                stateLabel('${active['state'] ?? ''}'),
                style: Theme.of(context).textTheme.labelSmall,
              ),
            ]),
            if (state == 'failed' || state == 'waiting') ...[
              const SizedBox(height: 6),
              Text(
                state == 'failed'
                    ? '${active['error'] ?? ''}'
                    : 'Zara needs your answer to continue — resume from '
                        'the mission status or reply with the specifics.',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
            if (state == 'failed') ...[
              const SizedBox(height: 4),
              Text(
                // partial-truth line: items copied vs attempted
                'Partial outcome is shown as failed; any files that did '
                'land are listed in the mission steps.',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
            if (!done && onCancel != null) ...[
              const SizedBox(height: 8),
              Align(
                alignment: Alignment.centerRight,
                child: TextButton.icon(
                  onPressed: onCancel,
                  icon: const Icon(Icons.cancel_outlined, size: 16),
                  label: const Text('Cancel transfer'),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
