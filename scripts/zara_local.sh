#!/usr/bin/env bash
# zara_local.sh — local launcher for Zara Core (uvicorn core.app:app).
# Commands: start | stop | status
#   start [--port P] [--db-dir D] [--env-file F]
#         [--tls-cert CERT --tls-key KEY [--tls-ca CA]]
#         TLS: serves https when cert+key given; --tls-ca makes the
#         readiness probe verify against that CA (OMIT: pinned-loopback
#         liveness probe only — real clients MUST verify; see
#         docs/TRANSPORT_TLS.md). ZARA_REQUIRE_HTTPS=1 additionally
#         makes Core refuse any non-loopback plain-HTTP request.
#   stop  [--port P]
#   status [--port P]
#
# Rules (see docs/RUNBOOK.md; tested contract in scripts/runbook_lib.py):
# - Default port 8080 ONLY if free; else first free port in 18080-18099
#   with an explicit notice. An explicitly requested busy port is refused
#   (exit 2) — existing occupants are NEVER signalled or killed.
# - stop reads the pidfile, verifies /proc/<pid>/cmdline contains
#   "zara_local" before SIGTERM, waits bounded, never pattern-kills.
# - Secrets are never echoed: *KEY*/*TOKEN* values print as ***.
set -uo pipefail

PID_DIR="/tmp/opencode"
DEFAULT_PORT=8080
FALLBACK_LO=18080
FALLBACK_HI=18099
READY_TIMEOUT_S=30
STOP_TIMEOUT_S=10

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "zara_local: $*" >&2; exit "${2:-1}"; }

need_bin() {
  command -v "$1" >/dev/null 2>&1 || { echo "zara_local: missing required binary: $1" >&2; exit 1; }
}

pidfile_for() { printf '%s/zara_local.%s.pid' "$PID_DIR" "$1"; }
logfile_for() { printf '%s/zara_local.%s.log' "$PID_DIR" "$1"; }

port_in_use() {
  # $1 = port. True (0) when something LISTENs on it.
  ss -ltnH 2>/dev/null | awk '{print $4}' | grep -q ":$1\$"
}

occupant_info() {
  # $1 = port. Prints the ss LISTEN line incl. PID/cmd, or "unknown".
  local line
  line="$(ss -ltnp 2>/dev/null | grep ":$1\\b" | head -n 1 || true)"
  if [ -n "$line" ]; then
    echo "$line"
  else
    echo "(ss reports no detail; port $1 is in use)"
  fi
}

print_env_contract() {
  # Show which contract vars are set; redact secret VALUES. Names only otherwise.
  local var val upper
  for var in ASSISTANT_TOKEN LLM_PROVIDER LLM_BASE_URL LLM_API_KEY LLM_MODEL \
             ZARA_MEMORY_DB ZARA_MISSION_DB ZARA_AUDIT_DB ZARA_FABRIC_DB \
             ZARA_SESSION_DB ZARA_CHAT_DB ZARA_DELEGATION_DB ZARA_DEVICE_AUTH_DB \
             ZARA_EMBEDDINGS; do
    val="${!var:-<unset>}"
    upper="$(echo "$var" | tr '[:lower:]' '[:upper:]')"
    case "$upper" in
      *KEY*|*TOKEN*) val="***" ;;
    esac
    # Never print a real secret value, even if it leaked into val.
    if [ "$val" != "<unset>" ] && [ "$val" != "***" ]; then
      case "$upper" in
        *KEY*|*TOKEN*) val="***" ;;
      esac
    fi
    echo "  $var=$val"
  done
}

load_env_file() {
  # $1 = path. Exports KEY=VALUE lines; values never echoed.
  local f="$1"
  [ -f "$f" ] || { echo "zara_local: --env-file not found: $f" >&2; exit 1; }
  set -a
  # shellcheck disable=SC1090
  . "$f"
  set +a
}

resolve_port_for_status_stop() {
  # $1 = requested port or "". Echo port. Single-pidfile auto-pick when omitted.
  local req="$1" matches n
  if [ -n "$req" ]; then echo "$req"; return 0; fi
  if [ -n "${ZARA_PORT:-}" ]; then echo "$ZARA_PORT"; return 0; fi
  matches="$(ls "$PID_DIR"/zara_local.*.pid 2>/dev/null || true)"
  n="$(echo "$matches" | grep -c . || true)"
  if [ "$n" -eq 1 ]; then
    basename "$matches" | sed 's/^zara_local\.//; s/\.pid$//'
    return 0
  fi
  echo "zara_local: --port required (found $n pidfiles)" >&2
  return 1
}

cmd_stop() {
  local req_port="${1:-}" port pidfile cmdline
  port="$(resolve_port_for_status_stop "$req_port")" || exit 1
  pidfile="$(pidfile_for "$port")"
  [ -f "$pidfile" ] || { echo "zara_local: no pidfile for port $port ($pidfile)"; if port_in_use "$port"; then echo "port_state=listening (foreign occupant — not ours, not signalling)"; else echo "port_state=closed"; fi; exit 1; }
  pid="$(tr -d '[:space:]' <"$pidfile" || true)"
  case "$pid" in
    ''|*[!0-9]*|0|1)
      echo "zara_local: stale pidfile for port $port (content '${pid:-empty}'); removing"
      rm -f "$pidfile"
      exit 0
      ;;
  esac
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "zara_local: stale pidfile for port $port (pid $pid not alive); removing"
    rm -f "$pidfile"
    exit 0
  fi
  cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
  case "$cmdline" in
    *zara_local*)
      ;;
    *)
      echo "zara_local: REFUSING to signal pid $pid (cmdline '${cmdline:-unknown}' lacks zara_local marker)" >&2
      exit 2
      ;;
  esac
  echo "zara_local: stopping pid $pid (port $port)"
  kill -TERM "$pid" 2>/dev/null || true
  local waited=0
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$STOP_TIMEOUT_S" ]; do
    sleep 1; waited=$((waited + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "zara_local: pid $pid still alive after ${STOP_TIMEOUT_S}s; NOT escalating (no SIGKILL by policy). Inspect: tail $(logfile_for "$port")" >&2
    exit 1
  fi
  rm -f "$pidfile"
  echo "zara_local: stopped; pidfile removed"
}

cmd_status() {
  local req_port="${1:-}" port pidfile pid alive port_state ready rc=0
  port="$(resolve_port_for_status_stop "$req_port")" || exit 1
  pidfile="$(pidfile_for "$port")"
  echo "port=$port"
  if [ -f "$pidfile" ]; then
    pid="$(tr -d '[:space:]' <"$pidfile" || true)"
    echo "pidfile=$pidfile pid=${pid:-<corrupt>}"
  else
    pid=""; echo "pidfile=$pidfile (absent)"
  fi
  alive="no"
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then alive="yes"; fi
  echo "pid_alive=$alive"
  port_state="closed"
  port_in_use "$port" && port_state="listening"
  echo "port_state=$port_state"
  ready="$(curl -sf --max-time 5 "http://127.0.0.1:$port/v1/ready" 2>/dev/null || true)"
  if [ -n "$ready" ]; then
    echo "ready_json=$ready"
  else
    echo "ready_json=<no response>"
    rc=1
  fi
  [ "$alive" = "no" ] && rc=1
  return $rc
}

cmd_start() {
  local req_port="" db_dir="" env_file="" port fell_back="no" pidfile logfile
  local tls_cert="" tls_key="" tls_ca=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --port) req_port="${2:-}"; shift 2 ;;
      --port=*) req_port="${1#--port=}"; shift ;;
      --db-dir) db_dir="${2:-}"; shift 2 ;;
      --db-dir=*) db_dir="${1#--db-dir=}"; shift ;;
      --env-file) env_file="${2:-}"; shift 2 ;;
      --env-file=*) env_file="${1#--env-file=}"; shift ;;
      --tls-cert) tls_cert="${2:-}"; shift 2 ;;
      --tls-cert=*) tls_cert="${1#--tls-cert=}"; shift ;;
      --tls-key) tls_key="${2:-}"; shift 2 ;;
      --tls-key=*) tls_key="${1#--tls-key=}"; shift ;;
      --tls-ca) tls_ca="${2:-}"; shift 2 ;;
      --tls-ca=*) tls_ca="${1#--tls-ca=}"; shift ;;
      *) echo "zara_local: unknown start flag: $1" >&2; exit 1 ;;
    esac
  done
  if { [ -n "$tls_cert" ] || [ -n "$tls_key" ]; } \
     && { [ -z "$tls_cert" ] || [ -z "$tls_key" ]; }; then
    echo "zara_local: --tls-cert and --tls-key must be given together" >&2
    exit 1
  fi
  if [ -n "$tls_cert" ]; then
    [ -r "$tls_cert" ] || { echo "zara_local: cert not readable: $tls_cert" >&2; exit 1; }
    [ -r "$tls_key" ] || { echo "zara_local: key not readable: $tls_key" >&2; exit 1; }
  fi
  [ -z "$req_port" ] && req_port="${ZARA_PORT:-}"
  [ -n "$env_file" ] && load_env_file "$env_file"

  if [ -n "$req_port" ]; then
    if port_in_use "$req_port"; then
      echo "zara_local: port $req_port is occupied; refusing to disturb it (exit 2)" >&2
      echo "occupant: $(occupant_info "$req_port")" >&2
      exit 2
    fi
    port="$req_port"
  else
    if ! port_in_use "$DEFAULT_PORT"; then
      port="$DEFAULT_PORT"
    else
      port=""
      candidate="$FALLBACK_LO"
      while [ "$candidate" -le "$FALLBACK_HI" ]; do
        if ! port_in_use "$candidate"; then port="$candidate"; break; fi
        candidate=$((candidate + 1))
      done
      if [ -z "$port" ]; then
        echo "zara_local: default port $DEFAULT_PORT busy ($(occupant_info "$DEFAULT_PORT")) and no free port in $FALLBACK_LO-$FALLBACK_HI" >&2
        exit 2
      fi
      fell_back="yes"
      echo "zara_local: NOTICE — default port $DEFAULT_PORT is occupied; using fallback port $port"
      echo "occupant of $DEFAULT_PORT: $(occupant_info "$DEFAULT_PORT")"
    fi
  fi

  mkdir -p "$PID_DIR"
  pidfile="$(pidfile_for "$port")"
  logfile="$(logfile_for "$port")"
  if [ -f "$pidfile" ]; then
    oldpid="$(tr -d '[:space:]' <"$pidfile" || true)"
    oldline=""
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
      oldline="$(tr '\0' ' ' <"/proc/$oldpid/cmdline" 2>/dev/null || true)"
    fi
    case "$oldline" in
      *zara_local*)
        echo "zara_local: already running on port $port (pid $oldpid)"; exit 0 ;;
      *)
        echo "zara_local: removing stale pidfile for port $port (was '${oldpid:-empty}')"; rm -f "$pidfile" ;;
    esac
  fi

  if [ -n "$db_dir" ]; then
    mkdir -p "$db_dir"
    : "${ZARA_MEMORY_DB:=$db_dir/memory.db}"
    : "${ZARA_MISSION_DB:=$db_dir/missions.db}"
    : "${ZARA_AUDIT_DB:=$db_dir/audit.db}"
    : "${ZARA_FABRIC_DB:=$db_dir/fabric.db}"
    : "${ZARA_SESSION_DB:=$db_dir/session.db}"
    : "${ZARA_CHAT_DB:=$db_dir/chat.db}"
    : "${ZARA_DELEGATION_DB:=$db_dir/delegation.db}"
    : "${ZARA_DEVICE_AUTH_DB:=$db_dir/device_auth.db}"
    export ZARA_MEMORY_DB ZARA_MISSION_DB ZARA_AUDIT_DB ZARA_FABRIC_DB ZARA_SESSION_DB ZARA_CHAT_DB ZARA_DELEGATION_DB ZARA_DEVICE_AUTH_DB
    echo "zara_local: file-backed DBs under $db_dir"
  else
    echo "zara_local: all-memory mode (no --db-dir; shell env passes through, unset DB vars = in-memory)"
  fi

  echo "zara_local: env (values redacted):"
  print_env_contract
  echo "zara_local: port=$port (fallback=$fell_back) log=$logfile pidfile=$pidfile"

  cd "$REPO_ROOT" || exit 1
  # Marker strategy: python's argv[0] must stay real (exec -a breaks
  # interpreter path resolution), so the "zara_local" marker lives in the
  # -c program text itself, which is preserved verbatim in /proc cmdline.
  # stop/ verifies this marker before signalling any PID.
  if [ -n "$tls_cert" ]; then
    TLSCF="$tls_cert" TLSKF="$tls_key" \
    nohup python3 -c '# zara_local managed server
import os
from uvicorn import run
run("core.app:app", host="127.0.0.1", port='"$port"',
    ssl_certfile=os.environ["TLSCF"], ssl_keyfile=os.environ["TLSKF"])' >"$logfile" 2>&1 &
  else
    nohup python3 -c '# zara_local managed server
from uvicorn import run
run("core.app:app", host="127.0.0.1", port='"$port"')' >"$logfile" 2>&1 &
  fi
  newpid=$!
  echo "$newpid" >"$pidfile"
  echo "zara_local: launched pid $newpid; waiting for /v1/ready (<= ${READY_TIMEOUT_S}s)"

  local waited=0 body="" proto="http://" rca=""
  if [ -n "$tls_cert" ]; then
    proto="https://"
    if [ -n "$tls_ca" ]; then
      rca="--cacert $tls_ca"
    else
      # Readiness ONLY: loopback liveness probe against our own server.
      # Without a staged CA we cannot verify a self-signed cert here;
      # pinned -k proves nothing beyond liveness. Real clients MUST
      # verify — docs/TRANSPORT_TLS.md.
      rca="-k"
    fi
  fi
  while [ "$waited" -lt "$READY_TIMEOUT_S" ]; do
    body="$(curl -sf $rca --max-time 2 "${proto}127.0.0.1:$port/v1/ready" 2>/dev/null || true)"
    if [ -n "$body" ]; then break; fi
    sleep 1; waited=$((waited + 1))
  done
  if [ -z "$body" ]; then
    echo "zara_local: readiness FAILED on port $port after ${READY_TIMEOUT_S}s" >&2
    echo "--- log tail ($logfile) ---" >&2
    tail -n 40 "$logfile" >&2 || true
    if kill -0 "$newpid" 2>/dev/null; then
      line="$(tr '\0' ' ' <"/proc/$newpid/cmdline" 2>/dev/null || true)"
      case "$line" in
        *zara_local*) kill -TERM "$newpid" 2>/dev/null || true ;;
        *) echo "zara_local: launched pid changed identity; not signalling" >&2 ;;
      esac
    fi
    rm -f "$pidfile"
    exit 1
  fi
  echo "zara_local: READY on port $port"
  echo "$body"
}

main() {
  need_bin ss; need_bin curl; need_bin python3
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    start)
      # shellcheck disable=SC2068
      cmd_start $@ ;;
    stop)
      req=""; while [ $# -gt 0 ]; do case "$1" in --port) req="${2:-}"; shift 2;; --port=*) req="${1#--port=}"; shift;; *) echo "zara_local: unknown stop flag: $1" >&2; exit 1;; esac; done
      cmd_stop "$req" ;;
    status)
      req=""; while [ $# -gt 0 ]; do case "$1" in --port) req="${2:-}"; shift 2;; --port=*) req="${1#--port=}"; shift;; *) echo "zara_local: unknown status flag: $1" >&2; exit 1;; esac; done
      cmd_status "$req" ;;
    *) echo "usage: zara_local.sh {start|stop|status} [--port P] [--db-dir D] [--env-file F]" >&2; exit 1 ;;
  esac
}

main "$@"
