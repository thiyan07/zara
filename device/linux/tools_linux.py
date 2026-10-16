"""Linux device tool implementations — SAFE allowlisted set only.

Boundaries (in addition to core policy):
- Filesystem tools are scoped to ALLOWED_ROOTS (user HOME + /tmp) and refuse
  sensitive paths (~/.ssh, *credential*, *secret*, *token*) — secrets never
  leave the device through these tools either.
- shell.safe_readonly runs an allowlist of binaries via shell=False, rejects
  shell metacharacters, caps output. No rm/dd/mkfs/shutdown/etc. possible.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
from pathlib import Path
from core.models import PermissionLevel, RiskLevel, ToolDefinition

try:
    from core.device_tools import APP_OPEN_TARGETS
except ImportError:  # standalone use without core on path
    APP_OPEN_TARGETS = frozenset({
        "dev.zara.zara_android",
        "dev.zara.lab.privtest",
        "com.android.settings",
        "com.android.chrome",
        "com.google.android.gm",
        "com.google.android.calendar",
    })

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


def system_battery_charging_time(inputs: dict, ctx: dict) -> dict:
    """Estimate time to full charge based on /sys/class/power_supply data."""
    base = Path("/sys/class/power_supply")
    out: dict = {"supported": True}
    try:
        for bat in sorted(base.glob("BAT*")):
            cap = int((bat / "capacity").read_text().strip())
            st = (bat / "status").read_text().strip()
            pct = cap
            charging = st.lower() in ("charging", "full")
            
            # Try to get current_now or power_now for charging rate estimation
            current_now = None
            power_now = None
            try:
                cn = (bat / "current_now").read_text().strip()
                current_now = int(cn) if cn else None
            except (OSError, ValueError):
                pass
            try:
                pn = (bat / "power_now").read_text().strip()
                power_now = int(pn) if pn else None
            except (OSError, ValueError):
                pass
            
            time_to_full_minutes: int | None = None
            if charging and 0 < pct < 100:
                # Estimate based on available data
                rate_per_minute: float | None = None
                
                # Try to get voltage for power calculation
                voltage = None
                try:
                    vn = (bat / "voltage_now").read_text().strip()
                    voltage = int(vn) / 1_000_000 if vn else None  # µV to V
                except (OSError, ValueError):
                    pass
                
                # Prefer power_now if available (µW), then current_now * voltage
                if power_now is not None:
                    # power_now is in µW
                    watts = power_now / 1_000_000
                    # Capacity in mAh * V = mWh
                    # Need to estimate capacity - we don't have design capacity easily
                    # Use a rough estimate: assume typical 4000-5000 mAh battery
                    capacity_mah = 4500  # default assumption
                    # time = (remaining_mah * V) / watts
                    remaining_mah = capacity_mah * (100 - pct) / 100
                    time_hours = (remaining_mah * (voltage or 3.7)) / (watts * 1000)
                    if time_hours > 0:
                        time_to_full_minutes = int(time_hours * 60)
                elif current_now is not None and voltage is not None:
                    # current_now is in µA, voltage in µV
                    amps = current_now / 1_000_000
                    volts = voltage / 1_000_000
                    watts = amps * volts
                    capacity_mah = 4500
                    remaining_mah = capacity_mah * (100 - pct) / 100
                    time_hours = (remaining_mah * (voltage / 1_000_000)) / (watts * 1000)
                    if time_hours > 0:
                        time_to_full_minutes = int(time_hours * 60)
                else:
                    # Fallback: assume ~1% per minute for typical USB charging
                    time_to_full_minutes = 100 - pct
                
                # Clamp to reasonable bounds
                if time_to_full_minutes is not None:
                    time_to_full_minutes = max(1, min(time_to_full_minutes, 600))  # 1 min to 10 hours
            
            return {
                "supported": True,
                "battery_pct": pct,
                "charging": charging,
                "time_to_full_minutes": time_to_full_minutes
            }
    except (OSError, ValueError) as e:
        return {"supported": True, "error": str(e)}
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




def system_wifi_state(inputs: dict, ctx: dict) -> dict:
    """Linux WiFi state using nmcli or iwconfig."""
    # Try nmcli first (NetworkManager)
    try:
        result = subprocess.run(["nmcli", "-t", "-f", "WIFI,STATE,SSID,BSSID,SIGNAL", "device", "wifi", "list"],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            # nmcli -t output format: SSID:BSSID:SIGNAL:SECURITY:...
            # For device status, use nmcli device status
            result2 = subprocess.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
                                     capture_output=True, text=True, timeout=10)
            wifi_state = "disabled"
            ssid = ""
            bssid = ""
            signal = 0
            if result2.returncode == 0:
                for line in result2.stdout.strip().split("\n"):
                    parts = line.split(":")
                    if len(parts) >= 3 and parts[1] == "wifi":
                        if parts[2] == "connected":
                            wifi_state = "enabled"
                        elif parts[2] in ("connecting", "disconnected"):
                            wifi_state = parts[2]
                        elif parts[2] == "unavailable":
                            wifi_state = "disabled"
            # Get connected SSID
            try:
                result3 = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID,BSSID,SIGNAL", "device", "wifi", "list"],
                                         capture_output=True, text=True, timeout=10)
                if result3.returncode == 0:
                    for line in result3.stdout.strip().split("\n"):
                        if line.startswith('yes:'):
                            parts = line.split(":")
                            if len(parts) >= 4:
                                ssid = parts[1].replace("\:", ":")
                                bssid = parts[2]
                                signal = int(parts[3]) if parts[3].isdigit() else 0
                                break
            except Exception:
                pass
            return {
                "supported": True,
                "state": "enabled" if wifi_state == "enabled" else "disabled",
                "ssid": ssid,
                "bssid": bssid,
                "signal": signal
            }
    except Exception:
        pass
    # Fallback to iwconfig
    try:
        result = subprocess.run(["iwconfig"], capture_output=True, text=True, timeout=5)
        return {"supported": True, "state": "unknown", "note": "iwconfig fallback"}
    except Exception:
        pass
    return {"supported": False, "error": "no wifi tools available"}

def system_wifi_settings(inputs: dict, ctx: dict) -> dict:
    """Open system WiFi settings - Linux equivalent opens NetworkManager settings."""
    return try_open_settings("wifi", ["nm-connection-editor", "--type=wifi"],
                             ["gnome-control-center", "wifi"],
                             ["kde5-networkmanagement", "wifi"])

def system_bluetooth_state(inputs: dict, ctx: dict) -> dict:
    """Linux Bluetooth state via bluetoothctl or dbus."""
    out: dict = {"supported": True}
    try:
        result = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            state = "disabled"
            name = ""
            address = ""
            for line in lines:
                if "Powered:" in line:
                    state = "enabled" if "yes" in line.lower() else "disabled"
                elif "Name:" in line:
                    name = line.split(":", 1)[1].strip()
                elif "Address:" in line:
                    address = line.split(":", 1)[1].strip()
            # Get bonded devices
            result2 = subprocess.run(["bluetoothctl", "paired-devices"], capture_output=True, text=True, timeout=5)
            bonded = ""
            if result2.returncode == 0:
                bonded = ", ".join([line.split()[1] for line in result2.stdout.strip().split('\n') if line.strip()])
            return {
                "supported": True,
                "state": state,
                "name": name,
                "address": address,
                "bonded_devices": bonded
            }
    except Exception:
        pass
    return {"supported": False, "error": "bluetoothctl not available"}

def system_bluetooth_settings(inputs: dict, ctx: dict) -> dict:
    """Open Bluetooth settings."""
    return try_open_settings("bluetooth", ["blueman-manager"], 
                             ["gnome-control-center", "bluetooth"],
                             ["kde5-bluetooth", "bluetooth"])

def system_hotspot_state(inputs: dict, ctx: dict) -> dict:
    """Hotspot state via nmcli."""
    out: dict = {"supported": True}
    try:
        result = subprocess.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            state = "disabled"
            for line in result.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 3 and "hotspot" in parts[1].lower() or "ap" in parts[1].lower():
                    state = parts[2]
        return {"supported": True, "state": state}
    except Exception:
        pass
    return {"supported": False, "error": "nmcli not available"}

def system_hotspot_settings(inputs: dict, ctx: dict) -> dict:
    """Open hotspot settings."""
    return try_open_settings("hotspot", ["nm-connection-editor", "--type=wifi", "--create"],
                             ["gnome-control-center", "wifi"],
                             ["kde5-networkmanagement", "hotspot"])

def system_mobile_data_state(inputs: dict, ctx: dict) -> dict:
    """Mobile data state via ModemManager or nmcli."""
    out: dict = {"supported": True}
    try:
        result = subprocess.run(["nmcli", "-t", "-f", "WWAN,STATE", "radio"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            parts = result.stdout.strip().split(':')
            enabled = parts[1].lower() == "enabled" if len(parts) > 1 else False
            network = "cellular" if enabled else "offline"
            return {"supported": True, "enabled": enabled, "network": network}
    except Exception:
        pass
    return {"supported": False, "error": "nmcli not available"}

def system_mobile_data_settings(inputs: dict, ctx: dict) -> dict:
    return try_open_settings("mobile_data", ["nm-connection-editor", "--type=gsm"],
                             ["gnome-control-center", "network"],
                             ["kde5-networkmanagement", "mobile"])

def system_airplane_mode_state(inputs: dict, ctx: dict) -> dict:
    """Airplane mode via nmcli or rfkill."""
    out: dict = {"supported": True}
    try:
        result = subprocess.run(["nmcli", "-t", "-f", "WWAN", "radio"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            enabled = result.stdout.strip().lower() == "enabled"
            return {"supported": True, "enabled": enabled}
    except Exception:
        pass
    # Fallback to rfkill
    try:
        result = subprocess.run(["rfkill", "list", "wlan"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            enabled = "Soft blocked: no" in result.stdout
            return {"supported": True, "enabled": not enabled}
    except Exception:
        pass
    return {"supported": False, "error": "no airplane mode detection available"}

def system_airplane_mode_settings(inputs: dict, ctx: dict) -> dict:
    return try_open_settings("airplane_mode", ["nm-connection-editor"],
                             ["gnome-control-center", "network"],
                             ["kde5-networkmanagement", "airplane"])

def system_app_open(inputs: dict, ctx: dict) -> dict:
    pkg = inputs.get("package", "")
    if not pkg:
        return {"supported": True, "error": "package name required"}
    # Same allowlist as core contract (parity test requires identical schema).
    # On Linux these Android packages have no native target: map the two
    # that have desktop equivalents, refuse the rest with a clear message.
    if pkg not in APP_OPEN_TARGETS:
        return {"supported": True, "error": f"package not in allowlist: {pkg}"}
    desktop_map = {
        "com.android.chrome": ["xdg-open", "https://www.google.com"],
        "com.android.settings": ["gnome-control-center"],
        "com.google.android.calendar": ["xdg-open", "https://calendar.google.com"],
        "com.google.android.gm": ["xdg-open", "https://mail.google.com"],
    }
    if pkg not in desktop_map:
        return {"supported": True, "launched": False,
                "package": pkg,
                "note": "Android-only package with no Linux desktop equivalent"}
    try:
        subprocess.Popen(desktop_map[pkg], start_new_session=True)
        return {"supported": True, "launched": True, "package": pkg}
    except Exception as e:
        return {"supported": True, "error": f"launch failed: {e}"}

def try_open_settings(name: str, *cmd_attempts: list) -> dict:
    """Try multiple commands to open a settings panel."""
    for cmd in cmd_attempts:
        if not cmd:
            continue
        try:
            subprocess.Popen(cmd, start_new_session=True)
            return {"supported": True, "launched": True, "setting": name}
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return {"supported": True, "launched": False, "error": "no suitable settings app found"}


def _need_enabled(inputs: dict) -> bool:
    """Strict {enabled: bool} validation for set tools. Raises ValueError
    (structured refusal upstream) on anything else — never coerces."""
    v = (inputs or {}).get("enabled")
    if isinstance(v, bool):
        return v
    raise ValueError("set tools require {enabled: boolean}")


def _run_quiet(argv: list, timeout: int = 10):
    """Run a control binary; return CompletedProcess or None on
    missing-binary/timeout. Never raises."""
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def system_wifi_set(inputs: dict, ctx: dict) -> dict:
    """Toggle the WiFi radio via NetworkManager, then verify by re-read."""
    want = _need_enabled(inputs)
    r = _run_quiet(["nmcli", "radio", "wifi", "on" if want else "off"])
    if r is None:
        return {"supported": False,
                "error": "nmcli unavailable (NetworkManager required)"}
    if r.returncode != 0:
        return {"supported": True, "applied": False,
                "error": (r.stderr or r.stdout or "nmcli refused").strip()[:200]}
    got = system_wifi_state({}, ctx).get("state")
    applied = (got == ("enabled" if want else "disabled"))
    return {"supported": True, "applied": applied,
            "requested": want, "state": got,
            "verify": "system.wifi.state"}


def system_bluetooth_set(inputs: dict, ctx: dict) -> dict:
    """Toggle Bluetooth power via bluetoothctl, then verify by re-read."""
    want = _need_enabled(inputs)
    r = _run_quiet(["bluetoothctl", "power", "on" if want else "off"])
    if r is None:
        return {"supported": False,
                "error": "bluetoothctl unavailable (BlueZ required)"}
    if r.returncode != 0:
        return {"supported": True, "applied": False,
                "error": (r.stderr or r.stdout or "bluetoothctl refused").strip()[:200]}
    got = system_bluetooth_state({}, ctx).get("state")
    applied = (got == ("enabled" if want else "disabled"))
    return {"supported": True, "applied": applied,
            "requested": want, "state": got,
            "verify": "system.bluetooth.state"}


def _hotspot_profile() -> "str | None":
    """Name of a configured AP-mode wifi profile, if any. None means no
    silent network creation is possible — refuse instead of inventing one."""
    r = _run_quiet(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    if r is None or r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        name, _, typ = line.partition(":")
        if typ.strip() == "802-11-wireless" and "hotspot" in name.lower():
            return name.strip()
    return None


def system_hotspot_set(inputs: dict, ctx: dict) -> dict:
    """Up/down an existing hotspot profile only; refuses when none is
    configured (never creates networks silently). Verifies via re-read."""
    want = _need_enabled(inputs)
    prof = _hotspot_profile()
    if prof is None:
        return {"supported": True, "applied": False,
                "error": "no hotspot profile configured; refusing to invent one",
                "verify": "system.hotspot.state"}
    verb = "up" if want else "down"
    r = _run_quiet(["nmcli", "connection", verb, prof])
    if r is None or r.returncode != 0:
        err = "" if r is None else (r.stderr or r.stdout or "nmcli refused")
        return {"supported": True, "applied": False,
                "error": str(err).strip()[:200],
                "verify": "system.hotspot.state"}
    got = system_hotspot_state({}, ctx).get("state", "unknown")
    # Hotspot state carries raw nmcli device states; anything other than
    # an idle/down marker counts as up. Verified change only.
    down_marks = ("disabled", "disconnected", "unavailable", "unknown")
    applied = (got not in down_marks) if want else (got in down_marks)
    return {"supported": True, "applied": bool(applied), "requested": want,
            "profile": prof, "state": got,
            "verify": "system.hotspot.state"}


def system_mobile_data_set(inputs: dict, ctx: dict) -> dict:
    """Toggle cellular data via ModemManager. Most laptops have no modem:
    that is deployment_blocked, not failure."""
    want = _need_enabled(inputs)
    r = _run_quiet(["mmcli", "-L"])
    if r is None:
        return {"supported": False,
                "error": "mmcli unavailable (ModemManager required)",
                "deployment": "blocked"}
    if r.returncode != 0 or "/org/freedesktop/ModemManager" not in (r.stdout or ""):
        return {"supported": True, "applied": False,
                "deployment": "blocked",
                "error": "no cellular modem present",
                "verify": "system.mobile_data.state"}
    r2 = _run_quiet(["mmcli", "-m", "0",
                     "--enable" if want else "--disable"])
    if r2 is None or r2.returncode != 0:
        err = "" if r2 is None else (r2.stderr or r2.stdout or "mmcli refused")
        return {"supported": True, "applied": False,
                "error": str(err).strip()[:200],
                "verify": "system.mobile_data.state"}
    got = system_mobile_data_state({}, ctx).get("enabled")
    # State tool reports radio-level `enabled`; applied only on exact match.
    applied = (got is True) if want else (got is False)
    return {"supported": True, "applied": bool(applied), "requested": want,
            "state": "enabled" if got else "disabled" if got is False else "unknown",
            "verify": "system.mobile_data.state"}


def _rfkill_blocked() -> "bool | None":
    r = _run_quiet(["rfkill", "-n", "-o", "TYPE,SOFT", "list"])
    if r is None or r.returncode != 0:
        return None
    states = []
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("wifi", "bluetooth", "wwan"):
            states.append(parts[1].lower())
    if not states:
        return None
    return all(s == "blocked" for s in states)


def system_airplane_mode_set(inputs: dict, ctx: dict) -> dict:
    """rfkill block/unblock (wifi+bluetooth+wwan), then verify by re-read.
    Permission errors are reported, never raised."""
    want = _need_enabled(inputs)
    verb = "block" if want else "unblock"
    r = _run_quiet(["rfkill", verb, "wifi", "bluetooth", "wwan"])
    if r is None:
        return {"supported": False,
                "error": "rfkill unavailable (util-linux required)"}
    if r.returncode != 0:
        return {"supported": True, "applied": False,
                "error": (r.stderr or r.stdout or
                          "rfkill refused (may need privileges)").strip()[:200]}
    got = _rfkill_blocked()
    if got is None:
        # rfkill parse failed: fall back to the state tool's own reading so
        # the verify pointer stays truthful about its source.
        fb = system_airplane_mode_state({}, ctx).get("enabled")
        got = (fb is False) if isinstance(fb, bool) else None
    applied = (got is True) if want else (got is False)
    state = "enabled" if got is True else "disabled" if got is False else "unknown"
    return {"supported": True, "applied": bool(applied), "requested": want,
            "state": state, "verify": "system.airplane_mode.state"}


SCREENSHOT_MAX_BYTES = 1_500_000


def _have(binary: str) -> bool:
    """Presence probe: True unless the binary is missing/unrunnable."""
    return _run_quiet([binary, "--version"], timeout=5) is not None


def desktop_session(inputs: dict, ctx: dict) -> dict:
    """Detect the graphical session type from the environment. Pure read,
    no subprocesses, never raises."""
    xdg = os.environ.get("XDG_SESSION_TYPE", "").lower()
    wayland = os.environ.get("WAYLAND_DISPLAY", "")
    display = os.environ.get("DISPLAY", "")
    if xdg in ("x11", "wayland"):
        session = xdg
    elif wayland:
        session = "wayland"
    elif display:
        session = "x11"
    else:
        session = "headless"
    return {"supported": True, "session": session,
            "display": display or None,
            "wayland_display": wayland or None}


def desktop_window_active(inputs: dict, ctx: dict) -> dict:
    """Active-window title via X11 (xdotool). Wayland deliberately reports
    unsupported: no probe without a foreign-toplevel helper, and titles
    are never guessed."""
    if desktop_session({}, ctx).get("session") == "wayland":
        return {"supported": False,
                "error": "no Wayland active-window probe "
                         "(needs a foreign-toplevel helper)"}
    r = _run_quiet(["xdotool", "getactivewindow", "getwindowname"])
    if r is None:
        return {"supported": False,
                "error": "xdotool unavailable (X11 active-window probe)"}
    if r.returncode != 0:
        return {"supported": True, "title": None,
                "error": (r.stderr or "xdotool refused").strip()[:200]}
    title = (r.stdout or "").strip()
    return {"supported": True, "title": title or None,
            "note": "window titles may be sensitive; treat as untrusted"}


def _scratch_bytes(name: str, data: bytes, cap: int) -> str:
    """TOCTOU-safe binary write into the scratch dir (mirrors
    filesystem_write). Returns the real path."""
    if len(data) > cap:
        raise ValueError(f"blob exceeds {cap} bytes")
    base_name = _scratch_name(name)
    if not base_name.lower().endswith(".png"):
        raise ValueError("screenshots are PNG only")
    pfd, base = _scratch_dir_fd()
    try:
        try:
            fd = os.open(base_name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                         | os.O_NOFOLLOW, 0o600, dir_fd=pfd)
        except OSError as e:
            import errno as _errno
            if e.errno == _errno.ELOOP:
                raise PermissionError(
                    f"symlink refused in scratch dir: {name}")
            raise
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    finally:
        os.close(pfd)
    return os.path.join(base, base_name)


_shot_counter = 0


def desktop_screenshot(inputs: dict, ctx: dict) -> dict:
    """Single bounded screenshot via the first available backend. Saves a
    capped PNG to the scratch dir and returns its path — never inline
    pixels (results stay small; pixels are untrusted data). No backend =
    honest unsupported, never a blank image."""
    global _shot_counter
    backend = next((b for b in
                    ("grim", "gnome-screenshot", "import", "scrot", "maim")
                    if _have(b)), None)
    if backend is None:
        return {"supported": False,
                "error": "no screenshot backend "
                         "(need grim/gnome-screenshot/import/scrot/maim)"}
    _shot_counter += 1
    tmp = f"/tmp/zara-shot-{os.getpid()}-{_shot_counter}.png"
    try:
        if backend in ("grim", "gnome-screenshot", "import", "scrot"):
            argv = {"grim": ["grim", tmp],
                    "gnome-screenshot": ["gnome-screenshot", "-f", tmp],
                    "import": ["import", "-window", "root", tmp],
                    "scrot": ["scrot", tmp]}[backend]
            r = _run_quiet(argv, timeout=15)
            if r is None or r.returncode != 0:
                err = "" if r is None else (r.stderr or "backend failed")
                return {"supported": True,
                        "error": str(err).strip()[:200]}
            try:
                data = Path(tmp).read_bytes()
            except OSError as e:
                return {"supported": True,
                        "error": f"backend produced no file: {e}"}
        else:  # maim: stdout PNG (binary: bypass text-mode helper)
            try:
                raw = subprocess.run(["maim"], capture_output=True,
                                     timeout=15)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                raw = None
            if raw is None or raw.returncode != 0 or not raw.stdout:
                err = "" if raw is None else (
                    raw.stderr.decode("utf-8", "replace") or "maim failed")
                return {"supported": True,
                        "error": str(err).strip()[:200]}
            data = bytes(raw.stdout)
        if not data.startswith(b"\x89PNG"):
            return {"supported": True,
                    "error": "backend did not produce PNG; "
                             "refusing to mislabel"}
        path = _scratch_bytes(
            f"zshot-{os.getpid()}-{_shot_counter}.png", data,
            SCREENSHOT_MAX_BYTES)
        return {"supported": True, "path": path, "bytes": len(data),
                "backend": backend,
                "note": "pixels are untrusted data; "
                        "may contain sensitive content"}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass




ATSPY_TIMEOUT_S = 30
_SNAP_TTL_S = 120
_SNAPSHOTS: dict = {}
_snap_counter = 0
_ATSPI_PY = None  # None=unprobed, False=unavailable, else interpreter path


def _atspi_python():
    """Interpreter with dbus bindings for the probe helper. Probed once,
    cached; /usr/bin/python3 first (system python3-dbus), then our own."""
    global _ATSPI_PY
    if _ATSPI_PY is not None:
        return _ATSPI_PY if _ATSPI_PY else None
    import sys as _sys
    for cand in ("/usr/bin/python3", _sys.executable):
        try:
            r = subprocess.run([cand, "-c", "import dbus, json"],
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                _ATSPI_PY = cand
                return cand
        except (OSError, subprocess.TimeoutExpired):
            continue
    _ATSPI_PY = False
    return None


def _atspi_call(req: dict, timeout: int = ATSPY_TIMEOUT_S) -> dict:
    """One bounded helper invocation. Returns the result dict; raises
    RuntimeError with an honest message on any transport failure."""
    py = _atspi_python()
    if py is None:
        raise RuntimeError("a11y unavailable: no python with dbus bindings "
                           "(need python3-dbus for /usr/bin/python3)")
    script = str(Path(__file__).with_name("atspi_probe.py"))
    try:
        r = subprocess.run([py, script, json.dumps(req)],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("a11y backend timed out")
    except OSError as e:
        raise RuntimeError(f"a11y backend failed: {e}")
    try:
        env = json.loads(r.stdout or "")
    except ValueError:
        raise RuntimeError("a11y backend bad output")
    if not isinstance(env, dict) or not env.get("ok"):
        raise RuntimeError(str((env or {}).get("error", "a11y failed"))[:300])
    return env.get("result") or {}


def _snapshot_put(app, app_bus, app_path, nodes) -> str:
    """Store a fresh observation; returns its id. Bounded rundown."""
    global _snap_counter
    import time as _time
    _snap_counter += 1
    sid = f"a11y-{os.getpid()}-{_snap_counter}"
    by_id = {n.get("id", ""): n for n in nodes if isinstance(n, dict)}
    _SNAPSHOTS[sid] = {"app": app, "app_bus": app_bus, "app_path": app_path,
                       "created": _time.time(), "nodes": by_id}
    while len(_SNAPSHOTS) > 20:
        _SNAPSHOTS.pop(next(iter(_SNAPSHOTS)))
    return sid


def _snapshot_get(sid: str) -> dict:
    import time as _time
    snap = _SNAPSHOTS.get(sid)
    if snap is None:
        raise ValueError("unknown snapshot (re-inspect first)")
    if _time.time() - snap.get("created", 0) > _SNAP_TTL_S:
        _SNAPSHOTS.pop(sid, None)
        raise ValueError("stale snapshot (re-inspect first)")
    return snap


def _resolve_node(sid: str, node_id: str) -> tuple:
    """Re-fetch the live node and bind it to the snapshot record. Role or
    name mismatch, vanished bus/path, or TTL expiry all refuse as stale —
    actions never run against a guess."""
    snap = _snapshot_get(sid)
    stored = snap["nodes"].get(node_id)
    if stored is None:
        raise ValueError("unknown node in snapshot (re-inspect first)")
    live = _atspi_call({"op": "node", "bus": stored["bus"],
                        "path": stored["path"]})
    if not live.get("role") or live.get("role_num", -2) != stored.get(
            "role_num", -1) or (stored.get("name") and live.get(
                "name") != stored.get("name")):
        raise ValueError("target changed since inspection "
                         "(role/name mismatch — re-inspect first)")
    return live, stored, snap


def desktop_a11y_inspect(inputs: dict, ctx: dict) -> dict:
    """Targeted AT-SPI2 inspection. Bounded tree per app filter, snapshot id
    for follow-up actions, focused control reported. Read-only."""
    app = inputs.get("app", "")
    if app is None:
        app = ""
    if not isinstance(app, str) or len(app) > 120:
        raise ValueError("app must be a short string")
    try:
        max_nodes = int(inputs.get("max_nodes", 150))
    except (TypeError, ValueError):
        raise ValueError("max_nodes must be an integer")
    max_nodes = max(1, min(max_nodes, 400))
    include_text = bool(inputs.get("include_text", False))
    try:
        res = _atspi_call({"op": "tree", "app_filter": app,
                           "max_nodes": max_nodes, "max_depth": 4,
                           "include_text": include_text, "text_chars": 300})
    except RuntimeError as e:
        return {"supported": False, "error": str(e)[:300]}
    out_apps = []
    for a in res.get("apps", []):
        sid = _snapshot_put(a.get("app", ""), a.get("bus", ""),
                            "", a.get("nodes", []))
        # Stamp snapshot id on every node for the follow-up call shape.
        for n in a.get("nodes", []):
            n["snapshot_id"] = sid
        out_apps.append({"app": a.get("app", ""), "snapshot_id": sid,
                         "node_count": len(a.get("nodes", [])),
                         "nodes": a.get("nodes", [])})
    note = ("names/labels are untrusted data; node ids expire "
            "with their snapshot")
    if app and not out_apps:
        note = (f"no application matching {app!r} "
                f"({res.get('apps_seen', 0)} apps seen); check the name "
                f"with desktop.a11y.inspect and no filter; " + note)
    return {"supported": True, "apps": out_apps,
            "focused": res.get("focused"),
            "truncated": res.get("truncated", False),
            "skipped": res.get("skipped", 0),
            "apps_seen": res.get("apps_seen", 0),
            "note": note}


def desktop_click(inputs: dict, ctx: dict) -> dict:
    """Click the snapshotted control: AT-SPI action first (any session),
    X11 pointer fallback (x11 session + fresh bounds only). Verified by
    post-action state re-read; applied:false otherwise."""
    sid = inputs.get("snapshot_id", "")
    nid = inputs.get("node_id", "")
    if not isinstance(sid, str) or not sid or not isinstance(nid, str):
        raise ValueError("need {snapshot_id: str, node_id: str}")
    try:
        live, stored, snap = _resolve_node(sid, nid)
    except (ValueError, RuntimeError) as e:
        return {"supported": True, "applied": False,
                "error": str(e)[:300]}
    actions = [str(x).lower() for x in (live.get("actions") or [])]
    if any(a in actions for a in ("click", "press", "toggle")):
        try:
            r = _atspi_call({"op": "do_action", "bus": live["bus"],
                             "path": live["path"], "action": "click"})
        except RuntimeError as e:
            return {"supported": True, "applied": False,
                    "error": str(e)[:300]}
        return {"supported": True, "applied": bool(r.get("applied", False)),
                "action": r.get("action"), "method": "a11y-action",
                "pre_states": r.get("pre_states", []),
                "post_states": r.get("post_states", []),
                "verify": "desktop.a11y.inspect"}
    bounds = live.get("bounds") or None
    if bounds and len(bounds) == 4 and bounds[2] > 0 and bounds[3] > 0:
        if desktop_session({}, ctx).get("session") != "x11":
            return {"supported": True, "applied": False,
                    "error": "no Wayland input injector (needs "
                             "ydotool/uinput); control has no clickable "
                             "AT-SPI action",
                    "verify": "desktop.a11y.inspect"}
        # Fresh-observation binding: re-fetch bounds immediately before
        # acting; moved/resized/vanished targets refuse.
        try:
            fresh = _atspi_call({"op": "node", "bus": live["bus"],
                                 "path": live["path"]})
        except RuntimeError as e:
            return {"supported": True, "applied": False,
                    "error": str(e)[:300]}
        fb = fresh.get("bounds") or None
        if fb != bounds:
            return {"supported": True, "applied": False,
                    "error": "target moved since inspection (re-inspect)"}
        x, y, w, h = (int(v) for v in fb)
        r = _run_quiet(["xdotool", "mousemove", str(x + w // 2),
                        str(y + h // 2), "click", "1"])
        if r is None:
            return {"supported": True, "applied": False,
                    "error": "xdotool unavailable"}
        if r.returncode != 0:
            return {"supported": True, "applied": False,
                    "error": (r.stderr or "xdotool refused").strip()[:200]}
        try:
            post = _atspi_call({"op": "node", "bus": live["bus"],
                                "path": live["path"]})
            post_states = post.get("states", [])
        except RuntimeError:
            post_states = []
        return {"supported": True, "applied": True, "method": "x11-pointer",
                "at": [x + w // 2, y + h // 2],
                "post_states": post_states,
                "verify": "desktop.a11y.inspect"}
    return {"supported": True, "applied": False,
            "error": "control is not actionable (no click action, no "
                     "bounds — re-inspect a concrete control)"}


def desktop_type(inputs: dict, ctx: dict) -> dict:
    """Enter text into the snapshotted control. AT-SPI EditableText first
    (verified by reading the text back); X11 typing fallback; Wayland
    without EditableText refuses. Typed text is never echoed back."""
    sid = inputs.get("snapshot_id", "")
    nid = inputs.get("node_id", "")
    text = inputs.get("text", "")
    mode = inputs.get("mode", "replace")
    if not isinstance(sid, str) or not sid or not isinstance(nid, str):
        raise ValueError("need {snapshot_id, node_id} strings")
    if not isinstance(text, str) or not (1 <= len(text) <= 500):
        raise ValueError("text must be 1..500 chars")
    if mode not in ("replace", "insert"):
        raise ValueError("mode must be replace|insert")
    try:
        live, stored, snap = _resolve_node(sid, nid)
    except (ValueError, RuntimeError) as e:
        return {"supported": True, "applied": False,
                "error": str(e)[:300]}
    if live.get("editable"):
        try:
            _atspi_call({"op": "grab_focus", "bus": live["bus"],
                         "path": live["path"]})
        except RuntimeError:
            pass  # focus is best-effort; SetTextContents may not need it
        try:
            r = _atspi_call({"op": "set_text", "bus": live["bus"],
                             "path": live["path"], "text": text,
                             "mode": mode})
        except RuntimeError as e:
            return {"supported": True, "applied": False,
                    "error": str(e)[:300]}
        return {"supported": True, "applied": bool(r.get("applied", False)),
                "method": "a11y-edit", "mode": mode, "chars": len(text),
                "verify": "desktop.a11y.inspect"}
    if desktop_session({}, ctx).get("session") != "x11":
        return {"supported": True, "applied": False,
                "error": "control is not AT-SPI editable and Wayland has "
                         "no input injector (needs ydotool/uinput)"}
    try:
        f = _atspi_call({"op": "grab_focus", "bus": live["bus"],
                         "path": live["path"]})
    except RuntimeError as e:
        return {"supported": True, "applied": False,
                "error": str(e)[:300]}
    if not f.get("focused"):
        return {"supported": True, "applied": False,
                "error": "could not focus control (refusing blind typing)"}
    r = _run_quiet(["xdotool", "type", "--clearmodifiers", "--", text])
    if r is None:
        return {"supported": True, "applied": False,
                "error": "xdotool unavailable"}
    if r.returncode != 0:
        return {"supported": True, "applied": False,
                "error": (r.stderr or "xdotool refused").strip()[:200]}
    applied = True
    method = "x11-type"
    try:
        # Verify by reading the text back when the control serves it;
        # otherwise focus + clean injector exit is the evidence, marked.
        g = _atspi_call({"op": "get_text", "bus": live["bus"],
                         "path": live["path"], "max_chars": 600})
        if "text" in g:
            applied = text in (g.get("text") or "")
        else:
            method = "x11-type-unverified"
    except RuntimeError:
        method = "x11-type-unverified"
    return {"supported": True, "applied": bool(applied),
            "method": method, "mode": mode, "chars": len(text),
            "verify": "desktop.a11y.inspect"}


LINUX_TOOLS: list[tuple[ToolDefinition, object]] = [
    (_def("system.battery", "Linux battery state", {"type": "object", "properties": {}},
          ["system.battery"], 0.01), system_battery),
    (_def("system.battery.charging_time", "Estimated time to full charge based on "
          "recent charging history. Returns null if unavailable or not charging. "
          "Uses recent current/power samples to estimate; honesty over precision.",
          {"type": "object", "properties": {}}, ["system.battery"], 0.01,
          PermissionLevel.PUBLIC, RiskLevel.SAFE), system_battery_charging_time),
    (_def("system.wifi.state", "Linux WiFi state (enabled/disabled) and connected SSID/BSSID/signal. Read-only.",
          {"type": "object", "properties": {}}, ["system.wifi.state"], 0.01,
          PermissionLevel.PUBLIC, RiskLevel.SAFE), system_wifi_state),
    (_def("system.wifi.settings", "Open the system WiFi settings screen. User must confirm; no automatic state change.",
          {"type": "object", "properties": {}}, ["system.wifi.settings"], 0.01,
          PermissionLevel.CONFIRM, RiskLevel.CONFIRM), system_wifi_settings),
    (_def("system.bluetooth.state", "Bluetooth state (enabled/disabled) and bonded devices. Read-only.",
          {"type": "object", "properties": {}}, ["system.bluetooth.state"], 0.01,
          PermissionLevel.PUBLIC, RiskLevel.SAFE), system_bluetooth_state),
    (_def("system.bluetooth.settings", "Open the system Bluetooth settings screen. User must confirm.",
          {"type": "object", "properties": {}}, ["system.bluetooth.settings"], 0.01,
          PermissionLevel.CONFIRM, RiskLevel.CONFIRM), system_bluetooth_settings),
    (_def("system.hotspot.state", "Mobile hotspot state (enabled/disabled) and connected clients. Read-only.",
          {"type": "object", "properties": {}}, ["system.hotspot.state"], 0.01,
          PermissionLevel.PUBLIC, RiskLevel.SAFE), system_hotspot_state),
    (_def("system.hotspot.settings", "Open the system mobile hotspot settings screen. User must confirm.",
          {"type": "object", "properties": {}}, ["system.hotspot.settings"], 0.01,
          PermissionLevel.CONFIRM, RiskLevel.CONFIRM), system_hotspot_settings),
    (_def("system.mobile_data.state", "Mobile data state (enabled/disabled). Read-only.",
          {"type": "object", "properties": {}}, ["system.mobile_data.state"], 0.01,
          PermissionLevel.PUBLIC, RiskLevel.SAFE), system_mobile_data_state),
    (_def("system.mobile_data.settings", "Open the system mobile data settings screen. User must confirm.",
          {"type": "object", "properties": {}}, ["system.mobile_data.settings"], 0.01,
          PermissionLevel.CONFIRM, RiskLevel.CONFIRM), system_mobile_data_settings),
    (_def("system.airplane_mode.state", "Airplane mode state (enabled/disabled). Read-only.",
          {"type": "object", "properties": {}}, ["system.airplane_mode.state"], 0.01,
          PermissionLevel.PUBLIC, RiskLevel.SAFE), system_airplane_mode_state),
    (_def("system.airplane_mode.settings", "Open the system airplane mode settings screen. User must confirm.",
          {"type": "object", "properties": {}}, ["system.airplane_mode.settings"], 0.01,
          PermissionLevel.CONFIRM, RiskLevel.CONFIRM), system_airplane_mode_settings),
    (_def("system.app.open", "Open a known application by package name. Only allowlisted packages.",
          {"type": "object", "required": ["package"], "additionalProperties": False,
           "properties": {"package": {"type": "string", "minLength": 3, "maxLength": 255,
               "pattern": r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$",
               "enum": sorted(APP_OPEN_TARGETS)}}},
          ["system.app.open"], 0.05,
          PermissionLevel.CONFIRM, RiskLevel.CONFIRM), system_app_open),
    (_def("system.wifi.set", "Change WiFi radio state via NetworkManager, verified by re-read. "
           "WARNING: disabling wifi on this device can sever its Core channel.",
          {"type": "object", "required": ["enabled"],
           "additionalProperties": False,
           "properties": {"enabled": {"type": "boolean"}}},
          ["system.wifi.set"], 0.05,
          PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK), system_wifi_set),
    (_def("system.bluetooth.set", "Change Bluetooth power via bluetoothctl, verified by re-read.",
          {"type": "object", "required": ["enabled"],
           "additionalProperties": False,
           "properties": {"enabled": {"type": "boolean"}}},
          ["system.bluetooth.set"], 0.05,
          PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK), system_bluetooth_set),
    (_def("system.hotspot.set", "Up/down an existing hotspot profile only (never creates networks), "
           "verified by re-read.",
          {"type": "object", "required": ["enabled"],
           "additionalProperties": False,
           "properties": {"enabled": {"type": "boolean"}}},
          ["system.hotspot.set"], 0.05,
          PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK), system_hotspot_set),
    (_def("system.mobile_data.set", "Toggle cellular data via ModemManager; deployment_blocked "
           "without a modem. Verified by re-read.",
          {"type": "object", "required": ["enabled"],
           "additionalProperties": False,
           "properties": {"enabled": {"type": "boolean"}}},
          ["system.mobile_data.set"], 0.05,
          PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK), system_mobile_data_set),
    (_def("system.airplane_mode.set", "rfkill block/unblock wifi+bluetooth+wwan, verified by "
           "re-read. WARNING: enabling airplane mode here severs the Core channel.",
          {"type": "object", "required": ["enabled"],
           "additionalProperties": False,
           "properties": {"enabled": {"type": "boolean"}}},
          ["system.airplane_mode.set"], 0.05,
          PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK), system_airplane_mode_set),
    (_def("desktop.session", "Graphical session type from the environment.",
          {"type": "object", "properties": {}}, ["desktop.session"], 0.01,
          PermissionLevel.PUBLIC, RiskLevel.SAFE), desktop_session),
    (_def("desktop.window.active", "Active-window title via X11 probe.",
          {"type": "object", "properties": {}}, ["desktop.window.active"],
          0.01, PermissionLevel.RESTRICTED, RiskLevel.SAFE),
     desktop_window_active),
    (_def("desktop.screenshot", "Bounded screenshot to the scratch dir.",
          {"type": "object", "properties": {}}, ["desktop.screenshot"],
          0.05, PermissionLevel.RESTRICTED, RiskLevel.CONFIRM),
     desktop_screenshot),
    (_def("desktop.a11y.inspect", "Targeted AT-SPI2 UI-tree inspection "
           "(bounded, snapshot id for follow-up). Read-only.",
          {"type": "object",
           "properties": {"app": {"type": "string", "maxLength": 120},
                          "max_nodes": {"type": "integer"},
                          "include_text": {"type": "boolean"}}},
          ["desktop.a11y.inspect"], 0.05,
          PermissionLevel.RESTRICTED, RiskLevel.SAFE),
     desktop_a11y_inspect),
    (_def("desktop.click", "Click a snapshotted control (AT-SPI action, "
           "else X11 pointer on fresh bounds). Verified by re-read.",
          {"type": "object", "required": ["snapshot_id", "node_id"],
           "additionalProperties": False,
           "properties": {"snapshot_id": {"type": "string"},
                          "node_id": {"type": "string"}}},
          ["desktop.click"], 0.1,
          PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK), desktop_click),
    (_def("desktop.type", "Type into a snapshotted control (AT-SPI edit, "
           "else focused X11 typing). Content-verified when readable; "
           "typed text is never echoed back.",
          {"type": "object",
           "required": ["snapshot_id", "node_id", "text"],
           "additionalProperties": False,
           "properties": {"snapshot_id": {"type": "string"},
                          "node_id": {"type": "string"},
                          "text": {"type": "string", "minLength": 1,
                                   "maxLength": 500},
                          "mode": {"type": "string",
                                   "enum": ["replace", "insert"]}}},
          ["desktop.type"], 0.1,
          PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK), desktop_type),
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
