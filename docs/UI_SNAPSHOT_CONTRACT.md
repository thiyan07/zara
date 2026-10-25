# UI Snapshot/Action Contract — v2

**Status:** implemented (Core + Android runner + native service) and
**validated live on the ZaraLab emulator** (see §7).
**Scope:** `gui.screen.inspect` / `gui.tap` / `gui.set_text` /
`gui.scroll` on lab-allowlisted packages only.
**Non-goals:** general app automation, non-lab devices.

## 1. Why this contract exists

Accessibility node ids are **positional DFS indices**: any screen change
can move a different control under an id observed earlier. Acting on a
stale id clicks the wrong control. Contract v1 makes that class of error
structurally impossible: every tap is bound to the snapshot it was
planned from, and the device re-reads fresh state before acting.

## 2. Snapshot (`gui.screen.inspect` → `a11yInspect[WithParams]`)

Success result fields (all bounded client-side):

| Field | Type | Notes |
|---|---|---|
| `contract_version` | int | **1**. Callers must refuse unknown versions. |
| `snapshot_id` | string | `"<8-hex>:<node_count>"`, §3. |
| `package` | string | Foreground package at capture. |
| `activity` | string | Foreground activity class (capped). |
| `nodes` | list | DFS order; `node_id` = list index (int). |
| `node_count`, `truncated` | int, bool | Bounded at `max_elements` (default 50). |
| `max_elements` | int | Limit actually used — echo back on tap, §3. |
| `focused_id`, `keyboard_visible`, `scrollable_regions`, `screen_w/h` | … | Hints, never authority. |
| Per node | map | `node_id` (int), `bounds` [l,t,r,b], `class`, `resource_id`, `text`?, `content_desc`?, `clickable`, `editable`, `enabled`, `visible`, `sensitive` (password heuristic). Text fields are **untrusted data** and capped. |

Failure shape: `{supported: false, reason: <code>, ...}` with codes
`UNEXPECTED_PACKAGE` (includes `expected_package` + `actual_package`),
`STALE_SNAPSHOT` (includes `expected_snapshot_id` + fresh `snapshot_id`),
or a human-readable reason for unavailable service / no window.

`expected_package` (exact match) and `expected_snapshot_id` are accepted
as inspect params and validated **on-device against the fresh tree**.

## 3. `snapshot_id` algorithm (normative)

```
digest = "v1|" + pkg + "|" + activity + "|"
for each node in DFS order:
    digest += "<node_id>|<class>|<resource_id>|<bounds>|" +
              "<clickable>|<editable>|<enabled>|<visible>" + ";"
snapshot_id = hex(hash(digest)) + ":" + node_count
```

- `hash` = Java/Kotlin `String.hashCode` (specified, stable across
  processes), rendered as unsigned lowercase hex.
- **Text and content-desc are excluded**: chat lines and labels change
  without moving tap targets; including them would make every tap on a
  live screen stale.
- The digest covers the **whole walked tree** (bounded by `max_elements`
  / depth 20 / cycle guard, identical on inspect and tap paths). A tap
  planned from an inspect with non-default `max_elements` MUST echo that
  `max_elements` back, or the ids describe different trees.

## 4. Tap (`gui.tap` → `a11yTap`)

Core input schema (`core/device_tools.py`, `additionalProperties: false`):

| Field | Type | Required |
|---|---|---|
| `target_package` | string (dotted path, lab allowlist) | yes |
| `node_id` | **integer** 0–9999 (DFS index) | no (runner refuses when absent) |
| `expected_snapshot_id` | string ≤64 | no (strongly recommended) |
| `expected_package` | string ≤255 | no (recommended) |

Historic note: the schema previously typed `node_id` as string while all
three implementations used ints — v1 pins **integer** everywhere.

Native execution order (all in `ZaraA11yService.tapResult`):

1. Allowlist (Dart + native mirror) → `denied`.
2. Service bound + window present → else `NO_WINDOW`.
3. `expected_package` vs fresh foreground package → `UNEXPECTED_PACKAGE`.
4. Full fresh walk → digest → compare `expected_snapshot_id` →
   `STALE_SNAPSHOT` (returns the **fresh** `snapshot_id` so the caller
   can re-inspect exactly once, never loop blindly).
5. Target present → else `NODE_MISSING`. Clickable → else `NOT_CLICKABLE`.
6. `ACTION_CLICK` → `{applied: true, verify_state: "clicked", ...}` or a
   non-applied failure.

Refusals travel as channel **errors** (`stale` / `mismatch` /
`unavailable` / `denied` / `failed`); the Dart runner maps every
non-applied outcome to `ok: false` with the code in the result. **`ok:
true` is returned only when `applied == true`.** No silent truncation,
no fake success. Stage 29 addition (mutating family): every success
result ALSO carries top-level `verified` — the honest per-action
verification verdict keyed on the NATIVE re-read confirm only
(tap: `verify_state == "clicked"`; set_text: `text_matched`
(applied-notes ⇒ false); scroll: always `false` — its verify is the
caller's snapshot-content diff; back: fresh post-navigation
`snapshot_id` + observed `package_after`). Runner-side default labels
are never proof; the runner-default 'clicked' fallback with native
omission stays `verified: false`. Core declares this contract as
`verification = "explicit"` (fabric keys on the flag; absence audited
as `verification_failed`).

## 5. What v1 does NOT do

- No messaging workflows (search/open/compose/send/verify) — needs a
  messaging-app contract against real app accessibility trees.
- No scroll/text-entry/select/dialog primitives — same reason.
- No emulator-independent verification: digest equivalence between the
  inspect and tap walks is a native invariant, verified on-device.
- Retry policy: never re-tap against a changed screen without a fresh
  inspect. A `STALE_SNAPSHOT` permits exactly one re-inspect + re-plan;
  repeated staleness means the screen is live — stop and tell the user.

## 6. Change log

- **v2.1 (universal operator)**: adds `gui.back` — a guarded global
  navigation (back/home) via `performGlobalAction`. It is NOT
  snapshot-bound (not positional); instead it carries the same
  foreground guard discipline: an optional `expected_package` (from the
  caller's fresh inspect) is compared against the CURRENT root package
  and a mismatch refuses with UNEXPECTED_PACKAGE before anything
  happens. Success returns `package_before`/`package_after` and the
  FRESH post-navigation snapshot_id; the caller verifies the new screen
  with its next inspect like everywhere else. Lab-allowlist target
  refusal (`denied`) applies exactly as for taps. Recents, global
  search, and multi-window are deliberately excluded from the contract.
  Also adds `android.apps.list` (launchable app discovery, read-only
  metadata, capped — never an authorization) and device-side focus
  verification on `system.app.open` (`focused_verified` or honest
  `focus_unconfirmed`, sampled via the a11y foreground package).
- **v2**: adds snapshot-bound `gui.set_text` (ACTION_SET_TEXT on editable
  nodes, content-verified by re-read when readable, typed text never
  echoed) and `gui.scroll` (ACTION_SCROLL on scrollable nodes, fresh
  post-scroll snapshot_id returned for caller diffing), with
  NOT_EDITABLE / NOT_SCROLLABLE / EDIT_FAILED / SCROLL_FAILED /
  EMPTY_TEXT codes. Snapshot shape and the v1 `snapshot_id` algorithm
  are unchanged, so v1 ids remain valid taps under v2.
- **v1**: `contract_version`, `snapshot_id`, `max_elements` echo, tap
  guards, integer `node_id`, error codes, `applied`-only success.
  Supersedes the unversioned boolean tap.

## 7. Live validation (ZaraLab emulator, 2026-10-16/17)

All cases executed against the real accessibility service via the
production path (Core `/v1/exec` → approval → agent poll → runner →
bridge → `ZaraA11yService`), contract build installed as debug APK:

- Inspect returns `contract_version` + `snapshot_id` + bounded nodes.
- Tap with a fresh id succeeds (`applied: true`); the tap-time digest
  EQUALS the inspect digest (walk equivalence proven live, twice:
  `7cc14564:39`, `482f1cb8:37`).
- After a UI change, the same id is refused as STALE_SNAPSHOT and the
  refusal carries the FRESH id end-to-end (native details → Dart merge
  → Core proxy evidence → exec error).
- Re-inspect returns exactly the refused fresh id; tapping with it
  succeeds.
- Wrong package → UNEXPECTED_PACKAGE (+ actual package); non-clickable
  node → NOT_CLICKABLE; non-editable set_text → NOT_EDITABLE;
  non-scrollable scroll → NOT_SCROLLABLE. Every refusal is
  `status: failed` — no refusal was ever reported successful.
- Scroll on a real ScrollView applied with a fresh post-scroll id.
- `set_text` content verification (`text_matched`) is unit-tested and
  code-reviewed but NOT live-proven: the allowlisted app's current UI
  exposes no editable node (Flutter composer hidden), so no editable
  target existed to type into. Same for a real messaging app: none is
  installed on the emulator, so conversation discovery/open/read/send
  run against realistic fakes (`tests/test_messaging.py`, labeled
  SIMULATED) plus the `core/messaging.py` contract.

### 7.1 Universal operator live validation (2026-10-17)

Fresh build + fresh core `:8080` + pairing (`android-phone`) + a11y
enabled; laptop-1 agent live via `device/linux/agent.py`:

- `android.apps.list` LIVE: 7 real launchable apps returned (Chrome,
  Settings, Calendar, Gmail, STK, Voice Search, zara_android,
  `truncated: false` after the cap-fix build).
- `system.app.open com.android.settings` LIVE: approval → launch →
  device reports `focused_verified` (a11y foreground package sampled
  after settle). Settings visibly took focus.
- `gui.back` (back) LIVE: applied; fresh post-action snapshot id
  returned (`refreshed` root same-package on the app root screen —
  honest no-op evidence, not invented movement).
- `gui.back direction=home` LIVE: applied; `package_after`
  `com.google.android.apps.nexuslauncher` with fresh snapshot
  `17b6f592:17` — real navigation observed, not a claim.
- Mixed-device NL mission LIVE: utterance
  `apps on my phone then battery on my laptop` → plan of 2 steps on two
  devices (`android-phone`, `laptop-1`) → both steps ok in ONE mission:
  real app listing on the emulator AND a real laptop battery read
  (`/sys/class/power_supply/BAT1`, 80%). Mission `mission-3a63eaa046c7`.
- NOT live-provable: `gui.back` UNEXPECTED_PACKAGE refusal against a
  foregrounded non-lab app — the agent app deliberately pauses job
  polling in background (documented, battery-honest), so a job to the
  app while another app is foreground expires honestly
  (`device job expired`, execution FAILED). The guard path itself is
  unit-tested (`universal_operator_test.dart`) and the native refuse
  code path is identical to the tap guard proven live above.

## 8. Messaging workflows (mission-bound single approval; live app pending)

`core/messaging.py` has two layers over these primitives.

**Pure contract:** conversation discovery (exact-substring,
ambiguous-vs-missing structured outcomes), composition-field and
send-button location, snapshot-bound send-plan building (tap-open →
set-text → tap-send, single attempt each), send verification (`sent`
only on a new matching node, else `uncertain` — never auto-retry), and a
TTL'd `SendLedger` fingerprint record.

**Mission-bound single approval (`MessagingWorkflow` /
`MessagingApprovalStore`):** ONE human approval covers the whole
sequence (open conversation → inspect → compose → send → verify),
replacing the earlier per-step holds. The approval record binds:
mission id, target device, recipient/conversation label, the exact
message-content fingerprint (never plaintext in the record's view or in
audit), and an ordered action skeleton; `plan_fingerprint` covers all of
them, so a material change (recipient, text, device, action sequence) or
a tampered record invalidates it and requires a fresh decision.

Execution runs ONLY the bound plan. Each action is executed through the
UNCHANGED `fabric.execute_on_device` path — identity → trust → presence
→ capability authorization → governor → engine → verification — and the
engine's HIGH_RISK pre-dispatch re-validation still applies. The single
approval contributes only a scoped `policy.grant` per bound action
(minted after a deny-pattern re-check), never a policy bypass; a direct
HIGH_RISK submit without the approval still parks (control-tested).

Targets are re-resolved from FRESH snapshots at execution time (node_ids
are positional and volatile across navigation): a vanished/ambiguous
conversation, a missing field or send button, a stale snapshot, or an
offline device aborts honestly BEFORE any send. The `SendLedger`
records a fingerprint only when the send tap was APPLIED (never on a
pre-send refusal), so a potentially-successful send is never blindly
retried; it is an idempotency record, not an authorization. It persists
across restarts when `ZARA_MESSAGING_DB` names a file.

HTTP surface: `POST /v1/messaging/send` (resolve + present, executes
nothing), `POST /v1/messaging/approvals/{id}/approve`,
`.../cancel`, `GET .../{id}`, and `GET /v1/messaging/approvals`
(redacted pending rows only: recipient, state, bound action labels, ids
— never message plaintext or the binding fingerprint; bounded,
oldest-first). Structured outcomes: `succeeded`,
`uncertain`, `failed`, `blocked`, `stale`, `ambiguous`, `duplicate`,
`cancelled`, `expired`.

The companion app surfaces the decisions on the Tasks tab
(`android/lib/messaging_review.dart` + `CoreClient` messaging methods):
pending approvals load on manual Refresh; each row offers
Approve-and-send / Cancel against the exact bound plan (the same
one-time consumption as the HTTP surface); rows without a decision
target or in terminal states are display-only; decision failures map
409/404/401 explicitly and always end with a reload from Core, never a
guessed state. No message content is ever rendered.

Simulated coverage is `tests/test_messaging.py` (17) and
`tests/test_messaging_workflow.py` (32), both labeled SIMULATED, using
scripted GUI handlers over the REAL fabric/engine path.

The request snapshot is OPTIONAL. When a caller supplies a fresh
conversation-list snapshot, the recipient is pre-verified before any
approval exists. When it is absent (e.g. a natural-language request),
the approval binds the caller-supplied recipient label and the
conversation is resolved from a fresh snapshot INSIDE the approved
execution — so the initial read is covered by the SAME single approval,
and an unresolvable recipient aborts at execution without guessing.

Natural language routes here: the local parser recognises
`send "<text>" to "<contact>"`, `message|tell|text "<contact>" [saying]
"<text>"` and proposes the `messaging.send` pseudo-capability (grounded,
never sent to the resolver as a tool). `IntentTurnService` routes it to
the workflow, which presents the approval; nothing executes until the
user approves. What remains for a real app: an allowlisted messaging
package/account and live verification of `text_matched` and
`verify_send(sent)`.

## 9. Semantic targets and `gui.find` (v3) — 2026-10-18

Adds stable-selector targeting on top of the v2 snapshot discipline.
`contract_version` is now 3.

### 9.1 Why

Positional `node_id`s are DFS indices — fragile across relayouts.
Resource ids, content descriptions, and text survive relaunches. Mission
§3 requires preferring stable selectors, so the acting tools now accept
a semantic `target` resolved against the **fresh** tree at action time
(no snapshot→act staleness window beyond what the native guards already
check).

### 9.2 Selector object (normative)

`target` is an object with AT MOST these keys, at least one selector key
present:

- `resource_id` (≤128 chars) — preferred, exact match, most stable;
- `content_desc` (≤120) — matched in matcher-canonical form;
- `text` (≤120) — matched exact-or-canonical (NFC, bidi controls
  stripped, whitespace collapsed, lowercased — mirrors core
  `uicontrol.normalize_text`);
- `role` (≤40) — class name (e.g. `android.widget.EditText`), matched
  in canonical form;
- `index_hint` (non-negative integer) — 0-based disambiguator among
  matches.

Rules:

1. A node matches when EVERY provided selector matches (AND semantics).
2. Zero matches → `TARGET_NOT_FOUND`; more than one match without
   `index_hint` → `TARGET_AMBIGUOUS` (candidates list, ≤8, with
   node_id/resource_id/role[+bounds for find]); `index_hint` beyond the
   match count → `TARGET_INDEX_OUT_OF_RANGE`. All are refusals: the
   device never guesses.
3. Selector values shaped like secrets (`password`, `token`,
   `api_key`, `secret`, `credential`, `private_key`, …) are refused
   `INSECURE_TARGET`. Unknown keys → `TARGET_INVALID`. Fail closed.
4. `target` and positional `node_id` are mutually exclusive
   (`TARGET_CONFLICT`); references below use the resolved element.

### 9.3 New evidence keys

- Acting results, when a target was used, carry `target_matched`
  {node_id, resource_id, content_desc, text, role (given casing only —
  canonical never overwrites the stored value)}.
- The resolver runs inside the same walk that the staleness digest
  uses: one walk proves freshness AND resolves the selector, so a
  resolved element can never be older than the checked snapshot.

### 9.4 `gui.find` (read-only lookup)

`gui.find` = same resolution against the fresh tree WITHOUT acting:

- `MATCH` result (observation only): {status: MATCH, package,
  snapshot_id, contract_version, element {node_id, resource_id,
  content_desc, text, role, bounds, clickable, editable}, note}.
- `TARGET_NOT_FOUND` / `TARGET_AMBIGUOUS` / `UNEXPECTED_PACKAGE` /
  bad-target arrive as failures with evidence — callers can NEVER
  mistake a no-match for success.
- Same lab allowlist as the tap family; CONFIRM risk, RESTRICTED
  permission, capability id `gui.find` (must equal the device-advertised
  record — mismatch authorizes never).
- Facts are captured already scrubbed: password fields contribute no
  text (v2 discipline); `gui.find` on a resource-id selector never
  returns the field's content unless readable text is itself permitted
  by the sensitive check.

### 9.5 Scope notes

- `gui.scroll` stays positional in v3 (scroll targets the container;
  index is stable enough within one snapshot-bound action).
- Android lifecycle unchanged; `a11yFind` is a new platform channel on
  the same accessibility service. `freshWalk`'s digest was NOT changed
  (additive WalkFacts only) so v2 snapshot ids remain byte-stable.

### 9.6 Live validation (ZaraLab emulator, 2026-10-18)

All runs through the real chain (HTTP /v1/exec → policy → approval →
job → Dart runner → DeviceBridge → SemTarget/native walk):

- exact content_desc MATCH (`Battery: …`) → node 13 w/ bounds,
  contract_version 3, "read-only lookup" note; repeated on the FINAL
  clean build.
- Canonical normalization: target `Tasks   Tab 2 of 4` (spaces) matched
  the node's `Tasks\nTab 2 of 4`; role+index_hint resolved node 34.
- Ambiguity: content_desc `Approve` → TARGET_AMBIGUOUS (3 candidates);
  role-only `android.widget.Button` → TARGET_AMBIGUOUS (10 candidates);
  both refused without guessing, index_hint resolves honestly.
- Honest NOT_FOUND: text selector against this Flutter app (empty text
  fields, labels in content-desc) and `Chat\nTab 1 of 4` while the tab
  badge prefixed the desc (`19\nChat\nTab 1 of 4`) — exact-match
  discipline refuses, never substring-matches.
- Acting-with-target: tap by content_desc switched to the Tasks tab,
  applied + verify_state clicked + target_matched {node 32,…}; tap with
  a no-match target → TARGET_NOT_FOUND (refused); tap with target+node
  → refused ("give target or node_id, not both"); secret-shaped target
  → INSECURE_TARGET refusal at the runner, pre-gateway.
- Guards: find with mismatched expected_package → UNEXPECTED_PACKAGE
  (actual package surfaced).

#### 9.7 Live validation — positive semantic text entry (ZaraLab, 2026-10-18)

Live fixture: the Settings tab's enrollment EditText (first-party lab
app, single editable node, inert unless the adjacent Enroll button is
tapped — never tapped). Every case through the full production chain
(HTTP /v1/exec → policy → approval → job → runner → bridge → native
a11y action → observation), approver = operator token; contents
non-consequential:

- find {role: android.widget.EditText} → MATCH node 28, editable:true,
  contract_version 3 (fresh-tree resolution at action time).
- set_text-with-target, value-changing writes: `zara-live-check-01..04`,
  `em—dash ✓ தமிழ் vanakkam`, `  spaced  \nline2` → applied:true,
  verify_state "text_matched" (observed post-action), plus independent
  out-of-band read-back (uiautomator dump) byte-identical each time.
  Unicode/Tamil survived transport intact; whitespace and newline
  preserved literally; re-submission of the identical value also
  text_matched.
- Refusal battery re-run on the final build: absent target →
  TARGET_NOT_FOUND; role=Button on a 9-button screen → TARGET_AMBIGUOUS
  ("9 elements match… give index_hint"); content_desc `Enroll`
  resolved by find (editable:false surfaced) but set_text →
  NOT_EDITABLE; target+node_id → TARGET_CONFLICT; secret-shaped
  selector → INSECURE_TARGET (runner, pre-gateway); shade covering the
  app → UNEXPECTED_PACKAGE (com.android.systemui); deliberate approval
  denial via the companion app's Deny button → cancelled (audit
  `approval_deny`), never dispatched.
- Honest-defect fix found during live validation (also the previously
  pending label above, now closed): ACTION_SET_TEXT could complete while
  the in-process a11y tree still served the pre-edit text, so
  value-changing writes reported the honest "applied (content changed,
  re-read differs)" state for a write that HAD landed (immediate
  fresh-walk re-read missed; an independent later read confirmed the
  new value). Fix: one bounded async grace re-read (POST_EDIT_REREAD_
  GRACE_MS = 800, single attempt) scheduled by the bridge only on the
  content-changed branch; the exact DFS node is refreshed before the
  text read (same-process node state). Honest states unchanged:
  text_matched only when actually observed; sensitive fields still
  skip re-read; structural drift still keeps the uncertain state.
  Regressed live (value 01→02, unicode, whitespace cases) on the
  rebuilt APK.
- Known-race note: approval re-validation can honestly deny
  ("capability snapshot stale"/"OS permission denied") when the
  approval lands inside the device's re-register→describe window or
  after a service rebind; retry after the next describe succeeds. Same
  behavior as documented earlier.
- Plan-level multi-action: a 4-step all-public/safe plan
  (battery/network/apps.list/charging_time) ran to COMPLETED in one
  mission in 97.2 s with 0 approvals, 0 model calls (per-step job
 _results in the audit; cadence = the device's 30 s poll tick). A
  plan whose steps need approval still parks at the first hold by
  design (never speculatively continues; the only resume path is the
  conversational loop with a real LLM) — consequential single steps
  were therefore each driven as a 1-step mission through the same
  chain (inspect → find → tap(tab) → find(observable) → tap(revert)
  → find → set_text), each with exactly one approval, zero model
  calls, per-step honest results (clicked/text_matched verified, plus
  independent read-back).
