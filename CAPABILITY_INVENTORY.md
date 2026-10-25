# Zara Capability Inventory

**Branch:** stage15-real-transfer | **HEAD:** a56a260 + uncommitted plan/contract/window work | **Disk:** 15 GB free

### New in this unit of work (uncommitted, tested)

| Capability | State |
|------------|-------|
| Multi-step deterministic plan execution (`IntentTurnService._run_plan` + MissionEngine tracking) | Implemented + tested (`test_intent_plans.py` 7: all-safe, unresolvable-stop, single-attempt, stateless, cancel, approval-park) |
| UI snapshot/action contract v1 (`docs/UI_SNAPSHOT_CONTRACT.md`: `contract_version`, structural `snapshot_id`, tap guards, integer `node_id`) | Implemented across Core schema + Dart runner/bridge + Kotlin service (compiles); tested (`stage9_jobs_test.dart` contract group, `test_rung2.py` U9) |
| `desktop.window.list` / `desktop.window.action` (X11, verified, Wayland-honest) | Implemented + tested (`test_desktop.py` 20); live exercise needs an X11 session |
| Mission-bound single-approval messaging (`core/messaging.py`, `/v1/messaging/*`, NL routing) | Implemented + tested (`test_messaging.py` 17, `test_messaging_workflow.py` 31 — SIMULATED); live send needs a messaging app on the emulator |
| **File workflows** on the Linux agent: `filesystem.search` (bounded read-only name search), `filesystem.edit` (one unambiguous substring replace: backup-first, atomic temp+rename, VERIFIED by re-read, never overwrites an existing .bak), `filesystem.move` (scratch rename: conflict-safe, confirmed-overwrite, verified by re-check) | Implemented + tested (`test_file_workflows.py` 19: real local filesystem; refuses out-of-scope/sensitive/symlink/directory; honest unreadable-dir counts; verification-failure surfaces, never faked) — same device boundaries as the pre-existing scratch tools |
| **Browser workflows**: `browser.links` (untrusted structured elements, `#id` selectors only for provably safe ids), `browser.search` (term-filtered extraction), `browser.form` (field inspection; secret-typed values masked, never returned), `browser.verify_fill` (re-reads a field to confirm a fill), `browser.download` (bounded 5 MB cap, content-length pre-check, no overwrite, scratch downloads dir, byte-count verified) | Implemented + tested (`test_browser_workflows.py` 8: real system Chrome against loopback pages — not simulated, but not a public-web session) |
| `filesystem.extract` — structured read of supported documents (txt/md head+stats, json top-level shape, csv header+row count+preview); unsupported formats and broken JSON reported as failures, capped at 200 KB, same HOME/tmp + sensitive-name scope | Implemented + tested (`test_file_workflows.py` 21; refusal paths included) |
| NL routing for the new capabilities (aliases + quoted-parameter extraction in `core/intent.py`) | Implemented + tested (`test_nl_workflow_routing.py` 10); clarify messages carried via existing convention |
| **Messaging approval listing** `GET /v1/messaging/approvals` (redacted pending rows, bounded, oldest-first) + companion-app **Messaging approvals panel** (`android/lib/messaging_review.dart`, Tasks tab: Approve/Cancel per bound approval, never plaintext/fingerprint; approve is the same one-time mission-bound decision) | Implemented + tested (`test_messaging_workflow.py` 32 incl. redaction + one-time listing; Flutter 393/393, analyze clean) |

---

## 1. IMPLEMENTED (Production-Ready, Physically Verified on Vivo V2338)

### Android Capabilities (DeviceBridge + native Kotlin)
| Capability | Mechanism | Permission | Risk | Verified |
|------------|-----------|------------|------|----------|
| `notification.receive` | FCM + poll fallback | FCM token | SAFE | ✅ |
| `notification.display` | Local NotificationManager | POST_NOTIFICATIONS | SAFE | ✅ |
| `battery.report` | BatteryManager sticky intent | BATTERY_STATS | SAFE | ✅ |
| `network.report` | ConnectivityManager | ACCESS_NETWORK_STATE | SAFE | ✅ |
| `system.battery` | DeviceBridge.battery() → Core proxy | system.battery | SAFE | ✅ (34%→33%) |
| `system.network` | DeviceBridge.network() → Core proxy | system.network | SAFE | ✅ |
| `permissions.report` | PackageManager runtime state | — | SAFE | ✅ |
| `android.lifecycle` | Pair/restore/reconnect/revoke | — | SAFE | ✅ |
| `audio.capture.api` | AudioRecord 16kHz/16-bit mono WAV | RECORD_AUDIO | CONFIRM | ✅ (peak dBFS) |
| `audio.playback.api` | AudioTrack USAGE_MEDIA | — | CONFIRM | ✅ (heard) |
| `push.mock` | Poll fallback (reliable) | — | SAFE | ✅ |
| `voice.input` | Capture→STT→Core turn | RECORD_AUDIO | CONFIRM | ✅ (live STT) |
| `voice.output` | Piper TTS→AudioTrack→heard | — | CONFIRM | ✅ (heard) |
| `assistant.role` | ASSIST proxy + RoleManager | — | SAFE | ✅ (session NOT) |
| `files.transfer` | Core relay chunked bytes | files.transfer | CONFIRM | ✅ (3↑/4↓) |

### Lab-Only Android Capabilities (Emulator/Priv-App Only)
| Capability | Mechanism | Permission | Risk | Device |
|------------|-----------|------------|------|--------|
| `android.app.force_stop` | ActivityManager.forceStopPackage | FORCE_STOP_PACKAGES | HIGH_RISK | Lab/Emulator only |
| `gui.screen.inspect` | AccessibilityService snapshot | BIND_ACCESSIBILITY_SERVICE | CONFIRM | Lab/Emulator |
| `gui.tap` | AccessibilityService tap node; contract v3: semantic `target` resolution at action time (`target_matched` evidence) | BIND_ACCESSIBILITY_SERVICE | HIGH_RISK | Lab/Emulator — LIVE 2026-10-18: semantic tab-switch tap verified `clicked` + `target_matched` (nodes 36/33), conflict/secret-shape/not-found refusals live |
| `gui.find` | AccessibilityService read-only semantic lookup (resource_id/content_desc/text/role + index_hint against the FRESH tree; contract v3 §9) | BIND_ACCESSIBILITY_SERVICE | CONFIRM | LIVE 2026-10-18 (ZaraLab, real core chain): exact cd MATCH node 13 w/ bounds; role+hints resolve; AMBIGUOUS (3 and 10 candidates) and NOT_FOUND honest refusals; UNEXPECTED_PACKAGE guard; through approval path (0 dispatch before) |

### Linux Capabilities (tools_linux.py)
| Capability | Mechanism | Scope | Risk | Verified |
|------------|-----------|-------|------|----------|
| `system.battery` | /sys/class/power_supply/BAT* | HOME+/tmp | SAFE | ✅ |
| `system.network` | /sys/class/net/* operstate | — | SAFE | ✅ |
| `system.processes.read` | ps -eo pid,comm,etime,pcpu,pmem | — | SAFE | ✅ |
| `filesystem.list` | os.listdir (HOME/tmp scoped) | HOME+/tmp | RESTRICTED | ✅ |
| `filesystem.read` | os.open O_NOFOLLOW capped | HOME+/tmp | RESTRICTED | ✅ |
| `filesystem.exists` | scoped check | HOME+/tmp | RESTRICTED | ✅ |
| `filesystem.metadata` | os.stat | HOME+/tmp | RESTRICTED | ✅ |
| `filesystem.write` | O_NOFOLLOW scratch dir | ~/zara-scratch | CONFIRM | ✅ |
| `filesystem.delete` | unlink(dir_fd) scratch dir | ~/zara-scratch | CONFIRM | ✅ |
| `filesystem.metadata` | os.stat scoped | HOME+/tmp | RESTRICTED | ✅ |
| `shell.safe_readonly` | allowlisted binaries (ls,df,free,git...) | no shell metachars | CONFIRM | ✅ |

---

## 2. PARTIALLY IMPLEMENTED

| Capability | Current State | Missing |
|------------|---------------|---------|
| `system.battery` time-to-full | Linux estimator + Core def + Android null-estimate guard implemented, unit-tested (Python) | Flutter tests + physical validation pending |
| `system.wifi/bluetooth/hotspot/mobile_data/airplane_mode` states + settings + `system.app.open` | Core defs + intent aliases + Linux impl + Android bridge/runner/capabilities wired; Python suites green | `flutter test` done (348/348 full suite, analyze clean); physical-device validation pending |
| `system.*.set` writes | Core defs (CONFIRM/HIGH_RISK) + intent direction extraction + Linux impl (nmcli/bluetoothctl/profile-gated hotspot/mmcli/rfkill, all verified) + Android tiered bridge (legacy-direct / consent-dialog / ADB-grant-direct / companion-or-settings) + companion skeleton + setup script; Python suites green (`test_syscontrol.py` 14) | `flutter test` done (321/321 incl. Phase-2 setter group); physical-device validation pending. LIVE emulator: airplane.set on+off applied:true (T1 grant), hotspot.set off+on+off applied:true (T3 companion), wifi/bt state reads verified |
| `desktop.session` / `desktop.window.active` / `desktop.screenshot` | Core defs (Linux-only) + Linux impl (env detect / xdotool X11 / backend-probed capped PNG to scratch) + intent aliases; Python suites green (`test_desktop.py`) | Actuation (click/type) intentionally absent — needs uinput group + foreground guarantees; Wayland active-window needs a foreign-toplevel helper |
| `desktop.a11y.inspect` / `desktop.click` / `desktop.type` | AT-SPI2 one-shot helper (`device/linux/atspi_probe.py`, live-verified read path on GNOME Wayland) + snapshot-bound actions (TTL, role/name re-bind, moved-target refusal) + tiered backends (a11y-action/edit → X11 fallback → honest Wayland refusal) + content/state verification + unmatched-filter diagnostics; Python suites green (`test_a11y.py` 18) | `flutter`-n/a (Python-only surface); live click/type exercise needs a lab session, not this dev host; `uicontrol.py` remains a tested validation library with no runtime caller (see gap note) |
| Device fleet UX (discover/enroll/revoke) | Server contracts pre-existing; Android Settings fleet panel (`fleetDevices`/`enrollDevice`/`revokeDevice` + claim-error mapping with TTL hints) wired; Linux agent stops after 3 consecutive 403s with re-claim instructions; onboarding workflow tests green (`test_onboarding.py` 10, incl. revoked fast-reject on `/v1/exec` + `/v1/executions`) | `flutter test` done (fleet_panel 10/10, full 348/348); physical-device UX validation pending. LIVE emulator: real-app UI pairing (uiautomator), revoke→403→fleet cleared, re-pair repair, revoked-submit fast 409 (new gate), stale-snapshot approval denial + retry success |
| Privileged companion contract | Operation allowlist + caller/signature checks + schemas + `applied`-only-if-observed verification + ~6 s poll bound documented (`android/privileged/PROTOCOL.md`); probe advertises version/operations; setup script verifies priv-app placement + documents rollback. LIVE 2026-10-13 (ZaraLab API 36 userdebug): APK built, installed, setTether disable+enable both applied:true verified; allowlist-boot-loop + authority-string + TetheringManager lessons baked in | Physical-phone deployment (out of scope by design); Play-store-image compatibility |
| `browser.*` | Pre-existing Core Playwright tools verified green; launch falls back system Chrome → bundled headless shell → honest error; screenshot contract hardened (no silent truncation); suites green (`test_browser_tools.py` 12) | Physical-UX validation only |
| Wake-word detection (ONNX contract) | `OnnxWakeSpotter` (validated tensor contract, cons-frames, cooldown) + `WakeListenSession` (one persistent arecord stream, sequential windows, bounded retries, generation-gated events, prompt kill, error parking) + `capture_snippet_alsa`/`wake_listen_once` (model-before-mic); Android model-status probe (`wake_models/` presence, `ready` never client-upgraded) + specific start errors via pure `describeWakeModelError`; persisted toggle/lifecycle unchanged and governing; tests green (`test_wake_onnx.py` 26 incl. live ALSA-error path with zero recording, `wake_optin_test.dart` 27, full Flutter 363) | NO "Hey Zara" model (vendor training needs user consent — nothing submitted/downloaded); real detection unavailable; Android has no engine/loop by footprint+license decision (foreground-service route documented only); no live mic runs (room privacy); no measured power/latency data |
| Android background-resume prompt (opt-in) | `ZaraBootReceiver` + `RECEIVE_BOOT_COMPLETED` + Settings toggle (default off) + bridge marker + jittered register backoff + revoked/sign-out halt (both timers cancelled, re-pair required) + foreground-resume gating (online/degraded only); notifies "tap to resume" only, never polls/listens/records; Dart tests green (`bg_recovery_test.dart` 9, `stage10` resume/revoke group) | No unattended recovery: no foreground service / WorkManager by evaluated design (FGS needs persistent notification + battery exemption yet still killed by Vivo/stock background-start limits; WorkManager is deferrable-only with quotas — neither provides continuous low-latency agent operation); no live reboot test; no mic at boot by design |
| `browser.screenshot` contract | `_encode_screenshot`: PNG-magic check + raw/transport caps enforced pre-encode, explicit errors with recovery guidance, never truncated output; regression tests green (`test_browser_tools.py` boundary cases) | Physical-UX validation only |
| Capability `supported_devices` metadata | Linux-only (filesystem/process/shell) and Android-only (force-stop/gui) defs corrected; table-driven compat test green (`test_device_compat.py`) | None (runtime behavior unchanged; resolver already used advertised caps) |
| Scheduler fire trigger + restart recovery + authorized dispatch | `on_fire` hook (bus publish + audit) + `ZARA_SCHEDULER_DB` persistence (new `scheduled_jobs` table, no migration); restart re-arms future timers, announces due work once, honors persisted cancels; jobs carrying an `exec` payload are offered to the NORMAL `engine.submit_async_idem` path with a `sched-{job}` idempotency key (fire-once + restart-safe, no second scheduler); policy re-decided at submit, CONFIRM/HIGH_RISK park as WAITING_FOR_PERMISSION (time is never approval), rejections audited as `dispatch_rejected`; plain timers stay announce-only; tests green (`test_scheduler.py` 31: S20–S24, S30–S34, S40–S47) | Overdue consequential actions still require their normal approval after restart (parked, never auto-run); revoked-target records stay unverified/unresulted until the normal proxy path settles them |
| Voice turn-generation guard + loud failures | Overlapping turns/interrupts invalidate in-flight speech (`superseded` flag), partial-marker preserved, self-transition no-op; STT / loop-LLM / TTS / resume-approval failures all return `ok: False` with transcript preserved and no invented reply; local faster-whisper tiny.en validated live (cached model, 1 s audio in ~5 s CPU, honest empty on pure tone); Tamil tiny.en refusal + Tamil Edge-TTS mapping (`ta-IN-PallaviNeural`) + unmapped-language refusal tested; audio bodies capped (2 MiB per voice endpoint, 5 MiB global `ZARA_MAX_BODY_BYTES`); no raw audio/transcript logging; tests green (`test_voice_pipeline.py` 17 + voice suites) | Acoustic Tamil STT needs a multilingual whisper model (absent, not downloaded); local Piper TTS needs a voice model (absent); always-on wake DSP absent by design; mic/speaker hardware round-trip pending (no safe harness run) |
| Memory retention lifecycle | `pinned` retention + `set_pinned` (+`POST /v1/memory/{id}/pin`), `ZARA_MEMORY_DEFAULT_TTL_DAYS` (default off), opt-in throttled auto-sweep (`ZARA_MEMORY_AUTO_SWEEP`, default off); tests green (`test_retention.py` 8) | No auto-sweep by default (existing behavior byte-identical unless opted in); no global retention-auto-delete policy invented |
| `assistant.role` session path | RoleManager.isRoleHeld=true but sessionService NOT_SUPPORTED on OriginOS | Session path not implemented |
| `files.transfer` | Core relay works | ✅ Complete |

---

## 3. MISSING (To Implement)

### 3.1 Android System-Control Capabilities — STATUS: implemented per tiers (see `docs/SYSCONTROL_MATRIX.md`)

Direct-change support on ordinary builds: airplane mode (T1 ADB grant only),
Bluetooth disable attempt + enable consent dialog (T2), Wi-Fi legacy direct
(API <29 only), hotspot via T3 lab companion only, mobile data settings-UI
only. State reads + settings intents + `system.app.open` + `system.*.set`
contracts all wired Core/Linux/Android (2026-10-13, uncommitted).
Remaining: `flutter test`, physical validation, companion APK build/install.

| Capability | Mechanism Required | Android API | Permission | Notes |
|------------|-------------------|-------------|------------|-------|
| `system.wifi.enable` (direct) | WifiManager.setWifiEnabled() | android.net.wifi | CHANGE_WIFI_STATE | API <29 only; API 29+ always returns false → settings UI + verify |
| `system.bluetooth.enable` (direct) | BluetoothAdapter.enable() | android.bluetooth | BLUETOOTH_CONNECT | Deprecated path; consent dialog (T2) is the supported route |
| `system.airplane_mode.enable` (direct) | Settings.Global.putInt() + broadcast | android.provider.Settings | WRITE_SECURE_SETTINGS | T1 ADB grant (`adb shell pm grant`); settings UI fallback |
| `system.hotspot.enable` (direct) | Companion provider `setTether` | hidden tether APIs | TETHER_PRIVILEGED | T3 lab priv-app only; settings UI fallback |

### 3.2 Linux Desktop/Browser Control Capabilities — STATUS: reads done, actuation blocked

| Capability | Mechanism | Notes |
|------------|-----------|-------|
| `desktop.session` | env detect (XDG_SESSION_TYPE/WAYLAND_DISPLAY/DISPLAY) | ✅ Implemented + tested |
| `desktop.window.active` | X11: xdotool | ✅ Implemented + tested; Wayland honestly unsupported (needs foreign-toplevel helper) |
| `desktop.screenshot` | grim/gnome-screenshot/import/scrot/maim probe, capped PNG to scratch | ✅ Implemented + tested (mocked); this host has no backend → reports unsupported here |
| `desktop.click/type/scroll/key` | xdotool/ydotool | BLOCKED: needs uinput group + foreground guarantees; intentionally not implemented |
| `desktop.a11y.inspect` | AT-SPI2 via atspi-dbus | ✅ Implemented + tested (`test_a11y.py` 20: shaping, snapshot binding, stale/moved/mismatch refusal, Wayland refusal, X11 fallback, content verification, timeout + missing-injector edges); read path live-verified on GNOME Wayland |
| `desktop.window.action` | wmctrl/xdotool (focus, graceful close, minimize), verified by re-read (close=absent, focus=active-match), Wayland refuses honestly; HIGH_RISK + CONFIRM | Implemented + tested (`test_desktop.py` 20) — X11 lab session still needed for live exercise |
| `desktop.apps.list` / `desktop.app.open` | Real .desktop enumeration (XDG dirs, capped, no Exec values, symlink-refused) + gtk-launch (detached, no shell) with X11 window-appear verification; Wayland reports `requested` honestly; app_id must come from the enumerated set | Implemented + tested (`test_universal_workflows.py` 7: real-FS parse, cap/symlink, refusals, X11 verified + Wayland requested + unavailable-launcher; NL reach via `desktop apps`/`launch desktop app`); live app-launch needs a windowed session (host is Wayland-only) |
| `android.apps.list` / `gui.back` (universal operator) | Launchable-app discovery (PM queryIntentActivities, capped, metadata-only) + guarded global back/home via the a11y service (UNEXPECTED_PACKAGE refuses before any navigation, fresh post-action snapshot returned); `system.app.open` now returns device-side focus verification (`focused_verified` / honest `focus_unconfirmed`); discovery is never an authorization | Implemented + unit-tested (Core chain: `test_universal_workflows.py` 8; Dart contracts: `universal_operator_test.dart` 12 + descriptor tests) + **LIVE on emulator 2026-10-17** (7 real apps listed; Settings open `focused_verified`; `home` navigation observed with fresh snapshot; mixed-device NL mission `apps on my phone then battery on my laptop` → 2 steps, 2 devices, both ok) |
| `android.notifications.read` (passive listener read) | Reads notifications captured by `ZaraNotifListener` (`NotificationListenerService`, NO polling — the OS pushes entries into a bounded in-process ring, max 20 entries, per-field caps, scrubbed AT CAPTURE with the secret patterns; buffer is per-process, never persisted). Access honestly gated on the user's Notification-listener grant read live from `enabled_notification_listeners` — never opened/asked silently; refusal states the missing grant. RESTRICTED/CONFIRM observation class like screen inspect: parks for approval, zero dispatch before it. Result shape: `{granted, count, truncated, items[{posted_at_ms, package, title, text, category}], note}` | Implemented + unit-tested (Core chain `test_universal_workflows.py` N1–N4: contract honesty, stub rows pass-through, missing-grant honest failure, NL routing; Dart contracts `universal_operator_test.dart` 5 incl. bounds clamp 1–50, unexpected-shape refusal) + **LIVE on emulator 2026-10-18** (listener granted via lab `cmd notification allow_listener`; post (`cmd notification post`) → captured → tool returned the real notification row verified:true; secret-shaped posted content came back `[REDACTED]` with zero leak; pre-grant run refused honestly with no content invented) |
| `gui.set_text` / `gui.scroll` (contract v2) | Native snapshot-bound set-text (content-verified, never echoed) + scroll (fresh post-scroll id); Dart runner + Core defs + intent aliases; refusal evidence merges end-to-end | Implemented + unit-tested (Dart 46, `test_messaging.py` 17); LIVE: scroll applied + NOT_EDITABLE/NOT_SCROLLABLE/STALE refusals on ZaraLab; **semantic set_text-with-target LIVE 2026-10-18** (Settings-tab EditText through the full chain: value-changing writes unicode/Tamil + whitespace/newline → applied + text_matched + independent read-back; bounded single grace re-read fix after an honest-lag finding; refusal battery NOT_FOUND/AMBIGUOUS/NOT_EDITABLE/CONFLICT/INSECURE/UNEXPECTED_PACKAGE + operator Deny all live) |
| Messaging workflow contract (`core/messaging.py`: discovery, plan, verify, ledger) | Pure contract over v2 primitives; simulated fakes; plans stop/park on fail/approval/offline | Implemented + tested (SIMULATED); live send + single-approval UX pending |
| Mission-bound single-approval messaging (`MessagingWorkflow` + `MessagingApprovalStore` + durable `SendLedger`) | ONE approval for the whole HIGH_RISK sequence (open conversation → compose → send → verify); binds mission + recipient + exact-content fingerprint + device + action skeleton; invalidates on material change/tamper; fresh-snapshot resolution at execution (request snapshot optional — so the initial read is covered by the SAME approval); scoped `policy.grant` per bound action through the unmodified fabric/engine gates; cancel/expiry/denial/disconnect/stale/missing-button/unverifiable handled; ledger records only an APPLIED send (never a blind resend) and survives restarts via `ZARA_MESSAGING_DB`; HTTP `/v1/messaging/send` + approve/cancel/status; natural-language routing via the `messaging.send` pseudo-capability (`send "<text>" to "<contact>"`, `message|tell|text "<contact>" [saying] "<text>"`) | ✅ Implemented + tested (`test_messaging_workflow.py` 31, `test_messaging.py` 17, SIMULATED; control test proves direct HIGH_RISK submits still park); live send needs a messaging app (none installed) |
| `browser.*` | Playwright (system Chrome → bundled headless shell → honest error) | ✅ Pre-existing tools green; launch fallback added 2026-10-13 |

### 3.3 Cross-Device Execution (Fabric) — STATUS: routing infrastructure complete, UX pending

Verified 2026-10-13: strict resolver pipeline (`core/resolver.py:431`
`resolve`: exists→schema→device→auth→trust→presence→support→availability→
constraints→policy→governor→approval→rank) + `fabric.route_capability` +
`execute_on_device` + JobQueue proxy + agent poll/claim/execute are fully
generic — new `system.*.set`/`desktop.*` caps route with zero per-tool
wiring (276 routing/fabric/device/session focused tests green). Remaining
is UX surface, not machinery:

| Capability | Current State | Missing |
|------------|---------------|---------|
| Phone → Laptop execution | Fabric route/execute + agent poll works (verified live: `shell.safe_readonly`) | Per-device discovery UI |
| Laptop → Phone execution | Same machinery (Core→Android job queue) | Verified-result display polish |
| Capability-aware routing | Resolver + FabricRegistry integrated | Nothing structural |
| Results/errors/cancellation return | JobQueue + `complete_once()` + proxy | Nothing structural |
| Device capability discovery | Describe protocol + re-describe on change | Fleet panel wired (validation pending) |
| Multi-device onboarding | Pairing + enrollment protocol exists | Enroll UI + claim-error TTL hints wired (validation pending) |

---

## 4. BLOCKED BY PLATFORM LIMITATION

| Capability | Blocker | Mitigation |
|------------|---------|------------|
| `system.wifi.enable` on Android 10+ | WifiManager.setWifiEnabled deprecated; requires Settings UI | Open WiFi settings Intent |
| `system.bluetooth.enable` on Android 10+ | BluetoothAdapter.enable deprecated | Open Bluetooth settings Intent |
| `system.hotspot.enable` | WifiManager.setWifiApEnabled hidden | Open Hotspot settings Intent |
| `system.mobile_data.enable` | Requires WRITE_SECURE_SETTINGS (system app) | Open Mobile Data settings Intent |
| `system.airplane_mode.enable` | Requires WRITE_SECURE_SETTINGS (system app) | Open Airplane Mode settings Intent |
| `desktop.click/type` on Wayland | Requires uinput/ydotool + user in input group | Document prerequisites |
| `core/uicontrol.py` (Android snapshot/matcher library) | Tested validation library (86 unit tests) with no runtime caller: desktop actions go through AT-SPI helper + `tools_linux.py`, Android actuation through the a11y-service bridge — nothing routes through `uicontrol`. No safe hook exists without inventing a new execution path, so it stays unwired by design | Do not wire without a dedicated approved execution path + approval re-check |
| `system.battery.time_to_full` | Not exposed by BatteryManager | Estimate from charging history |

---

## 5. IMPLEMENTATION PRIORITY

### Phase 1: Android System-Control (Week 1) — STATUS: code wired, validation pending
1. `system.wifi.state`, `system.bluetooth.state`, `system.hotspot.state`, `system.mobile_data.state`, `system.airplane_mode.state` (read-only) — DONE (Core/Linux/Android bridge+runner)
2. `system.settings.open` for WiFi/Bluetooth/Hotspot/MobileData/AirplaneMode — DONE (Settings-intent only, user-confirmed)
3. `system.app.open` for known packages — DONE (allowlist mirrored Core/Android, package-visibility queries added)
4. Charging-time estimation to `system.battery` — DONE (Linux estimator; Android returns honest null estimate)
5. REMAINING: `flutter test` (esp. `stage9_jobs_test.dart` Phase 1 group) + physical-device validation; intent regression added (`tests/test_intent.py` Phase 1 group, green). Also fixed: "mobile data" no longer misparsed as a device mention.

### Phase 2: Linux Desktop/Browser (Week 1-2) — STATUS: reads + a11y interaction done 2026-10-13
1. Wayland/X11 detection + active window — DONE (`desktop.session`, `desktop.window.active`)
2. Screenshot (bounded, scratch-dir, PNG-verified) — DONE (`desktop.screenshot`)
3. AT-SPI2 accessibility inspection — DONE (`desktop.a11y.inspect`: targeted, bounded, snapshot-bound; live-verified read path)
4. Click/type — DONE where backends exist (`desktop.click`/`desktop.type`: a11y-action/edit → X11 fallback → honest Wayland refusal; stale/moved refusal; content/state verification). Raw uinput injection still BLOCKED (perms + no injector binary)
5. Playwright browser tools — DONE pre-existing, launch fallback added (system Chrome → bundled shell → honest error)

### Phase 3: Cross-Device Fabric (Week 2) — STATUS: machinery complete, fleet UX wired, validation pending
Routing/resolver/execute/result-propagation are generic and green (see §3.3).
Android Settings fleet panel (discover/enroll with TTL display/revoke with
sweep counts) + claim-error mapping + Linux agent stop-on-revoked landed;
workflow tests green (`test_onboarding.py` 8). Remaining: `flutter test`,
physical-device UX validation.

### Phase 4: Multi-Device Onboarding (Week 2)
1. Device list UI in Settings
3. Capability discovery + sync
4. Revocation UI

### Phase 5: Voice/Testing Improvements (Week 2)
1. Pairing code TTL extension for testing (config flag)
2. Anti-promissory response guard (already done)
3. Physical voice diagnostic test

---

## 6. ARCHITECTURE DECISIONS

### Android Privileged Operations
- **Ordinary app permissions**: Used where available (read-only state)
- **Settings UI fallback**: For write operations blocked on Android 10+ (Intents to Settings)
- **Privileged APK**: Only for `android.app.force_stop` (FORCE_STOP_PACKAGES) — lab/emulator only
- **Device Owner / Managed Device**: NOT implemented (requires factory reset + QR code enrollment)
- **System APK / Platform signature**: NOT implemented (requires custom ROM/OEM build)
- **Root/Custom ROM**: Explicitly NOT supported — user must opt-in explicitly

### Linux Desktop Access
- **Wayland**: Use `ydotool` (uinput) + `wlr-screencopy` for screenshots, AT-SPI2 for a11y
- **X11**: Use `xdotool` + `xwd`/`import`, AT-SPI2
- **Browser**: Playwright (chromium/firefox) — no custom automation

### Cross-Device Execution
- Uses existing Fabric: DeviceManager + FabricRegistry + JobQueue + ExecutionEngine
- Phone→Laptop: Android agent enqueues job on Core; Linux agent polls + executes
- Laptop→Phone: Core enqueues job for Android device; Android agent polls + executes
- Results flow back via JobQueue wait_for_result + proxy handler

### Security Model
- No arbitrary shell execution — all tools allowlisted
- Filesystem scoped to HOME+/tmp + scratch dir (TOCTOU-safe)
- PolicyEngine + Governor + approvals gate every execution
- No Device Owner / System APK / Root unless explicitly user-authorized
- Pairing codes single-use, short TTL, ADB reverse only (no LAN exposure)