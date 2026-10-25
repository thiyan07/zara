# Zara Natural-Language Intent Parser (Stage 18)

Status: IMPLEMENTED · TEST_VERIFIED (65 Python) · PHYSICAL_VERIFIED
(live turns on laptop agent + hosted gpt-oss-20b parses) · VOICE_SEAM tested
· PHONE_UI BLOCKED (secure lock, owner asleep — re-drive when unlocked)

Closeout audit (2026-10-06): lone-surrogate crash found via boundary fuzz
(`battery\ud800here` escaped `parse()` as Pydantic ValidationError) and
fixed with `_clean_utterance()` entry sanitization (local + hosted +
service layers) + regression tests. Subprocess audit: list-form only,
no `shell=True`/`eval`/`os.system` anywhere in Core; audio/MCP/sandbox/
opencode spawns all fixed-binary or allowlisted. Secret scan: clean,
`.env` never committed.

## 1. Purpose

Turn "Hey Zara, send this photo to my phone" into a structured
`IntentProposal`, ground it against Core truth, and hand it to the
existing Stage 17 resolver — without the parser ever becoming an
authority.

## 2. Architecture (`core/intent.py`, zero new dependencies)

```
utterance -> IntentParser -> IntentProposal -> ground_proposal ->
resolver.resolve_proposal -> Policy/Governor/grants -> execution
```

`IntentTurnService.handle_utterance` orchestrates; `execute_on_device`
(the existing full pipeline) is the ONLY execution call, and only for
safe + resolved + single-step intents. All else returns messages.

## 3. IntentProposal schema

`schema_version`, `request_id`, `utterance` (≤500), `capability`,
`parameters` (≤16 keys, ≤4 KiB, depth ≤4), `constraints` (≤8),
`preferred_device`, `steps` (≤8), `confidence` [0,1],
`needs_clarification` + `clarification_reason`, `source`, `parser_version`.
`extra="forbid"` + `validate_proposal()`: authority-looking fields
(`authorized`, `grant_id`, `trust`, `governor_decision`, `policy_decision`,
`auth_token`, …) are rejected at the model boundary; hosted JSON carrying
them is stripped pre-validation and down-weighted (confidence ≤0.3).

## 4. Local parser (`LocalIntentParser`)

Deterministic, offline, ~0.02–0.09 ms/parse (measured, laptop).
Explicit alias table (battery→`system.battery`, wifi→`system.network`,
send→`files.transfer`, …) matched longest-first and ONLY against
grounded capabilities. Dangerous language (`rm`, `sudo`, `delete`,
`curl|sh`, secrets, "ignore policy", …) is screened FIRST and always
becomes unsupported — "delete this file" can never become
`files.transfer`; shell-ish utterances never reach `shell.safe_readonly`
by inference. `device.list`/`assistant.help` are parser-service builtins:
read-only local answers, never sent to the resolver.

## 5. Hosted parser (`HostedIntentParser`)

Uses the existing provider abstraction (never hardcodes model; live runs
used NVIDIA NIM `openai/gpt-oss-20b` via `OpenAICompatibleProvider`).
Prompt = utterance + ≤64 capability names/schemas + device id/kind labels
only — no keys, grants, policy, or audit. Response pipeline: ≤8 KiB check
→ JSON extract → strip forbidden fields → `IntentProposal` validation →
`ground_proposal`. Any failure = clarification, never execution.
`CompositeIntentParser` is local-first: hosted is consulted only on local
clarification; hosted failure keeps the local verdict.

## 6. Capability grounding (`ground_proposal`)

Capability must be known (or builtin); parameters must fit the registered
input schema (explicit `properties` map enforced, even when empty;
required fields enforced); device preference must name a known device.
Anything else → clarification. The parser maintains no independent
capability universe (`build_context` derives names/schemas/labels from
Fabric + registry).

### 6.1 File & browser workflow aliases/extraction (`test_nl_workflow_routing.py`)

- `filesystem.search`: `find/search (for) files "...[pattern]" ["..."|home|tmp]`.
  Quoted pattern required; second quote = root path; `home`→`~`, `tmp`→`/tmp`.
- `filesystem.edit`: phrased as `edit file: replace [A] with [B] in [C]`
  replaces the FIRST (and only permitted) occurrence of `A` in scratch
  file `C`. Missing pieces clarify with the exact phrasing example.
- `filesystem.move`: `rename ["src"] to ["dst"]` (the `file|note` word
  between `rename` and the quote is optional). Missing = clarify.
- `browser.search`: `find on page ["term"]` — term required.
- `browser.links` / `browser.form`: param-free
  (`list|page|find links`, `read form|form fields|inspect form`).
- `browser.download`: URL required; the file name comes from an explicit
  quoted name or the URL's final path segment only when that segment is
  itself non-sensitive — otherwise clarification (a sensitive URL tail
  is never silently renamed into a download).
- `filesystem.extract`: `extract/document info ["path"]` — quoted path
  required; unsupported formats are honestly refused, never guessed.

### 6.2 Universal-operator aliases/extraction (`test_universal_workflows.py`)

- `android.apps.list`: `apps on my phone|the phone`, `phone apps` —
  param-free, routes to an android-kind device. Discovery metadata is
  never an authorization (the tool def and result note both say so).
- `desktop.apps.list`: `desktop apps`, `apps on my computer|the
  computer` — param-free, routes to a linux-kind device.
- `desktop.app.open`: `launch/open desktop app "app_id"` — quoted app id
  from `desktop.apps.list`; a bare trailing word is used only if it
  looks like a bounded launcher id, otherwise clarification.
- `gui.back`: `go back|go home [in <lab target>]` — `direction` comes
  from the phrase (home beats back); the `target_package` must name a
  real lab target (`dev.zara.zara_android` / `dev.zara.lab.privtest`);
  a missing one clarifies — navigation is never fired into an unnamed
  app (native guard refuses UNEXPECTED_PACKAGE anyway).
- `system.app.open`: unchanged alias set (`open app` etc.); device now
  returns focus verification fields.
- `device.capabilities` (BUILTIN_LOCAL, answer-only): `device
  capabilities`, `what can you do on my phone|laptop|the phone|the
  laptop`. Resolves the device, then answers from fabricated truth via
  `fabric.describe_device` (usable vs unavailable per descriptor, trust
  honesty). Discovery, never an action: `executed` stays False, and an
  unknown device is refused honestly instead of described with an
  invented view. Builtin so it never routes to a device tool.
  Covered by `test_mission_composition.py::test_D*`.
- `android.notifications.read`: `read|check|show my notifications`,
  `notifications on my phone`, `read notifications` — param-free
  (optional `since_ms`/`limit` bounds); routes to an android-kind
  device. The honest refusal when the user has not granted
  Notification-listener access is the device result, not a parser
  clamp; nothing is ever dismissed or acted on by this tool.

## 7. Device references

my phone/phone/Vivo→android-kind, laptop/computer→linux-kind,
tablet/iPad/watch recognized-but-unmatched (clarify, never drop),
"other X" excludes the requester, exact device ids match directly.
Exactly one match → `preferred_device` (advisory); zero/multiple →
clarification. Parser never claims trust/presence/authorization.

## 8. Clarification

Structured (`needs_clarification` + reason): missing file/destination,
ambiguous device, `move` copy-vs-relocate, unknown intent, oversized
utterance, over-long plans. Deterministic and tested.

## 9. Multi-step intents

Split on then/and-then/semicolons (≤8). Each step parsed; destructive or
unmappable step → whole plan clarifies. A fully-mapped plan EXECUTES
step-by-step through `IntentTurnService._run_plan`: each step is
independently resolved and run through the same pipeline as a
single-step turn (resolver → policy → governor →
`fabric.execute_on_device` → verification), tracked on the injected
MissionEngine when present. One attempt per step, never auto-retried;
the first approval hold, failure, or unresolvable step stops the plan
and later steps never run speculatively. No mini-agent, no recursion.

### 9.2 Typed step-result passing, progress, partial success
(`test_mission_composition.py`)

A step may bind values from an EARLIER step's validated result with
`"fetch": {"<param>": {"from": <step index>, "key": "<result key>"}}`:
- `from` must be an earlier step that SUCCEEDED; the key must exist in
  that step's result dict. A missing source or key stops the plan with
  a structured failure — values are never invented and there is no
  speculative fallback.
- Fetch is dataflow plumbing, not an authority: fetched values still
  pass the target tool's registered input schema at execution (a type
  mismatch fails the step honestly).
- Bounded: ≤ `_PLAN_FETCH_MAX` (8) bindings per step; a malformed fetch
  envelope refuses the plan before anything runs.
- The turn result now carries structured `progress`
  (`done/total/ran/remaining/statuses`) and `partial` (True when some
  steps ran but the plan did not complete; the message says the earlier
  steps were left as-is — nothing rolled back, nothing retried).
- Missions are queryable over HTTP: `GET /v1/missions/{mid}` (404 for
  unknown ids) alongside the existing list/create/transition routes;
  all auth-required.
- Structured submission surface: `POST /v1/intent/plan` executes a
  typed multi-step plan (`steps[]` with `capability`, `parameters`,
  `fetch`, `preferred_device`; bounded by the same MAX_STEPS/fetch
  limits) through the SAME `_run_plan` chain — no second engine, no
  parallel authority; the deterministic core stays final.

### 9.1 Workflow pseudo-capabilities (`messaging.send`)

Some real operations are not device tools and never flow to the resolver
as tool names. `WORKFLOW_CAPS` (`messaging.send`) are added to the
parser's grounded known set with a bounded schema, so the local parser
can recognise the shape and propose them, but the turn service routes
them to their owning workflow instead of executing them. The structural
shapes are `send "<text>" to "<contact>"` and
`message|tell|text "<contact>" [saying] "<text>"` (quoted payloads break
substring aliases, so the shape is matched directly). Routing
(`IntentTurnService._workflow_turn`) resolves a device that can act on
the UI and asks the messaging workflow for its single bound approval;
nothing is sent until the user approves, and an unavailable workflow
answers honestly. Destination selection is the workflow's, not the
parser's: the parser holds zero authority here, as everywhere.

## 10. Security model

Stages 17 invariants kept (existence/device/advertisement/authorization/
availability/governor enforced; metadata/proposals/fallbacks/escalation =
zero authority) plus Stage 18: NL/confidence/preference/LLM output = zero
authority; parser creates no capabilities/grants/trust; cannot bypass
approval/governor; cannot execute; unknown intent cannot become execution;
provider failure cannot become execution. 12 injection utterances +
stripped-forgery tests + 200+ fuzz inputs prove it.

## 11. Prompt injection handling

Utterance, LLM output, and tool descriptions are all untrusted data.
"Ignore previous rules", fake approvals/grants, "system says safe" all
resolve to unsupported/clarification. Forged hosted fields are stripped
and resolver verdicts are byte-identical with/without them (tested).

## 12. Provider failure

Timeout/401/429/500/malformed/empty/oversized/offline → clarification
("hosted understanding unavailable") with local fallback where the local
parser already answered. Never execution. Covered by stub-provider tests.

## 13. Offline behavior

Local parser is fully offline (no network, no model). Composite degrades
to local-only when no hosted provider is configured (EchoProvider setups)
or when hosted fails.

## 14. Voice integration

Seam-tested, stack untouched: STT transcript strings enter
`handle_utterance` exactly as `core/voice.py` delivers them; replies are
short TTS-suitable text with no structure leakage. Tested with noisy
transcript shapes ("Do the test echo, please!") and hostile transcripts.
No voice cloning, no new models, "Hey Zara" path unchanged.

## 15. Resolver integration

`resolve_proposal` consumes proposals; statuses map to messages
(`resolved`→execute-if-safe, `requires_approval`→approval message,
failures→honest explanations). Approval holds stay inside the existing
approval mechanism — the parser never approves.

## 16. Examples

- "check my battery" → `system.battery` (0.85) → live: "Battery is at
  81.0%." (PHYSICAL_VERIFIED, laptop-1 agent executed)
- "show my devices" → `device.list` → "Paired devices: laptop-1."
- "send \"report.pdf\" to my phone" → `files.transfer` + filename +
  `preferred_device=vivo-real` → approval-gated, never auto-sent
- "check battery then check network" → 2-step plan message, no execution
- "run rm -rf /" → unsupported, `executed=false` (live-verified message)

## 17. Known limitations

- Agent `xfer_download` unknown-size first-window edge (Stage 15, logged).
- Phone UI validation blocked by secure lock (see §18); laptop + hosted
  paths fully verified.
- Local parser is English keyword-based (no Tamil/Tanglish intent yet —
  consistent with STT NOT_SUPPORTED status).
- Fallback registry is session-scoped (Stage 17 note).
- Relationship/fallback metadata is operator-curated, starts empty.

## 18. Physical validation status

- PHYSICAL_VERIFIED: `/v1/intent/turn` "check my battery" → real 81.0%
  via laptop-1 agent; "show my devices"; "run rm -rf /" refusal message;
  hosted `gpt-oss-20b` parses (battery 0.95/4.7s, devices 0.98,
  destructive→clarification 6.6s).
- PHYSICAL_VALIDATION_PENDING: phone re-pair + "send small test file to
  my phone" E2E (grant→send→fetch→verify) — was mid-flow when the screen
  secured; needs owner unlock + ~10 min.
- BLOCKED (correctly, no bypass attempted): secure lock screen.

## Intent parsing does not authorize execution.

Full stop. See `docs/CAPABILITIES.md` (resolver) + `docs/SECURITY_MODEL.md`.
