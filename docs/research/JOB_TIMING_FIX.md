# JOB TIMING FIX — Core proxy wait vs Android poll cadence

**Status: IMPLEMENTED. No redesign. No new dependencies.**

## Root cause

Core device-proxy wait (20 s, `core/device_tools.py::_def` default) was
shorter than the Android poll tick (30 s healthy, 60 s battery-throttled).
`approve → enqueue → wait(20 s)` routinely expired before the device's next
poll, so production jobs failed with `device job expired` while the device
never saw them (Rung-3 forensic evidence: 1 success, 5 expiries, zero
device-side claims). Test helpers hid this with a 0.05 s background drain.

The 20 s carried no semantic meaning (governor uses cost+risk, not timeout;
platform defaults are 30 s elsewhere). `CLAIM_TIMEOUT_S = 300.0` in
`core/jobs.py` was dead code.

## Invariant (pinned by `tests/test_job_timing.py::test_T0`)

> **Every device proxy wait MUST exceed the worst-case poll gap (60 s).**

`timeout_s = 90 s` gives a healthy device ≥2 poll opportunities and a
throttled device ≥1 plus execution margin. Do NOT lower any
`DEVICE_TOOL_DEFS` timeout below 61 s without also shortening the Android
tick.

## Changes

| File | Change |
|------|--------|
| `core/device_tools.py` | `_def` default `20.0 → 90.0` + invariant comment |
| `core/jobs.py` | `complete()` ignores late results after `expired` (no resurrection); new `cancel_by_execution()`; new `cancel_device_jobs()` (pending only, never claimed) |
| `core/execution.py` | Optional `jobs` handle; `cancel()` propagates to the queued job (wakes proxy waiter as `cancelled`) |
| `core/app.py` | Engine constructed with `jobs=`; `/v1/agent/revoke` sweeps pending device jobs + audits count; response gains `queued_jobs_cancelled` |
| `android/lib/main.dart` | `_pollJobs` moved before heartbeat/notifications in `_tick` (mid-tick failure can no longer starve polling) |
| `tests/test_job_timing.py` | NEW — 11 scaled-time regression tests (cadence poller, claim-latency fingerprint, offline/revoke/cancel/approval gates) |

## Deliberately NOT changed

- Poll interval stays 30 s/60 s (shortening = 2–3× always-on radio for a race that only matters post-enqueue).
- No push/FCM trigger (transport is mock; polling stays the reliable channel).
- No per-tool timeout splits (ALL device tools share the poll race, including reads).
- `CLAIM_TIMEOUT_S` left wired-out (claimed-lease expiry is UNSAFE: device may be actuating; Core fiction can't recall side effects).
- Approval, risk classes, revocation terminality, audit shapes: untouched.

## Emulator proof (2026-10-08, ZaraLab API 36, rebuilt APK)

- Guard flip re-verified: disabled → `unavailable`; enabled+bound → `available`.
- `POST /v1/exec gui.screen.inspect {max_elements:10}` → approve → **`succeeded`
  in 13 s wall time** (10 nodes, `package: dev.zara.zara_android`,
  `truncated: True` at the cap) — no test helper involved.
- Real device nodes fed into Core `match()`: `content_desc='Zara'` → MATCH
  (node 7); missing text → NOT_FOUND; repeated role → AMBIGUOUS.
- `UNEXPECTED_PACKAGE` deny path re-verified (system dialog owned screen).
- `/v1/agent/revoke?device_id=tmp-dev` → `{"revoked","queued_jobs_cancelled":0}`;
  emu-lab unaffected (trusted/online).
- Behavior note: app pause (Settings foreground) cancels the tick loop by
  design; jobs enqueued while paused wait for resume. 90 s window absorbs
  this; expiry stays honest when it fires.

## Known residual

- Worst case (throttled 60 s + a fully skipped round ≈ 120 s) can still exceed 90 s; expiry stays honest (`FAILED`, audited) rather than assumed.
- HIGH_RISK consent staleness: approval at T, actuation up to T+90 s. Accepted for lab pilot; device does fresh-root lookup per actuation. Re-confirmation after N seconds is future work.
- Longer waits hold ~2 threads per in-flight exec; offline devices now fail at 90 s instead of 20 s (bounded, never indefinite).
