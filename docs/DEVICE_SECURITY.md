# Device Security (extends SECURITY_MODEL.md)

- **Identity**: per-device keys (`secrets.token_urlsafe`, sha256-hashed
  server-side). Pairing codes one-time, 10-min TTL. Keys expire (365 d
  default), revocable (`/v1/agent/revoke` → 403 everywhere + device
  marked offline). Forged/unknown identity → 403 (tested).
- **Challenge-proof transport (Ed25519)**: since the challenge feature,
  devices SHOULD register an Ed25519 public key at claim time
  (`POST /v1/agent/claim` with `pubkey`, raw-key headers authorized).
  Thereafter every device call authenticates with *proofs*, not the
  raw key:
  1. `POST /v1/agent/challenge` → `{nonce, next_nonce}` (single-use
     nonces, TTL 300 s, ≤5 live per device, ≤1000 total in flight;
     `409` when no pubkey is bound — the raw-key path keeps working).
  2. The request carries `X-Device-Id` + `X-Device-Nonce` +
     `X-Device-Proof` where proof = base64(Ed25519 sign of the canonical
     message — v2 binds the exact body bytes:
     `"{METHOD} {path} {nonce} {sha256_hex(body)}"`, path without
     query string; the 3-field v1 form remains verified-served for
     older credentials during the rollout window).
  3. `next_nonce` is a chained spare: the NEXT call fetches its nonce
     proof-authed with the spare, so the raw key travels exactly ONCE
     per process lifetime (one bootstrap challenge fetch).
  - Server-side: nonces are single-use (replay → 401, tested over a
    real socket), TTL 300 s, tampered/wrong-key/revoked/expired proofs
    fail closed. Mixing proof + raw-key headers → 401 "ambiguous".
  - _Pubkey authorization:_ only the raw-key path may bind or replace
    the pubkey (`POST /v1/agent/pubkey`); proof-auth cannot change
    keys.
  - Clients: Linux agent (`device/linux/agent.py`, `.sk` file 0600 next
    to the key file) and Android (`android/lib/ed25519.dart` vendored
    RFC 8032 signer verified against the official test vectors; seed in
    Keystore-backed SecureStore). Both fall back to the proven raw-key
    transport when the core predates the feature (404) or the
    credential has no bound key (409), and only on auth-shaped errors
    — proofs are rejected *before* handler execution, so a fallback
    retry cannot double-execute a consequential action.
- **Credential persistence** (`ZARA_DEVICE_AUTH_DB`): per-device key
  hashes + Ed25519 pubkeys + pairing state survive a Core restart
  (`PersistentDeviceAuthStore`). A previously enrolled trusted device
  reconnects automatically after any Core/app restart — register +
  heartbeat with its stored key (0600 files on Linux, Keystore-backed
  secure storage on Android) — no re-pair, no re-prompt. Enforced on
  restart: revoked and expired credentials fail closed; consumed or
  stale pairing rows are purged at restore; concurrent claims race to
  exactly one winner. Only SHA-256 digests and public keys are ever
  written (the private Ed25519 seed never leaves the device); the DB
  (and WAL/SHM) is chmod 0600. Without the env var the store stays
  in-memory (tests / ephemeral servers).
- **Layers preserved**: OS perms → core policy → tool allowlists. Agents run
  unprivileged; no elevation path exists in this checkpoint.
- **Never trusted**: device-provided inputs are schema-validated twice
  (protocol parse + registry schemas); unknown tools from a device are
  reported, never executed; strict message parsing rejects unknown types.
- **Never logged**: device keys, signing seeds, pairing codes, file
  contents beyond metadata. Audit stores operational metadata only
  (tested: no `zara-dev-` material in audit).
- **Transport**: TLS is the production confidentiality layer — see
  docs/TRANSPORT_TLS.md. Core serves HTTPS directly
  (`zara_local.sh start --tls-cert/--tls-key`); `ZARA_REQUIRE_HTTPS=1`
  refuses non-loopback plain HTTP (tested); Linux-agent and Android
  clients refuse plain HTTP to non-local cores unless the override
  (`ZARA_ALLOW_INSECURE_TRANSPORT=1` / `allowInsecureTransport`) is set
  explicitly; clients fail closed on certificate validation. Signature
  proofs protect device identity, never wire confidentiality.
- **Blast radius**: filesystem tools scoped to `$HOME`+`/tmp` minus sensitive
  paths; shell is allowlist-only; `confirm`+ tools need human approval even
  when requested through dispatch.
