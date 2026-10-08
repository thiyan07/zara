# Zara Capability Discovery (Stage 16)

Status: IMPLEMENTED · TEST_VERIFIED (34 Python + 7 Dart) · PHYSICAL_VERIFIED
(laptop agents + Vivo V2338, 2026-10-05 — see Physical validation below)

## Physical validation (2026-10-05, real hardware)

- Linux agents `laptop-1`/`laptop-2` (real processes, new code): register →
  describe 7/7 accepted → `resolve` authorizes `system.battery` /
  `files.transfer` (approval-flagged) / rejects hallucinated `system.shell`
  with zero candidates. Revoke → candidates empty + snapshot wiped;
  fresh re-pair → publishes again. Core restart → 7-cap snapshot persists.
  Reconnect-without-describe → `stale=true`; describe → `false`.
- Vivo V2338 + release APK (new code, `ZARA_CORE_URL` + `adb reverse`):
  old key correctly 403s → re-paired `vivo-real` trusted/online →
  `describe ok accepted=[15 caps] rejected=[]` (logcat) → 15 descriptors
  visible via API → `resolve voice.input` authorizes (approval-required).
- Permission-dependent availability on hardware: `pm revoke RECORD_AUDIO`
  + relaunch → `voice.input` = `os_denied`, `usable=false`, reason set;
  `pm grant` + relaunch → `available`, resolve authorizes again.
- Transfer regression: real 64 KiB `laptop-1`→`laptop-2` through live
  grant, byte-identical, SHA-256 verified both sides (INTEGRATION label:
  two local agents). Phone→laptop send on the new path was NOT re-driven
  (grant-field tap produced no transfer; phone removed afterwards) —
  Stage 15 phone↔laptop byte evidence stands; engine untouched by Stage 16.
- Transport lesson (found + fixed physically): Dart `HttpClientRequest`
  is latin-1 — the `→` in voice notes killed describe client-side.
  Wire text is now ASCII-sanitized (`capability_descriptors.dart` +
  Dart test); in-app notes untouched.

## Capability vs authorization vs availability

Three facts Core never confuses:

| Question | Answered by | Example |
|---|---|---|
| What CAN this device do? | Capability descriptor (device advertises, Core validates) | `system.battery` |
| MAY it do it for this request? | Policy + Governor + trust + grants (Core only) | confirm-risk needs approval |
| CAN it do it RIGHT NOW? | Availability + presence + staleness | `os_denied: permission revoked` |

A descriptor NEVER grants permission. The LLM NEVER sees descriptors as
permission. Execution runs only through `authorize_capability` →
`route_capability` → `execute_on_device`.

## Descriptor (version `1`)

Required: `id`, `name` (both `^[a-z][a-z0-9_.:\-]{1,63}$`), `version`
(MAJOR.MINOR.PATCH), `risk` (existing `RiskLevel`: safe/confirm/high_risk
— no competing risk system). Optional with defaults: `description`
(≤500), `requires` (OS permissions), `platforms` (any/linux/android),
`execution` (on-device/core-mediated), `requires_foreground`,
`requires_network`, `battery_sensitive`, `supports_cancellation`,
`supports_streaming`, `timeout_s` (≤600), `input_schema`/`output_schema`
(≤8 KiB, depth ≤5), `availability` + `availability_reason`,
`os_permission_granted`, `aliases`/`examples`/`keywords` (bounded).

Unavailable capabilities MUST state a reason (`permission_required`,
`hardware missing`, …). Static support vs live state stays explicit:
`supported=true, available=false` is representable and routable-to-deny.

## Untrusted input rules

Whole-document scan before parsing: ≤32 KiB/doc, ≤100 docs/batch,
duplicate IDs rejected, unknown `descriptor_version` rejected,
path/command-like IDs rejected by the ID pattern, control characters
rejected, schema bombs (depth/width) rejected. Top-level authority
fields (`grant_id`, `trust`, `policy`, `exec`, `shell`, `secret`,
`token`, …) reject the document — except inside a JSON Schema
`properties` mapping, where keys *document* field names (transfer
documents a `grant_id` input; the allowlisted shell tool documents a
`command` input). Schemas are never executed: no `eval`, no subprocess,
no `Runtime.exec` in `core/capabilities.py` (test-enforced).

Identity comes ONLY from device auth: documents carrying `device_id`
are rejected outright (spoof-proof structurally, not by overwrite).

## Lifecycle

1. Device registers (`/v1/agent/register`) → old snapshot marked `stale`
   (routing still works; `resolve` reports staleness — no re-pair needed).
2. Device describes (`/v1/agent/capabilities/describe`) → Core validates,
   diffs (`capability_added/removed/changed` audited), replaces snapshot.
3. Permission/hardware change → device re-describes (Android does this on
   the mic/notification grant buttons; Linux on next boot).
4. Revoke → snapshot wiped (never executable); tombstone + audit remain.
   Fresh pair alone does NOT re-trust: explicit `clear_revocation` needed.
5. Restart → snapshots persist in fabric sqlite (`record_json`).

## API (operator auth unless noted)

- `POST /v1/agent/capabilities/describe` (device auth) — the only write.
- `GET /v1/fabric/capabilities` — fleet index (capability → devices with
  trust/presence/availability/staleness).
- `GET /v1/fabric/devices/{id}/capabilities` — one device snapshot.
- `GET /v1/fabric/devices/{id}/capabilities/{cap}` — single descriptor
  (404 when not advertised — never invented).
- `POST /v1/fabric/resolve` — dry-run: candidates with per-device
  trust/presence/availability/policy/governor checks. No execution, no
  grant. Deterministic ordering (authorized → available → device_id).

## Advertisers (single source of truth — no drift)

- Linux: `linux_descriptors()` built from `LINUX_TOOLS` allowlist +
  canonical `files.transfer` descriptor. Consistency test fails the build
  if tools ≠ advertised.
- Android: `androidCapabilityDocs()` built from `supported=true` entries
  only (`capabilities.dart`); reserved entries (camera/screen/location/
  accessibility) never get docs.

## Transfer integration

`transfer_descriptor()` documents the EXISTING Stage 15 interface
(limits pinned to `core/transfer.py` constants by test). Grants, policy,
governor, SHA-256 verification unchanged. `execution: core-mediated`.

## Deferred (Stage 17+)

Deterministic resolver ranking beyond current sorted-candidates,
declared fallback relationships (`fallback_for`), escalation ladder
representation, natural-language → capability matching. No new executable
surface until then.

---

# Stage 17 — Deterministic Resolver + Safe Escalation Ladder

Status: IMPLEMENTED · TEST_VERIFIED (34 Python, suite 378) ·
PHYSICAL_VERIFIED (Linux agents + Vivo V2338, incl. phone→laptop bytes)

## Resolver (`core/resolver.py`, no authority of its own)

`CapabilityResolver(fabric)` orchestrates: per-candidate verdicts via
`authorize_capability` (the strict pipeline: exists → trust → presence →
availability → policy → governor → approval), device choice via
`route_capability` (pinned no-failover rule), execution stays on
`execute_on_device`. Same state → same result, always; works with no LLM.

Strict order per resolution: capability shape → device known →
candidates collected (one authorize each) → constraints → rank →
pinned-substitution rule → top verdict → explicit fallback chain
(max 3) → terminal status. Later stages never bypass earlier ones.

Terminal statuses: `resolved`, `requires_approval`, `unavailable`,
`unauthorized`, `no_capability`, `no_device`, `offline`,
`governor_blocked`, `policy_blocked`, `requires_escalation`.
`device_id` is set ONLY on `resolved`/`requires_approval`/substituted —
failures carry candidates + reasons, never an executable target.
Stale snapshots still route (Stage 16) but the verdict brands them
(`(snapshot stale — …)` in reason + `stale` flag).

Ranking (total order, documented in code): authorized → usable →
pinned match → preferred-device match → trust rank → presence rank →
device-kind order → battery desc → capability version desc →
`device_id` asc (final tiebreak; missing data sorts last, never invented).

## API (extends Stage 16 `resolve`, shape-compatible)

- `POST /v1/fabric/resolve` now `{capability, device_id?, who?,
  preferred_device?, constraints?, allow_fallback?}` →
  `{capability, status, device_id?, candidates[] (each + status/reason/
  authorization), fallback_used?, required_escalation?, substituted,
  reason}`. `constraints`: `platform`, `min_version`, `risk_max`,
  `online_only` (explicit opt-in; default INCLUDES offline devices so
  authorize reports OFFLINE honestly).
- `POST /v1/fabric/resolve/proposal` — untrusted LLM proposals. Only
  capability/device/constraint/preference fields are read; `authorized`,
  `trust`, `governor`, `policy`, `grant_id`, … are IGNORED structurally.
- `GET/POST /v1/fabric/relationships` — explicit Core fallback metadata
  (operator-only write, audited as `capability_relationship`).
- `GET /v1/fabric/ladder` — rung per known capability + non-executability
  note for GUI/HUMAN.

## Fallback discipline

Registration validates: known references, known relation type, no
self-reference, no cycles, ≤5/capability, risk never exceeds original,
HIGH_RISK originals get NO fallback. Fallbacks are planning hints: each
still resolves through the full pipeline. Shipped registry starts EMPTY
(no invented equivalences like transfer⇔photo); tests register
scenario fallbacks programmatically. Destructive pinned-offline requests
never substitute (`substituted=false`, terminal failure).

## Ladder

NATIVE (descriptors) → TOOL (registry) → MCP (`mcp.*`, Core-authorized) →
BROWSER (`browser.*`, `code.*`, Core-authorized) → GUI (future-only,
represented, never executable) → HUMAN (terminal planning output).
`level_of()` derives rungs; unknown names sit at HUMAN.

## Preferences

`preferred_device` is advisory ranking input only. Tests prove it breaks
ties but never overrides trust/revocation/policy/governor/availability.

## Physical validation (live Core 2026-10-05, phone absent)

- `laptop-1` re-paired: `resolve system.battery` → `resolved`; proposal
  with forged `authorized/trust/governor/grant_id` → identical verdict
  (fakes ignored); hallucinated `system.shell` → `no_capability`, rung 5.
- Fallback `system.battery→system.network` registered; cycle + unknown
  ref → 400 with exact reasons; ladder live: NATIVE 7, TOOL 6,
  BROWSER 13, MCP/GUI/HUMAN 0 + note.
- Phone→laptop transfer through new path: VERIFIED 2026-10-05
  (Vivo V2338 → laptop-1, live operator grant `tmp-7c77625dbf91`):
  107 B text + 64 KiB + 1 MiB, all Core-SHA-256-verified, all downloaded
  by the laptop agent byte-identical with verified acks. The Stage 16 gap
  is CLOSED. Known edge (logged, not changed): agent `xfer_download`
  with unknown size requests a full 64 KiB first window, which Core
  rejects for sub-window files — pass `size_bytes` from pending info
  (documented usage); a retry-clamp fix is deferred follow-up work.

---

# Relation to Stage 18 (intent parser)

`docs/INTENT_PARSER.md` is the parser contract. The resolver consumes
`IntentProposal` via `resolve_proposal` exactly like any other untrusted
proposal: capability/device/constraint/preference fields only, everything
else ignored. Parser builtins (`device.list`, `assistant.help`) never
reach the resolver. No resolver changes were needed for Stage 18.
