# Privileged companion request contract (T3, lab only)

Authority: `content://dev.zara.privcompanion/control` via
`ContentResolver.call(method, arg, extras)`. Synchronous binder round-trip;
no polling, no callbacks, no retained state.

## Authentication

1. Manifest `android:permission="dev.zara.permission.SYSCONTROL"`
   (protectionLevel `signature`) on the provider — enforced by the OS
   before `call()` runs.
2. In-code `checkCallingPermission()` — same verdict, explicit.
3. Both APKs must be signed with the SAME lab keystore, or every call
   returns `applied:false` / `companion permission denied`. Signature
   mismatch is a deployment fact, never retried as an error.

## Operation allowlist (exact — anything else is refused)

| method | extras in | result extras out |
|--------|-----------|-------------------|
| `probe` | — | `applied:false`, `state:"ready"`, `version:"1"`, `operations:["probe","setTether"]` |
| `setTether` | `enabled:boolean` (required) | `applied`, `state` (`enabled`/`disabled`/`unknown`), `error` (`""` on applied) |

Unknown methods → `applied:false`, `error:"unknown method (refused)"`.
Missing/non-boolean `enabled` → provider treats absent as `false`
(disable); the main app always sends an explicit boolean and validates
before calling (job runner refuses non-boolean `enabled` pre-gateway).

## Verification semantics (shared with the main app)

- `applied:true` is returned ONLY after the bounded post-action poll
  observes the requested state (`getTetheredIfaces()` authoritative where
  available, transport heuristic fallback — documented in code).
- Otherwise `applied:false` with `state` as observed and `error` naming
  the cause (`TETHER_PRIVILEGED not effective`, `state unverified`, …).
- Outcomes the main app distinguishes: `applied` (verified change),
  `denied` (permission), `unsupported` (no companion / no API),
  `requires-user-action` (settings-UI fallback path, separate channel),
  `timed-out` (binder/verification bound exceeded), `unverified`
  (call returned but state unobserved). No `applied:true` without
  observation, ever.

## Bounds

- Verification poll: 12 × 500 ms (~6 s), then report what was observed.
- The main-app binder call inherits the job timeout (90 s proxy bound);
  provider work is CPU-trivial (one poll loop, no I/O beyond D-Bus).
- No cancellation token crosses the binder: the call is short and
  idempotent-safe (re-issuing `setTether` converges); engine-level cancel
  applies to the job, and a duplicate delivery re-verifies rather than
  re-toggling blindly.

## What is deliberately NOT here

Wi-Fi/Bluetooth/airplane toggles (covered by T0–T2 paths), any network
access, any UI, device-owner enrollment, or platform-key signing steps —
those stay in the main app or the lab setup docs. This companion does
exactly one privileged thing.
