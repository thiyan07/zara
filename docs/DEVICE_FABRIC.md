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

---

# Stage 15 — real byte transfer + security (2026-10-04/05)

## Transfer model

`core/transfer.py`: `TransferEngine` over the Stage 14 `FabricRegistry`
(no parallel trust/policy truth). Core-mediated chunked relay: bytes
flow sender → Core staging → recipient; Core never executes content.

States (Stage 14 names kept, `expired` added):
`proposed` (=CREATED) → `authorized` → `running` (=TRANSFERRING) →
`verifying` (SHA-256 over reassembled bytes) → `succeeded` (=COMPLETED)
| `failed`; `cancelled`/`rejected`/`expired` terminal from the
documented transitions. "Authorized" and "completed" are never confused:
completion requires full receipt + hash match.

## Authorization chain (per request AND per chunk)

request → contract validation → sender+recipient trust (must both be
TRUSTED/TEMPORARILY_TRUSTED) → both presence online/degraded → policy
decide (`files.transfer`, risk `confirm`; hard-deny always wins) → live
sender-scoped temp grant mandatory (confirm risk) → governor (cost
scales with size) → concurrency cap → AUTHORIZED. Every chunk re-checks
both endpoints' trust: revocation mid-transfer cancels motion, never
silent continuation. Revoke path eagerly cancels via
`TransferEngine.revoke_device`.

## Storage model

`TransferStore`, root from `ZARA_TRANSFER_DIR` (default
`~/.local/share/zara/transfers`, mode 0700). Disk names derive ONLY from
validated transfer IDs (`<id>.part` → atomic rename → `<id>.bin`);
client filenames are metadata only, strictly sanitized (flat namespace,
no separators/traversal/hidden/shell chars/non-ASCII; rejected, never
rewritten). `O_NOFOLLOW`, realpath containment, 0600 files, no shell.

## Limits / integrity / motion

Max transfer 5 MiB; max chunk 64 KiB; filename ≤128; metadata ≤2 KiB;
4 concurrent per device; TTL 600 s; stall (no progress) 120 s → expired
(lazy sweep on every engine op + `POST /v1/fabric/xfer/sweep`).
SHA-256 declared at request, verified over staged bytes before
promotion; mismatch → FAILED + staging discarded. Strict chunk order
(seq must equal expected; duplicates/replays → 409). Recipient downloads
windowed reads, verifies locally, acks only on match (mismatch ack
recorded as failure, never success).

## Restart / offline semantics (honest, no resume claims)

- Completed transfers + bytes + hashes persist (sqlite + disk) and stay
  downloadable after re-pair (auth creds are memory-only by design).
- In-flight transfers NEVER survive restart: deterministic expiry,
  partials discarded, audited. No phantom success.
- Offline recipient: transfers wait (no false completion); app goes
  Offline, transfer buttons disabled. No resume protocol: retry mints a
  new transfer ID (RETRY_REQUIRED semantics).
- Revoked endpoints: chunks rejected at identity gate (401/403) +
  record cancelled eagerly.

## Endpoints

Operator: `POST /v1/fabric/xfer/request`, `GET /v1/fabric/xfer`,
`GET /v1/fabric/xfer/{id}`, `POST .../cancel`, `POST .../sweep`.
Device-auth (identity forced from headers):
`POST /v1/agent/xfer/request`, `POST .../{id}/chunk` (base64, bounded),
`GET .../pending`, `GET .../{id}/bytes?offset&length`,
`POST .../{id}/ack`, `POST .../{id}/cancel` (parties only).
Rejections map honestly: 400 malformed, 403 refused, 409 conflict, 404
unknown. Audit covers requested/authorized/rejected/completed/failed/
cancelled/expired/verified/mismatch; bytes and credentials never logged
(verified: no sensitive-field hits in 71 live audit rows).

## Physical validation (vivo V2338 Android 16 API 36 ↔ laptop-1)

Device: vivo V2338, Android 16, API 36, build PD2353NF_EX_A_16.2.16.0.W30,
1080x2408, Zara 0.1.0 default assistant, USB-powered 43–44%.

| Test | Result |
|---|---|
| laptop→vivo text 107 B (`340968d6…`) | PASS PHYSICAL_VERIFIED (Core hash + phone-local verify + ack) |
| vivo→laptop text + 64 KiB bin + 1 MiB bin | PASS PHYSICAL_VERIFIED (all laptop-local hash match + ack) |
| duplicate send (double-tap) → 6 distinct IDs, all verified | PASS (idempotent downloads, no confusion) |
| laptop→vivo 1 MiB (`19f294bb…`) incl. post-restart fetch | PASS PHYSICAL_VERIFIED |
| lifecycle 1 MiB through HOME-background + return | PASS (verified=true, deterministic) |
| app force-stop + relaunch → re-fetch, no phantom success | PASS |
| cancel mid-transfer → CANCELLED, staging gone, chunk→409 | PASS PHYSICAL_VERIFIED |
| stall 141 s → EXPIRED via sweep, staging cleaned | PASS PHYSICAL_VERIFIED |
| core restart ×4: 21 records + 9 .bin + 71 audit rows survive; in-flight → expired | PASS PHYSICAL_VERIFIED |
| revoke sender mid-upload → chunk 403 + record cancelled; post-revoke request 403 | PASS PHYSICAL_VERIFIED |
| offline (reverse removed): app Offline, buttons disabled, reconnect w/o re-pair | PASS PHYSICAL_VERIFIED |
| adversarial battery live (14 traversal names→400; oversize/bad-sha/self→400; unknown/cross-grant/expired-grant→403; wrong-seq→409; sender-read→403; overrun→400) | PASS (live Core, software devices) |
| governor: depleted scratch device → defer; healthy → proceed | PASS (live Core) |
| corruption (1 flipped byte) → FAILED, no file | PASS SOFTWARE_VERIFIED (pytest) |
| replay/duplicate-chunk/old-id → 409; terminal cancel → 409 | PASS SOFTWARE_VERIFIED |
| policy hard-deny (`.ssh/id_` recipient) → rejected | PASS SOFTWARE_VERIFIED |

## Hosted-LLM voice E2E (2026-10-05, PHYSICAL_VERIFIED)

Server restarted (host reboot wiped `/tmp` state) and relaunched with
`LLM_PROVIDER=openai-compatible`, `LLM_MODEL=openai/gpt-oss-20b`
(`/v1/ready` confirms `openai-compatible`; no hosted-LLM claim is made
for the earlier Stage 14 echo turn — that stays labeled echo).
Both devices re-paired (auth is memory-only by design) and online.

User spoke on the vivo V2338; screen evidence + server audit agree:
mic 160044 B peak −22.8 dBFS → STT **local-whisper** (honest note:
`STT_PROVIDER=nvidia` was not exported on this run, so the NVIDIA
Parakeet path from Stage 13 was not the transcriber this time) →
transcript "What is my mobile battery level?" → hosted LLM planned and
proposed `system.battery` → Core executed it on **laptop-1, succeeded**
(device job ok=True, `tools=1`, mission completed) → Piper TTS
(99840 B @22050 Hz) → AudioTrack playback exited played=true →
user-confirmed audibility. The turn ran twice (13:04:20, 13:04:46 UTC);
both show identical full chains in audit. Reply phrasing cited the
phone's 69% context while the audited tool execution was laptop-1 —
recorded as observed; the authorization/execution path is what this
stage validates.

Suite: 310 Python (254 Stage 14 intact + 56 transfer) + 59 Flutter
(54 + 5 transfer incl. pure-Dart SHA-256 NIST vectors) green,
`flutter analyze` clean, release APK 49.5 MB.
Phone additions: `lib/transfer.dart` (dependency-free SHA-256,
deterministic payloads, chunk windows), CoreClient xfer methods,
Transfer diagnostic UI (send/fetch/stop + grant field); transport
errors surface verbatim instead of sticking `xferBusy` (found + fixed
during offline testing).
Linux agent: `xfer_request/upload/pending/download` (local verify, ack
only on match; overrun clamp fixed during testing).
NOT_SUPPORTED / deferred: resume protocol (none — by design), FCM,
DSP wake, Appium (unavailable in this environment — adb/manual used;
byte/filesystem/crypto proof came from device + Core evidence, never UI).

