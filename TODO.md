# Zara Stage 3 — Todo

- [x] Inspect repo, verify Stage 1+2 (32/32 pass, 22GB free)
- [x] Structured LLM contract (`core/llm.py`)
- [x] Provider layer: reason/stream/cancel/errors/env-config (`core/providers.py`)
- [x] Memory write policy + persistent store (`core/memory_policy.py`, `core/memory.py`)
- [x] Sessions + tracing (`core/sessions.py`, `core/tracing.py`)
- [x] NL tool loop (`core/conversation.py`)
- [x] Voice: STT/TTS/state/wake/pipeline (`core/voice.py`)
- [x] App wiring: stack + endpoints (`core/app.py`)
- [x] Android voice foundation (Dart + Kotlin + tests)
- [x] Secret redaction in prompt builder
- [x] Fix router local-execution fallback (loop tests blocked: no device → deny)
- [x] Fix remaining Stage 3 test failures (62/62 green)
- [x] Full suite green (Stages 1+2+3)
- [x] Docs (9 new + updates)
- [x] Live E2E TEST 1–7 (text, memory, secret, policy, injection, wake, voice)
- [x] APK rebuild (Kotlin changed)
- [ ] Final commit + report
