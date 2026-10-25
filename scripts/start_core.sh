#!/usr/bin/env bash
# Zara core launcher — persistent location (the /tmp/opencode copy
# kept being wiped; reconstructed twice from OBSERVED behavior,
# 2026-10-24, see RESEARCH R41.4).
# - state DBs live in ~/.local/state/zara (the /data docker paths in
#   .env are unreachable locally and MUST be overridden);
# - LLM_* come from .env, but ASSISTANT_TOKEN stays unset so the
#   operator API keeps the default dev-token contract;
# - the device-auth DB is persistent and must survive restarts
#   (NEVER delete zara_devauth.db).
set -eu
cd /home/thiyan/projects/assistant
if [ -f .env ]; then set -a; . ./.env; set +a; fi
unset ASSISTANT_TOKEN
export ZARA_DEVICE_AUTH_DB="$HOME/.local/state/zara/zara_devauth.db"
export ZARA_AUDIT_DB="$HOME/.local/state/zara/zara_audit.db"
export ZARA_MEMORY_DB="$HOME/.local/state/zara/zara_mem.db"
export ZARA_MISSION_DB="$HOME/.local/state/zara/zara_mission.db"
export ZARA_SCHEDULER_DB="$HOME/.local/state/zara/zara_sched.db"
export ZARA_REMINDERS_DB="$HOME/.local/state/zara/zara_sched.db"
export ZARA_FABRIC_DB="$HOME/.local/state/zara/zara_fabric.db"
# P6-b27: named routines persist next to reminders (same WAL sqlite)
export ZARA_ROUTINES_DB="$HOME/.local/state/zara/zara_routines.db"
# P6-b28: chat ledgers persist (one additive table)
export ZARA_LEDGERS_DB="$HOME/.local/state/zara/zara_ledgers.db"
# P6-b29: capture inbox persists; the mail wire is env-config (off in the lab)
export ZARA_CAPTURE_DB="$HOME/.local/state/zara/zara_capture.db"
# batch-32: habit chains persist (uhabits streak design)
export ZARA_HABITS_DB="$HOME/.local/state/zara/zara_habits.db"
# the local .ics calendar the read/write flows use (observed live since
# Stage 31-P4: the lab calendar at ~/.local/state/zara/calendar/lab.ics)
export ZARA_ICS_PATH="$HOME/.local/state/zara/calendar/lab.ics"
exec python3 -m uvicorn core.app:app --host 0.0.0.0 --port 8080
