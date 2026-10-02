# Android Status (truth source for the mobile body)

Labels: VERIFIED (unit/integration) · EMULATOR_VERIFIED · LIVE_SERVER_VERIFIED
(live Core over real HTTP) · PHYSICAL_VALIDATION_PENDING · DEFERRED.

## Lifecycle / identity

| Item | Status |
|---|---|
| enroll→claim→register→heartbeat→disconnect | LIVE_SERVER_VERIFIED (real HTTP + TestClient) |
| Real app pairing on emulator (`emu-body2` → online) | EMULATOR_VERIFIED 2026-10-03 |
| Keystore restore after force-stop (no re-pair) | EMULATOR_VERIFIED |
| Revoke → 403 → keys wiped → "Revoked — pair again" | EMULATOR_VERIFIED (screenshot) |
| Rotation kills old key; replay of pairing code refused | VERIFIED |
| Backend restart → 404 → re-register | VERIFIED |
| Logout clears creds | VERIFIED |

## Connection / jobs / approvals / notifications / push

| Item | Status |
|---|---|
| Bounded retry (5, then offline), battery-critical no auto-retry | VERIFIED |
| Bounded offline queue, heartbeat coalescing, stale-response drops | VERIFIED |
| End-to-end device job: Core dispatch → emulator `system.battery` → verified result (100%, charging) | EMULATOR_VERIFIED |
| Unknown tools refused (incl. shell/URL shapes); dup jobs refused | VERIFIED |
| Approval hold auto-notifies target device; device approves/denies own executions; foreign approval 403 | VERIFIED |
| Notification pull/ack scoping; secret scrub; dedup | VERIFIED |
| Push register/rotate/invalidate/dedup; payload has IDs only | VERIFIED |
| Real FCM delivery | DEFERRED (no credentials; mock transport + poll fallback) |

## Voice runtime

| Item | Status |
|---|---|
| Session states + limits 5/300 s/60 s, battery/offline gating | VERIFIED |
| "Hey Zara" exact; VAD-gated; battery pause; no authority | VERIFIED |
| Native capture/playback API paths (`audioSelfTest`: both true) | EMULATOR_VERIFIED |
| Mic signal / speaker audibility / DSP wake | PHYSICAL_VALIDATION_PENDING |
| Barge-in/cancel paths (mock audio, Magpie RPC in Stage 8) | VERIFIED (software) |

## Battery / network / UI / permissions

Battery levels follow Core thresholds; charging override; background
thrift advisory-only — VERIFIED. Network states + truthful offline UX —
VERIFIED. UI reflects connection/lifecycle/battery/voice/approval/error,
Wrap-fixed overflow, survives recreation via Core restore —
EMULATOR_VERIFIED. Permissions: INTERNET/ACCESS_NETWORK_STATE (install),
RECORD_AUDIO + POST_NOTIFICATIONS (runtime, denial = normal state) —
manifest + flow VERIFIED, grant-dialog UX PHYSICAL_VALIDATION_PENDING.

## Blocked / deferred

Physical Android device (`adb` had none): mic signal, audibility, DSP
wake, grant dialogs, FCM delivery, rotation sensor, radio behavior —
all PHYSICAL_VALIDATION_PENDING. Nothing in this file claims them.
