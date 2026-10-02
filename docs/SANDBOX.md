# Sandbox (`core/sandbox.py`)

Application-level execution isolation for OpenCode subprocess tools, MCP
stdio servers (spawn path), and future external tools. Honest level:
**application restrictions, NOT container/seccomp/VM isolation** (no Docker
on this machine; nothing here claims otherwise).

## Policy (`SandboxPolicy`)

- `roots`: absolute-path args and cwd must stay inside (relative args are
  the caller's validated responsibility); sensitive paths (`.ssh`, gnupg,
  credentials, keys) refused.
- `allowed_binaries`: bare names resolved via `shutil.which` over fixed
  dirs (`/usr/bin`, `/bin`, `~/.local/bin`) — no ambient PATH hijack.
- `timeout_s`, `max_output_bytes` (default 8 KB, tail-kept), stdin ≤200 KB.
- `scrub_env()`: minimal env (`PATH/HOME/NO_COLOR/LANG`) + explicit
  non-secret keeps; secret names never cross.
- `TempWorkspace`: self-cleaning temp dirs.

## Applied to

- `code.test` / `code.diff` / `code.apply_patch` run through `sandbox.run`
  (verified by the existing allowlist tests + new sandbox tests).
- `code.session` runner resolves the binary the same way with scrubbed env.
- Browser page work runs on a dedicated worker thread (thread-affinity
  requirement), still schema/policy-gated; page content stays untrusted.
- MCP stdio spawn: no shell, scrubbed base env, explicit argv.

## Limits (not hidden)

No CPU cgroups, no network namespace (`allow_network` is documentary until
netns support exists), no seccomp. A malicious binary that is explicitly
approved and allowlisted could still use CPU/network within its timeout —
approval remains the backstop, as everywhere in Zara.
