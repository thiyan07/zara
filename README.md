# Personal Assistant — Core (Stage 1)

One coherent assistant system; stages are checkpoints, not product forks.
See `docs/` for architecture, contracts, security, battery, and plan.

## Run

```bash
pip install -r requirements.txt
python -m pytest              # 14 contract tests
ASSISTANT_TOKEN=dev-token uvicorn core.app:app --port 8080
```

Auth: `Authorization: Bearer $ASSISTANT_TOKEN` on all `/v1/*` except health.

## Layout

- `core/` — models, tools, policy, execution, events, missions, devices,
  memory, context, providers, scheduler, notifications, audit, governor,
  db, builtin_tools, app
- `tests/test_stage1.py` — contract, failure, permission, timeout tests
- `docs/` — the five specification documents
