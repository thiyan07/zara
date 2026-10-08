# Zara Tool Validator Forensic Review

Status: REVIEWED — decision **C (NARROW FIX REQUIRED)**, fix applied,
all suites green. No commit. Stages 15–19 + Rung-1 preserved.

## Scope

Forensic review of the Rung-1 hardening of `core/tools.py`
`validate_against_schema` (envelope bounds + `additionalProperties`),
covering correctness, compatibility, security value, and the one
concrete overbreadth found (per-tool envelope override). Seven
read-only forensic subagents supplied evidence; every load-bearing
claim below was independently re-verified against code and test runs.

## Starting Repository State

HEAD `4f03492`, branch `stage15-real-transfer`, Stages 15–19 plus
Rung-1 implementation uncommitted and intact; baselines 507 Python /
71 Flutter green; ~22 GB free. No code changes by the review except
the narrow fix documented below.

## Exact Change Reviewed

`git diff HEAD -- core/tools.py` (Rung-1 only):

1. Non-dict input → `["input must be an object"]`, hoisted before the
   `type == "object"` gate (previously fail-open for schemas without
   `"type"`).
2. Envelope bounds on every validated call: >16 top-level keys, >4096
   serialized JSON bytes, nesting depth >4.
3. `additionalProperties: False` honored — but ONLY when a schema
   explicitly declares it (today: exactly one tool,
   `android.app.force_stop`).

Post-review narrow fix (this task, see Decision): per-tool
`max_input_bytes` override (`ToolDefinition` field, default 4096),
threaded through `registry.call` and `conversation._validate_proposal`;
`code.apply_patch` set to 102400 (its handler's own contract),
MCP adapter definitions set to 8192 (transport `MAX_ARGS_BYTES`);
envelope checks moved above the falsy-schema early return; unknown
type names fail closed instead of raising `KeyError`.

## Validator Behavior

- Shape checks (required/type/enum): unchanged from baseline.
- Envelope: exact boundaries verified by execution — 16 keys pass, 17
  fail; 4096 serialized bytes pass, 4097 fail; depth 4 passes, depth 5
  fails (`tests/test_tools_validator.py`).
- `additionalProperties: False`: top-level only, never recursed into
  nested schemas (pinned by characterization test).
- Unknown fields on tools WITHOUT the flag: accepted and forwarded
  (pinned, unchanged from baseline).
- Schemaless tools (`input_schema == {}`): bounds now apply; shape
  checks still vacuous. No registered tool is schemaless today.
- String contents (Unicode/bidi/surrogates/patterns/lengths): never
  inspected by this validator (verified by execution); patterns and
  length caps in schemas are documentation-only here. Secret-shaped
  content is handled by audit redaction (`core/tracing.py`), not here.
- Unknown type names: now a clean error string (was an uncaught
  `KeyError` escaping `conversation._validate_proposal`).
- Every rejection path raises `ValueError` pre-handler (handler-run
  count zero, verified); no crash path found.

## Caller Compatibility

Every caller traced (`engine.submit` ×4 call sites, `registry.call`,
`conversation._validate_proposal`, MCP/browser/OpenCode handlers,
transfer engine, session turns, intent grounding, resolvers, both
agents, all tests): all legitimate inputs are exact-match, flat,
scalar, 0–4 keys, <200 B. No legitimate caller trips the new bounds.
The single real conflict found — `code.apply_patch`'s 100 KB handler
contract vs the 4096 B default — was fixed by the per-tool override,
not by weakening the default. MCP 4–8 KiB restored to its transport
bound the same way. Linux agent dispatch and Dart job transport are
unaffected (they never construct oversized inputs; oversized device
results are capped by their own output limits).

## Security Analysis

The validator is a shape/hygiene filter, NOT the authority boundary.
Authorization lives in `PolicyEngine.decide` + fabric trust/presence/
governor + `submit`'s `allow` gate, none of which read
attacker-influenced fields. Removing the validator would grant zero
unauthorized executions.

What the hardening does contribute (verified by execution):
- Closes envelope smuggling through no-approval SAFE/PUBLIC tools
  (100-key / 5 KB / depth-50 payloads now fail pre-policy).
- `additionalProperties: False` on the high-risk pilot blocks
  `{"target_package": <victim>, "cmd": "rm -rf /"}`-class smuggling
  at Core (device mirror ignores extras independently).
- Non-dict inputs fail closed everywhere instead of crashing or
  pair-list-smuggling through `dict(inputs)`.

What it does NOT do (documented, not defects):
- Authority-shaped keys (`grant_id`, `approved`, `trust`, …) pass on
  non-flag tools — inert, because no authority reads them (verified
  per reader).
- Nested contents, string patterns/lengths, bool-as-int: unchecked.
- `registry.call` performs no policy check by documented design (only
  reachable in-repo post-`submit`-gate); `submit(auth=…)` accepts
  caller-minted decisions (no in-repo abuser; latent, documented).
- `approve()` is bearer-token-grade on execution IDs (holds only
  pre-held records; documented).

## Authority Boundary Analysis

Unchanged by hardening or fix: LLM output → proposal-only
(`extra="forbid"` + strip + `resolve_proposal` field filter);
device identity, grants, approval, revocation all Core-derived;
transfer authorization untouched (separate engine, never calls the
validator); session/memory non-authoritative (re-tested with poisoned
history); Rung-1 allowlist-only (Core enum + device mirror + approval).

## MCP/Browser/OpenCode Compatibility

All schemas 0–2 props, exact-match callers, tiny payloads — safe under
the default envelope. The two pre-existing larger contracts are now
mirrored per-tool (apply_patch 102400, MCP adapters 8192); verified by
dedicated tests (50 KB patch validates; 6 KB MCP validates; 9 KB
fails). No behavior change for any other tool.

## Rung-1 Compatibility

Verified link-by-link: resolver → approval → grant → proxy → validator
→ enum allowlist → device mirror → reflection → verification → audit.
The validator change alters no approval/grant/targeting/revocation/
audit semantics; oversized/malformed inputs now fail Core-side
pre-handler (`FAILED`) instead of consuming a device round-trip
(fail-closed direction). Forged-authority invariance re-verified
byte-identical. End-to-end emulator proof (OS kill lines, pidof,
relaunch) stands unmodified.

## Test Coverage

Pre-existing: U-5/N-789 (grossly-over + unknown-field on CAP),
intent bounds, MCP 9 KB rejection. Added (`tests/test_tools_validator.py`,
11 tests): exact/at-limit boundaries for count/size/depth on the
registry path, nested-`additionalProperties` characterization,
authority-shaped CAP inputs, permissive-side pinning, apply_patch +
MCP restored contracts, unknown-type fail-closed, schemaless bounds,
nested-content characterization. No test weakened or deleted.

## Findings

| Area | Finding | Evidence | Risk | Recommendation |
|------|---------|----------|------|----------------|
| Envelope bounds | Correct, exact, all-paths | boundary tests | None | Keep |
| additionalProperties | Correct and already narrow (single opt-in) | N-789, grep | None | Keep; do not globalize without per-tool audit |
| Nested schemas | Uninspected (documented) | characterization test | Low | Keep as known limitation |
| Legacy compatibility | One real conflict found + fixed (apply_patch/MCP) | failing-then-passing tests | None remaining | Keep overrides tied to handler contracts |
| MCP/browser/OpenCode | Compatible after fix | schemas + tests | None | Keep |
| Transfer/session/intent/resolver | Untouched paths, re-verified green | full suite | None | Keep |
| Privileged Rung-1 | Semantics unchanged, forgery invariance holds | N-10 re-run green | None | Keep |
| Authority fields | Inert by architecture, not by validator | per-reader trace | None (documented) | Keep documenting, don't validator-mission-creep |
| Error handling | Fail-closed everywhere incl. unknown types | tests | None | Keep |
| Oversized/malformed | Rejected pre-handler with exact bounds | tests | None | Keep |
| Security value | DoS-envelope + contract-pinning; not an auth boundary | demos | None | Keep scoped; resist "validator as boundary" claims |
| Regression risk | One shared-code change, fully covered | 507+11 green | None | Keep |

## Decision

**C — NARROW FIX REQUIRED** (applied): per-tool `max_input_bytes`
override + falsy-schema envelope coverage + unknown-type fail-closed.
Default 4096 unchanged for all other tools. No redesign, no
architecture change, no new dependency.

## Remaining Risks

- Validator is shape-only: future handlers reading extra/authority-
  shaped keys would inherit an injection primitive — mitigated by the
  P8 pinning test (any change to permissive behavior fails loudly).
- `submit(auth=…)` caller-minted decisions and bearer-grade `approve()`
  remain latent (no in-repo abuser; documented, out of scope).
- `pattern`/`minLength`/`maxLength` unenforced — enum + device mirror
  carry the Rung-1 boundary instead (documented).
- Nested `additionalProperties`, bool-as-int, non-scalar extras pass
  through — inert today, characterized in tests.

## Recommendation

`core/tools.py` (as fixed) is safe to carry forward as foundation.
Rung 2 may proceed — with the precondition noted in the Rung-1 report:
a registration-time GUI guard is still required before any
UI-automation work, and no new capability may widen the validator's
contract without its own forensic pass.
