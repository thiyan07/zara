# Privileged Android Zara — Final Integration Report

Status: RESEARCH + EMULATOR POC COMPLETE. No repo implementation changed
(Stages 15–19 untouched). One new doc (this file). No commit. Vivo never
touched.

Evidence tiers used below: **DEVICE** = measured on the ZaraLab emulator
(API 36, `userdebug`, this session) · **DOC** = official Android/AOSP
documentation · **REPO** = read from this repository · **INFERENCE** =
reasoned synthesis, marked as such.

---

## A. What a privileged Zara app actually gives us (DEVICE + DOC)

Proven on-device this session (self-signed lab key — no platform key):

1. **Placement is honored**: APK in `/system/priv-app` → `PRIVILEGED` in
   `privateFlags`/`pkgFlags` (dumpsys), UID stays app-range (`u0_a217` —
   sandbox intact).
2. **Allowlisted `signature|privileged` permissions are actually granted**:
   `BATTERY_STATS` + `FORCE_STOP_PACKAGES` → `granted=true` by both app
   self-report (`checkSelfPermission`) and `dumpsys package`.
3. **SELinux domain upgrades**: process runs as `u:r:priv_app:s0`, not
   `untrusted_app` (observed `ps -Z`).
4. **Missing allowlist entry is fatal, not silent**: without the XML entry,
   `system_server` boot-crashes (`IllegalStateException … not in
   privileged permission allowlist`) in a crash loop — on `userdebug`,
   contrary to "warning-only" folklore. This also proves placement was
   recognized (the check ran against our package).

On-device permission taxonomy (API 36, `pm list permissions -f` — DEVICE):
priv-app-holdable (`signature|privileged` flag present):
`FORCE_STOP_PACKAGES`, `INSTALL_PACKAGES`, `DELETE_PACKAGES` (+`role`),
`TETHER_PRIVILEGED`, `BLUETOOTH_PRIVILEGED`, `MANAGE_USB`,
`BATTERY_STATS` (+`development`), `CAPTURE_AUDIO_OUTPUT` (+`role`),
`MODIFY_AUDIO_ROUTING` (+`role`), `STATUS_BAR` (+`recents`),
`MANAGE_VOICE_KEYPHRASES`, `CAPTURE_AUDIO_HOTWORD` (+`role`),
`WRITE_SECURE_SETTINGS` (`signature|privileged|development|installer|role`).
NOT priv-app-holdable (no `privileged` flag):
`INJECT_EVENTS` (`signature`), `GRANT_RUNTIME_PERMISSIONS`
(`signature|installer|verifier`), `DEVICE_POWER` (`signature|role`),
`READ_FRAME_BUFFER` (`signature|recents`), `MANAGE_HOTWORD_DETECTION`
(`internal|preinstalled`), `BIND_NOTIFICATION_LISTENER_SERVICE`
(`signature`, system-binds-only).

Net: a priv-app Zara honestly gains — force-stop, silent install/remove
(permission held; installer-bypass behavior version-gated, verify per
flow), secure-settings writes, battery-stats/USB/BT/status-bar privileged
ops, voice-keyphrase + hotword-capture holdings, factory-reset persistence,
image-time default grants — all still individually gated by Core policy.

## B. What it does NOT give us (DEVICE + DOC)

- No runtime-permission self-grant (`GRANT_RUNTIME_PERMISSIONS` lacks the
  `privileged` flag — DOC adjudication of research P0-3).
- No input injection, no silent screenshots, no tethering enable, no
  power-key control, no device-power verbs (all pure-`signature` or
  role/internal-gated).
- No UID change (still `u0_aXXX`), no sandbox exit, no SELinux escape
  (confined `priv_app` domain).
- No bypass of user-consent gates: dangerous runtime permissions, NLS
  binding, MediaProjection dialog, assistant-role selection all still
  require the user.
- No quiet background mic/camera (AppOps + FGS + indicators apply to all
  app tiers); no radio toggles; no arbitrary file escape.
- The allowlist itself is a boot-fatal tripwire: one missing entry
  crash-loops `system_server` (observed). Privilege is load-bearing
  image configuration, not a flag.

## C. What Device Owner gives us (DEVICE)

Proven on-device this session (test DPC, zero-signature lab key):
`dpm set-device-owner` succeeded with no accounts; `isDeviceOwnerApp=true`;
`setPermissionGrantState(RECORD_AUDIO → GRANTED)` verified by API
read-back (`1`) AND `dumpsys` (`granted=true, flags=[POLICY_FIXED|…]`) —
no dialog, no signature. (First attempt returned `0` because the
permission wasn't in our manifest — the API silently no-ops on
undeclared permissions; caution documented.)

DO therefore buys, with zero image changes: silent managed
install/uninstall delegation, permission-grant state + auto-grant policy,
suspend/hide, lock-task/kiosk, lockNow/wipe/reboot, restrictions, update
and VPN policy. It buys NO UI inspection/injection, file escape, silent
capture, secure-settings free-write, or force-stop. Provisioning is
setup-time (fresh emulator needed no wipe; a daily driver would).
Removal friction observed: `dpm remove-active-admin` refuses non-test
admins (`SecurityException`) — revocation effectively means reset; test
DO left on the disposable lab emulator, documented as revocation-friction
evidence, not a defect.

## D. What a custom system service gives us (DOC — not built)

Direct manager internals (AMS/WMS/PMS/AudioService/PowerManagerService/
NMS), silent screenshots via SurfaceFlinger, input injection,
persistent scheduler/compositor/HAL policy — the only tier that escapes
consent gates by design. Price, all DOC-sourced: framework surgery
(`SystemServer.startOtherServices`, AIDL, `service_contexts`, `.te`
policy), per-method `enforceCallingPermission` (missing = world-callable
confused deputy), own platform keys + AVB signing, own OTA stream with
per-bulletin rebases, Play Integrity failure by design. NOT attempted:
requires a full AOSP sync/build (tens of GB, hours) — explicitly deferred
per mission bounds; exact POC plan in §I.

## E. What root/custom ROM gives us (DOC)

UID-0 shell paths (`pm/am/settings/input/screencap`, DAC bypass) on an
existing build, still SELinux-confined unless policy rewritten; durable
guarantees (indicator suppression, persistent policy, pre-granted
listeners) require a custom OS, not just root. Operationally useful on
the bench; shippable to nothing; every assistant bug becomes a root bug.
Not used, not recommended beyond bench validation.

## F. Limitations actually reduced (by tier)

- Reliable deploy/update + permission auto-grant + kiosk/policy → DO,
  no image work (proven).
- Force-stop, silent install/remove, secure-settings writes, battery
  stats, USB functions, privileged BT, status-bar collapse, voice-
  keyphrase/hotword-capture holdings → priv-app + allowlist on an owned
  image (grant mechanics proven; installer-bypass per-flow verification
  still open).
- Silent screenshots, input injection, tethering enable, power-key
  prevention, device-power verbs → platform signature on owned image
  (taxonomy-verified, not yet exercised — needs own platform keys).
- Direct service internals + persistent policy → custom system service /
  custom OS only (planned, not built).

## G. Limitations that remain fundamentally unsolved

- Keystore/StrongBox extraction, FBE-at-rest access, Gatekeeper/Weaver
  bypass: no tier grants these (by design).
- User-consent gates (assistant role selection, NLS binding, runtime
  dialogs, MediaProjection per-session consent, mic/camera indicators):
  reducible only via owned-image pre-grants/roles, never removable for a
  store-distributed app.
- Background mic/camera without indicators, silent radio toggles,
  arbitrary private-file access: no legitimate app-tier path; custom-OS
  paths are surveillance-adjacent and out of scope.
- Play Integrity/attestation on any custom image: fails by design.
- Cross-app UI automation without user-enabled Accessibility: no sanctioned
  path below platform signature; Zara's by-design refusal stands.

## H. Recommended architecture

```
                    ZARA CORE  (authority — unchanged)
                        |  device key + grant-bound dispatch
                Authenticated IPC (existing X-Device headers / jobs)
                        |
          +-------------+-------------+
          |  Normal app body (today)  |  Privileged adapters (later,
          |  15 caps + pull jobs      |  one narrow capability ID each)
          +-------------+-------------+
                        |
              Policy Gateway (Core decide — unchanged)
              Resource Manager (governor — unchanged)
              Android Adapter (per-capability native call)
                        |
              Privileged APIs (manifest + allowlist + user consent)
                        |
              Android Framework / Kernel
```

Rules: the app never becomes the authority; Core stays the authority;
the privileged surface is narrow capability IDs with bounded params
(T9 pattern); every privileged addition flows describe→resolver→policy→
governor→approval→job→audit with zero new gates invented. Rung order:
Rung 0 = today + DO for lab/fleet management (no image work). Rung 1 =
single priv-app + one custom permission on an owned emulator image
(validated mechanics this session). Rung 2+ (system service) = gated on
a written per-primitive gap + approved AOSP build cost.

## I. Minimum viable proof-of-concept (status)

| # | POC item | Status |
|---|---|---|
| 1 | APK in system-image location | DEVICE-proven (`/system/priv-app`, flag set) |
| 2 | Permissions declared + allowlisted | DEVICE-proven (crash without entry, granted with entry) |
| 3 | App ↔ controlled Zara component comms | DEVICE-proven (DPC activity ↔ DPM; job transport already proven Stages 9–15) |
| 4–5 | Binder interface + Core auth to it | NOT BUILT — needs AOSP service; exact plan: `frameworks/base/services/core/.../zara/`, AIDL, `SystemServer.startOtherServices`, `service_contexts`, one `signature\|privileged` custom permission enforced per-method, client via `@SystemApi` accessor; negative test = unprivileged caller denied |
| 6–10 | Policy-decides / no-shell / audit / denials / LLM-proof | ARCHITECTURE-SPECIFIED (conformance checklist in research doc §6); enforceable today for Rung 0/1 via existing Core chain |

Artifacts (all outside the repo, disposable): `/tmp/opencode/poc/`
(172 KB: two signed lab APKs + allowlist XML + build notes),
`google_apis` API-36 userdebug image (4.3 GB), `ZaraLab` AVD (left
booted for morning verification).

## J. Risks

Boot-fatal allowlist (observed), OTA-fork burden, key-custody burden,
Play Integrity loss, permission creep (mitigated: CI manifest diff,
per-release re-certification), confused deputy (mitigated: Binder caller
checks, `exported=false`), compromised-Core blast radius (mitigated:
OS consent + on-device auth + forensic audit, never zero), approval
fatigue (mitigated: concrete effect display, no bulk approve), supply
chain (mitigated: offline keys, reproducible builds, cert re-verify on
boot/update). Residuals explicitly accepted in research §5.

## K. Compatibility consequences

Custom image ⇒ no stock OTAs (own OTA stream + rebase per bulletin);
fails Play Integrity (banking/DRM apps refuse); `test-keys` lab-only;
Assistant-role/hotword behavior re-verified per API level; hidden-API
use remains tech debt. Lab images and the user's daily driver must
never be the same device (Vivo untouched throughout — verified: no
`adb` target other than emulators ever connected).

## L. Where to implement

Emulator first (done: ZaraLab userdebug AVD, reproducible). AOSP build
only after written Rung-1 gap approval (cost: tens of GB + hours).
Dedicated lab device (a spare Pixel-class device with unlockable
bootloader, owned keys) before any thought of production. Existing
Vivo: NEVER — no unlock/root/flash/wipe, no privileged components
installed; it stays the store-distributed-app validation target.

---

## Verification appendix (primary-agent adjudication)

**P0 disputes resolved by on-device `pm list permissions -f` (API 36):**
P0-1 `INSTALL_PACKAGES`=`signature|privileged` → capability matrix
correct (priv-app+allowlist can hold; silent-install flow still needs
per-flow verification). P0-2 `FORCE_STOP_PACKAGES`=`signature|privileged`
→ capability matrix correct. P0-3 `GRANT_RUNTIME_PERMISSIONS`=
`signature|installer|verifier` (no `privileged` flag) → privilege
matrix correct (priv-app cannot self-serve grants). P1-1 `STATUS_BAR`=
`signature|privileged|recents` → capability matrix correct. P0-4
(Rung-1 voice justification): revised — keyphrase/hotword-capture
holdings ARE priv-app-reachable (measured), but background-mic/
indicators/DSP-service gates remain; Rung 1 justified primarily by the
secure-settings/stats/USB/BT/install/force-stop class, voice secondarily.

**F1 (GUI rung):** independently confirmed in `core/resolver.py` —
`level_of()` cannot return GUI and nothing refuses `gui.*`
registration; non-execution holds by absence + policy, not by block.
Required precondition (not implemented now, per Stage 15–19 preservation):
a registration-time guard before any UI-automation work.

**F2:** confirmed — blocked capabilities resolve to
`policy_blocked`/`governor_blocked`/`unavailable`/`unauthorized`/
`offline`, not `REQUIRES_ESCALATION` (which is `no_capability`-only).

**Repo-reference spot-checks:** all Core endpoint/handler citations
verified resolving; three P2 citation nits recorded (transfer.py:1-19
is header not relay; 409 mapping lives in app.py not execution.py;
"13-step" pipeline is ~6 check stages).

**Official-docs cross-check:** an independent verification pass against
developer.android.com/source.android.com/AOSP manifest confirmed the
allowlist mechanism, INJECT_EVENTS/READ_FRAME_BUFFER levels (with the
`signature|recents` nuance for the latter), WRITE_SECURE_SETTINGS flag
set, dpm preconditions, MediaProjection/NLS consent gates, permission-
policy ownership scoping, accessibility screenshot/gesture APIs,
sharedUserId deprecation, and hidden-API enforcement — with the
correction that hidden-API exemption keys off platform signature, not
priv-app location.

## Tests run

Full Python suite: **481 passed** (no repo code changed by this task, so
baseline holds; rerun clean post-reboot environment). Flutter: not
re-run (zero `android/` changes — mission-gated). POC tests are
emulator-side shell/logcat evidence above, not repo tests (no test
files added or weakened).

## Physical tests

Vivo: never connected for this task (only emulators on `adb`);
no modification, no verification claimed. Emulator evidence labeled
DEVICE throughout; anything not measured is labeled UNKNOWN, never
upgraded.

## Files changed / committed

- Added: `docs/research/` (7 files: 6 subagent research + this report).
- Modified: nothing else. Stages 15–19 deltas intact and untouched.
- POC artifacts live outside the repo (`/tmp/opencode/poc/`, SDK image,
  emulator AVDs).
- **Committed: nothing.** HEAD remains `4f03492` on
  `stage15-real-transfer`; entire tree uncommitted for review.
