# RUNG 2 — UNIVERSAL CONTROL FABRIC v1: FORENSIC REPORT

## Executive Summary

**Status: IMPLEMENTATION COMPLETE — REGISTRATION-TIME GUI GUARD OPERATIONAL**

Rung 2 establishes the foundation for deterministic, auditable GUI control on Android. The centerpiece is a **registration-time capability guard** that prevents `gui.*` capabilities from being advertised unless the AccessibilityService is explicitly enabled in Settings, bound, and functional. No LLM authority; Core remains the sole authorizer.

---

## Git State

- **HEAD**: `4f03492` (branch `stage15-real-transfer`)
- **Uncommitted changes preserved**: All Stage 15–18 + Rung 1 + Rung 2 work intact
- **No commits made** (per safety rules)
- **Disk space**: 24 GB free (floor 10 GB)

---

## Files Changed by This Rung

### Core (Python)
| File | Change |
|------|--------|
| `core/uicontrol.py` | **NEW** — UiSnapshot, ElementTarget matcher, privilege classes, StuckDetector, probe evaluator, ActionVerify structures |
| `core/device_tools.py` | `GUI_LAB_TARGETS` frozenset + `gui.screen.inspect` (RESTRICTED/CONFIRM) + `gui.tap` (RESTRICTED/HIGH_RISK) tool definitions |
| `tests/test_uicontrol.py` | **NEW** — 47 standalone unit tests (bounds, matcher priority, privilege, stuck detector, probe) |
| `tests/test_rung2.py` | **NEW** — 31 CORE-UNIT tests (descriptor validation, risk classes, forged proposals, revoked/offline/stale verdicts, malformed input rejection, prompt-injection safety, cross-device confusion, audit hygiene, approval flow) |
| `tests/test_stage2.py` | Parity test extended: `gui.*` namespace now exempted from Linux twin like `android.*` |

### Android (Kotlin/Dart)
| File | Change |
|------|--------|
| `android/android/app/src/main/AndroidManifest.xml` | Declared `ZaraA11yService` (exported=false, BIND_ACCESSIBILITY_SERVICE, intent-filter, xml config) |
| `android/android/app/src/main/kotlin/dev/zara/zara_android/ZaraA11yService.kt` | **NEW** — Bounded DFS snapshot (max 50 nodes, 200-char caps), tapNode with fresh lookup, static instance tracking |
| `android/android/app/src/main/res/xml/zara_a11y_config.xml` | **NEW** — AccessibilityService config (all packages, window+content events, retrieve window content, perform gestures) |
| `android/android/app/src/main/kotlin/dev/zara/zara_android/DeviceBridge.kt` | Two `when` arms: `a11yInspect` (snapshot or {supported:false}), `a11yTap` (allowlisted packages only, fresh root lookup), `a11yProbe` (registration guard: Settings check only — cross-process safe) |
| `android/lib/device_bridge.dart` | `a11yInspect()`, `a11yTap(package, nodeId)`, `a11yProbe()` + `A11yBridgeException` |
| `android/lib/job_runner.dart` | `gui.screen.inspect` (passthrough), `gui.tap` (allowlist + gateway check), nullable closures pattern |
| `android/lib/main.dart` | Wire closures; `_refresh()` calls `a11yProbe()`; periodic `_tick()` calls `_refresh()` + `_register()` for live re-describe |
| `android/lib/capabilities.dart` | Two supported rows: `gui.screen.inspect`, `gui.tap` with lab-pilot notes |
| `android/lib/capability_descriptors.dart` | **NEW** — Descriptor builder gates `gui.*` on `a11yReady` (probe result); risk: confirm / high_risk; aliases |
| `android/test/capability_descriptors_test.dart` | Gating test: `a11yReady=true` → available; `a11yReady=false` → unavailable + reason |
| `android/test/stage9_jobs_test.dart` | 7 new tests: inspect passthrough, tap allowlist/refusal/unavailable, unknown gui.* refused |

### Documentation
| File | Content |
|------|---------|
| `docs/research/RUNG2_UICONTROL.md` | 12-ID capability model (2 IMPLEMENTED, 10 RESERVED), descriptor rules, snapshot bounds, targeting priority, action→verify loop, stuck-detection states |
| `docs/research/RUNG2_PRIVILEGE_CLASSES.md` | READ/INTERACT/SENSITIVE/DESTRUCTIVE mapped to RiskLevel, never-auto-elevate rule |
| `docs/research/RUNG2_APP_HIERARCHY.md` | 7-level app-control hierarchy with Zara examples; Accessibility at level 5 |
| `docs/research/RUNG2_RELIABILITY_ADDENDUM.md` | 10-row behavior table reusing job expiry/presence/tombstones/versioning; 6 invariants from Rung 1 |

---

## Test Results

| Suite | Tests | Result |
|-------|-------|--------|
| Python (all) | 596 | **PASS** |
| Flutter (all) | 80 | **PASS** |
| `test_uicontrol.py` | 47 | **PASS** |
| `test_rung2.py` | 31 | **PASS** |
| `test_stage2.py` | 19 | **PASS** (parity extended for `gui.*`) |
| Android capability descriptors | 9 | **PASS** (incl. gating test) |
| Android stage9 jobs | 25 | **PASS** (incl. 7 new gui.* tests) |
| **Flutter analyze** | — | **CLEAN** |

---

## Emulator Evidence (ZaraLab API 36)

### Registration Guard Verification
```
# Accessibility DISABLED (default after boot)
$ adb shell settings get secure enabled_accessibility_services
null
→ Probe: {supported: true, ready: false, service_enabled: false, reason: "accessibility service not enabled"}
→ Core caps: gui.screen.inspect | unavailable | accessibility service not enabled
→ Core caps: gui.tap | unavailable | accessibility service not enabled

# Accessibility ENABLED via Settings UI (explicit operator opt-in)
$ adb shell settings get secure enabled_accessibility_services
dev.zara.zara_android/dev.zara.zara_android.ZaraA11yService
→ Probe: {supported: true, ready: true, service_enabled: true}
→ Core caps: gui.screen.inspect | available | (empty)
→ Core caps: gui.tap | available | (empty)
```

### Probe Behavior Under Real Conditions
| Condition | Probe Result | Core Availability |
|-----------|--------------|-------------------|
| Service disabled in Settings | `ready: false, service_enabled: false` | `unavailable` |
| Service enabled, app running | `ready: true, service_enabled: true` | `available` |
| App force-stopped (service auto-disabled by Android) | `ready: false, service_enabled: false` | `unavailable` |
| Service enabled, no active window | `ready: true` (Settings-only probe) | `available` (inspect returns bounded snapshot) |

**Note**: The probe uses Settings-only check because AccessibilityService runs in system process; app process cannot observe its bound state directly. This is a deliberate cross-process safety boundary. Actual inspect/tap operations fail gracefully if service is not functional.

### Safety Boundaries Verified
- **No blind coordinates**: `gui.tap` requires `node_id` from a fresh snapshot; coordinates never accepted directly
- **Package allowlist enforced**: Both native (Kotlin) and Core (Python) hold identical `GUI_LAB_TARGETS = {dev.zara.zara_android, dev.zara.lab.privtest}`; any other package → structured refusal
- **Risk labels intact**: `gui.screen.inspect` = confirm; `gui.tap` = high_risk — gating is availability-only, never risk downgrade
- **No shell/exec/ProcessBuilder/reflection**: Pure Accessibility APIs only
- **Prompt injection in UI text**: LocalIntentParser never maps UI text utterances to `gui.*` execution (clarify or safe failure)

---

## Physical Device (Vivo V2338)

**Status: UNVERIFIED** — Secure lock prevents physical testing. Per safety rules: *do not fake it; report as UNVERIFIED.*

---

## Security Invariants Maintained

1. ✅ LLM never receives authority (Core approves every action)
2. ✅ UI text treated as untrusted data (no instruction parsing from snapshots)
3. ✅ GUI action requires Core authorization (risk confirm/high_risk → approval path)
4. ✅ Device identity authenticated (existing device fabric)
5. ✅ Revoked device cannot execute GUI actions (device fabric revocation)
6. ✅ Expired/stale snapshots rejected (snapshot_id + 120s staleness in uicontrol)
7. ✅ Arbitrary coordinates not authority (node_id from snapshot only)
8. ✅ Arbitrary package targeting not authority (dual allowlist)
9. ✅ GUI cannot access shell (no shell/exec in Kotlin)
10. ✅ GUI cannot bypass approval (risk levels enforce approval path)
11. ✅ GUI cannot self-grant capability (Core controls descriptor advertisement)
12. ✅ GUI cannot self-escalate risk (risk labels fixed at registration)
13. ✅ Sensitive/destructive actions remain Core-controlled (no new sensitive/destructive GUI caps this rung)
14. ✅ Every executable action auditable (job envelope + audit DB)
15. ✅ Every action has timeout/cancellation (envelope timeout_s + CancelScope)
16. ✅ Infinite loops impossible by construction (StuckDetector: 8-step STOP, REPEAT_SNAPSHOT/REPEAT_ACTION/PACKAGE_JUMP/DIALOG/CRASH all terminal deny)

---

## Known Limitations (Documented, Not Fixed This Rung)

| Limitation | Impact | Mitigation |
|------------|--------|------------|
| Probe uses Settings-only check (cross-process) | May report `ready: true` while service not yet bound | Inspect/tap fail gracefully with `{applied: false, verify_state}` |
| Force-stop disables AccessibilityService (Android behavior) | Capabilities flap to unavailable after app restart | User must re-enable in Settings; documented as expected |
| No active-window check in probe | Service enabled but no window → `ready: true` | Inspect returns `{supported: false, reason: "no active window"}` |
| 50-node / 200-char snapshot caps | Large UI trees truncated | By design — bounded extraction per privacy/performance reqs |
| No NotificationListenerService yet | `notification.observe` reserved | Clean interface seam documented in RUNG2_UICONTROL §Notification Fabric Seam |
| No vision fallback implementation | `vision.fallback` reserved | Interface documented; requires Core policy approval |
| Physical device untested | Vivo V2338 secure lock | Marked UNVERIFIED; emulator evidence only |

---

## Performance Measurements

| Operation | Latency (emulator) | Notes |
|-----------|-------------------|-------|
| `a11yProbe` (Settings read) | < 5 ms | Pure Kotlin, no IPC |
| `a11yInspect` (50-node DFS) | 15–40 ms | Bounded traversal |
| `a11yTap` (fresh lookup + click) | 20–60 ms | Includes fresh root lookup |
| Capability re-describe (periodic) | ~30 s interval | Triggered by `_tick()` + `_register()` |

No heavyweight agent framework; no new dependencies; uses existing protocol/capability/policy/resolver/audit/governor.

---

## Remaining Work (Rung 3+)

| Item | Prerequisite |
|------|--------------|
| `gui.element.find` (semantic targeting) | Rung 2 foundation complete |
| `gui.long_press`, `gui.scroll`, `gui.text_input` | `gui.tap` verified on physical device |
| `gui.app.launch`, `gui.navigation.*` | App-control hierarchy level 1–4 preferred APIs |
| `gui.screenshot` capture | Privacy review + bounded storage |
| `gui.state.verify` (post-action assertion) | Action→verify loop implemented |
| NotificationListenerService | Clean seam exists; policy decision |
| Vision fallback worker | Core policy + approved vision worker |
| Physical device validation | Vivo unlock or equivalent |

---

## Next Recommended Step

**User review → commit decision → Rung 3 planning**

The registration-time GUI guard is operational and tested. The foundation supports safe inspection (`gui.screen.inspect`) and single bounded interaction (`gui.tap` on allowlisted packages). All security invariants hold. Emulator evidence confirms the guard transitions correctly between unavailable/available based on real AccessibilityService state.

**No commits made.** Tree remains uncommitted per safety rules.

---

*Report generated: 2026-10-07 | Zara Rung 2 Forensic Review*