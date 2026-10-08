# Rung 2 — Privilege Classes (READ / INTERACT / SENSITIVE / DESTRUCTIVE)

Status: SPECIFICATION ONLY. No code changed, no device work.
Evidence conventions: **DEVICE** / **CORE-UNIT** / **DOC** per
`RUNG1_FINAL_REPORT.md`; everything not measured is **NOT YET MEASURED**.

This doc fixes the class semantics that `gui.*` IDs (and any future Rung-2
primitive) inherit. Classes map onto the existing `RiskLevel`
(`safe/confirm/high_risk`) and the existing gates (resolver → policy →
governor → approval → job → audit); they invent no new authority.

## 1. Class definitions

### READ — observe, never change state

- Semantics: pure observation. No input event, no setting, no file, no
  traversal beyond the on-screen tree the OS exposes to the bound service.
- Risk mapping: `RiskLevel.SAFE`. No approval required beyond normal routing
  (policy/governor still apply; trust/presence still gate).
- Hard rule: a READ capability MUST NOT accept action parameters. If it can
  carry a tap/gesture/text payload, it is not READ.
- Audit: result hash + node count + `truncated` flag; redacted nodes appear
  as `redacted: true`, never with content.

### INTERACT — reversible, user-visible state change

- Semantics: injects the same class of event a human finger could produce
  (tap, swipe, scroll, key Back/Home/Recents, dismissing a non-consent
  dialog). Effects are visible on screen and undoable by further UI action.
- Risk mapping: `RiskLevel.CONFIRM`. Requires the confirm gate (operator
  acknowledgment path per existing policy chain); exact target + package
  bound at dispatch.
- Hard rules: exactly one action per job; no sequences in one envelope;
  non-idempotent gestures never blind-retry (see UICONTROL §6); system
  consent dialogs are never targets (stop-and-surface, UICONTROL S3).
- Audit: full action record (selector, pre/post snapshot hashes,
  verify predicate + outcome).

### SENSITIVE — reads or writes data with exfiltration / credential / PII surface

- Semantics: the capability handles, transports, or writes data that is
  itself the asset — typed text (passwords, OTPs, messages), screen pixels
  (which may show banking, health, private messages), anything crossing the
  device→Core boundary in user-legible form.
- Risk mapping: `RiskLevel.HIGH_RISK`. Core-approved per single use:
  approval binds the exact `(who, capability, device, target, payload-shape)`
  tuple via single-use grant (RUNG1 §12 precedent). Approval cards show the
  concrete effect.
- Hard rules: payload values (typed text) never enter audit/LLM context —
  names + lengths only (extends RUNG1 N-16 secret-free invariant); capture
  outputs travel as handles, never inline pixels; password/redacted nodes
  are invisible to both inspect output and targeting.
- Audit: approval tuple + name/length metadata + delivery receipt; content
  excluded by construction.

### DESTRUCTIVE — irreversible or externally-visible effects

- Semantics: anything that cannot be undone on-screen — send, post, pay,
  delete, wipe, permission/policy change, factory-affecting ops. Includes
  GUI paths that WOULD reach such effects (there are none registered —
  deliberately).
- Risk mapping: `RiskLevel.HIGH_RISK` minimum, plus the two existing
  destructive disciplines: (a) `decide()` never consults grants for
  high_risk — every instance needs fresh human approval (RUNG1 §12);
  (b) high_risk originals get NO fallback — destructive work never fails
  over (`core/resolver.py:102-146`, `FallbackRegistry` risk discipline).
- Hard rule: no GUI shortcut to a destructive effect is ever registered
  (UICONTROL §1 refusal list). Destructive effects route via their own
  non-GUI capability path or HUMAN — never smuggled inside an INTERACT tap
  ("tap the Send button" is DESTRUCTIVE-by-effect and refused as INTERACT).

## 2. Which IDs fall where

| Class | IDs | Rationale |
|-------|-----|-----------|
| READ | `gui.screen.inspect`, `gui.wait_for_element`, `gui.assert_visible` | Observation/predicates only; no input injection. (Spec status; device behavior NOT YET MEASURED.) |
| INTERACT | `gui.tap`, `gui.swipe`, `gui.scroll`, `gui.long_press`, `gui.drag`, `gui.key_event` (Back/Home/Recents), `gui.dismiss_dialog` (non-consent allowlist) | Human-finger-equivalent, visible, reversible. (All RESERVED except `gui.tap` spec; NOT YET MEASURED.) |
| SENSITIVE | `gui.text_input`, `gui.screen.capture` | Credential/PII-bearing payloads in both directions. (RESERVED; NOT YET MEASURED.) |
| DESTRUCTIVE | (no `gui.*` member — by design) | Destructive-by-effect UI flows are classified by their EFFECT, not their gesture: "tap Send" is DESTRUCTIVE and refused at INTERACT level. |

Effect-over-gesture rule: classification follows the OUTCOME, never the
mechanism. A `gui.tap` whose approval-bound target resolves (post-inspect)
to a Send/Pay/Delete/Grant control is re-classified DESTRUCTIVE at verify
time and refused (`failed(reason=effect-escalation)` + audit). The device
does not "notice and approve anyway" — escalation always stops the job.

## 3. The never-auto-elevate rule

**Classes never auto-elevate. Motion is only downward by refusal, never
upward by convenience.** Concretely:

1. No fallback climbs a class. The existing `FallbackRegistry` already
   enforces risk discipline (a fallback may never exceed the original's
   risk; high_risk originals get NO fallback — `core/resolver.py:102-146`,
   CORE-UNIT). SENSITIVE and DESTRUCTIVE therefore have no fallback
   entries, and none may be registered: a failed `gui.text_input` never
   becomes a `gui.tap`-plus-paste, a refused destructive tap never becomes
   a "helpful" alternative gesture.
2. SENSITIVE and DESTRUCTIVE stay Core-approved, per instance. `high_risk`
   bypasses grant consultation entirely (RUNG1 §12: `decide()` never
   consults grants for high_risk); single-use engine grants minted on
   approve bind one exact tuple and die with the job. There is no
   pre-approval, no standing grant, no "same as last time" fast path.
3. The device holds a class mirror per ID (same dual-allowlist pattern as
   Rung 1's `forceStopLabTargets`): the runner refuses any job whose
   class tag exceeds the ID's registered class, even if Core messaging
   were forged — forgery yields byte-identical denial (RUNG1 N-10 precedent).
4. Unknown / unregistered IDs fail closed device-side (`notImplemented` +
   runner refusal default — RUNG1 §5), which is class-deny by default.

Policy risk classes cited (REPO, all CORE-UNIT-covered): `RiskLevel`
`safe/confirm/high_risk` (`core/models.py`, `core/capabilities.py:131,340`);
`PermissionLevel` gating on code tools (`core/opencode_tools.py`);
`authorize_capability` trust→presence→policy→governor ordering and
`route_capability` deny-vs-defer mapping (`core/fabric.py`,
`RUNG1_RELIABILITY.md` mechanisms section). This doc adds no new gate —
it pins the mapping from UI-control IDs onto those existing classes so a
future implementer cannot "just" widen a tap into a send.
