// Memory review panel: search + forget + purge + pending-candidate confirm.
//
// Frozen Core contracts (core/app.py, read-only):
//   GET    /v1/memory/search?q=&top_k= -> [{text,category,score}]
//   DELETE /v1/memory/{id}            -> {forgotten} (404 unknown id)
//   GET    /v1/memory/candidates       -> [{candidate_id,text(redacted
//                                          server-side),category,source}]
//   POST   /v1/memory/candidates/confirm {candidate_id,ok} -> verdict
//   POST   /v1/memory/purge           -> {purged}
// Pending candidates render from GET /v1/memory/candidates on manual
// Refresh only (never polled); each row carries Accept/Reject buttons
// that POST the confirm endpoint, then re-fetch the list. Core search
// rows currently omit `id`, so per-row Forget works only when a row
// carries one (parsed forward-compatibly); rows without ids get an
// explicit state, never a guessed DELETE.
import 'package:flutter/material.dart';

import 'core_client.dart';

/// One memory search row. `id` is empty when Core omits it (current
/// behavior) — callers must treat empty as "not forgettable from here".
class MemoryHit {
  final String id;
  final String text;
  final String category;
  final double? score;
  const MemoryHit({
    this.id = '',
    this.text = '',
    this.category = '',
    this.score,
  });

  static MemoryHit fromJson(Map<String, dynamic> m) {
    final sc = m['score'];
    return MemoryHit(
      id: '${m['id'] ?? ''}',
      text: '${m['text'] ?? ''}',
      category: '${m['category'] ?? ''}',
      score: sc is num ? sc.toDouble() : null,
    );
  }
}

/// Parse GET /v1/memory/search rows (tolerates missing id/score — Core
/// omits id today). Non-list envelopes yield []. Pure for testing.
List<MemoryHit> parseMemorySearchResults(dynamic json) {
  if (json is! List) return [];
  return json
      .whereType<Map>()
      .map((e) => MemoryHit.fromJson(Map<String, dynamic>.from(e)))
      .toList();
}

/// One pending memory candidate. `text` arrives redacted server-side
/// (core/app.py memory_candidates) and renders verbatim.
class MemoryCandidate {
  final String candidateId;
  final String text;
  final String category;
  final String source;
  const MemoryCandidate({
    this.candidateId = '',
    this.text = '',
    this.category = '',
    this.source = '',
  });

  static MemoryCandidate fromJson(Map<String, dynamic> m) {
    return MemoryCandidate(
      candidateId: '${m['candidate_id'] ?? ''}',
      text: '${m['text'] ?? ''}',
      category: '${m['category'] ?? ''}',
      source: '${m['source'] ?? ''}',
    );
  }
}

/// Parse GET /v1/memory/candidates rows. Non-list envelopes yield [].
/// Pure for testing.
List<MemoryCandidate> parseMemoryCandidates(dynamic json) {
  if (json is! List) return [];
  return json
      .whereType<Map>()
      .map((e) => MemoryCandidate.fromJson(Map<String, dynamic>.from(e)))
      .toList();
}

/// One-line, explicit rendering of memory-endpoint failures. 404/422 each
/// get their own state (never silent); 401/403 point at the operator
/// token, not the network. Pure for testing.
String describeMemoryError(Object e) {
  final s = e.toString();
  if (s.contains('401') || s.contains('403')) {
    return 'unauthorized/forbidden (operator token missing/wrong)';
  }
  if (isMemoryNotFound(e)) {
    return 'not found (unknown id — removed elsewhere)';
  }
  if (isMemorySecretRejected(e)) {
    return 'refused (secret material is never stored)';
  }
  return 'core error';
}

/// Transport behind [MemoryReviewPanel]: search, forget, candidates,
/// confirm, purge. Production runs on the shared [CoreClient] (operator
/// Bearer + X-Device auth, bounded timeouts); widget tests inject a fake
/// so no sockets are needed — wire behavior is covered by loopback tests
/// against [CoreClient] directly.
abstract class MemoryBackend {
  Future<List<MemoryHit>> search(String q, {int topK = 5});
  Future<Map<String, dynamic>> forget(String id);
  Future<List<MemoryCandidate>> candidates();
  Future<Map<String, dynamic>> confirm(String candidateId, bool ok);
  Future<Map<String, dynamic>> purge();
}

class CoreMemoryBackend implements MemoryBackend {
  final CoreClient client;
  final TextEditingController tokenCtrl; // shared operator token, memory only
  const CoreMemoryBackend(this.client, this.tokenCtrl);

  void _applyToken() {
    final tok = tokenCtrl.text.trim();
    client.operatorToken = tok.isEmpty ? null : tok;
  }

  @override
  Future<List<MemoryHit>> search(String q, {int topK = 5}) async {
    _applyToken();
    final rows = await client.memorySearch(q, topK: topK);
    return parseMemorySearchResults(rows);
  }

  @override
  Future<Map<String, dynamic>> forget(String id) async {
    _applyToken();
    return client.memoryForget(id);
  }

  @override
  Future<List<MemoryCandidate>> candidates() async {
    _applyToken();
    final rows = await client.memoryCandidates();
    return parseMemoryCandidates(rows);
  }

  @override
  Future<Map<String, dynamic>> confirm(String candidateId, bool ok) async {
    _applyToken();
    return client.memoryConfirmCandidate(candidateId, ok: ok);
  }

  @override
  Future<Map<String, dynamic>> purge() async {
    _applyToken();
    return client.memoryPurge();
  }
}

class MemoryReviewPanel extends StatefulWidget {
  final CoreClient client;
  final TextEditingController tokenCtrl; // shared operator token, memory only

  /// Test seam: production passes null (operations run on [client]).
  /// Widget tests inject a fake so no sockets are needed.
  final MemoryBackend? backend;
  const MemoryReviewPanel({
    super.key,
    required this.client,
    required this.tokenCtrl,
    this.backend,
  });

  @override
  State<MemoryReviewPanel> createState() => _MemoryReviewPanelState();
}

class _MemoryReviewPanelState extends State<MemoryReviewPanel> {
  final _queryCtrl = TextEditingController();
  final _forgetCtrl = TextEditingController();
  bool _busy = false;
  String _error = '';
  String _notice = '';
  List<MemoryHit> _hits = [];
  bool _searched = false;
  final Set<String> _forgetting = {};
  final Set<String> _removedElsewhere = {};

  // Pending candidates: fetched on manual Refresh only (never polled,
  // never on open). Per-row Accept/Reject confirms, then re-fetches.
  List<MemoryCandidate> _candidates = [];
  bool _candidatesLoaded = false;
  bool _candidatesBusy = false;
  final Set<String> _deciding = {};

  MemoryBackend get _backend =>
      widget.backend ?? CoreMemoryBackend(widget.client, widget.tokenCtrl);

  @override
  void dispose() {
    _queryCtrl.dispose();
    _forgetCtrl.dispose();
    super.dispose();
  }

  Future<void> _search() async {
    final q = _queryCtrl.text.trim();
    if (_busy || q.isEmpty) return;
    setState(() {
      _busy = true;
      _error = '';
      _notice = '';
    });
    try {
      final hits = await _backend.search(q);
      if (!mounted) return;
      setState(() {
        _hits = hits;
        _searched = true;
        _removedElsewhere.clear();
        if (hits.isEmpty) _notice = 'no memories matched "$q"';
      });
    } catch (e) {
      if (mounted) setState(() => _error = 'search ${describeMemoryError(e)}: $e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// Forget one id. 404 = removed elsewhere (explicit row state, never a
  /// retry). Empty ids are never sent — explicit note instead.
  Future<void> _forget(String id) async {
    if (id.isEmpty) {
      setState(() => _notice =
          'Core search rows carry no id — paste the memory id below to forget');
      return;
    }
    if (!_forgetting.add(id)) return;
    if (mounted) {
      setState(() {
        _error = '';
        _notice = '';
      });
    }
    try {
      await _backend.forget(id);
      if (!mounted) return;
      setState(() {
        _hits = _hits.where((h) => h.id != id).toList();
        _notice = 'forgot $id';
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        if (isMemoryNotFound(e)) {
          _removedElsewhere.add(id);
          _notice = '$id was already removed elsewhere';
        } else {
          _error = 'forget $id ${describeMemoryError(e)}: $e';
        }
      });
    } finally {
      _forgetting.remove(id);
      if (mounted) setState(() {});
    }
  }

  Future<void> _forgetById() async {
    final id = _forgetCtrl.text.trim();
    if (_busy || id.isEmpty) return;
    setState(() {
      _busy = true;
      _error = '';
      _notice = '';
    });
    try {
      await _backend.forget(id);
      if (!mounted) return;
      setState(() {
        _hits = _hits.where((h) => h.id != id).toList();
        _forgetCtrl.clear();
        _notice = 'forgot $id';
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        if (isMemoryNotFound(e)) {
          _notice = '$id was already removed elsewhere';
        } else {
          _error = 'forget $id ${describeMemoryError(e)}: $e';
        }
      });
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// Manual Refresh only: fetch the pending-candidates list. Never
  /// polled, never fetched on open. Preserves the decision notice so
  /// Accept/Reject feedback stays visible after the re-fetch.
  Future<void> _refreshCandidates() async {
    if (_candidatesBusy) return;
    setState(() {
      _candidatesBusy = true;
      _error = '';
    });
    try {
      final rows = await _backend.candidates();
      if (!mounted) return;
      setState(() {
        _candidates = rows;
        _candidatesLoaded = true;
        if (rows.isEmpty && _notice.isEmpty) {
          _notice = 'no pending candidates';
        }
      });
    } catch (e) {
      if (mounted) {
        setState(() => _error = 'candidates ${describeMemoryError(e)}: $e');
      }
    } finally {
      if (mounted) setState(() => _candidatesBusy = false);
    }
  }

  /// Accept (ok=true) or reject (ok=false) one candidate row, then
  /// re-fetch the list so the decision is visible immediately. A 404
  /// means the candidate is gone (decided/expired elsewhere): explicit
  /// gone state, then re-fetch.
  Future<void> _decide(MemoryCandidate c, bool ok) async {
    final id = c.candidateId;
    if (id.isEmpty || !_deciding.add(id)) return;
    if (mounted) {
      setState(() {
        _error = '';
        _notice = '';
      });
    }
    try {
      final out = await _backend.confirm(id, ok);
      if (!mounted) return;
      setState(() {
        _notice = ok
            ? 'candidate $id confirmed (stored: ${out['stored'] ?? '?'})'
            : 'candidate $id rejected (${out['reason'] ?? 'dropped'})';
      });
    } catch (e) {
      if (!mounted) return;
      if (isMemoryNotFound(e)) {
        setState(() => _notice = 'candidate $id gone (removed elsewhere)');
      } else {
        setState(() => _error = 'confirm $id ${describeMemoryError(e)}: $e');
        return;
      }
    } finally {
      _deciding.remove(id);
    }
    await _refreshCandidates();
  }

  Future<void> _purge() async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = '';
      _notice = '';
    });
    try {
      final out = await _backend.purge();
      if (!mounted) return;
      setState(() => _notice = 'purged ${out['purged'] ?? 0} expired memories');
    } catch (e) {
      if (mounted) setState(() => _error = 'purge ${describeMemoryError(e)}: $e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        TextField(
          controller: _queryCtrl,
          decoration: const InputDecoration(
            labelText: 'Search memories',
            hintText: 'uses operator token from Tasks section',
          ),
          onSubmitted: (_) => _search(),
        ),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            FilledButton(
              onPressed: _busy ? null : _search,
              child: const Text('Search'),
            ),
            OutlinedButton(
              onPressed: _busy ? null : _purge,
              child: const Text('Purge expired'),
            ),
          ],
        ),
        if (_searched && _hits.isEmpty && _notice.isEmpty)
          const Text('no memories matched'),
        for (final h in _hits)
          Card(
            child: Padding(
              padding: const EdgeInsets.all(8),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    h.text.isEmpty ? '(empty text)' : h.text,
                    style: Theme.of(context).textTheme.bodyMedium,
                  ),
                  Text(
                    'category: ${h.category.isEmpty ? '?' : h.category}'
                    '${h.score == null ? '' : ' · score: ${h.score!.toStringAsFixed(3)}'}',
                  ),
                  if (_removedElsewhere.contains(h.id))
                    const Text(
                      'removed elsewhere — nothing to forget',
                      style: TextStyle(color: Colors.orange),
                    ),
                  const SizedBox(height: 4),
                  OutlinedButton(
                    onPressed: (_forgetting.contains(h.id))
                        ? null
                        : () => _forget(h.id),
                    child: const Text('Forget'),
                  ),
                ],
              ),
            ),
          ),
        const SizedBox(height: 8),
        TextField(
          controller: _forgetCtrl,
          decoration: const InputDecoration(
            labelText: 'Forget by memory id',
            hintText: 'Core search omits ids — paste one here',
          ),
          onSubmitted: (_) => _forgetById(),
        ),
        const SizedBox(height: 8),
        OutlinedButton(
          onPressed: _busy ? null : _forgetById,
          child: const Text('Forget id'),
        ),
        const SizedBox(height: 8),
        const Text('Pending candidates (refresh to load — never automatic)'),
        const SizedBox(height: 4),
        OutlinedButton(
          onPressed: _candidatesBusy ? null : _refreshCandidates,
          child: const Text('Refresh candidates'),
        ),
        if (!_candidatesLoaded)
          const Text('candidates not loaded — tap Refresh'),
        if (_candidatesLoaded && _candidates.isEmpty)
          const Text('no pending candidates'),
        for (final c in _candidates)
          Card(
            child: Padding(
              padding: const EdgeInsets.all(8),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    c.text.isEmpty ? '(empty text)' : c.text,
                    style: Theme.of(context).textTheme.bodyMedium,
                  ),
                  Text(
                    'category: ${c.category.isEmpty ? '?' : c.category}'
                    ' · source: ${c.source.isEmpty ? '?' : c.source}',
                  ),
                  Text('id: ${c.candidateId.isEmpty ? '?' : c.candidateId}'),
                  const SizedBox(height: 4),
                  Wrap(
                    spacing: 8,
                    children: [
                      OutlinedButton(
                        onPressed: _deciding.contains(c.candidateId)
                            ? null
                            : () => _decide(c, true),
                        child: const Text('Accept'),
                      ),
                      OutlinedButton(
                        onPressed: _deciding.contains(c.candidateId)
                            ? null
                            : () => _decide(c, false),
                        child: const Text('Reject'),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
        if (_notice.isNotEmpty) Text(_notice),
        if (_error.isNotEmpty)
          Text('Memory: $_error',
              style: const TextStyle(color: Colors.red)),
      ],
    );
  }
}
