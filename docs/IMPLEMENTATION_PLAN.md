# Implementation Plan (four stages — checkpoints, not products)

## Stage 1 — Core assistant (DONE)

Backend + contracts + tests.

- [x] Docs: architecture, contracts, security, battery, this plan
- [x] `core/`: models, db, tools, policy, execution, events, missions,
      devices, memory, context, providers, scheduler, notifications,
      audit, governor, app
- [x] `tests/`: unit + failure + permission + timeout + recovery tests
- [x] SQLite works; Postgres DDL documented for Oracle
- Gate: `pytest` green; API boots; every contract has tests.

## Stage 3 — Intelligence + natural interaction (DONE)

- [x] Provider layer: echo/scripted/openai-compatible, env-configured,
      streaming, retries, error classification, cancellation
- [x] Structured LLM contract + strict validation (malformed discarded)
- [x] NL tool loop with budgets, repeat guard, truthful failures
- [x] Mission-integrated turns + approval resume
- [x] Memory write policy (AUTO/CANDIDATE/SESSION/NEVER) + secret refusal
- [x] SQLite-persistent memory (Postgres+pgvector path documented)
- [x] Supersede-with-provenance corrections + expiry
- [x] Prompt-injection defenses (untrusted wrapping, policy supremacy)
- [x] Secret isolation (store refusal + prompt/trace redaction)
- [x] Sessions (bounded, compacted, expiring) + tracing (redacted)
- [x] STT/TTS abstractions, voice state machine, barge-in, wake "Hey Zara"
      with battery gating (mock engines; hardware validation pending)
- [x] Android voice boundary (`getVoiceSupport`, real flags only)
- [x] Router local fallback for core-scoped tools (no fake device needed)
- [x] 30 new tests; all 32 Stage 1+2 tests still pass
- Gate (this checkpoint): live E2E TEST 1–7 verified (see commit notes).

## Stage 2 — Device bodies (DONE)

- [x] Linux daemon (`device/linux/`): enroll/claim/register, heartbeat,
      job execution (safe allowlisted tools), offline queue, reconnect
      backoff, graceful shutdown
- [x] Device auth (`core/device_auth.py`): per-device keys, pairing codes,
      expiry, revocation
- [x] Capability discovery: agents advertise only implemented capabilities;
      core routes from registered capabilities
- [x] Presence: registered|online|offline|degraded|reconnecting + staleness
- [x] Protocol v2.0 (`core/protocol.py`) + REST endpoints + `/v1/dispatch`
- [x] Router: capability -> online -> policy -> governor -> device
- [x] Battery-aware routing + offline behavior (tested)
- [x] Flutter Android shell + Kotlin `zara/device` bridge; capability table
      with `voice.wake_word` ("Hey Zara") reserved, debug APK builds
- [x] 17 new tests; all 14 Stage 1 tests still pass
- Gate (this checkpoint): full E2E core->agent->result->audit verified.

## Stage 3 — Intelligence + natural interaction (DONE — see checklist above)

Stage 4 is next: E2E + cross-device continuity, failure/permission/battery/
offline matrices, security review, concurrency + resource limits,
long-mission soak, Oracle Always-Free deploy, backup/recovery, full suite.

## Stage 4 — Integration + hardening

E2E + cross-device continuity, failure/permission/battery/offline matrices,
security review, concurrency + resource limits, long-mission soak, Oracle
Always-Free deploy, backup/recovery, full test suite, docs.

## Reuse note

Checked prior work: `projects/chatterbox-friday` (voice streaming
experiments) and `projects/rag` (scratch RAG) are experiments, not a core —
nothing to merge yet. Voice work will plug into the Stage 3 voice interface;
nothing deleted.

The following is the consolidated building list (added 2026-10-20;
it replaces the fragments this session had scattered into the same
section — no recorded decision was dropped in the consolidation).

## Next building list (consolidated 2026-10-20 — post-Stage-29 backlog)

All candidates stay in the repo's standing discipline: Core sole
authority, no second loop, no shell, no new dependencies without an
explicit decision, fixture tests first, device evidence only from
devices that actually exist, everything consent-gated.

---

### P0 — DONE ✅ (2026-10-20) — emulator-validated; see ACCEPTANCE_REPORT §4v

The 12 DeviceBridge.kt compile errors were reconciled (public-API
replacements, no work discarded), the APK builds, and the FULL
Core↔device loop was validated on the Pixel_8 emulator (pair →
register → challenge → job submit → native execution → verified
result). P3 items below now stand unblocked at the EMULATOR-LAB
level; claims needing real-hardware-only behavior (torch flash
loudspeaker acoustics, etc.) still wait for a real phone.

### P1 — Stage 30 DONE ✅ (2026-10-20): read-only actions stop prompting
### (laptop-side, buildable today)

dummy's approval-bucket argument (Round 14): prompting for harmless
reads trains rubber-stamping, which destroys the value of approval
for dangerous actions. Plan:

- Auto-allow the READ-ONLY observation family: `gui.screen.inspect`,
  `gui.find`, `gui.wait`, `android.notifications.read` (plus the
  already-SAFE laptop read tools — no change needed).
- KEEP approval exactly where it is today: `gui.tap/set_text/scroll/
  back`, `storage.delete`, `filesystem.delete`,
  `android.app.force_stop`, `messaging.send` (never-auto class),
  clipboard write, xfer/transfer, screen-cast consent.
- The relaxation is POLICY-LEVEL (risk label / policy allowlist),
  never per-flow special cases.
- Tests: policy matrix (read-only allows, mutating still parks),
  mislabeled-read regresses → still parks, evidence unchanged (the
  allow is NOT a verification claim).

### P2 — DONE ✅ (2026-10-20) — laptop-side convenience; see ACCEPTANCE_REPORT §4w

- **P2a. Laptop volume / mute / brightness** — SHIPPED: `audio.
  volume.set` (level/delta), `audio.mute.set`, `display.brightness` /
  `display.brightness.set` via wpctl + sysfs backlight; every SET
  re-read-verified on-device; confirm=true structural gate.
- **P2b. Laptop playback control via MPRIS** — SHIPPED: `media.
  now_playing` (SAFE) + `media.playback` (play/pause/play_pause/stop
  verified by PlaybackStatus re-read; next/previous honestly declared
  "sent"). Live-validated honest-empty on this laptop (no player
  playing = plain answer).

### P3 — phone daily-use set — all gated behind P0 📱

Each = one bounded Android surface + a Core flow; stock intents/APIs;
no new dependencies.

- **P3a. Media control** — ✅ SHIPPED + live-validated (2026-10-22,
  ACCEPTANCE §4ab): (1) global media-key dispatch
  (`android.media.playback`, honest "sent" verdict) shipped in
  phase A; (2) phase B: `android.media.control` — ONE named app's
  playback session through MediaSessionManager (hint REQUIRED, no
  global blast; 0/many matches are honest answers with a bounded
  listing). Verdicts carry an OBSERVED playback-state delta via a
  bounded callback re-read (8 s; never a stale snapshot): verified
  only when the observed change matches the action, else reported
  unconfirmed. Threading facts pinned in code: observe via a worker
  handler, never block the main looper while observing (the session
  shares it), the registration echo is not the change. A labeled
  self-test session (process-owned) makes the full path exercisable
  end-to-end: playing→paused and paused→playing observed live with
  verified:true; third-party matching live on Youtube's session. Timeouts 90 s after the T0 invariant test failed the initial 60 s.
- **P3b. Clipboard bridge laptop→phone** — ✅ SHIPPED (2026-10-22,
  ACCEPTANCE §4ad): phone-side clipboard WRITE surface
  (`android.clipboard.set`, re-read compare, length-only result,
  focus dependency surfaced honestly) shipped in phase A; phase B
  closed the plan's TODO with ClipboardFlow in core — "copy <text>
  to my phone" parses at the invite mouth (full text only in the
  approval card + the confirm; results/audit rows carry LENGTH
  only), payloads bounded 4096 (refused, never truncated), free
  text on a confirm-phase drain never becomes content, cancel
  keeps the clipboard untouched. The reverse direction (phone→
  laptop READ) stays OS-restricted, honestly limited.
- **P3c. Photo backup mission** — ✅ SHIPPED + live-validated
  (2026-10-22, ACCEPTANCE §4ac): COUNT-ONLY inventory gate
  (`android.photos.inventory`, CONFIRM parks every run — per-run
  consent is structural) + PhotoFlow in core (delete flavors
  refused at the mouth; ring-of-one pending; counts + the honest
  operator pull plan in the report; no names/paths/content/
  thumbnails ever leave the device; READ_MEDIA_IMAGES only, no
  ACCESS_MEDIA_LOCATION). The PULL itself is the operator's adb
  seam, consented per run — a phone→laptop READ inside a third
  party app stays Android-restricted and the design says so. LIVE:
  seeded JPEG → parked card → real scan (count 1, 161 B, folder
  only) → conversational report with the pull plan; the empty
  library answered "nothing to pull" BEFORE the seed. DELETE NEVER
  (live-refused: "backup and delete after").
- **P3d. Call X (contact resolution)** — ✅ SHIPPED + live-validated
  (2026-10-22, ACCEPTANCE §4y): all six sub-items as written —
  bounded `android.contacts.lookup` (name+number+starred only, zero
  or multiple matches are questions never guesses), the alias-ask →
  confirmed memory fact (`contact_alias`) → future calls resolve
  through memory, the correctness confirm ("Calling <name> —
  correct?") → YES → direct ACTION_CALL with the per-call approval
  still required, the a11y call-screen verdict (250 ms × 4 s
  focused-window re-read → confirmed/absent; a fired intent is
  NEVER proof), and raw numbers from untrusted text refused at every
  layer. LIVE E2E on the emulator gate: real ContactsContract rows →
  confirm → ACTION_CALL → verified call screen (Dialer incall
  package captured + screenshot). Note: the a11y service must be
  ENABLED for the poll to see anything — a rootless service honestly
  reports absent (first live run PROVED the honest-absent path
  end-to-end; Round 20 #2).
- **P3e. SMS send** — ✅ SHIPPED + live-validated (2026-10-22,
  ACCEPTANCE §4z): native carrier SMS through SmsManager on the same
  contact-resolution spine as the call (bounded lookup, confirmed
  alias memory, tail masking). Per-send explicit approval with the
  FULL DRAFT echoed in the card and the confirm; SEND_SMS granted
  once at operator enablement, never silently; dial-grammar re-check
  at the bridge (a bypassed Core cannot text untrusted numbers);
  400-char cap refused, never truncated; job results and audit rows
  carry length + masked tail only (live audit.db verified draft-free
  and digit-free); the honest verdict is "sent" = radio accepted,
  verified:false — carrier delivery is NOT confirmable by a bounded
  re-read and is never claimed (live: the SMS materialized in
  content://sms with creator=dev.zara.zara_android). Drain fix found
  live: a still-RUNNING record keeps the flow pending ("still
  working") instead of closing.
- **P3f. OTP reading** — ✅ SHIPPED + live-validated (2026-10-22,
  ACCEPTANCE §4aa): `android.otp.read` (HIGH_RISK×CONFIRM — the
  policy floor parks the approval EVERY run, so ambient grants can
  never carry it) + deterministic OtpFlow (ring-of-one pending,
  10-min TTL). Shape-gated 4–8 digit codes inside OTP-context words,
  freshness window enforced at the device (<= 5 min default), sender
  hint bounded 40 chars, full bodies never leave the device
  (candidates = package/title/code/age only). The code surfaces
  ONLY in the asking turn; memory never receives it and the live
  audit.db verified ZERO rows contain the code. Executor picked
  phone-first (requester android online; honest none/ask paths).
  LIVE gate: operator-posted notification → parked approval →
  real scan → "Fresh code: 991227 (from shell, 77s old)" with
  clean audit; the honest-absent path was proven live BEFORE the
  final grant (ungranted listener → count 0 as data).
- **P3g. Call recording** — ✅ HONEST REFUSAL SHIPPED (2026-10-22,
  ACCEPTANCE §4ac): a deterministic refusal gate in the call flow
  (`_RECORD_RE`, before any invite parsing — otherwise "call
  recording" parses as a contact lookup, caught live by fixtures).
  The refusal names the two real reasons: the OS privileged role the
  build refuses to seek, and two-party consent law. NO covert
  capture surface exists anywhere (no mic bridges, no a11y call
  capture). Never claims recording; offers notes during/after.
- **P3h. Quick actions** — ✅ phase A shipped + live-validated
  (2026-10-22, §4x): torch (unverified-honest on emulator), volume
  (re-read verified), ring (DND rule kept), alarm (handed-to-clock,
  `<queries>` visibility fix), open_uri (scheme-bounded, Chrome live),
  clipboard, power saver, media keys + intent routes.
- **P3i. Find-my-phone ring** — ✅ (`android.ring`, MediaPlayer
  bounded playback; DND refusal without explicit per-run bypass).
- **P3j. Open link / file on my phone** — ✅ (`android.open_uri`,
  VIEW + handler check + scheme bound; "sent" never claimed verified;
  `<queries>` VIEW declared). Per-domain allowlist refinement TODO.
- **P3k. Battery saver toggle** — ✅ (`android.power_saver.set`,
  WRITE_SECURE_SETTINGS granted ONCE via adb (SYSCONTROL_MATRIX T1
  pattern), PowerManager+Settings dual re-read per change).

### P4 — laptop-side or cross-device (USER DECISIONS 2026-10-22)

## Conversation brain (2026-10-22 user report: echo placeholder as a "reply")

- **DETERMINISTIC FASTPATH in the talk loop** ✅ DONE: read-only
  flows (calendar/weather) answer conversationally BEFORE any
  model call; everything else falls through. Live-proofed on the
  phone UI. Fixtures test_talk_fastpath.py (3). ACCEPTANCE §4ah.
- **NVIDIA key wiring** ✅ DONE (2026-10-22, corrected after the
  operator pushed on the second key): the two keys map to two
  NVIDIA accounts; key2's account binds openai/gpt-oss-20b initially;
  contract-probe sweep then picked nvidia/nemotron-3-ultra-550b-a55b
  (7.8 s + valid JSON vs 106 s; blockers pinned in test_llm_salvage.py:
  1024-token budget + plan-glitch salvage). Launcher extracts key2 from
  ~/Documents/nvidia.txt at startup (never echoed). stream_reason
  sends reasoning_effort=low + 1024 tokens (reasoning models burn
  budget pre-content). LIVE: phone conversation renders real
  streamed replies ("Hi! How can I help you today?"); calendar/
  weather fastpath unchanged. ACCEPTANCE §4ah corrected,
  RESEARCH Round 30.

- **OPERATOR TOKEN one-time entry (user request 2026-10-22)** ✅ DONE:
  Keystore-persisted operator token (SLOT zara_operator_token),
  restored at app start, wiped with device credentials; live
  verified (token once → restart → pre-filled → talk/stream 200).
  ACCEPTANCE §4ag; Dart 490 passed.

- **DAILY-USE DURABILITY (close-out of 2026-10-22 session)** ✅ DONE:
  runtime DBs durable in ~/.local/state/zara/ — core restart re-pair
  ELIMINATED (live proof: challenge 200s immediately, /v1/devices
  intact, zero interaction). Device side: the revoked->error throw
  that silently killed _tick fixed (idempotent handle) + pinned by
  test; Dart 487 passed; APK rebuilt/installed. ACCEPTANCE §4af.

- **Calendar read** — USER PICKED: local .ics files (zero network
  dependencies, offline-first, honest stale-file reporting). ✅
  SHIPPED + **LIVE 2026-10-22**: bounded stdlib parser +
  deterministic flow (ACCEPTANCE §4ae, RESEARCH Round 26); live
  .ics read through the real core API both lab events reported with
  the honest RRULE note. "what is ..." invite form fixed + pinned.
  CalDAV was the alternative; not picked this round — revisit only
  if a server shows up.
- **Home Assistant integration** — USER DROPPED (2026-10-22): no HA
  at home. Recorded as a decision, same class as local-voice.
- **Weather answers** — USER PICKED: free open-data endpoint
  (Open-Meteo, no account). ✅ SHIPPED + **LIVE 2026-10-22**:
  stdlib urllib + deterministic flow with source/time labels
  (ACCEPTANCE §4ae); live Open-Meteo answer through the real core
  API (Lisbon: temperature + wind + rain window + fetch-time label).
  "what is ..." invite form fixed + pinned.

### P5 — feature hunt, all-green loop (2026-10-22, user: "repeat until a
personal agent's daily features are achieved")

Continuing batches; every batch = tests/fixture → installer rebuild →
LIVE emulator check → ACCEPTANCE/RESEARCH/PLAN updates. Emulator-5554 is
the live gate; no real-device claims without the phone.

1. ✅ **Timer (`android.timer.set`)** — tool + Kotlin hand-off + Dart +
   phrase seeds; then the LIVE chat probe exposed the systemic parked-
   confirm gap and closed it (grant-time receipt injection, timer slot
   extraction, grounding stub fix). LIVE end-to-end on the emulator:
   "set a timer for 5 minutes to eggs" → approval → clock app opened
   pre-filled, "eggs" timer counting down (ACCEPTANCE §4ai, RESEARCH
   R31).
2. ✅ **DND state + toggle (`android.dnd.get/set`)** — read-only filter
   report + verified-re-read toggle; lab grant probed
   (`cmd notification allow_dnd`); LIVE: on → ZEN_MODE_NO_
   INTERRUPTIONS via chat approval, off verified again; void-return
   lesson recorded (ACCEPTANCE §4aj, RESEARCH R32).
3. ✅ **Weather forecast / rain-tomorrow** — daily block + verdict-first
   tomorrow answers (city+tomorrow parse, honest missing-daily,
   rate-gate fix); LIVE real-Open-Meteo answered the ASKED day
   (ACCEPTANCE §4ak, RESEARCH R33).
4. **App-open allowlist expansion** — `system.app.open` enum →
   android.apps.list discovery with per-app approval. DESIGN-HELD
   (2026-10-23): phone-side byte-equal refusal is a real phone-local
   security property; the honest expansion needs a phone-side consent
   surface (notification action or surface dialog), not a relaxed
   runner — designed at RESEARCH R35 before implementation.
5. ✅ **Calendar WRITE with approval** — laptop-side tool
   (`calendar.event.add`, agent executes via `device/linux/tools_linux`
   — parity pinned), quoted chat matcher (`_calendar_hit`: quote
   required, tomorrow/weekday/HH:MM/am-pm/duration/all-day resolved
   deterministically, nonsense → hosted), duplicate refusal, atomic
   tmp+rename write, re-parse verification. LIVE end-to-end on the
   REAL laptop agent (laptop-1 paired in place): "add \"Zara status
   demo today 23:30\" to my calendar" → approval → SUCCEEDED
   verified:true + the VEVENT landed in lab.ics + the read side
   answered "today 23:30: Zara status demo"; unquoted ask → honest
   quote-clarify; duplicate re-ask → "nothing added". Live-found
   routing gap FIXED + pinned: laptop descriptors advertise
   FEATURE-level ids (calendar.write) while chat routes TOOL names —
   resolver/fabric now translate (ACCEPTANCE §4am, RESEARCH R35).
   Laptop agent now launched by the core launcher (persistent daily
   use; pairing key persisted).
6. ✅ **Screen-off boot offline-cache fix** — `_tick` re-probe with the
   fresh-read-decides contract; LIVE no-restart resume verified
   (ACCEPTANCE §4al, RESEARCH R34).
7. ✅ **Recurring (RRULE) events expand on the calendar read side** —
   bounded supported subset (DAILY/WEEKLY+BYDAY/MONTHLY, INTERVAL,
   COUNT, UNTIL), per-event honest label for systems refused
   (YEARLY, BY* exotics), jump-started walk (old rules cost nothing;
   COUNT anchors at the base), DTEND-carrying occurrences. LIVE on
   the real lab.ics: DAILY;COUNT=3 → three named occurrences, no
   note; YEARLY → once + the "rule I don't expand" note; both test
   events surgically removed after the checks (ACCEPTANCE §4an,
   RESEARCH R36).
8. ✅ **Calendar REMOVE with per-event approval** — laptop-side tool
   (parity), UID or quoted-driver selection, ambiguity refuses with
   candidates, repeating parents say "whole series", atomic +
   selection-semantics re-parse verification, the ONE lawful
   destructive-word exception (quoted calendar remove), CRLF
   read-fix for both calendar tools. LIVE: cancel quoted → approval
   → SUCCEEDED verified, event gone; delete-verb parks; ghost
   → "no event matches — nothing removed" (ACCEPTANCE §4ao,
   RESEARCH R37).
9. ✅ **Day-scoped calendar answers** — "calendar tomorrow/friday"
   filters to the named LOCAL date, horizon stretches to the
   target, scoped labels carry stamp+title only (no UTC-date
   lies), day-honest empty wording; unscoped sweep unchanged.
   LIVE live-checked on the real lab.ics incl. the IST 23:30Z
   cross-day case (ACCEPTANCE §4ap, RESEARCH R38).
10. ✅ **"when is my X?" title lookup** — earliest future match by
    title substring with local-tz stamps, "+N later" honesty,
    RRULE occurrences counted, narrow routing (quoted / noun-tail
    / "next <X>"; day words stay with the sweep), honest absence.
    LIVE: status-demo meeting → correct 05:00 IST stamp;
    recovery demo (next-shape) → correct; ghost ritual → honest
    absence (ACCEPTANCE §4aq, RESEARCH R38 #6).
11. ✅ **Past-day calendar history** — "yesterday"/"last <weekday>"
    scope backward with OVERLAP semantics (DTEND honored, all-day
    covers the day, RRULE walker's anchor moves into the past
    window), past-tense routing; honest day-scoped empty wording.
    LIVE: yesterday → the Oct 22 lab event; today → honest empty
    (ACCEPTANCE §4ar, RESEARCH R38 #7).
12. ✅ **Free-time reporting** — "when am I free <day>?" gap report
    (merged busy intervals, DTEND authoritative, 08:00–22:00
    local, ≥15-minute gaps, all-day blocks the day, no-end events
    counted as 2 hours AND SAID), "am I free at <time>?" point
    check naming the blocker, honest single-day-only limit for
    heavier planning. LIVE: gap report + point check + limit
    reply on the real lab.ics (ACCEPTANCE §4as, RESEARCH R38 #8).
13. ✅ **Laptop health reading** — system.health (laptop PUBLIC/
    SAFE): disk/RAM/load/uptime facts with a deterministic
    formatter so the LLM-free path answers too; honest laptop
    aliases. LIVE: full real reading via chat (both alias shapes)
    and /v1/exec (ACCEPTANCE §4at, RESEARCH R38 #9).
14. ✅ **Weekly calendar digest** — "this week"/"last week": ISO
    Monday-anchored window, per-local-day grouping with counts,
    +N-more collapse, all-day labels, RRULE occurrences anchored
    at the window, named empty. LIVE: this-week digest groups the
    two Oct-24 events on local Saturday (ACCEPTANCE §4au,
    RESEARCH R38 #10).
15. ✅ **Morning brief** — `core/morning_brief.py` composite READ:
    today's calendar (CalendarFlow's own today-scope turn), weather
    (WeatherFlow verbatim), laptop health (system_health numbers) in
    one deterministic answer with per-section named degradation,
    LOCAL-day header, end-anchored routing guard (action asks never
    swallowed), single-period joining. LIVE: good morning +
    good-morning-zara + what's-my-day-like on the real core; guard
    probe routed "good morning, add scotch…" to the calendar write
    path (ACCEPTANCE §4av, RESEARCH R39).
16. ✅ **Meeting move/reschedule** — `calendar.event.move`
    (laptop-side, CONFIRM, never-auto): uid-or-quoted-title
    selection shared with removal, all-day/timed kinds never mix,
    duration preserved (DTEND delta) unless `new_end` pins, repeat
    parent moves the series and says so, destination collision
    refuses, atomic + re-parse verified; chat shape push/postpone/
    reschedule/shift + quote + my calendar (selector reads the quote
    ONLY; destination from the tail: day+clock/day-only/clock-only).
    tries). Plus the TZ repair: the chat WRITE frame now stores true
    UTC (R40.1 — third live catch; `_cal_shape` one frame for add/
    remove/move). Dispatch repairs: JOB_POLL_SLICE_S NameError
    (parked polls 500'd → agent storm) fixed; zombie long-poll
    claims now re-parked via owner receipt (claim_ack) with bounded
    redelivery (3) — expired stays terminal (R40.2-3). LIVE: add →
    chat move approved → succeeded/verified ~12 s, honest label
    "15:00 (+0530)" for a "3pm" ask, demos cleaned via quoted
    removals (ACCEPTANCE §4aw, RESEARCH R40).
17. ✅ **Disk breakdown** — `storage.scan` (PUBLIC/SAFE read, in-turn):
    dust/gdu/dua interface ideas on stdlib (`core/storage_scan.
    scan_dir_usage`), home-disk top-N by subtree apparent size,
    symlinks never followed, bounded walk with floor-honest partials,
    sensitive-path refusal via the house scope; matcher keeps the
    health asks on system.health while routing "what's using my disk"
    structurally (BEFORE the "disk usage" alias); deterministic
    formatter. LIVE: real chat answer with real ranked home numbers —
    20.7 GB .android, 5.6 GB .cache, no fabricated values (ACCEPTANCE
    §4ax, RESEARCH R41).
18. ✅ **Unit + timezone converters** — `convert.units` + `time.zone`
    (core-local, PUBLIC/SAFE); NEW named contract `LOCAL_EXEC_CAPS`
    (registry-backed executors through the /v1/exec no-device path —
    distinct from answer-only BUILTIN_LOCAL; live-caught R41.5).
    Same-kind-only conversion (exact fixtures incl. temperature
    round-trip, decimal/binary data families kept distinct, plural
    forms), world clock (city alias map + case-insensitive IANA,
    next-occurrence pinned host clock). LIVE: all three shapes
    executed in-turn with correct answers (ACCEPTANCE §4ay,
    RESEARCH R41.5–7).
19. ✅ **Process snapshot** — `system.top` (laptop-side read
    like system.health): top -b/btop interface ideas on stdlib
    /proc (windowed CPU delta ("cpu lazy"), RSS, argv-minimized
    names — no full command lines by design), matcher keeps health
    and disk asks on their own surfaces. The timing invariant
    caught system.top's 10 s AND batch-17 storage.scan's hidden
    60 s (loop order) — both raised into the 90 s band (ACCEPTANCE
    §4az, RESEARCH R42).
20. ✅ **Internet diagnostic** — `system.connectivity` (laptop-side
    read): three bounded layers beyond NIC-up (keyless DNS, raw
    TCP relays, end-to-end HTTPS 200x with two candidates),
    architecturally bounded probes (daemon-thread joins — dead
    resolver cannot hang), descriptive verdicts with measured
    latencies, matcher kept interfaces/wifi-settings asks on their
    own surfaces. LIVE: honest "limited_web" description of this
    lab's TLS-blocked egress (ACCEPTANCE §4ba, RESEARCH R42.5–7).
21. ✅ **File lens** — `storage.find` ("which files are over 1 gb"
    / "biggest files"): file-level view on the same floor-honest
    walk machinery (iterative stack), decimal display family
    unified across storage surfaces (R43.1), delete-shape refused
    at the matcher, folder asks stay on storage.scan. LIVE: real
    paths ranked with partial-budget disclosure (ACCEPTANCE §4bb,
    RESEARCH R43).
22. ✅ **Checksum utility** — `fs.checksum` (laptop-side read):
    chunked sha256/sha512/sha1/md5-over-one-file with an honest
    512 MB PREFIX cap ("NOT a compare-safe full digest"), digest-only
    output, _fs_path scope, RAW-text path capture preserving case
    and spaces, valueless-path clarify with the exact example.
    BONUS repair (live-caught): the failed stage now surfaces the
    tool's REAL ValueError (reason → error → stage) for every device
    tool (ACCEPTANCE §4bc, RESEARCH R43.4).
23. ✅ **Growth lens** — `storage.growth` ("what grew on my disk"):
    baseline-diff over the folder machinery — LEVEL-1 stable unit
    (nested top-N churned membership and hid real changes — R44.1),
    entry cap sized against the measured home walk (1.02M entries,
    13 s — R44.2), shape-marked ledger with same-day keyed replace
    and an 8-day bound, honest first-run/shape-migration
    re-baselines, and the equal-vs-gone state chain fix (R44.4).
    LIVE: baseline save, honest re-baseline, and a real
    "+3.1 MB .cache (grown)" diff through chat (ACCEPTANCE §4bd,
    RESEARCH R44).
24. ✅ **Compound calendar asks + the conflict lens** — "today and
    tomorrow" / "friday and saturday" list whole-day sections
    (≤4 days, and/or-chained only, prose-separated days refused);
    the conflict check ("any conflicts tomorrow?", "am I
    double-booked?") reports real pairwise overlaps (back-to-back
    never, no-DTEND 2 h noted, all-day excluded+named, pairs ≤5
    with a count, sweep window starts NOW). Answer order policy:
    title → free → conflicts → week → compound → sweep. 17 tests +
    full green; LIVE on the real core (ACCEPTANCE §4be, R45).
25. ✅ **Morning brief reads five facts** — +Conflicts +Reminders
    sections; conflicts reuse `CalendarFlow.conflicts_on_date`
    (the tested `_collect_pairs`, no second implementation);
    reminders ride `ReminderService.upcoming` filtered by LOCAL
    end-of-day ("pending today"), unwired ≠ empty, failures named.
    app.py wiring updated. 13 tests; LIVE five-section brief
    (ACCEPTANCE §4bf, R46).
26. ✅ **Phone-side app-open consent surface** (released from item 4's
    DESIGN-HELD hold) — out-of-allowlist `system.app.open` runs only
    through the phone's OWN consent surface: an explicit dialog tap
    persists a validated per-package grant in SecureStore (Keystore),
    revocable on the device Settings card; Core has no API into the
    store and its CONFIRM gate still applies per run; byte-equal
    refusal stays the default for deny/dismiss/no-surface/no-store.
    10 Dart tests + full suite 507 green, analyze clean; LIVE
    validation deferred (emulator absent) with an explicit 4-step
    checklist recorded at ACCEPTANCE §4bg (RESEARCH R47).
27. ✅ **Named recurring routines + weekly review** — `RoutineService`
    on the SAME scheduler/notifications/audit as reminders (one live
    job each, re-arm at fire, missed-run announced once), stable
    names + weekday-set cadences parsed from words, no-time clarify,
    reminder-shaped asks excluded; `WeeklyReviewFlow` = read-only
    five-section composite delegating to the TESTED reads (calendar
    week digest, conflicts, reminders, routines; capture/ledgers
    slotted in by 28/29). 14 tests; R48.
28. ✅ **Chat ledgers** — one additive sqlite table for
    water/expense/grocery/meal/habit/medication; deterministic
    parse; counted units never merged across kinds; expenses = bare
    sum + count, no currency invented; grocery list with
    check-off/remove; in-memory fallback is a first-class store;
    review's chat-logs line = `LedgerService.week_line()`. 12 tests;
    R49.
29. ✅ **Capture routing (GTD bridge) + READ-ONLY mail wire** —
    capture (brain-dumps split) → unsorted → explicit `file N as
    next-action/project/someday/waiting` (+ trash named, never
    silent); review reads `open_count()`. Mail: stdlib imaplib,
    readonly select + BODY.PEEK (asserted by the seam test), rules-
    first digest, dormant until ZARA_IMAP_* is set, headers-only,
    never drafts (user rule: never draft unprompted — and this build
    has no drafting surface even when asked). 13 tests; R52.
30. ✅ **Meeting scheduler** — CalendarFlow free-window math
    extracted verbatim as `free_windows` (free-slot chat tests green
    unchanged); propose ≤5 windows +N more → explicit pick → engine
    submit `calendar.event.add` confirm:true through the resolving
    device; CONFIRM never auto-approves; stale slot refuses; per-
    flow pending (restart clears). 3 structurally-unpassable batch-24
    calendar fixtures found and repaired against shipped semantics
    (R50). 11 tests; R51.
31. ✅ **Full validation round (LIVE)** — laptop probes for 27–30
    (routines, ledgers, capture/inbox, mail-dormant refusal, weekly
    review); batch-26 app-consent checklist on the emulator
    in-allowlist open (Settings launched, focus verified externally)
    + out-of-allowlist core-side refusal live ("field 'package' not
    in [allowlist]", nothing executed; dialog chain live-unreachable
    because the Core enum byte-equals the runner mirror — recorded
    as a design finding); pair/enroll ceremony completed for
    android-phone (61 caps) with the relay + background-uid fixes
    (§4bl); mobile probes via the phone (water/expense/grocery/
    capture/file/routine + device-approval taps on the phone);
    mobile→laptop transfer mission first full two-party run
    (3 payloads relayed, staged + sha-verified, recipient_verified
    true). Five live defects found and fixed during the round
    (approval teaching text; schedule-flow pick-refusal consuming
    the proposal; calendar dateline; sub-64 KiB relay fetch clamp;
    stale disconnect poisoning presence), each with a regression
    test. See ACCEPTANCE §4bl, RESEARCH R53 for details, dates, and
    honest gaps (stale approval cards linger; hosted parser
    availability nondeterministic; a "reminder"-word plan trips the
    routine exclusion guard).
32. Repeat the hunt (repo search → batch selection until the user
    stops) — P5 is explicitly open-ended by user instruction.

### Parked / dropped (decisions recorded)

- **Stage 31 (reach a once-paired, invisible device)** — DEFERRED
  until a second real device exists (Round 14 survey: ADB revive /
  WOL relay / self-hosted push / P2P mesh). Cheapest revisit item:
  one-action ADB revive.
- **Local brain + local voice (Ollama/Piper)** — DROPPED by user
  decision (phone-side complexity not worth it); hosted/cloud voice
  path stays.
- **Reference-only, no adoption:** scrcpy/sndcpy, KDE Connect
  (feature checklist only — Zara covers most of it), OpenDroid
  runtime, ARTEMIS, MYRA (closed-source binary — reject), dummy
  (mined: Stage 30 idea only).
