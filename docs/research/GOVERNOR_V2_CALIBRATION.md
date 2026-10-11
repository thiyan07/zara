# GOVERNOR V2 CALIBRATION — evidence, methodology, readiness

**Status: EVIDENCE ONLY. No scheduler. No weights. No ranking changes.**

## Architecture and data flow

`tests/test_calibration.py` (harness, ~300 lines, stdlib+pytest only):
seeded generators → existing pure functions (`demand_defer_reason`,
`Governor.check`, `normalize_constraints`, `resolver.resolve` on
make_stack fixtures) → `make_record()` (validates schema/bounds/secrets)
→ `docs/research/evidence/calibration_v1.jsonl` + `.summary.json`.
No execution dispatch, no approvals, no production imports of tests.

## How to run

`PYTHONHASHSEED=42 python -m pytest tests/test_calibration.py -q`
Deterministic: fixed seed + fixed stamps; K00 proves byte-identical reruns.

## Evidence schema and provenance

`zara.evidence/v1`: scenario_id, category, seed, source
(measured|synthetic|replayed), device_class, observed_at, command,
metrics[] (name/value/unit/at), freshness{}, demand{}, thresholds{},
constraints{}, resolver{}, execution{not_executed}, expected/actual,
mismatch_class, calibration_suitable, notes≤300. Caps: 500 records,
256 KiB, 8 KiB/record, secret-pattern scan.

## Reproducibility

Fixed seed 42; no wall-clock in generated output; K00 byte-equality;
K06 summary self-consistency; K03 proves zero engine records created.

## Measured vs synthetic vs replayed

39 records: 31 synthetic (demand boundaries/unknown/invalid),
6 replayed (resolver incl. revoke), 2 measured (Task-3 baselines,
provenance-cited, `calibration_suitable: false`). Mismatches: none
(all goldens agree with current behavior — regression, not improvement).

## Scenario coverage

Demand bounds (below/at/above × 3 tiers × cpu/mem/battery), unknown/
stale/invalid per tier, low-power, multi-dimension order, network
no-predicate, resolver 4-key combos + revoke, all-LOW baseline.

## Observed mismatches

None. Any future divergence fails K01 (predicate drift) by design.

## Unavailable metrics

GPU (both), Android CPU%, Linux thermal/battery-temp/low-power,
network QoS, per-field timestamps. Never faked; absent from evidence
except as documented `None`.

## Limitations and bias

N=1 measured cells (unsuitable for weights); synthetic ≠ statistical
evidence; single emulator + single laptop; light loads only; resolver
goldens pin current behavior (agreement ≠ optimality).

## Security boundaries

Evidence never authorizes/approves/dispatches (K03: zero records);
secret scan on every record; size caps; malformed quarantined.

## Readiness criteria for Governor v2

NOT READY: needs N≥30 measured cells, threshold-derivation memo,
ranker-divergence report, fault/rollback logs, gap acceptances.

## Work intentionally not done

Weights, scores, ranking, migration, scheduler, constraint extension,
GPU/latency telemetry, stress runs, TTL/lease/push.
