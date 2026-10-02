# Stage 4 Status (Zara — same system, checkpoints not versions)

## Capability ledger

| Capability | Status | Evidence |
|---|---|---|
| Real LLM provider path (openai-compatible) | INTEGRATION TESTED | real-HTTP stub server: structured, retry, stream, auth-fail-fast |
| Real hosted LLM inference | NOT VALIDATED | no credentials; BLOCKED on API key (user step) |
| Echo/scripted providers | INTEGRATION TESTED | suite + live |
| Android enrollment/auth/heartbeat/caps | INTEGRATION TESTED | live protocol with android-kind device; no physical phone |
| Android secure storage (Keystore) | UNIT TESTED | dart tests; Keystore path NOT VALIDATED (no hardware) |
| Physical Android testing | BLOCKED | `adb devices` empty |
| Voice pipeline (STT→core→tool→device→TTS) | INTEGRATION TESTED | mocks + real proxy/queue/device; audio NOT VALIDATED |
| Wake "Hey Zara" | UNIT TESTED | exact phrase, states, battery gating; detection NOT VALIDATED |
| WebSocket realtime | INTEGRATION TESTED | auth, live events, replay, invalid-frame reject, read-only |
| RAG/memory | INTEGRATION TESTED | bounded, persistent, supersede, injection-tested |
| OpenCode tools | INTEGRATION TESTED | inspect/search/test/diff live; apply_patch + session gated; real CLI run BLOCKED (needs model auth) |
| Browser tools | INTEGRATION TESTED | real Chrome, local pages; thread-safe worker |
| Rate limiting / idempotency / rotation | UNIT TESTED | middleware + store tests |
| Backup/restore / restart recovery | INTEGRATION TESTED | live restart proof |
| Deployment artifacts | IMPLEMENTED | local-run validated; image build + Oracle DEFERRED (no docker/cloud) |

## Truthfulness notes

- Scripted LLM stand-ins used wherever a real model would sit; never
  presented as model output. Live TEST1 battery reading (80%) was real
  sysfs data through the real agent.
- No physical-device, audio, hosted-LLM, or cloud claims are made.
- `code.session` real-CLI execution awaits model credentials; tool
  contract + stubbed path are tested.
