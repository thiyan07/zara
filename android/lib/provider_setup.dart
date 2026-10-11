// Provider setup panel: read-only explanation + connection check.
//
// Read-only over GET /v1/ready checks.provider (no auth, no secrets).
// This panel NEVER takes credentials: no TextField, no secure storage,
// no shared prefs writes. Provider state is derived ONLY from the
// ready payload (echo/scripted/openai-compatible), never from local
// guesses. Operator configures the Core process environment
// (LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY) and restarts
// Core; this panel only explains + verifies reachability.
import 'package:flutter/material.dart';

import 'core_client.dart';
import 'diagnostics_panel.dart'
    show ReadyBackend, ReadyInfo, CoreReadyBackend, redactSecretsForDisplay;

/// True for the built-in keyless providers (no live inference).
/// Pure for testing.
bool isBuiltInProvider(String provider) =>
    provider == 'echo' || provider == 'scripted';

/// Display label for a ready checks.provider string.
/// Pure for testing.
String describeProviderKind(String provider) {
  switch (provider) {
    case 'echo':
      return 'echo (built-in, no key)';
    case 'scripted':
      return 'scripted (built-in, no key)';
    case 'openai-compatible':
      return 'openai-compatible';
    case '?':
      return '?';
    default:
      return provider;
  }
}

/// Configured/unconfigured state derived ONLY from checks.provider.
/// openai-compatible = configured; echo/scripted = unconfigured for live
/// inference; anything else = unknown. Pure for testing.
String providerSetupState(String provider) {
  if (provider == 'openai-compatible') return 'configured';
  if (isBuiltInProvider(provider)) return 'unconfigured for live inference';
  return 'unknown';
}

/// One-line rendering of a provider connection-check failure.
/// Pure for testing.
String describeProviderCheckError(Object e) {
  final s = e.toString();
  if (s.contains('401') || s.contains('403')) {
    return 'reachable but refused (unauthorized)';
  }
  return 'unreachable';
}

/// Read-only provider setup explainer + connection check.
///
/// Hard rules (asserted in tests):
/// - No TextField/TextFormField anywhere in this widget subtree.
/// - No writes to secure storage / shared prefs (no such imports).
/// - Ready output shows only the redacted provider string + booleans.
class ProviderSetupPanel extends StatefulWidget {
  final CoreClient client;

  /// Test seam: production passes null (fetch runs on [client] via ready).
  final ReadyBackend? backend;
  const ProviderSetupPanel({super.key, required this.client, this.backend});

  @override
  State<ProviderSetupPanel> createState() => _ProviderSetupPanelState();
}

class _ProviderSetupPanelState extends State<ProviderSetupPanel> {
  ReadyInfo? _ready;
  String _status = '';
  bool _busy = false;

  ReadyBackend get _backend =>
      widget.backend ?? CoreReadyBackend(widget.client);

  Future<void> _check() async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _status = '';
    });
    try {
      final info = await _backend.fetch();
      if (!mounted) return;
      setState(() {
        _ready = info;
        _status = 'reachable — provider ${describeProviderKind(info.provider)}';
      });
    } catch (e) {
      if (mounted) setState(() => _status = describeProviderCheckError(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final r = _ready;
    final builtIn = r != null && isBuiltInProvider(r.provider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Provider setup (Core environment, operator-owned)',
            style: Theme.of(context).textTheme.titleSmall),
        const Text('The model provider is configured on Zara Core via '
            'environment variables, not in this app. This panel never '
            'asks for keys and stores nothing.'),
        const SizedBox(height: 4),
        const Text('LLM_PROVIDER: echo | scripted | openai-compatible '
            '(default echo when unset).'),
        const Text('LLM_BASE_URL: base URL for openai-compatible, e.g. a '
            'local Ollama or LMStudio URL. Requests go to '
            '/chat/completions under this base URL.'),
        const Text('LLM_MODEL: model name sent to the provider.'),
        const Text('LLM_API_KEY: key for openai-compatible (server-side '
            'only — never entered here).'),
        const SizedBox(height: 4),
        const Text('Setup steps (operator): 1) export LLM_PROVIDER, '
            'LLM_BASE_URL, LLM_MODEL, LLM_API_KEY on the Core host '
            '(e.g. export LLM_PROVIDER=openai-compatible). '
            '2) Core restart by operator is required for new values. '
            '3) Return here and use the connection check below.'),
        const SizedBox(height: 4),
        if (r == null)
          const Text('Provider: unknown — check connection to find out'),
        if (r != null) ...[
          Text('Provider: ${redactSecretsForDisplay(describeProviderKind(r.provider))}'),
          Text('Setup state: ${providerSetupState(r.provider)}'),
        ],
        if (builtIn)
          const Text('live inference unverified without credentials: '
              'echo/scripted providers answer locally and never call a '
              'model API.'),
        if (_status.isNotEmpty) Text('Connection: $_status'),
        const SizedBox(height: 8),
        OutlinedButton(
          onPressed: _busy ? null : _check,
          child: const Text('Check provider connection'),
        ),
      ],
    );
  }
}
