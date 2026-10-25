# Zara Acceptance Report — 2026-10-19

Scope: NL task execution, Linux + Android device integration, app control,
browser, voice + wake word, memory, auth/reconnect, reliability, UX.
Baseline: `stage15-real-transfer` @ `a56a2600` (unchanged; all work is
uncommitted working-tree changes, nothing cleaned/reset/committed).

## 1. Wake word — "Hey Zara"

| Item | Status | Evidence |
|---|---|---|
| Detection runtime on this laptop | **working** | live — `OpenWakeWordSpotter` streams mic audio through onnxruntime with exact openWakeWord reference semantics |
| Semantics parity with upstream reference | verified | unit — differential test: 331 streaming calls, 0 mismatches (≤5e-6 threshold) vs the real `openwakeword` library |
| Public model v1 in use | **live** | physical-device — v1 restored over the retrained v2 (v2 measured worse: FAR 16.4%→24.5%) |
| Real-take recall (recorded clips) | measured | live audio-file — 9/9 user "Hey Zara" takes fire at ≥0.99 score |
| Measured metrics | recorded | unit — `training/hey_zara_metrics.json`: n_pos 1009, recall@0.5 = 0.954; **no threshold met FP≤0.5/hr**, documented limitation |
| FAR at operating point | **weak, disclosed** | unit — "Sarah"-soundalike false accepts are elevated; false wakes only start a transcription turn (no consequential action) |
| Live-room continuous listening | **untested** | blocked on user participation/consent (user-gated; recorded-clip testing done) |
| Quality ceiling cause | documented | unit — ACAV100M background features (15 GB) not downloadable under the 10 GB disk floor |

## 2. Device challenge-response auth (Ed25519)

Protocol: device binds an Ed25519 pubkey at claim; every later call signs
the canonical message with a server-minted single-use nonce — **v2 binds
the exact request body**: `"{METHOD} {path} {nonce} {sha256_hex(body)}"`
(path without query string; the 3-field v1 form stays verified for
older credentials during rollout); raw key travels exactly once per
process lifetime (chained spare nonces); raw-key path retained as
fallback; mixing headers refused.

| Item | Status | Evidence |
|---|---|---|
| Server core (`core/device_auth.py`, `core/app.py`) | complete | unit — 27 tests: bound-store persistence across restart, single-use, TTL, per-device/global caps, tamper/wrong-key/revoked/expired fail closed, replay → 401 |
| Body binding (v2 canonical) | tested | unit — exact body bytes bound; tampered body → 403 and the nonce burns (fail-closed, no retry), legacy v1 compat still accepted, POST-shaped proof invalid for a GET (method binding) and the burned nonce unusable |
| Concurrent same-nonce race | tested | unit — two threads send the same signed request: exactly one 200, other 401/403 (single-use enforced under concurrency) |
| Linux agent signed transport | complete | live — real uvicorn socket run: claim binds key, register/heartbeat/describe/heartbeat-via-proof all 200 with raw key sabotaged (never travels); first-use 200, replay 401 |
| Agent fallback semantics | tested | unit — 12 tests (`tests/test_agent_challenge.py`, stubbed transport): 404/409 → one raw retry, 500 → never retried, unbound-key 409 → proofs disabled permanently, `.sk` written 0600 / removed when cryptography missing, v2 message verified against Core's verifier |
| Transport guard (Linux agent) | tested | unit — plain HTTP to a non-local core refused at construction; `ZARA_ALLOW_INSECURE_TRANSPORT=1` override stamps `[INSECURE-TRANSPORT-ALLOWED]` into software_version |
| Raw-key travel-after-bootstrap guarantee | proven | live — in-pytest (`test_agent_signed_transport_live`) with a real socket, ephemeral port |
| Android (Dart) client | complete | unit — vendored RFC 8032 signer in `android/lib/ed25519.dart` passes the official RFC vectors (3 pubkeys + signatures, from ed25519.cr.yp.to); SecureStore seed slot same discipline as the key; proof signatures now include the sha256 body digest |
| Android transport guard | tested | unit — Dart `CoreClient` refuses plain-HTTP construction to non-local hosts; `allowInsecureTransport` constructor opt-out (default false); loopback + `10.0.2.2` + https allowed |
| Android signed transport end-to-end | **live (emulator)** | physical-device (emulator) — fresh pair: claim with pubkey → `/v1/agent/challenge` fetches in the core log for every device call → dispatch → job poll → `/v1/agent/jobs/result` 200, all over proofs (that build signed the v1 canonical; v2 body-binding is unit-verified on the Dart signer path and live on the Python agent) |
| Old-core / unbound-credential fallback | implemented + unit-tested on both clients | unit (agent) / unit (Dart 409→disable, 404→fallback logic) |

## 2b. Transport TLS (production confidentiality)

Signatures protect identity, never wire confidentiality. HTTPS is now a
supported, tested configuration (docs/TRANSPORT_TLS.md):

| Item | Status | Evidence |
|---|---|---|
| Core serves HTTPS | **live** | real run — `zara_local.sh start --tls-cert/--tls-key` (uvicorn ssl_certfile/keyfile); ready probe over https |
| Default client verification fails closed | tested + live | unit + live — stock urllib/curl against a self-signed cert → certificate verification error, never a silent downgrade |
| Verified HTTPS with staged trust | tested + live | unit + live — trust the staged cert (+ hostname check ON) → 200; a WRONG trust anchor is also rejected |
| Linux agent over verified TLS end-to-end | tested | unit (real socket) — claim → signed register → heartbeat over https with `SSL_CERT_FILE` staged trust, raw key sabotaged, no insecure flags anywhere |
| Strict transport enforcement | tested | unit — `ZARA_REQUIRE_HTTPS=1`: non-loopback source plain HTTP → 403 with doc pointer (this host's real LAN interface address); spoofed/proxied `x-forwarded-proto: http` → 403; loopback keeps the documented dev exemption; flag off by default |
| Launcher TLS flags | live | `--tls-cert/--tls-key [--tls-ca]` validated togetherness + readiness over https |


## 3. Android app

| Item | Status | Evidence |
|---|---|---|
| Dart suite | 422 passed | unit |
| `flutter analyze` | 0 new issues (12 pre-existing infos + 1 pre-existing warning) | unit |
| Pairing → register → heartbeat → capabilities | **live (emulator)** | physical-device — `zara:describe ok` with 40 capabilities accepted |
| Core restart reconnect | **live (emulator)** | physical-device — Keystore credentials survived, auto re-registered, no re-pair |
| Job round-trip (battery) | **live (emulator)** | physical-device — `succeeded`, `verified: true` |
| Kotlin/Manifest changes | built into the same APK | unit (Gradle `assembleDebug` succeeded) |

## 3b. Android v2 proof flow — live emulator re-run (2026-10-19)

Fresh AVD session (`ZaraLab`), fresh enrollment `emu-v2`, APK rebuilt from
the current Dart (v2 canonical + transport guard) — full cycle:

| Step | Result |
|---|---|
| enroll → app-pairing (adb-driven pinch UI) | claim `200`, **pubkey bound** (`device_auth.db` `paired=1`, 64-hex pubkey) |
| auto register + capabilities + heartbeat | all `200`, every call **preceded by `/v1/agent/challenge` 200** in the core log (proof mode); `device_key` appears **0 times** in the whole log |
| `system.battery` dispatch | phone-executed: `{battery_pct 100, charging false, power_save false}`, job `succeeded`, **verified: true** |
| app force-stop → relaunch | auto-reconnect on saved credentials, battery dispatch `succeeded/verified` — no re-pairing |
| Core stop → start (same db) | app reconnects over persistent enrollment; dispatch `succeeded/verified` |
| revocation (`/v1/agent/revoke`) | poll `403`, then challenge/register/heartbeat all `403 Forbidden` — **fails closed on every path** (disposable test enrollment) |
| memory live controls | add ✓ search ✓ **correct creates a superseding record with provenance (`supersedes: <old-id>`)** ✓ pin (`retained: true`) ✓ delete (+ search confirms gone) ✓ secret refusal `422` ✓ |

## 3c. Live mission trace (§ capabilities, core `18082`, real requests)

| Case | Live outcome |
|---|---|
| NL read-only device status ("what is my phone's battery level?") | **end-to-end executed**: "Battery is at 100.0%", routed to `emu-v2`, job verified |
| App discovery (NL) | honest clarification fallback when the local parser can't map the phrasing; direct `android.apps.list` contract unit-tested |
| Browser research read-only | `browser.open` + `browser.search` on a live local page: hits extracted and explicitly marked **`"untrusted": true`** — content treated as data, never instructions |
| Approval-required (airplane mode) | hold created — `waiting_for_permission`, approval reference returned, **nothing executed** |
| Approval denial | hold cancelled → `cancelled`, `result: null` — never ran |
| Cancellation boundary | cancelling an already-terminal job → `409 already terminal` (bounded honest semantics); hold-cancellation path verified above |
| Unsupported request ("book me a flight and pay") | honest non-execution message |
| Ambiguous request | clarification question, no execution |
| LLM provider failure | unreachable provider → **502 `{"detail": "provider network: unavailable"}`**, no stack/internal leak; intent turn survives (local parser still works) |
| Desktop apps discovery (Linux agent, Wayland session) | `desktop.apps.list` **succeeded** (real `.desktop` entries); `desktop.session` → `wayland` verified; `desktop.window.active` honestly **unsupported** (GNOME Eval/Introspect denials live-probed 2026-10-19) |
| Duplicate/delayed job delivery | unit — second poll under a claimed job does not re-dispatch; push lifecycle dedup (`tests/test_android_stage9.py`) |


## 4. Reliability / UX / memory / browser / app control

Carried over intact from prior stages (tested in the suite). No regressions:
Python **1632 passed, 1 skipped** (final full-suite regression after the
Android-v2/emulator session; this session added the proof-method-binding
test and the whole vocabulary of TLS/body-binding tests earlier; baseline
1616 → net).
Core restart, offline-key tracking, execution renames, ghost-deaths, etc.
unchanged. Flutter: **425 tests passed**, analyzer 0 errors/warnings
(11 informational notices, all in `test/universal_operator_test.dart`). Key rotation with restart survival:
`test_P9_rotation_survives_restart` (rebind pubkey, restart store, old
key invalid / new valid).

## 4b. Final-gap implementation run (2026-10-19)

Four genuinely missing/partly-stale items found by a fresh missing-feature
matrix; all implemented, tested, evidenced:

1. **Documented memory export (zara-memory-export-1)** — was missing.
   `MemoryStore.export()` (core/memory.py) + `GET /v1/memory/export`
   (dev-token auth): stable JSON shape `{format, exported_at, count,
   items[{id, text, category, source, confidence, importance, metadata,
   created_at, updated_at, expires_at}]}`. Derived embedding column
   excluded by design (forgotten items vanish from every later export);
   secrets never appear (never enter memory). Tests: shape + no-embedding,
   delete→restart→export truth, live endpoint incl. no-secret-material
   (`tests/test_memory_consent.py`, 22 passed).
2. **`assistant.status` diagnostics skill** — was missing (missions
   endpoints existed, no NL utterance answered "what are you working
   on"). Added to `BUILTIN_LOCAL` + 7 utterance patterns + read-only
   `_local_answer` branch: paired devices, active missions with state and
   step counts, recent terminal missions, waiting-approval pointer.
   Never executes and never grants (`executed: false` asserted, audit
   checked). Tests: 3 new (empty-state honesty, active+terminal display,
   no-execution) in `tests/test_intent.py` (70 passed).
3. **Wayland active-window limitation — live-verified precisely**. Probed
   the stock GNOME session (2026-10-19): `org.gnome.Shell.Eval` →
   `(false, '')` (unsafe-mode gate), `org.gnome.Shell.Introspect.
   GetWindows` → `AccessDenied`. The honest-fallback handler now names
   these concrete denials instead of a vague helper complaint
   (`device/linux/tools_linux.py`). No unsafe mode is ever enabled; a
   user-designed foreign-toplevel helper remains the only path.
4. **Laptop mic "digital silence" — re-tested, now UNBLOCKED (2026-10-19)**.
   `arecord -D default` (the route `record_backend()` selects) delivers
   real signal; full acoustic loopback verified: piper TTS → speakers →
   REAL laptop mic → faster-whisper → transcript containing "check the
   battery" (wake phrase heard "Hey Sarah"/"He's out" — the documented
   Zara confusable), and the REAL OWW "hey zara" spotter scored the same
   capture **0.9955 with exactly 1 confirmed detection** (cooldown).
   Repeatable evidence: new `scripts/voice_mic_probe.py` (JSON output
   with explicit labels: mic=real hardware, speech source=synthetic TTS
   via speakers; never claims human speech / live-room FAR / FAR rates).
   Raw `hw:X,Y` ALSA devices still record nothing — the working route is
   the system default Zara uses. `docs/VOICE_STATUS.md` updated with
   dated evidence; wake false-acceptance criteria unchanged.

## 4c. Stage 20 — universal connected-device awareness (2026-10-19)

New subsystem: **peripheral discovery + registry + storage/file ops**,
implemented in bounded batches with no hardware faking and no trust
escalation.

**Core (`core/discovery.py`, `core/storage_scan.py`)**

- `PeripheralRegistry`: detection-lifecycle registry for host-attached
  peripherals (USB storage/pendrives, phones, displays, audio, HID,
  cameras). Stable ids (`stable_id`) from durable identity fields
  (serial/fsuuid/connector) — replug keeps identity, transient path
  changes produce no duplicate; connect/disconnect/change events are
  de-duplicated and capped. **Detection never sets `trusted`.**
- Event payloads are privacy-conscious: pid/class/label only — no
  serials, no block-device or mount paths (unit-tested).
- `scan_tree`: bounded, cancellable scandir walk → per-category counts
  (explicit extension rules; unknowns reported as `other` with
  samples), sizes, symlinks never followed, unreadable/huge-dir
  accounting, caps reported honestly.

**Linux adapters (`device/linux/discovery_linux.py`)**

- `lsblk` (removable block devices with durable serial/uuid), `lsusb`
  (coarse classes, root-hubs filtered), `adb devices` (authorized and
  unauthorized phones distinguished), displays via **xrandr (X11) or
  the supported Mutter DisplayConfig D-Bus (`busctl --user`, Wayland —
  no unsafe mode)**, audio via `aplay/arecord`. Live-verified on this
  laptop: all five adapters ran; camera/Bluetooth-radio/eDP-1
  (1920×1080, current 60 Hz, preferred 144 Hz)/audio cards detected.
- Missing tools or denied interfaces are honest absence in meta, never
  failures.

**Agent-side storage/file ops (`device/linux/tools_linux.py`, 7 new
tools registered in the parity contract)**

- `storage.volumes` / `storage.capacity` / `storage.stats` (read-only;
  removable mounts only), `storage.copy` (capacity pre-check,
  conflict→ask by default, sha256 verify, partial file removed on
  verify failure), `storage.move` (copy+verify then delete source only
  after success), `storage.mkdir`, `storage.delete` (explicit
  `confirm:true` in the same call; refusing root deletion and
  oversized trees). Scope: HOME+/tmp+mounted removable media only;
  media only; sensitive-path refusal kept for standard roots. Unit +
  tmp-dir tests
  cover collision/auto-rename, insufficient-space (nothing written),
  interrupted/corrupt copy (no success claim, source preserved),
  cancellation, verification, delete confirmation.

**Core routing + UX**

- Builtins `peripheral.list`/`peripheral.describe` (core-local,
  read-only, ZARA_DISCOVERY_OFF for tests); REST `/v1/peripherals`,
  `/v1/peripherals/refresh`, `/v1/peripherals/events`; **bounded
  background poller** (`ZARA_PERIPHERAL_POLL_S`, default off).
- NL phrases routed ("what device did I just connect", "show
  connected devices", "what can I do with this device") with the
  detection-is-not-trust boundary stated in every answer; empty
  registry stays honest.
- Local answers never run discovery inline (deterministic; freshness
  comes from the poller/builtin/endpoint refresh).

**Android**

- Kotlin `DeviceBridge`: `observedDevices()` over **supported
  interfaces only** (StorageManager volumes + StatFs usage, no
  permission; UsbManager listing with `user_granted_access` reported,
  never silent-granted; DisplayManager external displays), plus
  permission-gated `mediaCounts()` (counts only; denied permission
  returns `permissions_needed`, never guessed numbers). Compiles
  clean (`compileDebugKotlin`).
- Dart: `android.observe` / `android.storage.stats` job cases
  (honest abstain + gateway-unavailable + counts paths, 4 unit tests),
  capability/descriptor registration, `DeviceObservedPanel` with
  class-aware action cards (unsupported actions never rendered;
  widget-tested), action press routes ONE utterance through the
  existing `/v1/talk` loop — no side channel, no self-authorization.
- Contract parity (`test_contract_parity_core_vs_linux`) extended:
  all new tool names, capabilities, risk levels, and schemas match on
  the registered side; long-tool timeout contract (>60 s poll gap)
  caught and fixed (stats 90 s, copy/move 120 s).

**Evidence this session**

- Python suite: **1676 passed / 1 skipped** full run (was 1638;
  +38 tests across discovery, linux adapters, storage ops, routing).
- Flutter: **433 passed** full run (was 425; +8), analyzer 11
  pre-existing infos, 0 errors/warnings; Kotlin gradle compile clean.
- Live: `/v1/peripherals/refresh` → 5 adapters OK, 4 real peripherals
  (all `trusted: false`), 4 connect events; public projection carries
  no serials/vid:pid (asserted live).
- Cross-device file transfer remains Core-mediated via the Stage-15
  relay (`core/transfer.py`) — no second transfer engine.

**Physical-hardware gaps (honest)**: real pendrive plug/unplug UX, a
real phone's USB/storage behavior via the new Android probes, and
HDMI hotplug events are device-connected validations (same gating as
the physical-device test plan; no device attached this session).

## 4d. Stage 21 — cross-device file-transfer missions + connected-display workflows (2026-10-19)

Stage 20 answered "what is connected"; Stage 21 answers "move data and
screens between the connected things, with Core as the only authority".
Everything below runs on the EXISTING mission engine, execution funnel,
policy engine, and audit — there is still **no second workflow or
transfer engine**; mission hops execute through `ExecutionEngine.submit`
(policy re-decided per hop), approvals park and resume through the
normal path, and bytes never ride tool envelopes (agent-side relay
tools bound by the Stage-15 relay ceilings).

### What was built

**Deterministic mission runner** (`core/transfer_missions.py`, wired in
`build_stack`, process-global handle `core/transfer_state.py`):
- `transfer.mission` (SAFE builtin; empty spec → mission goes WAITING
  with concrete questions, never guessed), `transfer.status` (SAFE),
  `transfer.cancel` (CONFIRM — approval-gated like every destructive
  control).
- Supported hops, each through the funnel: laptop→pendrive
  (`storage.copy`/`storage.move` per item), laptop→phone (one
  `storage.xfer_send` per item; local digest → chunked upload → server
  digest enforcement → bounded ack wait), phone→pendrive (the operator
  shares from the phone's Zara app; the laptop mission matches the
  sender+recipient*+filename relay records, then stages onto the drive
  with a re-verified `storage.xfer_fetch`).
- Truth semantics, enforced by tests: **partial multi-file missions end
  FAILED** with per-item evidence and "N/M verified" — never COMPLETED;
  ambiguous destinations (2+ writable mounts, unknown device labels)
  → WAITING with the options listed; failed copies never delete or
  modify the source; cross-device `move` (remote source delete) is
  refused as unverifiable; `delete_source_after` only legal with
  `move=true`; destination-capacity precheck records a checkpoint
  before any copy; a missing capacity reading pauses instead of
  guessing.
- Cancellation is checked between items and mirrored to in-flight
  relay transfers (`xfer.cancel`); disconnect mid-mission surfaces as
  the precise per-item failure — the mission never claims the
  un-verified items.
- Phone-share timeout ends in WAITING (resumable), not FAILED.
- Restart recovery: the spec is checkpointed on the mission model, so
  with `PersistentMissionEngine` (SQLite) a Core restart resumes from
  the checkpoint; killer meanwhile-sent relay transfers are matched by
  `metadata.mission_id`.
- REST: `POST/GET /v1/transfer-missions`,
  `GET /v1/transfer-missions/{id}`, `POST .../resume` (spec_patch),
  `POST .../cancel`.
- NL routing (deterministic aliases + hosted path): "copy these videos
  from my phone to the pendrive" → `transfer.mission`, "cancel the
  current transfer" → `transfer.cancel`, "transfer status" →
  `transfer.status`, "how much free space on the pendrive" →
  `storage.volumes`, "how many videos on the pendrive" →
  `storage.stats`.

**Agent-side relay tools** (`device/linux/tools_linux.py` +
core parity in `core/device_tools.py`, both `files.relay`/CONFIRM,
120 s timeout, ≤5 MiB per call): `storage.xfer_send`,
`storage.xfer_fetch` — registered, schema-identical on both sides
(tool-parity asserts in the test suite).

**Interactive-display workflows** (Section E, supported interfaces
only — no GNOME unsafe mode): new `display.preview_config` (SAFE,
read-only) and `display.set_config` (CONFIRM, `confirm=true` required)
agent tools over Mutter DisplayConfig / xrandr on X11.
`describe_config` infers the current layout (single/extended/
duplicated) from live geometry and states the hardware limits in
plain words (a mirrored laptop cannot gain touch input; screen
streaming is a separate portal/MediaProjection protocol; deprecated
`ChangeBacklight` is not used). Apply is verified by re-reading
geometry — a change that did not land is reported FAILED with the
before/after layouts, never claimed as success. The Mutter
`ApplyMonitorsConfig` path is designed but **not validated on a real
external display this session → it refuses rather than guesses**;
the X11 xrandr path is the supported one here (validated against real
Desktop live reads; applies run only with operator confirm — no live
layout change was executed in tests).

**Flutter side (Section D/C7–9)**: `TransferMissionPanel` on the
status tab renders the active mission from Core's `/v1/transfer-missions`
(state labels for executing/verifying/waiting/failed-partial/
completed/cancelled, the partial-truth error line, and a Cancel
action). Cancellation routes through the SAME /v1/talk pipeline as
every operator-initiated action — policy, CONFIRM approval, and audit
stay in Core; no REST shortcut from the panel. Also fixed a real
`setState`-after-dispose race in `messaging_review.dart` `_reload`
(mounted guard at entry) that widget tests exposed.

### Evidence (this session)

- Python: **1708 passed / 1 skipped** full suite (was 1676+1 at Stage-20;
  +19 in `tests/test_transfer_missions.py`: scripted-hop copy,
  move semantics, partial=FAILED with manifest, conflict-ask honored,
  ambiguity→WAITING→resume completes, no-writable-mount abstention,
  cancellation between items, laptop→phone delivered + oversize
  precise-refusal, phone-share timeout→WAITING, restart recovery via
  PersistentMissionEngine, progress+audit events, NL routing, builtin
  risk levels, agent/core tool parity, spec checkpoint persistence).
- Display: **13 passed** `tests/test_display_config.py` (fixture = REAL
  GetCurrentState capture from this laptop) incl. no-false-success on
  verification failure and backend refusal.
- Flutter: **439 passed** full suite (was 433; +6 mission-panel tests);
  analyzer: 11 infos, ALL pre-existing in `universal_operator_test.dart`
  (unchanged); **Kotlin untouched** this stage (no Android-native
  source changed → no recompile required).
- Live read-only laptop smokes (real only, nothing changed): mission
  endpoints create/list/get/resume/cancel on a live core; Mutter
  GetCurrentState parse → `single / eDP-1 1920x1080@60 builtin primary`;
  `preview_config('extend_right')` honestly refuses with "needs a
  second active display — 1 active"; apply without confirm refuses.

### Still hardware-gated (honest, never faked)

Real pendrive plug flows with the mission loop end-to-end, a real
phone's share-screen hop, real HDMI hotplug mid-mission, and applying
a real external-display layout (extend/duplicate) on actual hardware.
The Mutter apply path additionally waits for that same hardware
validation before it will execute at all.

## 4e. Stage 22 — bluetooth, audio routing, MTP/PTP, touch & streaming honesty (2026-10-19)

Stage 21 moved data between confirmed devices; Stage 22 closes the
remaining *connected-device capability* gaps that are feasible without
new dependencies, again on the existing engines only.

### What was built

**Bluetooth peripherals** — `device/linux/discovery_linux.py`
`_bluetooth_items()` (read-only `bluetoothctl devices` + per-device
`info`): paired/trusted/connected/blocked as honest booleans, and the
device's OWN advertised service record mapped to names
(audio-source/sink, a2dp, obex-object-push, pbap…), with
`service_use_inferred_only: true` — advertised UUIDs never imply a
completed handshake or permission to use a service; vendor UUIDs are
counted as `unidentified_service_uuids`. Live on this laptop: the real
paired vivo T3x 5G is listed with its advertised services (2 mapped, 1
vendor-unknown). Registry taxonomy extended
(`core/discovery.py` `DEVICE_CLASSES` + `"bluetooth"`); `bluetoothctl`
absence is honest meta, never a fake empty success.
- `bluetooth.devices` (SAFE), `bluetooth.connect` (CONFIRM,
  `mac`+`confirm` required): refuses unregistered MACs, non-paired
  devices (pairing needs an interactive agent — explicitly unsupported
  here), blocked devices; already-connected is a verified no-op; a
  connect is VERIFIED by re-reading the adapter state (`Connected:
  yes`) and an unverified attempt is reported failed, never success.

**MTP/PTP phones over USB (no adb)** — `_gio_mtp_items()` parses the
desktop's own GVFS layer (`gio mount -li`): a mount whose monitor or
URI identifies MTP (`GProxyVolumeMonitorMTP`, `mtp://`) or PTP
(`GProxyVolumeMonitorPTP`, `gphoto2://`) is recorded as class `mtp`
with `protocol_confirmed` from the OS's own identifier; plain
UDisks/unix mounts are never misclassified. When nothing MTP/PTP is
mounted, the meta says so — nothing is guessed from USB ids.
Transfers
over these mounts go through the OS gvfs layer and its permissions;
Core relay missions remain the cross-device path (unchanged).

**Audio output routing** — `device/linux/audio_linux.py` over
PipeWire's `wpctl` (the session's own interface; no new deps):
- `audio.sinks` (SAFE): parses `wpctl status` into devices/sinks/
  sources/streams with the default marker. Live: real sink 57
  (Ryzen analog) parsed as default.
- `audio.set_output` (CONFIRM, `sink_id`+`confirm`): refuses without
  confirm, refuses unknown ids (`nothing_changed`), refuses backend
  rejections, then `wpctl set-default` and VERIFIES by re-reading the
  default marker — a routing that did not land is failure with the
  before/after ids, never success.

**Touch-input reporting** — `display_linux._touch_inputs()` reads the
kernel's own `/proc/bus/input/devices` table: touchscreens/touchpads/
stylus-class devices listed; `external_touch_confirmed` is always
false — attributing a touch device to a specific display requires a
protocol handshake that does not exist, and the record says exactly
that. Folded into `describe_config`'s ledger.

**Screen-streaming honesty** — `display_linux.stream_status()` +
`display.stream_status` (SAFE): read-only probe for
`org.gnome.Mutter.ScreenCast` and the ScreenCast portal; live on this
laptop the Mutter interface is present. `supported_now` stays **false
with the exact limitation**: streaming needs a user-consented capture
session (Portals/MediaProjection) plus a PipeWire receiver workflow
that is not implemented — the operation is reported unavailable rather
than half-working, and files never need this path (relay missions).

**Network devices** — `_network_services_meta()`: mDNS scanner
(`avahi-browse`) presence is reported; it is absent on this host, and
Core deliberately does not port-scan, so LAN discovery is honestly
*unavailable on this host* (a supported, bounded mDNS adapter can be
added when avahi-tools is installed — no new dependency was bundled).

**Tool parity** — 5 new Linux tools
(`bluetooth.devices`, `bluetooth.connect`, `audio.sinks`,
`audio.set_output`, `display.stream_status`) with schema/risk/
capability/timeout parity against `core/device_tools.py` defs;
agent side now **57 tools** (was 52). NL aliases added: "list
bluetooth devices", "audio output devices", "screen streaming
available", "can you mirror my screen".

### Stage 22b additions (same session): network discovery, receivers, transfer-audit tests

**Network discovery is now FUNCTIONAL, not a probe of installed
tools** (`device/linux/netdisco_linux.py`): a stdlib-only RFC
6762/6763 DNS-SD pass — one bounded multicast query of a validated
service-type allowlist (refused otherwise) plus the documented
`_services._dns-sd._udp.local` enumeration, listening ≤2 s via the
RFC 6762 §6.7 legacy-unicast rule (ephemeral source port; no
privileged :5353 bind; multicast membership join degrades honestly).
No port scan, no subnet sweeps, no credential probing; malformed
packets are counted; results are capped (64), deduplicated, and each
record stays `verified: false / advertised-unverified` — claims the
device made about itself, never trust. Available as
`network.discover` (SAFE) and, specialized for screens,
`display.receivers` (SAFE; AirPlay/GCast/DIAL/RAOP allowlist). The
live pass on this laptop found no advertisements (quiet LAN,
honestly reported with the "cannot distinguish filter/no-responders"
note). NL aliases: "what devices are on my network", "find airplay
receivers".

**Screen-casting probe upgraded**: the earlier portal check watched
for a bus name that does not exist (portals live under
`org.freedesktop.portal.Desktop`); fixed. `stream_status` now reports
Mutter ScreenCast presence, the portal bus, and a bounded count of
advertised receivers; the limitation names the exact two missing
legs — portal consent is present on this host, but no receiver
protocol stack (AirPlay/GCast/RAOP pairing+framing) is implemented,
so NO stream can be created; advertised receivers stay unverified
identities only.

**Transfer-mission audit (§5)**: re-verified per-item evidence,
capacity precheck checkpoints, conflict-ask, cancelled-between-items,
WAITING resumption, move refusal, restart recovery — all covered by
the Stage-21 suite; fixed a REAL pre-existing test-file defect: a
duplicate (dead) definition of
`test_phone_share_timeout_is_waiting_not_failed_fake` in
`tests/test_transfer_missions.py` (undefined `calls` reference,
mis-asserted COMPLETED for a no-match case) that silently shadowed
nothing because the second def won — removed; added two missing
evidence tests:
`test_device_revocation_mid_mission_fails_closed` (funnel denial per
item → FAILED partial, sources untouched, audit recorded) and
`test_phone_share_resume_restages_same_file_no_duplicates` (resume
after WAITING: exactly one staged+verified entry per item, no
duplicate work).

**Suite-environment fix (root cause of two hung full-suite runs)**:
`tests/conftest.py` now pins
`ZARA_DISCOVERY_OFF=1 / HF_HUB_OFFLINE=1 / TRANSFORMERS_OFFLINE=1`.
Without the HF pin, faster-whisper performs a live hub cache check
whose connect can wait indefinitely on the current network path —
observed twice (>10 min per run, stack = huggingface_hub
`model_info` → `create_connection`); offline mode makes the verified
local cache (75 MB tiny.en snapshot) the only load path, and any
missing model fails fast instead of hanging.


### Evidence (this session)

- New suite `tests/test_device_stage22.py`: **21 passed** — BT service
  mapping + unknown-UUID honesty, bluetooth-class registry acceptance,
  MTP/PTP classification without misclassifying plain mounts,
  connect-tool confirm/MAC/unpaired/blocked/idempotency/verified/
  no-false-success paths, wpctl sink parse incl. devices row and
  default marker, set-output confirm/unknown/refused/verified/
  verify-failure honesty, describe ledger carrying touch + streaming,
  stream probe present/absent, and Stage-22 NL routing.
- New suite `tests/test_netdisco.py`: **12 passed** — real-packet wire
  parsing (advertisement + SRV/A enrichment, enumeration types,
  malformed counting), dedupe/cap/refusal/cancel/socket-failure/
  multicast-join-degradation honesty, a REAL UDP loopback round-trip
  (scripted responder, genuine query/response wire format), tool
  parity, NL routing.
- Transfer-mission suite: **21 passed** (19 existing, dead duplicate
  removed, +2 new evidence tests per the §5 audit).
- Timing-invariant compliance: ALL device tool defs re-pinned above the
  60 s poll-gap bound (`test_job_timing.py` T0) — the new probes were
  initially given 15 s timeouts which would have raced the proxy poll
  gap; both core and agent sides now 90 s, parity intact.
- Scheduler audit-row race hardened (`test_S24…` now polls for the
  audit row with a deadline — publish-then-audit ordering in
  `core/app.py` confirmed by inspection; the assertion is unchanged).
- **Full suite (final tree): 1744 passed / 1 skipped, exit 0, 4:38**
  (`python -m pytest -q`); earlier full runs exposed the two hang/expect
  issues above, both root-caused and fixed, third run green, flake
  reproduced once and hardened.
- Flutter: **439 passed** full suite (unchanged tree; no Dart edits this
  session); analyzer status unchanged from the previous verified state;
  **Kotlin untouched** (no native changes → no compile required).
- Live read-only laptop smokes (real only, nothing changed):
  bluetooth listing shows the paired phone with its advertised
  services; `wpctl status` parse shows the real sink as default;
  `stream_status` finds the Mutter ScreenCast interface while keeping
  the operation unsupported; `gio` reports no MTP mounts (true — no
  phone mounted); `network.discover` single bounded pass found no
  advertisements (honest quiet-LAN result); no `set-default`,
  `bluetoothctl connect`, or state-changing display apply was executed
  live.

### Session checkpoint

Backup **e** created after the final green run
(`/home/thiyan/projects/assistant-worktree-backup-20261019e.tar.gz`,
1.2 GB, gzip verified OK, SHA-256 appended to
`assistant-worktree-backups.sha256`). An initial over-sized variant
(5.6 GB — it accidentally nested the prior archives) was deleted
immediately; disk floor was restored to 12 GB before regenerating the
small archive. Working tree at session end: branch
`stage15-real-transfer` @ `a56a2600cb0c`, no commits made,
~95 uncommitted entries preserved.

### Still hardware-gated (honest, never faked)

Real BT connect/cycle with the physical phone, a real MTP mount's
file-visible behavior through gvfs, real HDMI audio routing, and a
real external touch panel remain user-gated hardware validations; none
were faked, and nothing here claims them. An end-to-end screen
stream additionally requires a receiver protocol stack (AirPlay/
GCast/RAOP pairing + framing) that is deliberately not implemented
half-way; mDNS discovery found no responders on this LAN during the
session (honest zero).

## 4f. Stage 22c — consent-driven screen-cast lifecycle + verified teardown (2026-10-19)

### What was built

`device/linux/screencast_linux.py` implements a bounded, single-session,
explicit lifecycle over the REAL XDG ScreenCast portal
(`org.freedesktop.portal.Desktop` — state on the live session bus on
this host), driven through an injectable D-Bus backend whose default
implementation is `busctl` subprocesses (one-shot calls + a filtered
NDJSON `busctl monitor` reader for portal Request/Session events):

- States: `idle → requesting_session → session_created →
  awaiting_user_consent → sources_selected → stream_starting →
  stopping → stopped | failed` (STREAM_ACTIVE is UNREACHABLE in this
  build by design — see honesty boundary below).
- **Consent is real**: `CreateSession` then `SelectSources` invoke the
  portal's own desktop source-selection/consent dialog. Nothing
  simulates, records, or bypasses it; the engine waits (bounded, 30 s
  default) for the portal's `Response` signal, subscribed BEFORE the
  call (the standard portal race) and matched by request object path.
- Portal response codes mapped per the spec: 0 = ok, 1 = user
  cancelled, 2 = other error — each maps to a distinct, honest
  terminal state (cancelled → `stopped` with reason; denied/error →
  `failed` with reason; timeouts close the pending desktop REQUEST
  object so no stranded dialog remains).
- **Verified teardown**: `Session.Close` is issued, then teardown is
  claimed successful ONLY when either the portal's `Closed` signal is
  observed for that session path OR the session object's absence is
  verified via introspection; otherwise the state becomes `failed`
  with `cleanup_unverified: true`, the error names the surviving
  session handle, and the bounded lifecycle log retains the safe
  diagnostic (path only — never media).
- **Duplicate-start prevention**: `start()` refuses while the engine
  holds any non-terminal state; `stop()` is idempotent (true no-op on
  idle/stopped, verified teardown otherwise) and wakes a blocked
  consent wait promptly, closing the outstanding dialog request.
- **Media honesty boundary**: `OpenPipeWireRemote` + a PipeWire client
  bound to the portal remote are NOT implemented (a CLI D-Bus bridge
  cannot receive an fd reply; no PipeWire python binding exists in
  this environment). A consented session is therefore torn down right
  after source selection with an exact reason string; NO stream is
  created, no frames are ever touched, nothing is persisted (no
  screenshots/recordings anywhere, by construction and by test).
- Zara cancellation propagates into the underlying operation (stop
  event ⇒ Request.Close of the outstanding dialog ⇒ Session.Close ⇒
  verified absence).
- Tools + parity: `display.cast_start` (CONFIRM risk/permission,
  timeout 90.0 s both sides), `display.cast_stop` (SAFE, idempotent),
  and `display.stream_status` now carries the live lifecycle state
  alongside the availability ledger. NL aliases distinguish
  availability ("screen streaming available", "is my screen being
  shared"), start ("start screen sharing", "share my screen"), and
  stop ("stop screen sharing").

### Live interface evidence (read-only; nothing captured)

- `busctl --user introspect` org.freedesktop.portal.Desktop /
  /org/freedesktop/portal/desktop `org.freedesktop.portal.ScreenCast`:
  methods CreateSession(a{sv})→o, SelectSources(o,a{sv})→o,
  OpenPipeWireRemote(o,a{sv})→h, Start(o,a{sv})→o;
  AvailableCursorModes=7, AvailableSourceTypes=7, version=5.
- A harmless `CreateSession` wire round-trip was executed live (no
  dialog can appear at that step): busctl returned the request object
  path `o "/org/freedesktop/portal/desktop/request/1_371/zaraProbe"`,
  confirming the count-style `a{sv}` CLI marshalling and the
  `o "path"` return format the engine parses. The deterministic
  session path was introspected (absent — auto-reaped); no consent
  dialog opened, no capture occurred, nothing left behind. The
  Response-signal payload shape was pinned from `busctl monitor
  --json=short` NDJSON.
- `SelectSources` was deliberately NOT run against the live portal in
  this unattended session (it opens the real on-screen dialog). See
  the interactive step below.

### Tests and results

- `tests/test_screencast_life.py` — **22 passed** (parity + T0 timing
  + smoke). Deterministic FakeBackend double ONLY; it is a test aid,
  never live evidence. Coverage: no-consent refusal, happy path (ends
  `stopped`, verified close, media refusal reason carried), user
  cancel (1), portal denial (2), timeout at the CreateSession AND
  consent boundaries (dialog closed via Request.Close), Zara stop
  during the consent wait (prompt wake, closed dialog), D-Bus
  disconnect, missing session_handle (never proceeds to sources),
  duplicate start refused, double stop idempotent, cleanup failure
  reported honestly (`cleanup_unverified`), restart after failed,
  SelectSources wire failure funnels to `failed` with verified
  teardown, bounded lifecycle log + audit hook, no-media-persistence
  (fake saw
  only CreateSession/SelectSources/Close), tool parity, handlers
  route through the single engine, aliases.
- Neighboring suites after the registry/alias changes: test_stage2
  parity + test_job_timing (T0) + test_device_stage22 +
  test_intent_plans — **58 passed**.
- **Full Python suite (final evidence, current tree)**: `python -m
  pytest tests/ -q` → **1766 passed, 1 skipped, exit code 0**, 4:32
  (the skipped item is the single pre-existing platform-gated skip;
  1744 prior baseline + 22 new). Flutter/Kotlin checks were not
  re-run — no Dart or native files changed, prior green results
  (439 passed Flutter) stand.

### Still interactive-user-gated (honest, never faked)

- The REAL desktop consent dialog has not been answered by a human in
  this build's lifecycle yet: the unattended session must not open
  it. Next step (user present): run `display.cast_start (confirm)`
  from Zara, observe the GNOME dialog, pick a monitor, and verify the
  engine lands on `sources_selected` → honest media-refusal teardown;
  this also validates the b/u `a{sv}` token marshalling on the wire.
- Receiver-protocol support (AirPlay/GCast/RAOP mirroring with
  pairing + stream framing) remains OUT OF SCOPE — unchanged from
  Stage 22: streaming stays unavailable by exact documented reason
  even with a consented session.

## 4g. Stage 23 — media-transport boundary: real fd transport + GStreamer PipeWire client (2026-10-19)

### What was built

The Stage-22c blocker — "a CLI D-Bus bridge cannot receive an fd
reply; no PipeWire python binding exists" — is resolved WITHOUT any
new dependency, using two already-installed, documented system
facilities:

- **fd transport (`device/linux/dbus_sd_linux.py`)**: a minimal
  ctypes client over **systemd's sd-bus** (`libsystemd.so.0`, present
  on this host). One PERSISTENT user-session-bus connection carries
  ALL portal method calls (portal sessions are owned by the calling
  connection — a transient connection would reap the session on
  disconnect; the Stage-22c textual calls did exactly that, silently).
  `a{sv}` options are marshalled through the documented
  open/append/close-container sequence (dict-entry container type is
  `SD_BUS_TYPE_DICT_ENTRY` = `e` — passing the `{` signature
  character returns EINVAL; established empirically and confirmed
  against the systemd v255 source). Reply reading supports `o`, `s`,
  `u`, and `h`: per the sd-bus man page, reading `'h'` does NOT dup —
  the code dups with `fcntl(F_DUPFD_CLOEXEC)` so the fd survives the
  message unref and is owned by the receiver. Absence probes use
  `Properties.GetAll` on the Session interface: live GDBus answers
  `Object does not exist at path …` only for GONE objects (verified
  live: introspection is useless here — the portal returns an EMPTY
  node for ANY sub-path).
- **PipeWire client (`device/linux/pipewire_linux.py`)**:
  **GStreamer `pipewiresrc`** (plugin v1.6.2 installed here, MIT/X11,
  shipped by the PipeWire project) driven through bounded
  `gst-launch-1.0` child processes. Chosen over ctypes-binding
  libpipewire (huge unreviewable SPA surface) and over anything not
  installed. The portal fd is passed to the child with
  `subprocess pass_fds` (the only inherited descriptor), never via
  text; frames flow to `fakesink` and are DISCARDED — nothing is
  written anywhere, no receiver protocol exists.

### Lifecycle integration (`screencast_linux.py`)

The engine's media boundary is now a real, gated attach sequence
behind the SAME lifecycle (no second engine, all confirmation
boundaries intact):

- States extended: `… → consent_granted → sources_selected →
  stream_starting → pipewire_connected → stream_active → stopping →
  stopped | failed`. `consent_granted` is set at `SelectSources`
  Response 0; `sources_selected` requires a VALID consented node list
  (an absent/empty/unusable `sources` payload is a hard refusal —
  we never attach to an unapproved stream).
- Attach sequence: portal `Start` (bounded 10 s; response 1 = user
  cancel, other = failure, timeout closes the pending REQUEST) →
  `OpenPipeWireRemote` (real fd via sd-bus) → **one-frame probe**
  `pipewiresrc fd=<fd> num-buffers=1 ! fakesink` (bounded 8 s): exit
  0 with EOS = at least one REAL media frame flowed — this is the
  independent media-flow proof required before any STREAM_ACTIVE
  claim → a SECOND `OpenPipeWireRemote` (a fresh remote; the probed
  connection is never reused) → continuous discard-sink pipeline
  child spawned → `STREAM_ACTIVE` only then. A child-death watcher
  (injectable poll interval) tears the session down honestly if the
  pipeline dies on its own.
- Honest refusals preserved per capability: a non-fd-capable backend
  (busctl fallback) refuses with the fd-transport reason; a missing
  gst/pipewiresrc reports the exact package prerequisite
  (`gstreamer1.0-pipewire` / `gstreamer1.0-tools`). Every failure
  path closes the received fd (verified by fstat in tests), kills
  bounded probe children, and tears the consented session down with
  verified cleanup; cleanup-unverified failures keep the surviving
  handle visible.
- Status: `display.stream_status` now carries
  `pipewire_media: {usable, reason, transport}` plus the lifecycle
  state; media evidence is bounded metadata only (probe exit code,
  negotiated-caps string ≤ 200 chars, frame count) — never bytes,
  never paths, never persisted.
- Teardown order: media child stopped FIRST (verified exit observed),
  then `Session.Close`, then Closed-signal/absence verification; an
  unverified child exit marks cleanup-unverified (fail-closed).

### Full-suite result (2026-10-19, single run, no overlap)

`python -m pytest tests/ -q` → **1784 passed, 1 skipped, EXIT=0**
(5:37; prior baseline 1766 passed + 18 new in
`tests/test_screencast_media.py`). Targeted first:
`tests/test_screencast_media.py` 18 passed + neighbors
(`test_stage2`, `test_job_timing` (T0), `test_device_stage22`,
`test_intent_plans`) 58 passed. Live read-only smokes this session:
engine default backend is the fd-capable `SdBusBackend`;
`pipewire_media.usable: true`; no-confirm start refused before portal
contact; idle stop no-op. Environment: PipeWire server 1.6.2, gst
plugin 1.6.2 (version-matched), GDBus 2.88.0 portal implementation,
libsystemd sd-bus.

### Still interactive-user-gated (honest, never faked)

- The media path has NEVER run against the real desktop: it requires
  the real consent dialog answered by a human. When the user is
  present, `display.cast_start (confirm)` will now proceed — dialog →
  Start → fd → probe → continuous pipeline → stop — and the
  STREAM_ACTIVE claim will be backed by the one-frame probe exit code
  and the live caps line captured during THAT session.
- Media frames flow to a discard sink for verification purposes only;
  there is still NO receiver protocol (AirPlay/GCast/RAOP remain out
  of scope — a user decision), no persistence, and no network egress.

## 4h. Stage 24 — LIVE interactive screen-cast validation + GNOME-form rework (2026-10-20)

### Objective

Validate the actual consent-driven screen-capture path on the real
Ubuntu desktop through Zara's normal funnel — no mocks, no fabricated
consent. The user explicitly authorized each numbered attempt; every
failure was captured with exact stage + teardown verification; nothing
was persisted beyond bounded metadata (state transitions, protocol
field dumps, engine evidence).

### What the live attempts revealed (each fix implemented before the next)

1. **Attempt 1 (encode bug)**: real `busctl monitor --json=short`
   serializes SCALAR variants with a bare `data` value
   (`{"type":"s","data":"<path>"}`), while the test fakes always
   injected list-wrapped data. `_decode_variants` required a list →
   `session_handle` was never unwrapped → "carried no usable
   session_handle". FIX: `_decode_variants` now unwraps both scalar and
   list shapes recursively (regression test
   `test_real_wire_bare_scalar_variant_decode` with the EXACT live
   payload shape).
2. **Attempt 2 (false consent revealed)**: after the decode fix the run
   reached `consent_granted` — analysis of the real
   `xdg-desktop-portal-gnome` source proved `handle_select_sources`
   answers SelectSources **instantly with an EMPTY vardict (no dialog,
   no consent!)**; the real consent dialog is shown at `Start`
   (`create_screen_cast_dialog`, `on_gnome_screen_cast_session_ready`).
   The engine's consent-at-SelectSources semantics were a FALSE claim.
   REWORK: `SelectSources(0, {})` is now only "selection acknowledged
   (GNOME form)"; `consent_granted` is claimed ONLY when the portal
   Start ok-response arrives after the dialog (audit
   `selection_acknowledged_no_nodes` vs `consent_granted`); GNOME's
   Start results (`streams` a(ua{sv})) validate node ids at that point
   (validator extended for `(u node_id, a{sv} props)` stream structs;
   present-but-unusable lists still hard-refuse; `_media_refusal` no
   longer hard-codes `consent_granted=True` — state-tracked
   `_consented` instead).
3. **Attempt 3 (InvalidArgs)**: live introspection fixed the Start
   signature `o s a{sv}` — our call lacked required `parent_window`
   ("" = no parent, per spec). Fix applied.
4. **Attempts 4–6 (dialog DID work)**: the REAL GNOME dialog appeared,
   the user clicked Share, Start completed, `consent_granted` (now
   truthful) + `sources_selected` + real fd via sd-bus — then the
   one-frame probe failed: pipewiresrc `Failed to set pipeline to
   PAUSED`. Verbose stderr tail (bounded 16-line `probe_ring` added to
   media evidence) captured the exact cause: **"stream error: target
   not found"** — the probe never bound to the consented node.
5. **Attempts 7–8 (bind semantics)**: pipewiresrc `target-object`
   parses numeric strings as SERIALS (never matching) per pipewire
   1.6.2 source; the element's numeric `path` property feeds
   pw_stream_connect's target_id ("the portal ... only provide the
   object id" — the documented deprecated bind). Node is now pinned via
   `path=<consented node id from Start streams>` for BOTH the probe
   and the continuous child (`pinned_target` recorded in evidence).
6. **Attempt 11 (root cause, pw-mon + pw-dump + WP debug)**: the
   consented mutter node IS healthy in the user session daemon
   (`Stream/Output/Video`, `media.name=meta-screen-cast-src`, live
   BGRx 1920×1080 output port) and our fd-bound client node registers
   with correctly pinned `node.target=<node id>` — BUT our stream node
   carried **`media.class = "Stream/Input/Unknown"`** (pipewiresrc
   doesn't declare a media type at connect). WirePlumber's video
   policy cannot classify "Unknown", its linking hooks crash on the
   nil `media.type` (`common-utils.lua`/`find-best-target.lua`), and
   `prepare-link.lua` sends the client error `target not found` and
   destroys the node. FIX IMPLEMENTED: both gst pipelines now declare
   `stream-properties=props,media.type=Video,media.class=Stream/Input/Video`
   so the session manager can classify and link the consented stream.

### Honest result

- Portal available ✓, session created ✓, real consent dialog shown and
  answered by the user ✓ (Start-response-backed), fd received ✓,
  pinned bind attempted ✓ — **media flow NOT yet verified live**: the
  attempt-11-validated root cause (stream media class) is fixed in
  code, but verifying the fix requires another user-answered dialog
  and the user is away. **STREAM_ACTIVE has not been claimed.**
- All teardown effects were verified on every attempt
  (`closed: true`, `cleanup_unverified: false` in all 11 runs; no
  media, no screenshots, nothing persisted; no network egress).
- Fixes are covered by 45 targeted tests (23 lifecycle + 22 media;
  GNOME-form flows, wire-shape regression, consent-at-Start ordering,
  user-cancel never counts as consent, streams-empty refusal,
  fd-incapable refusals never claim consent) + 58 neighbor tests.

### Still interactive-user-gated (honest, never faked)

- Real `display.cast_start (confirm)` → dialog → probe exit 0 →
  STREAM_ACTIVE with live caps evidence — the ONE remaining gap for
  Stage 24 completion, blocked on the user re-authorizing one run.
- Receiver protocols (AirPlay/GCast/RAOP) still out of scope; discard
  sink only; no persistence; no network egress.

## 4i. Stage 24c — research round 2: verify-after-act at both act surfaces (2026-10-20)

Gap-first follow-on to the round-1 survey (§ reason paths in
`docs/RESEARCH_2026-10-20.md`, "Round 2"): the act→verify loop was
found incomplete at exactly two boundaries, and each was closed with
the smallest honest integration — no new dependency, no independent
agent, no policy change.

### What was implemented

1. **`browser.wait`** (`core/browser_tools.py`): bounded poll
   (0.2–30 s, 0.5 s cadence, hard-capped) until AND-combined observable
   conditions hold — `url_contains` / `title_contains` /
   `text_present` / `selector_visible`. Transient evaluation errors
   (mid-navigation teardown) are recorded per round and surfaced in the
   final report; timeout honestly returns `satisfied:false` plus the
   untouched page snapshot. Read-only while waiting. Registered in
   `BROWSER_TOOLS` → auto-registered at Core startup (no wiring
   change). Inspired by Playwright's web-first-assertions best
   practice; browser-use's independent-judge loop was rejected for
   violating the single-operator contract.
2. **`verify_ui` / pure `match_state`** (`device/adb_linux.py`):
   verification half of the observe→act loop over the Stage-24b
   observation layer. Validated AND-combined conditions (text /
   text_contains / desc / desc_contains / cls / clickable; unknown
   keys raise — typos fail loudly), bounded retry (≤3 dumps, ≤2 s
   intervals), truthful `per_condition` evidence that names which key
   broke the closest node. Read-only always: a failed verification
   never triggers an act; input stays exclusively with the
   paired-device protocol. Inspired by android-use's structured
   UI-state validation; openatx's device-side stub server rejected
   (stale, install-invading) and Appium rejected for this gap (the
   need was verification logic over dumps we already produce, not a
   transport).

### Tests and results (this session)

- `tests/test_browser_verify_wait.py` (new, 21 tests): validation
  guardrails + bounded-timeout clamps, fake-page error-recording
  rounds, REAL loopback-Chrome verification: satisfied path,
  timeout-honesty path (`satisfied:false` + unmet condition + page
  snapshot), malformed-selector error path, registry contract (safe /
  restricted / `browser.automation`).
- `tests/test_adb_observe.py` extended (14 → 21 tests): matcher
  semantics (exact vs contains, clickable bool), evidence + broken-key
  reason naming, loud condition validation, bounded-retry-then-honest
  failure, attempt cap, dump-failure stage reporting, `expect`
  required.
- Neighbor suites: `tests/test_browser_tools.py` +
  `tests/test_browser_workflows.py` (31 tests) — all pass alongside.
- Full suite: run recorded below at completion.

### Boundaries kept (honest)

- No ADB taps/keys/stream surface added; `device/adb_linux.py` stays
  observation+verification-only.
- No consent dialog opened this session; ScreenCast live-media
  validation remains the same pending user-approved run (§4h).
- No installs/downloads; disk ≥10 GB maintained (package caches
  cleared earlier round; user training workspace untouched).

## 4j. Stage 24d — GUI-speed round for mobile + Ubuntu (same day)

Follow-up research on fast GUI automation (survey + verdicts in
`docs/RESEARCH_2026-10-20.md` "Round 3"). Outcome: Zara ALREADY owns
the fast paths on both surfaces (phone: in-app AccessibilityService
snapshot with structural ids over the paired protocol, ms-level — the
same architecture droidrun markets; desktop: AT-SPI in-tree actions
with content verification). The slow path is the ADB observation
fallback (uiautomator dump 1–5 s) and the absent Wayland injector.

### Implemented (speed, policy-safe)

1. **Compressed-first ADB dump** (`device/adb_linux.py::ui_dump`):
   `uiautomator dump --compressed` tried first (smaller XML → faster
   transfer + parse), one honest fallback to the plain dump on
   adb-level flag rejection; spawn/transport failures fail fast.
2. **Truthful latency telemetry everywhere**: every dump reports
   `elapsed_ms`, `attempts`, `compressed`, `nodes`, `bytes`; every
   verification reports `total_ms`, `nodes_observed`,
   `dump_elapsed_ms` on failure — evidence-based fast-path routing
   (paired-app snapshot preferred, ADB dump fallback).
3. **Fast-path routing rule documented** (research doc):
   phone observe/verify prefers the paired app's a11y snapshot;
   `verify_ui` is the no-paired-app fallback; desktop acts stay
   AT-SPI-preferred with X11 xdotool fallback.

### Tests and results (this session)

- `tests/test_adb_observe.py` extended (22 tests): compressed-first
  ordering, rejected-flag fallback (still bounded, mode recorded),
  fail-fast spawn semantics (no flag-retry spray), parse-stage mode
  counting, `verify_ui` speed telemetry (satisfied + honest-failure
  shapes).
- Regression before the full run: adb + browser-verify +
  screencast-media suites — 59 passed.
- Full suite: 1821 passed, 1 skipped (includes rounds 2–3 additions;
  exit 0). Stage-24d delta: +6 tests over §4i state.

### Blocked for user (honest)

- Ubuntu-Wayland input injection (ydotool + uinput udev rule +
  `ydotoold`) = root operations awaiting explicit user approval;
  Zara keeps refusing rather than faking until installed.
- Live phone latency numbers need a paired device.
- ScreenCast live-media validation (§4h) — unchanged, dialog pending.

## 4k. Stage 24e — real benchmark + read-batch op (same day)

Benchmark-first round (methodology + numbers in
`docs/RESEARCH_2026-10-20.md` "Round 4"): new reproducible
`scripts/gui_bench.py` with a capability/routing report, machine
patchable `--json`, medians/p95/failure/timeouts per probe, honest
`hardware: real|fixture|pending` labels.

### LIVE benchmark results (this Ubuntu Wayland desktop, 2026-10-20)

| probe | source | samples | median | p95 | note |
|---|---|---|---|---|---|
| adb_inventory | real adb (no device) | 5 | 3.18 ms | 3.67 ms | honest empty inventory |
| adb_dump | pending | — | — | — | no attached Android device |
| fixture_ui_parse | fixture | 5 | 3.63 ms | 3.88 ms | 400-node XML via production parser (CPU) |
| atspi_tree | real AT-SPI | 5 | 336 ms | 387 ms | bounded 150-node walk |
| atspi_node | real AT-SPI | 5 | 43.9 ms | 44.8 ms | freshness re-read per action |
| atspi_multi | real AT-SPI | 5 | 49.8 ms / 8 reads | 50.9 ms | ≈7× vs 8 spawns (~351 ms) |
| x11_window_list | real (Xwayland) | 5 | 2.29 ms | 3.23 ms | works under Wayland session |
| x11_window_active | real (Xwayland) | 5 | 2.24 ms | 2.57 ms | same |
| android_paired_a11y | pending | — | — | — | contract verified by fixtures only |
| adb_dump (flag fallback) | fixture | — | — | — | compressed→plain tested |

### Implemented (evidence-backed software change + scaffolding)

1. **`atspi_probe.py` op `multi` + `tools_linux._atspi_batch`** — one
   helper spawn performs up to 8 read-only a11y ops. Measured ~7×
   fewer roundtrips for the read side; structurally refuses
   state-changing children on BOTH layers (a write can never be
   reordered ahead of a failed freshness check). Additive; live ops
   untouched and re-verified by the existing a11y test suite.
2. Benchmark harness with failure-containment (a raising probe
   becomes a `pending` data row, never a crash).
3. `verify_ui` no-stale-reuse regression test: attempt N's evidence
   may only come from attempt N's fresh dump.

### Test results (this session)

- New: `tests/test_gui_bench.py` (15 tests) — summarize math, fixture
  parse probe, honest pending on inventory timeouts, batch read-only
  gating (both layers), spawn-count accounting, probing containment,
  no-paired-phone honesty entry.
- Extended: `tests/test_adb_observe.py` (+1 freshness proof).
- Targeted: gui_bench + adb + a11y + verify-wait suites — 63 passed.
- Full suite (final, after §4l): **1862 passed, 1 skipped, 0 failed.**

## 4l. Stage 24f — Wayland-native launch verification (same day)

Gap: `desktop.app.open` could not verify Wayland-native launches
(`wmctrl -l` measurably empty on this desktop — 0 X-managed windows).
Additive fix, all software-only:

- `atspi_probe.py` new read op `apps` (registry app names only):
  live median ≈ 50.7 ms (8 samples) vs ≈ 336 ms full tree.
- `desktop_app_open` Wayland path: before/after a11y-registry diff +
  word-bounded name match → `verified` / `requested` honestly,
  bounded ≤ 3 × 0.6 s polls; a11y down → 'requested', never faked.
- X11 path unchanged (window-count diff, still verified).
- Tests: `tests/test_desktop_app_verify.py` (20 tests) +
  bench probe `atspi_apps` with honest real/fixture/pending labeling.

Security/policy invariants preserved: launch is still a CONFIRM
state change; verification adds read-only observation only; both
`_atspi_batch` write-gating and the freshness contracts untouched.

## 4m. Stage 24g — mission-bound plan continuation (same day)

Evidence-driven reliability feature: multi-step plans already combined
tools with typed `fetch` data-passing, per-step approval, and
cancellation — but an approval hold inside a plan left the remainder
permanently dormant (step i would run on approval; steps i+1..N never
did, and the mission stayed `WAITING_FOR_PERMISSION` with no path
forward). Methodology and sources in `RESEARCH_2026-10-20.md` "Round 6".

### Implemented (additive; Zara Core stays the single authority)

1. `Mission.plan` envelope (bounded 16 KiB, persisted by
   `PersistentMissionEngine` via additive in-place schema migration —
   `ALTER TABLE missions ADD COLUMN plan` verified against a
   legacy-schema DB).
2. Held-step → execution-record binding recorded at park time
   (`held: {step, execution_id, tool}`).
3. `IntentTurnService.resume_plan`: verifies the held record
   (mission_id + tool + terminal outcome) through
   `fabric.engine.get`, adopts its result, and re-enters the SAME
   plan driver from the next step; FAILED/CANCELLED ends the mission
   honestly; still-running/mismatched/pruned refuse with reason.
   Steps run under the plan's recorded `who`. One attempt per step, a
   mid-continuation approval parks again.
4. Continuation control: `POST /v1/missions/{mid}/continue` (audit:
   `api/mission_continue`) + exact NL phrases ("continue the task")
   resuming the most recent parked mission. No second engine, no
   executor-side queue; nothing bypasses resolver → policy → governor
   → fabric → verification per step, and approval auto-fire is
   impossible (continuation observes the record instead).

### Tests (this session)

- `tests/test_plan_continuation.py` (16 tests, all fixture-based):
  park binding, refusal-before-approval, completion-without-re-run
  (proved by tool call counts), failed-held-record honesty with
  terminal re-refusal, unknown/wrong-state refusals, stored-result
  fetch continuation, re-park on second independent CONFIRM step,
  ghost-record + wiped-envelope refusals, NL phrase drive/miss,
  persistence across restart, oversized-plan refusal, legacy-DB
  migration, HTTP 404 + roundtrip, mid-flight refusal.
- All mission/intent/messaging/scheduler/universal suites re-run green.
- Full suite: 1878 passed, 1 skipped (final count at session end).

## 4n. Stage 24h — plan-continuation audit + hardening (same day)

Adversarial audit of §4m against restart/duplicate/concurrency/
authorization scenarios. Three genuine gaps found; smallest additive
fixes implemented; no behavior changed for the un-hooked path.

### Confirmed gaps

1. **Concurrent continuation race**: two simultaneous `resume_plan`
   calls for the same mission could both pass the WAITING check and
   both drive the remaining steps (duplicate side effects, no
   ownership).
2. **Crash between side effect and persistence**: step results were
   persisted only at the mission's next transition, so a crash after a
   step's effect but before the next transition left the stored
   mission claiming that step was still pending.
3. **Restart between approval and continuation**: execution records
   are in-memory; the mission store is not. After a restart the held
   record was gone (refusal forced a restatement that would re-run the
   approved step — an unseen duplicate side effect), with no warning.

### Fixes (additive)

1. `IntentTurnService` per-mission continuation lock (non-blocking
   acquire): the second concurrent continue refuses with
   "already being continued" instead of racing; the lock is a
   single owner from record verification through the full drive
   (including a mid-continuation RE-park), and is dropped for
   terminal missions.
2. Per-step checkpoint in the shared `_drive_plan` loop after every
   ok step (and in adoption), so each step's outcome is stored at the
   moment the side effect happened (`_persist_plan_progress`).
3. Mission-record adoption hook (bookkeeping only, wired in
   `build_stack` via `engine.set_mission_adopt`): when a mission-bound
   record reaches SUCCEEDED, `adopt_mission_record` marks the parked
   step ok with its result immediately, clears the hold (persisted).
   `resume_plan` then accepts the stored step as the verification and
   continues without any record. Missing-record-without-adoption now
   refuses with explicit caution ("the step may already have taken
   effect — check the device before restating the task").

### Guarantees (honest)

- Exactly-once for CONTINUATION side effects: yes
  (single-owner lock + one attempt per step + adopted/terminal state
  checks). Exactly-once for arbitrary external tool effects vs a crash
  between dispatch and record persistence: NOT guaranteed — the
  residual window (crash after record-success but before adoption,
  or a lost in-flight effect) is honestly reported and never retried
  automatically; the refusal warns the step may have taken effect.
- Terminal missions stay terminal; FAILED/CANCELLED/TIMED_OUT/DENIED
  held records end the plan honestly; grants/who/approval semantics
  untouched; audit events added (`intent/mission.record.adopted`).

### Tests

- `tests/test_plan_continuation_hardening.py` (14, all fixture):
  concurrent single-owner refusal (deterministic barrier tool, each
  step runs exactly once), per-step checkpoint survives reload, 
  restart-after-approval continues via adoption without the record
  (approved step never re-run), pruned-but-adopted record continues,
  foreign/wrong-state adoption no-ops, missing-record-without-
  adoption caution, CANCELLED/TIMED_OUT/DENIED held records, two
  sequential holds in one plan ("paused again"), cancellation during
  a continuation (later steps never run, terminal), recorded-`who`
  identity preserved even when continuation is requested by another
  identity, held=missing-execution_id / non-dict / adopted-without-
  progression refusals, duplicate-after-completion.
- Pre-existing Stage 24g suite unchanged: all 16 still pass.
- Focused: 139 passed (plan/intent/grant/idempotency/stage1) +
  285 passed (app/endpoint/mission/messaging/execution).
- Full suite: **1892 passed, 1 skipped, 0 failed, exit 0** (~4:50).

## 4o. Stage 24i — natural-language reminder management (same day)

NL reminders ("remind me to X at 7 PM", "show my reminders", "cancel
my X reminder", "move my X reminder to 9 PM") built entirely ON the
existing infrastructure: each reminder is exactly ONE live
`Scheduler.schedule_once` job (recurring reminders re-arm the NEXT
occurrence at fire time — never unbounded job creation), delivery is
a notification through the existing `NotificationManager`, and every
lifecycle change is audited via the existing audit log. Deterministic
parse only (no model in the reminder path). Zero execution authority
is added: reminder text is opaque data, stored verbatim, never sent
to the resolver/policy/executor.

### Design decisions (documented, not guessed)

- **Timezone**: `ZARA_TIMEZONE` env (IANA zone), loudly validated;
  unset → the machine's local zone (IANA key, else fixed UTC offset —
  documented DST limitation of the offset fallback). Every reminder
  stores its zone name; restarts reconstruct the intended wall time
  instead of silently flipping to UTC.
- **Period words are definitions**: morning 08:00, noon 12:00,
  afternoon 14:00, evening 19:00, tonight 20:00, night 21:00,
  midnight 00:00. A day-part word also disambiguates a bare hour
  ("evening at 7" → 19:00).
- **Ambiguity policy**: bare hours 1–11 with no period word and no
  AM/PM are CLARIFIED, never guessed (12 = 12 PM by convention;
  13–23 and H:MM are literal 24 h). Relative "in N minutes|hours|
  days" must be positive and future; combined with day words →
  clarify, not both-honored.
- **Past-time policy**: bare wall time already passed today rolls to
  TOMORROW (documented); an explicit day or day-part phrase whose
  time passed today ("today at 9 PM" at 22:13, "evening walk" at
  22:13) is CLARIFIED instead of silently reinterpreted. noon/midnight
  are clock quantities and follow the rolling rule.
- **Recurrence**: "every day", "every <day-part>", "every week"
  (anchored to the current local weekday), "every <weekday>" — each
  recurrence stores an EXPLICIT anchor (hour/minute [+ weekday]) so a
  spring-forward gap that pushes one occurrence forward never drags
  the schedule with it. Wall time-of-day is fixed in the reminder's
  zone across DST (8 PM stays 8 PM; epoch deltas 23/24/25 h).
  Nonexistent wall times in a spring-forward gap map forward per
  zoneinfo fold=0 (documented).
- **Duplicate prevention**: identical (normalized text, due minute,
  recurrence) returns the SAME reminder id (idempotent create); a
  moved reminder lives in its new slot and a new time means a new
  optional second reminder, never a silent double.
- **Delivery honesty**: states scheduled → due → delivery_attempted →
  delivered_to_service / delivery_failed (final after bounded
  `MAX_DELIVERY_ATTEMPTS=3` retries) / cancelled.
  "delivered_to_service" = a queued notification was created; the
  service NEVER claims the user saw a reminder. Ledger-deduped by
  (reminder id, occurrence) — retries and restarts never duplicate a
  delivered notification. Recurring reminders chase the schedule, not
  past deliveries (a failed occurrence is not retried beyond the
  read-time honesty; the next occurrence re-delivers).
- **Missed-run policy**: after downtime a one-shot due in the gap is
  delivered ONCE (audited `restart_receipt` / `missed_run` when >2 min
  late); a recurring reminder NEVER bursts — one notification for the
  most recent missed occurrence, then re-anchor. Ledger-based
  recovery, never hook-availability-dependent (the scheduler's
  `_recover`-time hook exceptions are tolerated and the service's own
  startup reconciliation re-delivers).
- Routing via `IntentTurnService` steer (`is_reminder_utterance`)
  BEFORE the parser (precedent: `_RESUME_PHRASES`); unknown op shapes
  fall through to the normal parse path. Stack wiring in
  `build_stack` (`reminders` key; `_sched_fire` reminder-payload
  branch does NOT touch exec announce/dispatch semantics — S12–S47
  scheduler contracts untouched).

### Out of scope (honest)

Full calendar dates ("on 15 March"), "next Tuesday" recurring beyond
simple weekday phrasing, per-reminder per-user ACLs, and server-side
calendar sync. Reminders are process + additive-SQLite persisted
(`ZARA_REMINDERS_DB`), not synced anywhere.

### Tests

- `tests/test_reminders.py` (39, all fixture, deterministic fake
  clock + ManualScheduler driving the REAL fire path + fake failing
  provider; no real notifications sent): clock forms, bare-hour
  clarify, 12-PM convention, period-word definitions/disambiguation,
  day-part past clarify, bare time rolling, "today" past clarify,
  relative intervals (+0 refuse, day+relative clarify), daily/weekly
  anchors with stored (hour, minute), vague clarify, invalid
  `ZARA_TIMEZONE` loud failure, zone-stored-not-UTC-assumed, task/when
  extraction with case preservation, "chapter 12" number guard,
  list/cancel/reschedule/find extraction, injection guard (command-
  shaped text stored as data, executed False), steer gate, exactly-one
  job, idempotent duplicate create, past/invalid/empty/bad-recurrence
  refusals, fire-once + ledger dedup across restarts, cancel-before-
  delivery suppression, cancel unknown/ambiguous refuse, reschedule
  replaces the job (exactly one live), bounded delivery-failure
  honesty (3 attempts → delivery_failed, no unbounded retry), retry
  never duplicates one delivery, recurring re-arm one job at a time
  (+24 h anchor), distinct honest states ("seen"/"read" never
  claimed), restart persistence + recovery (future re-armed; due-during
  -downtime delivered once), two-restart idempotence (no re-delivery),
  recurring day-offline no-burst (one `missed_run`), DST fall-back wall
  hold (25 h epoch delta) and spring-forward gap map-forward WITHOUT
  schedule drift, NL turn integration (create/list/cancel/reschedule,
  recurring create, ambiguous cancel clarifies without cancelling,
  non-reminder utterances unaffected, audited ops).
- Neighboring suites unchanged: intent/plans/scheduler/continuation
  138 passed.
- Full suite: **1931 passed, 1 skipped, 0 failed, exit 0** (~4:43).

## 4p. Stage 25 — local document intelligence + consent-based clipboard (same day)

Bounded, read-only local document workflow — find documents → read/
extract → search within → deterministic local summary — implemented as
Linux-agent device tools under the SAME allowlist rules as the
filesystem tools (allowed roots HOME + /tmp, realpath admission,
sensitive-name refusal), plus a deliberately tiny consent-based
clipboard read/write pair. Registered through the existing registry,
proxied through the existing device-job pipeline, gated by the
existing policy: no second file subsystem, no alternate authorization
path, no new scheduler, nothing leaves the host by default.

### Existing infrastructure reused (nothing duplicated)

- **Device-tool pipeline**: contracts in `core/device_tools.py`
  (`DEVICE_TOOL_DEFS` + `register_device_proxies`), real handlers as
  Linux agent tool tables (`device/linux/docs_linux.py`,
  `device/linux/clipboard_linux.py`, appended to `LINUX_TOOLS` in
  `device/linux/tools_linux.py`); parity is test-locked by
  `tests/test_device_compat.py` plus an explicit schema/caps/risk
  parity test. `_scoped` admission, scratch/O_NOFOLLOW discipline and
  sensitive hints are the pre-existing filesystem-tool rules reused at
  call time (deferred import keeps module boundaries acyclic).
- **Extraction libraries — already installed, no new installs**: PyPDF2
  3.0.1 (pure Python; final maintained release of the PyPDF2 line,
  BSD/Apache-lineage licensing) for page-aware PDF text. DOCX without
  python-docx on purpose: `.docx` is a ZIP of OOXML, so stdlib
  `zipfile` + `xml.etree` extracts paragraphs and simple table rows
  with zero new dependencies. TXT/MD/JSON/CSV bounded reads with an
  explicit decode chain (utf-8-sig → utf-8 → utf-16(BOM) →
  latin-1-replace, `encoding` reported); HTML stripped with the
  stdlib parser (script/style content never extracted). OCR engines,
  embedding stack and system packages are deliberately NOT installed
  (approval-gated; no demonstrated need yet).
- **Authorization/audit/execution**: PolicyEngine (SAFE docs tools
  under the existing `filesystem.read` capability; clipboard tools are
  RESTRICTED + CONFIRM → a fresh single-use scoped grant each call),
  ExecutionEngine record/verify, MissionStep.result bookkeeping in
  the existing plan flow — mission steps receive the full source-
  attributed result via the normal executor path.

### Implementation boundaries (documented, enforced in code + tests)

- **Everything bounded**: discovery per run ≤5000 entries visited /
  depth 8 / 200 results with a wall-clock budget (8 s) and cooperative
  cancellation between directories and pages; extraction stops at
  20 MB/file and a char cap (default 40 000, requestable lower,
  hard max 512 000); find ≤40 matches with ≥40-char excerpts
  (hard cap 600); summarize ≤12 sentences. Every truncation is
  reported as a limit (`truncated: true`), never presented as truth.
- **Discovery is not a home scan**: explicit root path required (HOME
  fallback explicit), hidden dirs/files and sensitive-named files are
  SKIPPED + counted, symlinks are never followed during traversal
  (symlinked *files* are skipped and counted, not dereferenced) and
  unreadable directories are counted honestly via the walk `onerror`
  hook. Files are never copied/moved/renamed/deleted.
- **Honest PDF states**: encrypted → refused ("password required",
  never guessed); corrupted → refused with the exception class, no
  partial pretend-extract; oversized → refused (never silently
  truncated mid-index); all-pages-empty → `ocr_required: true`,
  `extraction: insufficient_text`, with the note that OCR is NOT
  installed and NOT run — nothing claims content it cannot read;
  partial files report `empty_text_pages` (which pages carry no
  machine text), so "no text on page 3" is visible instead of hidden.
- **Untrusted-data discipline**: document text is opaque data — the
  `docs.*` result shape is fixed (path/kind/page/block/excerpt/score/
  char offsets) and contains no action/proposal fields by contract
  (asserted); a document containing "run terminal.exec rm -rf"
  returns that string as an excerpt and nothing routes/executes it.
  No logging of content anywhere on these paths (engine events carry
  ids, not payloads — audited in the contract tests).
- **Summaries are labelled, never overstated**: `docs.summarize` is a
  deterministic LOCAL extractive summary (keyword-sentence scoring,
  static method string "extractive-local (no model, no network)"),
  every returned sentence carries page/block provenance, and a
  scanned/blank PDF yields an empty summary — never an invented one.
  A model-grade summary remains the brain's job on the returned
  provenance (existing provider-consent rules; this module never
  touches the network, so nothing uploads by default).
- **Clipboard never becomes a logger**: no polling, no history, no
  persistence, content never in error strings/audit output; strict
  caps (read bounded at 100k chars with `truncated` flag; writes ABOVE
  the cap refused, not truncated); backends limited to
  `wl-paste/wl-copy` (Wayland) and `xclip` (X11) via shell=False
  arg-lists; missing backend / headless session → `supported: false`
  (success never faked), and a backend timeout is an honest FAILED
  state — NOT misreported as "empty clipboard"; xclip's exit-1-no-
  stdout convention is the one path reported as `empty: true`.
  Writes are verified by read-back and `verified` is only true on
  exact match; `desktop.clipboard.read` never implies write (disjoint
  capabilities, tested).

### New tools (all `supported_devices=["linux"]`, RESTRICTED)

| tool | capability | risk | notes |
|---|---|---|---|
| `docs.search` | filesystem.read | SAFE | bounded discovery, metadata + limits |
| `docs.read` | filesystem.read | SAFE | page-aware extraction, honest state flags |
| `docs.find` | filesystem.read | SAFE | keyword excerpts with page provenance |
| `docs.summarize` | filesystem.read | SAFE | local extractive, provenance per sentence |
| `desktop.clipboard.read` | desktop.clipboard.read | CONFIRM | one-shot read, fresh grant |
| `desktop.clipboard.write` | desktop.clipboard.write | CONFIRM | ≤100k chars, read-back verified (envelope widened to 131072 to mirror the handler cap) |

### Tests

- `tests/test_docs_linux.py` (30, real local fixtures under /tmp —
  hand-built minimal PDFs readable by PyPDF2, stdlib-zip DOCX, real
  text/HTML/JSON, corrupted/encrypted/blank scanned-like PDFs; no
  network, no account, no OCR): bounded discovery (ext filter,
  `max_results` truncation honesty, hidden/sensitive/unsupported
  skips, symlink never dereferenced, unreadable dirs counted,
  cancellation honoured, `/etc` scoped refusal); page-provenance
  extraction; scanned → `ocr_required` with the note, chars 0,
  nothing fabricated; partial extraction reports empty pages;
  encrypted/corrupted/oversized refusals (over-cap refusal is
  honest, not truncation); DOCX paragraphs + tables, corrupted DOCX;
  HTML script/style content excluded; unsupported `.doc` and
  sensitive names refused; decode-chain TXT read; char-cap truncate +
  report; find (ranking, bounded excerpt, empty/no-match honesty,
  stop-word query refusal, prompt-injection stays data with the
  exact result-schema assertion); summarize (provenance per
  sentence, scanned → empty summary, top bounded).
- `tests/test_clipboard_linux.py` (13, fixture fakes via the module's
  monkeypatch targets — NO real clipboard read/write in tests):
  headless/missing-backend honesty (wl-paste vs wl-copy named per
  op), x11 backend read with shell=False arg list asserted, empty +
  timeout distinction (NOT conflated), bounded read truncation flag,
  write round-trip verified=True, read-back mismatch → verified
  False, backend failure honest + content-free reasons, oversized
  refusal without touching any backend, non-string/empty refusal,
  contract shape (caps, risks, envelope width mirrors handler cap).
- `tests/test_docs_clipboard_contracts.py` (12, core side): def
  honesty (linux-only, SAFE vs CONFIRM, capability strings), core↔
  agent schema/caps/risk parity, advertised agent capabilities,
  proxy registration, input-envelope bounds (pre-dispatch, `4096`
  default vs clipboard-write `131072` mirrored), oversized input
  rejected BEFORE any job queue entry, device-job round trip with a
  sensitive marker carried through memory only (no history growth),
  single-use CONFIRM grant consumed (no reuse), read-capability
  never grants write, proxy without device context refuses, and a
  REAL ExecutionEngine mission-bound submit (long-poll device agent
  → complete) keeping byte-for-byte provenance on the record.
- Neighboring suites unchanged (intent/plans/scheduler/reminders/
  jobs 184 passed) and `tests/test_device_compat.py`,
  `test_desktop.py`, `test_file_workflows.py` green.
- Full suite: **1986 passed, 1 skipped, 0 failed, exit 0** (~4:47).

### Real vs fixture validation (honest)

All coverage above is REAL local fixtures/dirs and the REAL PyPDF2 /
zipfile / stdlib extraction code paths on this machine, plus REAL
backend binary presence (`wl-copy/wl-paste/xclip/xsel` all present).
NOT done, claimed, or tested: OCR on a genuinely scanned human
document (no scanner document available; blank-page fixture only),
live clipboard content of this session's user (never read by tests),
mission NL phrasing for docs tools (registered/policy-gated, but the
NL parser does not propose them yet), and any cloud-model summary
(deliberately local-only). Flutter/Kotlin untouched (no client
change) — no client checks required.

### Known limitations (honest)

- Legacy binary `.doc` (OLE) unsupported by design — refused with a
  clear error; no OCR anywhere; scanned PDFs return honest insufficiency
  instead of content; PyPDF2 prints a deprecation warning (moving to
  the renamed `pypdf` successor would be a dependency decision, not a
  new install requirement).
- Keyword search is term-frequency with page provenance — no
  embeddings/vector DB (existing architecture + measured needs do not
  justify one; documented threshold for reconsidering).
- Summaries are extractive and deterministic, capped at 12 sentences;
  a language-model summary is the brain's existing capability and
  keeps the same consent rules (nothing in Stage 25 sends content
  anywhere).
- Clipboard: Wayland path is the deployed session (`wl-*`); xclip
  exists for X11 sessions but Stages 22–24 already established that
  most window probes are honestly unsupported under Wayland — the
  read/write tools themselves ARE Wayland-capable via `wl-*`.
- `docs.search` caps at 5000 visited entries/depth 8/run as a
  documented limit (a huge tree reports truncation instead of
  scanning silently).

## 4q. Stage 26 — natural-language capability integration + end-to-end verification (same day)

### What actually broke (audit findings, fixed)

- **Resolution defect (Stage 25 + pre-existing):** agent descriptors
  advertise per `required_capabilities` id; the four `docs.*` tools
  shared cap `filesystem.read`, so a proposal naming `docs.find` found
  no advertised candidate even with a real agent present
  (`no_device`). The same defect pre-existed for `filesystem.search`
  and `filesystem.extract` ("find files"/"extract document" aliases
  could never resolve). Fix: each of `docs.search/read/find/summarize`,
  `docs.searchtext` (new), `filesystem.search`, `filesystem.extract`
  now requires its own same-name capability in BOTH the core
  `device_tools` contract and the agent tool table (parity gate
  unchanged, `test_device_compat` green).
- **Single-turn approval-grant leak (engine-wide, found via the new
  clipboard tests):** `approve()` minted a PolicyEngine grant that the
  approved dispatch (carrying `rec.auth`) never consumed — a later,
  unrelated request of the same tool could harvest the leftover grant
  via `decide()` and run WITHOUT fresh approval. Fix (central, no new
  system): grants mint by minter intent — non-mission approval mints
  are consumed at mint (`used_now=True`); mission-bound approval
  grants are mission-scoped (`grant(mission_id=...)` +
  `decide(mission_id=...)`), consumable only inside THAT mission's
  continuation, which is exactly the Stage 24g semantics the existing
  tests locked (park → approve once → "continue" finishes remaining
  steps). Unrelated traffic never consumes a mission-scoped grant.
- **NL vocabulary gap:** the deterministic parser had no aliases or
  structural matchers for documents/clipboard, so registered Stage 25
  tools were invisible to natural language.

### NL integration (existing routing architecture only — no second dispatcher)

- Substring aliases added under the existing `_ALIASES` table for
  `docs.search`, `docs.searchtext`, `docs.find`, `docs.read`,
  `docs.summarize`, `desktop.clipboard.read/write` (longest-match
  first as before; matching still requires the capability to be known
  from Core truth — devices/registry — nothing parser-local).
- New structural matcher `_doc_shape` (same precedent as the GUI/
  messaging matchers) for phrasings that break substring aliases:
  `summarize|explain|main points of "<name>.<ext>"` → `docs.summarize`,
  `read|open "<name>.<ext>"` → `docs.read`, `search <name>.<ext> for
  <terms>` → `docs.find`, `copy "<text>" to my clipboard` →
  `desktop.clipboard.write`, broad clipboard-read phrasings. A file
  referent must end in a supported document extension — "this/that
  document" always clarifies, never guesses.
- Param extraction stays preference-level: quoted paths, well-known
  folder words ONLY after an in/from/on/under preposition
  (Downloads/Documents/Desktop/notes → `~/X`), home/tmp words. The
  device-side allowlist (`_scoped` on the real allowed roots,
  sensitive-name refusal) remains the final authority; phrasing never
  widens scope. Kind words ("pdf files") ride the utterance: the tool
  result stays complete, and the RESPONSE wording filters kinds while
  counting the non-requested remainder honestly.
- Extension narrowing is NOT passed as a parameter (proposal params
  are scalar by contract, `check_params`); the honest alternative is
  result-side wording, not contract drift.
- Turn-level anchor flow: "copy that summary to my clipboard" resolves
  the pronoun ONLY against a verified previous result — mission steps
  first (most recent 25 missions), then a bounded single-turn result
  ring (8 entries, in-memory, data-only). The payload goes into a
  NORMAL one-step plan (engine still parks for the fresh CONFIRM
  grant); with no prior summary it clarifies instead of guessing.
- Success messages for the new capabilities report counts, provenance
  summary and honest state ("verified by read-back", "attempted; not
  verified", empty/unsupported sessions) — clipboard text is NEVER
  echoed into messages or logs; the parked record's inputs are the
  only place the payload exists, engine-side as before.

### New tool: `docs.searchtext` (cross-document keyword search)

- Composed from the EXISTING bounded primitives (docs.search discovery
  + docs.find per document) inside one device handler — no second
  engine: same allowed-root admission, same cancellation/budget, same
  untrusted-data semantics. Bounds: ≤12 docs/call, ≤8 excerpts/doc,
  ≤20 total (clamped), refusals COUNTED (`sources_refused`,
  `discovery_skipped/ unreadable`) — corrupt/credential-named files
  are never opened and never silently skipped. Every excerpt carries
  path + page-kind provenance.

### Tests (all fixture-only; real clipboard/HOME never touched)

- `tests/test_docwork_nl.py` (17): full in-process stack (real
  descriptors → resolver → policy → engine → proxy device-job → REAL
  agent-side handlers driven by a fake-agent long-poll thread; HOME
  monkeypatched to a fixture tree under /tmp; clipboard faked at the
  existing `_DETECT/_WHICH/_run_bounded` seams). Covers: end-to-end
  find-PDFs (kind-filtered wording + count of hidden remainder),
  cross-note search with provenance, summarize→bounded message,
  "read this document" ambiguity clarify, injection-stays-data (the
  seeded instruction appears only as an EXCERPT and triggers no tool),
  clipboard park-before-approval / approve → verified read with
  count-only messaging / single-use re-park / denial-cancel prevents
  execution / quoted-copy verified write / headless honesty,
  anchor-copy uses the byte-for-byte verified summary (never audit
  logged), anchor-copy-without-summary clarify, mission fetch-binding
  (summary → clipboard step), duplicate-continuation never duplicates
  the write, bounded results through `MissionStep.result`, routing
  positives + 7 false positive guards, sensitive-named-files skipped
  at discovery with honest skip counting, mission-grant does not
  authorize unrelated requests.
- Grant-scope regressions: existing `test_grant_race`,
  `test_approval_staleness`, `test_plan_continuation(+_hardening)`,
  `test_messaging_workflow` all green (mission-scoped semantics
  preserve Stage 24g; the leak is closed).
- Full suite after Stage 26: **2003 passed, 1 skipped, 0 failed,
  exit 0** (~4:58). Remaining warnings are pre-existing third-party
  deprecations (websockets legacy in `test_device_challenge`; wake
  model in `test_wake_oww`) — none from Stage 26 code.

### Real vs fixture validation (honest)

- Everything above is FIXTURE-ONLY (in-process fake agent + fake
  clipboard backends; no network, no real clipboard access, no
  notifications, user HOME unread by tests). No real Wayland/X11
  backend validation was performed for the NL path in this stage (the
  Stage 25 backend behavior is unchanged; its live evidence status
  carries over unchanged).

### Known limitations (honest)

- Deterministic routing covers unambiguous phrasings; anything else
  falls through to clarification or the existing hosted LLM path
  (which may propose the same capabilities; grounding + policy remain
  the authority for it too).
- Mission-scoped grants: an approved plan record's grant can complete
  AT MOST one additional step (the next park) — multi-hold plans
  re-park per step, by the Stage 24g sequential-holds semantics.
- `docs.searchtext` per-call doc budget (≤12) is a documented limit;
  a larger tree reports truncation rather than scanning silently.
- Kind-filtered response wording only applies to the search phrasings
  where kind words are adjacent to files/docs/documents ("the word
  budget" never narrows); standalone polysemous words are excluded.

## 4r. Stage 27 — unified task execution, truthful verification, recovery evidence (same day)

### What the lifecycle audit actually found (and fixed)

- **Document tools claimed verification with zero checks:**
  `docs.search/read/find/summarize/searchtext` declared
  `verification="none"`, so the engine latched `verified=True` on any
  non-exception return and fabric's post-execution verdict was literally
  `succeeded (no output contract)`. Fix (Stage 27): every docs tool now
  declares a fixed result CONTRACT (`DOCS_OUTPUT_SCHEMAS`, one shared
  constant imported by BOTH core and agent defs so parity is
  structural) and runs `verification="output_schema"` — the registry
  validates the full result shape (a malformed agent result now fails
  the step with `output failed verification`) and fabric's post-check
  verifies the required keys (counts, refusals, truncation, provenance
  backbone). Malformed results are FAILED steps, never silent
  successes.
- **Clipboard records overclaimed verification:** both clipboard tools
  declared `verification="none"` while their handlers self-report
  read-back truth in `result["verified"]` — the `explicit` mode never
  engaged, so a failed read-back STILL produced `rec.verified=True` at
  the record level. Fix: `verification="explicit"` on both sides; the
  record now carries the handler's verdict honestly (write verified by
  read-back; a read reports `verified: None → False` because a read
  has no read-back by design).
- **Verification verdicts were invisible at the mission level:** the
  mission store kept status "ok" but dropped executed-vs-verified.
  Fix: step outcomes now carry `verified` + the fabric `verification`
  reason verbatim; verdicts are persisted into the resumable plan
  (`plan["verdicts"]`, written at execution/adoption time) and ride
  back into rebuilt continuation outcomes; a fully-done mission
  message appends `Succeeded but NOT independently verified: …`
  whenever an ok step reported an explicit not-verified verdict.
- **Clipboard text could persist through the mission store:** the
  clipboard contract says "no polling, no history, no persistence",
  but an approved mission clipboard read stored its full `text` field
  in the (SQLite-backed) mission record. Fix: storage-boundary
  withholding (`_STORAGE_WITHHELD_KEYS`) — stored/restored step
  results keep a bounded honest note
  (`<withheld from the stored mission record: N characters>`); the
  in-flight result (fetch bindings, approval fingerprint) is
  unaffected, and the disclosure is checkable in tests.
- **Device-side error text was discarded:** the mission failed-branch
  detail used only the stage fallback, so `RuntimeError: boom` /
  `device agent went away` vanished from the stored step record. Fix:
  the real error (`out["error"]`) is carried into the outcome and the
  stored step error (wording `step N: failed: <cause>`).

### Verified lifecycle (unchanged ownership — audit only confirmed)

request → parser/resolver (Core-known capabilities only) → policy +
governor → engine record (park-or-run; mission-bound holds) → device
proxy via bounded device jobs → handler-side allowlists/bounds →
registry output verification → fabric `verify_device_result` →
mission step checkpoint (result + verdict, crash-consistent persist)
→ continuation (single-owner, record inspection, never repeat) →
final message distinguishing executed / verified / partial / stopped.
Authorization is rechecked per step (`_prepare` decides with the
mission binding; Stage 26 mission-scoped grants are consumable only
inside their own mission); a denied step is never dispatched.

### Recovery + concurrency guarantees (with tests)

- Crash windows: before dispatch (park, nothing ran), after dispatch
  before result (resume refuses "has not finished"), missing/ pruned
  record (resume refuses "no longer retained" — uncertain outcomes
  are NEVER replayed), after result before checkpoint (adoption hook
  + per-step persist, Stage 24h, re-asserted), terminal states
  (FAILED/COMPLETED/CANCELLED cannot be reopened by resume).
- Concurrency: single-owner continuation lock (existing hardening
  suite) plus new duplicate-continuation regression on real handlers;
  cancelled missions stay closed; completed work is not re-run
  (exactly-one-job counts asserted per tool).

### Measured behavior (fixture-level, in-process; `tests/test_perf_budgets.py`)

- resolver.resolve_proposal: median **0.37 ms**, p95 **0.44 ms**
  (n=200) for a known capability name — one resolve per mission step
  (exactly 1.0, counted), so no duplicate/quadratic re-resolution.
- 2-step all-SAFE mission turn (real handlers end-to-end): median
  **3.2 ms** (n=20).
- docs.searchtext turn (discovery + 2 documents): median **3.2 ms**
  (n=8); discovery is bounded per call by documented caps, never
  cached across permission boundaries.
- Bounds are deliberately generous regression detectors; numbers are
  fixture-level (no real device latency involved).

### Tests

- `tests/test_task_workflows.py` (12, new): Workflows A–E on the real
  pipeline (fixtures for HOME/clipboard/agent loop): A research chain
  with provenance survival through the mission store + parked
  protected step; B deny → honest FAIL without dispatch, approve →
  continuation without repetition (verdict survives), grant scoped to
  its own mission only; C agent-reported failure never replayed,
  malformed result fails verification, record-lost/refuses dispatch
  while unknown; D duplicate + terminal continuation refusals,
  cancelled stays closed; E injection stays data through a fetch
  binding (missing-key structured refusal), payload held internally
  only, clipboard text absent from mission store AND audit — with
  read-back counts proving exactly-once dispatch.
- `tests/test_docs_clipboard_contracts.py`: +2 — verification modes
  declared and in parity (docs = output_schema with required keys +
  output budgets; clipboard = explicit on both sides); malformed
  result fails validation.
- Contract round-trips updated to contract-conforming markers (the
  pipeline now (correctly) rejects arbitrary result shapes).
- Full suite: **2,020 passed, 1 skipped, 0 failed, exit 0** (~5:02).
  Warnings remain pre-existing third-party deprecations.

### Real vs fixture validation (honest)

- Everything above is FIXTURE-ONLY (in-process fake agent + fake
  clipboard backends; no network, no real clipboard, no notifications,
  no user HOME reads). No real Wayland/X11/Android validation was
  performed in Stage 27; prior-stage evidence carries over unchanged.
- Clipboard-read text persistence is withheld from the mission DB by
  design but payloads DO live in transient engine records (required
  for approval fingerprints and fetch bindings — in-memory only).


## 4s. Stage 28 — Android automation completed + bounded evidence
## traces (same day)

Follow-up to the ARTEMIS assessment (§4r / Round 11): ARTEMIS is NOT an
integration target (autonomous LLM loop, cloud perception, ≥3.12), but
two of its patterns were adopted WITHOUT the runtime, on the existing
single-authority architecture. No new agent loop, no second scheduler,
no new dependency.

### What the rerun audit found (already built, now completed end to end)

The Android surface already had read-only ADB observation
(device/adb_linux.py, Stage 24b), paired-agent GUI executors
(gui.screen.inspect/tap/set_text/scroll/find/back with lab allowlist +
snapshot-bound refusals in android/lib/job_runner.dart), Core identity/
jobs/policy/missions. Gaps this stage closed:

1. **No host-side phone inventory**: ADB-based discovery existed but
   was wired to NO registered capability. New `system.android.inventory`
   (Linux agent, `device/linux/android_adb_linux.py`): bounded serial/
   state/model inventory via the existing adb layer. Honest absence —
   "adb not on PATH" and "adb present, nothing attached" are
   detection results (adb=false / count=0 + note), never fabricated.
   Output contract (`ANDROID_OUTPUT_SCHEMAS["system.android.inventory"]`) is
   shared with the agent side and enforced as
   `verification="output_schema"`: a malformed inventory is a FAILED
   step, not a silent success. Inventory is explicitly NOT
   authorization (note says so in the result).
2. **No bounded wait-for-element**: new `gui.wait` — read-only polling
   of the fresh accessibility tree with a HARD total-time bound
   (≤15 s schema-clamped; fixed 500 ms interval; never busy-spins),
   implemented in the Dart runner (reuses the a11y-find channel, no
   new native bridge). A timeout returns `matched:false, verified:false`
   plus the attempt/wait counters — the wait RAN, the element simply
   never appeared; never ok:false, never a guess, never a retry. The
   wait never acts (the runner has no tap/type/scroll probes in its
   construction path for waits; acting still flows through gui.* with
   their own approval). Registered on BOTH sides (core + Dart
   descriptors/aliases); lab allowlist mirrored byte-equal.
3. **No evidence traces for Android operations**: Stage 27's verdict
   plumbing now carries a BOUNDED evidence projection for `gui.*`
   steps (`_android_evidence` allowlist: package, verify_state,
   applied, matched, attempts, waited_ms, snapshot ids, bridge codes,
   device, capability, verdict — one string cap each). It rides the
   step outcome AND is persisted with the verdict in `plan["verdicts"]`
   (success path, adoption hook, failure path) so the record survives
   approval/resume/restart. Evidence NEVER contains element text,
   content descriptions, typed text, or clipboard material; the audit
   trail stays id-shaped (tests assert the label is absent from both).
   A matched condition is evidence for THAT condition — never proof of
   the whole task and never a permission.
4. **NL routing false-positive fix**: "inspect my connected android
   phone" / "list connected android devices" answer FROM THE LAPTOP
   (adb runs here); the phrase's "android" words describe the target,
   not the executor, so grounding no longer mis-resolves to an (often
   absent) phone device (same suppression precedent as "mobile data").
   Structured wait phrasing (`wait until "X" appears on <lab package>`
   [+ `within N`]) requires a quoted element AND a named lab target —
   anything less clarifies; plans stay scalar except the documented
   bounded `target` selector object (docs/UI_SNAPSHOT_CONTRACT §8).

### Authorization / safety (unchanged ownership — extended, not weakened)

- Every phase-2 surface executes through the SAME pipeline: resolver →
  policy (confirm family still parks for approval; `wait` is no
  exception) → engine record → device-job proxy → allowlisted handler
  → registry input contract → fresh evidence verdict.
- Denial cancels; cancellation stops future dispatch; a non-lab
  package can never dispatch (schema enum refuses before enqueue, and
  even a hypothetically approved hold fails validation with no job
  enqueued). A missing element is not guessed; ambiguity surfaces with
  actionable codes (TARGET_NOT_FOUND / TARGET_AMBIGUOUS / stale /
  package-changed) per the existing contract.
- Uncertainty stays honest: a timeout is `matched:false` — no auto
  retry, no uncertain side effects to replay (the wait has no side
  effects by construction).

### Tests

- `tests/test_android_workflows.py` (11, new, on the REAL pipeline —
  real agent-side handlers + fake a11y world + faked adb seam):
  honest inventory (phone attached / adb-missing / none-attached with
  truthful notes), end-to-end NL wait (approve → park → adopt →
  matches; evidence rides outcome + verdict store), timed-out wait
  (matched:false, verified:false, note + counters), structural
  false-positive rejection (no payload/package → clarify; docs
  routes untouched), tap authorization HOLD (no dispatch), denial
  prevents dispatch, non-lab package never enters the queue (even
  after approve, no job), allowlist refusal with actionable failure,
  two-step mission (wait→tap) with three truth properties: adoption
  without repetition (one tap job total), step-1 evidence survives
  continuation, verdict store + audit carry no element labels, and
  cancelled mission closes FAILED with zero tap dispatch + terminal
  refusal; contract-parity test (schemas shared by identity, enums
  mirrored, verification modes)).
- `android/test/stage28_wait_test.dart` (6, new): match/immediate
  evidence, honest timeout semantics, lab allowlist mirror refusing
  without touching the probe, terminal structural refusals fail
  honestly, missing gateway/target, and absence-proof that the wait
  has no acting probes. `flutter analyze`: 0 errors/warnings in the
  new code (11 pre-existing info lints in an untouched file).

### Real vs fixture validation (honest)

- ALL Python evidence is fixture-level (in-process fake paired agent,
  fake adb) — consistent with every prior Android stage; the Dart
  runner logic is validated by unit tests + flutter analyze only (no
  emulator run).
- `flutter analyze` post-change: 0 errors/warnings in ALL Stage 28
  code; the only lints left are 11 pre-existing `info` items in the
  untouched `test/universal_operator_test.dart`. `flutter test` (whole
  suite as this stage needs it): 6/6 new wait tests + the pre-existing
  suite set pass.
- Full Python suite (rerun after all Stage 28 fixes): **2,031 passed,
  1 skipped, 0 failed** (~5:07; baseline 2,020 + 11 new). Regression
  fixes applied en route (honest): the first full-suite run caught
  three pre-existing contract tests that this stage's first cut
  violated — the namespace rule (`android.*`/`gui.*` must never
  resolve on the Linux agent; host inventory renamed to
  `system.android.inventory`), the proxy-wait rule (every Core→device
  proxy timeout MUST exceed the worst-case device poll gap;
  `gui.wait` proxy wait raised to 90 s while the executor's own bound
  stays ≤15 s), and schema identity/non-copy semantics in the parity
  test — all fixed and the suite re-run to green.
- `./gradlew :app:compileDebugKotlin`: **FAILS on 12 pre-existing
  errors in UNCOMMITTED PRIOR-STAGE work — untouched by Stage 28 and
  unrelated to it (Stage 28 changed no Kotlin code)**:
  `android/android/app/src/main/kotlin/dev/zara/zara_android/DeviceBridge.kt`
  (531 uncommitted insertions predating this stage) references HIDDEN
  APIs missing from the public SDK jar (`VolumeInfo`, `StorageManager
  .volumes`, `Display.realMetrics`), has a constant typo
  (`USB_CLASS_CSC_DATA`), an internal type mismatch, and a syntax
  error around line 1732 (`battery()` body statement run-on). Per the
  preserve-uncommitted-work rule this file was NOT modified in Stage
  28; apk builds and on-device validation of the GUI surface are
  blocked on reconciling that prior work against this SDK (compileSdk
  from flutter, platforms 35/36/36.1 installed).
- Real-device validation: **BLOCKED — no Android device/emulator is
  attached (`adb devices` lists zero devices at implementation and
  re-check time)**. Nothing was installed, no settings changed, no
  permissions granted, no ScreenCast dialog launched. The most
  important remaining blocker for daily Android use is exactly this:
  a paired phone running the Zara Android client with an enabled
  accessibility service, so the gui.* evidence surface can be
  validated against live snapshots (end-to-end real-device run) —
  and, before that, the DeviceBridge.kt build reconciliation above.


## 4t. Stage 29 — mined hardening: never-auto-approve class + explicit
## gui verification (same day, OpenDroid follow-up)

Follow-up to the OpenDroid assessment: two mined patterns were adopted
on the existing single-authority architecture, no runtime, no new
dependencies, no shell/root surface: (A) a HARD **never-auto-approve
class** — standing/ambient grants can never unlock irreversible
actions (OpenDroid's never-auto list idea, made enforceable); (B)
**explicit verification for the mutating gui.* family** — "an ack from
the device is not proof"; the device's OWN re-read confirm is the
verdict (OpenDroid's verify-after-act idea, generalized beyond taps).

### A. Never-auto-approve class (core/policy.py)

- `NEVER_AUTO_APPROVE_CAPS` = {`android.app.force_stop`,
  `storage.delete`, `filesystem.delete`, `messaging.send`} —
  membership on the BARE capability name, so `tool:`/`capability:`
  spellings hit the same rule. Enforcement in `decide()`: these
  capabilities IGNORE the grant store entirely and always require
  explicit per-use human approval, BEFORE the risk branches (so a
  future SAFE/CONFIRM relabel cannot widen them — mislabel protection;
  HIGH_RISK labels were already grant-immune). Not a hard deny: the
  real approval paths still complete.
- Why the approval paths are safe: engine `_prepare` uses
  `decision = auth or self.policy.decide(...)` — the per-use approval
  dispatches with INJECTED auth (engine mint inside
  `_grant_checked`, mission-bound mint in messaging
  `_step_decision`), never by re-consuming a grant from the store.
  Both legitimate minters are now tagged with provenance:
  `policy.grant(..., source="approval")`; every other mint defaults
  `source="standing"`. The grant store therefore can never bless a
  never-auto action — not standing mints, not even leftover
  approval-sourced ones (tests assert `decide()` never consumes any).
- `ToolDefinition.never_auto_approve: bool` added; BOTH `_def`
  builders (core `device_tools.py` and the linux agent-side
  `tools_linux.py`) DERIVE the tag from policy-set membership — the
  policy set is the single source of truth, and a registry test
  cross-checks the tags on both sides. Zara had no grant-liftable
  dangerous tool before this stage (HIGH_RISK already held); the class
  makes that invariant durable against future relabels and closes the
  CONFIRM-class holes (deletions previously auto-allowable by a live
  standing grant).
- `messaging.send` is a record member: its gateway already approves
  every send per-use through its own dedicated protocol; holding a
  set entry protects any future registered tool of that name.

### B. Explicit verification for the mutating gui.* family

- Core defs `gui.tap` / `gui.set_text` / `gui.scroll` / `gui.back`
  now declare `verification="explicit"` (read-only inspect/find/
  wait keep their existing modes). Read-only tools already
  `explicit` (wait, Stage 28) or contract-verified; the mutating
  family previously relied on the ack (`applied:true`) — Stage 27's
  lesson said that is a claim with zero checks.
- Dart runner (`android/lib/job_runner.dart`): `_guardedA11y` takes a
  `verifyMatch` matcher over the NATIVE `verify_state`; the success
  result ALWAYS carries a top-level `verified` key. Mapping (per the
  native contract): tap `verified = (verify_state == 'clicked')`;
  set_text `verified = startsWith('text_matched')` — applied-notes
  (content changed, sensitive field not re-read) stay unverified;
  scroll NEVER self-verifies (no automatic native re-read; its
  in-contract verify is the caller's snapshot-content diff);
  back `verified = fresh post-navigation snapshot present &&
  package_after observed`. Runner-side default labels
  (defaultVerify) are labels only, never proof — native omitting
  verify_state yields the empty string, which matches nothing
  (Dart test asserts the ack-only shape stays `verified: false`,
  the key explicitly present).
- fabric's explicit verifier now AUDITS `verification_failed` on the
  device id when the flag is absent (parity with the output_schema
  path) — a misbehaving device agent is diagnosable from the audit
  alone. The verdict reason is actionable either way ("explicit
  verification flag absent in result").
- Evidence plumbing: `_ANDROID_EVIDENCE_KEYS` gained
  `fresh_snapshot_id` (back's confirm evidence); the bounded evidence
  projection already carries the verdict + `verify_state`, so mission
  records now show executed-vs-verified for Android steps honestly.

### Tests

- `tests/test_s29_mined_hardening.py` (13, new): standing grant cannot
  unlock any class member (grant visibly unconsumed); prefix
  spellings and a SAFE relabel fail closed while keeping approval
  possible; non-class CONFIRM tools keep the grant-consumed flow (no
  overreach); approval-sourced leftovers are never consumed by
  `decide()`; the REAL per-use approval path completes for a
  never-auto CONFIRM tool (policy-stub engine: park → approve → ran
  exactly once, with the approval provenance mint tagged); registry
  tags match the policy set on BOTH def builders; tap approved once
  with the device-owned verified flag latched (record + fabric
  verdict); the old ack-only device shape is honestly
  `verified:false` (state succeeded — the tap happened — but
  unverified, reason names the contract); back keys on the fresh
  post-nav snapshot; scroll's false verdict; failed steps never
  verify; flag-absence is audited.
- `android/test/stage29_verified_test.dart` (6, new): native
  clicked→verified, native omitting verify_state→never verified
  (key present, false), set_text text_matched→true vs applied-note
  →false, scroll never self-verifies, back fresh-snapshot
  verify true / missing→false.
- Regression sweeps: the Android/policy/execution/messaging/desktop/
  scratch/approval-staleness set (196 passed) and the
  plan-continuation/universal/docwork/intent/a11y set (149 passed)
  stayed green; the ack-only fake shapes elsewhere now honestly
  produce `verified:false` records (no assertions relied on the old
  latch).

### Honest validation boundary

- Same as Stage 28: fixture-level only (in-process fake paired
  agent + fake a11y world); the Dart runner logic is unit-tested and
  analyze-clean with NO emulator or real-device run; nothing new was
  installed and no device was touched. No integration of the
  OpenDroid source tree (its Kotlin/agent-runtime remains out as
  assessed); only the two patterns above were adopted, on Zara's
  own lines.


## 4u. Stage 30 — read-only observation stops prompting (same day,
## dummy mining, buildable without a device)

The mined approval-bucket principle formalized: approval prompts are
currency — spending them on harmless reads trains rubber-stamping and
destroys their value for everything dangerous.

### What shipped

- `core/policy.py`: new policy set `READ_ONLY_AUTO_ALLOW_CAPS` =
  {gui.screen.inspect, gui.find, gui.wait, android.notifications.read}.
  `decide()` now allows members WITHOUT approval — membership-based,
  POLICY-level, checked on the bare capability name; the tool risk
  labels are intentionally UNCHANGED (still CONFIRM; the policy, not
  a relabel, is the mechanism). Reason string:
  "read-only observation: policy-level allow, no approval currency
  spent".
- Branch order (leak guards): DENY_PATTERNS outrank everything;
  NEVER_AUTO_APPROVE_CAPS outranks read-only membership (a member that
  ever joins the dangerous class parks again regardless of membership
  here); the read-only branch returns allow=True and touches the
  grant store zero times (no grant needed or consumed).
- Derived tagging parallels Stage 29: `ToolDefinition.
  read_only_auto_allow` = derived from membership in BOTH `_def`
  builder passes (single source of truth, never hand-maintained).
- Approval kept exactly where it is: gui.tap/set_text/scroll/back,
  storage.delete, filesystem.delete, android.app.force_stop,
  messaging.send, clipboard write, xfer/transfer, screen-cast
  consent — all still park and still prompt.
- Evidence/verdict semantics untouched: the Stage-30 allow is NOT a
  verification claim; only the Stage-28 native re-read verifies the
  mutating family and only per Stage-29 rules.

### Tests

- `tests/test_s30_read_only_allow.py` (9, new): family allows with no
  grants; allow never consumes the grant store; members exist as real
  registered defs with derived tag true; two-way tag parity;
  never-auto outranks membership (monkeypatched regression guard);
  mutating set still parks under identical CONFIRM labels; deny
  patterns outrank read-only; sets disjoint; full decision matrix
  snapshot.

### Honest validation boundary

- Laptop-side, no device involved (fixture policy tests only; the
  Android surface itself is unchanged). Runtime validation of the
  no-prompt behavior on-device rides the same emulator/real-device
  gate as Stage 28's phone work.

## 4v. P0 — DeviceBridge reconciliation + FIRST paired-device run
## (same day, Pixel_8 emulator)

The 12 prior-stage compile errors in the uncommitted `DeviceBridge.kt`
were reconciled against compileSdk 36 WITHOUT discarding the work
(preserve rule) — every fix is a public-API replacement, never a
behavior invention:

- hidden `android.os.storage.VolumeInfo` (import + type + `.file` +
  `.fsUuid` + `.fsType`) → public `StorageVolume`
  (`storageVolumes` / `directory` / `uuid` / `description(ctx)`),
  same "skip primary internal" rule expressed as
  `isPrimary && !isRemovable`; fstype honestly "unknown".
- hidden `UsbConstants.USB_CLASS_CSC_DATA` → literal class code 0x0A
  (comment says why; CDC data now honestly labelled `cdc_data`, not
  mislabelled usb_storage).
- gone-from-public-SDK `Display.realMetrics` → public
  `Display.Mode.physicalWidth/Height`.
- truncated `mediaCounts()` body (the line-1732 syntax error was its
  orphan tail) REBUILT to the documented contract:
  permission-gated (READ_MEDIA_* API 33+ / legacy below) with count
  only queries, absent permission reported as `available:false` +
  permissions_needed — never faked; primary-storage stat from an
  app-visible path with honest error key.

New validator-surfaced bug (found BY the emulator run — exactly what
the emulator lab is for): MethodChannel nested maps decode as
`Map<Object?, Object?>` and `DeviceObservedPanel`'s
`List.cast<Map<String, dynamic>>` threw at render → red-screen box.
Fix: `_deepStringMap` deep conversion in `device_bridge.dart`
`observedDevices()`; values preserved verbatim (no faking). Analyze
clean.

### Validated on the Pixel_8 emulator (real runtime evidence)

- `./gradlew :app:compileDebugKotlin` BUILD SUCCESSFUL;
  `flutter build apk --debug` ✓; `adb install` Success.
- Launch: MainActivity fully drawn, no crash; audio self-test,
  assistant-status, voice-support, secure-backend keystore all logged.
- Pairing end to end: operator enroll → app claim (pairing-code TTL
  honored — one genuinely-expired code was honestly refused) →
  register → Ed25519 challenge-proofs steady (register/heartbeat/
  capabilities all 200).
- Core → device async round trip: POST /v1/executions 201 → app
  claimed the job → native battery bridge ran → POST jobs/result 200
  → core records **succeeded (done), runs on emu-pixel8 (verified),
  result {battery_pct 100.0, charging false, power_save false}**.
- Devices panel renders the honest observed-device wisdom
  (SDCARD free/total, "Detection is not trust" note); device action
  round trip through /v1/talk works (401→operator token entered via
  Tasks tab; unreachable→running core; all transitions HONEST,
  never faked).
- Boundary by design: a11y service is opt-in and was not enabled;
  gui.* surfaces remain fixture-validated (Stage 28/29 discipline).
  No real phone attached — physical-hardware-only claims (camera
  flash, sensors) stay unvalidated.

## 4w. Stage 31-P2 — laptop system control: volume / mute / brightness
## + MPRIS playback (same day, buildable + live-validated today)

P2 shipped as ONE coherent tool family with the same discipline:
user-perceptible state changes are always verified by an immediate
RE-READ, and the verdict comes from the re-read — exit status alone is
never "applied".

### What shipped

- `device/linux/syscontrol_linux.py` (new): single `_run` seam
  (shell=False list argv throughout; fakes/injection patch this ONLY).
  Handlers: `volume_set` (level 0-100 or ±delta points; wpctl
  set-volume with target clamp; before/after re-reads; target
  honored → "applied", re-read disagreement → "not-applied",
  refusal → reported plainly), `mute_set` (on/off/toggle; toggle's
  verdict IS the observed flag flip), `_read_brightness`/`
  brightness_set` (sysfs backlight API with per-device max scaling,
  honest `permission-refused` verdict on EACCES — this laptop's
  backlight happens to be video-group-writable, stated, never
  relied on), `_mpris_players`/`_mps_property` (busctl --user, no
  daemon discovered or installed), `now_playing` (honest empty),
  `playback_control` (transport actions verified by PlaybackStatus
  re-read; Next/Previous CANNOT be confirmed via status — declared
  "sent" honestly, never a fabricated "changed track").
- Core defs mirrored (`core/device_tools.py`): audio.volume.set /
  audio.mute.set (CONFIRM, caps audio.volume), display.brightness
  (SAFE/PUBLIC) / .set (CONFIRM), media.now_playing (SAFE) /
  media.playback (CONFIRM, caps media.playback) — devices=["linux"].
- Device defs mirrored (`device/linux/tools_linux.py`): same names,
  same confirm contracts; capabilities derive automatically
  (audio.volume, display.brightness, media.playback now in
  LINUX_CAPABILITIES).
- Intent routing (`core/intent.py`): louder / turn it up / quieter /
  volume up|down → audio.volume.set; mute the laptop / unmute →
  audio.mute.set; dim / brighten the screen → display.brightness.set;
  laptop brightness → display.brightness; pause/resume the music /
  next track → media.playback; what's playing → media.now_playing.
- Prompts stay a currency: all four SET surfaces are
  user-perceptible state → they correctly PROMPT (policy matrix
  pinned in tests). Only the two read surfaces are SAFE.

### Tests

- `tests/test_s31_p2_syscontrol.py` (19, new): applied-by-re-read;
  not-applied when the re-read disagrees despite exit 0; delta math;
  no-confirm refusal; honest unavailable (wpctl / backlight / MPRIS);
  mute on/off/toggle verified; brightness apply + real-EACCES
  permission-refused (0444 file, no pathlib patching) + no-device;
  play_pause verified status change; next/previous honest "sent";
  both-side registration parity; policy matrix; intent table;
  LIVE non-destructive round-trip (set volume to its CURRENT value;
  wpctl honored; re-read verdict "applied" at unchanged pct).
- Regression slice green: stage2 + docs/clipboard contracts +
  governor + capabilities + intent + calibration = 169 passed.
- Live evidence on this laptop: now_playing honest-empty (no MPRIS
  player this minute — plain answer, nothing faked); backlight read
  works (currently 0% — panel dimmed/sleeping; deliberately NOT
  written, hence no live set evidence for brightness — honestly
  recorded as untested-on-live-hardware, fixture-covered).

### Honest validation boundary

- wpctl volume path live-validated (non-destructive live test);
  MPRIS validated against fixtures + honest live-empty (no player
  was running at validation time); brightness live-read validated,
  live-write NOT exercised on this machine's panel (would have lit
  the user's sleeping screen — an un-requested state change; the
  fixture + real-EACCES paths cover the machine-independent logic).
  No new dependencies were installed; nothing runs in the
  background.

## 4x. Stage 31-P3 phase A — quick/one-shot phone actions
## (2026-10-22)

### Scope shipped (8 device surfaces, one job channel, zero new deps)

Rides the EXISTING Stage-28 job channel end to end — no second loop:

| Tool | Native surface | Verdict semantics |
|---|---|---|
| `android.torch.set` | `CameraManager.setTorchMode` (no camera permission) | applied / not-applied / **unverified** — a re-read that only FAILS TO CONFIRM is unverified (rides `ok:true` with verdict data); a re-read that REFUTES stays a real failure. Never a manufactured applied claim. |
| `android.volume.set` | bounded `STREAM_MUSIC` clamp via `setStreamVolume` | `before_pct`/`after_pct` from a `getStreamVolume` re-read → verdict applied/not-applied |
| `android.ring` | `MediaPlayer` alarm playback (bounded seconds), vol max | rang:true/-:false; refuses inside DND without an explicit per-run `allow_dnd_bypass`; `SKIP_UI` refused structurally |
| `android.media.playback` | media `KeyEvent` dispatch (play/pause/next/previous) | honestly **"sent"** — a dispatched key is not a verified playback change |
| `android.clipboard.set` | `ClipboardManager` write | applied via primary-clip re-read COMPARE; text never echoed (length only); device-core honest-fails when the app lacks window focus (Android rule, surfaced verbatim) |
| `android.open_uri` | `ACTION_VIEW` + `queryIntentActivities` handler check | schemes bounded (http/https/mailto/tel/geo/market/content/file); verdict "sent" is NEVER claimed as verified |
| `android.power_saver.set` | `PowerManager` + `Settings.Global` dual write | dual re-read → applied/not-applied; needs the one-time T1 `WRITE_SECURE_SETTINGS` grant |
| `android.alarm.set` | `ACTION_SET_ALARM` (permission `com.android.alarm.permission.SET_ALARM`) | **handed-to-clock only**: the clock opens pre-filled and the USER completes the alarm; `SKIP_UI` refused structurally |

### The verification contract (`verification="explicit"`)

Found live: core's `verified` flag defaulted `bool(out)` for the whole
device family → a torch result with verdict "unverified" displayed
`verified:true`. Fixed at both ends:

- All 8 defs are now `verification="explicit"`; the device runner
  carries a top-level `verified` boolean per verdict: applied-class
  tools (volume/clipboard/power_saver) → `verified=applied`; ring →
  `verified=rang`; torch → `verified=(verdict=="applied")`; media keys,
  open_uri, alarm → **`verified=false`** (sent/hand-off is not proof).
- Live re-run: torch E2E now reports `succeeded / verified:false /
  verdict:unverified` — the honest emulator boundary as DATA.

### Timing-invariant repair (T0 caught it live)

`test_T0_proxy_timeout_exceeds_max_device_poll_gap` failed on the
P2/phase-A rows ≤60 s (`display.brightness(15)`, `media.now_playing(15)`,
`media.playback(15)`, and eight 30/60 s phase-A rows). Live cost shown by
`android.open_uri` `timed_out after 30.0s / job expired` when the app's
25 s long-poll wasn't already waiting. All 12 rows restored to the
documented ≥61 s bound (90 s).

### Package-visibility discovery (Android 11+)

First live `android.alarm.set` honestly abstained: "no clock app handles
ACTION_SET_ALARM" — `pm query-activities` proved Google Clock DOES
handle it. Root cause: apps targeting API 30+ see NOTHING without a
`<queries>` declaration. Manifest now declares exactly the two match
intents Zara resolves (`SET_ALARM`, `VIEW`+https) — grants nothing.
Second live run: clock opened pre-filled 7:30 "zara-e2e" (screenshot
evidence), verdict handed-to-clock; the test alarm was toggled OFF
afterwards as cleanup.

### Live E2E evidence (emulator gate `emu-pixel8`)

- Submit → park (`waiting_for_permission`) → approve (Bearer) → device
  claims → run → honest verdict, for FIVE tools live:
  - torch: `succeeded / verified:false / verdict:unverified` (virtual
    camera — documented honest boundary, NOT claimed as applied)
  - volume: `before 33% → after 73%`, applied+`verified:true` (real
    OS re-read)
  - clipboard: applied+`verified:true`, length 21 correct, text never
    echoed (app foregrounded — the focus dependency proven live)
  - alarm: handed-to-clock, `verified:false`, clock opened pre-filled
  - open_uri: "sent", `verified:false`, Chrome opened on the device
- Registration integrity: pairing re-established via the SANCTIONED
  enroll→claim flow (the devauth store was found EMPTY after a mid-session
  restart — origin unclear, likely /tmp lifecycle; nothing was deleted by
  hand; re-pair honored the 10-min TTL).

### Stage-30 fallout sweep (full-suite disclosure)

The Stage-30 read-only auto-allow policy was shipped with slice
regressions only; the first FULL suite run since (this stage) exposed
21 stale expectations + the two `test_android_workflows` wait tests.
All were pre-Stage-30 park assumptions, not logic regressions; each was
rewritten to the current policy while keeping its core discipline
claim (no new dispatch, honest verdicts, secret-free audit, replay/TTL
proofs re-pinned on still-parking `gui.tap`). Files updated:
`test_async_exec.py` (A08/A14/A19), `test_job_timing.py` (T0/T5),
`test_mission_wiring.py` (W08), `test_resolver_async.py` (R02),
`test_retention_ttl.py` (R07/R17), `test_retention_http_burst.py`
(H01), `test_rung2.py` (U3/H1/H3/N5/N10/N12/R1/P1),
`test_universal_workflows.py` (N2/N3 + await_rec helper),
`test_android_workflows.py` (gui.wait ×2, mission two-step).

### Runtime discipline

- Alarm Claims the hand-off, never the alarm; ring never bypasses DND
  uninvited; clipboard result is length-only; open_uri scheme-bounded
  with a handler-existence check; power saver re-verifies per change
  (T1 adb grant, SYSCONTROL_MATRIX pattern); media keys honestly "sent".
- Emulator torch framing: unverified is RECORDED AS DATA — real-phone
  claims remain gated on physical hardware (unchanged boundary).

## 4y. Stage 31-P3 phase B — P3d call with contact resolution
## (2026-10-22)

### Scope shipped (P3d, the user's standing verbatim spec)

End to end on the EXISTING job channel (no second loop, no new deps):

- **App (Kotlin `CallActions.kt`)**: `contactsLookup` (bounded LIKE search,
  display name + number + starred only — nothing else ever leaves the
  contacts DB; missing READ_CONTACTS is an honest refusal);
  `callDirect` (ACTION_CALL after a dial-grammar re-check — a number
  with characters outside `[+0-9*#,,]` is refused even if Core were
  bypassed; missing CALL_PHONE refuses rather than silently falling
  back to the dialer); `callScreenPoll` (250 ms × 4 s bounded a11y
  focused-window re-read for the call screen). Manifest: READ_CONTACTS
  + CALL_PHONE (both DANGEROUS — granted by the operator via adb in the
  lab, runtime-requested at surface enablement never silently).
- **Dart**: bridge methods + two runner cases. The call case NEVER
  reports the full number — results carry a tail (`********0100`)
  written in the RUNNER, so the approval prompt is the only place the
  digits exist. `verified` is the a11y poll's verdict (`confirmed`),
  NOT the firing — a launched intent is not proof, and absence is
  honest data riding `ok:true`.
- **Core defs**: `android.contacts.lookup` (CONFIRM×CONFIRM, cost 0.08)
  / `android.call.place` (HIGH_RISK×CONFIRM, cost 0.15) —
  verification="explicit", timeout 90 s each.
- **Core flow (`core/call_flow.py`)**: a deterministic turn flow
  injected into IntentTurnService (the reminder-path precedent), with
  ring-of-one pending state (10 min TTL, machine-local, honest about
  non-survival across restarts). Spec-critical behaviors:
  - lookup → 0 matches asks "saved under a different name?"; the
    user's "saved as Y" drives the SECOND lookup; a hit writes the
    alias fact to memory with type `contact_alias` — CONFIRMED by that
    ask, never inferred — and future "call x" resolves THROUGH the
    alias memory before any device contact is touched;
  - 1 match → "Calling <name> (<tail>) — correct?" (the correctness
    confirm the user decided 2026-10-20); "yes" submits
    `android.call.place`, which STILL parks for its own per-call
    approval — both gates before any dial;
  - multiple matches → numbered disambiguation (never a guess);
  - raw numbers from chat text are refused at every layer;
  - every step goes through `engine.submit` — policy/trust/presence/
    approval/audit all apply; the flow adds no authorization path;
  - pending execs are DRAINED at the next turn (parked lookups/calls
    complete asynchronously after in-app approval; the flow reports
    what actually happened, never what was hoped).

### Fixture tests

- `tests/test_call_flow.py` (7): park→consume→confirm with masked tail;
  alias-ask→second-lookup→confirmed-memory write (metadata pinned);
  yes→per-call gate holds BEFORE dispatch→confirmed verdict→the full
  number is absent from results AND the audit stream; fired-but-absent
  reported as NOT placed; raw-number refusal + two-match
  disambiguation→cancel leaves zero dispatched jobs; no-phone honest
  no-write; alias memory resolves future calls (the parked lookup's
  input is the RESOLVED name).
- `android/test/stage31_call_test.dart` (8): bounded matches verbatim;
  zero-match is ok:true data; missing-grant and missing-gateway honest
  refusals; tail-only in results; `verified` comes from the poll
  verdict (confirmed→true, absent→false with ok:true); refusals →
  ok:false. Full app suite 478 passing, analyze unchanged (11
  pre-existing info lints).

### LIVE E2E evidence (emulator gate `emu-pixel8`)

Full `/v1/intent/turn` conversation with the REAL app + ContactsContract
+ Telecom (contact seeded by the operator via `content insert`):
"call sarah chen" → lookup parked → in-app a11y poll... → approved →
**real ContactsContract rows came back** (count 1: "Sarah Chen"
+15551230100) → "continue the task" → "Calling Sarah Chen
(********0100) — correct?" → "yes" → call parked → approved → ACTION_CALL
→ **verdict: confirmed, verified:true, package
com.google.android.dialer, focused-window a11y re-read** + screenshot
evidence of the Dialer incall screen ("Sarah Chen / Mobile
+1 555-123-0100"). The final turn reports the verified outcome
conversationally. First live pass also recorded an HONEST
failure-and-fix chain:

1. First run: `verdict:absent, verified:false` — the a11y service was
   NOT enabled on the emulator (rootless by design — `Bound services:`
   empty). The flow reported not-placed and did not retry or fake.
   Operator enabled it via `settings put secure
   enabled_accessibility_services` + `accessibility_enabled 1` (lab
   grant, same class as the permission grants). Re-run: confirmed.
   Manual focus trace: dialer incall window appears ~2–2.5 s after
   dispatch (INSIDE the 4 s window) — the absent verdict was fully
   explained, not a mystery.
2. The emulator call screen closes by itself ~4.5 s in (virtual radio
   hang-up); screenshots must be timed DURING the window.

### Honest validation boundary

- LIVE: contacts read, confirm → per-call approval → ACTION_CALL →
  a11y-verified call screen, alias-free path. The alias-write +
  future-call-resolution path is fixture-proven end to end (real
  MemoryStore); its live pass would need a second seeded contact pair
  and is NOT claimed. No real-person call was placed; the emulator's
  virtual radio is THE sanctioned call gate (physical-device acoustics
  and real dial routing stay unvalidated). Nothing new runs in the
  background; no new dependency.

## 4z. Stage 31-P3 phase B — P3e native carrier SMS
## (2026-10-22)

### Scope shipped (ONE carrier SMS per approval, same spine as P3d)

- **App (Kotlin `smsSend` in `CallActions.kt`)**: SmsManager
  `sendTextMessage` behind the SAME layered guards — the recipient
  must pass a dial-grammar re-check INDEPENDENTLY of Core (a bypassed
  Core still cannot text untrusted strings), SEND_SMS is a DANGEROUS
  permission granted by the operator via adb in the lab (never
  silently in-flow — missing grant = honest refusal), and the DRAFT
  is capped at 400 characters (refused, never truncated and hoped).
- **Dart**: `smsSend` bridge + runner case. Job results carry the
  verdict, the masked number tail, and the TEXT LENGTH ONLY — the
  draft never rides the result (same rule as clipboard), so chat,
  job results, and audit rows never hold the message the way the
  approval card does.
- **Core**: `android.sms.send` (HIGH_RISK×CONFIRM, cost 0.15,
  verification=\"explicit\", timeout 90 s) + `SmsFlow` — a subclass of
  the call flow sharing the contact-resolution spine (bounded
  lookup, confirmed alias memory, tail masking, per-flow pending
  ring). The final step differs and adds SMS-specific honesty:
  - \"text <name> saying <draft>\" parses name+draft at the invite
    mouth; bare \"text <name>\" lands in a DRAFT-ASK phase
    (\"what should the text say?\") where CANCEL WORDS are checked
    BEFORE they could become a draft (\"never mind\" never sends);
    retyping replaces, \"send it\" advances;
  - >400-char drafts are REFUSED at the flow mouth (same rule the
    native surface enforces — one rule, two layers);
  - raw numbers as recipients are refused at every layer;
  - the confirm echoes the FULL draft + masked tail, and the per-send
    approval STILL parks (double gate: correctness confirm + in-app
    approval before any radio touch);
  - verdict honesty: \"sent\" means ONLY \"the radio accepted the
    send\" — carrier delivery is not confirmable by any bounded
    re-read, so `verified:false` rides every SMS result (the flow
    reports SENT-UNCONFIRMED conversationally); no \"delivered\" is
    ever manufactured.
- **Drain robustness (found live)**: a parked record that is still
  RUNNING at the drain turn no longer closes the flow — the flow
  reports \"still working\" and stays pending (TTL-bounded) until a
  terminal record lands. Approval → job-claim → run is throttled by
  the app's poll gap (25–60 s), so \"still running\" is the COMMON
  case, not an error. Terminal refusals (denied/cancelled/failed)
  still close immediately with the honest reason.

### Live E2E evidence (emulator gate `emu-pixel8`)

Full `/v1/intent/turn` conversation: \"text sarah chen saying live
gate: this SMS came from zara\" → contacts lookup parked → in-app
approval → real ContactsContract rows → confirm echoed the FULL draft
+ `********0100` → \"yes\" → per-send approval parked → in-app approval
→ SmsManager dispatched → **the SMS materialized in the telephony
provider** (`content query --uri content://sms`: body \"live gate:
this SMS came from zara\", `creator=dev.zara.zara_android`) and the
system Messages app synced it (\"added SMS: 1\"). Final turn:
\"Sent the SMS to Sarah Chen — the radio accepted the send...
SENT-UNCONFIRMED, honestly\", verified:false.

Privacy assertions verified against the LIVE audit.db: neither the
draft text nor the full number appears in ANY audit row (the flow
logs lengths/ids only); the full number never appears in job results.

### Honest validation boundary

- The emulator's virtual radio is THE sanctioned SMS gate (real-
  carrier routing, service-center behavior, and delivery accounting
  stay unvalidated — no physical device). The recipient was the
  fake-lab contact (+1555…) — no real person texted.
- Delivery receipts (PendingIntent follow-ups) are deliberately NOT
  promised: a bounded surface either confirms with a re-read or says
  so; a C2-level delivery promise would be a manufactured overview.

## 4aa. Stage 31-P3 phase B — P3f OTP reading (per-use consent)
## (2026-10-22)

### Scope shipped (exactly one fresh-scan per approval)

- **App (Kotlin, `ZaraNotifListener.otpScan` + bridge case)**: a
  bounded scan over the EXISTING passive notification ring —
  shape-gated (4–8 digit standalone codes inside OTP-context words:
  code/otp/verification/verify/passcode/pin), FRESHNESS-gated
  (max_age_ms ≤ 600000 sent by Core; default 5 min), optional
  sender hint (bounded 40 chars, matched against package + text).
  The FULL message body never leaves the device: candidates carry
  package/title/code/age only (least disclosure — the code is the
  only token the user needs to type). The listener itself remains
  the OS-granted Notification-listener surface (Settings once, no
  silent in-flow grant).
- **Dart**: `otpScan` bridge + `android.otp.read` runner case. A
  zero-count scan rides ok:true (an ANSWER, not an error to retry
  away); the unavailable gateway/listener are honest refusals.
- **Core**: `android.otp.read` (HIGH_RISK×CONFIRM, cost 0.05,
  verification="explicit", timeout 60 s) + `core/otp_flow.py`
  (OtpFlow, injected into the turn service like call/sms):
  - per-use consent is STRUCTURAL: HIGH-RISK means the policy
    invariant parks the tool for approval EVERY run — no ambient
    grant can carry it (existing policy test pins this);
  - the CODE surfaces ONLY in the asking turn: the flow never
    writes memory and its audit rows record counts/ids only
    (fixture: the code string is absent from the entire audit
    stream); the code's only persistence is the execution
    record's retention-TTL lifecycle, DISCLOSED in the report
    message;
  - executor picked like the call flow's phone picker (requester
    online android first; 0 = honest none, >1 = ask);
  - "read the otp [from X]" invite regex; ring-of-one pending with
    10-min TTL; "never mind" before the read closes without
    reporting; "still working" keeps pending while the record is
    non-terminal.

### Live E2E evidence (emulator gate `emu-pixel8`)

"read the otp from google" → approval parked (message discloses the
5-minute window + no-store discipline) → in-app approval → REAL
notification ring scan. The notification was posted via the lab
operator (`cmd notification post`, the sanctioned notification
gate): **the scan returned the code 991227 with age 77 s and the
posting app**, and the final turn surfaced exactly that — nothing
more. LIVE audit.db checked: the code string appears in ZERO audit
rows; otp_flow rows carry counts/ids only. The live path proved the
honest-absent behavior BEFORE the final gate: an ungranted
notification listener reported count=0/no-fresh (data, not a fake
code), and a runner-side int/Long bridge cast bug became an honest
FAILED record — fixed and re-run to success.

### Honest validation boundary

- The lab notification (posted by the operator through
  `cmd notification post`) is the sanctioned OTP gate; real
  provider-app pushes (bank/gmail variants, bundled-message
  formats) stay unvalidated. No real OTP was read; the code never
  left the emulator except into the operator's terminal history.
- OTP codes from QUIET/latent channels (notification extras only
  visible to specialized listeners, e.g. SMS Retriever-style) are
  NOT claimed.

## 4ab. Stage 31-P3 phase B — P3a targeted media control
## (2026-10-22)

### Scope shipped (ONE app's session, never a global blast)

- **App (Kotlin `mediaSessionTargeted` in `CallActions.kt` + bridge
  case)**: MediaSessionManager.getActiveSessions (rides the existing
  OS-granted notification-listener access). The hint is REQUIRED —
  a hintless call refuses ("targeted; no global blast"); matching is
  owner-package contains (ignoreCase); 0 and many matches are honest
  answers with a bounded listing (nothing sent). Play/pause/next/
  previous/stop go through the matched session's transportControls.
- **Verdict honesty**: the cached playback snapshot does NOT refresh
  by itself — the result carries a state_before→state_after observed
  via a bounded PlaybackStateCallback (8 s window, worker handler).
  verified=true only when the observed change MATCHES the action
  (play→playing, pause→paused, stop→stopped); a silent session
  reports the delta as unconfirmed (data, verified:false). Failed
  observation reads "playing→playing" with verified absent — the
  fixture- and live-pinned honest shape.
- **Core**: `android.media.control` (CONFIRM×CONFIRM, timeout 90 s —
  the T0 poll-gap invariant caught an initial 60 s and it was bumped).
- **Self-test session (`MediaSelftest` in DeviceBridge + boot
  bootstrap)**: a LABELED zara-selftest session owned by the app, so
  the production path (matching → transport dispatch → observed
  state change → verdict) can be exercised end-to-end on a device
  where every media app stops playing when it leaves the foreground.
  It claims nothing about other apps; it lives until process death.

### Threading facts recorded live (three honest failures before green)

The full loop took three failed live runs to get right, each pinned
in code comments: (1) register→dispatch-same-turn delivered the
change only at the NEXT dispatch (the framework echoes the current
state at registration — that echo must not satisfy the observe
latch); (2) the session processes transport commands on the MAIN
looper it shares with the method channel, so an awaited latch on
main froze exactly what it waited for — the call now runs on a
worker thread and delivers the channel result back on main;
(3) the emulator's session→command routing latency is 1.5–8 s
(variable), so the observe window is 8 s and the tool timeout 90 s
(pinned by the T0 invariant test, which FAILED the first 60 s choice
— the standing invariant doing its job).

### Live E2E evidence (emulator gate `emu-pixel8`)

`hint=spotify` → honest zero (`count:0, active:[...youtube]`,
nothing sent). `hint=zara, action=pause` on the selftest session →
`state_before: playing, state_after: paused` (observed change,
verified) — dispatch to observed-change measured ~1 ms on the
worker-path build; the PLAYING→PAUSED and PAUSED→PLAYING deltas both
observed live. YouTube deep links were used as the external probe:
targeted matching on a real third-party session succeeded live
(`count:1, package:com.google.android.youtube`), with the session's
own state terminal-at-dispatch reported honestly (stopped/stopped,
no manufactured delta).

### Honest validation boundary

- The PLAYING→PAUSED delta was only reproduced against the app's own
  labeled selftest session; on a real third-party app (YouTube) the
  dispatch + matching is live but that session was ended by the time
  the bounded window ran (the app pauses audio whenever it leaves
  the foreground; no background playback without Premium). The code
  path is identical; the third-party observed-delta transfer is
  pending a real device.
- Track-change verification (next/previous) rides queue-item deltas
  and stays best-effort in this build.

## 4ac. Stage 31-P3 phase B — P3c pull-only photo backup + P3g call-recording refusal
## (2026-10-22)

### P3c: pull-only photo backup (inventory gate + operator pull)

- **App (Kotlin `photosInventory` + bridge + runner case)**: a
  COUNT-ONLY MediaStore metadata scan — count, total bytes,
  newest/oldest epochs, top-level folder names. No file names, no
  paths beyond top-level folders, no content, no thumbnails. The
  scan is READ-ONLY by construction and the surface generates no
  destructive command anywhere; READ_MEDIA_IMAGES is the only new
  permission and ACCESS_MEDIA_LOCATION is DELIBERATELY absent (no
  EXIF/GPS reads).
- **Core**: `android.photos.inventory` (CONFIRM × CONFIRM — every
  run parks, so the per-run consent is structural) +
  `core/photo_flow.py` (PhotoFlow, injected like call/sms/otp): the
  flow refuses delete flavors at the mouth ("backup and delete
  after" = an explicit destructive refusal, never parsed away),
  keeps a ring-of-one pending, and the report carries counts +
  the honest pull plan: `adb pull /sdcard/DCIM/ …` from the
  laptop's operator seam, consented per run. The phone→laptop READ
  by a third-party APP stays Android-restricted, and the design
  says so instead of smuggling a covert channel.
- **Live E2E**: "backup my photos" → parked approval card →
  approved → the REAL MediaStore scan counted the operator-seeded
  JPEG (count 1, 161 bytes, folder `DCIM/Camera/') → the final
  conversational turn reports counts + the pull plan + "I don't
  delete anything, ever". Honest-zero path proven live FIRST (the
  pre-seed run: empty library → "nothing to pull"), and the
  missing READ_MEDIA_IMAGES surfaced as a silent zero-count (fixed,
  re-run to the counts path — the silent-zero behavior was the
  honest data path, never a fake number).

### P3g: call recording — an explicit honest refusal (NOT a feature)

- The flow freezes a refusal BEFORE any invite parsing
  (`core/call_flow.py`, _RECORD_RE): "record the call" / "call
  recording" / "record my calls" return the deterministic no —
  recording on this OS needs a privileged role the build refuses to
  seek, and it is a two-party consent matter in many regions, so no
  legal grey path is promised. Notes during/after calls are offered.
- Caught-live shape: without the early gate, "call recording" was a
  PLAUSIBLE CONTACT NAME for the lookup spine (the fixture pins the
  refusal order; live-checked via the same fixture path).
- No covert capture surface exists anywhere in the codebase for
  calls (no mic bridges, no a11y call capture).

## 4ad. Stage 31-P3 phase B — P3b clipboard mission flow (the conversational piece)
## (2026-10-22)

- Phase A shipped the bridge surface; this closes the plan's "TODO
  phase B": `core/clipboard_flow.py` (ClipboardFlow, injected into
  the turn service like the other deterministic flows).
- **Capture at the invite mouth**: "copy <text> to my phone" parses
  the payload ONCE — the full text rides the approval card and the
  verbal confirm only; the RESULT and the AUDIT ROWS carry the text
  LENGTH only (fixture-pinned: neither the code strings nor the
  example payloads appear anywhere downstream).
- **Mirrored discipline**: >4096-char payloads refuse at the mouth;
  free text on a confirm-phase drain NEVER becomes content ("tell
  me the weather" cannot become a clipboard paste); "no" keeps the
  phone's clipboard untouched; the write verdict quotes the
  verbatim device re-read.
- **Live-validated via the same surfaces this session**: the bridge
  surface (phase A) is unchanged; the flow's fixtures prove the
  full conversation shape end-to-end (capture → confirm → parked
  write → length-only verdict).

## 4ae. Stage 31-P4 — calendar (.ics) + weather (Open-Meteo), laptop-side (2026-10-22)

USER DECISIONS (2026-10-22): calendar = local .ics files (CalDAV not
picked); Home Assistant = DROPPED (not in use); weather = free
open-data endpoint. All three recorded in the plan.

- **core/calendar_flow.py** — bounded stdlib .ics read:
  - Z-suffixed stamps → UTC; naive stamps → laptop local; naive
    stamps with a TZID are shown UNSHIFTED and labeled "timezone
    unresolved" (never silently converted); all-day DATE events
    labeled; RRULE recurrences NOT expanded (honest gap stated in
    every answer); 2 MB / 8000-line / 500-event parse budget; paths
    via ZARA_ICS_PATH (colon-separated, lazily re-read each turn);
    unreadable files surface per-file in the answer (never hidden);
    READ-ONLY by construction.
- **core/weather_flow.py** — stdlib urllib against Open-Meteo
  (forecast + geocoding, no account): 'in <city>' geocodes and NAMES
  the top match ("<city>, <country>"); no city → ZARA_WEATHER_LAT/LON;
  neither → an honest ask; every answer carries the SOURCE and the
  fetch TIME label; offline/failed fetches are reported as failures,
  never fabricated weather; 4 s timeout, 200 KB read cap, one fetch
  per turn (2 s rate courtesy).
- **Wiring**: both flows are deterministic, injected into
  IntentTurnService via new positional slots after clipboard_flow;
  build_stack() constructs them with the shared audit. Zero new
  dependencies. Device-tool timeout invariant unaffected (no device
  tools added).
- **Fixtures (7)**: tests/test_p4_calendar_weather.py — calendar:
  today+TZID-unresolved+RRULE note, unreadable-file honesty,
  no-config ask; weather: geocode names the result (mocked),
  unknown-city geo miss, offline = failure-not-fake, no-location ask.
- **LIVE (this session, real core API /v1/intent/turn)**:
  - weather in Lisbon → real Open-Meteo answer: "Lisbon, Portugal:
    20.1°C, mostly clear, wind 11.0 km/h, no rain expected in the
    next 6 h — source: Open-Meteo, no account, fetched
    2026-10-22T14:15 (Europe/Lisbon)".
  - calendar → the lab .ics (/tmp/opencode/calendar/lab.ics) read
    live: both events reported with correct local stamps + the
    honest RRULE note.
- **Two session incidents, recorded because they cost re-pairing**:
  - The host workspace restarted and WIPED /tmp/opencode — runtime
    DBs included zara_devauth.db, so the phone's device identity died
    (its next register → 403 "invalid/revoked device identity", app
    fell to its Recovery "pair again" tab, by design). Core-side
    re-enroll + app-side claim from the pairing field restored the
    pair in one pass; grants (a11y service, notification listener,
    SEND_SMS/READ_MEDIA_IMAGES) survive userdata and were re-verified.
  - LESSON (identity, not a bug): the operator enrollment must use
    the DEVICE's claim id — the Android app claims as
    device_id='android-phone' (core_client.claim(2nd arg)); enrolling
    any other label yields a claim 403 "unknown or expired code"
    even with a fresh code. Pinned here to save the next pair.
- **Regex lesson (bugs found by live probes, fixture-tested)**: both
  flows' invite regexes handled "what's/whats" but NOT "what is
  ..."/"how is ..." (the `'?s?` class). The naive pattern silently
  swallowed a whole utterance class live (fixture utterances all
  used the contracted form). Fixed and pinned:invite coverage tests
  now include the expanded forms; non-matching utterances
  ("check my battery") still do NOT ride the flows.

## 4af. Daily-use reliability: durable identity + the silent-loop-death fix (2026-10-22)

Both incidents from this session's host restart are now CLOSED:

- **Durable runtime state** — all runtime DBs moved from ephemeral
  /tmp/opencode to ~/.local/state/zara/ (memory, missions, audit,
  DEV AUTH, scheduler + the lab calendar). The launcher now points
  there. **Acceptance proof (live)**: sequential core restarts — the
  paired device re-registers with challenge proofs WITHOUT
  re-pairing (0 x 403, challenge 200 immediately,
  /v1/devices = android-phone). Device-tool timeouts untouched.
- **WAL lesson (ops, recorded)**: SQLite sidecars carry the live
  state — copying a .db WITHOUT -wal/-shm copies an empty store.
  Copy all three (or checkpoint first). Caught by the first
  migration attempt producing an empty-but-valid DB.
- **Silent-loop-death fix (Dart)**: live crash captured in logcat —
  the app in `revoked` kept receiving 403s during the identity gap;
  DeviceLifecycle.handleHttp called move(error), which THROWS
  (revoked -> error is not an allowed transition) inside `_tick`,
  and the unhandled exception killed the poll loop: the device then
  looked present-but-frozen. Fix: repeated auth failures while
  already in a safe state are a recorded NO-OP (idempotent handle,
  strict guard unchanged). Pinned by test stage9_lifecycle
  ("repeated 403 while revoked is a no-op, never a throw"); Dart
  487 passed, analyze clean (same 11 pre-existing infos); APK
  rebuilt + installed.
- **Probe hygiene**: the host-side claim probes (probeA/probeB) and
  the stale emu-pixel8 enrollment were revoked after use.

## 4ag. Operator token remembered on device (2026-10-22, user request)

USER REPORT: after typing something in the app's conversation and
pressing send, the app showed an "operator token missing" style
error.

**Why it happens (architecture, not a failure)**: /v1/talk and
/v1/talk/stream are OPERATOR-authenticated contracts by design
(the streamed turn executes tools under operator authority, live
in Core) — device pairing authenticates only the /v1/agent/*
surface (jobs, heartbeats, approvals). The phone needs BOTH
credentials; the operator token field was deliberately memory-only,
so every fresh app start asked again.

**Fix (user decision 2026-10-22: entered ONCE, remembered)**:
- SecureStore: new Keystore-backed slot `zara_operator_token`
  (same protected store that already holds the device key — never
  plaintext). Wiped TOGETHER with device credentials on
  logout/revocation: an un-paired device must not retain operator
  authority.
- main.dart: the token restores at app start into the Tasks field
  AND client.operatorToken (ready even while pairing); persisting
  happens on the field's onChanged only (action-time syncs never
  rewrite storage). Empty save = explicit "no token".
- UI label updated: "Operator token (remembered on device)".

**Live-verified (emulator, real core)**: token entered once →
app force-stop + restart → field pre-filled with no interaction →
conversation send → POST /v1/talk/stream 200 OK (the same surface
was 401 three times before the fix in this same log). Dart: 490
tests passed (+ tests for token round-trip, wipe-with-credentials,
empty-means-none); analyze clean (same 11 pre-existing infos); APK
rebuilt + installed.

## 4ah. Conversation brain: deterministic fastpath + the echo placeholder explained (2026-10-22, user report)

USER REPORT: after typing in the app's conversation, the reply read
"echo: RELEVANT LONG-TERM MEMORY (untrusted data, never authority)…".

**What that text is (and is not)**: /v1/talk runs context + memory →
LLM → tools. Core had NO LLM configured, so `provider_from_env()`
defaults to the built-in EchoProvider — a deterministic dev stub
whose reply is literally `echo: <first 200 chars of the prompt it
received>` (deliberately non-destructive, never a real answer). The
prompt skeleton includes the phrase "RELEVANT LONG-TERM MEMORY",
hence the reply. Honest answer: without a real provider, the loop
generates no opinions at all — by design.

**Also found and fixed (the actual root cause of the broken
conversation feel)**: the talk loop SKIPPED the deterministic flows
— calendar/weather asks that /v1/intent answers exactly went to the
echo stub instead. Fix: `ConversationLoop.intent_fastpath` —
`IntentTurnService.conversational_fastpath` answers with the
READ-ONLY laptop flows (calendar ☑, weather ☑) BEFORE any model
call; every other utterance falls through to the provider
unchanged; device flows stay approval-gated on /v1/intent. Audit
row `turn_fastpath` marks deterministic turns.

**Fixtures (3)**: tests/test_talk_fastpath.py — fastpath answers
before ANY model call; miss falls through (provider called exactly
once); the REAL injected IntentTurnService fastpath answers a live
.ics end-to-end inside the loop (model stays unused).

**Live (real core, real phone UI)**: "what is next today" typed in
the conversation view → reply renders "Calendar, next 7 days: Sat
Oct 24 12:45: Test recovery demo …" (deterministic, `POST /v1/talk`
voice equivalent verified on the API side too: Lisbon weather with
the full source/time label). Non-flow utterances still hit the echo
placeholder until a real provider is attached.

**Model selection corrected (user asked: is it the best?)**: a
per-account sweep + core-contract probes ranked the invokables.
nemotron-3-ultra-550b-a55b: 7.8 s + valid contract JSON; gpt-oss-20b:
106 s; 3.5-super-vl-120b: 76 s and NOT-JSON; llama-90b-vision:
183-с timeout. Switched LLM_MODEL to nemotron-3-ultra. TWO core
fixes it forced (pinned tests/test_llm_salvage.py): (a) blocking
path also needs the 1024-token budget (512 truncated the plan);
(b) NEMOTRON-GLITCH SALVAGE: the real stack once received the plan as
'{"type":"{"type":"response",…}' — a truncated first attempt the model
restarted; _salvage_action anchors at each '{"' (rightmost-first, ≤200)
and accepts ONLY a candidate that passes the FULL strict parse
(parse_llm_action). It can never widen execution. LIVE after the
switch: blocking 'hi' → "Hello! How can I help you today?" (responded),
streaming moon-fact full answer, phone 'how are you' →
"I'm doing great—thanks for asking! How can I help you today?".

**Follow-up (same day, user sent 'hii' on the phone)**:

**Follow-up (same day, user sent 'hii' on the phone)**: even the
`echo:`-prefixed dump leaked internals (mission rows from the
context block). Now the loop intercepts the DEFAULT echo stub
(`provider.name == "echo"` only; scripted/mock test providers are
untouched) and returns a terminal, plain-language placeholder:
what is running, what DOES work deterministically, and how an
operator attaches a real LLM. Nothing internal leaks. Pinned in
tests/test_talk_fastpath.py (echo-honesty + scripted-still-passes).

**NVIDIA keys (user-provided) — RESOLVED same day (user pushed on
the second key)**: the TWO keys belong to TWO DIFFERENT NVIDIA
accounts (error payloads name different account ids). Sweeping the
catalog per key: key1's account has gpt-oss-20b bound; key2's
account binds ONLY openai/gpt-oss-20b (nemotron/mistral → 404
`Function … Not found for account`; llama-3.3/3.1-8b → 410 EOL).
The FIRST sweep wrongly concluded both keys were fully blocked —
one gpt-oss probe crashed reading its output file and was never
retried. Lesson: an inconclusive probe is NOT a negative result;
re-probe before declaring blocked. LIMITS DISCOVERED LIVE (all
through real probes): (a) model enumeration ≠ invocation
entitlement — /v1/models lists 83 models for accounts owning one;
(b) gpt-oss-20b is a REASONING-class model: in streaming mode it
burns the completion budget in a `reasoning` channel BEFORE any
content — at 512 max_tokens the stream ended reasoning-only with an
EMPTY visible reply; `reasoning_effort: low` + 1024 tokens yields
real content (core stream_reason now sends both; reasoning deltas
are dropped, content-only extraction, so the scratchpad never
reaches the user). The key material was never echoed into logs,
docs, or test output (stored masked in /tmp only). When a working
OpenAI-compatible endpoint exists (NVIDIA account with credits, or
any other provider), wiring it is 4 environment variables +
one core restart: LLM_PROVIDER=openai-compatible,
LLM_BASE_URL=<…>/v1, LLM_API_KEY=<never echoed>, LLM_MODEL=<model>.

## 4ai. P5 feature-hunt ①: chat-path CONFIRM receipts — the systemic
quick-action gap, found live and closed (2026-10-22)

Live proof of the chat loop exposed a SYSTEMIC gap, not a timer bug: chat
proposals for quick actions (timer/torch/alarm…) parked their approval
hold WITHOUT the tool-level `confirm` input — the chat path never asked
the user for it as a slot — so approving sent `registry.call` inputs that
failed validation (`invalid inputs for android.timer.set: missing
required field: minutes / confirm`). Direct-tool callers always set
`confirm=true` explicitly, which is why §4x/tool-level validations never
saw this: **the chat path to CONFIRM quick actions had never worked.**
Three structural fixes (all tested, one end-to-end recreated the live
failure and proves the repair):

1. **Grant-time confirm receipt (core/execution.py `_grant_checked`)**:
   the user's approval action IS the tool's confirmation. AFTER the
   drift + revalidate checks bind the parked operation (so the
   pre-approval fingerprint is never re-seen — nothing drifts), the
   grant injects `confirm: True` for tools whose schema declares the
   property, emitting `approval_confirm_injected`; the receipt lands in
   the audited inputs handed to the tool. Schema-lookup failure never
   blocks a grant. (`tests/test_p6_timer_confirm_chatpath.py` ×2: parked
   without confirm → approve → SUCCEEDED; explicit confirm=true never
   overridden.)
2. **Timer slot extraction (core/intent.py `_extract_params`)**: the
   generic `.set` direction rule must never see the timer ("start a
   timer…" would emit `enabled=True` from "start"); a bounded
   minutes/hours rule extracts duration + optional quoted-or-"to <x>"
   label, clamps 1–7200, and honestly clarifies without a duration
   ("For how long should the timer run?"). Aliases: "set a timer",
   "start a timer", "timer for 5 minutes", "kick off a timer".
3. **Grounding authority (core/intent.py `build_context` +
   `ground_proposal`)**: a properties-less device-advertised stub schema
   won `setdefault` over the registry schema and SILENTLY disabled the
   missing-required check (the vehicle that nearly parked empty-input
   approvals). The registry — the engine's own validation authority —
   now overrides any stub lacking a `properties` map, and the
   required-check runs independently of the properties guard. `confirm`
   is exempted from slot-filling (`CONFIRM_SLOT_KEYS`): it is an
   execution RECEIPT the approval click satisfies, never a user slot
   (asking would double-prompt: slot answer, then approval) — the parser
   also never injects it itself (forged authority — the
   FORBIDDEN_PROPOSAL_FIELDS guard class).

**Live, on the paired emulator (android-phone)**: `"set a timer for 5
minutes to eggs"` → proposal `android.timer.set {minutes:5, label:"eggs"}`
(deterministic, no model call) → approval `exec-a3d4947bb987` →
SUCCEEDED: result `{"verdict":"handed-to-clock","minutes":5,"verified":
false,"verification":"the clock app opened pre-filled (USER completes
the timer; Zara claims only the hand-off)"}` — and a screenshot shows
the Clock app running an "eggs" timer counting down (04:44 in flight).
Honesty preserved: Zara claims only the hand-off. Core restarts required
by the intent.py/execution.py edits were performed with /v1/devices
re-registration verified between probes.

Lt. Ceiling: unchanged (chat-utterance → approval → pre-filled clock UI ≈
15–20 s host-side; timer accuracy is the Clock app's). Known honest gap:
the engine count-down start is device-side Clock behavior; Zara reports
hand-off, not timer firing.

## 4aj. P5 feature-hunt ②: Do-Not-Disturb state + verified toggle
(2026-10-22/23)

`android.dnd.get` (SAFE read; PUBLIC permission tier like
`system.wifi.state`) and `android.dnd.set` (CONFIRM) were added across
the full stack (core device_tools defs → Kotlin QuickActions `dndGet`/
`dndSet` → DeviceBridge cases → DeviceBridge Dart `dndGet`/`dndSet` →
job_runner typedefs/constructor/allowlist/cases → main.dart gateway →
capabilities.dart → phrase aliases). Honesty contracts: the GET is
read-only and reports the named interruption filter + whether Zara
holds `ACCESS_NOTIFICATION_POLICY`; the SET refuses honestly (nothing
changed) without policy access, refuses `skip_ui` structurally (no
clock-style UI to hand off — DND sets directly with policy access), and
verifies by a filter RE-READ (`verified` reflects re-read agreement).

**API ground-truth lesson (build caught by the compiler)**:
`NotificationManager.setInterruptionFilter` returns **void** on API 36
(javap on the SDK jar); a boolean call-result would be Unit — the
Kotlin build failed hard and the verified verdict now comes from the
re-read ONLY. Live: the lab one-time grant is
`adb shell cmd notification allow_dnd dev.zara.zara_android`
(the appops CLI does not know this appop on this build;
`isNotificationPolicyAccessGranted` returned true after the grant —
probed by the live dnd.get).

**Live, emulator (android-phone)**: direct exec read →
`{filter:1, filter_name:"all (DND off)", dnd_on:false, policy_access:
true}`; chat `"turn on do not disturb"` → proposal
`android.dnd.set {enabled:true}` → approval → SUCCEEDED
`{verdict:"verified-by-reread", filter:3, verified:true}`; OS ground
truth `mZenMode=ZEN_MODE_NO_INTERRUPTIONS, mInterruptionFilter=3`;
chat `"turn off do not disturb"` → SUCCEEDED `{filter:1, verified:
true}`, OS `ZEN_MODE_OFF`. State restored off. Grant-time confirm
receipt (§4ai) exercised by these approvals — the holds were parked
without `confirm` and succeeded.

Tests: python `tests/test_p6_dnd.py` ×4 (registry tiers, direction-word
mapping, read-only invite) + chat-path receipt tests (§4ai); Dart
stage31 ×4 new (filter report, verified re-read, policy refusal,
unavailable-honest) + allowlist pin extended to eleven; full Dart suite
496 passing; analyze clean (11 pre-existing info lints unchanged).
Full python suite re-run after this batch (run log).

Lt. Ceiling: DND grant is once-per-device interactive; the grant-screen
open/answer is interactive-user-gated on a real phone (lab grant via
adb documented). Bedtime/Driving automatic zen rules are Device
Well-being surfaces — out of scope; Zara only reports the legacy
filter.

## 4ak. P5 feature-hunt ③: forecast / rain-tomorrow asks answer the
asked DAY, not today (2026-10-23)

The weather flow already matched "will it rain tomorrow" in its invite
regex but then answered with the CURRENT sky — an honesty mismatch
discovered in this hunt round. Fix inside `core/weather_flow.py` (same
Open-Meteo endpoints, still no new deps, still one bounded fetch per
turn):

- the fetch now also requests the `daily` block (weather_code,
  min/max temp, precipitation-probability max, 2 days) alongside the
  existing current+hourly fields;
- `describe_tomorrow(out)` answers from `daily[1]` — verdict-FIRST
  when the utterance asks about rain/snow ("Yes / Very likely / Maybe /
  Probably not — rain chance up to N%"), else tomorrow's condition with
  min–max and precip chance; every answer keeps the source+time label;
- a missing/short daily block is an honest "I couldn't read tomorrow's
  forecast block" as an EXECUTED=FALSE flow result (never fabricated),
  audited as `weather.forecast_missing`;
- city extraction survives a trailing "tomorrow" (`weather in Lisbon
  tomorrow` → city Lisbon), and the courtesy gate now ticks on ACTUAL
  fetches only — a no-location honest ask no longer consumes the
  rate window (live-discovered UX bug in this batch).

Tests: `tests/test_p4_calendar_weather.py` +4 (verdict first with the
daily min/max — never the current temp; low-probability honest
"Probably not"; city+tomorrow geocode with the date echoed; missing
daily honest non-executed) — 11 passing in the file.

**Live (real Open-Meteo, no mocks)**: "what's the weather in Lisbon"
→ today's answer unchanged (regression preserved); "will it rain in
Lisbon tomorrow" → `Lisbon, Portugal: Tomorrow (2026-10-23): Probably
not — rain chance up to 3% (12.0–22.0°C, overcast) — source: Open-Meteo,
no account, daily block fetched 2026-10-23 (Europe/Lisbon)`. Core
restart after the weather edits; /v1/devices re-registration verified.

Lt. Ceiling: daily[1] is Open-Meteo's +1 day (UTC-offset aware via
timezone=auto); snow asks read the same precipitation maximum
(worded "precipitation chance"); multi-day ranges stay out of scope.

## 4al. P5 feature-hunt ④: the screen-off boot offline strand — closed
(RESEARCH R21 #3, 2026-10-23)

Round 21 recorded a REAL robustness gap: the app's boot-time network
probe can cache "offline" when the screen is off, and `_tick` refused
to re-probe while the cached snapshot said offline — the loop idled
forever until a fresh app start with the screen awake. Fixed in
`android/lib/main.dart` `_tick`: a cached-offline tick now RE-PROBES
the bridge (cheap `_refresh()` on the same tick; exceptions keep the
cached snapshot for the next tick) and the conn state is mapped from
the FRESH read only (`coreLinkForNetwork`, pure, test-pinned) — a
fresh online read falls through into the normal tick and the job loop
(proxy listener) unwinds via the shared snapshot ≤30 s later (the
job-loop's own bounded idle re-check rides on the shared `network`
field; no second probe there by design — one metering point, no busy
loops).

Tests: `stage10_approval_test.dart` +1 (fresh-read-decides contract;
`NetworkSnapshot` semantics already pinned). Full Dart suite 497
passing; analyze clean.

**Live (emulator, android-phone)**: force-stop with the screen OFF →
`am start` while the screen stays off → app re-registered within
40 s → screen ON, no app restart → still online. Both checks pass with
the fix installed (cross-check vs the R21-#3 recorded strand: the
cached-offline race itself could not be deterministically reproduced
on the emulator — the pure contract is the regression pin).

## 4am. P5 feature-hunt ⑥: calendar WRITE with per-event approval
(RESEARCH R35, 2026-10-23)

A daily-use agent must be able to ADD an event, not only read one. The
write is a laptop-side tool (`calendar.event.add`) implemented ONCE in
`core/builtin_tools.py` and executed by the laptop agent
(`device/linux/tools_linux.py` — the parity test pins both sides). It
takes summary + dtstart (pattern `^20\d{6}(T\d{6}Z?)?$`), optional
dtend/description/location/calendar selector; refuses duplicates
(same title+start — re-parse of the target file, nothing written);
bounds timed events to a 7-day horizon; writes atomically (tmp +
`os.replace`, pre-existing file bytes preserved) and verifies by
re-parse. Writes to operator data: CONFIRM permission, CONFIRM risk,
`never_auto_approve=True` (policy `NEVER_AUTO_APPROVE_CAPS` membership
— standing grants can NEVER unlock it; each event costs one explicit
human approval).

Chat path: a structural local matcher (`_calendar_hit` in
`core/intent.py`) fires only for quoted event text ("add \"...\" to my
calendar") — the quote is required, so an unquoted ask CLARIFIES with
the quote template instead of guessing what the event is. Cal/time
resolution is deterministic and local: today/tomorrow/weekday anchors,
`HH:MM`, `2pm`-style meridiems ("room 3" stays non-time — a match
needs a marker: `at`, colon, or am/pm), "for N minutes" duration,
"all day" → VALUE=DATE pair. Nonsense times fall through to the
hosted parser, which now receives a "today is <date>" anchor line in
`prompt_for`.

**Live-discovered routing gap (fixed, pinned)**: laptop descriptors
advertise FEATURE-level ids (`calendar.write`) while the chat resolver
routes TOOL names (`calendar.event.add`) — Android devices advertise
per-tool ids, so only the phone chat ever worked. Before the fix,
every laptop tool was unreachable in chat ("no eligible device").
Both `resolver._candidates`/`_verdict_for` and
`fabric.route_capability` now translate tool name →
required_capabilities and match the advertised feature id
(`fabric._tool_required_caps`). Pinned in `tests/test_resolver.py`
(routing through the feature id; NO routing when the device
advertises a different feature only).

The device-job proxy wait uses the house timing invariant (default
90 s — job expiry forever with the sub-poll-gap wait recorded as a
lesson in R35).

**Live probes (real core + real laptop agent, laptop-1 paired and
advertising calendar.write)**:
- quoted: `add "Zara status demo today 23:30" to my calendar` →
  proposal `{summary:"Zara status demo", dtstart:"20261023T233000Z",
  dtend:"20261024T003000Z"}` → approval `exec-47c48b329d56` →
  SUCCEEDED, `verified:true`, re-parse `events_total:3`, and the
  VEVENT is in `~/.local/state/zara/calendar/lab.ics` before
  `END:VCALENDAR`.
- read side confirms: "what is on my calendar" → "…today 23:30: Zara
  status demo…".
- unquoted: "add dentist tomorrow 2pm to my calendar" → clarify
  "What is the event? Quote it — e.g. add \"Dentist tomorrow 2pm\" to
  my calendar." (structural honesty; no guessed event).
- duplicate re-ask → approval `exec-0c63df170894` → honest refusal
  "that event is already on the calendar (same title and start) —
  nothing added", file unchanged (still exactly one Zara-status-demo
  VEVENT).

Tests: `tests/test_p6_calendar_write.py` (tool contract incl.
never_auto_approve + linux parity; duplicate refusal; all-day pair;
7-day bound; tmp atomicity; parser matcher battery: quote required,
tomorrow/weekday/all-day/duration determinism, nonsense → None),
resolver pins above. Final full python suite: 2143 passed + 1 skipped
(plus one latent pre-existing test bug FIXED — the storage
copy-verify corruption test flipped the dst byte to `q`
unconditionally, a silent no-op ~1/256 runs when the source's last
byte already was `q`; now corrupts by bitwise-inverse of the source's
last byte, 6/6 standalone).

Operational note: an external process wiped `/tmp/opencode` mid-batch
(launcher + key-extraction file gone; core killed). Both were rebuilt
from the documented contract (`scripts/zara_local.sh` env list +
`.env`) without ever echoing key material; the laptop agent key
(`~/.config/zara/laptop-1.key`) survived and re-pairing was NOT
needed. Lesson recorded in R35.

## 4an. P5 feature-hunt ⑦: recurring (RRULE) events expand on the
read side (RESEARCH R36, 2026-10-23)

The calendar read flow previously showed a repeating event ONCE at
its base occurrence with the honest "I don't expand recurrences yet"
note — an acknowledged daily-use gap. Now a BOUNDED subset of RRULE
expands honestly into real occurrences (still read-only, still
zero-dependency, stdlib only):

- supported: FREQ=DAILY | WEEKLY (+ a BYDAY weekday list) | MONTHLY
  (base day-of-month; short months are honestly skipped), INTERVAL,
  COUNT, UNTIL (date or datetime).
- unsupported stays HONEST per event: YEARLY, exotic BY* members,
  ordinal BYDAY (`2MO`), malformed values → that ONE event still
  shows once at its base occurrence and the answer carries
  "N repeating event(s) use a rule I don't expand …". A supported
  rule produces NO note.
- bounded by design: at most 400 candidate steps per event;
  COUNT clamped at 366; INTERVAL clamped at 365.
- old-base honesty: the walk JUMP-STARTS to the read window
  arithmetically (floor at the interval bucket containing the
  window start) so a daily rule created years ago costs zero budget
  and still yields the in-window occurrences.
- occurrences keep the event's base duration (DTEND captured in
  parse_ics now) for the "right now (in progress)" logic; the
  12-line answer cap still applies.

Tests: `tests/test_p4_calendar_weather.py` — unit math on FIXED
dates (daily-count window, BYDAY ordering with the base week's
in-window Friday, fortnight interval over a 22-day horizon,
short-month skip of a 31st, UNTIL stop, 7 unsupported forms, the
jump-start contract); flow-level: the no-RRULE fixture now asserts
NO note appears, and a recurring fixture (DAILY;COUNT=4 + a YEARLY
rule) asserts exactly 4 named occurrences plus the per-event honesty
note for the unsupported one.

**Live probes (real core + real lab.ics, both events appended and
then surgically removed, file never corrupted)**:
- `FREQ=DAILY;COUNT=3` stand-in event → "what is on my calendar" →
  "…today 17:00: Zara stand-in daily; … Sat Oct 24 17:00: …; Sun
  Oct 25 17:00: …" (three occurrences, no honesty note).
- removed → read back clean (3 events, stand-in gone).
- `FREQ=YEARLY` check-in → the event shows once at its base
  occurrence WITH "1 repeating event(s) use a rule I don't expand —
  each still shows once at its base occurrence."
- removed → read back clean again.

Final full python suite for the batch: 2153 passed + 1 skipped,
zero failures (core restart after the calendar_flow change; live
stack back up with the persistent laptop agent line in the
rebuilt launcher).

## 4ao. P5 feature-hunt ⑧: calendar REMOVE with per-event approval
(RESEARCH R37, 2026-10-23)

Completes the write family: `calendar.event.remove` (laptop-side,
same shape as the add — implemented once in `core/builtin_tools.py`,
executed by the laptop agent, parity-pinned; never_auto_approve;
CONFIRM). Selection honesty by construction:

- remove by UID (exact block match) or by summary — NEVER unnamed:
  "I never delete an unnamed thing".
- a summary + optional dtstart/date pin; several same-title events
  and no pin → REFUSE with a candidate list ("2 events match … I
  refuse to guess"), nothing removed.
- a repeating (RRULE) parent removes the WHOLE series and the
  result says so ("the whole series is gone").
- removal is the block's raw span only (folding/line endings of
  everything else preserved byte-exact); atomic tmp + rename;
  re-parse verification with the SAME comparison semantics as the
  selection (exact UIDs; summary+pin) — the "gone" check can never
  false-positive on a sibling UID that merely CONTAINS the target
  ("p1@x" lives inside "rp1@x" — live-caught in the first run, now
  pinned) nor fail a pinned removal because a same-title event at
  a different start survives.

Chat path: `cancel|remove|delete|drop "…" from my calendar` — the
quoted matcher reuses the add matcher's day/time resolution (now
`_cal_shape`, shared); a quoted clock time pins the start exactly,
a named day pins the date only, neither = title-only removal. THE
DESTRUCTIVE-WORD GUARD (delete/remove/…) gains exactly ONE
exception: the quoted "my calendar" remove shape — because the real
gate is the never-auto-approve per-event approval; every other
destructive wording still blanket-refuses. A CRLF line-ending bug
was found and fixed in BOTH calendar tool reads (a default text
read silently translated CRLF→LF and the rewrite would have
normalized the whole file) — pinned in both test files.

**Live probes (real core + real laptop agent + real lab.ics)**:
- `add "Zara remove demo tomorrow 10am"` → approval → SUCCEEDED.
- `cancel "Zara remove demo tomorrow 10am" from my calendar` →
  proposal `{summary:"Zara remove demo", dtstart:"20261024T100000Z"}`
  → approval → SUCCEEDED, `verified:true`, "removed exactly one
  VEVENT", file back to its 3 real events (grep 0 for the demo).
- `delete "Zara remove demo" from my calendar` → PARKS for approval
  (the guard exception works; the destructive verb no longer
  blanket-refuses the sanctioned shape).
- `cancel "Zara ghost event"` → approval → honest refusal "no event
  matches 'Zara ghost event' — nothing removed", file untouched (3
  events).

## 4ap. P5 feature-hunt ⑨: day-scoped calendar answers (RESEARCH R38, 2026-10-23)

A daily-use ask — "what's on my calendar tomorrow" — used to bury
tomorrow's items inside the 7-day sweep, indistinguishable from
later ones. Batch 9: a NAMED day (today / tomorrow / a weekday,
"next" forward-only, bare *"calendar friday"* included) scopes the
read reply to that LOCAL date:

- the sweep horizon stretches to reach the named day beyond 7 days,
  so an RRULE occurrence series and plain events both get walked to
  the target; items filter by their LOCAL calendar date (not the
  UTC-calendar date — in IST a 23:30Z event is tomorrow 05:00 local
  and answers as tomorrow).
- wording: ` Calendar for <day>: <stamp>: <title>; …` — items carry
  stamp + title only (the head owns the day; the old per-item
  UTC-calendar day label could contradict the head, e.g. a "today"
  label inside a *tomorrow* answer — live-caught, pinned in tests).
- empty scope is day-honest: "Nothing on the calendar tomorrow."
  (the old wording said "next 7 days", which would be a lie for a
  scoped ask). Unscoped asks ("what's next?") keep the exact
  next-7-days wording and labels.

**Live probes (real core + real lab.ics, IST +5:30 host)**:
- "what's on my calendar tomorrow" → `Calendar for tomorrow: 23:30:
  Zara status demo; 12:45: Test recovery demo.` (both land on local
  Oct 24: 23:30Z=05:00 IST, 12:45Z=18:15 IST — the local-date filter
  is what honest means here).
- "what's on my calendar today" → "Nothing on the calendar today."
  (both events sit on local tomorrow — honest).
- "What's on my calendar friday" → "Nothing on the calendar
  friday." (tomorrow is the next Friday; the resolver is
  forward-only — target Oct 30, nothing there).
- "what's next?" → unchanged 7-day sweep wording.

Known stamp caveat (pre-existing, unchanged): item clock stamps
follow the file's UTC convention ("23:30" = 23:30Z), as in every
calendar answer before this batch.

Two shards of honesty fell out of this batch while the full suite
ran on the real IST host:

1. **An event that already started was invisible to "what's next
   today?"** — the upcoming-window guard (`now <= dt`) excluded
   every non-RRULE event that had begun, so the "Right now: … (in
   progress)" machinery could only ever fire for RRULE
   occurrences. Now a started event with time remaining (DTEND, or
   the 2-hour default) reports as Right now and is still excluded
   from the upcoming list. Pinned by fixing a fixture that had been
   passing on timing luck.
2. **An all-day event now runs its whole local day** ("Right now"
   from midnight to midnight), not the 2-hour default.
3. The talk-fastpath test itself had a tz lie: it stamped LOCAL
   now+2h with a Z suffix — true only on a UTC host; on this IST
   machine the instant sat 3.5h in the past. The fixture is now a
   DATE-VALUE all-day event (clock independent), and the test
   passes standalone across seconds boundaries (it was a latent
   second-boundary flake before).

## 4aq. P5 feature-hunt ⑩: "when is my X?" calendar title lookup
(2026-10-23)

The other half of daily calendar triage — finding a meeting by name
— was impossible: "when is my dentist appointment?" fell into the
hosted fallback. Batch 10 adds a title-lookup mode to the read flow:

- shapes: quoted title ("when is my \"trivia\""), noun-tailed
  ("when is the dentist appointment/meeting/event/session/call"),
  and nounless-next ("when is my next haircut"). NARROW by design —
  anything else, and crucially anything with a DAY word
  ("when is my dentist tomorrow"), does not hijack the lookup: the
  sweep/scope machinery or the hosted parser keeps the turn.
- answer: the earliest future match with a LOCAL-tz stamp —
  `Next "status demo" on the calendar: Sat Oct 24 05:00 (+0530):
  Zara status demo.` (23:30Z shown as the 05:00 IST instant it is
  for the operator); at most two stamps then an honest "+N later"
  count; substring matches only true titles ("Dentist" does NOT
  match a "dental" query); RRULE occurrences count inside the
  window; absence is explicit: `No upcoming "ghost ritual" on the
  calendar (next 7 days).`

**Live probes (real core + real lab.ics)**: 'when is the status
demo meeting' → earliest correct local stamp; 'when is my next
recovery demo' → `Sat Oct 24 18:15 (+0530): Test recovery demo`
(12:45Z); 'when is my ghost ritual appointment' → honest absence;
'when is my recovery demo' (nounless, no "next") → stays with the
hosted path by design.

## 4ar. P5 feature-hunt ⑪: past-day calendar history (2026-10-23)

"what was on my calendar yesterday?" could only answer about the
FUTURE — the sweep started at `now`. Batch 11 scopes the past too:

- "yesterday" and "last <weekday>" resolve backward (said on a
  Friday, "last monday" means the Monday before — ISO indices,
  which the battery initially got wrong and the resolver was
  right); past-tense asks ("what was on…") route to the flow.
- a historical day is answered by OVERLAP: an event counts if it
  started, ended, or ran through that day — DTEND honored, an
  all-day event covers its whole day. RRULE occurrences inside the
  day come from the walker with its jump-start anchor moved to the
  past window's start (previously occurrences before `now` were
  structurally invisible; yesterday's stand-up simply did not
  exist for the agent). Honest wording: `Calendar for yesterday:
  …` / `Nothing on the calendar yesterday.`
- The fixture tz lesson appeared AGAIN (R38 #5): the test helper
  stamped a local clock value with a Z suffix — true UTC requires
  the conversion, and the suite caught it before the docs did.

**Live probes (real core + real lab.ics)**: "what was on my
calendar yesterday" → `Calendar for yesterday: 15:45: Zara status
sync (lab).` (Oct 22 15:45Z = 21:15 IST — the only event that day);
"what is next today" → honest empty (both live events sit on local
tomorrow in IST).

## 4as. P5 feature-hunt ⑫: free-time reporting (2026-10-23)

Daily planning ask: "when am I free tomorrow?" and the point check
"am I free tomorrow at 5pm" are now answered from the real events:

- busy intervals come from every event overlapping the asked LOCAL
  day (merged over overlaps), computed with DTEND AUTHORITATIVE —
  a latent `max(default 2h, DTEND)` bug inflated every short event
  into a 2-hour block (the in-progress "Right now" logic from ⑨
  carried the same inflation; both now honor the DTEND, the 2-hour
  default only for DTSTART-only events — and the answer says
  "counted as 2 hours" whenever that guess applies).
- gaps ≥15 minutes inside 08:00–22:00 LOCAL: `Free tomorrow:
  08:00–18:15, 19:15–22:00 (08:00–22:00 local).` (the 18:15–19:15
  block is exactly the recovery-demo's DTEND in IST — live-check).
- an all-day event → "Not free any time <day> — an all-day event
  blocks the whole day."; no events → "You are free all day <day>".
- the point check names the blocking event and its LOCAL span, or
  "You are free <day> at <time>."
- heavier planning (next week, dates) honestly declines: "I compute
  free time for a single named day — …" instead of a sweep
  masquerading as an answer.

**Live probes (real core + real lab.ics)**: gap report above; point
check → free at 17:00; next-week → honest limit reply.

## 4at. P5 feature-hunt ⑬: laptop health reading (2026-10-23)

"how is my laptop?" had no capable tool — a daily-triage ask on the
device the calendar actually writes to. `system.health` (laptop
PUBLIC/SAFE, like the battery read, never-auto fields false by
construction):

- instantaneous /proc + statvfs facts only: home-fs disk
  total/free/used-%, RAM total/available/used-%, 1/5/15-minute
  load, CPU cores, uptime days, recorded in the result with the
  honesty line "no judgments, no predictions".
- aliases (laptop health / system health / how is my laptop /
  disk space / disk usage / free memory / memory usage) route only
  when the capability is really advertised; agent-side `_def`
  explicitly PUBLIC (the agent's own `_def` defaults RESTRICTED —
  a parity trap that had to be overridden by hand).
- the DETERMINISTIC fallback path gained a formatter: a verified
  reading answers with the real numbers even when the hosted LLM
  is degraded — a bare "Done: system.health." after a verified
  execution was a truth leak for the LLM-free path.

**Live probes (real core + real laptop-1)**: "how is my laptop" →
`Laptop: disk 79.1 GB free of 199.1 GB (60.3% used); RAM 46.0% used
(7.79 GB free of 14.44 GB); load 0.72/0.8/0.89 on 12 cores; up 0.13
days.`; "what is my disk space" → same full reading; direct
/v1/exec → `succeeded` with the same numbers.

## 4au. P5 feature-hunt ⑭: weekly calendar digest (2026-10-23)

"what did I do this week?" / "what did I do last week?" change the
report shape from the 7-day sweep to a Monday-anchored ISO WEEK
digest:

- events grouped by their LOCAL calendar day, in day order, with
  per-day counts: `Calendar this week (3 event(s)): Thu Oct 22: 1
  — Zara status sync (lab); Sat Oct 24: 2 — Test recovery demo;
  Zara status demo.` (both Oct-24 events sit on local Saturday in
  IST — the local-day grouping keeps them together); more than 6
  titles on one day collapse honestly to "+N more"; all-day
  events are labelled; RRULE occurrences walk the week with the
  window start as the walker's anchor.
- an empty week is named: "Nothing on the calendar last week." —
  no sweep masquerading as a digest. Month asks ("last month")
  honestly stay with the hosted parser.

**Live probes (real core + real lab.ics)**: this-week digest
(3 events across 2 days, both Oct-24 events grouped on Saturday)
and the named empty for last week.

## 4av. P5 feature-hunt ⑮: the morning brief (2026-10-23)

"good morning" / "morning brief" / "brief me" / "what's my day
like" / "what's on my plate (today)" now answer with ONE
deterministic, read-only composite (`core/morning_brief.py`, new
`MorningBriefFlow`):

- three REAL reads stitched per turn, reusing the exact tested
  machinery rather than a parallel implementation: CalendarFlow's
  today-scope turn (its scoped head is absorbed into the section —
  never double-printed), WeatherFlow's turn ("weather today",
  message verbatim through — the flow keeps owning source/time/
  failure wording), and system_health's numbers (same figures the
  system.health answer reports, compact subset);
- per-section NAMED degradation: an offline weather fetch or a
  failed health/ calendar read shows that section's honest failure
  line — a section never silently disappears and nothing is
  invented;
- header stamps the LOCAL day (host-TZ honesty rule) and the
  message closes "read-only brief; nothing was executed";
- punctuation joining is single-period (a live probe caught
  "asks.. Laptop" double periods; `_dot` now joined, pinned by
  test).

Live-checked message (real core, real lab.ics, weather
unconfigured): `Morning brief for Friday, Oct 23: Calendar:
Nothing on the calendar today. Weather: Where? Name a city (…
ZARA_WEATHER_LAT/LON…)… Laptop: disk 79.1 GB free of 199.1 GB
(60.3% used); RAM 46.4% used; up 0.15 days.` — the calendar today
scope is forward-only (batch 9 pin), so at 22:15 local the two
Oct-24 events correctly do not appear.

**Routing guard (live-verified)**: "good morning, add scotch to my
calendar" does NOT land on the brief — the anchor regex is
end-anchored, action words break the match, and the turn goes to
the calendar write path (clarification ask for a quoted event).
Brief triggers are: good morning (+ optional zara tail), morning
briefing, daily briefing, brief me, what's my day like, what's on
my plate (today).

**Tests** `tests/test_p6_morning_brief.py` (8): narrow anchors vs
device/call/action asks; live-shaped composite (event shows,
empty-day honesty, LOCAL header); no double periods; per-section
degradation for weather/health/calendar failures; calendar head
absorption.

## 4aw. P5 feature-hunt ⑯: move/reschedule a calendar event + the
dispatch dependability repairs (2026-10-23)

**Feature**: "push my \"Dentist\" meeting to friday 3pm", "move
\"X\" in my calendar to tomorrow". Same honesty class as removal:
per-event human approval, laptop-side single-source behavior
(`core/builtin_tools.calendar_event_move`), never auto-approved.

**Tool** (`calendar.event.move`, `["calendar.write"]` family,
CONFIRM, `NEVER_AUTO_APPROVE_CAPS`):
- Selection EXACTLY like removal (uid, or summary + optional
  dtstart/date pin; ambiguity refuses naming candidates; absence
  refuses; nothing guessed). Reused `_scan_vblocks` — extended with
  DTEND/tzid + VALUE=DATE kind capture (removal untouched).
- Time KINDS never mix: all-day → clock-time refuses (a duration
  would be invented); timed → date-only refuses (the end would be
  dropped silently). Destination forms: `new_start` full pin;
  `new_date` keeps the event's own time-of-day; `new_clock`
  (HH:MM/HHMM) keeps the date — the clock is host-local, named so.
- Duration preserved: DTEND shifts by the same delta unless
  `new_end` pins it; a block with no DTEND moves its start only and
  the report SAYS the default duration applies; DTEND insertion is
  supported for end-pinning, param lines (TZID…) preserved
  byte-identically.
- A same-title event already at the destination refuses
  (collision-before-write); a repeating parent moves the whole
  series and says so; atomic tmp+rename; re-parse verification under
  the SAME selector semantics (count unchanged, old value gone, new
  start matches; a byte-level no-op assert).
- Shared `_cal_target` extracted (add/remove/move single-source).

**Chat shape** (`_calendar_move_hit`): move|push|postpone|
reschedule|shift + quote + "my calendar". Selector reads ONLY the
quoted slice (a destination day word must not pin the selector);
destination from the tail: day+clock → full pin, day-only →
new_date, clock-only → new_clock, all-day → date; neither → honest
clarification example. Cancel/remove verbs stay on their matcher;
the destructive guard needs no new exception (move verbs aren't
dangerous).

**TZ write-frame fix (third live catch, R40.1)**: `_cal_shape` now
stores the REAL UTC instant for asked local times (was: local
digits wearing a Z). Verified live: "move demo 2 tomorrow 11am" →
stored `053000Z`, moved "to tomorrow 3pm" → `093000Z`, chapter-read
back as "Sat Oct 24 15:00 (+0530)" — the operator's ask round-trips
honest. Selector pins, full pins, ends, and the three pinned test
helpers updated in the same frame.

**Dispatch dependability repairs (R40.2-3)** — the failure this
batch live-caught while probing: approved move jobs sat CLAIMED for
90s then expired while the (alive) agent polled on:
- `wait_for_job`'s parked long-poll line referenced a bare
  `JOB_POLL_SLICE_S` (class attr invisible to a bare name) →
  NameError → HTTP 500 on EVERY parked poll → agent swallows, re-
  polls at full speed (observed 196k rounds/80 s) → rate-limit
  storms. Fixed: `self.JOB_POLL_SLICE_S = 2.0`.
- Zombie-claim redelivery: the owner's state read of a claimed job
  is its delivery receipt (`claim_ack`); the proxy waiter re-parks
  an unacked claim (`CLAIM_ACK_TIMEOUT_S = 8.0`) to pending for
  redelivery, bounded by `MAX_JOB_DELIVERIES = 3`; the cap
  exhausted terminates expired — Task 8's terminal-expiry pins and
  expired-never-returns-to-pending all re-verified green.

**Live evidence** (core `http://127.0.0.1:8080`, agent laptop-1):
add demo approved → `verified` appended; chat move routed
`calendar.event.move` {'summary','new_start'} → approved →
`succeeded/verified` in ~12 s (previously 90 s → expired); title
read-back honest-label as above; both demos cancelled via quoted
remove (each `verified`, file back to its 3 real events).

**Tests**: `tests/test_p6_calendar_move.py` (21: selection
fidelity, kind-mixing refusals, duration preservation, all-day
moves + pinned end, repeating-series say-so, collision refusal,
no-DTEND say-so, TZID param preservation, add→move→read roundtrip,
registry/agent parity, matcher pins incl. cancel-stays-remove,
quote-only selector, absent capability fall-through);
`tests/test_job_redelivery.py` (6: receipt ack, redelivery
completes, cap→expired never resurrects, acked-claim untouched,
owner-scoped receipt, pending-completion wins, short-deadline pin).
All Task 8 timing/lease/longpoll suites re-run green (88 tests).

**Incident + restore (same batch)**: the batch's first write of the
move function spliced text up to `def peripheral_list` and
accidentally deleted the `_CLASS_ACTIONS` table that sat between the
move code and the peripheral builtins — caught later by the full
suite (`peripheral.describe` NameError), NOT by the batch battery.
The table could not be recovered from git (the peripheral work is
uncommitted); it was restored from evidence only: the routing test's
pinned actions (usb_storage shows storage.stats/storage.copy as
`{"action": …}` items), `android/lib/device_panel.dart`'s
`_classActions` (usb_storage → volumes/stats/copy/move; display →
display.report; everything else honestly empty), and the describe
fallback shape that names the item keys. Removable-storage classes
(`removable_disk`, `cardreader`) were given the same storage cards as
`usb_storage` (the storage tools operate on mounted removable media
regardless of the fine-grained class). LESSON recorded: a splice
bounded by "insert before X" must assert what it is deleting — every
byte removed by an edit belongs in the edit's diff review, not just
what is added.

**Known limits (honest)**: the chat shape requires the event title
in quotes and a "my calendar" phrase (an unquoted "push my 2pm
meeting" falls to the hosted parser — a `when`-anchored selector is
future work); `new_clock` interprets host-local (named in the tool
contract); at-least-once delivery means a re-delivered tool may run
twice — device tools stay honest under re-run (a move's second run
refuses at selection because the old start is gone); a same-title
+different-title collision is checked only within titles (two
different titles at one slot are allowed — calendars legitimately
overlap); retrospective ops (per-occurrence move of a recurring
series) remain unsupported and refused with an honest whole-series
message.


## 4ax. P5 feature-hunt ⑰: the disk breakdown ("where did my disk
go") (2026-10-24)

**Feature** (non-calendar direction by user instruction): "what's
using my disk space", "where did my disk go", "what's taking up all
my space". A read-only, bounded, in-turn answer — the daily-use
question `system.health` cannot answer (it gives free space, not
the breakdown).

**Tool** (`storage.scan`, `["storage.read"]`, PUBLIC/SAFE, in-turn;
laptop-side):
- single source: `core/storage_scan.scan_dir_usage` (stdlib;
  agent-side wrapper `device/linux/tools_linux.storage_scan`
  applies the house scope via `_fs_path` — allowed roots only,
  sensitive paths refused, default home).
- dust/gdu/dua semantics, interface only (RESEARCH R41): level-1
  ranking is recursive-subtree apparent size (st_size) with live
  per-ancestor accumulation; symlinks never followed/counted; hard
  links deduped within a bounded 200k-inode window (stated in the
  result's semantics line); unreadable directories skipped AND
  counted (bounded sample kept).
- budget-bounded (default 4 s, cap 30 s; entry cap 400k): a stop is
  `partial=True` with the reason and Floor semantics ("sizes are
  floors, items may be missing") — never a silent approximation
  wearing a total's clothes.
- result schema: root, items[{path(bytes,files,dirs)}],
  root_files_bytes/count, total_bytes (top-level sum only),
  entries_seen, symlinks_skipped, unreadable, budget{partial,
  reason}, semantics, detail.

**Chat** (`_disk_breakdown_hit`): breakdown verbs (where did |
what(')s using/taking up/eating/consuming | biggest|largest
folders/files | disk usage breakdown) + disk/space/storage wording;
matched BEFORE the substring aliases so "disk usage" (an
alias→system.health) cannot swallow the breakdown ask; "how much
disk space" stays on health. Absent capability falls through
honestly. The reply formatter is deterministic (LLM-free path never
degrades a measured walk to "Done"): top-N lines with human bytes +
file counts, loose-files line, PARTIAL + unreadable honesty tails.

**Tests** `tests/test_p6_disk_scan.py` (14): exact-byte roll-up
fixtures (which caught the total double-count), symlink exclusion,
hardlink dedup, unreadable-dir accounting, budget/entry-cap floor
semantics, wrapper scope refusals (sensitive + outside roots),
registry def presence (PUBLIC/SAFE/["storage.read"]/linux),
matcher routing incl. health asks kept + absent-capability
fall-through, deterministic formatter partial + clean shapes.
Prior battery re-run green (`test_storage_ops`, `test_stage2`
parity, `test_p6_timer_confirm_chatpath`).

**Live evidence**: chat "what is using my disk space" on the real
core ran in-turn (executed=True) and answered with the real home
breakdown — 20.7 GB .android (AVDs), 5.6 GB .cache (92,660 files),
110.9 KB loose files — ranked, no approval needed (read-only), no
fabricated numbers.

**Known limits (honest)**: apparent size (not on-disk blocks);
hardlink dedup is bounded (double-counts beyond the 200k window);
the walk is single-threaded (budget covers slowness, parallel walks
rejected as a dependency); the chat shape always scans HOME (an
explicit subfolder ask is a future path arg through quoting); the
answer's top-N mixes depths (dust semantics) so level-1-only
coverage is not promised beyond the top N slots themselves.

**Launcher incident**: /tmp/opencode's wipe deleted start_core.sh
mid-session; rebuilt from observed behavior (dev-token contract +
state-DB override + LLM env), not from memory (RESEARCH R41.4).

## 4ay. P5 feature-hunt ⑱: everyday unit + timezone converters
(2026-10-24)

**Feature** (core-local, no device): "convert 5 kg to lb", "what time
is it in tokyo", "9am in tokyo" — the two asks a personal assistant
gets daily that depended on nothing so far.

**Tools** (BUILTINS registry, PUBLIC/SAFE, `supported_devices=["any"]`,
in-turn through a NEW `LOCAL_EXEC_CAPS` contract):
- `convert.units` {value,unit,to}: length (mm/cm/m/km/in/ft/yd/mi),
  mass (mg/g/kg/t/oz/lb/st), data (decimal KB/MB/GB/TB AND binary
  KiB/MiB/GiB/TiB kept distinct, never merged), temperature (C/F/K
  through exact Celsius formulas). Same-kind only; cross-kind and
  unknown units are honest ValueErrors that name the offender and
  the supported sets. Common plural s-forms ("kgs", "gibs") and a
  trailing period parse.
- `time.zone` {zone, clock?}: world clock. City alias map (≈55
  everyday places, `NickCirv/timezone-converter` +
  `mattjohnsonpint/TimeZoneConverter` interface ideas, stdlib
  re-implementation) + ANY raw IANA id resolved case-insensitively
  (case-sensitive ZoneInfo keys vs free-typed operators — a cached
  lowercase map over `available_timezones()` fixes it). Pinned
  host-local clock = the NEXT occurrence of that wall time (today's
  if still ahead, else tomorrow — the calendar day-word convention),
  and the chosen local date is stated explicitly. Unmapped places
  refuse honestly (the ValueError text names "common cities or any
  region/city IANA name").

**LOCAL_EXEC_CAPS contract** (live-caught R41.5 regression, found on
the REAL turn inside 30 s of wiring): the first wiring put the two
caps in BUILTIN_LOCAL (parser-service builtins) — the turn then
answered with the "I can: …" capabilities listing INSTEAD of
executing. The two classes are distinct: BUILTIN_LOCAL = answered
inline without any registration; LOCAL_EXEC_CAPS = registry-backed
core-local executors that run through the /v1/exec no-device_id
contract (governor + engine policy gates intact, NO device trust
gate) so they keep working when every agent is offline.

**Chat**: structural matchers (values/units/city names are shapes a
substring alias cannot span): `_convert_hit` (value REQUIRED — a
valueless ask clarifies with the honest example, never a silent 1.0)
and `_zone_hit` (pinned-clock shape FIRST but never when
remind/alarm/timer owns the turn; then the anchored now-shape with a
greedy tail capture + noise-word trim so multi-word cities survive).

**Tests** `tests/test_p6_convert_zone.py` (19): exact conversion
fixtures incl. temperature round-trip, decimal/binary family
distinctness, cross-kind + unknown-unit refusals naming themselves,
plural/period forms, zone now/pinned shapes with a LIVE-computed
next-occurrence expectation, bogus-clock refusal, matcher positive +
anti-steal (remind/timer asks stay clear), absent-capability
fall-through, deterministic formatter shapes, and two END-TO-END
turn tests (build_stack, no devices at all) proving the
LOCAL_EXEC_CAPS contract + offline independence.

**Live evidence** (real core, android-phone requester): "convert 5
kg to lb" → executed "5.0 kg = 11.02 lb."; "what time is it in new
york?" → "America/New_York: 10:56 EDT (-0400), Sat Oct 24 — 9.5 h
behind host"; "what is 9am in tokyo" → "09:00 host-local (Sun Oct 25,
+0530) = 12:30 in Asia/Tokyo (JST, Sun Oct 25); UTC 2026-10-25T03:30Z."

**Known limits (honest)**: everyday units only (GNU-units exactness
deliberately not adopted); data units span families by factor rule
(1 GiB = 1.07 GB is stated, not "rounded"); the city map is finite
(unknown place → honest refusal, not a guess); pinned clocks carry
no DST caveats beyond what zoneinfo computes; no currency (rates
need live data — out of the honest-offline surface by policy).

## 4az. P5 feature-hunt ⑲: the process snapshot ("what's slowing my
laptop") (2026-10-24)

**Feature** (laptop-side, read-only): "what's slowing my laptop",
"what's using all my CPU", "what's hogging my ram", "top processes" —
the third diagnostics surface next to system.health (numbers) and
storage.scan (disk): the answer health cannot give (which PROCESS).

**Tool** (`system.top`, `["system.health"]` family, PUBLIC/SAFE,
devices=["linux"]): single behavior source in
`core/builtin_tools.system_top` (agent delegates like health — the
laptop's /proc is the truth). top(1) -b / btop interface ideas,
stdlib re-implementation (RESEARCH R42): ONE non-interactive
snapshot, per-process CPU measured as a delta over a ~0.6 s sampling
window (btop's "cpu lazy" — ranked over the window, never process
lifetime), memory = resident set (RSS), names from cmdline's FIRST
ARG (basename, bounded 60) — full command lines deliberately NOT
kept (an interactive terminal's argv can embed secret tokens; the
loss is stated in the result), kernel threads shown bracketed
([kworker/…]), vanished mid-walk pids skipped AND counted,
sort follows the ask (ram/memory wording → memory sort, else cpu),
top_n ≤ 8.

**Timing-invariant catch (R42.1)**: the first wiring used
`timeout=10.0` (a 0.6 s sample feels instant) — test_T0 refuses
every device tool proxy wait ≤60 s (the Android throttled poll gap).
storage.scan's `timeout=60.0` (batch 17) was violating the same `>`
60.0 pin but sat BEHIND the earlier failure in the loop's def order;
both now 90/91 s. The invariant is doing exactly what it was pinned
for — it also caught a batch-17 slip that full18 could not see
(storage.scan did not exist yet).

**Chat** (`_top_hit`): breakdown verbs (using|hogging|eating|
consuming|slowing|bogging down) near cpu/ram/memory/process/laptop
wording; matched BEFORE the memory-usage alias (health keeps the
single-number asks: "how much memory usage", "free memory" carry no
breakdown verb) and AFTER the disk guard — disk/storage/space in the
ask returns None so storage.scan keeps its own turns. "laptop"
resolves through the existing device-word machinery as the preferred
executor.

**Tests** `tests/test_p6_system_top.py` (11): real-/proc structural
assertions (counts, ranked ordering, honesty fields, bracketed
non-empty names), memory sort, top_n bounds, bad-sort fallback,
matcher positives + anti-steal (health asks, disk asks),
deterministic formatter CPU/RAM shapes + stated unavailability.
 LIVE evidence: "what is slowing my laptop" → executed in-turn with
real ranked leaders (ptyxis 2.0%, gnome-shell 1.2%, bracketed
kworker threads) and the names-only honesty tail; "what is hogging
my ram" → RAM leaders (opencode 443 MB, chrome 331 MB …).

**Known limits (honest)**: sampled CPU is % of TOTAL system cpu (a
600 ms window to the nearest tenth at top_n resolution); RSS is not
PSS (shared pages repeat) — stated in the result detail; no
per-core breakdown (not asked); no kill/re-nice (a read-only
surface; controlling processes is a future CONFIRM-gated sibling);
cgroup/containerized hosts can hide entries (unreadable count
discloses).

## 4ba. P5 feature-hunt ⑳: end-to-end internet diagnostic ("is my
internet actually working") (2026-10-24)

**Feature** (laptop-side, read-only): "is my internet down /
working / up", "check my internet", "why is the internet so slow".
`system.network` answers interface state (which NIC is up) — it
cannot answer reachability. The gap: a host with NIC-up can have a
dead resolver, a captive portal, or blocked TCP.

**Tool** (`system.connectivity`, `["system.health"]` family,
PUBLIC/SAFE, devices=["linux"]): single behavior source
`core/builtin_tools.system_connectivity` (agent delegates). THREE
bounded layers, keyless endpoints only:
1. DNS: getaddrinfo on two stable names;
2. raw TCP: connect to three allowlisted public relay endpoints;
3. end-to-end HTTPS: HTTP(S) 200x from keyless probe URLs
   (gstatic generate_204, first hit wins over the cloudflare trace
   fallback) — DNS+TCP+TLS+HTTP as the web sees it.
**Bounding is architectural, not a constant:** a dead resolver hangs
getaddrinfo far past socket timeouts (observed live — the first
version hung >40 s). Every layer therefore runs in a daemon thread
joined on a BUDGET (3.2 s DNS/TCP, 7 s HTTPS); a join timeout is
honestly "unreachable", never "pending". Worst-case wall ~30 s,
device def timeout 90 s (timing invariant held).

**Verdicts are descriptive labels** (full / limited_dns /
limited_web / offline) with measured latencies and stated failures —
never attitude, never predictions. The web check wins on the FIRST
2xx of two candidates: one blocked endpoint is not evidence of no
internet. The wording says "keyless diagnostic endpoints", never
"the internet works everywhere".

**Chat** (`_connectivity_hit`): end-to-end internet question shapes
(down|offline|broken|up + is/are … working/connected/online |
internet connectivity check) — deliberately narrower than the bare
network/wifi aliases (which keep their own surfaces: system.network
interface state, system.wifi.state, wifi settings/set asks).
Test-pinned 10-case routing table.

**Tests** `tests/test_p6_connectivity.py` (8): real probe structure
+ bounded wall time (<32 s), timing invariant + def fields, matcher
positives, alias-surfaces separation, absent-capability
fall-through, formatter full / limited_web / offline / limited_dns
shapes with measured numbers.

**Live evidence** (real core + laptop): "is my internet working" →
executed in-turn: "limited: raw connectivity answers but the web
check fails — DNS ok on 2/2 names; relay connects: 1.1.1.1 258.3 ms,
8.8.8.8 204.6 ms, 9.9.9.9 205.2 ms; web check failed (status none).
Bounded probes to keyless diagnostic endpoints only." — the honest
description of THIS lab's TLS-blocked egress; on an unfiltered
network the same probes read "full".

**Known limits (honest)**: latency only — no Mbps throughput (a
download probe floods/samples networks; refused by the honest
surface for now); endpoints are fixed keyless names (an ISP blocking
exactly those shows limited_web — two candidates reduce that risk);
no IPv6-specific story (getaddrinfo may return v6 first — subject to
host resolver order); the ask runs on the LAPTOP (the def is
linux-scoped) — "is the PHONE's internet working" is a future
android-scope sibling.

## 4bb. P5 feature-hunt ㉑: the file-level lens ("which files are
over 1 GB") (2026-10-24)

**Feature** (laptop-side, read-only): the second view of the disk
question. dust/gdu give BOTH directory and file views; batch 17
shipped folders (`storage.scan`), this ships files:
"which files are over 1 gb", "find my biggest files",
"my largest files".

**Tool** (`storage.find`, `["storage.read"]`, PUBLIC/SAFE,
devices=["linux"], timeout 91 s): single source
`core/storage_scan.find_big_files` (iterative-stack walk — the
recursive directory aggregator would not scale to a 24-deep tree
without recursion caps doing the work again). Apparent size
(st_size), symlinks never followed/counted, hard links deduped
within the bounded window, min_bytes floor filter, top_n ≤ 50, entry
cap 400k + budget (default 6 s) → `partial=True` floors that SAY
"HOWEVER more big files may exist". Paths ARE returned here — a file
answer without paths is useless — bounded to top_n and only from the
house scope (`_fs_path`: allowed roots, sensitive refusal).

**Chat** (`_bigfiles_hit`): FILE wording (files, not
folders/directories-only) + a size intent (biggest|largest|over
<size>|bigger than|too big); the size floor parses everyday DECIMAL
units ("over 1 gb" = 10**9 — the ask's family, k/m/g/t accepted);
deletion asks refuse at the matcher with the honest guide ("say
'store the file and I'll confirm the delete'" — delete is another
conversation, never smuggled under a read-only tool); folder asks
stay on `storage.scan`.

**DECIMAL display repair (R43.1)**: both storage formatters
(`storage.scan`, `storage.find`) previously divided by 1024 while
labeling GB — the floor filter parsed "1 gb" = 10**9 but the
display said "3.9 GB" for a 4.2e9 file. Label and filter must share
a family: file sizes are now decimal end to end (the everyday
listing convention; the fixtures changed from 1024**n to 10**n so
the mixed-family bug cannot return).

**Tests** `tests/test_p6_storage_find.py` (14): exact-byte ranking +
floor filter, no-floor all-files, symlink exclusion, hardlink-once,
budget floor wording, bad root, wrapper scope refusals, registry def
+ timing invariant, core↔linux parity, matcher positives with floor
parse, folder-ask separation, delete-shape refusal, absent-capability
fall-through, formatter ranked/empty/partial shapes. Prior battery
(`test_p6_disk_scan` incl. the decimal-fixture repair) re-run green.

**Live evidence**: "which files are over 1 gb" → executed in-turn:
"Biggest files: 4.4 GB …/system-images/android-36/…/system.img; 2.3
GB …; 1.2 GB …/assistant-worktree-backup-20261019e.tar.gz; … PARTIAL
scan — stopped at the time budget after 284817 entries; more big
files may exist." — real paths, decimal labels, honest floor.

**Known limits (honest)**: apparent size; hard link dedup bounded
(the window is stated); deep trees under a tight budget are floors
(disclosed in the same breath); no per-owner info (files belong to
the host user here); same read-only posture — deletion/cleanup is a
separate CONFIRM conversation, never auto-suggested by name list.

## 4bc. P5 feature-hunt ㉒: the checksum utility ("sha256 of my
file") + the failed-stage honesty repair (2026-10-24)

**Feature** (laptop-side, read-only): `fs.checksum` — sha256 default,
sha512/sha1/md5 only when NAMED explicitly, chunked walk over ONE
device-scoped file. Digest only (content never leaves the laptop);
bytes_hashed counted; the 512 MB prefix cap makes over-cap digests
honest PREFIX digests labelled "NOT a compare-safe full digest"
(NOT whole — never a silent truncation pretending completeness).
Scope via `_fs_path` (allowed roots; sensitive subtrees refused —
tested for ~/.ssh, /usr/share, /etc/hosts).

**Chat** (`_checksum_hit`): digest word + a path captured from the
RAW utterance (quoted slice, or ~//… token) — paths keep their case
and spaces ("My File.zip"); a capture from the lowercased text slice
was the first bug (fixed before commit). A valueless-path ask
clarifies with the exact example (never guessing a default). The
digest-word probe ("keyboard" contains "key"…) is NOT a matcher —
only sha*|md5|checksum|hash words trigger.

**FAILED-STAGE HONESTY REPAIR (R43.4, live-caught)**: the missing-file
probe answered "I couldn't do that: failed" — the real ValueError
("cannot read …: [Errno 2] …") was sitting in the record's `error`
field while the formatter preferred a missing `reason`. `_maybe_execute`
now renders reason → error → stage (dict errors unwrap their
`message`), and a bare "failed" reason is replaced by the actual
error text. This benefits EVERY device tool's failure path, not just
checksums. Live re-probe: "sha256 of ~/definitely-missing-file.bin" →
"I couldn't do that: RuntimeError: device job failed: ValueError:
cannot read … [Errno 2] No such file or directory …" — machinery
wording included, truth unedited.

**Small-file display floor added** (kB tier) — "0.0 MB hashed" for a
9 KB file was a rounding lie-adjacent; every tier is now stated
(B/KB/MB/GB, decimal family consistent with the storage surfaces).

**Tests** `tests/test_p6_checksum.py` (13): digest == hashlib on
exact fixtures, cap-stop digest equals digest-of-prefix (bytes NEVER
overshoot the stated cap — chunk reads are clamped to the cap
boundary), named weak digests, refusals (unsupported algo, dir,
scope), registry/timing/parity, matcher shapes with case + spaces
preserved, valueless-path clarify, absent-capability fall-through,
formatter full/prefix/floor shapes, and the failed-stage honesty
turn test (error wins over bare "failed").

**Live evidence**: prefix digest of a 1.0 GB backup ("first 536.9
MB hashed (512 MB cap). NOT a compare-safe full digest."); full
digest with KB tier; missing-file honest error through the real
device path.

**Known limits (honest)**: 512 MB prefix cap on huge files (stated
every time, compare-unsafe beyond — no "unbounded hashing"); md5/
sha1 exist for legacy compare only and are never the default; the
error text may carry machinery wording (RuntimeError/device-job
wrappers) — truth first, wording second.

## 4bd. P5 feature-hunt ㉓: the growth lens ("what grew on my disk")
(2026-10-24)

**Feature** (laptop-side, read-only + assistant-internal baseline):
the third storage view. Folder rundown answers "where did my disk
go" NOW; file finder answers "which files"; growth answers "WHAT
CHANGED" — the question a rundown cannot answer because a 20.7 GB
`.android` may have grown 0 or 2 GB since last month.

**Tool** (`storage.growth`, `["storage.read"]`, PUBLIC/SAFE): scan
now, diff against the last saved baseline for the same root (name-
keyed), then save this scan as the new baseline. The ledger
(`core/storage_scan.StorageLedger`, sqlite in
`~/.local/state/zara/zara_storage_ledger.db`) is keyed-replace per
(root, day) — at-least-once redelivery cannot stack baselines — and
bounded to the newest 8 days per root. The persisted payload is
LEVEL-1 bucket sizes ONLY (no file paths, no content): assistant
machinery, not user content — deliberately outside the approval
gate (the same rule that lets the core's own state databases
write), and the result says so.

**Three live-caught lessons (R44)**:
1. **Diff across shapes is diff across nonsense.** The first ledger
   rows stored the scan's top-N across the nested union — and
   `.cache` (5.6 GB) was not even IN that top-N (deep AVD chains
   flood it), so a real 3 MB change on disk showed "nothing
   changed". Growth now tracks LEVEL-1 buckets only (complete,
   stable, non-overlapping), and rows saved under an older SHAPE
   marker are treated ABSENT — the next run re-baselines honestly
   instead of diffing nonsense (shape version `level1-v1` in the
   payload, old rows skipped and same-day overwritten).
2. **A growth tool that can never finish is a refusal machine.**
   Whole-home growth refused twice live: first at the 12 s budget
   (home walk needs ~13 s for 1.02M entries), then at the 400k
   entry cap (fires in 2.8 s — a TIME raise cannot fix an ENTRY
   bound). The cap is now threaded separately (2M for growth) and
   the refusal still says the fix when a bound is genuinely hit.
3. **Equal-byte buckets are unchanged, not gone.** The state chain
   fell through to "gone" when both snapshots held a bucket with
   equal bytes — an exact-fixture test pinned grown/shrunk/new/gone
   AND the skip; "gone" now means the bucket is absent from the
   current scan, not merely equal.

**Chat** (`_growth_hit`): BOTH disk wording AND growth wording
(grew|growth|changed|delta|since) required — folder rundown and file
finder keep their own turns; delete-shaped asks refuse with the
honest guide (read-only surface, cleanup is another conversation).
Scoped-folder growth asks clarify honestly (the whole-house run is
what the baseline is FOR); a scoped diff is reachable via exec-level
path.

**Tests** `tests/test_p6_growth.py` (7): pure-diff state pins incl.
the unchanged-skip and the gone-vs-equal distinction, partial
refusal names the fix (and carries the 2M cap to the walker),
first-run then diff then no-change (exact fixtures), ledger
keyed-replace + redelivery non-stacking + 8-day bound, matcher
separation, registry + timing + parity, formatter first-run/diff/
no-change shapes with byte-human arrows.

**Live evidence** (chat through the real pair): first ask → "No
earlier baseline for /home/thiyan — saved one now (aggregate
level-1 sizes only) … 5 bucket(s) recorded."; after a shape
migration → honest re-baseline; after creating a 3 MB file →
"Disk growth since 2026-10-24T21:52:43+05:30: +3.1 MB .cache
(grown). … both scans completed (non-partial)." (probe artifacts
removed afterwards; leftover ~3 MB scratch from the failed first
design was also deleted.)

**Known limits (honest)**: level-1 resolution — a change inside a
renamed SUBtree reports gone+new under its bucket, not a merge;
both scans must complete (a changed budget between snapshots is
honest noise → refusal, not a diff); the baseline is per-host
ledger state (delete the ledger file to re-baseline); buckets
match by level-1 name (rename of a top-level dir = gone+new, said
plainly); no auto-cleanup, ever.

## 5. Known limits (honest)

0. **Stage 22/22b/22c/23/24**: bluetooth pairing of NEW devices requires an
   interactive pairing agent — unsupported by design (connect is
   limited to already-paired devices after confirm); screen
   streaming/casting media path is IMPLEMENTED end to end (real sd-bus
   fd transport + GStreamer `pipewiresrc`) AND live-validated up to the
   consent-answered, fd-received, target-pinned stage (§4h); the
   WirePlumber-media-class fix (§4h, item 6) awaits one more
   interactive dialog to prove STREAM_ACTIVE with live probe evidence.
   No unattended run ever claims STREAM_ACTIVE; a receiver protocol
   stack (AirPlay/GCast/RAOP pairing + stream framing) is still NOT
   implemented (user decision) — media goes to a local discard sink
   only; GNOME-form portals answer SelectSources instantly without a
   dialog, so consent is claimed ONLY on the Start response
   (`_consented` state-tracked), every failure path closes the fd and
   tears the session down (verified cleanup or explicit
   `cleanup_unverified`); `sources` payloads that ARE present but
   unusable are still hard-refused — the GNOME-form absent-list
   path is allowed because the portal binds the fd server-side to
   the consented stream (§4h); network
   mDNS
   discovery runs with a
   bounded stdlib probe and found NO advertisements on this LAN
   (still an honest result — quiet network vs filtered responder
   cannot be distinguished by design); `gio`-based MTP/PTP
   classification depends on the GVFS layer's documented mount
   format (fixture-tested; a systemd/gvfs format drift would degrade
   to honest "no MTP mounts" meta, never wrong claims).
0. **Stage 21 (prior session)**:NL mission phrasings cover the bounded
   deterministic alias set only (the hosted path handles richer
   requests); the Mutter display-apply path refuses until validated on
   real external-display hardware; mission restart recovery requires
   `PersistentMissionEngine` to be wired via `mission_db`.
1. **Wake FAR**: no threshold reaches FP≤0.5/hr with current
   synthetic+limited-real data; live-room behavior unmeasured (user-gated).
   Retrain lesson recorded: 3×10k-step retrain made FAR worse (v1 shipped).
2. **Disk floor** blocked the ACAV100M background download → wake-quality
   ceiling as measured above.
3. **TLS certificate provisioning is a deployment decision**: Core + both
   clients now fully support HTTPS (server flags, strict-transport
   enforcement, fail-closed validation — see §2b), but no CA cert exists
   for this machine and undecided who stages trust on the phone. ACME
   automation deliberately not built (requires a domain + public
   exposure = user decision). Until a cert is deployed, plain-HTTP dev
   transport remains the working default.
4. **live-room ankle-biter long-run FAR**: any future live numbers need a
   recorded continuous session on a real room; nothing claimed untested.
5. Flutter signed-transport cert-validation behavior is delegated to the
   platform TLS stack (Dart HttpClient/OkHttp defaults) with the
   transport-guard unit tests as the in-repo anchor; widget-level TLS
   failure UX is not separately tested.
6. PROFILE-style runbooks for retraining wake models are dev-time-only
   (REA not shipped in runtime paths; unchanged).
7. **Wayland**: `desktop.window.active`/`window.list` are honestly
   unsupported without a foreign-toplevel helper — live-verified 2026-10-19
   (GNOME `Eval` gated `(false, '')`; `Introspect.GetWindows`
   `AccessDenied`; no foreign-toplevel protocol export). `desktop.apps.list`
   /`desktop.session` work. X11-only window probes return `supported: false`
   rather than guessing.
8. This trace's `emu-v2` enrollment ended **revoked** (deliberate
   fail-closed test, /tmp/opencode/zdata-v2trace, disposable);
   user-owned enrollments elsewhere were untouched.

## 6. Verdict

The shipped union is: wake-word detection live and honestly measured;
per-device auth upgraded from shared-secret headers to Ed25519
challenge proofs across Core, Linux agent, and Android — now binding the
exact request body (v2 canonical) with fail-closed tamper handling —
with the raw key reduced to a one-time bootstrap; transport
confidentiality moved from "assumed HTTPS" to a implemented, tested TLS
configuration with insecure downgrades refused by default on all three
sides. Android re-validated end-to-end on an emulator over the new
transport. Remaining items are user-consent-gated live-room wake
validation, wake-model data budget (ACAV100M), and the TLS certificate
provisioning decision — environment/user-gated, not implementation-gated.

## 7. Capability matrix (final)

| Capability | Implemented | Tested |
|---|---|---|
| "Hey Zara" wake word (v1 model, on-device) | yes | unit (upstream parity) + recorded real takes; **live-room/physical untested** (user-gated) |
| Voice conversation STT→TTS loop | yes | integration (synth audio); physical Android mic/speaker verified 2026-10-04; laptop mic loopback **real-mic verified 2026-10-19** (probe script; speech source synthetic) |
| Memory export (documented format) | yes | unit + persistence + live endpoint tests (2026-10-19) |
| Ed25519 challenge proofs (body-bound v2) | yes | unit (tamper/replay/compat/concurrency) + live socket + **emulator live** |
| TLS transport (server+strict+clients) | yes | unit (real sockets, cert chains) + **live launcher run**; cert provisioning **blocked (user decision)** |
| Android enrollment/pairing/reconnect/revoke | yes | unit + **emulator live** incl. revocation fail-closed |
| Android signed job dispatch → verification | yes | unit + **emulator live** (battery, network) |
| Never-auto-approve class (irreversible actions never grant-liftable) + explicit device-verified gui.tap/set_text/scroll/back | yes (Stage 29) | unit (policy + engine + fabric + Dart runner); live validation device-gated |
| NL intent turn → policy → execution → answer | yes | unit + **live** (battery answer, clarification, unsupported) |
| Approval hold / denial / cancellation | yes | unit + **live** (hold → cancelled, never ran; 409 already-terminal) |
| Browser read-only research (open/extract/search) | yes | unit + **live** (`untrusted` marking) |
| Desktop (Linux) skills | yes | unit + **live** (apps.list, session=wayland); window probes **honestly unsupported under Wayland** |
| Messaging (draft ≠ send, approval before send) | yes | unit (`tests/test_messaging*.py`); no live message ever sent |
| Memory (add/search/correct+provenance/pin/delete/export, secret refusal) | yes | **live** + unit/persistence tests |
| Filesystem/file skills (restricted dirs, approval) | yes | unit (`tests/test_file_workflows.py`) |
| Document intelligence (docs.search/read/find/summarize; PDF page-aware, DOCX, TXT/MD/HTML/JSON/CSV; scanned-honest, OCR-free) | yes | unit (39; real local fixtures + real extraction paths; §4p) — OCR/scanned human documents untested (approval-gated) |
| NL integration for documents/clipboard (deterministic routing + structural matcher + anchor summary copy; resolve/policy/verify unchanged) | yes | unit (17 end-to-end in-process; §4q) — real Wayland/X11 backend + hosted-LLM routing = device-gated |
| Cross-document keyword search (docs.searchtext; bounded composition, provenance per excerpt) | yes | unit (17 incl. mission-path bounds; §4q) |
| Truthful verification + recovery (declared output contracts, explicit clipboard verdicts, mission verdicts persisted, storage withholding, no-replay recovery) | yes | unit (17 end-to-end + 2 contracts; §4r) — real-device recovery windows = device-gated |
| Android automation + evidence-verified steps (host-side adb inventory `system.android.inventory`; bounded read-only `gui.wait`; bounded per-step evidence in mission verdicts; NL phrasings without mis-grounding) | yes | unit (11 on the real pipeline; Dart 6 + flutter analyze; §4s) — real-device validation BLOCKED: no device attached (paired phone + enabled accessibility service is the remaining daily-use blocker) |
| Clipboard read/write (consent per use, verified write, no polling/history/persistence) | yes | unit (25 incl. core contracts; real backend availability; live content never touched in tests) |
| Reliability (long-poll, dedup, restart recovery, audit) | yes | unit + **live** (app restart, core restart) |
| LLM failure mode | yes | **live** (502, no internals leak) |
| Connected-peripheral discovery (USB/storage/display/audio/HID/camera, Lifecycle dedup, privacy payloads) | yes | unit + **live** laptop (§4c) |
| Pendrive storage stats/capacity (bounded, cancellable, category counts) | yes | unit (fake mounts; real plug = device-gated) |
| Verified copy/move/delete (conflict-ask, capacity pre-check, verify-then-delete, explicit confirm) | yes | unit + live /tmp smoke |
| Android observed devices (volumes/USB/displays listings + permission-gated media counts) | yes | unit (4 tests) + gradle compile; hardware = device-gated |
| Flutter device panel (class-aware action cards, honest abstain) | yes | widget tests (4) |
| Cross-device file-transfer missions (phone↔laptop↔pendrive, partial=FAILED truth, ambiguity=WAITING, cancel, restart recovery) | yes | unit (19) + **live** core endpoints smoke; real plug/share = device-gated |
| Flutter transfer-mission card (progress, partial-truth error, cancel via /v1/talk) | yes | widget tests (6) |
| Display-layout describe/preview; confirm-gated verified apply (X11 path) | yes | unit (13) + **live** read-only smokes; real second display = device-gated |
| Bluetooth discovery + registered-device connect (console-gated, advertising-honest) | yes | unit (7 in test_device_stage22) + **live** read-only listing of the paired phone; real connect = device-gated |
| Audio output routing (PipeWire wpctl; confirm + verified re-read) | yes | unit (6) + **live** read-only parse of the real sink topology; no live set-default | 
| MTP/PTP phones via GVFS (confirmed-protocol discovery, no adb) | yes | unit (2; fixture = documented gvfs format); real phone mount = device-gated |
| Touch-input reporting (kernel table; attribution stays unknown) | yes | unit (in display ledger tests) |
| Screen streaming/casting (media) | partial — media path implemented (sd-bus fd + gst pipewiresrc), interactive-user-gated end-to-end | unit (18 in test_screencast_media, deterministic doubles — never live evidence); live read-only smokes (backend fd-capable, gate usable); real dialog answer = interactive-user-gated |
| Screen-cast consent lifecycle (session/consent/teardown) | yes (consent dialog real; media legs now real, §4g) | unit (22 in test_screencast_life + 18 in test_screencast_media) + live read-only wire verification incl. sd-bus CreateSession round-trip and fd-capability probes; real dialog answer = interactive-user-gated |
| Network mDNS device discovery (bounded DNS-SD pass) | yes | unit (21 incl. real UDP loopback round-trip) + live quiet-LAN honest pass; no port scan by construction |
| Physical Android device (vivo) | prior evidence only | not re-validated this session (no device attached) |

## 4be. Feature-hunt ㉔: compound calendar asks + the conflict lens
(RESEARCH R45, 2026-10-24)

Daily use exposed two gaps in the calendar read: a named SINGLE day
scopes the answer but "today and tomorrow" silently degraded to
"today" (the rest of the ask swallowed), and there was no way to ask
whether the schedule has OVERLAPS ("am I double-booked?") — the
free-slot lens reports GAPS, not collisions.

**P6-a — compound multi-day reads** (`core/calendar_flow.py`): day
tokens (today/tomorrow/yesterday/bare weekday, next/last qualifiers)
chained ONLY by and/or-commas scope the read to a list of ≤4 distinct
local days. Prose-separated day words are REFUSED (the single-day
sweep keeps the turn); duplicates collapse; days answer
chronologically in utterance order independence. The surface is a
WHOLE-day digest (like the week digest): each named day gets its own
section — empty or not ("Sun Oct 25: 0 —") — with RRULE expansion
inside the covered window, per-day title cap 6 (+N more), all-day
tags, and an honest unsupported-RRULE note. Empty scope wording names
every asked day ("Nothing on the calendar for today and tomorrow.").

**P6-b — the conflict check**: phrases ("any conflicts tomorrow?",
"am I double-booked?", "overlapping meetings this week", "conflict
check") compute pairwise overlaps between TIMED events in the asked
window: compound | single day | week | default 7-day sweep FROM NOW
(a conflict that already ended this morning is history, not a
conflict). Semantics, bounded: back-to-back (end == start) is never a
conflict; a missing DTEND runs the honest 2-hour default, counted
and noted; all-day events have no clock times — excluded, but counted
and NAMED (first title); pairs shown ≤5, remainder "+N more
overlap(s)"; titles quoted, times LOCAL. Empty is never silent:
"No overlapping events tomorrow." is distinct from "Nothing on the
calendar tomorrow." — the check answers BEFORE the week digest
(specific beats general) and before the sweep.

**Collection is a primitive**: `_collect_pairs` is separated from the
chat wording so the morning brief reuses the EXACT tested computation
(R46.1). A latent parser crash was fixed on the way: `to_utc` degrades
regex-plausible but calendar-invalid stamps to "unresolved" instead
of raising (R45.4).

**Tests**: `tests/test_p6_calendar_conflicts.py` (17: compound
listing/cap/section-for-every-day/chronology/collapse; refusals;
conflict detect/none/back-to-back/no-DTEND/all-day/today-vs-sweep/
pair-cap; guarded anchor; free-ask collision; media-wording
non-collision). All existing calendar suites green
(test_p6_calendar_*: day-scope, week digest, free slots, past scope,
title lookup, write/move/remove). Full python suite: 2339 passed
+10 skipped (midnight-window skips, house pattern).

**LIVE (real core + laptop-1, lab.ics with two late-today events)**:
- "any conflicts this week" → "No overlapping events this week."
- "what conflicts today" → "No overlapping events today."
- "calendar today and tomorrow" → "Calendar for today and tomorrow
  (2 event(s)): Sat Oct 24: 2 — Test recovery demo; Zara status
  demo; Sun Oct 25: 0 — ."
- "am i double-booked tomorrow" → "No overlapping events tomorrow."

**Operational**: the persisted launcher was missing ZARA_ICS_PATH
(craft-reconstruction drift, R45.5) — a healthy core answered
"No calendar configured". Launcher fixed in the repo; the
ZARA_ICS_PATH export is now part of the documented contract. Core
restart + agent relaunch verified (registration does not survive).

## 4bf. Feature-hunt ㉕: the morning brief reads five facts
(RESEARCH R46, 2026-10-24)

The P5 morning brief stitched calendar+weather+health. Two first-
thing answers were missing: whether today's schedule has conflicts,
and what reminders land today.

**Conflicts section**: `CalendarFlow.conflicts_on_date` — the brief
asks the calendar flow for its OWN tested collection over tonight's
day (start-of-day → end-of-day), so chat and brief can never
disagree about the same calendar. Rendering: "Conflicts: none
today." / "Conflicts: 1 today — 14:30–15:00 \"A\" ↔ \"B\"." / capped
pair list; all-day and no-DTEND events keep their named caveats.

**Reminders section**: ReminderService.upcoming (existing tested
service), filtered by due-epoch ≤ LOCAL end-of-day → "pending
today". Degradation words matter: an unwired store is NOT a zero
(absence ≠ emptiness), a failed read names its error, and a
day-late list keeps the "(N later)" tail so an unstated "today"
filter is still honest.

Order on the line: Calendar → Weather → Conflicts → Reminders →
Laptop health; the read-only tail is unchanged; the INVITE anchor
stays narrow (no device asks swallowed); per-section degradation
naming unchanged.

**Wiring**: app.py passes the live `reminders` service into
MorningBriefFlow (optional arg — existing constructions work).

**Tests**: `tests/test_p6_morning_brief.py` extended to 13:
conflict detection rides the real collection (no second
implementation); none-today wording; reminders due-today listed
with a later row excluded; empty vs unwired distinction; failed
read named while the rest of the brief still assembles.

**LIVE** (real core, lab.ics): "morning brief" →
"Morning brief for Saturday, Oct 24: Calendar: Nothing on the
calendar today. Weather: Where? Name a city … Conflicts: none
today. Reminders: none pending today. Laptop: disk 79.0 GB free of
199.1 GB (60.3% used); RAM 36.2% used; up 0.11 days. (read-only
brief; nothing was executed)" — all five sections, two of them new,
each either read or named.

## 4bg. Feature-hunt ㉖: the phone-side app-open consent surface
(RESEARCH R47, 2026-10-24)

Released from item 4's DESIGN-HELD hold. The Phase-1 allowlist
(`com.android.settings`, chrome, gmail, calendar, lab app, Zara
itself) refuses any other package BYTE-EQUAL on the device — a real
phone-local security guard. The honest allowlist expansion is NOT a
relaxed runner: it adds a phone-side consent surface while keeping
the refusal as the default.

**Device contract** (`android/lib/app_open_consent.dart` — new):
per-app OPEN consents stored per package (validated name `[A-Za-z0-9_.]`,
≤100 chars, nothing else ever stored) in SecureStore under one slot
(`zara_appopen_consents`, Keystore-backed). Corrupt/foreign payload
decodes to NO grants (default refusal wins). The store only holds
state — it never launches anything; Core has no API into it; revoke is
device-local for good.

**Runner ladder** (`system.app.open` in `android/lib/job_runner.dart`):
allowlisted → open (unchanged, `consented:false`); out-of-allowlist →
stored grant? open with `consented:true`; else a real phone-side
dialog (main.dart wires the `appConsent` gateway via `navigatorKey`)
— Allow persists the grant and opens (`consented:true`), Deny
refuses with the package named, dismissal/unshowable dialog refuses
neutrally ("not granted"), missing store or missing surface fall to
the byte-equal refusal, and a grant that fails to persist refuses
the open ("Allow must never open anything that cannot be
remembered"). Core's own CONFIRM gate still applies per run — the
device consent is the second decision in two-party consent.

**Device UI**: Settings tab ("This device") gains a consent card —
the grant list with per-app Revoke, and the honest empty line when
there are none. (First placement on the Chat home was rejected by
the navigation test — correct: the home must answer pairing+status
first; device-local policy belongs on the device card.)

**Tests** (`android/test/p6_app_open_consent_test.dart`, 10):
allowlisted unchanged; byte-equal refusal with no surface; refusal
with surface-but-no-store (consent without memory refused to invent);
Deny names the package and never touches the gateway; dismissal
refuses neutrally; Allow opens with consented:true and the grant
PERSISTS (a second run needs no dialog — asserted with a deny-only
surface); invalid shapes can never be granted nor opened; revoke
undoes on the phone (next run refuses as declined); store validation
battery. `flutter analyze` — no issues in new/changed files (11
pre-existing infos in an untouched test). **Full Dart suite: 507
passed.**

**Live validation DEFERRED**: emulator-5554 not running. The
remaining live checklist when it returns: (a) an out-of-allowlist
open through chat → Core approval → dialog appears, Deny → honest
refusal; (b) Allow → app opens, grant visible in Settings card, and
a re-run skips the dialog; (c) Revoke from the card → re-refused;
(d) kill/relaunch app → grant survives (SecureStore persistence).

---

## 4bh. Feature-hunt ㉗: named recurring routines + the weekly review

**Ask**: "every Monday 8am do X" as a NAMED plan (not an unlabeled
reminder blob), and a weekly review that stitches the week together.

**Routines** (`core/routines.py`): built ON the existing scheduler +
notifications + audit (never a second engine), exactly one live
scheduler job per routine, next occurrence re-armed at fire time,
restart recovery announces a missed run ONCE (audited
missed_run/restart_receipt) and re-anchors forward — never bursts.
A routine carries a stable NAME (≤40 chars, duplicate names refuse),
a plan text (verbatim, opaque data — never sent to the resolver,
policy, or executor), and a cadence parsed from words: "every day",
"every weekday", "every weekend", or weekday lists ("monday,
wednesday and friday", abbreviations mon/weds/fri included). A
creation ask with NO explicit wall time is clarified, never guessed;
clock tokens reuse ReminderService's tested grammar (`_clock_in_text`
+ PERIOD_DEFAULTS — no second time implementation). Firing = a
review PROMPT notification ("Routine: <name> — <plan>"), never a
run: zero execution authority is added. Surface ops: create/list/
cancel/find-next; `wants_turn` guards exclude reminder-shaped asks
("remind … routine" routes to ReminderService) and the no-every-spec
creation ask is taught with an example instead of silently created.
Calendar/meeting nouns in a creation-shaped ask are excluded so the
calendar flow keeps them.

**Weekly review** (`core/weekly_review.py`): one deterministic
READ-ONLY turn on "weekly review / review my week / how was my
week". Section order — calendar (delegates to CalendarFlow's tested
"this week" digest), conflicts (conflicts_on_date over all 7 ISO
days), reminders (upcoming ≤ 7 days + next item), routines (list +
next fires), capture inbox (open count; wired in b29), chat logs
(wired in b28). Per-section NAMED degradation everywhere — "not
wired" until later batches plug stores in, failures named with the
service's honest line; a throwing section never aborts the whole
review. Nothing executes; audited.

**Tests** (`tests/test_p6_routines_review.py`, 14): cadence word
variants (day/weekdays/weekend/lists incl. abbreviations),
gate+exclusions, parse ops, no-time clarify, create/list/cancel
round-trip, weekday-set anchoring (Sun now → next fire Monday),
duplicate-name refusal, fire announces + re-arms through the REAL
fire path at the OCCURRENCE time, restart persistence, review
assembly with absent stores (not-wired lines), per-section conflict
count across the week, calendar-exception degradation,
routines-section real read. `start_core.sh` exports
ZARA_ROUTINES_DB (persistent).

---

## 4bi. Feature-hunt ㉘: chat ledgers (water/expenses/grocery/meals/

habits/medication)

**Ask**: the user types their daily logs in natural language; Zara
keeps them and can total them.

`core/ledgers.py`: ONE additive sqlite table (`ledger_entries`,
ZARA_LEDGERS_DB; memory fallback for tests): id, kind, status,
amount, unit, note, at, local_date, who. Units honesty pinned:
counted units are NEVER merged across kinds (3 glasses + 2 bottles
totals as "3 glass; 2 bottle" — glasses and ml stay distinct);
expenses are a bare sum + count with NO currency invented ("sum of
the amounts you gave; no currency assumed"); bounds refuse the
absurd (water ≤100 units, expenses ≤1e6, positive-only). Grocery is
a list ledger: add (duplicate-open refuses), check-off (fuzzy
single-match), remove, list with □ markers and a done tail.

Deterministic parse, no model: "add water 2 glasses" / "I drank 3
glasses of water" / "water 500 ml" / "add expense 250 for coffee" /
"spent 60 on lunch" / "add milk to my grocery list" / "check off
milk" / "log meal …" / "I ate …" / "log habit …" / "took
medication …" + reports ("how much water today", "expenses this
week", "what did I eat today", "habits today", "medications today",
"show my ledger"). Exclusion guard sends remind/routine/calendar
shaped asks to their own flows. `executed` stays False: ledger
writes touch the user's OWN store, nothing else (ReminderService
posture). The weekly review's chat-logs line calls `week_line()`
here — one totals computation, two surfaces.

**Tests** (`tests/test_p6_ledgers.py`, 12): gate+exclusions,
water shapes (unit-required clarify, singularized units), water log
+ per-unit totals, expense shapes/sums, grocery full flow (add dup
refusal, list markers, check-off, remove, done tail), out-of-range
refusals, meal/habit/medication logs + reports, journal summary,
persistence across restart (real sqlite), week_line neutral-empty +
real reads, unknown-shape fall-through.

---

## 4bj. Feature-hunt ㉙: capture routing (GTD bridge) + the READ-ONLY

mail wire

**Capture** (`core/capture.py`): "capture X" (also jots, brain-dumps
split on ';'/'and'/newlines — each thought is its OWN item, never a
blob) lands UNSORTED in one additive table (ZARA_CAPTURE_DB).
Bucketing is EXPLICIT, never guessed: "file N as next action /
project / someday / waiting" (1-based numbering against the same
ordering the inbox list showed), "file N as trash" drops with a
named message (not silent deletion). "show my inbox" lists numbered
unsorted items + teaches the filing verbs; "my next actions /
projects / someday / waiting" lists queues; "what's my next action"
= oldest filed next action. The weekly review calls
`capture.open_count()` — one computation, two surfaces.
`wants_turn` excludes remind-routine-calendar-grocery-nouns BUT the
exclusion does not apply to diary text AFTER the capture verb ("capture water
the plant" is a capture, not a water ask).

**Mail** (`core/mail_wire.py`): user-added but DORMANT by default.
stdlib imaplib + email only (no new dependencies);
ZARA_IMAP_HOST/PORT/USER/PASSWORD (app-password literature, never
logged or printed). Architecture guarantees, asserted by a seam
test: `select("INBOX", readonly=True)` and BODY.PEEK-only fetches —
the mailbox \Seen state is never touched and nothing in the account
can change; headers-only digest (buckets: newsletter / meeting /
finance / travel / security / other via rules-first regexes;
≤20 messages, ≤10 items shown). Unconfigured → an honest "wire is
off" with the exact env names and NOTHING queried; connection
failure → honest detail, mailbox unchanged. Never drafts: a
draft/compose/reply ask answers "drafting disabled in this build by
your own rule (never draft unless you first ask)" and nothing else.
Gate excludes "show my inbox" (capture owns it), remind/calendar
shaped asks.

**Tests** (`tests/test_p6_capture_mail.py`, 13): gate+exclusions,
parse shapes (file/trash/waiting-for), brain-dump splitting, file +
bucket counts, drop named-not-silent, next-action oldest-first,
out-of-range refusal, sqlite persistence, rules-first classify,
dormant wire, READ-ONLY end-to-end via a fake IMAP that ASSERTS
readonly select + PEEK specs (two unseen, buckets counted, password
never printed), draft-ask refusal teaching, mail gate exclusions.
`start_core.sh` exports ZARA_CAPTURE_DB.

---

## 4bk. Feature-hunt ㉚: the meeting scheduler (propose → pick → book

through CONFIRM)

**Ask**: "schedule a meeting" — Zara proposes times that actually
work, books only on an explicit pick, and the booking still passes
the existing approval gate.

`core/schedule_flow.py` + one CalendarFlow extraction: `free_windows
(target, min_minutes)` — the busy-interval collection and 08:00–22:00
gap math were EXTRACTED verbatim from `_answer_free_slots` (single
computation, two surfaces — R51.2); the free-slot chat tests stayed
green unchanged, and `schedule a meeting for/… tomorrow` parses the
asked day via CalendarFlow's own `_day_scope` (no second date
grammar), the duration via "for N minutes/hours" (15–120 clamped
with a named refusal), and the title via "meeting about/for X"
(day words and durations stripped — scope tokens, not a title).

Flow states: propose (≤5 windows + "+N more", honest no-window
answer, nothing submitted) → pick ("take slot 2", "book it at
14:30", "book it", cancel/never-mind) → freshness re-check (a
consumed/past slot refuses and re-asks, never books stale) →
`engine.submit("calendar.event.add", {summary, dtstart, dtend,
confirm:true}, device)` — CONFIRM never auto-approves; the pick
message says where to approve ("approve it on <device>") and that
the report follows. Out-of-range picks and off-window times refuse
with nothing submitted; a pending proposal is per-flow state (not a
stored approval — a restart clears it). Guarded anchors: remind/
grocery/water/expense/capture asks never land here.

**Tests** (`tests/test_p6_schedule_flow.py`, 11): propose lists and
asks (nothing submitted), cap='+N more' against the real
free_windows math, no-slots honesty, duration bounds, pick-slot-N
submits confirm:true to the resolving device, out-of-range pick
refused, book-at-time inside a window, off-window refused,
cancel clears, stale-slot honesty, gate exclusions. Fixture ICS is
passed via CalendarFlow's explicit `ics_paths` arg — a leaked
ZARA_ICS_PATH env would poison later calendar tests (caught when an
earlier ordering leaked it; the leak was the test-double's bug, not
the flow's).

## 4bl. Batch-31 LIVE round on the emulator + phone: dateline fix,
relay architecture, pairing ceremony, two-party modes 27–31
(LIVE, 2026-10-25)

Complete live pass on the Android-13 emulator + the Android app
(dev.zara.zara_android, Oct-23 debug build, 61 caps) + laptop-1,
after the networking/pairing blockers from earlier rounds were
resolved. Every claim below was executed against the running stack,
not inferred.

**Live defects found and fixed this round (5):**

1. **Misleading approval teaching text**: the parked-approval
   replies ("That needs your approval first … Say 'approve' in the
   approvals flow to proceed") taught a chat action that does not
   exist — bare "approve" is not a chat alias (it is even an
   injection-guard pattern), observed live when the pills-routine
   park prompted it and the honest chat answer was the generic
   clarify. Repaired to the truthful surface: approval is decided
   on the device's approval card (the live-tested path), core/
   intent.py (3 sites); no test asserted the old string; the
   approval/held message set re-ran green.
2. **A pick refusal consumed its own proposal**
   (`core/schedule_flow.py` `_book`): the pending propose was
   cleared BEFORE any pick check, so the border-time refusal ("not
   one of the proposed windows — book a time inside one") also
   consumed the list: the follow-up "book it at 08:30" it taught
   then fell to the generic parser (observed live exactly this
   way). Fix: the pending proposal survives every refusal and is
   consumed only when a pick is actionable (or cancel); the
   repaired loop verified live (refuse → re-pick → "Submitted
   'Meeting' for tomorrow 08:30–09:00 — approve it on laptop-1"),
   and `tests/test_p6_schedule_flow.py` updated + a new regression
   (refused pick → in-window pick → booked; later cancel submits
   nothing).
3. **Calendar propose dated slots to TODAY unconditionally**
   (`core/calendar_flow.py` `free_windows()`:
   `sdt = now.replace(hour=0, …)` — a "tomorrow" propose emitted
   today-dated slots, so a pick at 09:30 read as stale and refused its
   own proposal). Fix: `sdt = datetime.combine(target,
   datetime.min.time(), tzinfo=now.tzinfo)` — slots carry the asked
   day's date. Verified directly (tomorrow → 2026-10-26 08:00–22:00
   IST) + the p6 suite. Core restarted after the fix.
4. **Recipient download of sub-64 KiB relay transfers always failed**
   (`device/linux/agent.py` `xfer_download`): the first bytes read
   assumed 64 KiB, but Core refuses reads past the declared size
   ("read overruns transfer size"), so every small transfer died on
   its very first fetch (the txt payload; observed live with the
   1M/bin payloads succeeding while the 107-byte file failed). Fix:
   learn `size_bytes` from the recipient's pending list before the
   first read and clamp every read to it
   (`tests/test_agent_xfer_clamp.py` asserts exactly one read asking
   for the declared 107 bytes). The same release also stops deriving
   the staged filename from a device-GET that does not exist on the
   device surface (`/v1/agent/xfer/{tid}` is operator-only; it always
   404'd and the fetch silently wrote "relay-<id>" names) — the
   pending list is the truthful record (`device/linux/tools_linux.py`
   `storage_xfer_fetch`).
5. **Stale graceful disconnect poisoned presence permanently**
   (`core/devices.py` `heartbeat()`): during an agent restart the
   dying agent's `/v1/agent/disconnect` can land AFTER the successor's
   `register` — `status` stayed "offline" forever while
   `online=True` and last_seen stayed fresh, so every approval
   re-check failed closed ("device presence offline", observed live
   three times on `storage.xfer_fetch` approvals). Fix: a heartbeat
   confirming `online=True` restores `status="online"` and emits
   `device_online` (`tests/test_resource_telemetry.py` regression:
   mark_offline → heartbeat → presence not offline; already-online
   heartbeats emit nothing).

**Emulator networking root causes + relay architecture (documented
for reuse; the relays are session infrastructure, not repo code):**

- slirp-NAT connections to uvicorn's listener are dead at the host
  (TCP handshake accepted, payload lost); python http.server-shaped
  listeners work. Two stdlib relays (`/tmp/opencode/httpproxy.py`
  host:8080→8180, `httpproxy18082.py` host:18082→8180) front the real
  core (uvicorn on 127.0.0.1:8180). The relays decode Dart's chunked
  request bodies, re-emit with Content-Length, and force
  `Connection: close` per request (keep-alive corrupted sequential
  app requests: register 422 "body missing", claim 422 "Field
  required").
- The Android 13+ emulator default-DENIES background networking per
  app-uid (`dumpsys connectivity`: "Background firewall chain
  enabled: true") — fixed live:
  `adb shell cmd connectivity set-background-networking-enabled-for-uid
  10224 true` (re-apply after emulator reboots).
- The APK bakes `ZARA_CORE_URL=http://10.0.2.2:18082` (docs/
  ACCEPTANCE_REPORT §3c convention), hence the second relay on
  18082.

**Pairing ceremony (complete, first fully clean run):** enroll
(`POST /v1/agent/enroll {device_id:"android-phone",kind:"android"}`)
→ typed code into the app's pairing field (bounds [42,531][1038,678];
the Connect button's layout shifts with error-text size — re-dump
bounds before tapping) → "Connect “Hey Zara” device" IS the pair
submit (`_connect()` → claim 200) → `android-phone` registered+online
with 61 caps. laptop-1 re-registered after the core restart (the
agent only registers at boot; heartbeat-404 never self-heals).

**Scheduler path live end-to-end:** propose "can we schedule a
meeting tomorrow" → windows 08:00–22:00 (dated 2026-10-26, the
dateline fix visible live) → "book it at 09:30" → CONFIRM parked
(dtstart 20261026T040000Z = 09:30 IST) → approved → laptop agent wrote
`calendar.event.add` to the lab ICS: ok/committed/verified=true,
`uid=zara-c9e68b6b…@zara`, events_total=4, "appended one VEVENT before
END:VCALENDAR (re-read verified)". An earlier approval expired out of
its 300 s window during debugging ("approval expired after 1346 s"
— honest; the loop is re-run, not resurrected). One live teaching
moment: an approval fired while the laptop agent lacked
`ZARA_ICS_PATH` returned the honest refusal "no calendar configured
on the laptop — set ZARA_ICS_PATH once and ask again" (nothing
written); the agent was relaunched with the env and the booking then
committed.

**App-open modes (batch-26 checklist, honest findings):**

- In-allowlist open live: `POST /v1/exec {tool:"system.app.open",
  inputs:{package:"com.android.settings"}, device_id:"android-phone"}`
  → approval → `launched:true`, honest `verify_state:"focus_unconfirmed
  (accessibility provider unavailable)"` (no a11y service on this
  emulator); remembered focus externally verified via
  `dumpsys window` (com.android.settings focused).
- Out-of-allowlist refusal live: the same path with
  `com.google.android.youtube` parked, then failed at run time with
  `ValueError: field 'package' not in [allowlist]` — nothing executed,
  no job dispatched. Schema validation runs at run time, so the
  approval card can exist for a doomed operation (note: the card text
  leaks the raw `ValueError:` prefix — cosmetic, truthful).
- Chat parser: the local parser never fills `package` (by design),
  so every "open app"-shaped chat ask honestly clarifies "To do that
  I still need: package."; the hosted parser mapped one quoted form
  ("open app \"com.android.chrome\"") to `android.open_uri` in an
  earlier round but plain phrases currently fall through to local
  clarification (source:local in the same session).
- The phone-local dialog chain (`_AppOpenConsentPanel`, the
  appConsent "Allow opening this app?" dialog in `lib/main.dart`) is
  NOT renderable in the current build: the panel does not appear on
  the installed APK's Settings tab (verified by two full uiautomator
  dumps), and unreachable from chat because Core's `APP_OPEN_TARGETS`
  enum byte-equals the runner's mirror (the dialog is widget-test-
  covered but live-unreachable until the core enum ever diverges).
  Recorded as a design-level finding, not forced synthetically.
- `open_uri` consent path live (from the earlier round, still the
  goto consent surface): youtube URL approval parked (`exec-71f0b0a8a4c9`)
  → operator approve → phone executed with `fired:true,
  verdict:sent, verified:false` + handler-verified VIEW intent.

**Approval consent surface on the phone (design intent, live):**
device-bound CONFIRM executions (`android.alarm.set` for the pills
routine: hour=20) rendered Approval cards on the phone's Chat tab;
tapping Approve on the DEVICE executed the tool and Android's clock
app opened pre-filled ("8:00 PM evening pills reminder" visible in
DeskClock). A duplicate routine ask's approval was DENIED on the
device. Stale-card finding: cards for already-decided execs (one
succeeded, one core-refused) lingered across several poll cycles —
resolved-concise reconciliation is not immediate; noted.

**Phone talk/chat surface live:** the Streaming panel needed the
OPERATOR token (the paired device creds do not authorize /v1/talk;
the app's error text says so). With the token entered once on the
Tasks tab (Keystore-persisted per the 2026-10-22 decision), "add two
glasses of water" streamed a full turn (turn id + session + mission
reported in the panel). `input text` on this image splits at spaces
— `%s` encoding (e.g. `add%stwo%sglasses%sof%swater`) is required;
sends report "stream unavailable — used /v1/talk fallback" when the
SSE leg fails auth while /v1/talk succeeds.

**Batch-31 mobile probes (device_id=android-phone):** water
("add water 2 glasses" → "Logged 2 glass of water — 3 entries — 7
glass; 7 whole glasses so far", honest unit preservation), expense
("expense 45 for tea" → totalling 295.00, "no currency assumed"),
grocery (dedupe refusal "already on the grocery list", then "Added
'oranges' (3 open items)"), capture → "show my inbox" → "file 2 as
next action" → "Filed as next action: 'call the dentist tomorrow'",
routines ("show my routines" → 1 routine 'workout', next Mon Oct 26
07:00; "add routine pills…" parked android.alarm.set for device
approval as above). One transient: the first pills ask returned the
generic clarify fallback, the identical re-ask mapped correctly
(hosted parser availability is not deterministic — honest, logged).
Honest gap found here: a plan phrase containing "reminder"
("do the evening pills reminder") trips the routine flow's
reminder-exclusion guard (`\bremind`), so that create falls through
to the generic clarify live; the identical plan worded without the
r-word ("do the vitamin check") creates the routine cleanly
(workout/vitamins run both ways live; the vitamins probe artifact
was cancelled after the check). Left as-is: the guard's job is to
keep reminder-shaped asks out of the routine flow — widening it to
the word "reminder" is a design change, not a defect.

**Mobile→laptop transfer mission live (first full two-party run):**
files.transfer grant issued via `POST /v1/fabric/grants`
(`tmp-…`, confirm, 30-min TTL — grants are in-memory and do NOT
survive core restarts) → the phone app's Transfer diagnostic
("Send test files" with the pasted grant — left empty it shows the
refusal) relayed three payloads through Core: 107 B txt, 64 KiB bin,
1 MiB bin, all `succeeded` with the phone-computed SHA-256 (pure-Dart
SHA-256 in lib/transfer.dart, no packages) → the laptop staged all
three via `storage.xfer_fetch` approvals into
`~/.local/state/zara/xfer_stage/`, post-write digest verified, and
the relay records report `recipient_verified:true` for all three.
Without defects 2 (clamp) the txt fetch would never have completed.

**Infra state at close:** uvicorn core on 8180 (both relays
fronting), `android-phone` (61 caps) and `laptop-1` online, lab ICS
carrying the approved event, three staged transfer files on the
laptop, one alarm pre-filled in DeskClock (the live pills reminder
artifact), fresh 30-min grant on both-sides-verified records.


## 4bm. Batch-32: repo-sourced features (habit chains, budget, todo.txt
capture tokens) — design adopted, code stdlib-only, validated LIVE
(2026-10-26)

Per the user's directive the P5 hunt sourced this batch from proven
open-source repos instead of from-zero designs (R54 records the scan,
the adoptions, and the honest rejections). The standing constraint
stdlib-only / no new dependencies / interface-design-only never-copy
held throughout, so each adoption is a re-implementation of the
documented BEHAVIOR CONTRACT with the repo's own edge cases.

**Feature 1 — habit chains (from github.com/iSoron/uhabits,
Loop Habit Tracker):** `core/habits.py` (2 additive sqlite tables,
own WAL db `zara_habits.db` so restarts keep the chains). NL surface
(existing-flow shape): "add habit gym every day" / "…every monday and
thursday" (single weekday-list cadences; weekly-count refused with
the exact reason), "mark gym done" / "gym done" / "did gym",
"show my habits", "remove habit gym". Streak math mirrors uhabits:
marks→scheduled-day runs; best = longest ever; today-unmarked
NEVER breaks or shortens the chain; a chain is CLOSED only when a
scheduled occurrence that already passed went unmarked — the message
then says "last run N day(s), ended <date>", never "streak N" while
yesterday was missed. Duplicate same-day marks are idempotent ("the
chain does not grow twice"). Unknown habit names fall THROUGH to the
rest of the parser (no habit called X is a routing fact, not an
answer). The ledger's free-text habit diary is untouched (different
surface, guarded by its own wants-turn).

**Feature 2 — honest monthly budget (from Firefly III budgets):**
additive `ledger_budgets` table in the ledgers db; ONE monthly number
("set my monthly budget to 500" / "budget 480.50 a month"), unset by
default. Status answers with Firefly's four insights computed from
the SAME expense entries (one computation, two surfaces):
spent/mo, left (negative → "over budget by X"), days left in the
month, left/day; "no currency assumed" on every line, and unset
budget answers with the setter words (the mail-dormant precedent).
The weekly review's chat-logs line appends a budget tail ONLY when a
budget exists (unset → text unchanged → review tests stayed green
unchanged).

**Feature 3 — todo.txt capture tokens (from github.com/todotxt):
`core/capture.py` gains the grammar subset: leading `(A)`–`(Z)`
priority (spec Rule 1) + `due:YYYY-MM-DD` (key:value rule). The
captured text stays VERBATIM; parsed values go to a
`capture_meta` sidecar keyed by item id. When ANY unsorted item
carries metadata the inbox view sorts due-then-priority-then-arrival
and annotates items — and `file N` uses the SAME ordering (the
numbers the user saw are the numbers that file). An invalid
`due:2026-99-99` never becomes a date: capture stays verbatim, no
date is stored, and the reply names it. Without metadata, ordering
and messages are byte-identical to the shipped behavior (existing
tests untouched and green).

**Wiring (one restart, the "one go"):** habits service + flow in
`core/app.py` (after capture flow, before mail), flows tuple gains
"habits_flow" after capture_flow; `scripts/start_core.sh` exports
ZARA_HABITS_DB next to the other persisted DBs; weekly review gains
the habits section (named degradation like the others: "not wired" /
"none tracked yet" / real read).

**Tests (17 new + 6 amended):** `tests/test_p6_habits.py` (16: gate
and exclusions, parse shapes, daily chain grow/keep/break, gap closes
with "ended", today-unmarked never breaks, best-streak math,
weekday-habit adjacent-occurrence keep, weekday miss closes, weekly
cadence refused with reason, duplicate create refused, unknown-name
falls through, delete named, tmp_path persistence, invalid names);
budget tests appended to `tests/test_p6_ledgers.py` (7); capture
token tests appended to `tests/test_p6_capture_mail.py` (6 incl.
sidecar persistence); review section tests appended to
`tests/test_p6_routines_review.py` (3). One existing schedule-flow
test updated with a comment (R53.5 contract change from the same
day). Full fast set green before the restart; full suite after.

