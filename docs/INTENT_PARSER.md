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
unmappable step → whole plan clarifies. The proposal DESCRIBES the plan;
the turn service executes nothing multi-step (message lists steps, asks
to proceed one at a time). No mini-agent, no recursion.

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
