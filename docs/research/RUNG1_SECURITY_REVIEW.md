# Rung-1 Security Review — Privileged Capability Gateway (forceStopPackage pilot)

Status: READ-ONLY REVIEW — no code changes, no device work.
Scope: proposed Rung-1 design only:

```
Zara Core → authenticated protocol (existing X-Device-Id/Key + device jobs)
  → Android agent → privileged capability gateway
  (single APK, explicit capability IDs, pilot: android.app.force_stop)
  → Android framework API (ActivityManager.forceStopPackage
  via FORCE_STOP_PACKAGES held through priv-app + allowlist)
```

References:
- Threat model: `docs/research/PRIVILEGED_SECURITY_MODEL.md` (T1–T13, invariant chain §2, MUST-NOT list §4, checklist §6).
- Architecture: `docs/research/PRIVILEGED_ANDROID_FINAL_REPORT.md` (§H rungs, §A/B permission taxonomy, §I POC gaps 4–5).
- Inherited Core gates (unchanged, must all hold): `core/resolver.py` (`resolve_proposal`, ignores `PROPOSAL_SECURITY_FIELDS`), `core/policy.py` (`PolicyEngine.decide`, deny-patterns, `safe|confirm|high_risk`), scoped single-use grants (`grant(who, capability, device_id, resource, ttl_s)`), `core/execution.py` (`submit` requires `auth.allow`, `approve` only from `WAITING_FOR_PERMISSION`), `core/audit.py` (append-only, no secrets/CoT), revocation (`/v1/agent/revoke` → 403 + offline), `docs/DEVICE_SECURITY.md` (per-device hashed/expiring/revocable keys, double schema validation).

## Overall verdict

**CONDITIONAL GO for lab-emulator Rung-1 only, subject to one BLOCKER fixed before any build.**
The architecture direction (Core stays authority; narrow capability-ID dispatch; no new gates; no shell) matches the threat model. The pilot API choice (`forceStopPackage`) is inherently destructive (kills target, clears alarms/widgets, user-visible denial of function), so the allowlist/validation/audit conditions below are load-bearing, not polish. Nothing here transfers to the daily driver (Vivo) or production: owned-image + own OTA + Play Integrity loss + key custody (Final Report §J/K) still apply.

## Risk-by-risk verdicts

### 1. Arbitrary package targeting — BLOCKER
- **Why:** `forceStopPackage(String)` takes one attacker-controllable string. Unbounded, it kills phone/dialer/launcher/system UI, bricks lab availability, kills Zara itself (self-DoS), or enables kill-then-spoof (kill genuine app, overlay fake). Threat-model mapping: T9 (over-broad surface) + invariant step 5 (param bounds).
- **Verdict: BLOCKER** if the gateway accepts any package name Core/LLM names.
- **Exact safeguard (required before build):**
  - Lab target allowlist, hardcoded in BOTH Core registry and device gateway mirror, e.g. exactly `{"com.zara.lab.victim"}`. No wildcards, no prefixes.
  - Device rejects anything not byte-equal to an allowlist entry — even if Core somehow sent it (T9 device-mirror rule).
  - Explicit denylist belt-and-braces: refuse own package, `android`, `com.android.systemui`, `com.android.phone`, launcher packages.
  - New target requires registry change + audit + fresh consent (T10); never silent enrollment.

### 2. Parameter injection / package-name smuggling — NEEDS-SAFEGUARD
- **Why:** Low injection surface (single String, no shell), but T2 still applies: oversized/unicode/extra-field smuggling, case tricks, embedded nulls/intents.
- **Verdict: NEEDS-SAFEGUARD.**
- **Exact safeguard:** gateway param schema, checked independently of Core (defense in depth):
  - `package_name: required String, length 1–255, regex ^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$, exact-match allowlist (§1).`
  - Reject unknown/extra fields, enforce size/depth caps (Stage-18 pattern: ≤16 keys, ≤4 KiB, depth ≤4).
  - Normalize once, compare once, use the compared value; log redacted package only.

### 3. Unauthorized local invocation / trust-by-transport — NEEDS-SAFEGUARD
- **Why:** MUST-NOT #8: "possession of a Binder handle, an intent extra, or a LAN/HTTP connection is never authorization." Job arrival over the existing authenticated transport proves the channel, not the right to kill a package. Final Report §I items 4–5 (Binder interface + Core auth) are explicitly NOT BUILT — this is the biggest structural gap in the proposal.
- **Verdict: NEEDS-SAFEGUARD (load-bearing).**
- **Exact safeguard — grant-bound dispatch:**
  - Every gateway call requires a live Core grant token bound to `(caller, capability_id, device_id, resource=package_name, expiry)`; verify on every call, single-use, TTL-bounded (minutes, not hours).
  - No job-ID-only or device-key-only dispatch. Cached/expired/replayed grant → deny.
  - Until the AIDL/service with per-method `enforceCallingPermission` exists, keep gateway dispatch a private in-process method called only from the authenticated job handler — no exported Service/Activity/Provider, no deep-link path, no SDK handle (see §4).

### 4. Confused deputy between app components — NEEDS-SAFEGUARD
- **Why:** T8. Exported components, implicit intents, deep links, or bundled SDK code in the same UID could call the gateway directly ("Core approved something similar earlier").
- **Verdict: NEEDS-SAFEGUARD.**
- **Exact safeguard:**
  - All components `exported=false` by default; no intent-filter on the gateway path; deep-link extras carry zero authority (re-authorize through Core).
  - On every gateway entry: `getCallingUid()/getCallingPid()` + package-name + signing-cert check against Zara cert; reject cross-UID callers.
  - Acknowledge residual: same-UID SDK code cannot be Binder-distinguished — mitigate by minimizing in-process third-party code in privileged builds, isolating dispatch in a separate UID/service process when the AIDL service lands (T8 residual).

### 5. LLM authority leakage (forged authorized/grant_id/allow) — NEEDS-SAFEGUARD
- **Why:** T1. If any forged field survives to the gateway, the pilot becomes LLM-triggered kill.
- **Verdict: NEEDS-SAFEGUARD (test-gated, not design-blocked).**
- **Exact safeguard:** extend Stage 18 + Stage 17 verbatim to the new capability ID: `extra="forbid"` on proposals, strip-before-validate, `resolve_proposal` ignores all `PROPOSAL_SECURITY_FIELDS`; add proposal-forgery test proving resolver verdicts byte-identical with/without forged fields for `android.app.force_stop`; `ExecutionEngine.submit` without `auth.allow` still parks/denies.

### 6. Privilege escalation via compromised/mislabeled LLM output — NEEDS-SAFEGUARD
- **Why:** T1/T2. Model proposes legitimate-looking but wrong target (wrong package, wrong device) that a tired user approves.
- **Verdict: NEEDS-SAFEGUARD.**
- **Exact safeguard:**
  - Register `android.app.force_stop` at `confirm` minimum (recommend `high_risk` given kill semantics); risk from resolved shape (`capability + resource`), never from LLM's label — deny-patterns + policy run on resolved shape.
  - Approval UX shows concrete effect ("Force-stop com.zara.lab.victim on ZaraLab emulator? Kills app, clears its alarms/widgets."), what/where/why, never bulk-approve; verification output shown before completion.
  - GUI rung stays non-executable; HUMAN rung terminal-only (existing resolver posture).

### 7. Capability spoofing / arbitrary API exposure — NEEDS-SAFEGUARD (design SAFE if kept narrow)
- **Why:** T9 + MUST-NOT #1/#2. The proposal's "explicit capability IDs" is the right shape; the failure mode is adding `runShell`/`writeSetting`/`grantPermission` generics later.
- **Verdict: NEEDS-SAFEGUARD (SAFE today only if the pilot ships exactly one ID).**
- **Exact safeguard:**
  - Gateway dispatches on exact capability-ID match (`android.app.force_stop`), allowlisted param schema only. Unknown IDs refused on-device even if Core sent them (device allowlist mirrors Core registry; default-deny).
  - No stringly-typed pass-through to `Settings.*`, `DevicePolicyManager`, shell, broadcasts. No `Runtime.exec`/`ProcessBuilder`/script-engine (MUST-NOT #1), no dynamic classloading/WebView-bridge/URL-chosen-by-content (MUST-NOT #2) — verify by scan per release.
  - Each future ID needs: risk label ≥confirm, bounded schema, verification criteria, audit shape, approval text (checklist §6).

### 8. Device spoofing (stolen device key impersonating the lab device) — NEEDS-SAFEGUARD
- **Why:** T6. Attacker with extracted device key advertises fake capabilities, forges job results, probes privileged dispatch.
- **Verdict: NEEDS-SAFEGUARD (existing posture sufficient as floor, harden for privileged tier).**
- **Exact safeguard:** preserve `DEVICE_SECURITY.md` properties (per-device keys hashed server-side, expiring, revocable → 403; advertisements = existence only, never authorization; job results feed verification, not blind success; approvals device-scoped to own-device); add privileged-tier hardening: short grant/key TTLs, rotation support, rate-limit + anomaly detection on device endpoints, Keystore (hardware-backed, non-exportable) storage, and — critically — revocation/unpair remotely disables the on-device privileged dispatcher (cached grants purged, pending approvals expired).

### 9. Replay (old grant / old job / old approval-tap re-executed) — NEEDS-SAFEGUARD
- **Why:** T3/T11 edge. Job redelivery or replayed grant token re-kills target without fresh approval.
- **Verdict: NEEDS-SAFEGUARD.**
- **Exact safeguard:** single-use grants consumed on first use; job handler idempotent per `(execution_id, grant_id)` — duplicate delivery re-resolves, never re-executes; notification taps carry only `(execution_id, allow)` for a Core-created hold (existing `zara_notifications.dart` pattern), never package/URL/command extraction; replay of an old ID re-resolves under current policy.

### 10. Stale authorization (cached approval, revoked device, outdated build, stale Fabric snapshot) — NEEDS-SAFEGUARD
- **Why:** T11. Resolver's current staleness behavior (route-but-flag) is unsafe for privileged actions.
- **Verdict: NEEDS-SAFEGUARD.**
- **Exact safeguard:** upgrade to deny for privileged IDs: stale-snapshot → deny; version-gated dispatch (Core records per-device privileged-component version, minimum-version policy refuses outdated builds); rollback protection via versionCode monotonicity (+ verified-boot where available); revocation propagates both directions (Core 403 + offline + no new grants; device disables dispatcher, purges cache).

### 11. Missing / insufficient audit — NEEDS-SAFEGUARD
- **Why:** Invariant step 8: "what did the privileged body do?" must always be answerable; audit failure must fail closed for privileged actions.
- **Verdict: NEEDS-SAFEGUARD.**
- **Exact safeguard — audit-before-or-with, both sides, fail-closed:**
  - Core-side (`core/audit.py`): `actor, capability_id=android.app.force_stop, target_package (redacted to allowlist value), device_id, grant_id, execution_id, policy_verdict, approval_ref, result, verification_outcome`. No secrets, no CoT (existing contract).
  - Device-side tamper-evident log: `timestamp, caller_uid/package/signature, grant_id, capability_id, package, gateway_verdict, framework_result, verification`. Audit-write failure → deny execution.

### 12. Compromised Core / rogue operator ordering kills — accepted residual, bounded (SAFE with documented assumption)
- **Why:** T7. Core is inherently powerful; valid credentials can order legitimate-looking kills.
- **Verdict: SAFE as architecture (residual explicitly accepted in threat-model §5), provided it is documented, not eliminated.**
- **Boundings:** device verifies Core (pinned HTTPS/WSS cert in prod, pairing binding, dispatch signatures/session tokens — never plain-HTTP trust); OS permission remains outer wall; security-sensitive dispatches get on-device user-visible confirmation a Core-only attacker cannot synthesize; device-side log makes the episode forensically visible. Production deployment must document this trust assumption explicitly.

## MUST-NOT list compliance (threat-model §4)

| # | Prohibition | Pilot verdict |
|---|---|---|
| 1 | No arbitrary shell | SAFE by design (single framework-API call, no exec) — hold via per-release scan |
| 2 | No arbitrary code/URL loading | SAFE by design — no URL/classload/WebView-bridge in path |
| 3 | No self-granting permissions | SAFE — and platform-enforced: `GRANT_RUNTIME_PERMISSIONS` lacks the `privileged` flag (Final Report §A), so priv-app cannot self-serve grants |
| 4 | No silent security disablement | SAFE — no such path in pilot |
| 5 | No consent/auth-dialog bypass | SAFE provided no auto-dismiss is added (T4); force-stop needs no consent dialog, so nothing to bypass — do not invent automation around any future consent surface |
| 6 | No silent private-data access | N/A to pilot (no data read) — audit metadata still redacted |
| 7 | No local execution of unvalidated proposals | NEEDS-SAFEGUARD — LLM/notification/screen/MCP/deep-link/clipboard content enters only as proposal data through Core validation (§5/§6) |
| 8 | No trust by transport alone | NEEDS-SAFEGUARD — grant-bound dispatch per §3, the load-bearing gap |
| 9 | No persistent elevation | NEEDS-SAFEGUARD — bounded-TTL single-use grants only; no always-allow; revocation disables dispatcher (§10) |

## Recommended pre-build checklist (Rung-1 pilot only)

- [ ] `android.app.force_stop` registered in Core registry with risk ≥confirm (prefer `high_risk`), bounded schema (§2), verification criteria (target package state == stopped), audit shape (§11), approval text (§6).
- [ ] Lab target allowlist in Core registry AND device gateway mirror; arbitrary-target test proves non-allowlisted package denied device-side even when Core message is forged.
- [ ] Grant-bound dispatch implemented and tested: missing/expired/replayed/wrong-resource grant → deny; negative test with unprivileged caller denied (covers Final Report §I gaps 4–5 at Rung-1 scope).
- [ ] `exported=false` default verified; Binder caller-identity check on gateway entry; deep-link/intent zero-authority test.
- [ ] Proposal-forgery + injection-utterance tests extended to the new ID; verdicts byte-identical with/without forged fields.
- [ ] Stale-snapshot deny + version gate + revocation-disables-dispatcher tests.
- [ ] Audit-before-or-with both sides; audit-failure → deny test.
- [ ] Permission-diff CI + MUST-NOT scan wired; release notes declare privilege change in plain language (T10).
- [ ] Lab-only confinement re-confirmed: emulator image only, Vivo never touched, no stock-OTA / Integrity expectations (Final Report §K).

## What this review does NOT approve

- Any second capability ID, any broadening of the package allowlist beyond the single lab victim, any generic primitive (`runShell`, `writeSetting`, `grantPermission`, URL/intent forwarding).
- Any approval path that skips a link in the invariant chain ("trusted caller, skip policy", cached approvals, UI-already-asked).
- Any production/daily-driver deployment: OTA-fork, key-custody, Integrity-loss, and platform-verified-boot assumptions (threat-model §5, Final Report §J/K) remain unmet by design at this rung.
