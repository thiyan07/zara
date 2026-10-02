# LLM Architecture (Zara Stage 3)

The LLM is Zara's reasoning engine. The deterministic core is Zara's authority.
This document states the boundary precisely.

## Pipeline

```
Text/Voice -> Context+Memory -> LLM proposes (ONE JSON action) ->
validate schema/tool/args -> policy -> governor -> router -> device tool ->
verification -> result back to LLM -> natural response
```

## Provider layer (`core/providers.py`)

`ModelProvider.reason(system, user, tools)` returns one structured
`LLMOutcome`; `stream_reason` yields text chunks (true streaming where the
endpoint supports SSE, single-chunk fallback otherwise). Errors are
classified (`LLMErrorKind`): timeout/network/malformed/rate_limited/auth/
context_too_large/unavailable/cancelled. Transient failures retry with
backoff (max 2); anything else fails safe with state preserved.

Providers: `echo` (default dev), `scripted` (deterministic stand-in for
tests/keyless E2E — never a real brain), `openai-compatible` (any local
model, free API, or hosted endpoint over stdlib HTTP; JSON-mode requested,
strict-parsed regardless). Selection via `LLM_PROVIDER / LLM_BASE_URL /
LLM_API_KEY / LLM_MODEL`. No key is logged; unset config = echo, never a
paid dependency.

## Loop (`core/conversation.py`)

`ConversationLoop.handle_text` enforces: max tool calls per turn (3),
turn time budget (120 s), repeat-action guard, context budget (6000 chars),
relevant-tools-only offering (max 8, high-risk hidden unless clearly
relevant). Every turn runs inside a Mission (planning->executing->
verifying->completed/failed, or waiting_for_permission). Approval holds
pause; `resume_after_approval` continues after human approval. Provider
failure maps to a safe user message; nothing executes on LLM failure.

## What the LLM can never do

Execute, access fs/db/credentials, bypass policy/governor/routing/approval,
pick devices directly, invent tools or results, self-verify, bulk-write
memory, or see secrets (prompt builder redacts; memory refuses secrets).
See LLM_CONTRACT.md + TOOL_REASONING_LOOP.md.
