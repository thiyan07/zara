"""Linux device tool implementations — SAFE allowlisted set only.

Boundaries (in addition to core policy):
- Filesystem tools are scoped to ALLOWED_ROOTS (user HOME + /tmp) and refuse
  sensitive paths (~/.ssh, *credential*, *secret*, *token*) — secrets never
  leave the device through these tools either.
- shell.safe_readonly runs an allowlist of binaries via shell=False, rejects
  shell metacharacters, caps output. No rm/dd/mkfs/shutdown/etc. possible.
"""
from __future__ import annotations
import os
import re
import subprocess
from pathlib import Path
from core.models import PermissionLevel, RiskLevel, ToolDefinition

HOME = str(Path.home())
ALLOWED_ROOTS = (os.path.realpath(HOME), "/tmp")
SENSITIVE_HINTS = [re.compile(p, re.I) for p in
                   (r"\.ssh", r"credential", r"secret", r"\btoken\b",
                    r"private[_-]?key", r"\.gnupg", r"\.pki",
                    r"\bid_rsa\b", r"\.pem$")]


def _scoped(path: str) -> str:
    real = os.path.realpath(os.path.expanduser(path))
    if not any(real == r or real.startswith(r + os.sep) for r in ALLOWED_ROOTS):
        raise PermissionError(f"outside allowed roots: {path}")
    if any(p.search(real) for p in SENSITIVE_HINTS):
        raise PermissionError(f"sensitive path refused: {path}")
    return real


def _def(name: str, desc: str, schema: dict, caps: list[str], cost: float,
         perm=PermissionLevel.RESTRICTED, risk=RiskLevel.SAFE,
         timeout: float = 15.0) -> ToolDefinition:
    return ToolDefinition(name=name, description=desc, input_schema=schema,
                          output_schema={"type": "object"},
                          required_capabilities=caps, permission=perm, risk=risk,
                          estimated_cost=cost, timeout_s=timeout,
                          success_criteria="structured result returned",
                          failure_behavior="fail", verification="none",
                          supported_devices=["linux"], reversible=True,
                          version="2.0.0")

# ---------- handlers ----------

# NOTE: `system.info` is intentionally NOT a device tool. The core's local
# `system.info` (supported_devices=["any"]) is the authority for that name;
# device-specific detail is exposed via system.battery/network/processes.read.
# This keeps one unambiguous owner per tool name.
def system_battery(inputs: dict, ctx: dict) -> dict:
    base = Path("/sys/class/power_supply")
    out: dict = {"present": False}
    try:
        for bat in sorted(base.glob("BAT*")):
            cap = (bat / "capacity").read_text().strip()
            st = (bat / "status").read_text().strip()
            out = {"present": True, "battery_pct": float(cap),
                   "charging": st.lower() in ("charging", "full") and st.lower() == "charging",
                   "status": st, "source": str(bat)}
            break
    except (OSError, ValueError):
        pass
    return out


def system_network(inputs: dict, ctx: dict) -> dict:
    ifaces: dict = {}
    base = Path("/sys/class/net")
    try:
        for nic in base.iterdir():
            try:
                state = (nic / "operstate").read_text().strip()
            except OSError:
                state = "unknown"
            ifaces[nic.name] = state
    except OSError as e:
        return {"error": str(e), "interfaces": {}}
    up = [n for n, s in ifaces.items() if s == "up" and n != "lo"]
    return {"interfaces": ifaces, "network": "online" if up else "offline"}


def system_telemetry(inputs: dict, ctx: dict) -> dict:
    """Resource telemetry snapshot (observation only, never authority).

    Stdlib-only: sysfs + /proc + shutil. Every probe is best-effort;
    missing signals are omitted (Core treats absent as unknown, never
    zero). Never raises. NOT registered as a device tool — heartbeat
    plumbing only (keeps the tool allowlist unchanged).
    """
    out: dict = {}
    # CPU: true utilization via /proc/stat two-sample delta (stdlib file
    # reads, no subprocess). Falls back to the loadavg approximation,
    # then to omission. Labeled honest either way; Core validates bounds.
    try:
        cores = os.cpu_count() or 0
        if cores > 0:
            out["cpu_count"] = cores

        def _stat_totals():
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("cpu "):
                        p = [int(x) for x in line.split()[1:]]
                        total = sum(p)
                        idle = p[3] + (p[4] if len(p) > 4 else 0)
                        return total, idle
            return None

        first = _stat_totals()
        if first is not None:
            import time as _time
            _time.sleep(0.1)
            second = _stat_totals()
            if second is not None:
                dtotal = second[0] - first[0]
                didle = second[1] - first[1]
                if dtotal > 0:
                    out["cpu_pct"] = round(
                        max(0.0, min(100.0,
                                     (dtotal - didle) * 100.0 / dtotal)), 1)
                    out["cpu_source"] = "proc_stat"
        if "cpu_pct" not in out and cores > 0:
            try:
                load1 = os.getloadavg()[0]
                out["cpu_pct"] = round(
                    max(0.0, min(100.0, load1 / cores * 100.0)), 1)
                out["cpu_source"] = "loadavg_approx"
            except (OSError, ValueError, IndexError):
                pass
    except Exception:  # noqa: BLE001 — never fail the heartbeat
        pass
    # RAM via /proc/meminfo.
    try:
        total = avail = None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])  # KiB
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
                if total is not None and avail is not None:
                    break
        if total and total > 0 and avail is not None and 0 <= avail <= total:
            out["mem_total_mb"] = total // 1024
            out["mem_avail_mb"] = avail // 1024
            out["mem_pct"] = round((total - avail) * 100.0 / total, 1)
    except (OSError, ValueError):
        pass
    # Storage via shutil (no directory scan).
    try:
        import shutil as _shutil
        du = _shutil.disk_usage("/")
        out["storage_total_mb"] = int(du.total // 1048576)
        out["storage_free_mb"] = int(du.free // 1048576)
    except (OSError, ValueError):
        pass
    # Battery temperature: real sensor only (BAT*/temp, tenths °C).
    # Best-effort; absent on desktops/odd drivers → omitted.
    try:
        base = Path("/sys/class/power_supply")
        for bat in sorted(base.glob("BAT*")):
            try:
                raw = (bat / "temp").read_text().strip()
                out["battery_temp_c"] = round(int(raw) / 10.0, 1)
                break
            except (OSError, ValueError):
                continue
    except Exception:  # noqa: BLE001 — never fail the heartbeat
        pass
    # GPU: no telemetry stack — deliberately omitted (unavailable).
    # Thermal: no portable stdlib source — deliberately omitted.
    return out


def system_processes_read(inputs: dict, ctx: dict) -> dict:
    limit = min(int(inputs.get("limit", 20)), 50)
    p = subprocess.run(["ps", "-eo", "pid,comm,etime,pcpu,pmem", "--sort=-pcpu"],
                       capture_output=True, text=True, timeout=10)
    lines = p.stdout.strip().splitlines()
    return {"header": lines[0] if lines else "",
            "processes": lines[1:limit + 1], "count": len(lines) - 1}


def filesystem_list(inputs: dict, ctx: dict) -> dict:
    path = _scoped(inputs["path"])
    entries = sorted(os.listdir(path))[:200]
    return {"path": path, "entries": entries, "count": len(entries)}


def filesystem_read(inputs: dict, ctx: dict) -> dict:
    path = _scoped(inputs["path"])
    max_bytes = min(int(inputs.get("max_bytes", 65536)), 262144)
    # O_NOFOLLOW on the ORIGINAL (unresolved) path closes the check-use
    # race: a symlink swapped in after _scoped() is refused instead of
    # followed into sensitive files. _scoped already validated policy;
    # opening the resolved path would re-traverse (and re-resolve) links.
    # Trade-off: even legitimate in-scope symlinks are refused — read
    # their real target path instead (documented).
    try:
        fd = os.open(inputs["path"], os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        import errno as _errno
        if e.errno == _errno.ELOOP:
            raise PermissionError(f"symlink refused: {inputs['path']}")
        raise
    with os.fdopen(fd, "rb") as f:
        data = f.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    return {"path": path, "text": data[:max_bytes].decode("utf-8", "replace"),
            "truncated": truncated}


def filesystem_exists(inputs: dict, ctx: dict) -> dict:
    try:
        path = _scoped(inputs["path"])
    except PermissionError:
        return {"path": inputs["path"], "exists": False, "refused": True}
    return {"path": path, "exists": os.path.exists(path)}


SCRATCH_DIR_NAME = "zara-scratch"
SCRATCH_MAX_BYTES = 65536


def _scratch_name(name: str) -> str:
    """Validate a bare filename (no directories, no traversal tokens,
    no sensitive names). Returns the final component."""
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    if os.path.basename(name) != name or name in ("", ".", ".."):
        raise ValueError("name must be a bare filename")
    if any(p.search(name) for p in SENSITIVE_HINTS):
        raise PermissionError(f"sensitive name refused: {name}")
    return name


def _scratch_dir_fd():
    """Open the scratch dir itself (created on demand) and return
    (fd, base). Callers operate with dir_fd so a swapped parent cannot
    redirect them, and use O_NOFOLLOW so a swapped final symlink is
    refused instead of followed (TOCTOU-safe open, not string checks)."""
    import stat as _stat
    base = os.path.realpath(os.path.join(HOME, SCRATCH_DIR_NAME))
    os.makedirs(base, exist_ok=True)
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        st = os.fstat(fd)
        if not _stat.S_ISDIR(st.st_mode):
            raise PermissionError(f"scratch dir obstructed: {base}")
        return fd, base
    except Exception:
        os.close(fd)
        raise


def filesystem_write(inputs: dict, ctx: dict) -> dict:
    name = inputs.get("name", "")
    text = inputs.get("text", "")
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if len(text.encode("utf-8")) > SCRATCH_MAX_BYTES:
        raise ValueError(f"text exceeds {SCRATCH_MAX_BYTES} bytes")
    base_name = _scratch_name(name)
    pfd, base = _scratch_dir_fd()
    try:
        try:
            fd = os.open(base_name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                         | os.O_NOFOLLOW, 0o600, dir_fd=pfd)
        except FileExistsError:  # pragma: no cover - no O_EXCL used
            raise
        except OSError as e:
            import errno as _errno
            if e.errno == _errno.ELOOP:
                raise PermissionError(
                    f"symlink refused in scratch dir: {name}")
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    finally:
        os.close(pfd)
    return {"path": os.path.join(base, base_name),
            "bytes": len(text.encode("utf-8"))}


def filesystem_delete(inputs: dict, ctx: dict) -> dict:
    base_name = _scratch_name(inputs.get("name", ""))
    pfd, base = _scratch_dir_fd()
    try:
        # unlink(2) never follows the final component: a swapped-in
        # symlink removes only the link itself, never its target.
        # dir_fd pins the parent against directory-swap races.
        try:
            os.unlink(base_name, dir_fd=pfd)
        except FileNotFoundError:
            return {"path": os.path.join(base, base_name), "deleted": False}
    finally:
        os.close(pfd)
    return {"path": os.path.join(base, base_name), "deleted": True}


def filesystem_metadata(inputs: dict, ctx: dict) -> dict:
    path = _scoped(inputs["path"])
    st = os.stat(path)
    return {"path": path, "size": st.st_size, "mtime": st.st_mtime,
            "is_dir": os.path.isdir(path)}


READONLY_ALLOW = {
    "ls": None, "df": None, "free": None, "uptime": None, "uname": None,
    "whoami": None, "date": None, "hostname": None, "ps": None,
    "git": {"status", "log", "diff", "branch", "rev-parse"},
}
META_CHARS = re.compile(r"[;&|$`><\n\\]")


def shell_safe_readonly(inputs: dict, ctx: dict) -> dict:
    cmd = inputs["command"]
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError("command must be a non-empty string")
    if META_CHARS.search(cmd):
        raise PermissionError("shell metacharacters refused in safe_readonly")
    parts = cmd.strip().split()
    if parts[0] not in READONLY_ALLOW:
        raise PermissionError(f"binary not in readonly allowlist: {parts[0]}")
    allowed_subs = READONLY_ALLOW[parts[0]]
    if allowed_subs is not None:
        if len(parts) < 2 or parts[1] not in allowed_subs:
            raise PermissionError(f"git subcommand not allowlisted: {parts[1:2]}")
    p = subprocess.run(parts, capture_output=True, text=True, timeout=15)
    out = (p.stdout + p.stderr)[:8000]
    return {"command": cmd, "returncode": p.returncode, "output": out}


LINUX_TOOLS: list[tuple[ToolDefinition, object]] = [
    (_def("system.battery", "Linux battery state", {"type": "object", "properties": {}},
          ["system.battery"], 0.01), system_battery),
    (_def("system.network", "Linux network state", {"type": "object", "properties": {}},
          ["system.network"], 0.01), system_network),
    (_def("system.processes.read", "Read-only process list",
          {"type": "object", "properties": {"limit": {"type": "integer"}}},
          ["process.read"], 0.05), system_processes_read),
    (_def("filesystem.list", "List directory (scoped to HOME/tmp)",
          {"type": "object", "required": ["path"],
           "properties": {"path": {"type": "string"}}},
          ["filesystem.list"], 0.02), filesystem_list),
    (_def("filesystem.read", "Read file (scoped, capped)",
          {"type": "object", "required": ["path"],
           "properties": {"path": {"type": "string"}, "max_bytes": {"type": "integer"}}},
          ["filesystem.read"], 0.03), filesystem_read),
    (_def("filesystem.exists", "Check path existence (scoped)",
          {"type": "object", "required": ["path"],
           "properties": {"path": {"type": "string"}}},
          ["filesystem.read"], 0.01), filesystem_exists),
    (_def("filesystem.metadata", "File metadata (scoped)",
          {"type": "object", "required": ["path"],
           "properties": {"path": {"type": "string"}}},
          ["filesystem.read"], 0.01), filesystem_metadata),
    (_def("filesystem.write", "Write text file (scratch dir only, capped)",
          {"type": "object", "required": ["name", "text"],
           "properties": {"name": {"type": "string"},
                          "text": {"type": "string"}}},
          ["filesystem.write"], 0.05,
          perm=PermissionLevel.CONFIRM, risk=RiskLevel.CONFIRM), filesystem_write),
    (_def("filesystem.delete", "Delete scratch-dir file (reversible)",
          {"type": "object", "required": ["name"],
           "properties": {"name": {"type": "string"}}},
          ["filesystem.write"], 0.02,
          perm=PermissionLevel.CONFIRM, risk=RiskLevel.CONFIRM), filesystem_delete),
    (_def("shell.safe_readonly", "Allowlisted read-only commands, no shell",
          {"type": "object", "required": ["command"],
           "properties": {"command": {"type": "string"}}},
          ["terminal.safe"], 0.05,
          perm=PermissionLevel.CONFIRM, risk=RiskLevel.CONFIRM), shell_safe_readonly),
]

#: Capabilities the Linux agent advertises — derived from implemented tools.
LINUX_CAPABILITIES = sorted({c for d, _ in LINUX_TOOLS for c in d.required_capabilities})
