# Personal Assistant — Zara (ONE system; stages are checkpoints, not versions)

Wake phrase (reserved, engine in a later stage): **"Hey Zara"**.

One coherent assistant system; stages are checkpoints, not product forks.
See `docs/` for architecture, contracts, security, battery, and plan.

## Run (Stage 2: core + Linux agent)

```bash
pip install -r requirements.txt
python -m pytest              # 31 tests (14 Stage 1 + 17 Stage 2)
ASSISTANT_TOKEN=dev-token uvicorn core.app:app --port 8080
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
