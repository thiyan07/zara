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

Stage 5 adds MCP interop + sandbox isolation + real-world validation.
Stage 6 validated on the Android emulator (see `docs/STAGE6_EMULATOR_TEST_REPORT.md`). See `docs/STAGE4_STATUS.md`, `docs/TEST_REPORT.md`, `docs/DEPLOYMENT.md`.
Stage 9 builds the Android mobile body runtime: lifecycle, secure identity,
jobs, voice session, notifications, push abstraction — see
`docs/ANDROID_ARCHITECTURE.md`, `docs/ANDROID_STATUS.md`,
`docs/ANDROID_TEST_REPORT.md`. Physical-device testing is pending
(`PHYSICAL_DEVICE_VALIDATION_PENDING` in those docs).

```bash
pip install -r requirements.txt
python -m pytest              # full suite: 1187 passed, 1 skipped
                              # (1 load-sensitive tiny.en STT assertion flakes
                              #  under full-suite CPU contention; green in
                              #  isolation and in the voice subset — evidence
                              #  in tests/test_voice_realistic.py)
ZARA_MEMORY_DB=assistant.db ASSISTANT_TOKEN=dev-token \
  uvicorn core.app:app --port 8080
```

## Verified state (2026-10-11, branch stage15-real-transfer)

- Python full suite: **1187 passed, 1 skipped** (+1 known STT-load flake, see
  above). Focused approval/polling races (`tests/test_grant_race.py` + 12
  lifecycle suites): **212/212**. Flutter: **279/279, analyze clean**.
  End-to-end demo (`scripts/demo_e2e.py`): **9/9**.
- Laptop lifecycle **verified live** (`laptop-1` agent, isolated port):
  submit CONFIRM task → 202 parked → approve 200 → agent polls/claims/
  executes `uname -a` → `succeeded`, verified, genuine output. Duplicate
  approve → 409. Genuine hosted-LLM path verified (`openai/gpt-oss-20b`
  via `/v1/talk` exact-reply + provider-check; not echo/mock/fallback).
- Android physical device: **NOT TESTED** (no device connected / no UI
  harness). Approval→poll wiring is unit-tested (`stage10_approval_test`,
  incl. post-approval immediate-poll nudge guard); full-app widget path
  needs the manual steps in `docs/RUNBOOK.md`.
- Wake-word: detection is a **VAD-gated transcript spotter + opt-in
  keyword-model adapter** (`KeywordModelDetector`), not continuous DSP
  listening; physically unvalidated. Tamil: **capability matrix only**
  (`voice_capabilities()`) — local English-only models refuse Tamil
  loudly; no Tamil acoustic validation. A connected LLM provider is not
  a complete live voice experience (mic→STT→loop→TTS→playback validated
  with local models only; transcript near-misses pinned, not hidden).
- Governor v2: **disabled** (calibration gate authoritative).
- Known residual: concurrent duplicate result deliveries are state-safe
  and emit once via `complete_once()`; emission ordering under true
  simultaneity follows the queue lock (see `tests/test_grant_race.py`
  G6b/G6c).

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
