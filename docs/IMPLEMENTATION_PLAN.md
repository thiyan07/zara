# Implementation Plan (four stages — checkpoints, not products)

## Stage 1 — Core assistant (NOW, this folder)

Backend + contracts + tests. No Android UI yet.

- [x] Docs: architecture, contracts, security, battery, this plan
- [ ] `core/`: models, db, tools, policy, execution, events, missions,
      devices, memory, context, providers, scheduler, notifications,
      audit, governor, app
- [ ] `tests/`: unit + failure + permission + timeout + recovery tests
- [ ] SQLite works; Postgres DDL documented for Oracle
- Gate: `pytest` green; API boots; every contract has tests.

## Stage 2 — Device bodies

Laptop daemon (terminal/files/process/Git/OpenCode tools), Flutter Android
shell + Kotlin bridges, device registration/auth, capability discovery,
battery/network reporting, notification channel. Mock-first tests.

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
