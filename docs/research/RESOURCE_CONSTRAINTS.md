# RESOURCE CONSTRAINTS — closed filter model (non-authoritative)

**Status: IMPLEMENTED. Filters only; no ranking, no authority.**

## Model

`core/models.py::ResolveConstraints` (pydantic, `extra="forbid"`):
`platform` (str ≤64, default ""), `min_version` (str ≤64, default ""),
`risk_max` (str ≤16, default ""), `online_only` (bool, default False).
Unknown keys rejected at the model; unknown/invalid values are
no-ops or fail-closed excludes at evaluation — never matches.

## Normalization

`resolver.normalize_constraints(raw)`: None→{}; model→dump; dict→
validate-or-lenient-allowlist-pick (known keys, correctly typed values
only); anything else→{}. Never raises. `resolve()` normalizes at entry;
`resolve_proposal` whitelists through it. HTTP envelope guards (≤8/≤20
keys, 400) unchanged — no 422 churn for existing clients.

## Evaluation (unchanged semantics)

`_constraints_match` per candidate, before `authorize_capability`:
platform membership (legacy empty = unconstrained), min_version
ordering (unparsable sorts lowest), risk_max ladder
(safe<confirm<high_risk; unknown value = most permissive = no-op),
online_only opt-in (offline/unknown excluded; default includes so
authorize reports OFFLINE honestly). Malformed → False (exclude), never
raise. Conjunctive AND across keys.

## Authority boundary

Constraints FILTER authorized candidates only. They cannot authorize,
approve, grant trust, clear revocation, select targets, or substitute
devices. Revoked/offline/policy-blocked verdicts win regardless of
constraints. Forged `PROPOSAL_SECURITY_FIELDS` never read (extended
by construction: binding/grant keys are not constraint keys).

## Demand interaction

Demand predicates evaluate inside `governor.check` (authorize path);
constraints evaluate before authorize per candidate. Order: constraint
filter → authorize (trust/presence/policy/governor+demand) → rank.
Constraint mismatch yields filtered-no-device, never defer confusion.
