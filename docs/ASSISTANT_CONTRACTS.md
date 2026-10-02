# Assistant Contracts (the six core contracts)

All contracts are implemented in `core/models.py` + their engine modules.
Field names below match the code exactly.

## 1. Tool contract (`core/tools.py` — `ToolDefinition`)

Every tool defines: `name` (unique, `^[a-z][a-z0-9_.:-]{1,63}$`),
`description`, `input_schema` / `output_schema` (JSON-Schema dicts, validated
on register and at call time), `required_capabilities`, `permission`
(`public|restricted|confirm|high_risk`), `risk` (`safe|confirm|high_risk`),
`estimated_cost` (0..1 abstract energy), `timeout_s`, `success_criteria`,
`failure_behavior`, `verification` (`none|output_schema|explicit`),
`supported_devices` (`["android","linux","cloud","any"]`), `reversible: bool`,
`version`. Handler signature: `handler(inputs, ctx) -> dict`.
`ToolRegistry.register()` rejects duplicates and invalid schemas.
`ToolRegistry.call()` validates input against `input_schema` (minimal
validator — object/required/type/enum), enforces timeout via worker thread,
validates output when `verification == output_schema`.

## 2. Execution contract (`core/execution.py` — `ExecutionRecord`)

Every execution tracks: `id`, `tool`, `inputs`, `device_id`, `mission_id`,
`auth` (PolicyDecision snapshot), `state`
(`pending|running|waiting_for_permission| succeeded|failed|cancelled|timed_out`),
`requested_at/started_at/ended_at`, `result`, `error`, `verified: bool`,
`attempt`, `max_retries`, `cancel_requested`. `ExecutionEngine.submit()`
requires a prior `PolicyDecision.allow()`, else records `denied` and raises
`PolicyDenied`. Retries apply only when the tool's `failure_behavior ==
"retry"` and attempts remain. Cancellation is cooperative via
`cancel_requested` + `cancel()`.

## 3. Policy / permission contract (`core/policy.py` — `PolicyDecision`)

`PolicyEngine.decide(who, capability, device_id, resource, risk, context)`
returns `allow: bool`, `requires_approval: bool`, `grant_id`, `scope`,
`expires_at`, `reason`. Rules: `high_risk` → never auto-allow
(`requires_approval=True`); `confirm` → approval unless a live scoped grant
exists; `safe` → allow, but governor/battery can still defer. Scoped grants
(`grant()`) carry capability+device+resource+expiry; OS permissions and
policy are separate layers — policy never overrides an OS denial, it only
adds restrictions. Dangerous names (`rm -rf`, `shutdown`, credential paths)
are deny-by-default via `DENY_PATTERNS` even if risk was mislabelled.

## 4. Event contract (`core/events.py` — `Event`)

Fields: `id`, `type`, `source`, `device_id`, `mission_id`, `payload`,
`created_at`. Canonical types: `user_message task_completed task_failed
device_online device_offline battery_changed network_changed file_changed
process_failed scheduled_event permission_required permission_granted
mission_state_changed notification_created execution_finished`.
`EventBus` is in-process pub/sub (subscribe/publish/history); persistence +
fan-out to devices/WebSocket happens in `app.py` via subscribers. Prefer
events over polling everywhere.

## 5. State / context contract (`core/models.py`)

`AssistantState`, `UserSession`, `DeviceState` (device_id, kind,
capabilities, online, battery_pct, charging, network, last_seen),
`MissionState` (full lifecycle enum in missions.py), `ExecutionRecord`,
`ConversationTurn`, `MemoryItem` (source/confidence/importance/
created_at/updated_at/expires_at, no secrets), `PermissionGrant`.
All state models are Pydantic v2 with strict field types; DB rows map 1:1.

## 6. Mission contract (`core/missions.py`)

Lifecycle: `created → planning → waiting_for_permission → executing →
waiting → retrying → verifying → completed | failed | cancelled`.
`Mission` has `goal`, `state`, `device_id`, `steps` (each with
tool/inputs/result/status), `checkpoints`, `approvals`, `retries`,
`max_retries`, `error`, `created_at/updated_at`. `checkpoint()` snapshots
steps so execution can resume after restart/offline. `approve()` unblocks
`waiting_for_permission`. Terminal states are immutable.

## Stage 2 additions

- **Device protocol contract** (`core/protocol.py`): `{v, type, device_id,
  payload}` v2.0, strict parsing; 12 message types from register to disconnect.
- **Presence** (`devices.py` + `models.DeviceState.status`): registered |
  online | offline | degraded | reconnecting; `stale_ids()` for reconnect
  candidates. Additive — Stage 1 fields unchanged.
- **Dispatch** (`core/routing.py` + `POST /v1/dispatch`): capability ->
  online -> policy -> governor -> device. Deny/defer reasons recorded.
- **Device jobs** (`core/jobs.py`): pending|claimed|done|failed|cancelled|
  expired; claim-once; server waits on Condition.

## Stage 3 additions

- **LLM response contract** (`core/llm.py`): response|tool_call|
  clarification|approval_required, strictly parsed, malformed discarded.
- **Turn contract** (`TurnResult`): reply, status, mission/execution/device/
  tool/trace ids. Budgets: 3 tool calls, 120 s, repeat guard.
- **Memory write contract**: AUTO_SAVE|CANDIDATE|SESSION_ONLY|NEVER_STORE;
  corrections supersede with provenance.
- **Voice contract**: state machine transitions, barge-in, wake phrase
  exactly "Hey Zara", battery-gated listening.
