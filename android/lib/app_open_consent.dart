// P6-c26 (2026-10-24): the PHONE's own per-app OPEN consents — the
// consent surface behind the Phase-1 app-open allowlist expansion.
//
// Design contract (never relaxed by this file):
// - the byte-equal allowlist refusal stays the DEFAULT: an
//   out-of-allowlist package is refused unless THIS human consented
//   ON THIS PHONE for THAT package, by a real dialog tap;
// - the consent is a DEVICE-side decision. Core cannot add, revoke,
//   or enumerate grants over the job channel: every API here is
//   device-local, and the runner is the only consumer;
// - a grant is per-package (package name only — never display
//   labels, paths, or free text) and is REVERSIBLE from this very
//   phone (revoke()); grants are stored in SecureStore (Keystore
//   backed, the same storage device credentials use);
// - nothing in this class opens anything. It holds consent state
//   only; launching stays the runner + gateway's job under the
//   Core CONFIRM gate that already applies per run.
import 'dart:convert';

import 'secure_store.dart';

class AppOpenConsents {
  static const _slot = 'zara_appopen_consents';

  final SecureStore _store;
  bool _loaded = false;
  Set<String> _grants = {};

  AppOpenConsents(this._store);

  /// Package-name validation: a package is [A-Za-z0-9_]. segments ≤ 100
  /// chars total. Anything else is refused storage (never stored,
  /// never treated as granted).
  static bool validPackage(String pkg) {
    if (pkg.isEmpty || pkg.length > 100) return false;
    return RegExp(r"^[A-Za-z0-9_\.]+$").hasMatch(pkg);
  }

  Future<void> _load() async {
    if (_loaded) return;
    var raw = '';
    try {
      raw = await _store.readValue(_slot) ?? '';
    } catch (_) {
      raw = '';
    }
    try {
      final v = jsonDecode(raw.isEmpty ? '[]' : raw);
      _grants = {}
        ..addAll((v is List ? v : [])
            .whereType<String>()
            .where(validPackage));
    } on FormatException {
      _grants = {}; // corrupt slot = no grants (default refusal wins)
    }
    _loaded = true;
  }

  Future<void> _save() async {
    await _store.writeValue(_slot, jsonEncode(_grants.toList()));
  }

  Future<List<String>> list() async {
    await _load();
    return _grants.toList()..sort();
  }

  /// True only when a real prior consent exists for exactly this
  /// package.
  Future<bool> allows(String pkg) async {
    await _load();
    return validPackage(pkg) && _grants.contains(pkg);
  }

  /// A user tap on the consent dialog. Invalid packages are refused
  /// storage (the runner's own validation keeps bad shapes away; this
  /// is the second, independent inside the trust boundary).
  Future<void> grant(String pkg) async {
    await _load();
    if (!validPackage(pkg)) return;
    _grants.add(pkg);
    await _save();
  }

  /// Device-local revocation. Returns whether a grant existed.
  Future<bool> revoke(String pkg) async {
    await _load();
    if (!validPackage(pkg) || !_grants.contains(pkg)) {
      return false;
    }
    _grants.remove(pkg);
    await _save();
    return true;
  }
}
