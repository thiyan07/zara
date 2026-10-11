# GOVERNOR V2 — integration (deterministic, reversible, off by default)

**Status: IMPLEMENTED. Disabled by default. NOT rollout-ready.**

## Integration architecture and call paths

- Policy boundary: `governor.evaluate_policy(snapshot, demands,
  constraints_ok, telemetry_stale, cost, risk)` → `PolicyVerdict`
  (`eligible|deferred|rejected|legacy`, closed reason codes). Pure.
- Flags: `ZARA_GOVERNOR_V2` / `ZARA_GOVERNOR_SHADOW` (env `1/true/yes/on`
  → on; missing/invalid → off). Observable in `GET /v1/ready`
  (`governor_v2`, `governor_shadow` keys, additive).
- Explained resolve: `resolver.resolve_explained()` (identical resolve
  + bounded explanation) and `explain_resolution()` (no re-resolve);
  opt-in `explain=true` on `POST /v1/executions/resolve`.
- Shadow: `authorize_capability` evaluates the v2 boundary alongside
  the legacy gate when `ZARA_GOVERNOR_SHADOW=1`; divergence audits
  `shadow_divergence` only; verdict never altered; exceptions swallowed.

## Feature flags and safe defaults

Off by default; invalid resolves off; no telemetry/evidence can enable;
legacy paths byte-identical when off (full suite proves). Rollback =
unset flag (no migration, no re-pair, no redeploy).

## Disabled-mode behavior

Legacy decisions exactly (874-suite green with flags off). No alternate
scheduler runs; shadow emits nothing.

## Enabled-mode flow

Same verdicts through the same gates; additionally: per-candidate
policy verdicts available, explanations on request, shadow divergence
audited. Ranking strategies untouched; approval/fingerprint/TTL/
revalidate/terminal paths untouched.

## Ranking preservation

`resolver_rank_key`, `fabric_rank_key`, `kind_device_key`,
`order_candidates` contracts unchanged. Legacy wrappers delegate as
before. No new ordering.

## Explanation reason codes

`eligible`, `demand-defer`, `cost-defer`, `constraint-reject`,
`unknown-demand-defer`, `stale-demand-defer`, `legacy-mode`,
`unreadable-demand` (+ scheduler closed codes in explanations).
Bounded detail (≤200), no secrets, fingerprint never exposed.

## Shadow behavior

Read-only comparison (legacy gate vs v2 boundary) on live authorize
traffic when enabled; audit-on-divergence only; never dispatches,
approves, grants, or alters binding.

## Concurrency and security invariants

Pure boundary (no locks/IO); existing TOCTOU coverage unchanged
(approve-time + HIGH_RISK pre-dispatch revalidation); terminal
precedence untouched; telemetry-never-authorizes pinned; revoke
linearization untouched.

## Tests and results

`tests/test_governor_v2.py`: 25 tests (flags, boundary, explained
resolve, shadow, security). Full Python + Flutter + calibration below.

## Live measurements and provenance

Emulator + Linux verification with timestamps (see task report).

## Known gaps

N≥30 measured cells absent; divergence report unmeasured at scale;
fault/rollback logs manual-only; Android CPU/GPU unavailable; network
QoS absent; tombstone memory-mode restart gap; `parked_at` overwrite on
re-park.

## Rollout and rollback steps

1. Set `ZARA_GOVERNOR_V2=1` on canary; confirm `/v1/ready` flags.
2. Compare shadow/audit divergence + suite goldens.
3. Rollback: unset flag; legacy path resumes; no data backfill.
4. Never enable globally without N≥30 evidence + derivation memo.

## Readiness verdict

NOT READY for rollout (evidence gates unmet — see task report §U).
Implementation complete and reversible.
