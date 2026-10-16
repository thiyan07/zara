"""Pure, stdlib-only helpers describing the contract tested in
tests/test_runbook.py and documented for operators in docs/RUNBOOK.md.

NOT used by scripts/zara_local.sh at runtime (bash cannot import Python).
The shell script re-implements the same rules; this module is the tested
specification of those rules. Keep the two in sync; tests cover THIS file,
script behaviour is verified by the self-test transcript in the runbook.
"""

from __future__ import annotations

import os
import re

DEFAULT_PORT = 8080
FALLBACK_LO = 18080
FALLBACK_HI = 18099
PID_DIR = "/tmp/opencode"
SECRET_HINTS = ("KEY", "TOKEN")


def parse_ss_listen_ports(ss_output: str) -> set[int]:
    """Extract local LISTEN ports from `ss -ltn` output.

    Pure string parsing: takes captured ss stdout, returns the set of
    local ports in LISTEN state. Unparseable lines are ignored.
    """
    ports: set[int] = set()
    for line in ss_output.splitlines():
        line = line.strip()
        if not line or line.startswith("State"):
            continue
        parts = line.split()
        # ss -ltn columns: State Recv-Q Send-Q LocalAddr:Port PeerAddr:Port
        if len(parts) < 4:
            continue
        local = parts[3]
        m = re.search(r":(\d+)$", local.strip("[]"))
        if m:
            try:
                ports.add(int(m.group(1)))
            except ValueError:
                continue
    return ports


def pick_port(requested: int | None, occupied: set[int],
              default: int = DEFAULT_PORT,
              lo: int = FALLBACK_LO, hi: int = FALLBACK_HI,
              ) -> tuple[int | None, bool]:
    """Port-selection rule (mirrors scripts/zara_local.sh start).

    Returns (port, fell_back). (None, False) means "no port available":
    - explicit --port busy -> (None, False); caller must refuse (exit 2),
      never signal the occupant.
    - default 8080 busy -> first free port in [lo, hi] with fell_back=True.
    - range exhausted -> (None, False).
    """
    if requested is not None:
        if requested in occupied:
            return None, False
        return requested, False
    if default not in occupied:
        return default, False
    for candidate in range(lo, hi + 1):
        if candidate not in occupied:
            return candidate, True
    return None, False


def pidfile_path(port: int, pid_dir: str = PID_DIR) -> str:
    """Pidfile location: unique per port under the pid dir."""
    return os.path.join(pid_dir, f"zara_local.{port}.pid")


def logfile_path(port: int, pid_dir: str = PID_DIR) -> str:
    """App log location: alongside the pidfile, unique per port."""
    return os.path.join(pid_dir, f"zara_local.{port}.log")


def parse_pidfile(text: str) -> int | None:
    """Parse pidfile content. Returns the PID or None when stale/corrupt
    (empty, non-numeric, or <= 0). Never raises."""
    try:
        pid = int(text.strip().split()[0])
    except (ValueError, IndexError):
        return None
    return pid if pid > 1 else None


def cmdline_looks_ours(cmdline: str) -> bool:
    """True only if the /proc/<pid>/cmdline text contains zara_local.

    stop/ must refuse to signal any PID whose cmdline lacks this marker
    (protects against stale pidfiles and PID reuse). NUL bytes from the
    raw cmdline file are treated as separators.
    """
    return "zara_local" in cmdline.replace("\x00", " ")


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Redact secret values for display. Any variable whose name contains
    KEY or TOKEN (case-insensitive) has its value replaced with '***';
    all other names pass through with values intact. Names are never
    dropped so operators can see WHAT is set without seeing secrets."""
    redacted: dict[str, str] = {}
    for name, value in env.items():
        if any(hint in name.upper() for hint in SECRET_HINTS):
            redacted[name] = "***"
        else:
            redacted[name] = value
    return redacted


def db_env_for_dir(db_dir: str) -> dict[str, str]:
    """DB file layout used when --db-dir is given: one sqlite file per
    store under db_dir. Mirrors the export block in zara_local.sh start.
    Empty db_dir means all-memory (no files written)."""
    if not db_dir:
        return {}
    return {
        "ZARA_MEMORY_DB": os.path.join(db_dir, "memory.db"),
        "ZARA_MISSION_DB": os.path.join(db_dir, "missions.db"),
        "ZARA_AUDIT_DB": os.path.join(db_dir, "audit.db"),
        "ZARA_FABRIC_DB": os.path.join(db_dir, "fabric.db"),
        "ZARA_SESSION_DB": os.path.join(db_dir, "session.db"),
        "ZARA_CHAT_DB": os.path.join(db_dir, "chat.db"),
        "ZARA_DELEGATION_DB": os.path.join(db_dir, "delegation.db"),
    }
