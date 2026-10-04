# Device Fabric (Stage 14) — foundation

One Zara, many bodies. The Fabric is an **adapter** over the existing
stores — it creates no parallel registry, auth, policy, or execution:

| Fabric concept | Source of truth |
|---|---|
| presence, heartbeats | `DeviceManager` |
| trust root | `DeviceAuthStore` (enroll/claim/verify/revoke) |
| tool schemas, risk | `ToolRegistry` |
| authorization | `PolicyEngine` (sole authority) |
| resources | `ResourceGovernor` (sole authority) |
| execution, verification | `ExecutionEngine` + device jobs |
| persistence | shared SQLite `Database` (`ZARA_FABRIC_DB`; empty = in-memory) |

Core rule preserved: **capability existence != authorization**.
A device advertising `camera.capture` means the capability EXISTS; use
requires trust + presence + policy + governor + OS permission, checked
at route/execute time. The LLM may propose; only this layer + Core decide.

## Trust (derived, never self-declared)

`revoked` (tombstone or auth revoked) → `REVOKED` · operator
`restricted` + paired → `RESTRICTED` · paired + live grant →
`TEMPORARILY_TRUSTED` · paired → `TRUSTED` · enrolled, code outstanding →
`PAIRING` · discovered only → `DISCOVERED` · else `UNKNOWN`.

Only `TRUSTED` / `TEMPORARILY_TRUSTED` execute. `RESTRICTED` executes a
capability only under a live, scoped temporary grant naming that
capability at or above its risk. `REVOKED` is terminal except via
explicit re-pair: a **fresh** enroll→claim lifts the tombstone; old keys
stay dead. Revocation tombstones persist across Core restart
(`fabric_revocations`).

## Endpoints (all operator-auth unless noted)

- `GET /v1/fabric/devices`, `GET /v1/fabric/devices/{id}` (detail +
  capability records + grants + prefs)
- `POST /v1/fabric/discover` (operator metadata; never identity/trust)
- `POST /v1/fabric/devices/{id}/restrict|unrestrict`
- `POST /v1/fabric/route` (dry-run: route|defer|deny + reasons;
  safe-risk pins may substitute a healthy device, never silently for
  riskier ones)
- `POST /v1/fabric/grants`, `GET /v1/fabric/grants`,
  `POST /v1/fabric/grants/{id}/revoke`
- `POST /v1/fabric/transfers`,
  `POST /v1/fabric/transfers/{id}/transition?state=…` (protocol
  foundation; no bytes move yet)
- `POST /v1/fabric/prefs` (advisory only, never authority)
- `POST /v1/agent/capabilities/describe` (**device-auth**; identity
  forced from auth, records validated, unknown names stored unmapped —
  never executable by themselves)

## Validation (honest labels)

- 51 new tests in `tests/test_fabric.py` (UNIT + INTEGRATION via real
  FastAPI stack): trust machine, identity spoof/rename, capability
  validation, presence/stale, routing tie-break/substitution/governor,
  grants lifecycle/scope/expiry, execution gating, transfers, prefs,
  persistence round-trip + corrupt-row tolerance, security (replay,
  cross-device grant confusion, hard-deny intact, malicious metadata
  inert). Full suite: 254 Python + 54 Flutter green, `flutter analyze`
  clean, release APK builds.
- PHYSICAL (2026-10-04, fabric server + `ZARA_FABRIC_DB` sqlite):
  real `laptop-1` Linux agent paired/online; real `vivo-real`
  re-paired over `adb reverse` and online; `system.battery` routed to
  `laptop-1` and executed for real (80.0%, not charging, verified);
  `scratch-1` revocation tombstone survived Core restart (trust=revoked
  after restart); phone voice self-test showed real NVIDIA Parakeet STT
  ("hey zara what is my battery level", mic −28.0 dBFS) with speaker
  playback started and user-confirmed heard + stop. Note: this run's
  LLM provider was `echo` (no `LLM_PROVIDER=openai-compatible` in server
  env), so the spoken reply was the echo fallback — no hosted-LLM claim.
- PENDING: camera/screen/location/bluetooth remain by-design
  non-capabilities; FCM delivery; DSP wake; transfers move no bytes yet
  (Stage 15+).
