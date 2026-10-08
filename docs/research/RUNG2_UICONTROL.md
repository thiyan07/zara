# Rung 2 — UI Control Capability Model (gui.*)

Status: SPECIFICATION ONLY. No code changed, no device work.
Evidence conventions (per `RUNG1_FINAL_REPORT.md`): **DEVICE** = measured on
the ZaraLab emulator (API 36 userdebug) · **CORE-UNIT** = pytest, no device ·
**DOC** = official Android/AOSP documentation. Anything else is marked
**NOT YET MEASURED**.

Parent constraints: FINAL REPORT §H (rung order — Rung 2+ system-service work
is gated on a written per-primitive gap + approved AOSP build cost; this doc
is part of that written gap, not an authorization to build) and §F/G (UI
inspection/injection has no sanctioned path below user-enabled Accessibility
or platform signature; Zara's by-design refusal of `accessibility.automation`
stands until explicitly reversed).

## 1. The 12 GUI capability IDs

Namespace `gui.*`, one narrow capability ID each (T9 pattern: bounded params,
per-ID descriptor, per-ID policy/approval — same discipline as Rung 1's
single `android.app.force_stop` leaf).

| # | ID | Status | Class (see `RUNG2_PRIVILEGE_CLASSES.md`) |
|---|----|--------|------------------------------------------|
| 1 | `gui.screen.inspect` | IMPLEMENTED (spec + descriptor shape; device behavior NOT YET MEASURED) | READ |
| 2 | `gui.tap` | IMPLEMENTED (spec + descriptor shape; device behavior NOT YET MEASURED) | INTERACT |
| 3 | `gui.swipe` | RESERVED | INTERACT |
| 4 | `gui.scroll` | RESERVED | INTERACT |
| 5 | `gui.long_press` | RESERVED | INTERACT |
| 6 | `gui.drag` | RESERVED | INTERACT |
| 7 | `gui.key_event` | RESERVED (Back/Home/Recents only; no power-key) | INTERACT |
| 8 | `gui.text_input` | RESERVED | SENSITIVE |
| 9 | `gui.screen.capture` | RESERVED | SENSITIVE |
| 10 | `gui.wait_for_element` | RESERVED | READ |
| 11 | `gui.assert_visible` | RESERVED | READ |
| 12 | `gui.dismiss_dialog` | RESERVED | INTERACT |

"IMPLEMENTED" above means: ID registered in the spec, descriptor field rules
(§3) fixed, resolver/policy/governor path defined, audit shape defined.
It does NOT mean measured on device — no `gui.*` op has DEVICE evidence.
No `AccessibilityService` exists in the app today (confirmed absent per
`ZARA_ANDROID_INTEGRATION.md` §4); enabling even IDs 1–2 on-device requires
the items in §2.

There is deliberately NO `gui.destructive_confirm`, `gui.send`, `gui.pay`,
`gui.delete`, or `gui.grant_permission`: destructive effects are never given
their own GUI shortcut (see privilege-classes doc; destructive stays
Core-approved via the non-GUI path or HUMAN).

## 2. RESERVED IDs — what would be required to enable each

Common preconditions for ALL 12 (even the two IMPLEMENTED-spec IDs) before
any on-device enablement:

1. Explicit design-decision reversal of the `accessibility.automation=false`
   refusal (`capabilities.dart:47`, tests pinning it not-advertised).
2. A `BIND_ACCESSIBILITY_SERVICE` `AccessibilityService` + XML config
   (`canRetrieveWindowContent`, `canPerformGestures` as needed), declared in
   the manifest — user enables it in Settings; there is no silent path (DOC).
3. Registration-time resolver guard for `gui.*` (FINAL REPORT appendix F1:
   `level_of()` cannot return GUI today; non-execution holds by absence +
   policy, not by block — the guard must exist before any UI-automation work).
4. Per-ID descriptor + input/output schema + risk class + approval binding
   (concrete effect on the approval card, per RUNG1 §12 improvement).
5. Google Play Accessibility API policy disclosure if ever store-distributed
   (DOC — use restricted to accessibility purposes + prominent disclosure).

Per-ID additions:

| ID | Extra requirement to enable |
|----|------------------------------|
| `gui.swipe` / `gui.scroll` / `gui.drag` | Gesture API (`dispatchGesture`, API 24+; DOC); `canPerformGestures`; bounded path/duration params; NOT YET MEASURED |
| `gui.long_press` | Gesture API long-press (duration bound); NOT YET MEASURED |
| `gui.key_event` | `GLOBAL_ACTION_BACK/HOME/RECENTS` only; power-key/device-power verbs are platform-gated and stay out (FINAL REPORT §B); NOT YET MEASURED |
| `gui.text_input` | `ACTION_SET_TEXT` on an editable node; SENSITIVE class: never into password fields without separate Core approval binding the exact payload; Play-policy sensitive; NOT YET MEASURED |
| `gui.screen.capture` | `AccessibilityService.takeScreenshot()` (API 30+, window-scoped, throttled — DOC) OR MediaProjection per-session consent + FGS; silent capture has no app-tier path (FINAL REPORT §G); SENSITIVE class; NOT YET MEASURED |
| `gui.wait_for_element` | Read-only poll loop with bounded timeout; must terminate in `no-match` (never tap a guess); NOT YET MEASURED |
| `gui.assert_visible` | Read-only boolean; verify-step of the action→verify loop (§6), never an action by itself; NOT YET MEASURED |
| `gui.dismiss_dialog` | Allowlist of dismissable dialog classes (system permission dialogs are NEVER auto-dismissed — consent gates are not bypassable, FINAL REPORT §G); NOT YET MEASURED |

## 3. Descriptor field rules (per-ID)

Every `gui.*` descriptor follows `core/capabilities.py`
`CapabilityDescriptor` validation (CORE-UNIT-verified bounds; REPO):

- `id`/`name` match `^[a-z][a-z0-9_.:\-]{1,63}$`; `descriptor_version == "1"`
  (anything else rejected, never executed); `version` is MAJOR.MINOR.PATCH.
- `platforms: ["android"]`; `execution: "on-device"`.
- `risk` from the privilege class (READ→safe, INTERACT→confirm,
  SENSITIVE→high_risk; see classes doc). `requires` lists the OS mechanism
  (`BIND_ACCESSIBILITY_SERVICE`, and for capture the MediaProjection path).
- Non-`available` descriptors MUST carry `availability_reason` (≤200 chars);
  `usable() = available ∧ os_permission_granted ≠ False`.
- Core-attached snapshot fields (`advertised_at`, `source`,
  `software_version`) must be ABSENT from device-sent documents (Core sets
  them; device-sent copies rejected).
- Forbidden keys (`authorized/allow/grant/trust/exec/shell/…`) rejected at
  any depth outside schema `properties`; `extra="forbid"`.
- `input_schema`/`output_schema` per-ID below; `additionalProperties: false`
  on all action inputs.

Per-ID schema minima:

- `gui.screen.inspect`: input `{package?, max_nodes?}`; output
  `{nodes: [{resource_id?, text?, content_desc?, bounds?, class?}], truncated}`.
  Text of password/flag-secure nodes MUST be redacted device-side
  (empty string + `redacted: true`), never shipped to Core/audit/LLM.
- `gui.tap`: input `{target: TargetSelector, verify?: VerifySpec}` —
  exactly one selector, exactly one action per job (no tap-sequences in one
  envelope; sequences are separate Core-dispatched jobs).
- Gesture IDs (`swipe/scroll/long_press/drag`): input adds bounded
  `{duration_ms ≤ 2000, distance_px ≤ screen diagonal}`; output adds
  `{performed: bool}`.
- `gui.text_input`: input `{target, text (≤500 chars), submit?: bool}`;
  `text` is SENSITIVE — audit stores the field NAME and length, never the
  value (secret-scan invariant from RUNG1 §14/N-16 extends here).
- `gui.screen.capture`: input `{scale?, max_age_s?}`; output is a
  handle/reference, never inline pixels in the job envelope.
- `gui.wait_for_element`: input `{target, timeout_s ≤ 30}`; output
  `{found: bool}`.
- `gui.assert_visible`: input `{target}`; output `{visible: bool}`.
- `gui.dismiss_dialog`: input `{dialog_class ∈ allowlist-enum, target?}`;
  unknown classes rejected pre-handler (dual-allowlist pattern from RUNG1 §5).

## 4. Snapshot bounds table

Device→Core snapshot traffic for `gui.*` obeys the existing envelope bounds
(all CORE-UNIT-verified in `core/capabilities.py`, `core/tools.py`; REPO):

| Bound | Value | Source |
|-------|-------|--------|
| Descriptor doc | ≤ 32 KiB (`MAX_DOC_BYTES`) | `capabilities.py:25` |
| Describe batch | ≤ 100 descriptors | `MAX_DESCRIBE_BATCH` |
| `description` | ≤ 500 chars | `MAX_DESCRIPTION_LEN` |
| `requires` | ≤ 20 permissions | `MAX_PERMISSIONS` |
| `aliases` / `examples` / `keywords` | ≤ 10 / ≤ 5 / ≤ 15 | `MAX_ALIASES/EXAMPLES/KEYWORDS` |
| Schema doc | ≤ 8192 bytes, depth ≤ 5 | `MAX_SCHEMA_BYTES/DEPTH` |
| Job result envelope | ≤ 16 keys, ≤ 4 KiB, depth ≤ 4 | `core/tools.py` (RUNG1 §21) |
| Inspect node list per snapshot | ≤ 200 nodes, then `truncated: true` | NEW bound (spec; NOT YET MEASURED) |
| Snapshot freshness | `stale_s = 120 s` presence derivation; re-register without fresh describe → `mark_stale` | `core/fabric.py` (RUNG1_RELIABILITY §verdicts) |

Oversized inspect output is truncated device-side with `truncated: true`,
never split across unbounded follow-up jobs by the device (Core decides
whether a narrower re-inspect is warranted).

## 5. Targeting priority order + ambiguity / no-match rules

Deterministic selector resolution, highest priority first:

1. `resource_id` (exact match, package-scoped)
2. `content_desc` (exact match)
3. `text` (exact match; password/redacted nodes never match — they are
   invisible to targeting)
4. `bounds` center-point (only if the bounds came from a Core-accepted
   inspect snapshot ≤ `max_age_s`, never from prose/OCR)

Rules (no exceptions):

- **Single-match required.** Zero matches → `no-match`: job completes
  `failed` with `reason=no-match`, audit `execution_finished(state=failed)`.
  Never tap a "closest" candidate, never fall back to coordinates, never ask
  the LLM to reinterpret the screen (LLM forgery is inert — RUNG1 N-10/N-11).
- **Ambiguity → stop.** Two or more matches at the winning priority level →
  `ambiguous`: complete `failed` with `reason=ambiguous` + `match_count`,
  audit it, and surface the candidates for a HUMAN or a narrower Core
  re-dispatch. Retrying the same selector is a deny, not a defer.
- **No-match → stop.** Same as above with `reason=no-match`. A `gui.tap`
  with no verified target never executes — "tap roughly here" does not exist.
- **Coordinates are never derived from OCR'd or accessibility-scraped
  labels by the LLM** (threat modelaviestablished in
  `PRIVILEGED_SECURITY_MODEL.md:178-188`): every coordinate must trace to a
  snapshot node ID from §4. Free-form `(x, y)` input from Core/LLM is
  rejected pre-handler.
- **Cross-package targeting** follows the Rung-1 dual-allowlist pattern:
  Core enum + device mirror const; any package outside both → deny
  pre-handler (RUNG1 §5/§12, security BLOCKER precedent).

## 6. Action→verify loop

```
Core dispatch (1 action, bound params, approval-bound effect)
        |
device: resolve selector (§5: single match or stop)
        |
device: perform action (gesture API / node action)
        |
device: re-inspect (fresh snapshot, ≤ max_age_s)
        |
device: VERIFY postcondition (assert_visible / state predicate)
        |
   +----+----+
   | pass    | fail, attempts left (≤ MAX_UI_ATTEMPTS = 3, spec; NOT YET MEASURED)
   |         v
 done    retry SAME action once semantics allow (idempotent only;
 (result  text_input re-type and tap are idempotent; drag is NOT —
  MUST     non-idempotent actions never blind-retry, go to stuck handling)
  carry
  verify_
  state)
             | fail, attempts exhausted
             v
        stuck handling (§7) → deny-and-report, never infinite loop
```

Verification is Core-`pidof`-style authoritative thinking applied to UI:
"API returned" (gesture dispatched) is never success — only the
post-`inspect` state predicate decides `done` vs `failed`
(RUNG1 §13 definition-of-done precedent). The verify predicate and its
result travel in the result envelope; `verify_device_result` fails the job
on mismatch (`verification_failed` audit).

## 7. Stuck-detection states and responses

Stuck = the loop in §6 cannot converge within bounds. Detection is
device-side counters + Core-side job timeout; response is always
deny-and-report (terminal audited state), never an unbounded recovery loop.

| # | Stuck state | Detector | Response |
|---|-------------|----------|----------|
| S1 | `no-progress` — N consecutive (§6: 3) action→verify cycles fail with identical pre/post snapshots | device counter | Complete `failed(reason=stuck/no-progress, attempts, snapshots_hash)`; audit; Core route = deny (not defer — same inputs cannot succeed) |
| S2 | `oscillating` — post-states cycle A→B→A across attempts (e.g. toggle fighting a system switch) | device state-hash history (last 4) | Stop after detecting the cycle; `failed(reason=stuck/oscillating)`; never "one more toggle" |
| S3 | `blocked-dialog` — a system consent/permission dialog appears mid-flow (runtime grant, NLS bind, MediaProjection consent, assistant-role) | dialog-class classifier on re-inspect | STOP immediately; `failed(reason=blocked/system-dialog)`; surface the user-consent step to HUMAN. Auto-dismissing consent dialogs is forbidden (see `gui.dismiss_dialog` allowlist, §2) |
| S4 | `app-changed` — foreground package/activity differs from the approval-bound target (app killed, crashed, or navigated away) | package/activity check on every re-inspect | STOP; `failed(reason=target-changed)`; never continue the flow in the new app (cross-package rule, §5). If the target was killed, the reliability addendum's app-killed row governs re-dispatch |
| S5 | `core-offline` — result cannot be delivered (poll/complete unreachable) | bounded `RetryPolicy` (≤5, 1s→60s; RUNG1_RELIABILITY case 3/4) | Hold result as bounded `PendingOp(kind=job-result)`; retry delivery only (never re-execute the action); queue-full → truthful offline, never silent drop |
| S6 | `verify-unavailable` — re-inspect itself fails (service unbound, snapshot unavailable) | inspect error / `os_denied` flip | `failed(reason=verify-unavailable)`; descriptor flips to `os_denied`/`unavailable` with reason + fresh `describe` (RUNG1_RELIABILITY case 9 pattern) |

All six responses complete the job terminally (`failed`, never `expired`
by silence), carry `attempts/snapshots_hash/reason`, and audit. Recovery is
a NEW Core decision (fresh approval, possibly HUMAN per the app-hierarchy
doc) — never device-side improvisation. Bounds above are spec values;
convergence rates, flake rates, and timing are all NOT YET MEASURED.
