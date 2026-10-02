# Assistant Architecture (ONE system, not versions)

## Principle

One coherent personal assistant. Stages are implementation checkpoints,
not separate products. Never create V1/V2/V3 forks.

```
USER
 ↓
VOICE / TEXT / UI (Flutter Android, laptop UI, API clients)
 ↓
ASSISTANT CORE  (this repo, Stage 1 — deterministic, LLM does not execute)
 ├── Context Manager      core/context.py
 ├── Memory / RAG        core/memory.py  (+ db.py persistence)
 ├── World State          core/models.py  (WorldState)
 ├── Mission Engine       core/missions.py
 ├── Planner / Reasoner   providers + tool selection (Stage 3 grows this)
 ├── Tool Registry        core/tools.py
 ├── Execution Engine     core/execution.py
 ├── Policy Engine        core/policy.py
 ├── Event Engine         core/events.py
 ├── Device Manager       core/devices.py
 ├── Scheduler            core/scheduler.py
 ├── Notification Manager core/notifications.py
 ├── Audit                core/audit.py
 └── Resource Governor    core/governor.py
 ↓
DEVICE / WEB TOOLS (Stage 2: laptop agent, Android node; Stage 3: browser, OpenCode)
```

## Key rule

The LLM **proposes** an action. The core decides: allowed? which device?
which tool? permissions? confirmation? battery OK? how verified? what on failure?
The LLM never executes system commands directly. `execution.py` enforces this:
every run requires a `PolicyDecision` (allow or scoped approval).

## Persistence

- Stage 1: SQLite (zero-dependency, laptop + CI friendly).
- Oracle stage: same schema ported to PostgreSQL + pgvector. `core/db.py`
  uses plain SQL with named parameters and no SQLite-only tricks beyond
  `AUTOINCREMENT`-free DDL, so migration is mechanical. `docs` schema section
  lists the Postgres DDL target.
- Secrets are NEVER in memory/RAG/logs/prompts. Separate secure mechanism
  (Stage 2 device keystore / server env). See SECURITY_MODEL.md.

## Context discipline

Six separated contexts (`core/context.py`): conversation, working, mission,
world state, long-term memory (retrieved, top-k only), device state.
`build_prompt()` assembles only what the reasoning step needs, with hard
token/byte caps. Never dump the whole memory DB into a prompt.

## Battery / offline

First-class, not afterthoughts. `governor.py` gates execution on
battery/charging/network/estimated-cost; `devices.py` tracks online/offline;
offline means queue + preserve mission state, sync later. Details:
BATTERY_MODEL.md.

## API

`core/app.py` (FastAPI): REST (`/v1/...`: chat/propose, tools, missions,
devices, events, memory, notifications, audit) + WebSocket `/v1/stream`
for events and mission updates. Stateless auth hook ready for device tokens
(Stage 2 fills real auth; until then a local dev token gate).
