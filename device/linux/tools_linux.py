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
                    r"private[_-]?key", r"\.gnupg", r"\.pki")]


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
    with open(path, "rb") as f:
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
    (_def("shell.safe_readonly", "Allowlisted read-only commands, no shell",
          {"type": "object", "required": ["command"],
           "properties": {"command": {"type": "string"}}},
          ["terminal.safe"], 0.05,
          perm=PermissionLevel.CONFIRM, risk=RiskLevel.CONFIRM), shell_safe_readonly),
]

#: Capabilities the Linux agent advertises — derived from implemented tools.
LINUX_CAPABILITIES = sorted({c for d, _ in LINUX_TOOLS for c in d.required_capabilities})
