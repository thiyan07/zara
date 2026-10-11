# RESOLVER → ASYNC EXECUTION — canonical path + approval binding

**Status: IMPLEMENTED. Additive only. Sync paths frozen.**

## Canonical path (Part A)

```
request
  ↓  POST /v1/executions/resolve {capability, inputs, device_id?, constraints?}
resolver.resolve → authorized concrete target (or 403/409, zero executions)
  ↓  submit_async(tool=capability, inputs, device, capability, binding)
approval if required (202 parked, no job, no thread)
  ↓  approve / approve_async (re-validated)
device job → poll → complete
  ↓
GET /v1/executions/{id} → status + result
```

`POST /v1/executions/resolve` is the canonical external entry. Direct
`/v1/exec`, `/v1/dispatch`, talk/intent/session paths keep sync semantics
(compatibility inventory in prior review; migration is future opt-in).

## Resolution binding

The execution record carries `capability` + `binding`
(`resolution_status`, `fallback_used`, `substituted`, `reason`). Binding
is advisory record, never authority:
- `_prepare` always re-decides policy from (tool, device, inputs, risk).
- Approve re-validates trust/presence/policy/capability.
- No silent substitution: the record stays on the resolved device;
  fallback is a new resolve, never a redirect (resolver-owned flags echoed).

Status views echo `capability` + binding summary. The approval
fingerprint is integrity material and never exposed.

## Approval binding (Part B)

Parked records store `approval_fingerprint` (SHA-256/32 hex over canonical
JSON `{capability, device, tool, tool_version, risk, args}` — exact form,
never lossy-normalized; 64 KiB cap) and `parked_at`.

`_grant` enforces, in order:
1. state is WAITING_FOR_PERMISSION (else ValueError→409);
2. park age within `APPROVAL_TTL_S` = 300 s (else re-park + ValueError —
   stays approvable, never dispatches stale);
3. recomputed fingerprint equals parked (else DENIED — args/device/tool/
   risk/capability drift);
4. revalidate hook — trust executable, presence fresh, capability
   authorizes, descriptors not stale, policy re-decide not hard-deny
   (else DENIED).

`_run` re-checks the hook pre-dispatch for HIGH_RISK tools only
(approve→dispatch window). Non-high-risk relies on the approve gate
(documented; keeps hot paths hot).

## Approval window

300 s default, matching `PolicyEngine` grant TTL and inside the 900 s
session approval horizon. High-risk takes the short end with no extension.
Expiry re-parks (fresh window + fingerprint); the user approves again.
Never auto-extends, never dispatches expired.

## Terminal precedence

CANCELLED > EXPIRED > done/failed (first terminal wins; late writes
dropped). Revoked → CANCELLED (pending) / device-owned (claimed, result
endpoint auth-gates). DENIED pre-execution only. `InterruptedError` from a
cancel wake maps to CANCELLED, never FAILED.

## Fallback semantics

Resolver-owned only (`FallbackRegistry`, depth ≤3, destructive never
fails over). Substitution is labeled (`substituted: true`) for safe risk
on presence/governor/OS failure; high-risk pinned-offline never migrates.
Async submit follows the ranked winner; repeated submits mint distinct ids.

## Security properties

- Capability requests MUST resolve; direct tool/device submit still passes
  policy + (new) approve/dispatch revalidation.
- `execution_id` non-authoritative; device endpoints scope-bound;
  `agent_result` device-bound; unknown ids 404; terminal cancel 409.
- Approval binds exact (capability, device, tool, args, risk); mutation →
  DENIED; replay onto another execution impossible (per-record fingerprint).
- Audit metadata only (no inputs/results/secrets); fingerprints never
  logged or exposed.
- Single-bearer operator plane: no per-user isolation (unchanged).

## Known limitations

- Records/jobs in-memory (restart drops); no list/TTL endpoints.
- Revalidate hook is build_stack-wired; direct-engine unit contexts skip
  trust/presence checks (fingerprint + TTL still enforced).
- Consent staleness inside the 300 s window accepted for lab pilot.
- Governor v2 explicitly NOT implemented (readiness assessment below).

## Governor v2 readiness assessment: NOT READY

Per review: current governor is a battery-first binary gate
(`proceed|defer|reroute`, no scores). `cpu_pressure`/`low_power_mode`
declared but unwired (builders never pass them); GPU/RAM/storage/latency/
thermal absent from `ResourceSnapshot`; `estimated_cost` is a scalar, not a
demand vector; no user-constraint language (deadline, charger/wifi-only,
budgets); three divergent rankers (Router vs route_capability vs resolve).
Prerequisites in order: telemetry fields → demand vectors → snapshot
provider → constraint schema → single scheduler → emulator calibration.
