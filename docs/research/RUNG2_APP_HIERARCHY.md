# Rung 2 — App-Control Hierarchy (7 levels, native API first, human last)

Status: SPECIFICATION ONLY. No code changed, no device work.
Evidence conventions: **DEVICE** / **CORE-UNIT** / **DOC** per
`RUNG1_FINAL_REPORT.md`; everything not measured is **NOT YET MEASURED**.

Rule: for any "make app X do Y" request, Core MUST attempt levels in order
1 → 7 and take the FIRST level that can satisfy the request. Skipping a
working lower level to use a more invasive one is a policy violation, not
an optimization. (This is the Rung-2 analogue of the resolver's
pinned-device no-failover and no-destructive-failover rules.)

## The 7 levels

### Level 1 — Native public API / SDK contract (preferred)

The target app (or the OS) offers a first-class programmatic interface:
public Intent actions, documented ContentProviders, app actions / shortcuts,
assistant integrations the app itself exposes, public SDK callbacks.
No view tree is read, no gesture injected, no special permission needed
beyond what the API declares.

- Zara examples: query playback state via the media-session/notification
  metadata the OS already surfaces; open a screen via the app's own deep
  link; read data the user shares through the share sheet. (Feasibility per
  app is DOC/app-specific; no blanket DEVICE claim — NOT YET MEASURED per
  target app.)
- Why first: zero consent debt, zero fragility (contracts versioned by the
  app vendor), fully auditable parameters, no PII over-collection.

### Level 2 — System-delegated / managed API (Device Owner / role-held)

A system API performs the effect on Core's behalf under an explicit,
revocable delegation: Device Owner / Profile Owner (`setPermissionGrantState`,
managed install/suspend/lock-task — DEVICE-proven in FINAL REPORT §C),
role-held APIs, companion-device pairing, notification-listener reads the
user explicitly bound. User consent happened once, at delegation time, and
is revocable in Settings.

- Zara examples: DO silent managed install of a lab package; permission
  auto-grant policy for a fleet app; suspend/hide a kiosk app.
  (DEVICE-proven on the ZaraLab emulator per FINAL REPORT §C.)
- Why second: still API-contract semantics with OS-side enforcement, but
  spends provisioning/consent capital (setup-time, revocation friction —
  FINAL REPORT §C documents removal friction), so Level 1 wins when it exists.

### Level 3 — Privileged adapter (priv-app allowlisted permission)

One narrow capability ID → one allowlisted `signature|privileged`
permission on an owned image (Rung-1 pattern: `android.app.force_stop`
via `FORCE_STOP_PACKAGES` — DEVICE-proven, `RUNG1_FINAL_REPORT.md` §9).
`WRITE_SECURE_SETTINGS`, battery-stats, USB/BT holdings live here.

- Zara examples: force-stop the allowlisted lab victim (DEVICE-proven);
  secure-settings write for a lab kiosk toggle (grant mechanics proven,
  per-flow behavior open — FINAL REPORT §F).
- Why third: spends image capital (allowlist is boot-fatal if wrong —
  FINAL REPORT §A.4; OTA fork + key custody + Play Integrity loss per §J/K)
  for exactly one primitive at a time, each gated on its own written gap.

### Level 4 — Platform signature / custom system service internals

Effects only reachable with the platform key or inside `system_server`:
silent screenshots via SurfaceFlinger, input injection, tethering enable,
direct AMS/WMS/PMS internals, persistent scheduler/compositor policy
(FINAL REPORT §D/F). Requires full AOSP sync/build, own keys, own OTA
stream, per-bulletin rebases — Rung 2+ build cost, explicitly deferred.

- Zara examples: NONE built. Candidate gaps that could justify this level
  (each needs its own written per-primitive gap + approved build cost):
  silent screenshot primitive, injected-input primitive.
  (All DOC-sourced; NOTHING at this level is DEVICE-measured — NOT YET MEASURED.)
- Why fourth, not higher: maximum capability, maximum cost and blast radius
  (confused-deputy per-method enforcement burden, compromised-Core blast
  radius — FINAL REPORT §J). It outranks Accessibility on power but is
  ordered here because reaching for OS surgery before trying user-consented
  UI actuation inverts the principle of least privilege.

### Level 5 — AccessibilityService UI actuation (user-enabled, revocable)

The bound `AccessibilityService` observes the `AccessibilityNodeInfo` tree
and injects human-equivalent gestures (`click`, `scroll`, `setText`,
`dispatchGesture`, API-30 `takeScreenshot`) — the `gui.*` capability model
in `RUNG2_UICONTROL.md`. Adds NO file, policy, signature, or consent
bypass (privilege-matrix Tier-2 finding, DOC + DEVICE taxonomy).

- Zara examples: `gui.screen.inspect` → `gui.tap` on an allowlisted
  in-house lab app's clearly-labeled control; `gui.assert_visible` as the
  verify step. (Spec only; device behavior NOT YET MEASURED; the service
  does not exist in the app today.)
- Why Accessibility sits at level 5 (and not higher):
  1. **Consent-gated and revocable** — works only while the user leaves the
     service enabled; any update, settings visit, or policy can remove it.
     Levels 1–4 rest on durable contracts or image facts instead.
  2. **Lossy observation** — `importantForAccessibility="no"`, custom views,
     secure-flagged windows, and withheld content mean the tree is a subset
     of the screen (privilege-matrix Tier-2 myth-busting, DOC). Acting on a
     partial tree is inherently less reliable than calling an API.
  3. **Human-finger semantics only** — it cannot do anything the user could
     not tap themselves, so it can never substitute for a missing API
     (no silent capture, no permission self-grant, no settings bypass —
     FINAL REPORT §G).
  4. **Store-policy and user-trust cost** — Play's Accessibility API policy
     restricts use to accessibility purposes with prominent disclosure
     (DOC); spending that trust on general automation is the fastest way to
     lose distribution and user confidence.
  5. **Fragility** — layouts, IDs, and flows change per app release; every
     selector is tech debt. API contracts (L1) and OS enforcement (L2–L4)
     do not break when a button moves.

### Level 6 — Human-assisted (Core guides, human acts)

Core prepares everything short of the act — deep link ready, exact steps,
pre-filled text for the human to paste, an approval card showing the
concrete effect — and the human performs the tap/type/consent-grant on
their own device. The resolver's HUMAN rung (`resolver.py:65`, terminal
planning output) and `REQUIRES_ESCALATION` status already point here.

- Zara examples: "open Settings → Accessibility → enable Zara UI control"
  guided flow with screenshots; pasting a Core-drafted message the human
  reviews first; granting a runtime permission at the system dialog.
  (Procedure-level; per-flow success rates NOT YET MEASURED.)
- Why sixth: always available (no build, no permission, no policy cost),
  but spends human attention — the scarcest resource — and cannot run
  unattended. Beats Level 7 only because Core still does the preparation.

### Level 7 — Human performs manually (terminal)

The human does the whole thing without Core assistance beyond the
suggestion. Resolver HUMAN output, no job, no audit beyond the
conversation record.

- Zara examples: "please attach the file in your banking app yourself —
  I can't reach inside it." (Fallback honesty, always available.)
- Why last: it is the admission that automation has no legitimate path —
  which, per FINAL REPORT §G (consent gates, keystore, indicators), is the
  correct answer for a large class of requests, not a failure.

## Worked Zara examples across levels (same intent, different levels)

Intent "silence the lab test app's notifications for the demo":

- L1: app exposes a mute action → call it. (NOT YET MEASURED for that app.)
- L2: DO sets the notification policy / suspends the package. (DEVICE-proven class.)
- L3: not applicable (no priv-app notification primitive held) → skip honestly.
- L4: NMS internals — disproportionate; refused without a written gap.
- L5: `gui.tap` through the app's settings screens. (Spec; NOT YET MEASURED.)
- L6: card with exact steps; human taps. (Always available.)
- L7: "please mute it yourself." (Always available.)

Intent "read the code shown in the authenticator app":

- L1–L4: no API, no delegation, no privileged read, service internals out
  of scope → all honestly unavailable.
- L5: REFUSED — secure-flagged/authenticator content is exactly what the
  redaction rule (UICONTROL §3) and SENSITIVE class exist to protect.
- L6/L7: human reads it and (if they choose) tells Zara. Consent gates are
  never bypassed (FINAL REPORT §G).
