# Device Architecture (Zara Stage 2)

Devices are execution bodies for the ONE Zara, not independent assistants.

```
ZARA CORE (core/)
    ↓  Device Protocol (core/protocol.py — schema-validated messages)
Device Agent (device/linux/agent.py | android/ Flutter+Kotlin)
    ↓  OS APIs / allowlisted tools
Linux syscalls · Android SDK
```

## Ownership rule

One unambiguous owner per tool name. Core's local `system.info`
(`supported_devices=["any"]`) is the authority for that name; the Linux
agent does NOT implement `system.info` — device detail lives in
`system.battery` / `system.network` / `system.processes.read`.
Contract parity core↔device is enforced by
`test_contract_parity_core_vs_linux`.

## Pieces

- **Linux daemon** (`device/linux/`): enroll (pairing code) → claim (device
  key, 0600 file) → register (capabilities) → heartbeat (30 s) + job poll
  (5 s idle, backoff to 60 s on failure) → execute allowlisted tools →
  post result. Offline: capped local queue (200), sync on reconnect.
  Graceful shutdown sends `device_disconnect`.
- **Device auth** (`core/device_auth.py`): per-device keys (sha256 hashes
  server-side), one-time pairing codes (10 min TTL), expiry, revocation.
  Dev bearer token is local-dev only.
- **Job queue** (`core/jobs.py`): core blocks on a Condition (no busy poll);
  agents claim at most once; cancel/timeout supported.
- **Router** (`core/routing.py`): capability → online → policy → governor →
  prefer cloud/linux over android. The LLM never picks a device.
- **Android** (`android/`): Flutter UI + `zara/device` MethodChannel +
  Kotlin `DeviceBridge` (battery/network/permissions). Capability table in
  `lib/capabilities.dart`; unsupported returns `supported=false`, never faked.
  Wake word `voice.wake_word` ("Hey Zara") reserved, not implemented.

## Implemented / deferred

Implemented: registration, auth, capabilities, presence
(registered|online|offline|degraded|reconnecting), safe Linux tools,
routing, battery-aware defer, offline queue, audit.
Deferred to later work on the same Zara: voice, wake-word engine, LLM
reasoning, full RAG, browser agent, OpenCode tools, accessibility
automation, production Oracle deploy, WebSocket push (REST poll for now).
