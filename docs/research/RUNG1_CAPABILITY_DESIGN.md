# Rung 1 Capability Design — `android.app.force_stop` (DESIGN ONLY, no code)

Status: READ-ONLY design. No repo code changed. This document specifies the
exact descriptor entry for ONE privileged operation so a future implementer
can add it without inventing new gates. Authority stays with the existing
chain: describe → resolver → policy → governor → approval → job → audit.

Candidate: `android.app.force_stop` — force-stop one lab-allowlisted app
package via the priv-app-held `FORCE_STOP_PACKAGES` permission
(`signature|privileged`; DEVICE+DOC proven in
`docs/research/PRIVILEGED_ANDROID_FINAL_REPORT.md` §§A, J: granted when
declared + allowlisted, boot-fatal when the allowlist entry is missing).

## 1. Exact descriptor fields

All bounds cite `core/capabilities.py`; all risk/policy behavior cites
`core/resolver.py` and `core/policy.py`; Dart-side maps cite
`android/lib/capability_descriptors.dart`.

```json
{
  "id": "android.app.force_stop",
  "descriptor_version": "1",
  "name": "android.app.force_stop",
  "version": "1.0.0",
  "description": "Force-stop one allowlisted lab package via ActivityManager. Destructive: kills process immediately. Core policy + approval + device allowlist still apply.",
  "risk": "high_risk",
  "requires": ["android.permission.FORCE_STOP_PACKAGES"],
  "platforms": ["android"],
  "execution": "on-device",
  "requires_foreground": false,
  "requires_network": false,
  "battery_sensitive": false,
  "supports_cancellation": false,
  "supports_streaming": false,
  "timeout_s": 30.0,
  "input_schema": {
    "type": "object",
    "required": ["target_package"],
    "additionalProperties": false,
    "properties": {
      "target_package": {
        "type": "string",
        "minLength": 3,
        "maxLength": 255,
        "pattern": "^[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z][A-Za-z0-9_]*)+$"
      }
    }
  },
  "output_schema": {
    "type": "object",
    "required": ["package", "stopped", "was_running"],
    "additionalProperties": false,
    "properties": {
      "package": { "type": "string" },
      "stopped": { "type": "boolean" },
      "was_running": { "type": "boolean" },
      "verify_state": { "type": "string", "enum": ["stopped", "running", "unknown"] }
    }
  },
  "availability": "available",
  "availability_reason": "",
  "aliases": ["force stop app", "kill app"],
  "examples": [],
  "keywords": ["force-stop", "kill", "stop"]
}
```

Field-by-field conformance:

- `id` / `name`: `android.app.force_stop` matches `_CAP_ID`
  (`core/capabilities.py:37`: `^[a-z][a-z0-9_.:\-]{1,63}$`; 21 chars).
- `descriptor_version`: `"1"` == `DESCRIPTOR_VERSION`
  (`core/capabilities.py:25,153-157`); anything else is rejected.
- `version`: `"1.0.0"` matches `_VERSION` MAJOR.MINOR.PATCH
  (`core/capabilities.py:38,158-160`).
- `description`: 169 chars, under `MAX_DESCRIPTION_LEN = 500`
  (`core/capabilities.py:29`). Plain ASCII (Dart `_ascii` rule in
  `android/lib/capability_descriptors.dart:103-117`); no control chars
  (scan at `core/capabilities.py:100-104`).
- `risk`: see §2.
- `requires`: 1 entry, under `MAX_PERMISSIONS = 20`
  (`core/capabilities.py:30`); `android.permission.FORCE_STOP_PACKAGES`
  matches `_PERM` (`core/capabilities.py:39`). Dart `_requiresOf` map
  (`capability_descriptors.dart:34-38`) gains one entry:
  `'android.app.force_stop': ['FORCE_STOP_PACKAGES']` (short OS name;
  Core descriptor carries the fully-qualified form).
- `platforms`: `["android"]` ⊂ `_PLATFORM`
  (`core/capabilities.py:40`); non-empty (`:169-170`).
- `execution`: `"on-device"` ∈ `_EXECUTION` (`:41`); the call executes in
  the app process via ActivityManager — not `core-mediated` (contrast
  `files.transfer` in `capability_descriptors.dart:78-79`).
- `timeout_s`: `30.0`, within `(0, 600]` (`core/capabilities.py:140`).
- Schemas: both under `MAX_SCHEMA_BYTES = 8192`, depth ≤
  `MAX_SCHEMA_DEPTH = 5` (`core/capabilities.py:34-35,109-116`).
- `aliases` (2 ≤ 10, each ≤ 80), `examples` (0 ≤ 5), `keywords`
  (3 ≤ 15, each ≤ 40), no case-insensitive duplicates, no control chars
  (`core/capabilities.py:178-194`).
- `availability`: `"available"` ∈ `_AVAILABILITY` (`:42`); no reason
  required when available (`:175-177`).
- No `os_permission_granted` key at describe-build time; Core attaches /
  the device reports it only as a real permission state (falsy blocks via
  `usable()`, `:197-199`). No snapshot fields (`advertised_at`, `source`,
  `software_version`) — Core sets those; a device sending them hits
  `extra="forbid"` (§6).

## 2. Risk class: `high_risk` (never `safe`)

`risk: "high_risk"`. Justification:

1. Effect is destructive and immediate: kills the target process,
   discards unsaved in-memory state. Matches the existing high-risk
   trigger class (`core/policy.py:18`: `delete|destroy|drop|format|wipe`)
   in spirit — process destruction, not reversible read.
2. `safe` would mean auto-allow (`policy.py:74-76`: `allow=True`,
   no approval). Force-stop must never auto-allow.
3. `confirm` would allow scoped-grant consumption (`policy.py:64-73`).
   Force-stop must not be grant-consumable or batchable; every invocation
   needs an explicit human yes.
4. `high_risk` buys two structural guarantees for free:
   - Policy: `decide()` returns `allow=False, requires_approval=True`
     (`policy.py:60-63`), reason `high-risk: explicit human approval
     required`. No grant path exists for this rung.
   - Resolver: `FallbackRegistry.register` refuses fallbacks for
     `high_risk` originals (`resolver.py:143-146`: `destructive
     capabilities never fail over`), and `_try_fallback` defensively
     skips them (`resolver.py:553-555`). A failed force-stop never
     silently reroutes to another capability or device.

Dart `_riskOf` (`capability_descriptors.dart:15-31`) gains
`'android.app.force_stop': 'high_risk'` (advisory; Core policy is
authoritative — file header `:9`).

## 3. Input schema — `target_package`

- Type `string`, `minLength: 3`, `maxLength: 255` (Android package-name
  ceiling; also far under the descriptor string cap of 2048,
  `capabilities.py:100-102`).
- Pattern: `^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$`
  - Requires ≥ 2 segments (rejects bare `chrome`, empty, leading digit,
    hyphens, spaces, `..`, trailing dot, path characters).
  - Tighter than the capability-ID regex on purpose: package names are
    not capability IDs.
- `additionalProperties: false`, `required: ["target_package"]` — no
  second parameter exists; anything extra fails Core-side validation.
- Lab-allowlist rule (Core-side, NOT in the schema): the handler checks
  `target_package` against a Core-owned allowlist (e.g. lab test
  packages only; never `android`, never the Zara app itself, never
  system-critical packages such as `com.android.systemui`). The schema
  bounds shape; the allowlist bounds authority. A non-allowlisted value
  passes schema validation and then is denied at execution (§7).
- Normalization: none. No lowercasing, no trimming-fallback: malformed
  input is rejected, not repaired (fail-closed).

## 4. Output schema

```json
{
  "type": "object",
  "required": ["package", "stopped", "was_running"],
  "additionalProperties": false,
  "properties": {
    "package": { "type": "string" },
    "stopped": { "type": "boolean" },
    "was_running": { "type": "boolean" },
    "verify_state": { "type": "string", "enum": ["stopped", "running", "unknown"] }
  }
}
```

- `package`: echo of the validated `target_package`.
- `was_running`: process/table state observed BEFORE the call.
- `stopped`: true only if post-call verification confirms stopped.
- `verify_state`: best-effort read-back (`stopped` / `running` /
  `unknown`); `unknown` on verification failure — never inferred true.
- No PII, no process dumps, no tokens, no log excerpts (audit-safe by
  construction; §8).

## 5. Required permissions (device side)

- Manifest: `android.permission.FORCE_STOP_PACKAGES`
  (`signature|privileged` — taxonomy DEVICE-verified, FINAL REPORT §A).
- Privileged permission allowlist XML entry for the Zara package on the
  owned image; missing entry = `system_server` boot crash loop
  (FINAL REPORT §A.4) — image-build checklist item, not a runtime retry.
- Placement: APK in `/system/priv-app` (DEVICE-proven, FINAL REPORT
  §A.1); SELinux domain `priv_app` expected.
- Runtime check before advertising: `checkSelfPermission ==
  PERMISSION_GRANTED`, else descriptor reports `os_denied` (§6).
- Explicit non-requirements: no `GRANT_RUNTIME_PERMISSIONS`, no
  `INJECT_EVENTS`, no Device-Owner needed for this op (DO buys no
  force-stop — FINAL REPORT §C). No `sharedUserId`, no platform key.

## 6. Availability conditions (honest advertisement)

Built per `androidCapabilityDocs()` pattern
(`capability_descriptors.dart:48-97`); reserved/unsupported entries are
never advertised (`:54`).

| Device state | `availability` | `availability_reason` | `os_permission_granted` |
|---|---|---|---|
| priv-app + allowlisted + `FORCE_STOP_PACKAGES` granted | `available` | `""` | `true` / omitted |
| permission not granted | `os_denied` | `"OS permission not granted"` | `false` |
| API path absent / OEM-removed | `unimplemented` | e.g. `"ActivityManager force-stop unavailable on this build"` | omitted |
| transient failure | `unavailable` | short reason (≤ 200 chars, `capabilities.py:144`) | omitted |

Core `usable()` (`capabilities.py:197-199`) returns false unless
`availability == "available"` AND `os_permission_granted is not False`,
so `os_denied` removes the candidate before ranking
(`resolver.py:302-312`, `_candidate_status` → `unavailable` at
`:331-332`).

## 7. Policy requirements (grant vs approval)

- `PolicyEngine.decide(who, "android.app.force_stop", device_id,
  resource=target_package, risk=high_risk)` → `allow=False,
  requires_approval=True, hard_deny=False`,
  reason `high-risk: explicit human approval required`
  (`core/policy.py:60-63`).
- No grant path: `grant()` / `_live_grant()` (`policy.py:24-46`) apply
  only to `confirm`; a `high_risk` decision never consults or consumes a
  grant. A pre-existing grant for this capability does not authorize
  execution.
- Approval UX (design intent, existing gates): per-invocation human yes
  showing capability id, target package, device, and destructive effect;
  no bulk-approve; approval binds exactly one `(who, capability,
  device_id, target_package)` tuple; TTL minutes, single use.
- `DENY_PATTERNS` (`policy.py:10-17`) still scan the request text first;
  a deny-pattern hit upgrades to `hard_deny=True`.
- Governor (existing): budgets/rate-limits apply unchanged; a
  `governor_blocked` verdict surfaces as `governor_blocked`, never
  escalated (`resolver.py:325-326,462-464`).

## 8. Device authorization preconditions (existing pipeline, no new gates)

Resolution follows `CapabilityResolver.resolve`
(`resolver.py:392-400`): exists → schema → device → auth → trust →
presence → support → availability → constraints → policy → governor →
approval → rank. Per-candidate checks go through
`FabricRegistry.authorize_capability`; selection through
`route_capability` (pinned-device no-failover rule —
`resolver.py:431-446`).

For `android.app.force_stop` to reach `resolved` / `requires_approval`:

1. Descriptor advertised and `usable()` true (§6).
2. Device trust ∈ (`trusted`, `temporarily_trusted`); anything else
   (notably `revoked`) → `unauthorized` (`resolver.py:305,322-324`).
3. Presence `online` (or `degraded` at operator risk); `offline` →
   `offline` verdict (`resolver.py:329-330`).
4. `authorize_capability(name, device_id, who)` returns
   `authorized=True`; for high-risk this always carries
   `approval_required=True`, so the terminal state is
   `requires_approval`, never bare `resolved` without a human yes
   (`resolver.py:317-319,453-459`).
5. Pinned `device_id` is honored exactly: failure on the pinned device
   never substitutes another device for high-risk work (route rule,
   `resolver.py:431-446`; fallback forbidden, §2).
6. Snapshot freshness: `stale=true` still routes but the reason string
   must carry `(snapshot stale — device should re-describe)`
   (`resolver.py:449-453`); execution should re-describe first.

## 9. Audit event shape (no secrets)

Every attempt — success, denial, and validation failure — emits one
event on the existing audit path. Shape (new event name, existing
envelope conventions):

```json
{
  "event": "capability.execute",
  "capability": "android.app.force_stop",
  "device_id": "<opaque device id>",
  "who": "<caller principal>",
  "target_package": "<validated package name>",
  "decision": "approved|denied|rejected",
  "reason": "<policy/resolver reason, <= 300 chars>",
  "was_running": true,
  "stopped": true,
  "approval_id": "<approval id, if any>",
  "snapshot_stale": false,
  "ts": "<utc iso8601>"
}
```

- Contains: who/what/where/decision/reason/outcome linkage.
- Never contains: secrets, tokens, `grant_id` values, package contents,
  process dumps, log text, user data. (`grant_id` is Core-internal;
  approval id is a reference, not a credential.)
- Validation rejections (`malformed package`, `duplicate`, schema fail)
  audit with `decision: "rejected"` and the truncated validator message;
  the raw document is never stored (`CapabilityRejected … never stored`,
  `capabilities.py:66,218-243`).

## 10. Verification criteria (running → stopped → relaunchable)

Lab procedure on the disposable emulator (never the Vivo daily driver —
FINAL REPORT §K/L):

1. **Running**: launch allowlisted target; confirm running via
   `dumpsys activity processes` / `pidof <package>` (pid observed).
2. **Stop**: invoke `android.app.force_stop` with approval through the
   full chain (§8); expect `stopped: true`, `verify_state: "stopped"`.
3. **Stopped**: `pidof <package>` empty AND package absent from
   `dumpsys activity processes`; device remains booted, Zara app alive
   (no collateral kill).
4. **Relaunchable**: cold-start the target via launcher/monkey;
   expect process present again (`verify_state: "running"` on re-check),
   no reboot, no re-install, no data-wipe claim beyond normal
   force-stop semantics.
5. **Negative**: non-allowlisted target denied (§7 row 3); stopped
   package re-invoked reports `was_running: false, stopped: true`
   (idempotent success); malformed input rejected before dispatch.
6. Pass bar: steps 1–4 green on ZaraLab AVD + §11 edge-case matrix all
   resolving to the specified states; full Python suite still green;
   zero `android/` changes beyond the future single-capability adapter.

## 11. Edge-case matrix (specified resolution states)

| Input / state | Where caught | Resolution / outcome |
|---|---|---|
| Unknown package (well-formed, allowlisted-pattern, not installed) | Handler pre-check (`PackageManager` lookup) | Success-shaped no-op: `was_running: false, stopped: true, verify_state: "stopped"`; audited. Alternative admissible: `unavailable` with reason `package not installed` — implementer picks ONE and pins it in a test. Never an exception to the caller. |
| Malformed package name (`""`, `..foo`, `a b`, trailing dot, > 255 chars, control chars) | Schema validation → `CapabilityRejected` (`capabilities.py:109-116,150-195`); control chars at scan (`:100-104`) | Rejected before dispatch; `resolve` maps bad capability/device ids to `no_capability`/`no_device` (`resolver.py:402-411`); proposal path → safe failure (`:591-621`). Audit `decision: "rejected"`. |
| Non-allowlisted target (well-formed, e.g. `com.android.systemui`, Zara itself) | Core-owned allowlist in handler, AFTER schema passes | `policy_blocked` (`resolver.py:327-328,462-464`); `decide()` denies; no dispatch to `ActivityManager`. Audit `decision: "denied"`. |
| Revoked device (`trust == revoked`) | `authorize_capability` → candidate `trusted=False` (`resolver.py:305`); `_candidate_status` → `unauthorized` (`:322-324`) | `status: "unauthorized"`, `device_id: None` on failure (`:461-476`); nothing executes. Re-trust is out of scope for this capability. |
| Stale snapshot (`rec.stale == true`) | Resolver annotation (`resolver.py:449-453`) | Still routes, but verdict reason appends `(snapshot stale — device should re-describe)`; operator procedure: re-describe before approving force-stop; approval UI surfaces the staleness flag. |

## 12. Why the LLM cannot inject authority (existing defenses, cited)

Three independent layers, all already in the repo — this design adds
zero new parsing of LLM text:

1. **Forbidden-key scan rejects authority claims at ingest**
   (`core/capabilities.py:44-106`): `FORBIDDEN_KEYS` (`:56-62`)
   includes `authorized, allow, allowed, grant, grant_id, policy, trust,
   trusted, exec, execute, execution_command, command, shell, eval,
   system, subprocess, popen, secret, password, passwd, token, api_key,
   apikey, private_key, credential, __proto__, constructor, prototype`.
   `_scan_tree` (`:69-106`) walks the whole descriptor (depth cap
   `MAX_SCHEMA_DEPTH + 2`, key-length 128, string-length 2048, control-
   char and type checks) and raises `CapabilityRejected` on any hit
   outside a schema `properties` mapping (`:84-87`). The exemption is
   exactly one level (`:90-94`, `properties` immediate keys document
   field names — e.g. transfer's legitimate `grant_id` input) and values
   are still scanned everywhere; schema content is never executed
   (`:49-55`). `validate_descriptor_doc` (`:202-215`) scans BEFORE
   pydantic parsing and caps the raw doc at `MAX_DOC_BYTES = 32768`.
2. **Proposal field filter ignores security claims**
   (`core/resolver.py:587-621`): `PROPOSAL_SECURITY_FIELDS` (`:587-589`)
   lists `authorized, trust, trusted, governor, policy,
   approval_granted, grant_id, allow, allowed` — and `resolve_proposal`
   (`:592-621`) deliberately NEVER reads them (`:619`). It reads only
   `preferred_capability`/`capability`, `preferred_device`/`device_id`
   (must be strings), and the advisory `constraints` subset
   (`platform`, `min_version`, `risk_max`, `online_only`). Everything is
   re-derived: ranking, trust, presence, policy, governor, and approval
   come from Core state, never from proposal text. Malformed proposals
   resolve to safe failures (`no_capability`), not to execution.
3. **Model shape lock (`extra="forbid"`) drops smuggled fields**
   (`core/capabilities.py:119-124`): `CapabilityDescriptor` sets
   `extra="forbid"` (class kwarg AND `model_config`), so Core-attached
   snapshot fields (`advertised_at`, `source`, `software_version` —
   docstring `:120-122`) or any LLM/device-invented authority field
   fails validation (`validate_descriptor_doc` wraps shape errors as
   `CapabilityRejected`, `:211-214`). Batch path (`:218-243`) further
   rejects dup IDs, oversize batches, and non-dict items; rejected
   records are summarized (120/200-char truncation), never stored.

Net: an LLM proposing `{capability: "android.app.force_stop",
"authorized": true, "grant_id": "x", "trust": "trusted",
"target_package": "com.android.systemui"}` gets: authority keys ignored
(layer 2), descriptor forgery rejected (layers 1+3), package denied by
the Core allowlist (§11 row 3), and execution still gated on human
approval (§7) — four misses before anything moves.

## 13. Future implementation checklist (not done here)

1. Dart: add `_riskOf` + `_requiresOf` entries, `target_package`-aware
   `input_schema` (bounded pattern), honest availability from
   `checkSelfPermission`; reserved entry in `capabilities.dart` with
   `supported=true` only when implemented.
2. Core: handler allowlist (lab packages), `PackageManager` existence
   check, `ActivityManager.forceStopPackage` call, post-call
   verification, audit event per §9.
3. Tests: descriptor-conformance (mirrors transfer-consistency test),
   schema accept/reject matrix, allowlist deny test, LLM-authority-
   injection test (layers §12), stale/revoked/offline verdict tests,
   emulator running→stopped→relaunchable (§10).
4. Image: manifest + allowlist XML + `/system/priv-app` placement on the
   owned lab image; boot-verify; never the Vivo.
