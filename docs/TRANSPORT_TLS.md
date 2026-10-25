# Transport TLS (production hardening) — Zara Core / agents

Signature proofs (docs/DEVICE_SECURITY.md) protect *device identity and
replay-resistance*, never wire confidentiality. They bind "METHOD path
nonce sha256(body)" — they do not hide anything, and they do not stop a
local network observer from reading traffic. Confidentiality and
server-authentication are TLS's job. This document is the supported TLS
model; the insecure configuration stays disabled by default.

## Server capability (Core)

`scripts/zara_local.sh start --tls-cert CERT.pem --tls-key KEY.pem`
serves HTTPS directly (uvicorn ssl_certfile/ssl_keyfile). Optional
`--tls-ca CA.pem` makes the readiness probe verify against that CA;
without it the readiness probe is a pinned-loopback LIVENESS check only
(never a client model — real clients must always verify).

When the environment `ZARA_REQUIRE_HTTPS=1` is set (e.g. in the
`--env-file`), Core's `strict_transport` middleware refuses any request
that is not provably HTTPS with a loopback-source exemption for
same-host development:

- direct HTTPS, or `x-forwarded-proto: https` from a local TLS
  terminating proxy → allowed;
- plain HTTP from a loopback source (`127.0.0.1`, `::1`, `localhost`)
  → allowed ONLY while the proxy has not claimed a non-https protocol
  (a proxy header disagreeing with the connection is a downgrade
  attempt and is refused);
- plain HTTP with a non-loopback source address → refused `403` with a
  pointer to this file.

Tested: `tests/test_transport_tls.py` — self-signed serving, default
client verification failing closed, wrong-trust-anchor rejection,
verified connect with staged trust, loopback exemption, non-loopback
source refusal (real LAN interface address), forwarded-proto downgrade
denial.

## Client capability

- Linux agent (`device/linux/agent.py`): refuses to construct against
  `http://` to any host that is not `localhost` / `127.0.0.1` / `::1` /
  `10.0.2.2` (Android-emulator NAT alias for the host loopback). The
  explicit override `ZARA_ALLOW_INSECURE_TRANSPORT=1` is for a trusted
  flat LAN only and stamps `[INSECURE-TRANSPORT-ALLOWED]` into the
  agent's reported `software_version` so the choice is visible in
  diagnostics. HTTPS URLs are validated by urllib's default context
  (system CA store + hostname) — no insecure-comparison or
  hostname-differential code exists in Zara.
- Android (`android/lib/core_client.dart`): same rule; the explicit
  opt-out is the `allowInsecureTransport` flag (default `false`).
  Dart `HttpClient`/OkHttp validate certificates and hostnames in the
  default configuration; Zara adds no bypass.
- Both clients fail CLOSED on TLS failure: a failed handshake surfaces
  as a connection error; no code path silently retries plain HTTP.

## Certificate & trust model (deployment)

Local LAN (no public exposure): create a single-purpose CA (offline
key), issue a leaf with SAN `zara.local` (or the host's LAN name), give
phones/laptops the CA cert (Android: Settings → install CA证书
*as a user cert is sufficient for user apps*; linux: distro trust
store). This is a user/route decision — Zara ships a **server passthrough
only**: point `--tls-cert/--tls-key` at whatever cert you produce, e.g.

    # create local CA + leaf (one-time, any standard tooling)
    # then:
    ./scripts/zara_local.sh start --port 8443 --tls-cert leaf.pem \
        --tls-key leaf.key --tls-ca ca.pem

Remote access: use a publicly-trusted certificate (Let's Encrypt or a
cloud CA) on the host — Zara requires no special handling as long as
the SAN matches the hostname clients use. **Zara deliberately does not
open ports, set up tunnels or port-forwarding by itself**: exposing the
service to the internet remains an explicit user action (docs/SECURITY_MODEL.md).

## Renewal & failure recovery

- Renewal = replace cert/key files and restart Core (`stop` + `start`);
  enrolled devices survive restarts (keys + pubkeys in the persistent
  store) and re-handshake automatically — a cert change needs NO
  re-pairing, only trust re-staging if the *CA* changed.
- If the certificate expires: clients fail closed with a validation
  error and show reconnect-not-possible; devices enter their existing
  offline queue/backoff. Renew, restart, clients reconnect — no data
  loss (jobs keep server-side state).
- CA rotation: stage the NEW CA on clients first (Android user-certs
  and system trust both possible; linux: update store), then restart
  Core with the new leaf. Device enrollment data is unaffected.

## What is deliberately NOT implemented

- ACME/Let's Encrypt automation inside Zara (needs a domain, public
  reachability, and an operator decision to expose the service).
- mTLS device certificates (challenge-response proofs give the same
  identity property without pinning client certs; mTLS can be added
  behind the same uvicorn flags if a deployment wants it: uvicorn
  supports ssl_ca_certs/ssl_cert_reqs).
- Any code path that disables certificate verification in shipped
  clients. There is none; do not add one.
