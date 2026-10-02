# Conversation Architecture (`core/conversation.py`, `core/sessions.py`)

## Turn flow (`POST /v1/talk`)

1. Session get/create (`SessionStore`: id, user/device, capped messages,
   mission/execution refs, voice state, timestamps; compaction past 60 msgs
   keeps 20 + marker; 7-day idle expiry).
2. Memory write-policy pass over the user turn (`MemoryService.consider`).
3. Mission created (planning).
4. Bounded recall (top-3) + budgeted context + relevant-tools-only offer.
5. Structured `reason()` -> validate -> route/submit -> verified result or
   truthful hold -> next step; budgets enforced; mission finished once at
   turn end; task-history memory recorded.
6. `POST /v1/talk/resume` continues approval-held turns after `approve`.

## Context budgets (`LoopConfig`)

max_tool_calls 3, max_time_s 120, max_context_chars 6000,
max_tools_offered 8, memory_top_k 3. Prompts carry: task, world (800),
mission (400), memories 5x200 (untrusted-labeled), device, last 6 turns,
working (800). Secrets redacted at build time.

## Tracing (`core/tracing.py`, `GET /v1/trace/{id}`)

One trace per turn: request/session/mission/execution/device/provider/model/
tool/latency/outcome. Values redacted (device keys, pairing codes, bearer
tokens, api keys). Audit log stays the durable record; traces are the
per-request view.

## Proactive events (foundation)

No phone calls, ever. Mission/device/scheduler events already flow through
`NotificationManager` (task completed/failed, approval required,
device online). Voice/TTS delivery of notifications is later work.
