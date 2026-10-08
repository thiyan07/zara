# Rung-1 Scope Review — Prove ONE Safe Privileged Capability End-to-End

- Date (UTC): 2026-10-06
- Scope: READ-ONLY review. No code changed, no device work.
- Goal under review: prove exactly ONE privileged capability end-to-end on a lab target: `android.app.force_stop` (FORCE_STOP_PACKAGES, `signature|privileged`) via the existing chain describe → resolver → policy → governor → approval → job → audit.
- Sources: `docs/research/PRIVILEGED_ANDROID_FINAL_REPORT.md` (Rung 0/1/2, POC table §I, verification appendix), `docs/research/PRIVILEGED_CAPABILITY_MATRIX.md` (§2 row: force-stop AVAILABLE-if-allowlisted for priv-app; §7 API-36 correction), `docs/research/ZARA_SYSTEM_ARCHITECTURE.md` (§4 Rung 0/1 definitions), `core/resolver.py` (escalation ladder, never-executable GUI/HUMAN rungs), `android/lib/capabilities.dart` (15 supported + `gui.*` absent-by-design).

## Ruling principle

Rung 1 = ONE priv-app placement + ONE allowlisted permission (FORCE_STOP_PACKAGES) + ONE capability ID exercised through ZERO new gates on an owned emulator image (Final Report §H, §I items 1–3 + 6–10; Architecture §4). Anything that adds a second permission, a second capability, a new service, a new transport/auth/UI/voice/MCP path, or any Rung-2 framework surgery is out.

Risk note: `force-stop` is NOT `safe`-risk in Core terms (destructive-adjacent; resolver forbids fallback for `high_risk`, `core/resolver.py:143-146`). "Safe" in the mission means lab-bounded + approval-gated + audited, single test package, emulator only — not risk=`safe`. Do not downgrade its risk label to sneak it past policy.

## CUT / KEEP decisions

| # | Candidate scope item | Verdict | One-line justification |
|---|---|---|---|
| 1 | Second permission (INSTALL_PACKAGES, DELETE_PACKAGES, WRITE_SECURE_SETTINGS, BATTERY_STATS, MANAGE_USB, BLUETOOTH_PRIVILEGED, STATUS_BAR, GRANT_RUNTIME_PERMISSIONS) | CUT | Goal needs exactly one allowlist entry (FORCE_STOP_PACKAGES); each extra entry is another boot-fatal tripwire (Final Report §A.4) with zero proof value. |
| 2 | GRANT_RUNTIME_PERMISSIONS specifically | CUT | API-36 measured `signature\|installer\|verifier`, NO `privileged` flag — priv-app cannot hold it (Matrix §7); including it guarantees failure. |
| 3 | Silent install / uninstall proof alongside force-stop | CUT | Separate POC with separate permission + installer-bypass per-flow verification still open (Final Report §A, §F); second execution path, not needed for force-stop. |
| 4 | Custom system service (`SystemServer.startOtherServices`, AIDL, `service_contexts`, `.te` policy) | CUT | Explicitly Rung 2, POC items 4–5 NOT BUILT (Final Report §I); building it converts Rung 1 into the full framework-surgery + OTA-fork cost (Architecture §5). |
| 5 | AIDL / Binder interface work of any kind | CUT | Same as #4: Rung-2 surface with confused-deputy risk per-method (Final Report §D, §J); force-stop needs only `ActivityManager.forceStopPackage`, no new Binder endpoint. |
| 6 | New auth system for the privileged path / Core-auth-to-service | CUT | POC items 4–5 deferred; existing device-key + grant-bound dispatch + X-Device headers already proven (Final Report §H, §I.3); zero new gates invented per architecture rules. |
| 7 | Second execution path (shell/`am force-stop`, root, `su`, DPC-as-executor) | CUT | Proves the shell, not the priv-app grant; success criterion is app self-report + `dumpsys granted=true` via allowlist (Final Report §A.2), never UID-0. |
| 8 | Device Owner provisioning as part of Rung-1 proof | CUT | Rung 0 already DEVICE-proven (Final Report §C); DO buys no force-stop (Matrix §2 force-stop row: DO = PARTIAL, no true API); re-proving it adds setup-wipe friction for nothing. |
| 9 | FCM / push work (new transport, server-side FCM, notification-action path) | CUT | Job transport already DEVICE-proven Stages 9–15 (Final Report §I.3); FCM changes no privilege fact and risks scope into notification-consent gates (Final Report §B). |
| 10 | MCP server changes / new MCP capability | CUT | Escalation-ladder rung 2 (`core/resolver.py:79-80`) is orthogonal to NATIVE priv-app grant; adds Core-authorized surface with no bearing on `FORCE_STOP_PACKAGES`. |
| 11 | Voice work (wake-word, hotword DSP, keyphrase, Tamil/ASR/TTS, audio routing) | CUT | Voice-hotword holdings are secondary justification at best (Final Report verification appendix P0-4); background-mic/indicator/DSP gates remain unsolved at every app tier (Matrix §2 mic/camera rows). |
| 12 | Tamil parser / NLU / LLM-prompt work | CUT | Resolver is deterministic, no-LLM by contract (`core/resolver.py:1-28`); golden-prompt work cannot change a `signature\|privileged` grant fact. |
| 13 | UI work (new screens, settings toggles, approval-UX redesign, SystemUI) | CUT | No UI needed: one forced package + existing approval display + audit read-back; STATUS_BAR/SystemUI is a different permission and partial at best (Matrix §2). |
| 14 | Background goals (always-on mic, persistent scheduler, Doze exemption, FGS policy) | CUT | No app tier below system service escapes background/AppOps/Doze gates (Matrix §3–4 FACT); persistent-policy work is Rung-2/custom-OS by definition. |
| 15 | Autonomous goals / fallback chains for force-stop | CUT | Destructive-adjacent capability must never fail over (resolver `high_risk` rule, `core/resolver.py:143-146`); autonomy + approval-fatigue mitigations (Final Report §J) argue for narrower, not wider. |
| 16 | Accessibility service as force-stop vehicle | CUT | Matrix marks it PARTIAL-brittle UI automation (click Settings → Force-Stop); proves brittleness, contradicts durable-grant goal (§5: durable = priv-app, not gestures). |
| 17 | Screenshot / screen-capture / MediaProjection / NLS additions | CUT | Consent-gated at all app tiers, pure-`signature` for silent path (Matrix §2; Final Report §B); unrelated primitive, own consent UX, own Rung-2 escalation. |
| 18 | Input injection (INJECT_EVENTS) / tethering / power-key / DEVICE_POWER | CUT | All pure-`signature`, priv-app-holdable = false by on-device taxonomy (Final Report §A); each demands platform keys = Rung-2+ cost for zero force-stop evidence. |
| 19 | New SELinux domain / `.te` policy / `sharedUserId=system` | CUT | Explicitly deferred past Rung 1 (Architecture §4: "no new SELinux domain, no sharedUserId=system"); confined `priv_app` domain already DEVICE-proven sufficient (Final Report §A.3). |
| 20 | OTA pipeline / release-key ceremony / production signing | CUT | Lab proof rides existing userdebug + lab keys (POC artifacts in `/tmp/opencode/poc/`); OTA-fork burden is the dominant long-term cost (Architecture §2.10) — incur only on ship decision. |
| 21 | GUI-rung execution (`gui.*` capabilities, screen interaction) | CUT | Represented-NEVER-executable by resolver design (`core/resolver.py:64`, EscalationLevel.GUI); Final Report §F1 requires registration guard first — out of scope for a NATIVE force-stop proof. |
| 22 | Touching Vivo / daily driver for any Rung-1 step | CUT | Hard boundary: Vivo stays store-distributed-app target, NEVER flashed/wiped/unlocked (Final Report §K, §L); lab emulator only. |
| 23 | Single priv-app placement + single FORCE_STOP_PACKAGES allowlist entry on owned emulator image | KEEP | The entire grant mechanism to prove; DEVICE-proven mechanics, boot-fatal without entry (Final Report §A.1–2, §A.4). |
| 24 | One narrow capability ID (`android.app.force_stop`-equivalent) through existing describe→resolver→policy→governor→approval→job→audit chain | KEEP | Architecture rule: narrow capability IDs, bounded params, zero new gates (Final Report §H); conformance checklist items 6–10 (Final Report §I). |
| 25 | Negative test (unallowlisted caller / missing entry denied or boot-checked; unprivileged caller denied) | KEEP | Only denial evidence that distinguishes grant from placebo; already the §I.4–5 plan pattern, applicable without building the service. |
| 26 | CI manifest diff + per-release re-certification note for the one permission | KEEP | Cheapest permission-creep mitigation named in risks (Final Report §J); one-line guard, not a feature. |
| 27 | `exported=false` + audit-log read-back of the single force-stop job | KEEP | Existing confused-deputy + forensic-audit mitigations (Final Report §J); verifies "Core stays authority," no new code implied. |

## Least-privilege / Rung-2-expansion flags

- FLAG (least privilege): any item 1–2 inclusion (extra `signature|privileged` permission "while we're here") violates least privilege directly — each widens blast radius if Core is compromised (Final Report §J) and adds a boot-fatal allowlist dependency.
- FLAG (least privilege): granting the force-stop capability to more than one test package, or with unbounded package-name params, breaks the T9 bounded-params rule (Final Report §H); pin to one lab target package.
- FLAG (Rung 2): items 4–6, 19 (system service, AIDL/Binder, custom auth-to-service, SELinux domain) are the custom-system-service tier by Final Report §D definition — approving any one silently approves the AOSP-sync/build + per-bulletin rebase + key-custody + Play-Integrity-loss package (§J, §K).
- FLAG (Rung 2): items 17–18 (silent screenshot, injection, tethering, power) require platform signature at minimum (Matrix §5), i.e. own platform keys ⇒ effectively custom OS signing — same gate as Rung 2.
- FLAG (scope masquerading as prerequisite): items 8–12 (DO redo, FCM, MCP, voice, Tamil/NLU) and 13–15 (UI, background, autonomy) are side workstreams that can each absorb the whole mission; none is on the grant path items 1–3 + 6–10 of the POC table.

## Minimal Rung-1 acceptance (no expansion)

1. APK in `/system/priv-app` on owned emulator image → `PRIVILEGED` flag in dumpsys.
2. Exactly one allowlist entry (FORCE_STOP_PACKAGES) → app self-report + `dumpsys package` both `granted=true`.
3. One `android.app.force_stop`-scoped job for one lab package flows through the unchanged Core chain with human approval and lands in audit.
4. Negative: unprivileged caller denied (documented read-back, not a new service).
5. Nothing else installed, granted, built, or redesigned; Vivo untouched.
