# Device Protocol v2.0 (`core/protocol.py`)

Transport: REST (Stage 2). WebSocket push reserved for later; agents poll
with adaptive interval + backoff — no aggressive polling.

Every message: `{v, type, device_id, payload}`, strictly validated
(`parse_message` rejects unknown types, version mismatch, bad shapes).
Core is authoritative; device parameters are re-validated by
policy/execution before anything runs.

## Messages

| Type | Direction | Purpose |
|---|---|---|
| `device_register` | agent→core | capabilities + software version |
| `device_auth` | agent→core | enroll/claim (pairing code → device key) |
| `heartbeat` | agent→core | battery/charging/network/online (30 s) |
| `capability_update` | agent→core | new capability set |
| `execution_request` | core→agent | job envelope (via poll response) |
| `execution_started` | agent→core | (implicit in claim; explicit in Stage 3) |
| `execution_progress` | agent→core | reserved for long jobs (Stage 3) |
| `execution_result` | agent→core | `{job_id, ok, result, error}` |
| `execution_failed` | agent→core | `ok:false` result variant |
| `execution_cancel` | core→agent | `POST /v1/agent/jobs/{id}/cancel` |
| `device_event` | agent→core | offline-queue sync + notifications |
| `device_disconnect` | agent→core | graceful shutdown |

## REST endpoints (all device endpoints need X-Device-Id/X-Device-Key)

- `POST /v1/agent/enroll` (dev token) → pairing code
- `POST /v1/agent/claim` → device key (once)
- `POST /v1/agent/register|heartbeat|capabilities`
- `POST /v1/agent/jobs/poll` → job or null (also refreshes presence)
- `POST /v1/agent/jobs/result`, `POST /v1/agent/jobs/{id}/cancel`
- `POST /v1/agent/events/sync`, `POST /v1/agent/disconnect`
- `POST /v1/agent/revoke` (dev token, operator)
- `POST /v1/dispatch` (dev token): core routes + executes, returns record.
