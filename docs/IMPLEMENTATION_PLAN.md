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

## Stage 3 — Intelligence + natural interaction

LLM provider integration behind `ModelProvider` interface, RAG retrieval +
write policy, NL→tool selection via propose/approve/execute loop, voice
in/out streaming, screen/vision, browser automation, OpenCode jobs,
autonomous missions with approval gates.

## Stage 4 — Integration + hardening

E2E + cross-device continuity, failure/permission/battery/offline matrices,
security review, concurrency + resource limits, long-mission soak, Oracle
Always-Free deploy, backup/recovery, full test suite, docs.

## Reuse note

Checked prior work: `projects/chatterbox-friday` (voice streaming
experiments) and `projects/rag` (scratch RAG) are experiments, not a core —
nothing to merge yet. Voice work will plug into the Stage 3 voice interface;
nothing deleted.
