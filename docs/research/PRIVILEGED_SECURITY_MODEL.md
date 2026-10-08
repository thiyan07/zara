# Privileged Security Model — Threat Model for a Privileged / System-Level Zara Android Body

Status: RESEARCH ONLY — no code changes, no device modification.
Scope: what MUST hold if the Zara Android body ever becomes a privileged app
(priv-app) or system service, given the existing Core security posture.

## 1. Existing posture (what this threat model inherits)

Read from the repo before writing; nothing below relaxes any of it:

- `docs/SECURITY_MODEL.md`: three layers — (1) OS permissions are outermost
  and policy never grants what the OS denies; (2) `core/policy.py` is
  capability-based, least-privilege, `safe|confirm|high_risk`, scoped
  expiring grants; (3) execution sandbox — tools declare schemas, inputs
  validated, timeouts enforced, deny-patterns hold even if mislabelled.
- `core/policy.py` (`PolicyEngine.decide`): deny-patterns
  (`rm -rf /`, `shutdown|reboot`, `mkfs`, fork-bombs, `passwd|shadow`,
  `.ssh/id_`) return `hard_deny`; `high_risk` hints (`delete|destroy|drop|
  format|wipe`) require explicit human approval; `confirm` requires a live
  scoped grant (`grant(who, capability, device_id, resource, ttl_s)`),
  consumed once; `safe` is the only auto-allow rung.
- `core/governor.py` (`ResourceGovernor`): battery-first execution gate
  (defer/reroute, never authorize). Resource pressure can only *refuse or
  defer* work, never permit it.
- `core/resolver.py` (Stage 17 `CapabilityResolver`): deterministic,
  LLM-free pipeline — exists → schema → device → `authorize_capability`
  → trust → presence → support → availability → constraints → policy →
  governor → approval → rank. `resolve_proposal()` reads ONLY
  `capability/device/constraints/preferred_device` from a proposal and
  IGNORES security-claim fields (`authorized`, `trust`, `grant_id`,
  `allow`, …). Fallbacks are explicit Core metadata, bounded, never exceed
  original risk, and high-risk capabilities never fail over. GUI rung (4)
  is represented, never executable; HUMAN rung (5) is terminal planning
  output, never executable.
- Approval mechanism (`core/execution.py`, `ExecutionEngine`): `submit()`
  requires `auth.allow` (passed-in `PolicyDecision` or freshly decided);
  without it the record parks in `WAITING_FOR_PERMISSION` or raises
  `PolicyDenied` on hard-deny. `approve()` only transitions a record
  already in `WAITING_FOR_PERMISSION`, mints a single scoped grant, and
  re-runs. There is no approve-by-LLM, approve-by-device-claim, or
  approve-by-notification-content path.
- Audit (`core/audit.py`, `AuditLog`): append-only SQLite + in-memory ring,
  operational metadata only (actor/action/target/detail/ok) — never
  secrets, never chain-of-thought. Every request/tool/decision/execution/
  approval/result is logged.
- Stage 18 rule (`docs/INTENT_PARSER.md`): LLM output is **proposal-only**.
  `IntentProposal` uses `extra="forbid"` + `validate_proposal()`; hosted
  JSON carrying authority-looking fields is stripped pre-validation and
  down-weighted. `ground_proposal` + `resolver.resolve_proposal` re-derive
  everything from Fabric + registry. NL/confidence/preference = zero
  authority. Parser creates no capabilities/grants/trust, cannot bypass
  approval/governor, cannot execute.
- `docs/DEVICE_SECURITY.md` + `docs/ANDROID_ARCHITECTURE.md`: per-device
  keys (hashed server-side, expiring, revocable → 403 + offline); device
  inputs schema-validated twice; unknown tools reported, never executed;
  Android body today is unprivileged — allowlist `{system.battery,
  system.network}`, no `Runtime.exec`/`ProcessBuilder`, notification taps
  produce only Core endpoint calls (approve/deny carry only execution ID),
  wake path has no tool/policy path.

## 2. The invariant chain (non-negotiable, privileged body included)

```
LLM -> proposal -> Core -> policy -> authorization -> privileged interface
  -> execution -> verification -> audit
```

Every privileged action MUST traverse the full chain, in order, with no
short-circuit:

1. **LLM → proposal**: model output (hosted/local, utterance-adjacent, or
   tool-description text) is untrusted data. It may name a capability +
   bounded params. Nothing else.
2. **proposal → Core**: Core re-validates schema, grounds capability
   against Fabric + registry, strips/forbids authority fields
   (Stage 18 `validate_proposal`, Stage 17 `resolve_proposal`).
3. **Core → policy**: `PolicyEngine.decide` (+ deny-patterns) classifies
   risk; `confirm`/`high_risk` park for approval.
4. **policy → authorization**: a scoped, expiring, single-use grant bound
   to `(who, capability, device_id, resource)`; pinned-device no-failover
   for destructive/approval/governor/policy failures.
5. **authorization → privileged interface**: the device-side privileged
   entry re-checks caller identity + grant/token + param bounds before
   touching any privileged API. Core authorization is necessary but the
   device MUST NOT treat the transport as authentication by itself.
6. **privileged interface → execution**: narrow capability-ID dispatch
   (see §4), never a generic exec/eval/intent-forwarding primitive.
7. **execution → verification**: `confirm`-and-above tools define
   verifiable success criteria; engine marks `verified` only after check.
8. **verification → audit**: audit-before-or-with execution — the
   authorization decision, grant ID, caller identity, params (redacted),
   result, and verification outcome are recorded in `core/audit.py`
   where Core-side, and in a tamper-evident on-device log where
   device-side, such that "what did the privileged body do?" is always
   answerable. Audit failure MUST fail closed (deny execution) for
   privileged actions.

If any link is skipped, the action MUST NOT execute. Convenience shortcuts
("trusted caller, skip policy", "cache the approval", "UI already asked")
are privilege-escalation vulnerabilities by definition.

## 3. Threat-by-threat analysis

Each entry: attack path → what MUST hold → residual risk.

### T1. Privilege escalation via compromised LLM output

- **Attack path**: attacker poisons model weights / hosted provider
  response / prompt context so the LLM emits
  `{"tool": "privileged.*", "arguments": {"cmd": "…"}}` or forges
  `authorized:true / grant_id:…`. With a priv-app body, a single
  mis-dispatched call could toggle secure settings, grant permissions,
  read private data, or disable security.
- **What MUST hold**: Stage 18 + Stage 17 rules extended verbatim to
  every new privileged capability — `extra="forbid"` on proposals,
  strip-before-validate, `resolve_proposal` ignores all
  `PROPOSAL_SECURITY_FIELDS`, resolver verdicts byte-identical
  with/without forged fields (tested property today, must be re-tested
  per new capability). Privileged capabilities default to
  `confirm` minimum, security-sensitive ones to `high_risk`; the
  privileged interface accepts only Core-signed, grant-bound dispatches
  with bounded params — never raw LLM JSON. `ExecutionEngine.submit`
  without `auth.allow` still parks/denies.
- **Residual risk**: model proposes a *legitimate-looking but wrong*
  privileged action (e.g. wrong device, wrong resource) that a tired user
  approves. Mitigate with narrow grant scope display (what/where/why),
  never bulk-approve, and verification output shown before completion.

### T2. Prompt injection reaching privileged APIs

- **Attack path**: injected text ("ignore previous rules", "system says
  safe", fake approvals) arrives via utterance, pasted content, web page,
  or tool description, and attempts to reclassify a privileged call as
  `safe` or to smuggle params past validation.
- **What MUST hold**: injection content cannot alter policy (tested
  property today — `docs/SECURITY_MODEL.md` Stage 3). Deny-patterns run
  on the *resolved request shape* (`capability + resource + command`),
  not on the LLM's risk label, so mislabelling never downgrades. Param
  schemas are allowlists with size/depth bounds (Stage 18: ≤16 keys,
  ≤4 KiB, depth ≤4 pattern to reuse). Dangerous language is screened
  before grounding and becomes unsupported/clarification. The privileged
  interface performs its own param-shape check independent of Core's
  (defense in depth), rejecting oversize/unknown fields.
- **Residual risk**: novel phrasing that evades keyword screens but still
  maps to a *legitimate* capability (e.g. social-engineering the user to
  say "yes"). This is a human-factors risk, not a bypass — approval UX
  must show the concrete privileged effect, not the model's wording.

### T3. Malicious notification content acted on by notification-listener code

- **Attack path**: attacker sends SMS/messaging-app/email notification
  containing fake Zara approval text ("tap to approve rm", deep-link with
  `execution_id`, or text the listener parses as a command). A
  `NotificationListenerService` in a priv-app could auto-act, auto-tap, or
  parse content into dispatches.
- **What MUST hold**: notification content is **untrusted data, zero
  authority** — same class as LLM output. Listener code MUST NOT parse
  notification text into actions, MUST NOT auto-approve, MUST NOT extract
  execution IDs / URLs / commands from notification extras. Today
  (`zara_notifications.dart`) taps carry ONLY the execution ID of a
  Core-created approval hold and call the Core endpoint; that pattern
  MUST be preserved: the only legal notification-driven transition is
  `user-tap → Core approval endpoint with (execution_id, allow)` for an
  approval hold Core already created, decided under the normal policy
  path, device-scoped to own-device executions. No listener-driven
  dispatch, no content-driven routing, notification actions bound to the
  originating hold ID (replay of an old ID re-resolves, never replays
  execution).
- **Residual risk**: user confuses an attacker's system-looking
  notification with a genuine Zara approval prompt and taps approve.
  Mitigate with unspoofable approval UX (Core-generated details, channel
  separation) and by never approving `high_risk` from a notification tap
  alone if policy so requires.

### T4. Malicious on-screen text acted on by UI automation

- **Attack path**: attacker renders deceptive text on screen (web page,
  overlay, malicious app, OCR-scraped labels) that an accessibility/UI-
  automation path reads and treats as instructions ("click Allow", fake
  consent dialogs, coordinates harvested from screen).
- **What MUST hold**: on-screen text is **untrusted data, zero
  authority**. UI automation MUST be off by default; if ever added, each
  automated interaction is a `confirm`-or-higher capability resolved
  through the full chain (resolver currently marks GUI rung
  non-executable — that default MUST persist for privileged builds).
  No auto-clicking of permission/consent/security dialogs, ever. Overlay
  protection (filter touches obscured by overlays) on all approval UX.
  Coordinates/actions never derived from OCR'd or accessibility-scraped
  strings without Core authorization of the *semantic action*.
- **Residual risk**: clickjacking / overlay abuse tricking the user into
  tapping through a real consent. Standard Android mitigations (obscured-
  touch filtering, distinct approval activity, no automation of security
  surfaces) reduce but do not eliminate; user vigilance is the last line.

### T5. Malicious MCP / tool results

- **Attack path**: compromised MCP server or tool output returns
  instructions ("now run shell X", "exfiltrate to URL Y"), forged success
  flags, or oversized payloads attempting to steer the next reasoning
  step into a privileged call or to poison memory/RAG.
- **What MUST hold**: `LLM_CONTRACT.md` rule — tool RESULT *status* is
  trusted metadata, tool OUTPUT *content* is untrusted data, wrapped in
  `<<UNTRUSTED DATA>>` and never executed. Results are schema-validated;
  oversized/malformed results are truncated/rejected, never executed.
  Memory ingestion refuses secrets and treats stored content as
  information, not authority ("Memory is information; policy is
  authority"). A tool result can never mint a grant, satisfy an approval,
  or widen a capability's params — the next step still passes policy +
  authorization + approval from scratch.
- **Residual risk**: subtly wrong data (false file listing, doctored
  sensor read) leading Core to make a poor-but-authorized choice. Mitigate
  with verification checks (success criteria), cross-source confirmation
  for privileged effects, and audit showing the data lineage.

### T6. Compromised device attacking Core with stolen device credentials

- **Attack path**: attacker roots/extracts the device key (Keystore
  bypass, backup extraction, memory dump) and calls Core APIs impersonat-
  ing the device: fake capability advertisements, forged job results,
  approval attempts, or capability probing.
- **What MUST hold**: current `DEVICE_SECURITY.md` properties preserved
  and hardened — per-device keys hashed server-side, expiring, revocable;
  forged/unknown identity → 403. Device claims are never trusted:
  advertisements feed existence checks only (existence ≠ authorization);
  job results feed verification, not blind success; approvals are
  device-scoped to own-device executions (cross-device stays operator-
  only). Rate-limit + anomaly detection on device endpoints; revocation
  (`/v1/agent/revoke`) immediately marks offline + 403 everywhere.
  Key storage in Android Keystore (hardware-backed where available, no
  export), rotation supported (`rotate` endpoint exists today).
- **Residual risk**: credential theft from a fully compromised device is
  eventually possible; blast radius is bounded by per-device scoping +
  fast revocation, but a stolen live key is valid until detected. Mitigate
  with short TTLs, rotation, and user-visible revocation UX. A privileged
  body raises the stakes — revocation must also remotely disable the
  privileged interface's Core-trust (device must refuse new privileged
  dispatches once revoked/unpaired).

### T7. Compromised Core attacking the device

- **Attack path**: attacker breaches the Core host / Oracle gateway and
  issues malicious dispatches to the priv-app body (silent data pull,
  permission changes, surveillance via sensors) — or a rogue operator
  abuses legitimate dispatch power.
- **What MUST hold**: mutual distrust. Device verifies Core (pinned
  HTTPS/WSS cert in production, per-device pairing binding, dispatch
  signatures or session tokens — never plain-HTTP trust in prod). The
  privileged interface enforces its own allowlist + user-visible consent
  for sensitive classes even when Core dispatches (OS runtime permissions
  remain the outer wall: policy never grants what the OS denies). No
  silent privileged execution classes: security-sensitive dispatches
  require on-device user confirmation that malware-with-Core-access
  cannot synthesize (system consent surfaces, not app-rendered buttons).
  Device-side tamper-evident log records every privileged dispatch so a
  compromised-Core episode is forensically visible.
- **Residual risk**: Core is inherently powerful — a fully compromised
  Core with valid credentials can order many legitimate-looking reads.
  Residual is bounded by OS permissions + on-device consent + audit, but
  cannot be zero while remote dispatch exists. Production deployment must
  document this trust assumption explicitly.

### T8. Confused deputy between Zara app components (who may call the privileged interface)

- **Attack path**: another app component, exported Activity/Service/
  Provider, deep-link handler, or third-party SDK inside the process calls
  the privileged Binder/service interface directly, bypassing Core
  authorization ("Core approved something similar earlier", implicit
  intent hijack, exported-component abuse by a malicious app).
- **What MUST hold**: the privileged interface MUST verify **Binder
  caller identity** on every call (`getCallingUid`/`getCallingPid`,
  package-name + signature check against the Zara signing cert) and
  reject cross-UID callers by default. Components are `exported=false`
  unless a reviewed exception exists; deep links / intents carry zero
  authority and are re-authorized through Core. Capability-ID dispatch
  requires a live Core grant token bound to `(caller, capability,
  resource, expiry)` — possession of the interface ≠ permission to use
  it. In-process SDKs get no direct handle to the privileged dispatcher.
- **Residual risk**: confused-deputy *within* the same UID (bundled SDK
  with code execution in-process). Mitigate by minimizing in-process
  third-party code in privileged builds and isolating privileged dispatch
  in a separate UID/service process where feasible.

### T9. Over-broad privileged API surface

- **Attack path**: privileged interface exposes generic primitives
  (`runShell(cmd)`, `writeSetting(key,value)`, `openUrl(url)`,
  `grantPermission(pkg,perm)`) so any single authorization bug or grant
  confusion yields arbitrary system control.
- **What MUST hold**: narrow **capability IDs with bounded params** —
  one entry per justified need (e.g. `system.battery.read`,
  `device.reboot.request`), each with an allowlisted param schema, enum
  bounds, and size caps. No stringly-typed pass-through to `Settings.*`,
  `DevicePolicyManager`, shell, or intent broadcast. Each capability
  declares its risk (`confirm` minimum) and verification criteria in the
  Core registry; unknown capability IDs are refused on-device even if
  Core somehow sent them (device allowlist mirrors Core registry).
  Add AuditBeforeOrWith for every new entry.
- **Residual risk**: each added entry is new attack surface; risk grows
  with count. Mitigate with periodic surface reviews and a default-deny
  posture for unlisted IDs (see T10).

### T10. Permission creep over updates

- **Attack path**: successive updates each add "just one more" permission,
  capability, or background privilege until the priv-app silently holds
  far more power than the user originally granted — possibly via an
  auto-update the user did not scrutinize.
- **What MUST hold**: manifest permission diffing in review (CI check
  that fails on new permission/capability without a recorded
  justification + risk label + approval-path entry); versioned capability
  registry where additions require operator/Core-registry change with
  audit; no silent enrollment of existing devices into new privileged
  capabilities — new grants require fresh user consent; update notes must
  declare privilege changes in plain language. Least-privilege is
  re-certified per release, not once at install.
- **Residual risk**: users approve updates without reading. Residual is
  accepted but bounded: new capabilities ship disabled until consented,
  so inattention yields *less* function, not more exposure.

### T11. Rollback / revocation of a privileged component

- **Attack path**: attacker downgrades the priv-app to a vulnerable
  version (rollback), or a revoked/compromised device keeps executing
  privileged jobs from cached approvals; stale snapshots route work to a
  device that should no longer be trusted.
- **What MUST hold**: version-aware trust — Core records per-device
  privileged-component version (descriptor `version` already exists in
  resolver candidates); minimum-version policy refuses privileged
  dispatch to outdated builds; rollback protection via versionCode
  monotonicity + (where available) Play/verified-boot guarantees.
  Revocation propagates both directions: Core revocation → 403 + offline
  + no new grants; device-side, unpaired/revoked state disables the
  privileged dispatcher entirely (cached grants purged, pending approvals
  expired). Resolver staleness flagging (already implemented: stale
  snapshots route but verdict says so) must be upgraded to *deny* for
  privileged capabilities when the snapshot is stale.
- **Residual risk**: rollback on attacker-controlled firmware cannot be
  fully prevented by app-layer checks; platform verified-boot is the real
  backstop. Document the dependency.

### T12. Stolen-device data exposure

- **Attack path**: thief has the physical device with a privileged body
  installed — lockscreen bypass, backup extraction, or coerced unlock
  exposes private data the privileged component can reach (messages,
  files, credentials, audit logs with metadata).
- **What MUST hold**: data minimization on the privileged body — no
  persisted transcripts/audio (current posture), secrets only in
  Keystore, device key non-exportable + (where available) biometric/
  lockscreen-bound; audit/metadata redacted (no secrets, per current
  `AuditLog` contract); remote revoke + wipe cooperation; privileged
  capabilities that read private data require fresh on-device
  authentication per use (never cached consent). Full-disk encryption
  assumed; privileged component adds nothing that weakens it (no debug
  backups, `allowBackup=false` for sensitive stores).
- **Residual risk**: unlocked-device access defeats most software
  controls. Goal is to bound exposure window (per-use auth, minimal
  retention, fast remote revoke), not to claim theft-proofness.

### T13. Supply-chain: malicious APK update to the system partition

- **Attack path**: attacker compromises the build/CI, signing key, or
  OTA channel and ships a trojaned priv-app / system image with
  backdoored privileged dispatch (silent exfil, self-granted permissions).
- **What MUST hold**: reproducible/signed builds, signing keys offline +
  rotation plan, update channel integrity (system-partition updates only
  via platform-verified OTA, never sideloaded APK claiming system
  privilege); on first boot / update, Core re-verifies component identity
  (package name + signing cert fingerprint + versionCode) before issuing
  any privileged grant — a re-signed or mismatched build gets zero
  privileged trust (falls to untrusted/discovered). Transparency: release
  hashes published, CI permission-diffing (T10) catches smuggled surface.
- **Residual risk**: signing-key compromise or platform-key compromise is
  catastrophic by nature; mitigations compress detection time (cert
  pinning, transparency, anomaly audit) rather than eliminating the
  scenario. Document key-custody assumptions explicitly.

## 4. What the privileged component MUST NOT do

These are prohibitions, not guidelines. Any one of them is a finding:

1. **No arbitrary shell** — no `Runtime.exec`, `ProcessBuilder`, shell
   string evaluation, or script-engine execution of Core/LLM/device
   provided strings. (Current codebase has none; privileged builds must
   keep it that way — verified by audit/subprocess scan per release.)
2. **No arbitrary code or URL loading** — no dynamic classloading from
   network, no WebView `addJavascriptInterface` bridging to privileged
   dispatch, no fetching executable content; URLs are never chosen by
   LLM/notification/screen/MCP content (current: "never pick URLs").
3. **No self-granting permissions** — the component never grants itself
   runtime permissions, device-admin, or role-holder status; every grant
   flows from OS consent + Core policy, never from an internal call.
4. **No silent security disablement** — never disables lockscreen,
   verified-boot, permission monitors, audit logging, or revocation
   handling, including "temporarily for debugging".
5. **No biometric / lockscreen / consent bypass** — automated or
   programmatic dismissal of system consent, permission, or
   authentication dialogs is forbidden (see T4).
6. **No silent private-data access** — no background reads of messages,
   files, location, microphone, camera, or credentials without a
   Core-authorized, user-approved, audited grant *and* (for sensitive
   classes) fresh on-device authentication. No bulk export path.
7. **No local execution of unvalidated proposals** — LLM text,
   notification text, screen text, MCP/tool output, deep-link extras,
   and clipboard content are never executed, evaluated, or dispatched;
   they enter only as proposal data through Core validation.
8. **No trust by transport alone** — possession of a Binder handle, an
   intent extra, or a LAN/HTTP connection is never authorization; every
   privileged call re-checks caller identity + live scoped grant.
9. **No persistent elevation** — no cached "always allow", no standing
   grants beyond bounded TTL, no background service that retains
   privileged power after revocation/unpair.

## 5. Residual risks accepted (explicitly)

- A fully compromised OS/firmware defeats app-layer checks (T11) —
  platform verified-boot is the backstop, not this model.
- A fully compromised Core can order legitimate-looking reads (T7) —
  bounded by OS consent + on-device auth + forensic audit, not eliminated.
- An inattentive user can approve a well-formed malicious proposal
  (T1/T2/T3) — mitigated by concrete approval UX, never auto-approval,
  verification display.
- A stolen unlocked device exposes what it can reach (T12) — bounded by
  minimization + per-use auth + remote revoke.
- Signing-key compromise is catastrophic (T13) — mitigated by custody +
  transparency + identity re-verification, with detection speed as the
  metric.

## 6. Conformance checklist for any future privileged-body proposal

- [ ] Invariant chain (§2) holds end to end; each link names its
      enforcing code (proposal validation, `decide`, grant, Binder check,
      dispatch allowlist, verification, audit).
- [ ] Every new capability ID has: risk label (≥confirm), bounded param
      schema, verification criteria, audit entry shape, approval UX text.
- [ ] Proposal-forgery tests (Stage 18 style) + injection-utterance tests
      extended to each new capability; verdicts byte-identical
      with/without forged fields.
- [ ] Binder caller-identity checks on every privileged entry; components
      `exported=false` by default; deep links carry zero authority.
- [ ] Notification/screen/MCP content handled as untrusted data with
      tests proving no action without Core authorization.
- [ ] Version-gated dispatch + revocation-disables-dispatcher (T11);
      stale-snapshot deny for privileged capabilities.
- [ ] Permission-diff CI + per-release least-privilege re-certification
      (T10); MUST-NOT list (§4) verified by scan + review.
- [ ] Audit-before-or-with execution for all privileged actions,
      Core-side and device-side, fail-closed on audit failure.
