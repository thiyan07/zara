# Security Model

## Layers

1. **OS permissions** (Android runtime permissions, Linux UID) — outermost.
   Our policy never grants what the OS denies.
2. **Policy engine** (`core/policy.py`) — capability-based, least privilege,
   risk levels (`safe|confirm|high_risk`), scoped expiring grants.
3. **Execution sandbox** — tools declare schemas; inputs validated; timeouts
   enforced; deny-patterns block destructive commands even if mislabelled.

## Rules

- LLM output is **untrusted input**: it may propose, never execute.
  `ExecutionEngine.submit()` requires a `PolicyDecision`.
- No secrets in: source, RAG memory (`memory.py` refuses items with
  `secret|password|api[_-]?key|token` markers and `secret: true` category),
  logs, prompts, git. Secrets live in env vars / device keystores only
  (Stage 2 wires Android Keystore + server env; Stage 1: `ASSISTANT_TOKEN`).
- Every device has unique identity + token auth (Stage 1: dev token gate in
  `app.py`; Stage 2: per-device registration/revocation in `devices.py`).
- Transport: HTTPS/WSS in production (Oracle gateway); Stage 1 local HTTP
  only for dev/test.
- Audit: every request/tool/decision/execution/approval/result logged via
  `core/audit.py` (append-only SQLite table + in-memory ring). Answers
  "what did the assistant do?" without storing hidden chain-of-thought —
  only operational metadata.
- Destructive ops (`delete`, `shutdown`, credential changes, mass file
  mutation) are `high_risk`: require explicit human approval + audit entry.
  `confirm` ops (send message, install software, external side effects)
  require scoped grants.
- Verification: `confirm`-level and above tools must define verifiable
  success criteria; engine marks `verified` only after check passes.

## Stage 2 addition

Per-device identity replaces the shared token for devices: one-time pairing
codes, per-device keys (hashed server-side), expiry, revocation, 403 on
forgery. Tool allowlists on the device back up core policy. Full detail:
docs/DEVICE_SECURITY.md. Dev bearer token is local-dev only.
