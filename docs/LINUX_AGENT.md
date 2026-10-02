# Linux Agent (`device/linux/agent.py`)

Execution body for the laptop. Registers with Zara Core, advertises ONLY
the capabilities it actually implements (`LINUX_CAPABILITIES`, derived from
`LINUX_TOOLS`), runs jobs, reports back.

## Run

```bash
# terminal 1 — core
ASSISTANT_TOKEN=dev-token python -m uvicorn core.app:app --port 8080
# terminal 2 — enroll (operator, get pairing code)
curl -H "Authorization: Bearer dev-token" -X POST localhost:8080/v1/agent/enroll \
  -H 'Content-Type: application/json' -d '{"device_id":"laptop-1","kind":"linux"}'
# terminal 3 — claim once, then run
python device/linux/agent.py --core http://127.0.0.1:8080 --device-id laptop-1 \
  --claim zara-pair-... --key-file /tmp/opencode/laptop-1.key
python device/linux/agent.py --core http://127.0.0.1:8080 --device-id laptop-1 \
  --key-file /tmp/opencode/laptop-1.key
```

## Tools (allowlisted, safe)

`system.battery` (sysfs), `system.network` (operstate), `system.processes.read`
(capped `ps`), `filesystem.list/read/exists/metadata` (scoped to `$HOME`+`/tmp`,
sensitive paths refused), `shell.safe_readonly` (binary allowlist
ls/df/free/uptime/uname/whoami/date/hostname/ps + `git status|log|diff|branch|rev-parse`,
no metacharacters, shell=False, capped output, `confirm` risk → needs approval).

## Explicitly NOT present

No arbitrary shell, no rm/dd/mkfs/shutdown/package-install/git-push,
no credential paths, no elevation. Dangerous requests die at three
layers: shell allowlist → core policy (deny-patterns + risk) → approval gate.

## Limitations

Single-agent process, REST poll transport, no sandboxing beyond scoping
(containers/seccomp later), probes best-effort (never crash the loop).
