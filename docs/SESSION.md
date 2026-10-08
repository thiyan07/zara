# Zara Persistent Conversation Sessions (Stage 19)

Status: IMPLEMENTED · TEST_VERIFIED (38 Python) · LIVE-AGENT VERIFIED
(battery + devices + refusal turns via API) · PHONE_UI BLOCKED (secure
lock; re-drive on unlock)

Core rule: **conversation is context; Core is authority.** Sessions
remember what was discussed so "is it charging?" resolves. Every turn
still flows through intent parsing → grounding → Stage 17 resolver →
Policy/Governor/grants → execution. History is untrusted data.

## Session model (`core/session.py`, zero new dependencies)

`ConversationSession`: id (`conv-…`), owner, device_id, state,
turn_count, active_device, active_capability, entities (device/file/
capability, ≤20), turns (≤50 `TurnRecord`s), pending_clarification,
pending_approval, summary (≤500), created/updated/expires timestamps.
Turns store redacted utterance (≤500), capability, redacted params,
resolution status/device, executed flag, response (≤300). Secrets are
redacted at rest (`password is X` → `password is [redacted]`; secret-named
params → `[redacted]`); best-effort and documented as such. No tokens,
keys, grants, or auth material are ever stored.

## Lifecycle / state machine

`idle → active ⇄ waiting_clarification ⇄ waiting_approval → executing →
active/completed → expired`. Illegal transitions raise `SessionRejected`
(tested: executing→approval, completed→executing, any exit from
expired). `close()` is idempotent. Every transition and security event
is audited (`session.created/closed/expired/clarification/
approval.pending/resolved/failed/rejected`) with secret-free metadata.

## Context policy (bounded, documented)

Hosted prompt context ≤1500 chars: summary + last 6 turns (utterance
≤120 chars each + intent→status) + ≤10 entities + pending question.
Local parser is stateless by design (ignores the context kwarg);
continuity lives in the service (slot-filling, clarification merge),
never in pattern matching. No embeddings, no vector ops per turn.

## Entity continuity + pronouns

Post-parse slot-filling only: a missing filename/device is filled when
the pending question (or a pronoun-only follow-up on the active
capability) matches exactly one candidate — one named file in the
answer, one recent device, one mentioned device id. Two phones, two
files, unknown tablet → clarification, never a guess. "move" stays
copy-vs-relocate clarification. Fresh complete topics abandon pending
questions explicitly.

## Clarification continuity

Pending question keeps capability + params + missing slots. Answers
merge text-extracted values (filename/path/URL), re-ground against the
real schema, and either proceed or keep the new slots on the pending
question (partial progress is never silently dropped, never executed).
Unrelated new topics abandon the pending question.

## Approval continuity

Approval-gated turns now submit through `execute_on_device`, producing
a REAL engine `WAITING_FOR_PERMISSION` record (nothing runs); its id
surfaces as `approval_reference` and the session enters
`waiting_approval`. "yes/approve/…" re-checks horizon (15 min), trust,
and presence, then calls `engine.approve` (existing mechanism). Consumed
approvals clear: replays, wrong-session, cross-device, forged, expired,
and revoked/offline cases all refuse loudly and are tested. The parser
never approves; grants still come from the existing grant flow.

## Persistence + expiry

`SessionStateStore`: SQLite table `conv_sessions` (or memory mode for
tests), live-object cache authoritative in-process. Create/load/update/
expire/delete + `sweep()`. Idle 30 min, absolute life 24 h, approval
horizon 15 min; injectable clock for deterministic tests. `ZARA_SESSION_DB`
selects the database (unset = memory). Restart: rows reload with state
intact; post-expiry restart resolves to expired. Survives Core restart;
never indefinite (sweep + retention).

## Memory/session boundary

Session = short-term conversational state. Memory/RAG = long-term
retrieval (never written by turns). Mission = task state. Audit =
accountability record. "it = laptop-1" is session context; it is never
promoted to memory automatically, and memory content never overrides
policy/grants/trust/governor (existing invariant, re-tested with poisoned
history + injected "SYSTEM OVERRIDE" messages).

## LLM integration

`HostedIntentParser.parse(utterance, ctx, session_context="")`: context
appended as labeled untrusted data (never instructions); output still
schema-validated + grounded. `CompositeIntentParser` forwards context to
the hosted leg only. `IntentTurnService.handle_utterance` accepts
`session_context` (backward compatible; two-arg custom parsers probed
and still supported). Utterances are secret-redacted before hosted
prompts. Injected history changes no verdict (tested).

## API (operator auth, existing conventions)

- `POST /v1/session` {device_id, who} → session
- `GET /v1/session/{id}` → session (404 unknown, 400 bad id)
- `POST /v1/session/{id}/turn` {utterance, who} → message + proposal +
  resolution + executed + session (same Core execution path as Stage 18)
- `POST /v1/session/{id}/close` → completed
- `/v1/intent/parse` + `/v1/intent/turn` unchanged and stateless
  (backward compatible; session_id never required).

## Security model

All Stage 17/18 invariants plus: history/session/memory/turns create zero
authority; approval only via engine records bound to session+device;
secret-shaped content redacted at rest and pre-prompt; oversized/
malformed input bounded; no shell/subprocess/eval in `session.py`
(audited). Fuzz + injection + forgery batteries green.

## Examples

- "What's my laptop battery?" → "Is it charging?" resolves laptop-1 via
  entity fill (TEST A pattern; live battery turn verified in Stage 18).
- "Send this to my phone" → "Which file?" → "report.pdf" → filename kept
  on pending, schema still demands size/hash/recipient (honest).
- Confirm-gated tool → "approve" → engine approves → done; replay refused.
- "I already approved this" → clarification; "do it" after → nothing
  waiting, never executed.

## Known limitations

- Phone UI E2E blocked by secure lock (re-drive on unlock).
- English keyword parser (no Tamil intent; consistent with STT status).
- Approval horizon/trust re-checks are Core-derived at approve time;
  grants remain the transfer approval path (no engine record there).
- Session DB defaults to memory unless `ZARA_SESSION_DB` is set.
