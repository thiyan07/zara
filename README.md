# Personal Assistant — Zara (ONE system; stages are checkpoints, not versions)

Wake phrase: **"Hey Zara"** (engine abstraction in `core/voice.py`;
mock-backed until hardware validation — see `docs/WAKE_WORD.md`).

Talk to Zara: `POST /v1/talk {"text": "..."}` — full loop (context+memory
→ LLM proposal → policy/governor/router → device → verified result → reply).
Voice: `POST /v1/voice/turn`; wake: `/v1/wake/*`. Provider config:
`LLM_PROVIDER` (`echo` default) | `scripted` | `openai-compatible` with
`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`. Persistent memory:
`ZARA_MEMORY_DB` (SQLite file; unset = in-memory dev).

## Run (Zara: core + agent + reasoning)

Stage 4 hardened, integrated. See `docs/STAGE4_STATUS.md`, `docs/TEST_REPORT.md`, `docs/DEPLOYMENT.md`.

```bash
pip install -r requirements.txt
python -m pytest              # 90 tests (Stages 1-4)
ZARA_MEMORY_DB=assistant.db ASSISTANT_TOKEN=dev-token \
  uvicorn core.app:app --port 8080
```

Linux agent: see `docs/LINUX_AGENT.md`. Android app: see `android/` +
`docs/ANDROID_ARCHITECTURE.md` (`flutter analyze` clean, debug APK builds).

Auth: `Authorization: Bearer $ASSISTANT_TOKEN` on operator endpoints;
per-device `X-Device-Id`/`X-Device-Key` on `/v1/agent/*`.

## Layout

- `core/` — models, tools, policy, execution, events, missions, devices,
  memory, context, providers, scheduler, notifications, audit, governor,
  db, builtin_tools, app
- `tests/` — `test_stage1.py` (core contracts) + `test_stage2.py` (devices)
- `device/linux/` — laptop agent + safe tools
- `android/` — Flutter shell + Kotlin bridge
- `docs/` — architecture, contracts, security, battery, devices, plan
