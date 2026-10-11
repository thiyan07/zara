# CANONICAL SCHEDULER — implementation

**Status: IMPLEMENTED (ranking centralization + explanation writer).
No weights. No scores. No order changes. No ranking-behavior change.**

## Canonical module and API boundaries

`core/scheduler.py` (appended to the pre-existing Stage-1 `Scheduler`
timer class, which is untouched) owns pure ranking + explanation:

- `resolver_rank_key(...)` — canonical 10-tuple (promoted `_rank_key`).
- `fabric_rank_key(...)` — canonical 3-tuple (promoted `_rank`;
  tiebreak stays with the caller, as before).
- `kind_device_key(...)` — canonical 2-tuple (Router/select).
- `order_candidates(items, key_fn)` — stable, non-mutating sort.
- `explain_decision(...)` — bounded deterministic explanation writer.
- Constants: `KIND_ORDER`, `TRUST_RANK`, `PRESENCE_RANK`,
  `STRATEGY_*`, `SCHEDULER_VERSION="v1"`, `EXPLAIN_*` budgets,
  `REASON_CODES` (13 closed codes).

Hard rules: stdlib only (`json`, `re`); no policy/engine/registry/jobs/
auth/store imports; no I/O, locks, threads, network, disk, subprocess;
no weights. Pinned by `test_S12` source scan.

## Legacy-to-canonical delegation mapping

| Entry point | Change | Contract preserved |
|---|---|---|
| `CapabilityResolver._rank_key` | data fetch stays; key assembly calls `scheduler.resolver_rank_key` (custom `kind_order` honored via `kind_ranks` param) | 10-tuple identical (NaN battery normalizes to unknown-last; previously undefined sort — documented micro-difference, unreachable via validated ingress) |
| `FabricRegistry._rank` | battery lookup stays; calls `scheduler.fabric_rank_key` | 3-tuple identical; tiebreak still at call site |
| `Router.route` sort | calls `scheduler.kind_device_key` | 2-tuple identical |
| `DeviceManager.select` sort | calls `scheduler.kind_device_key` | 2-tuple identical (str-Enum maps by value) |

## Strategies preserved and ordering contracts

Three named strategies (`lexicographic`, `fabric-kind`, `kind-device`)
— one per legacy caller. NO universal ordering imposed; each caller
keeps its exact contract (verified by goldens). `test_C19` still passes
(`battery` retained in both method sources).

## Golden fixture provenance and results

Goldens captured from PRE-migration code on a 5-device fleet
(m1/linux/88, a1/android/5, c1/cloud/None, m2/linux/88,
zz/android/100): fabric rank vectors, resolver order
`[c1,m1,m2,zz,a1]`, route/router/select winner `c1`. Post-migration
tests assert byte-equality (S01–S03); S06 proves insertion-order
independence.

## Determinism and tie-breaking

Total order everywhere (device_id asc terminal tiebreak); permutation
stable (S06); no input mutation (S07); explanation deterministic +
field-ordered (S08).

## Eligibility versus ranking safeguards

Scheduler takes pre-authorized values only; `eligible` flags and reason
codes are caller-supplied; writer cannot flip them. S13 proves a
revoked candidate still sorts (key function) while resolve never
selects it (ranking ≠ authorization).

## Explanation schema, limits, privacy

`zara.sched-explain/v1`, fixed 12-field order, ≤2000 chars (truncate
tail + flag, then minimal fallback), ≤32 candidates, ≤8 tied, detail
≤200, closed reason codes, secret-regex redaction, fingerprint never
emitted, failure returns minimal record without touching decisions
(S08–S11).

## Security and approval invariants

Approval fingerprint/TTL/revalidate/terminal/revoke paths untouched
(full suites green). Writer has no engine/policy refs (S12).

## Concurrency and lifecycle

Pure functions; no shared state; snapshot copy-out discipline
unchanged; background `_run` untouched.

## Tests and results

`tests/test_scheduler.py`: 13 tests (goldens S01–S03, equivalence
S04–S07, explanation S08–S11, non-authority S12–S13). Full Python suite
+ Flutter + calibration harness below.

## Compatibility and behavior equivalence

Wire shapes, status strings, audit actions, approval 409s, Android
expectations unchanged. Sync/async paths frozen.

## Remaining ranking paths and gaps

`FallbackRegistry._risk_rank` (boolean gate, not ordering — correctly
out of scope); `_RISK_COST` (governor cost scalar, not a ranker);
`_version_key` (key fragment, stays in resolver — scheduler takes the
parsed tuple). No other sorters found (grep-verified).

## Implementation gaps

Explanation writer not yet attached to resolve responses (compat:
would change wire shapes; standalone + tested). Shadow comparison not
implemented (design defers). Fairness/round-robin not implemented.
Weights forbidden until evidence gates pass.

## Governor v2 readiness

STILL NOT READY: scheduler centralized but not yet authoritative-single
(migration without deletion is step 1 of 3); calibration N=1;
divergence report unmeasured; fault/rollback logs absent.
