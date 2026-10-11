# CANONICAL RESOURCE SNAPSHOT — one provider, bounded demand, safe constraints

**Status: IMPLEMENTED. No scheduler weights. No ranking changes.**

## Previous architecture

Five construction sites built `ResourceSnapshot` independently
(`fabric._snapshot_for`, `routing.Router.route` + `_route_local`,
`app.py` ×2), plus parallel `FabricDeviceView`s. Same device could
govern differently per path (`kind.value` vs `_kind_of` normalization).

## Canonical provider

- `governor.canonical_snapshot(...)` — the ONE constructor. Closed kind
  normalization inside (`normalize_device_kind`); bare defaults on
  missing input. Pure function, no I/O.
- `FabricRegistry.resource_snapshot(device_id)` — the ONE read funnel
  (authorize, execute, transfer, app exec paths). Built from a single
  atomic `DeviceManager.snapshot()` deep-copy; unknown devices yield
  the bare default, never raise. `_snapshot_for` kept as a delegating
  wrapper (compatibility).
- `DeviceManager.snapshot()` — locked `model_copy(deep=True)`; no new
  lock. `get()`/`list()` untouched. `device_view` composes from the
  copy (inputs no longer torn).
- Routing/app sites call the canonical constructor/provider with
  value-identical outputs (DeviceKind is a closed enum, so kind
  unification changes no real outcome).

## Lifecycle

Heartbeat (only telemetry writer) → `last_telemetry_at` stamped iff ≥1
non-None telemetry field → presence from `last_seen` (unchanged) →
reconnect re-registers fresh (telemetry unknown, descriptors stale) →
offline keeps readable-but-stale telemetry → revoke deletes the object
(snapshot falls back to default; tombstone keeps trust terminal).

## Freshness: LIVE / STALE / UNKNOWN / UNAVAILABLE

- Stored clocks only: `last_seen` (any contact), `last_telemetry_at`
  (telemetry-bearing contact). Computed: `derive_presence` (120 s),
  `telemetry_stale(id, 120 s)`.
- UNKNOWN: never reported (`None`, never zero). STALE: value readable,
  must be treated as unknown. UNAVAILABLE: platform lacks the signal
  (Android CPU/GPU) — no field or permanent `None`, documented.
- No per-field timestamps; no second clock; device clock never trusted.
- Snapshot builders do NOT null stale fields (stale low-power still
  defers — fail-safe direction); readers consult `telemetry_stale()`.

## Security model

Snapshot ≠ authorization ≠ approval ≠ trust ≠ execution authority
(verified per-consumer; governor defer is availability-only).
Write path: `DeviceManager.heartbeat` via device-key-authed endpoint
only; poll advances presence without touching telemetry; no LLM/intent/
resolver write handle exists. Malformed → 422, stored values untouched.

## Reconnect / revoke

Reconnect: fresh state, telemetry unknown, descriptors flagged stale
until re-describe; trust untouched. Revoke: linearized (queue-cancel →
auth → offline → tombstone → transfers); object deleted so no stale
snapshot lingers; audit preserved.

## Demand descriptors

`ResourceDemands{cpu,memory,network,battery}` × `{low,moderate,high}`
(81 combos), all-LOW default, closed model, on `ToolDefinition`. Read
by zero gates — description only.

## Constraint foundation

Reuse resolver's 4 keys (`platform/min_version/risk_max/online_only`);
unknown keys ignored, unknown `risk_max` values ignored (documented),
revoked targets never resolve. No new schema; no ranking.

## Ranker inventory (documented, NOT rewritten)

1. `FabricRegistry._rank` (kind, presence, −battery).
2. `CapabilityResolver._rank_key` (11-tuple).
3. `Router.route` (`cloud<linux<android`, device_id tiebreak).
Pinned by `test_C19`. Canonical scheduler is future work.

## Calibration plan (before any weights)

Idle → light CPU → moderate CPU → light memory → moderate memory →
network op → power-save, recording before/during/after per signal on
emulator + Linux. Have: idle/light Android mem + idle Linux full vector.
No stress runs, no sustained thermal load. Weights forbidden until the
set is complete and reviewed.

## Demand Threshold Semantics

Pure predicate `governor.demand_defer_reason(snapshot, demands,
telemetry_stale)` → reason or None. No I/O, no locks, no mutation.

| Dimension | LOW | MODERATE | HIGH |
|---|---|---|---|
| memory_pct | defer if valid & >98 | defer if valid & >93, or unverifiable | defer if valid & >85, or unverifiable |
| cpu_pct | defer if valid & >98 | defer if valid & >90, or unverifiable | defer if valid & >75, or unverifiable |
| battery | nothing beyond cost gates | defer if low-power, or valid <30% uncharged | + defer if fully unverifiable |
| network | no predicate (name-only telemetry; deferred dimension) |

Bounds: memory {98/93/85}, cpu {98/90/75}, battery band 30. All
boundaries STRICT `>` (value == bound proceeds; matches existing
governor style). Order: memory → cpu → battery; first hit wins,
deterministic reasons. Invalid/NaN/out-of-range/wrong-type =
unverifiable. Stale snapshots evaluate field-wise as unknown (never
silently refreshed). Opt-in via `check(..., demands=...)`; default
path byte-identical. Wired at fabric authorize + execute_on_device +
routing + app exec paths (all-LOW fleet: zero outcome change).
Network demand intentionally unevaluated; storage/temp/thermal have no
demand dimensions. No scores, no weights, no ranking changes.

Future sequence: demand predicates → constraint schema → calibration
→ single scheduler → Governor v2 readiness.

## Live verification (2026-10-09, ZaraLab API 36)

- Canonical `system.battery` exec: 201 in 0 s → succeeded with real result.
- HIGH_RISK `gui.tap` via canonical endpoint: 202 parked, zero job →
  approve → running → device safely refuses blind tap (no node_id) →
  terminal failed (fail-closed, audited).
- Full suite 819 passed; Flutter 86; analyze clean.

## Governor v2 readiness: STILL NOT READY

Done: single snapshot source, bounded demand, constraint reuse, freshness
discipline. Still missing: demand-threshold semantics, constraint schema
extension, single scheduler, full telemetry (GPU/RAM-detail/latency),
calibration set.
