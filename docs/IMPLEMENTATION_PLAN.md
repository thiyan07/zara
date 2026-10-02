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
