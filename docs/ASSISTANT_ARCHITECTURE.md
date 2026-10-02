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

## Stage 2 addition (same Zara, no redesign)

- Device bodies attached via Device Protocol: `core/device_auth.py`
  (per-device keys), `core/protocol.py` (v2.0 messages), `core/jobs.py`
  (dispatch queue), `core/routing.py` (core-decided routing), `core/device_tools.py`
  (device-backed contracts; implementations live on devices).
- Linux agent: `device/linux/`. Android shell: `android/` (Flutter + Kotlin
  bridge). Boundary: Zara Core -> Device Protocol -> Device Agent -> OS APIs.
- Details: DEVICE_ARCHITECTURE.md, DEVICE_PROTOCOL.md, ANDROID_ARCHITECTURE.md,
  LINUX_AGENT.md, DEVICE_SECURITY.md.

## Stage 3 addition (same Zara)

Reasoning layer attached WITHOUT touching core authority: `core/llm.py`
(structured contract), `core/providers.py` (echo/scripted/openai-compatible,
env-configured), `core/conversation.py` (NL tool loop with budgets),
`core/memory_policy.py` + persistent SQLite memory, `core/sessions.py`,
`core/tracing.py`, `core/voice.py` (STT/TTS/state/wake/pipeline), new
endpoints `/v1/talk`, `/v1/talk/resume`, `/v1/voice/*`, `/v1/wake/*`,
`/v1/sessions`, `/v1/memory/correct`, `/v1/trace/*`. LLM proposes, core
disposes — see LLM_ARCHITECTURE.md, TOOL_REASONING_LOOP.md.
