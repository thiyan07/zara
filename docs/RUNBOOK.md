# Zara Core — Local Runbook (`scripts/zara_local.sh`)

Launcher for local runs of Zara Core (`uvicorn core.app:app`). Copy-paste
commands below. Assumes you run from the repo root
(`/home/thiyan/projects/assistant`).

Tested contract: pure port/pidfile/redaction logic lives in
`scripts/runbook_lib.py` (stdlib only) and is covered by
`tests/test_runbook.py` **without spawning servers**. The `.sh` file
re-implements the same rules in bash; its behaviour is verified by the
self-test transcript at the bottom of this doc.

## 1. Start / status / stop

```bash
# start (default port 8080 if free, else first free 18080-18099 + notice)
./scripts/zara_local.sh start
./scripts/zara_local.sh status --port 8080
./scripts/zara_local.sh stop --port 8080

# explicit port
./scripts/zara_local.sh start --port 18081
./scripts/zara_local.sh status --port 18081
./scripts/zara_local.sh stop --port 18081

# file-backed DBs (one sqlite file per store under DIR; DIR is created)
./scripts/zara_local.sh start --port 18081 --db-dir /tmp/opencode/zdata

# extra env from a file (KEY=VALUE lines; values are never echoed)
./scripts/zara_local.sh start --port 18081 --env-file /path/to/zara.env
```

`start` waits up to ~30 s for `GET /v1/ready`, prints the ready JSON
(includes the `checks` object), and exits non-zero with a log tail on
failure. Without `--db-dir` the server is all-memory (shell env passes
through; unset `ZARA_*_DB` vars mean in-memory). With `--db-dir`, any
`ZARA_*_DB` var already set in your shell is respected (not overwritten).

Pidfile + log per port (unique per port, under `/tmp/opencode`):

| Port 18081 | Path |
|---|---|
| pidfile | `/tmp/opencode/zara_local.18081.pid` |
| app log | `/tmp/opencode/zara_local.18081.log` |

## 2. Isolated test mode (never touch :8080)

```bash
P=18090
./scripts/zara_local.sh start --port $P
curl -s 127.0.0.1:$P/v1/health
curl -s 127.0.0.1:$P/v1/ready
./scripts/zara_local.sh status --port $P
./scripts/zara_local.sh stop --port $P
test -f /tmp/opencode/zara_local.$P.pid && echo "PIDFILE STILL THERE" || echo "pidfile gone"
ss -ltn | grep -q ":$P\$" && echo "PORT STILL BUSY" || echo "port free"
```

## 3. Environment table

Env passes through from your current shell; `--env-file` adds more.
`start` prints which of these are set, with `*KEY*`/`*TOKEN*` values
shown as `***` (names visible, secrets never echoed).

| Variable | Default (when unset) | Meaning |
|---|---|---|
| `ASSISTANT_TOKEN` | `dev-token` | Operator bearer token (`Authorization: Bearer …`). Change in prod |
| `LLM_PROVIDER` | `echo` | `echo` (default) \| `scripted` \| `openai-compatible` |
| `LLM_BASE_URL` | _(unset)_ | Base URL for `openai-compatible` provider |
| `LLM_API_KEY` | _(unset)_ | API key for hosted provider (never logged) |
| `LLM_MODEL` | _(unset)_ | Model name for hosted provider |
| `ZARA_MEMORY_DB` | _(unset → in-memory)_ | SQLite file for memory store |
| `ZARA_MISSION_DB` | _(unset → in-memory)_ | SQLite file for missions |
| `ZARA_AUDIT_DB` | `:memory:` | SQLite file for audit log |
| `ZARA_FABRIC_DB` | _(unset → in-memory)_ | SQLite file for device-fabric store |
| `ZARA_SESSION_DB` | _(unset → in-memory)_ | SQLite file for session state |
| `ZARA_CHAT_DB` | _(unset → in-memory)_ | SQLite file for chat sessions |
| `ZARA_EMBEDDINGS` | `hash` | `hash` (default) \| `local` (bge model, falls back loudly) |

`--db-dir D` defaults the six `ZARA_*_DB` vars (only those still unset)
to `D/memory.db`, `D/missions.db`, `D/audit.db`, `D/fabric.db`,
`D/session.db`, `D/chat.db`. Voice/STT/TTS vars (`STT_PROVIDER`,
`TTS_PROVIDER`, …) are optional pass-through; see `.env.example`.

Endpoint shapes:

```bash
curl -s 127.0.0.1:$P/v1/health  # {"ok":true,"version":"0.1.0"}
curl -s 127.0.0.1:$P/v1/ready   # {"ready":bool,"checks":{"registry":…,"memory_write":…,"event_bus":…,"provider":…},"governor_v2":…,"governor_shadow":…}
```

## 4. Recovery

**Port busy.** `start --port P` on an occupied port prints the occupant's
PID/command (`ss -ltnp` line) and exits 2 — it never signals or kills the
occupant. Pick another port or stop YOUR server on that port:

```bash
./scripts/zara_local.sh start --port 18081   # exit 2 + occupant line if busy
ss -ltnp | grep ':18081'                     # who holds it (read-only)
```

Default-port variant: if `:8080` is busy, `start` (no `--port`) prints an
explicit NOTICE and uses the first free port in `18080-18099`.

**Stale pidfile.** If the pidfile holds a dead/non-numeric PID, `stop` and
`start` report `stale pidfile … removing` and clean it up (exit 0 for
`stop`). If the PID is alive but its `/proc/<pid>/cmdline` lacks the
`zara_local` marker, `stop` REFUSES to signal it (exit 2) — protects
against PID reuse. Then investigate manually:

```bash
cat /tmp/opencode/zara_local.18081.pid
tr '\0' ' ' </proc/<pid>/cmdline; echo
rm /tmp/opencode/zara_local.18081.pid   # only after confirming it is stale
```

**Unready / crash on boot.** `start` prints `readiness FAILED` + the last
40 log lines and exits 1 (it also removes its pidfile). Full log:

```bash
tail -n 100 /tmp/opencode/zara_local.18081.log
```

Common causes: repo-root import error (`ModuleNotFoundError: core` — the
script `cd`s to the repo root itself, so this means a broken checkout),
unreachable `ZARA_*_DB` directory (app falls back to in-memory with a
notice; not fatal), port raced between check and bind (uvicorn error in
the log — just retry).

**App logs location:** `/tmp/opencode/zara_local.<port>.log` (one per
port, see table in §1).

## 5. Agent one-liners (core must be up; use your port as $P)

Linux agent (`docs/LINUX_AGENT.md`):

```bash
P=18081
# terminal 1 — core (via this runbook)
./scripts/zara_local.sh start --port $P
# terminal 2 — enroll (operator, returns one-time pairing code)
curl -H "Authorization: Bearer dev-token" -X POST 127.0.0.1:$P/v1/agent/enroll \
  -H 'Content-Type: application/json' -d '{"device_id":"laptop-1","kind":"linux"}'
# terminal 3 — claim once, then run (key persisted to key file)
python device/linux/agent.py --core http://127.0.0.1:$P --device-id laptop-1 \
  --claim zara-pair-... --key-file /tmp/opencode/laptop-1.key
python device/linux/agent.py --core http://127.0.0.1:$P --device-id laptop-1 \
  --key-file /tmp/opencode/laptop-1.key
```

Android agent (Flutter app under `android/`; flows per
`docs/ANDROID_STATUS.md`, build steps per `docs/ANDROID_TEST_REPORT.md`):

```bash
cd android && flutter test && flutter analyze
flutter build apk --debug
# substitute your serial from `adb devices` for emulator-XXXX below
adb -s emulator-XXXX install -r build/app/outputs/flutter-apk/app-debug.apk
# point the app at http://<host>:$P (emulator: 10.0.2.2:$P), then enroll→claim→register in-app
```

## 6. Self-test transcript (2026-10-10, isolated ports, all-memory + --db-dir)

`ss` confirmed `:8080` free before/after; it was never bound, signalled, or
probed. PID 59630 does not exist on this machine (`ps -p 59630` empty).

```
$ ./scripts/zara_local.sh start --port 18085        # ASSISTANT_TOKEN=dev-token LLM_PROVIDER=echo
zara_local: all-memory mode (no --db-dir; shell env passes through, unset DB vars = in-memory)
zara_local: env (values redacted):
  ASSISTANT_TOKEN=***  LLM_PROVIDER=echo  LLM_BASE_URL=<unset>  LLM_API_KEY=***
  LLM_MODEL=<unset>  ZARA_MEMORY_DB=<unset>  ZARA_MISSION_DB=<unset>  ZARA_AUDIT_DB=<unset>
  ZARA_FABRIC_DB=<unset>  ZARA_SESSION_DB=<unset>  ZARA_CHAT_DB=<unset>  ZARA_EMBEDDINGS=<unset>
zara_local: port=18085 (fallback=no) log=/tmp/opencode/zara_local.18085.log pidfile=/tmp/opencode/zara_local.18085.pid
zara_local: launched pid 24569; waiting for /v1/ready (<= 30s)
zara_local: READY on port 18085
{"ready":true,"checks":{"registry":true,"memory_write":true,"event_bus":true,"provider":"echo"},"governor_v2":false,"governor_shadow":false}
START_EXIT=0
$ tr '\0' ' ' </proc/24569/cmdline; echo
python3 -c # zara_local managed server from uvicorn import run run("core.app:app", host="127.0.0.1", port=18085)
$ curl -s 127.0.0.1:18085/v1/health
{"ok":true,"version":"0.1.0"}
$ ./scripts/zara_local.sh status --port 18085
port=18085  pidfile=/tmp/opencode/zara_local.18085.pid pid=24569
pid_alive=yes  port_state=listening
ready_json={"ready":true,"checks":{"registry":true,"memory_write":true,"event_bus":true,"provider":"echo"},"governor_v2":false,"governor_shadow":false}
STATUS_EXIT=0
$ python3 -m http.server 18086 --bind 127.0.0.1 &   # foreign occupant (pid 24625)
$ ./scripts/zara_local.sh start --port 18086
zara_local: port 18086 is occupied; refusing to disturb it (exit 2)
occupant: LISTEN … 127.0.0.1:18086 … users:(("python3",pid=24625,fd=3))
REFUSAL_EXIT=2
$ ./scripts/zara_local.sh start --port 18087 --db-dir /tmp/opencode/zdata-test
zara_local: file-backed DBs under /tmp/opencode/zdata-test
zara_local: READY on port 18087
{"ready":true,"checks":{"registry":true,"memory_write":true,"event_bus":true,"provider":"echo"},"governor_v2":false,"governor_shadow":false}
$ ls /tmp/opencode/zdata-test
audit.db chat.db fabric.db memory.db memory.db-shm memory.db-wal missions.db missions.db-shm missions.db-wal session.db
$ ./scripts/zara_local.sh stop --port 18087
zara_local: stopping pid 24721 (port 18087)
zara_local: stopped; pidfile removed
STOP87=0
$ ./scripts/zara_local.sh stop --port 18085
zara_local: stopping pid 24569 (port 18085)
zara_local: stopped; pidfile removed
STOP85=0
$ test -f /tmp/opencode/zara_local.18085.pid || echo "pidfile gone"; ss -ltn | grep -E ':(18085|18087|8080)' || echo "all test ports free; 8080 untouched"
pidfile gone
all test ports free; 8080 untouched
```

Notes: one `start` attempt failed first with `No module named uvicorn`
because the initial draft renamed python's argv[0] via `exec -a`
(breaks interpreter path resolution); fixed by carrying the
`zara_local` marker in the `python3 -c` program text instead (cmdline
above proves the marker survived). `pytest tests/test_runbook.py`: 15
passed. `df`: /tmp 114M used before and after (noise-level); test DB dir
removed. New files only: `scripts/zara_local.sh`,
`scripts/runbook_lib.py`, `docs/RUNBOOK.md`, `tests/test_runbook.py` —
nothing committed.

## 7. Approval/polling race fixes + live laptop verification (2026-10-11)

Races fixed in `core/execution.py` (`_granting` admission set, locked
compare-and-set grant, serialized idempotent submit), `core/device_tools.py`
(post-enqueue cancel check), `core/jobs.py` (`complete_once()` atomic
first-delivery flag), `core/app.py` (required-capability revalidation for
direct submits; single bus/audit emission), `android/lib/main.dart`
(post-approval immediate poll nudge, guarded + best-effort).
Regression suite: `tests/test_grant_race.py` (12 tests, Barrier-based —
G1/G3/G4/G6b fail on pre-fix code, pass on fixed code).

Live laptop run (isolated port 18202, `--env-file .env` for the real
provider; agent = real `device/linux/agent.py` as `laptop-1`, 5s poll):

```
bash scripts/zara_local.sh start --port 18202 --env-file .env
# enroll (operator) -> pairing code -> claim once -> run agent
curl -H "Authorization: Bearer $ASSISTANT_TOKEN" -X POST 127.0.0.1:18202/v1/agent/enroll \
  -H 'Content-Type: application/json' -d '{"device_id":"laptop-1","kind":"linux"}'
python device/linux/agent.py --core http://127.0.0.1:18202 --device-id laptop-1 --claim <code>
python device/linux/agent.py --core http://127.0.0.1:18202 --device-id laptop-1 &
# submit CONFIRM task -> 202 parked -> approve -> agent claims -> succeeded+verified
# duplicate approve -> 409 "not awaiting approval"
# /v1/talk exact-reply + provider-check prove the genuine hosted path
bash scripts/zara_local.sh stop --port 18202   # + kill agent by verified PID
```

Observed: `shell.safe_readonly uname -a` → `succeeded`, verified, genuine
kernel output in ~4s. Pairing codes destroyed after claim; device key at
`~/.config/zara/laptop-1.key` (mode 600, outside the repo).

## 8. Manual Android lifecycle (physical device NOT TESTED — no device
## connected, no UI harness; follow these steps on hardware)

1. `adb devices` shows the phone; Zara app installed (`pm list packages |
   grep zara`).
2. Start Core on the laptop LAN IP (not 127.0.0.1): record the port.
3. In-app: point at `http://<laptop-ip>:$P`, enroll → claim → register;
   confirm `GET /v1/devices` shows the phone `online`.
4. Submit a harmless CONFIRM task (e.g. `system.battery` for the phone,
   or a laptop-dispatched `shell.safe_readonly uname -a`).
5. Phone shows the approval card; approve exactly that action (do not
   approve anything else; never approve HIGH_RISK blindly).
6. Laptop agent claims within one poll interval; Core records terminal
   state; phone displays the verified result on next poll/refresh.
7. Re-tap approve and re-send the result: expect 409 / idempotent `{"ok":
   true}` with no second execution (server guards; see G6b/G6c).
8. Do not bypass approval, enable denied capabilities, run destructive
   commands, wipe app data, or disturb port 8080.
