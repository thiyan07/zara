# Device Security (extends SECURITY_MODEL.md)

- **Identity**: per-device keys (`secrets.token_urlsafe`, sha256-hashed
  server-side). Pairing codes one-time, 10-min TTL. Keys expire (365 d
  default), revocable (`/v1/agent/revoke` → 403 everywhere + device
  marked offline). Forged/unknown identity → 403 (tested).
- **Layers preserved**: OS perms → core policy → tool allowlists. Agents run
  unprivileged; no elevation path exists in this checkpoint.
- **Never trusted**: device-provided inputs are schema-validated twice
  (protocol parse + registry schemas); unknown tools from a device are
  reported, never executed; strict message parsing rejects unknown types.
- **Never logged**: device keys, pairing codes, file contents beyond
  metadata. Audit stores operational metadata only (tested: no `zara-dev-`
  material in audit).
- **Transport**: local HTTP for dev; production requires HTTPS/WSS + secure
  key storage (Android Keystore / server env) — Stage 3+ deploy work.
- **Blast radius**: filesystem tools scoped to `$HOME`+`/tmp` minus sensitive
  paths; shell is allowlist-only; `confirm`+ tools need human approval even
  when requested through dispatch.
