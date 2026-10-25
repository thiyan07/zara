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
import shutil
import subprocess
import time
from typing import Callable, Optional
from pathlib import Path
from core.models import PermissionLevel, RiskLevel, ToolDefinition
from core.policy import NEVER_AUTO_APPROVE_CAPS

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
                          version="2.0.0",
                          # Never-auto tagging mirrors the core _def:
                          # policy set membership is the single source.
                          never_auto_approve=(
                              name.split(":", 1)[-1]
                              in NEVER_AUTO_APPROVE_CAPS))

# ---------- handlers ----------

# NOTE: `system.info` is intentionally NOT a device tool. The core's local
# `system.info` (supported_devices=["any"]) is the authority for that name;
# device-specific detail is exposed via system.battery/network/processes.read.
# This keeps one unambiguous owner per tool name.
FS_CHECKSUM_CAP = 512 * 1024 * 1024      # 512 MB honest prefix cap


def hash_file_prefix(real: str, algo: str,
                     size_cap: int = FS_CHECKSUM_CAP) -> dict:
    """P5 ㉒: the chunked walk — sha/sha1/md5 over at most `size_cap`
    bytes; a cap-stop makes the digest an honest PREFIX (the caller
    is told compare-unsafety). Pagecache hashing is hundreds of
    MB/s, so the 91 s device slot never binds below the cap."""
    import hashlib as _hashlib
    h = _hashlib.new(algo)
    hashed = 0
    stopped = ""
    with open(real, "rb") as f:
        while True:
            # read exactly up to the cap boundary: the digest then
            # EQUALS the first `size_cap` bytes (bytes_hashed never
            # overshoots into an unstated region)
            chunk = f.read(min(1024 * 1024, size_cap - hashed))
            if not chunk:
                break
            hashed += len(chunk)
            h.update(chunk)
            if hashed >= size_cap:
                stopped = "size cap"
                break
    return {"digest": h.hexdigest(), "bytes_hashed": hashed,
            "stopped": stopped}


def fs_checksum(inputs: dict, ctx: dict) -> dict:
    """P5 ㉒ "sha256 of my file": chunked hashlib digest of ONE
    device-scoped file. Digest only — file content never leaves the
    laptop. Honest cap: if the size cap stops the read, the digest
    is a PREFIX digest and says so (compare-unsafe beyond the cap).
    Symlink-to-sensitive paths are refused by _fs_path like every
    filesystem read surface."""
    algo = (inputs.get("algo") or "sha256").strip().lower()
    if algo not in ("sha256", "sha512", "sha1", "md5"):
        raise ValueError(
            f"unsupported digest '{str(algo)[:20]}' — use sha256 "
            "(default), sha512, sha1, or md5 (name it explicitly)")
    path = (inputs.get("path") or "").strip()
    if not path:
        raise ValueError("path required")
    real = _fs_path(path, allow_external=True)
    try:
        hashed_run = hash_file_prefix(real, algo)
    except OSError as e:
        raise ValueError(f"cannot read {str(path)[:120]}: {e}") from e
    if hashed_run["bytes_hashed"] == 0:
        raise ValueError(f"could not read bytes from {str(path)[:120]}")
    return {"path": real, "algo": algo,
            "digest": hashed_run["digest"],
            "bytes_hashed": hashed_run["bytes_hashed"],
            "stopped": hashed_run["stopped"],
            "detail": ("PREFIX digest — stopped at the 512 MB size "
                       "cap; compare full files with the same cap "
                       "only" if hashed_run["stopped"] else
                       f"full file ({hashed_run['bytes_hashed']} "
                       "bytes hashed)")}


def system_health(inputs: dict, ctx: dict) -> dict:
    """P5 ⑬ (2026-10-23): the laptop executes the health reading;
    the single behavior source is core/builtin_tools.py."""
    from core.builtin_tools import system_health as _h
    return _h(inputs, ctx)


def system_top(inputs: dict, ctx: dict) -> dict:
    """P5 ⑲ (2026-10-24): the laptop executes the process snapshot
    (its /proc is the truth); single behavior source in
    core/builtin_tools.py."""
    from core.builtin_tools import system_top as _h
    return _h(inputs, ctx)


def system_connectivity(inputs: dict, ctx: dict) -> dict:
    """P5 ⑳ (2026-10-24): the laptop executes the layered probes
    (its NIC/resolver/network are the truth); single behavior source
    in core/builtin_tools.py."""
    from core.builtin_tools import system_connectivity as _h
    return _h(inputs, ctx)


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


# ---------- file workflows (find / edit / rename) ----------
# All stay inside the existing boundaries: search reads under already-
# allowed roots only; edit/move act only on scratch-dir files via the
# same dir_fd + O_NOFOLLOW discipline as write/delete. Nothing here
# widens what the agent may see or touch.

SEARCH_MAX_RESULTS = 200
SEARCH_MAX_ENTRIES = 5000
SEARCH_MAX_DEPTH = 6


def filesystem_search(inputs: dict, ctx: dict) -> dict:
    """Bounded read-only filename search under an allowed root.

    - fnmatch glob (case-insensitive): no regex, no arbitrary code.
    - Hidden entries (dot-first) are never enumerated, so private state
      (caches, .ssh, .gnupg) is never listed; SENSITIVE_HINTS matches are
      filtered even where a plain filename would otherwise hit.
    - The walk never follows symlinks, so neither the traversal nor any
      match can be redirected out of the allowed roots.
    - Unreadable directories are counted honestly (``skipped``) instead
      of failing the whole search with a guessed result.
    """
    import fnmatch as _fn
    pattern = inputs.get("pattern", "")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("pattern is required")
    pattern = pattern.strip()
    if len(pattern) > 200 or "\n" in pattern or "\x00" in pattern:
        raise ValueError("pattern must be one short line")
    root = _scoped(inputs.get("path") or HOME)
    try:
        limit = min(max(int(inputs.get("max_results", 50)), 1),
                    SEARCH_MAX_RESULTS)
    except (TypeError, ValueError):
        limit = 50
    hits: list = []
    scanned = 0
    skipped = 0
    unreadable = 0
    truncated = False

    def _count_unreadable(err):
        nonlocal unreadable
        unreadable += 1

    for dirpath, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=_count_unreadable):
        # Never enumerate/enter hidden state; never descend deeper than
        # the bound (pruned in place, so the walk itself stays bounded).
        dirnames[:] = [d for d in sorted(dirnames)
                       if not d.startswith(".")]
        if dirpath[len(root):].count(os.sep) >= SEARCH_MAX_DEPTH:
            dirnames[:] = []
        for name in sorted(filenames):
            if scanned >= SEARCH_MAX_ENTRIES:
                truncated = True
                break
            scanned += 1
            if name.startswith("."):
                skipped += 1
                continue
            full = os.path.join(dirpath, name)
            if not _fn.fnmatch(name.lower(), pattern.lower()):
                continue
            if any(p.search(full) for p in SENSITIVE_HINTS):
                skipped += 1
                continue
            try:
                size = os.stat(full, follow_symlinks=False).st_size
            except OSError:
                size = None
            hits.append({"path": full[:300], "size": size})
            if len(hits) >= limit:
                truncated = True
                break
        if truncated or scanned >= SEARCH_MAX_ENTRIES:
            truncated = truncated or scanned >= SEARCH_MAX_ENTRIES
            break
    return {"root": root, "pattern": pattern, "matches": hits,
            "count": len(hits), "scanned": scanned, "skipped": skipped,
            "unreadable_dirs": unreadable, "truncated": truncated}


def _scratch_open_rd(name: str, pfd: int) -> bytes:
    """Read a scratch file through a NOFOLLOW fd (swap-safe)."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=pfd)
    with os.fdopen(fd, "rb") as f:
        return f.read(SCRATCH_MAX_BYTES + 1)


EXTRACT_MAX_BYTES = 200_000
_EXTRACT_KINDS = {".txt": "text", ".md": "text", ".json": "json",
                  ".csv": "csv"}


def filesystem_extract(inputs: dict, ctx: dict) -> dict:
    """Extract STRUCTURED information from one supported document.

    Read-only, scoped (HOME/tmp), capped (EXTRACT_MAX_BYTES). Supported
    formats: .txt/.md (text stats + head), .json (parse + top-level
    keys or list length), .csv (header, row count, first rows capped).
    Anything else is reported honestly as `supported: False` with the
    reason — never guessed content. Parse errors are surfaced as
    `error`, not silently swallowed. The extract describes the file; it
    never becomes authority and never leaks a sensitive-name file
    (`_scoped` refuses those before any read).
    """
    path = inputs.get("path", "")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path is required")
    path = _scoped(path)
    ext = os.path.splitext(path)[1].lower()
    kind = _EXTRACT_KINDS.get(ext)
    st = os.stat(path)
    size = st.st_size
    out: dict = {"path": path, "size": size, "kind": kind}
    if st.st_mode & 0o170000 != 0o100000:  # regular file only
        out.update(supported=False, error="path is not a regular file")
        return out
    if kind is None:
        out.update(supported=False,
                   error=f"unsupported format for extraction: "
                         f"{ext or '(no extension)'}; supported: "
                         + ", ".join(sorted(_EXTRACT_KINDS)))
        return out
    if size > EXTRACT_MAX_BYTES:
        out.update(supported=False,
                   error=f"file exceeds the {EXTRACT_MAX_BYTES}-byte "
                         f"extraction cap")
        return out
    try:
        with open(path, "rb") as f:
            data = f.read(EXTRACT_MAX_BYTES + 1)
    except OSError as e:
        out.update(supported=False, error=str(e))
        return out
    if len(data) != size:
        out.update(supported=False, error="file changed while reading")
        return out
    text = data.decode("utf-8", errors="replace")
    if kind == "text":
        lines = text.splitlines()
        out.update(supported=True,
                   lines=len(lines),
                   words=len(text.split()),
                   preview="\n".join(lines[:20])[:1000])
        return out
    if kind == "json":
        import json as _json
        try:
            parsed = _json.loads(text)
        except ValueError as e:
            # A broken document is a FAILURE, not a plausible guess.
            raise ValueError(f"invalid JSON in {path}: {str(e)[:200]}")
        if isinstance(parsed, dict):
            out.update(supported=True, top_keys=sorted(parsed.keys())[:50],
                       value_type="object")
        elif isinstance(parsed, list):
            out.update(supported=True, list_length=len(parsed),
                       value_type="array")
        else:
            out.update(supported=True, value_type=type(parsed).__name__)
        return out
    # csv: header + count of data rows + a capped preview (std lib).
    import csv as _csv
    import io as _io
    reader = _csv.reader(_io.StringIO(text))
    rows = list(reader)
    if not rows:
        out.update(supported=True, header=[], data_rows=0, preview=[])
        return out
    out.update(supported=True, header=[c.strip()[:40] for c in rows[0]
                                       and rows[0]][:40],
               data_rows=max(0, len(rows) - 1),
               preview=[r[:10] for r in rows[1:6]])
    return out


def filesystem_edit(inputs: dict, ctx: dict) -> dict:
    """Replace ONE unambiguous substring in an existing scratch file.

    Design (never guesses, never partial):
    - Refuses when the search text is missing, occurs more than once
      (ambiguous → clarify, never first-or-any), or the result would
      exceed the cap.
    - The ORIGINAL is preserved first: `<name>.bak` is written
      exclusively (O_EXCL). An existing .bak is a refusal — a second
      edit must delete the old backup explicitly first, so consecutive
      edits can never destroy the only pre-edit copy.
    - New content lands via a same-directory temp file + os.replace
      (atomic; a crash leaves either the old or the new file, never a
      partial mix).
    - VERIFY: the final file is re-read through a NOFOLLOW fd and must
      be byte-equal to the intended result. Anything else is reported
      as replaced=False with the discrepancy — never silently 'ok'.
    """
    import stat as _stat
    name = _scratch_name(inputs.get("name", ""))
    old = inputs.get("old", "")
    new = inputs.get("new", "")
    if not isinstance(old, str) or not isinstance(new, str):
        raise ValueError("search/replace text must be strings")
    if not old:
        raise ValueError("search text is required")
    old_b = old.encode("utf-8")
    new_b = new.encode("utf-8")
    if len(new_b) > SCRATCH_MAX_BYTES:
        raise ValueError(f"replacement exceeds {SCRATCH_MAX_BYTES} bytes")
    backup_name = name + ".bak"
    pfd, base = _scratch_dir_fd()
    try:
        try:
            content = _scratch_open_rd(name, pfd)
        except FileNotFoundError as e:
            raise ValueError(f"no such scratch file: {name}") from e
        if len(content) > SCRATCH_MAX_BYTES:
            raise ValueError(
                f"file exceeds the {SCRATCH_MAX_BYTES}-byte scratch cap")
        occurrences = content.count(old_b)
        if occurrences == 0:
            raise ValueError(f"search text not found in {name}")
        if occurrences > 1:
            raise ValueError(f"search text occurs {occurrences} times in "
                             f"{name}: clarify which one to change")
        updated = content.replace(old_b, new_b, 1)
        if len(updated) > SCRATCH_MAX_BYTES:
            raise ValueError(
                f"edit result exceeds the {SCRATCH_MAX_BYTES}-byte cap")
        backup_path = None
        if inputs.get("backup", True):
            # O_EXCL: never overwrite an existing backup (pre-edit
            # evidence); overwriting it would destroy the only pre-edit
            # copy of the file.
            exists_bak = True
            try:
                os.stat(backup_name, dir_fd=pfd, follow_symlinks=False)
            except FileNotFoundError:
                exists_bak = False
            if exists_bak:
                raise PermissionError(
                    f"backup already exists: {backup_name} — remove or "
                    f"rename it first (the pre-edit copy is not replaced)")
            bfd = os.open(backup_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                          | os.O_NOFOLLOW, 0o600, dir_fd=pfd)
            with os.fdopen(bfd, "wb") as f:
                f.write(content)
            backup_path = os.path.join(base, backup_name)
        tmp_name = f"{name}.{os.urandom(4).hex()}.tmp"
        tfd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                      | os.O_NOFOLLOW, 0o600, dir_fd=pfd)
        with os.fdopen(tfd, "wb") as f:
            f.write(updated)
        try:
            os.replace(tmp_name, name, src_dir_fd=pfd, dst_dir_fd=pfd)
        finally:
            try:
                os.unlink(tmp_name, dir_fd=pfd)
            except FileNotFoundError:
                pass
        # Verify the outcome by reading the final state back.
        final = _scratch_open_rd(name, pfd)
        st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
        file_is_file = _stat.S_ISREG(st.st_mode)
    finally:
        os.close(pfd)
    verified = file_is_file and final == updated
    return {"path": os.path.join(base, name), "replaced": verified,
            "backup": backup_path, "bytes": len(updated),
            "verified": verified,
            "detail": "" if verified else "editorial verification failed: "
                       "final content differs from the intended result"}


def filesystem_move(inputs: dict, ctx: dict) -> dict:
    """Rename one scratch file within the scratch dir (atomic rename).

    - Conflict-safe: an existing destination returns a structured
      ``conflict: exists`` result instead of overwriting; `overwrite`
      confirms an intentional replacement explicitly.
    - Symlinks are never renamed through, replaced, or created: a
      symlink source is refused, a symlink destination is refused even
      with overwrite (renaming over a link is not the intent).
    - VERIFY: after the rename the source must be absent and the
      destination must be a regular file. Any mismatch is reported as
      moved=False with the discrepancy.
    """
    import stat as _stat
    src = _scratch_name(inputs.get("src", ""))
    dst = _scratch_name(inputs.get("dst", ""))
    if src == dst:
        raise ValueError("source and destination must differ")
    overwrite = bool(inputs.get("overwrite", False))
    pfd, base = _scratch_dir_fd()
    try:
        try:
            s_st = os.stat(src, dir_fd=pfd, follow_symlinks=False)
        except FileNotFoundError:
            raise ValueError(f"no such scratch file: {src}")
        if not _stat.S_ISREG(s_st.st_mode) or \
                _stat.S_ISLNK(s_st.st_mode):
            raise PermissionError(f"only regular files are moved: {src}")
        try:
            d_st = os.stat(dst, dir_fd=pfd, follow_symlinks=False)
            exists, is_link = True, _stat.S_ISLNK(d_st.st_mode)
        except FileNotFoundError:
            exists, is_link = False, False
        if exists and is_link:
            raise PermissionError(
                f"destination is a symlink: {dst} — refused")
        if exists and not overwrite:
            return {"moved": False, "conflict": "exists", "src": src,
                    "dst": dst,
                    "detail": f"{dst} already exists — nothing was "
                              f"changed; choose a different name or "
                              f"confirm overwrite"}
        os.rename(src, dst, src_dir_fd=pfd, dst_dir_fd=pfd)
    finally:
        os.close(pfd)
    gone = not os.path.lexists(os.path.join(base, src))
    landed = os.path.isfile(os.path.join(base, dst))
    verified = gone and landed
    return {"moved": verified, "src": os.path.join(base, src),
            "dst": os.path.join(base, dst), "verified": verified,
            "detail": "" if verified
            else "rename verification failed: endpoints do not match"}


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


# ---------- Universal operator: desktop app discovery + launch ----------

_APP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_APPS_LIMIT = 200
_APP_NAME_CAP = 80


def _desktop_data_dirs() -> list:
    """Application data dirs (xdg), most-authoritative first. Never shell."""
    dirs = []
    xdg = os.environ.get("XDG_DATA_DIRS", "")
    for d in (xdg.split(":") if xdg else ["/usr/local/share", "/usr/share"]):
        d = d.strip()
        if d and not d.startswith("~"):
            dirs.append(os.path.join(d, "applications"))
    home = os.path.expanduser("~")
    dirs.append(os.path.join(home, ".local", "share", "applications"))
    out, seen = [], set()
    for d in dirs:
        if d in seen:
            continue
        seen.add(d)
        out.append(d)
    return out


def _parse_desktop_entry(path) -> list:
    """Minimal .desktop reader: returns app rows, or [] if not a launchable
    application (Type≠Application, Hidden/NoDisplay, or missing Name).
    Exec values are deliberately never returned (untrusted shell args)."""
    rows = []
    try:
        # Cap the read: .desktop files are small, but never trust size.
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(32 * 1024)
    except OSError:
        return rows
    name, app_type = None, None
    hidden = no_display = False
    try_in_desktop = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            try_in_desktop = line.lower().strip("[]") == "desktop entry"
            continue
        if not try_in_desktop or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().lower()
        val = val.strip()
        if key == "name" and not name:
            name = val[:_APP_NAME_CAP] or None
        elif key == "type" and app_type is None:
            app_type = val
        elif key == "hidden" and val.lower() in ("true", "1"):
            hidden = True
        elif key == "nodisplay" and val.lower() in ("true", "1"):
            no_display = True
    if (app_type is not None and app_type.lower() == "application"
            and name and not hidden and not no_display):
        base = os.path.basename(str(path))
        rows.append({"app_id": base[:-8], "label": name})
    return rows


def desktop_apps_list(inputs: dict, ctx: dict) -> dict:
    """Launchable desktop applications (freedesktop .desktop entries):
    app_id + user-visible label, capped, deterministic. Read-only
    metadata — NEVER an authorization to open or automate anything."""
    seen = {}
    truncated = False
    for d in _desktop_data_dirs():
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for fname in entries:
            if not fname.endswith(".desktop") or len(fname) < 16:
                continue
            if len(seen) >= _APPS_LIMIT:
                truncated = True
                break
            path = os.path.join(d, fname)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            for row in _parse_desktop_entry(path):
                if row["app_id"] not in seen:
                    seen[row["app_id"]] = row
        if truncated:
            break
    apps = sorted(seen.values(), key=lambda row: row["app_id"])
    return {"supported": True, "apps": apps, "count": len(apps),
            "truncated": truncated,
            "note": "discovery metadata only; not an authorization to "
                    "open or automate any app"}


def _desktop_app_ids() -> set | None:
    """Known .desktop app ids, or None when nothing readable."""
    ids = set()
    for d in _desktop_data_dirs():
        try:
            for fname in os.listdir(d):
                if fname.endswith(".desktop"):
                    ids.add(fname[:-8])
        except OSError:
            continue
    return ids or None


def _app_matches_name(app_id: str, nm: str) -> bool:
    """Deterministic match between a .desktop app id and an a11y
    registry application name (pure; unit target). Word-bounded and
    dot-segment based — never free-substring, so 'thing' cannot match
    'Something Else'. Legal shapes: id equals the name; an id word
    equals a full name word ('firefox', 'gnome-calculator'); the last
    dot segment of the id equals/prefixes/suffixes the space- and
    hyphen-stripped name ('org.gnome.TextEditor' → 'Text Editor')."""
    aid = str(app_id or "").lower().strip()
    n = str(nm or "").lower().strip()
    if not aid or not n:
        return False
    if aid == n:
        return True
    name_words = [w for w in re.split(r"[\s\-_.]+", n) if w]
    id_words = [w for w in re.split(r"[\s\-_.]+", aid) if w]
    if any(w in name_words for w in id_words):
        return True
    if "." in aid:
        suffix = aid.rsplit(".", 1)[-1].replace("-", "")
        if len(suffix) >= 3:
            stripped = n.replace(" ", "").replace("-", "")
            return (suffix == stripped or stripped.startswith(suffix)
                    or stripped.endswith(suffix))
    return False


def _atspi_apps(app_filter: str = "") -> dict:
    """Fast a11y registry app list (one cheap read op). Raises
    RuntimeError honestly when the bus is unreachable — an action's
    verification layer must never endanger its actuation decision."""
    return _atspi_call({"op": "apps", "app_filter": app_filter,
                        "max_apps": 40})


def desktop_app_open(inputs: dict, ctx: dict) -> dict:
    """Launch one desktop application by .desktop app id via gtk-launch
    (no shell), detached. Verified honestly on BOTH session types:
    X11 by a new wmctrl window id in the bounded re-read; Wayland by
    the application appearing as a NEW entry in the a11y registry
    (`apps` read op — cheap, native, bounded); when verification is
    impossible the result says 'requested', never 'verified'.
    Selecting a launcher from the machine's own menu is a state change:
    CONFIRM per Core policy."""
    app_id = str(inputs.get("app_id", "")).strip()
    if not _APP_ID_RE.fullmatch(app_id):
        return {"supported": True, "applied": False,
                "error": "bad app_id (need an id from desktop.apps.list)"}
    known = _desktop_app_ids()
    if known is None:
        return {"supported": False, "applied": False,
                "error": "no readable .desktop entries (cannot verify "
                         "the launcher exists)"}
    if app_id not in known:
        return {"supported": True, "applied": False,
                "error": "app not installed (not applied)"}
    if _run_quiet(["gtk-launch", "--help"], timeout=5) is None:
        return {"supported": False, "applied": False,
                "error": "gtk-launch unavailable (no app-launch binary)"}
    wayland = desktop_session({}, ctx).get("session") == "wayland"
    before = None
    before_names = None
    if wayland:
        # a11y registry app list BEFORE the launch — verification later
        # requires a NEW entry (same freshness shape as the X11 diff)
        try:
            names = _atspi_apps()
            before_names = set(str(v) for v in names.get("apps", [])
                               if v)
        except RuntimeError:
            before_names = None  # a11y down -> honest "requested" later
    else:
        ids = _wmctrl_ids()
        before = len(ids) if ids is not None else None
    try:
        subprocess.Popen(["gtk-launch", app_id],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as e:
        return {"supported": True, "applied": False,
                "error": f"launch failed: {str(e)[:200]}"}
    # Bounded X11 verification: a NEW window in the re-read is real
    # appearance evidence.
    if not wayland:
        import time
        for _ in range(5):
            time.sleep(0.4)
            after = _wmctrl_ids()
            if after is not None and len(after) > before:
                return {"supported": True, "applied": True,
                        "app_id": app_id,
                        "verify_state": "verified (a new window appeared)"}
        if _wmctrl_ids() is None:
            return {"supported": True, "applied": True, "app_id": app_id,
                    "verify_state": "requested (window list unreadable)"}
        return {"supported": True, "applied": True, "app_id": app_id,
                "verify_state": "requested (no new window within "
                                "bounds; app may be single-instance)"}
    # Bounded Wayland verification: a NEW a11y registry app entry whose
    # name matches the launcher id is real appearance evidence.
    import time
    if before_names is None:
        return {"supported": True, "applied": True, "app_id": app_id,
                "verify_state": "requested (a11y verification "
                                "unavailable; launch was dispatched)"}
    for _ in range(3):
        time.sleep(0.6)
        try:
            names = _atspi_apps()
        except RuntimeError:
            break  # verification layer vanished mid-check -> honest
        fresh = set(str(v) for v in names.get("apps", []) if v)
        for nm in sorted(fresh - before_names):
            if _app_matches_name(app_id, nm):
                return {"supported": True, "applied": True,
                        "app_id": app_id,
                        "verify_state":
                        "verified (new a11y-registry app entry "
                        "matches the launcher)",
                        "app_name": nm}
        before_names |= fresh
    return {"supported": True, "applied": True, "app_id": app_id,
            "verify_state": "requested (no matching registry entry "
                            "within bounds; launch was dispatched)"}


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


def desktop_window_list(inputs: dict, ctx: dict) -> dict:
    """List X11 windows via wmctrl (id + title). Wayland deliberately
    reports unsupported (no foreign-toplevel helper on this path).
    Titles are untrusted data and may be sensitive."""
    if desktop_session({}, ctx).get("session") == "wayland":
        return {"supported": False,
                "error": "no Wayland window list "
                         "(needs a foreign-toplevel helper)"}
    r = _run_quiet(["wmctrl", "-l"])
    if r is None:
        return {"supported": False,
                "error": "wmctrl unavailable (X11 window list)"}
    if r.returncode != 0:
        return {"supported": True, "windows": [],
                "error": (r.stderr or "wmctrl refused").strip()[:200]}
    windows = []
    for line in (r.stdout or "").splitlines()[:100]:
        parts = line.split(None, 3)
        if len(parts) < 3 or not parts[0].startswith("0x"):
            continue
        windows.append({"window_id": parts[0][:18],
                        "title": (parts[3] if len(parts) > 3 else "")
                        [:200] or None})
    return {"supported": True, "windows": windows,
            "note": "window titles may be sensitive; treat as untrusted"}


def _wmctrl_ids() -> set | None:
    """Current X11 window ids via wmctrl, or None when unreadable."""
    r = _run_quiet(["wmctrl", "-l"])
    if r is None or r.returncode != 0:
        return None
    ids = set()
    for line in (r.stdout or "").splitlines()[:200]:
        wid = line.split(None, 1)[0] if line.split() else ""
        if wid.startswith("0x"):
            ids.add(wid.lower())
    return ids


def desktop_window_action(inputs: dict, ctx: dict) -> dict:
    """Act on one X11 window by id (focus/minimize/close), verified by
    re-reading state. Wayland refuses honestly. Close is graceful
    (wmctrl -i -c), never kill -9. Verification evidence (not the click
    return) decides the verdict."""
    if desktop_session({}, ctx).get("session") == "wayland":
        return {"supported": False, "applied": False,
                "error": "no Wayland window control "
                         "(needs a foreign-toplevel helper)"}
    wid = str(inputs.get("window_id", "")).strip().lower()
    action = str(inputs.get("action", "")).strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{1,16}", wid):
        return {"supported": True, "applied": False,
                "error": "bad window_id (need an id from desktop.window.list, "
                         "e.g. 0x01200003)"}
    if action not in ("focus", "minimize", "close"):
        return {"supported": True, "applied": False,
                "error": "unknown action (focus, minimize, or close)"}
    before = _wmctrl_ids()
    if before is not None and wid not in before:
        return {"supported": True, "applied": False,
                "error": "window not present (not applied)"}
    cmd = {"focus": ["xdotool", "windowactivate", wid],
           "minimize": ["xdotool", "windowminimize", wid],
           "close": ["wmctrl", "-i", "-c", wid]}[action]
    r = _run_quiet(cmd)
    if r is None:
        return {"supported": False, "applied": False,
                "error": "window control binary unavailable"}
    if r.returncode != 0:
        return {"supported": True, "applied": False,
                "error": (r.stderr or f"{action} refused").strip()[:200]}
    after = _wmctrl_ids()
    if action == "close":
        if after is not None and wid not in after:
            return {"supported": True, "applied": True,
                    "verify_state": "closed (absent from window list)"}
        return {"supported": True, "applied": False,
                "error": "close unverified (window still listed)"}
    if action == "focus":
        a = _run_quiet(["xdotool", "getactivewindow"])
        if a is not None and a.returncode == 0:
            try:
                active = int((a.stdout or "").strip())
                if int(wid, 16) == active:
                    return {"supported": True, "applied": True,
                            "verify_state": "focused (active window matches)"}
            except ValueError:
                pass
        if after is not None and wid in after:
            return {"supported": True, "applied": True,
                    "verify_state": "requested (window present; "
                                    "active-window match unconfirmed)"}
        return {"supported": True, "applied": False,
                "error": "focus unverified"}
    if after is not None and wid in after:
        return {"supported": True, "applied": True,
                "verify_state": "requested (window present)"}
    return {"supported": True, "applied": False,
            "error": "minimize unverified (window vanished)"}


def desktop_window_active(inputs: dict, ctx: dict) -> dict:
    """Active-window title via X11 (xdotool). Wayland deliberately reports
    unsupported: names the concrete, live-verified denials instead of a
    vague helper complaint (2026-10-19, stock Ubuntu GNOME/Wayland):
    - org.gnome.Shell.Eval -> (false, '') (unsafe-mode gated off),
    - org.gnome.Shell.Introspect.GetWindows -> AccessDenied (portal
      gate denies non-portal callers),
    - no ext-foreign-toplevel protocol export in this compositor.
    Titles are never guessed; enabling Shell unsafe-mode is refused by
    design."""
    if desktop_session({}, ctx).get("session") == "wayland":
        return {"supported": False,
                "error": "no Wayland active-window probe: GNOME Shell "
                         "Eval and Introspect are security-gated and "
                         "this compositor exports no "
                         "foreign-toplevel protocol (requires a "
                         "user-designed helper, never unsafe mode)"}
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

_ATSPI_READ_OPS = frozenset(("node", "tree", "get_text", "apps"))
_ATSPI_BATCH_MAX = 8


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


def _atspi_batch(reqs: list, timeout: int = ATSPY_TIMEOUT_S) -> list:
    """N READ-ONLY a11y ops in ONE helper spawn (operation 'multi').

    Same ops, same validation, same capping as standalone calls; the
    probe structurally refuses state-changing children, and this side
    reassesses that independently — an unrecognised or write-op entry
    fails the whole batch here, before the child ever sees it (never a
    reordered write)."""
    if not isinstance(reqs, list) or not reqs:
        raise RuntimeError("batch must be a non-empty op list")
    if len(reqs) > _ATSPI_BATCH_MAX:
        raise RuntimeError("too many batch ops (%d > %d)"
                           % (len(reqs), _ATSPI_BATCH_MAX))
    for r in reqs:
        if not isinstance(r, dict) or r.get("op") not in _ATSPI_READ_OPS:
            raise RuntimeError("batch children must be read-only ops "
                               "(node/tree/get_text)")
    res = _atspi_call({"op": "multi", "ops": reqs}, timeout)
    out = res.get("results")
    if not isinstance(out, list) or len(out) != len(reqs):
        raise RuntimeError("batch result shape mismatch")
    return out


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


# ---------------- Stage 20: removable storage + file ops ----------------

def _removable_mounts() -> list[dict]:
    """Mounted roots of removable block devices via lsblk (read-only).
    Returns [{mountpoint, device, fs, model, size_bytes, ro}]."""
    txt = _run_quiet(["lsblk", "-J", "-b", "-o", "NAME,RM,RO,SIZE,TYPE,"
                     "MOUNTPOINTS,MOUNTPOINT,FSTYPE,SERIAL,MODEL"])
    out: list[dict] = []
    if txt.returncode != 0 or not txt.stdout:
        return out
    import json as _json
    try:
        data = _json.loads(txt.stdout).get("blockdevices") or []
    except ValueError:
        return out

    def walk(nodes):
        for n in nodes:
            if n.get("type") == "disk" and n.get("rm"):
                mounts = []
                for m in (n.get("mountpoints") or []):
                    if isinstance(m, list):
                        mounts += [x for x in m if x]
                if n.get("mountpoint"):
                    mounts.append(n["mountpoint"])
                for child in (n.get("children") or []):
                    for m in (child.get("mountpoints") or []):
                        if isinstance(m, list):
                            mounts += [x for x in m if x]
                    if child.get("mountpoint"):
                        mounts.append(child["mountpoint"])
                mounts = sorted({m for m in mounts if m and m != "[SWAP]"})
                for m in mounts:
                    out.append({
                        "mountpoint": m, "device":
                            "/dev/" + "".join(n["name"].split()[-1:]),
                        "fs": n.get("fstype") or "unknown",
                        "model": (n.get("model") or "").strip(),
                        "size_bytes": int(n.get("size") or 0),
                        "ro": bool(n.get("ro")),
                    })
            walk(n.get("children") or [])

    walk(data)
    return out


def _fs_roots() -> tuple[str, ...]:
    """Allowed filesystem roots: user HOME, /tmp, plus mounted
    REMOVABLE media only (pendrives/MTP mounts appear when plugged).
    Internal system areas are never writable through these tools."""
    return tuple(ALLOWED_ROOTS) + tuple(
        os.path.realpath(m["mountpoint"]) for m in _removable_mounts())


def _fs_path(path: str, *, allow_external: bool = False) -> str:
    real = os.path.realpath(os.path.expanduser(path))
    if allow_external:
        rem = tuple(os.path.realpath(m["mountpoint"])
                    for m in _removable_mounts())
        in_rem = any(real == r or real.startswith(r + os.sep) for r in rem)
        in_std = any(real == r or real.startswith(r + os.sep)
                     for r in ALLOWED_ROOTS)
        if not (in_std or in_rem):
            raise PermissionError(f"outside allowed roots: {path}")
        if in_std and not in_rem and any(
                p.search(real) for p in SENSITIVE_HINTS):
            raise PermissionError(f"sensitive path refused: {path}")
    else:
        real = _scoped(real)
    return real


def storage_volumes(inputs: dict, ctx: dict) -> dict:
    """Mounted removable volumes with usage numbers. Read-only."""
    import shutil as _sh
    vols = []
    for m in _removable_mounts():
        mp = m["mountpoint"]
        try:
            du = _sh.disk_usage(mp)
            readwrite = _rw_probe(mp)
            vols.append({
                "mountpoint": mp, "device": m["device"],
                "filesystem": m["fs"], "model": m["model"],
                "total_bytes": du.total, "used_bytes": du.used,
                "free_bytes": du.free,
                "total_h": _fmt_size(du.total),
                "used_h": _fmt_size(du.used),
                "free_h": _fmt_size(du.free),
                "read_only": m["ro"] or not readwrite,
            })
        except OSError as e:
            vols.append({"mountpoint": mp, "error": str(e)[:120]})
    return {"volumes": vols,
            "detail": "" if vols else "no removable volumes mounted"}


def _rw_probe(mp: str) -> bool:
    """Non-destructive writability probe: attempt to create ONE
    uniquely-named zero byte file, then delete it. Never flushes a
    queue of data; a mounted-RO volume stays untouched logically."""
    import uuid as _uuid
    probe = os.path.join(mp, f".zara-rw-probe-{_uuid.uuid4().hex}")
    try:
        with open(probe, "xb"):
            pass
        os.unlink(probe)
        return True
    except OSError:
        try:
            os.unlink(probe)
        except OSError:
            pass
        return False


def storage_capacity(inputs: dict, ctx: dict) -> dict:
    import shutil as _sh
    mp = _fs_path(inputs.get("mount") or inputs.get("path") or "",
                  allow_external=True)
    du = _sh.disk_usage(mp)
    return {"mountpoint": mp, "total_bytes": du.total,
            "used_bytes": du.used, "free_bytes": du.free}

def storage_stats(inputs: dict, ctx: dict) -> dict:
    """Removable-volume media statistics (videos/audio/images/documents/
    other): bounded, cancellable, memory-safe. Scans a mount root or a
    subfolder below it."""
    from core.storage_scan import scan_tree
    root = inputs.get("mount") or inputs.get("path") or ""
    real = _fs_path(root, allow_external=True)
    out = scan_tree(real, max_entries=min(
        200_000, max(100, int(inputs.get("max_entries", 200_000)))))
    out.pop("unreadable", None)   # keep the count; drop raw dir list
    out.pop("huge_dirs", None)
    return out


def storage_scan(inputs: dict, ctx: dict) -> dict:
    """P5 ⑰ "where did my disk go": top level-1 children of a home or
    allowed path by subtree size (dust/gdu/dua semantics, stdlib
    re-implementation): apparent-size aggregation, symlinks never
    followed, hard links deduped within a bounded window, bounded by
    a time/entry budget — partial walks report floors, never totals.
    Read-only."""
    from core.storage_scan import scan_dir_usage
    path = (inputs.get("path") or "").strip()
    real = _fs_path(path or "~", allow_external=True)
    top_n = min(20, max(1, int(inputs.get("top_n", 8))))
    budget = min(30.0, max(0.5, float(
        inputs.get("budget_seconds", 4.0))))
    out = scan_dir_usage(real, top_n=top_n, budget_s=budget)
    if "detail" not in out:
        n = len(out["items"])
        out["detail"] = (f"top {n} under {real} by subtree size "
                         f"(apparent size, bounded scan)") if n \
            else f"nothing under {real}"
    return out


def storage_find(inputs: dict, ctx: dict) -> dict:
    """P5 ㉑ "which files are over X" / "my biggest files": the
    FILE-level lens of the disk breakdown. House scope (_fs_path),
    apparent size, bounded walk with floor semantics. Read-only."""
    from core.storage_scan import find_big_files
    path = (inputs.get("path") or "").strip()
    real = _fs_path(path or "~", allow_external=True)
    top_n = min(50, max(1, int(inputs.get("top_n", 10))))
    min_bytes = int(inputs.get("min_bytes", 0))
    budget = min(30.0, max(1.0, float(inputs.get("budget_seconds", 6.0))))
    out = find_big_files(real, min_bytes=min_bytes, top_n=top_n,
                         budget_s=budget)
    if "detail" not in out:
        n = len(out["items"])
        out["detail"] = (f"top {n} file(s) under {real} by apparent "
                         f"size (bounded scan)") if n \
            else (f"no files of that size under {real}")
    return out


def storage_growth(inputs: dict, ctx: dict) -> dict:
    """P5 ㉓ \"what grew on my disk\": the laptop executes the scan +
    ledger diff; single behavior source in core/storage_scan.py
    (this wrapper only makes the _fs_path scope decision). The
    ledger file is assistant-internal state on this host."""
    from core.storage_scan import storage_growth as _h
    path = (inputs.get("path") or "").strip()
    real = _fs_path(path or "~", allow_external=True)
    top_n = min(20, max(1, int(inputs.get("top_n", 10))))
    budget = min(30.0, max(1.0, float(inputs.get("budget_seconds", 30.0))))
    return _h(real, top_n=top_n, budget_s=budget)


def storage_copy(inputs: dict, ctx: dict) -> dict:
    """Copy a file (or directory tree) between allowed roots and
    removable mounts: destination validation FIRST, capacity pre-check,
    conflict policy (default ask -> structured result, never
    overwrites), sha256 verify, bounded. A failed/unverified copy
    leaves the destination partial file REMOVED — never reported as
    success."""
    import hashlib as _hashlib
    import shutil as _sh
    import uuid as _uuid
    src = _fs_path(inputs.get("src") or "", allow_external=True)
    dst = inputs.get("dst") or ""
    on_conflict = inputs.get("on_conflict", "ask")
    if on_conflict not in ("ask", "rename"):
        raise ValueError("on_conflict must be 'ask' or 'rename'")
    dtree = fs_copy_tree
    dst_real = _fs_path(dst, allow_external=True)   # validates dst FIRST
    dst_dir = os.path.dirname(dst_real)
    if os.path.isdir(src):
        return dtree(inputs, src, dst, on_conflict)
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    os.makedirs(dst_dir, exist_ok=True)
    # capacity pre-check on destination
    du = _sh.disk_usage(dst_dir)
    src_size = os.path.getsize(src)
    if du.free < src_size:
        return {"copied": False, "verified": False, "stage": "capacity",
                "needed_bytes": src_size, "free_bytes": du.free,
                "detail": "destination has less free space than the "
                          "source needs — nothing was written"}
    name = os.path.basename(dst_real)
    if os.path.lexists(dst_real):
        if on_conflict == "ask":
            return {"copied": False, "verified": False,
                    "stage": "conflict", "conflict": "exists",
                    "dst": dst_real,
                    "detail": "destination exists — rerun with "
                              "on_conflict='rename' to auto-rename"}
        b, ext = os.path.splitext(name)
        i = 2
        while os.path.lexists(os.path.join(dst_dir, f"{b} ({i}){ext}")):
            i += 1
        name = f"{b} ({i}){ext}"
        dst_real = os.path.join(dst_dir, name)
    tmp = os.path.join(dst_dir, f".zara-copy-{_uuid.uuid4().hex}")
    renamed = name != os.path.basename(inputs.get("dst") or "")
    try:
        with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
            _sh.copyfileobj(fsrc, fdst, 1024 * 1024)
        # verify: sha256 streamed from both sides
        h_src, h_tmp = _hashlib.sha256(), _hashlib.sha256()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h_src.update(chunk)
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h_tmp.update(chunk)
        verified = h_src.hexdigest() == h_tmp.hexdigest()
        if not verified:
            os.unlink(tmp)
            return {"copied": True, "verified": False, "stage": "verify",
                    "detail": "sha256 mismatch — partial file removed",
                    "src": src, "dst": dst_real}
        os.rename(tmp, dst_real)
        return {"copied": True, "verified": True, "stage": "done",
                "src": src, "dst": dst_real,
                "renamed_to": name if renamed else None,
                "size": src_size}
    finally:
        if os.path.lexists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def fs_copy_tree(inputs, src, dst, on_conflict):
    """Directory copy: file-by-file through filesystem_copy semantics,
    per-file verification, abort-on-first-unverified. Source never
    touched. Result carries a per-file manifest (paths only)."""
    import shutil as _sh
    dst_real = _fs_path(dst, allow_external=True)
    du = _sh.disk_usage(os.path.dirname(dst_real) or dst_real)
    total = 0
    for r, _d, files in os.walk(src):
        for fn in files:
            total += os.path.getsize(os.path.join(r, fn))
    if total > du.free:
        return {"copied": False, "verified": False, "stage": "capacity",
                "needed_bytes": total, "free_bytes": du.free,
                "detail": "destination free space below tree size — "
                          "nothing was written"}
    copied, manifest = 0, []
    for r, dirs, files in os.walk(src):
        rel = os.path.relpath(r, src)
        target = os.path.join(dst, rel)
        os.makedirs(_fs_path(target, allow_external=True),
                    exist_ok=True)
        for fn in files:
            c = filesystem_copy({"src": os.path.join(r, fn),
                                 "dst": os.path.join(target, fn),
                                 "on_conflict": on_conflict}, ctx)
            if not (c.get("copied") and c.get("verified")):
                return {"copied": False, "verified": False,
                        "stage": c.get("stage", "verify"),
                        "detail": f"aborted after {copied} files: "
                                  f"{c.get('detail', '')}",
                        "manifest": manifest, "src": src, "dst": dst}
            manifest.append(c["dst"])
            copied += 1
    return {"copied": True, "verified": True, "stage": "done",
            "files": copied, "bytes": total, "manifest": manifest,
            "src": src, "dst": dst}


def storage_move(inputs2: dict, ctx: dict) -> dict:
    """Cross-filesystem safe move: copy + verify THEN delete source
    (only after a verified copy). Same-fs moves rename directly.
    Never deletes on a failed or unverified copy."""
    import shutil as _sh
    src = _fs_path(inputs2.get("src") or "", allow_external=True)
    dst = _fs_path(inputs2.get("dst") or "", allow_external=True)
    if not os.path.lexists(src):
        raise FileNotFoundError(src)
    if dst == src:
        raise ValueError("source and destination must differ")
    same_fs = os.stat(src).st_dev == os.stat(
        os.path.dirname(dst) or dst).st_dev
    if same_fs:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(dst) and inputs2.get("on_conflict") != "rename":
            return {"moved": False, "conflict": "exists", "src": src,
                    "dst": dst}
        if os.path.lexists(dst):
            b, ext = os.path.splitext(dst)
            i = 2
            while os.path.lexists(f"{b} ({i}){ext}"):
                i += 1
            dst = f"{b} ({i}){ext}"
        os.rename(src, dst)
        return {"moved": os.path.lexists(dst) and
                not os.path.lexists(src), "src": src, "dst": dst,
                "verified": True}
    copied = filesystem_copy({"src": src, "dst": dst,
                              "on_conflict": inputs2.get(
                                  "on_conflict", "ask")}, ctx)
    if not (copied.get("copied") and copied.get("verified")):
        return {"moved": False, "stage": copied.get("stage", "verify"),
                "detail": "copy did not verify — source preserved, "
                          "nothing deleted", "src": src,
                "result": copied}
    # verified copy in place: now delete source
    if os.path.isdir(src):
        _sh.rmtree(src)
    else:
        os.unlink(src)
    return {"moved": not os.path.lexists(src), "src": src,
            "dst": copied.get("dst"), "verified": True,
            "renamed_to": copied.get("renamed_to")}


def storage_mkdir(inputs: dict, ctx: dict) -> dict:
    p = _fs_path(inputs.get("path") or "", allow_external=True)
    os.makedirs(p, exist_ok=True)
    return {"created": os.path.isdir(p), "path": p}


def storage_delete(inputs: dict, ctx: dict) -> dict:
    """Delete a file or bounded directory within allowed roots.
    Requires `confirm: true` explicitly in the SAME call; refuses
    otherwise (no defaults), requires the path to be inside allowed
    roots or mounted removable media, and protects against root-level
    asks on internal roots."""
    import shutil as _sh
    if not inputs.get("confirm"):
        return {"deleted": False, "stage": "confirmation_required",
                "detail": "deletion needs explicit confirm:true — "
                          "nothing deleted"}
    p = _fs_path(inputs.get("path") or "", allow_external=True)
    real_roots = (os.path.realpath(HOME), "/tmp")
    in_std = any(p == r or p.startswith(r + os.sep) for r in real_roots)
    if in_std and p in real_roots or p == "/tmp":
        raise PermissionError("refusing to delete an allowed root itself")
    if not os.path.lexists(p):
        raise FileNotFoundError(p)
    if os.path.isdir(p):
        n = 0
        for r, _d, files in os.walk(p):
            n += len(files)
            if n > 100_000:
                raise PermissionError(
                    "directory too large for a single delete — "
                    "refused (delete in smaller batches)")
        _sh.rmtree(p)
        ok = not os.path.lexists(p)
    else:
        os.unlink(p)
        ok = not os.path.lexists(p)
    return {"deleted": ok, "path": p, "verified": ok}


# ---- Stage 21: agent-side relay send/fetch (bytes never pass through
# Core tool envelopes; the relay enforces digests + recipient ack) ----

_XFER_MAX = 5 * 1024 * 1024   # relay single-transfer ceiling (must match core)


def storage_xfer_send(inputs: dict, ctx: dict) -> dict:
    """Send a file within this agent's allowed roots/removable mounts
    to another enrolled device via the Core relay. Digest computed
    locally; each chunk is digest-enforced server-side; bounded wait
    for the recipient's ack. >5 MiB is refused with the precise
    limitation (relay design), never partially sent."""
    agent = ctx.get("agent")
    if agent is None:
        return {"verified": False, "error": "agent transport context "
                "absent (tool must run inside the device agent)"}
    recipient = inputs.get("recipient_device") or ""
    if not recipient:
        return {"verified": False, "error": "recipient_device required"}
    src = _fs_path(inputs.get("src") or "", allow_external=True)
    if not os.path.isfile(src):
        return {"verified": False, "error": f"no such file: {src}"}
    size = os.path.getsize(src)
    if size <= 0 or size > _XFER_MAX:
        return {"verified": False, "error":
                f"relay ceiling {size}B not within 1..{_XFER_MAX} — "
                "nothing sent", "size": size}
    with open(src, "rb") as f:
        data = f.read()
    import hashlib as _hl
    digest = _hl.sha256(data).hexdigest()
    try:
        rec = agent.xfer_request(recipient, os.path.basename(src), data)
        tid = rec.get("transfer_id") or ""
        if not tid:
            return {"verified": False, "error":
                    f"relay refused: {rec.get('error', 'no record')[:150]}"
                    if isinstance(rec, dict) else "relay refused"}
        out = agent.xfer_upload(tid, data)
    except Exception as e:  # noqa: BLE001 — network/relay errors per-item
        return {"verified": False, "error": str(e)[:180],
                "detail": "source untouched"}
    # bounded ack wait (recipient fetch typically lands over long-poll)
    deadline = time.time() + float(inputs.get("ack_wait_s", 45.0))
    acked = False
    state = ""
    while time.time() < deadline:
        try:
            rec = ctx.get("_xfer_probe") or agent  # test hook or default
            r = agent._authed_request("GET",
                                      f"/v1/agent/xfer/{tid}", None)
            state = (r or {}).get("state", "")
            if state == "succeeded":
                acked = True
                break
            if state in ("cancelled", "expired", "failed", "rejected"):
                break
        except Exception:
            state = ""
        time.sleep(1.5)
    return {"uploaded": True, "acked": acked, "verified": True,
            "sha256": digest, "size": size, "transfer_id": tid,
            "status": ("delivered" if acked else
                       "sent_awaiting_fetch"), "relay_state": state}


def storage_xfer_fetch(inputs: dict, ctx: dict) -> dict:
    """Stage-15 fetch, then write the verified bytes into a
    destination directory (allowed roots or removable mounts).
    Conflict policy: auto-rename, never overwrite. Digest double-check
    after write; a mismatch removes the partial file and reports."""
    agent = ctx.get("agent")
    if agent is None:
        return {"verified": False, "error": "agent transport context "
                "absent"}
    tid = inputs.get("transfer_id") or ""
    dst_dir = _fs_path(inputs.get("dst_dir") or "", allow_external=True)
    try:
        payload, ack = agent.xfer_download(tid)
    except Exception as e:  # noqa: BLE001
        return {"verified": False, "error": str(e)[:200]}
    import hashlib as _hl
    digest = _hl.sha256(payload).hexdigest()
    name = inputs.get("filename") or ""
    if not name or os.path.basename(name) != name:
        # derive from the recipient's pending list (no device-scoped
        # single-record GET exists; the old operator-path GET always
        # 404s here and silently fell back to "relay-<id>" names)
        try:
            for r in agent.xfer_pending().get("to_download", []):
                if r.get("transfer_id") == tid:
                    name = (r.get("filename") or "") or name
                    break
        except Exception:
            name = f"relay-{tid[-8:]}"
    if not name or os.path.basename(name) != name:
        name = f"relay-{tid[-8:]}"
    dst = os.path.join(dst_dir, name)
    i = 2
    while os.path.lexists(dst):
        b, ext = os.path.splitext(name)
        dst = os.path.join(dst_dir, f"{b} ({i}){ext}")
        i += 1
    tmp = os.path.join(dst_dir, f".zara-fetch-{os.getpid()}")
    ok = False
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        # verify the written bytes independently
        with open(tmp, "rb") as f:
            h = _hl.sha256()
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != digest:
            os.unlink(tmp)
            return {"verified": False, "error": "post-write digest "
                    "mismatch — partial file removed"}
        os.rename(tmp, dst)
        ok = True
    except OSError as e:
        try:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        return {"verified": False, "error": f"write failed: {e}"[:180]}
    return {"verified": ok, "dst": dst, "sha256": digest,
            "size": len(payload)}


# ---- Stage 21: interactive display config (supported interfaces
# only; preview is read-only, apply is confirm-gated and verified) ----

def display_preview_config(inputs: dict, ctx: dict) -> dict:
    from . import display_linux as _dl
    mode = (inputs or {}).get("mode", "")
    if not mode:
        desc = _dl.describe_config()
        return {"ok": bool(desc.get("available")), **desc}
    out = _dl.preview_config(mode)
    return out


def display_set_config(inputs: dict, ctx: dict) -> dict:
    from . import display_linux as _dl
    mode = (inputs or {}).get("mode", "")
    confirm = bool((inputs or {}).get("confirm", False))
    out = _dl.apply_config(mode, confirm=confirm)
    return out


def display_stream_status(inputs: dict, ctx: dict) -> dict:
    from . import display_linux as _dl
    from . import pipewire_linux as _pw
    from .screencast_linux import engine
    out = _dl.stream_status()
    out["screencast_session"] = engine().status()
    ok, why = _pw.gst_available()
    out["pipewire_media"] = {"usable": ok, "reason": why,
                             "transport": "sd-bus fd + gst pipewiresrc"}
    return out


def display_cast_start(inputs: dict, ctx: dict) -> dict:
    from .screencast_linux import engine
    confirm = bool((inputs or {}).get("confirm", False))
    return engine().start(confirm=confirm)


def display_cast_stop(inputs: dict, ctx: dict) -> dict:
    from .screencast_linux import engine
    return engine().stop()


def bluetooth_devices(inputs: dict, ctx: dict) -> dict:
    from .discovery_linux import _bluetooth_items
    items, meta = _bluetooth_items()
    return {"devices": items, "adapter": meta,
            "note": "read-only listing; pairing/connect are separate "
                    "confirm-gated operations and require paired state"}


_BT_HOOK: Optional[Callable] = None   # test hook: fake btctl runner


def bluetooth_connect(inputs: dict, ctx: dict) -> dict:
    from .discovery_linux import _session_bus_env
    mac = (inputs or {}).get("mac", "").strip().upper().replace(
        "-", ":")
    confirm = bool((inputs or {}).get("confirm", False))
    if not confirm:
        return {"ok": False, "error": "explicit confirm=true required "
                "to change a bluetooth device connection state"}
    if not re.fullmatch(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}", mac or ""):
        return {"ok": False, "error": "mac must be a device address the "
                "bluetooth.devices listing reported (xx:xx:…)"
                if mac else "mac parameter required",
                "nothing_changed": True}
    if not shutil.which("bluetoothctl"):
        return {"ok": False, "error": "bluetoothctl absent — bluetooth "
                "control unavailable on this host"}

    def bt_run(argv, timeout=8.0):
        if _BT_HOOK is not None:
            return _BT_HOOK(argv)
        try:
            r = subprocess.run(argv, capture_output=True,
                               timeout=timeout, text=True,
                               errors="replace", check=False,
                               env=_session_bus_env() or None)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None

    info = bt_run(["bluetoothctl", "info", mac]) or ""
    if not re.search(r"^\s*Name:", info, re.M):
        return {"ok": False, "error": f"device {mac} is not a known "
                "registered device; nothing was changed",
                "note": "discovery of NEW devices is unsupported on the "
                        "operate path — use the OS bluetooth UI to "
                        "pair once"}
    if not re.search(r"^\s*Paired: yes", info, re.M):
        return {"ok": False, "error": f"device {mac} is not paired — "
                "connecting requires pairing first, which needs an "
                "interactive agent and is unsupported here",
                "nothing_changed": True}
    if re.search(r"^\s*Blocked: yes", info, re.M):
        return {"ok": False, "error": f"device {mac} is blocked; "
                "unblocking must be done in the OS UI",
                "nothing_changed": True}
    if re.search(r"^\s*Connected: yes", info, re.M):
        return {"ok": True, "verified": True, "detail": f"device {mac} "
                "was already connected; no change made"}
    connect_desc = bt_run(["bluetoothctl", "connect", mac])
    verify = bt_run(["bluetoothctl", "info", mac]) or ""
    connected = bool(re.search(r"^\s*Connected: yes", verify, re.M))
    return {"ok": connected, "verified": connected,
            "detail": ("connected and verified by re-read" if connected
                       else "connection attempt did not verify: "
                       f"{(connect_desc or 'refused')[:120]} — no "
                       "success claim")}


def audio_sinks(inputs: dict, ctx: dict) -> dict:
    from .audio_linux import audio_status
    return audio_status()


def network_discover(inputs: dict, ctx: dict) -> dict:
    from .netdisco_linux import discover
    raw = (inputs or {}).get("services")
    services = [str(s) for s in raw] if isinstance(raw, list) else None
    return discover(services=services, timeout_=2.0)


def display_receivers(inputs: dict, ctx: dict) -> dict:
    """Receiver discovery through the same bounded mDNS engine; every
    record stays an unverified advertisement and creating a stream
    remains unavailable (consent portal + receiver protocol stack)."""
    from .netdisco_linux import discover
    out = discover(timeout_=2.0,
                   services=["_airplay._tcp", "_raop._tcp",
                             "_googlecast._tcp", "_DIAL-service._tcp",
                             "_spotify-connect._tcp"])
    return {**out,
            "operation_note": "receivers are advertised identities "
            "only; establishing a stream requires user consent and a "
            "receiver protocol stack — neither runs automatically"}


def audio_set_output(inputs: dict, ctx: dict) -> dict:
    from .audio_linux import set_output
    raw = (inputs or {}).get("sink_id")
    confirm = bool((inputs or {}).get("confirm", False))
    try:
        sid = int(raw)
    except (TypeError, ValueError):
        return {"ok": False, "error": "sink_id must be an integer id "
                "from audio.sinks", "nothing_changed": True}
    return set_output(sid, confirm=confirm)


# Stage 31-P2 (2026-10-20): laptop system control — P2a volume/mute/
# brightness, P2b MPRIS playback. All in device/linux/syscontrol_linux.py;
# every SET is re-read-verified on-device (see the module's discipline).
# The confirm contract matches audio.set_output: confirm=False refuses
# without changing anything.

def volume_set(inputs: dict, ctx: dict) -> dict:
    from .syscontrol_linux import volume_set as _h
    return _h(inputs, ctx)


def mute_set(inputs: dict, ctx: dict) -> dict:
    from .syscontrol_linux import mute_set as _h
    return _h(inputs, ctx)


def brightness_read(inputs: dict, ctx: dict) -> dict:
    from .syscontrol_linux import _read_brightness
    return _read_brightness()


def brightness_set(inputs: dict, ctx: dict) -> dict:
    from .syscontrol_linux import brightness_set as _h
    return _h(inputs, ctx)


def now_playing(inputs: dict, ctx: dict) -> dict:
    from .syscontrol_linux import now_playing as _h
    return _h(inputs, ctx)


def calendar_event_remove(inputs: dict, ctx: dict) -> dict:
    """P5 feature-hunt ⑧ (2026-10-23): the laptop executes the
    calendar removal; the single behavior source is
    core/builtin_tools.py (the same function the core-local tests
    pin)."""
    import os as _os
    from core.builtin_tools import calendar_event_remove as _h
    if not _os.environ.get("ZARA_ICS_PATH"):
        return {"ok": False, "committed": False,
                "detail": "no calendar configured on the laptop — set "
                          "ZARA_ICS_PATH once and ask again"}
    return _h(inputs, ctx)


def calendar_event_move(inputs: dict, ctx: dict) -> dict:
    """P5 feature-hunt ⑯ (2026-10-23): the laptop executes the calendar
    move; the single behavior source is core/builtin_tools.py (the
    same function the core-local tests pin)."""
    import os as _os
    from core.builtin_tools import calendar_event_move as _h
    if not _os.environ.get("ZARA_ICS_PATH"):
        return {"ok": False, "committed": False,
                "detail": "no calendar configured on the laptop — set "
                          "ZARA_ICS_PATH once and ask again"}
    return _h(inputs, ctx)


def calendar_event_add(inputs: dict, ctx: dict) -> dict:
    """P5 feature-hunt ⑥ (2026-10-23): the laptop executes the calendar
    append; the single behavior source is core/builtin_tools.py (the
    same function the core-local tests pin)."""
    import os as _os
    from core.builtin_tools import calendar_event_add as _h
    # The agent reads its OWN env — the deployment sets ZARA_ICS_PATH for
    # both the core and the agent (start_core.sh exports it once).
    if not _os.environ.get("ZARA_ICS_PATH"):
        return {"ok": False, "committed": False,
                "detail": "no calendar configured on the laptop — set "
                          "ZARA_ICS_PATH once and ask again"}
    return _h(inputs, ctx)


def playback_control(inputs: dict, ctx: dict) -> dict:
    from .syscontrol_linux import playback_control as _h
    return _h(inputs, ctx)



# tip: `import time` is available at module scope (stdlib import).


LINUX_TOOLS: list[tuple[ToolDefinition, object]] = [
    (_def("storage.volumes", "List mounted removable volumes with usage and "
          "read/write state. Read-only.", {"type": "object", "properties": {}},
          ["storage.read"], 0.05, PermissionLevel.PUBLIC, RiskLevel.SAFE),
     storage_volumes),
    (_def("storage.capacity", "Free/used/total space of a mounted removable "
          "volume or allowed path. Read-only.",
          {"type": "object", "required": ["mount"],
           "properties": {"mount": {"type": "string"}}},
          ["storage.read"], 0.02, PermissionLevel.PUBLIC, RiskLevel.SAFE),
     storage_capacity),
    (_def("storage.stats", "Media-category statistics of removable storage "
          "(videos/audio/images/documents/other) without loading file names "
          "into memory. Read-only.",
          {"type": "object", "required": ["mount"],
           "properties": {"mount": {"type": "string"},
                          "max_entries": {"type": "integer"}}},
          ["storage.read"], 0.4, PermissionLevel.RESTRICTED,
          RiskLevel.SAFE, timeout=90.0),
     storage_stats),
    (_def("storage.scan", "Top level-1 directories of a home/allowed "
          "path by subtree size (apparent size; symlinks never "
          "followed; hard links deduped within a bounded window); "
          "bounded by a time/entry budget — partial walks are floors "
          "and say so. Read-only.",
          {"type": "object", "properties": {
              "path": {"type": "string"},
              "top_n": {"type": "integer"},
              "budget_seconds": {"type": "number"}}},
          ["storage.read"], 0.6, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=91.0),
     storage_scan),
    (_def("storage.find", "P5 ㉑ \"which files are over 1 GB\" / \"my "
          "biggest files\": rank FILE paths under a home/allowed "
          "path by apparent size with an optional size floor; "
          "apparent size; symlinks never followed; hard links "
          "deduped within a bounded window; bounded walk — partials "
          "are floors and say so. Paths are returned (bounded to "
          "top_n). Read-only.",
          {"type": "object", "properties": {
              "path": {"type": "string"},
              "min_bytes": {"type": "integer"},
              "top_n": {"type": "integer"},
              "budget_seconds": {"type": "number"}}},
          ["storage.read"], 0.6, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=91.0),
     storage_find),
    (_def("storage.growth", "P5 ㉓ \"what grew on my disk\": the "
          "current folder breakdown is diffed against the last "
          "saved assistant-internal baseline for the same root, "
          "then saved as the new baseline. Grown/shrunk/new/gone "
          "rows are byte deltas matched by name; a diff over a "
          "PARTIAL scan is refused (floors delta to noise); NO "
          "baseline means an honest first run that saves one. "
          "Aggregate level-1 sizes only are persisted — no file "
          "paths, no content. Read-only toward user data.",
          {"type": "object", "properties": {
              "path": {"type": "string"},
              "top_n": {"type": "integer"},
              "budget_seconds": {"type": "number"}}},
          ["storage.read"], 0.6, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=91.0),
     storage_growth),
    (_def("fs.checksum", "P5 ㉒ chunked digest of one device-scoped "
          "file (sha256 default; sha1/md5/sha512 named explicitly). "
          "The DIGEST is returned — never file content; bytes hashed "
          "are counted. Read-only.",
          {"type": "object", "required": ["path"],
           "properties": {"path": {"type": "string"},
                          "algo": {"type": "string"}}},
          ["filesystem.read"], 0.6, PermissionLevel.RESTRICTED,
          RiskLevel.SAFE, timeout=91.0),
     fs_checksum),
    (_def("storage.copy", "Copy a file or directory between allowed roots "
          "and mounted removable media. Verified (sha256); conflicts ask "
          "first; never overwrites without an explicit policy.",
          {"type": "object", "required": ["src", "dst"],
           "properties": {"src": {"type": "string"},
                          "dst": {"type": "string"},
                          "on_conflict": {"type": "string",
                                          "enum": ["ask", "rename"]}}},
          ["filesystem.write"], 0.2, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM, timeout=120.0),
     storage_copy),
    (_def("storage.move", "Move a file/directory within allowed roots or "
          "removable media: copy+verify then delete source only after a "
          "verified copy; conflicts ask.",
          {"type": "object", "required": ["src", "dst"],
           "properties": {"src": {"type": "string"},
                          "dst": {"type": "string"},
                          "on_conflict": {"type": "string",
                                          "enum": ["ask", "rename"]}}},
          ["filesystem.write"], 0.25, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM, timeout=120.0),
     storage_move),
    (_def("storage.mkdir", "Create a folder under allowed roots or "
          "mounted removable media.",
          {"type": "object", "required": ["path"],
           "properties": {"path": {"type": "string"}}},
          ["filesystem.write"], 0.05, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM),
     storage_mkdir),
    (_def("storage.delete", "Delete a file/folder inside allowed roots or "
          "mounted removable media. Requires explicit confirm in the same "
          "call; large trees refused.",
          {"type": "object", "required": ["path", "confirm"],
           "properties": {"path": {"type": "string"},
                          "confirm": {"type": "boolean"}}},
          ["filesystem.write"], 0.15, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM),
     storage_delete),
    (_def("storage.xfer_send", "Send a file within this device's allowed "
          "roots or removable mounts to another enrolled device through "
          "the Core relay (chunked, digest-enforced; 5 MiB ceiling).",
          {"type": "object", "required": ["src", "recipient_device"],
           "properties": {"src": {"type": "string"},
                          "recipient_device": {"type": "string"},
                          "ack_wait_s": {"type": "number"}}},
          ["files.relay"], 0.3, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM, timeout=120.0),
     storage_xfer_send),
    (_def("calendar.event.add", "Append one NEW event to a configured "
          "ZARA_ICS_PATH calendar (summary + start required; optional "
          "end, description, location, calendar selector). Duplicate "
          "title+start refused; write atomic; re-parse verified. A "
          "WRITE TO OPERATOR DATA: never auto-approved.",
          {"type": "object", "required": ["summary", "dtstart"],
           "properties": {"summary": {"type": "string", "maxLength": 120},
                          "dtstart": {"type": "string",
                                      "pattern": r"^20\d{6}(T\d{6}Z?)?$"},
                          "dtend": {"type": "string",
                                    "pattern": r"^20\d{6}(T\d{6}Z?)?$"},
                          "description": {"type": "string",
                                          "maxLength": 500},
                          "location": {"type": "string", "maxLength": 120},
                          "calendar": {"type": "string", "maxLength": 60},
                          "confirm": {"type": "boolean"}}},
          ["calendar.write"], 0.05, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM),
     calendar_event_add),
    (_def("calendar.event.remove", "Remove exactly ONE matched VEVENT "
          "from a configured ZARA_ICS_PATH calendar (uid, or summary "
          "with an optional dtstart/date pin). Ambiguous titles "
          "refuse with candidates; a repeating parent removes the "
          "whole series and reports it; atomic; re-parse verified.",
          {"type": "object", "required": [],
           "properties": {"uid": {"type": "string", "maxLength": 200},
                          "summary": {"type": "string",
                                      "maxLength": 120},
                          "dtstart": {"type": "string",
                                      "pattern": r"^20\d{6}(T\d{6})?$"},
                          "calendar": {"type": "string",
                                       "maxLength": 60},
                          "confirm": {"type": "boolean"}}},
          ["calendar.write"], 0.05, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM),
     calendar_event_remove),
    (_def("calendar.event.move", "Move ONE matched VEVENT's start on "
          "a configured ZARA_ICS_PATH calendar (uid, or summary with "
          "an optional dtstart/date pin). new_start/full pin, "
          "new_date keeps the time-of-day, new_clock keeps the date; "
          "all-day and timed kinds never mix; duration preserved; "
          "collisions refuse; atomic; re-parse verified.",
          {"type": "object", "required": [],
           "properties": {"uid": {"type": "string", "maxLength": 200},
                          "summary": {"type": "string",
                                      "maxLength": 120},
                          "dtstart": {"type": "string",
                                      "pattern": r"^20\d{6}(T\d{6})?$"},
                          "new_start": {"type": "string",
                                        "pattern":
                                        r"^20\d{6}(T\d{6}Z?)?$"},
                          "new_date": {"type": "string",
                                       "pattern": r"^20\d{6}$"},
                          "new_clock": {"type": "string",
                                        "maxLength": 8},
                          "new_end": {"type": "string",
                                      "pattern":
                                      r"^20\d{6}(T\d{6}Z?)?$"},
                          "calendar": {"type": "string",
                                       "maxLength": 60},
                          "confirm": {"type": "boolean"}}},
          ["calendar.write"], 0.05, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM),
     calendar_event_move),
    (_def("storage.xfer_fetch", "Fetch a completed relay transfer and "
          "write the digest-verified bytes into a destination directory "
          "(auto-rename on conflict; never overwrites).",
          {"type": "object", "required": ["transfer_id", "dst_dir"],
           "properties": {"transfer_id": {"type": "string"},
                          "dst_dir": {"type": "string"},
                          "filename": {"type": "string"}}},
          ["files.relay"], 0.2, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM, timeout=120.0),
     storage_xfer_fetch),
    (_def("display.preview_config", "Read-only display-layout report, or "
          "a compute-only preview of one layout change (never "
          "executes anything).",
          {"type": "object",
           "properties": {"mode": {"type": "string"}}},
          ["display.config"], 0.05, PermissionLevel.PUBLIC,
          RiskLevel.SAFE),
     display_preview_config),
    (_def("display.set_config", "Apply one display layout change "
          "(single|extend_right|duplicate) after an explicit "
          "operator confirm; verified by re-reading geometry.",
          {"type": "object",
           "required": ["mode", "confirm"],
           "properties": {"mode": {"type": "string"},
                          "confirm": {"type": "boolean"}}},
          ["display.config"], 0.2, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM),
     display_set_config),
    (_def("display.stream_status", "Read-only screen-casting status: "
          "portal/Mutter availability, receiver-honesty ledger, and "
          "the current consent-driven session-lifecycle state. The "
          "media streaming operation itself stays unavailable in this "
          "build (exact reason reported).",
          {"type": "object", "properties": {}},
          ["display.config"], 0.05, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=90.0),
     display_stream_status),
    (_def("display.cast_start", "Start a consent-driven screen-sharing "
          "lifecycle through the real XDG ScreenCast portal: creates a "
          "portal session, shows the actual desktop source-selection "
          "consent dialog, and on consent reports the selected sources "
          "plus this build's exact media limitation, then verifies "
          "teardown. Never captures frames; never bypasses consent; "
          "requires confirm.",
          {"type": "object", "required": ["confirm"],
           "properties": {"confirm": {"type": "boolean"}}},
          ["display.config"], 0.2, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM, timeout=90.0),
     display_cast_start),
    (_def("display.cast_stop", "Stop screen-sharing session state and "
          "verify the portal session is really closed (Closed signal "
          "or verified object absence). Idempotent.",
          {"type": "object", "properties": {"confirm": {"type": "boolean"}}},
          ["display.config"], 0.1, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=90.0),
     display_cast_stop),
    (_def("bluetooth.devices", "List registered bluetooth peripherals "
          "with their own advertised services (read-only; never "
          "pairs/scan/connect).",
          {"type": "object", "properties": {}},
          ["bluetooth.observe"], 0.05, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=90.0),
     bluetooth_devices),
    (_def("bluetooth.connect", "Connect an ALREADY-PAIRED bluetooth "
          "device after an explicit operator confirm; verified by "
          "re-reading the adapter state. Pairing of new devices "
          "requires an interactive agent and is unsupported here.",
          {"type": "object", "required": ["mac", "confirm"],
           "properties": {"mac": {"type": "string"},
                          "confirm": {"type": "boolean"}}},
          ["bluetooth.control"], 0.2, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM, timeout=90.0),
     bluetooth_connect),
    (_def("audio.sinks", "Read-only desktop audio routing report "
          "(PipeWire wpctl): devices, sinks, sources, streams and the "
          "current default output.",
          {"type": "object", "properties": {}},
          ["audio.observe"], 0.05, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=90.0),
     audio_sinks),
    (_def("audio.set_output", "Route the default desktop audio output "
          "to a sink id from audio.sinks after an explicit operator "
          "confirm; verified by re-reading the default marker.",
          {"type": "object", "required": ["sink_id", "confirm"],
           "properties": {"sink_id": {"type": "integer"},
                          "confirm": {"type": "boolean"}}},
          ["audio.routing"], 0.2, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM, timeout=90.0),
     audio_set_output),
    (_def("audio.volume.set", "Set the desktop volume (the default audio "
          "output) to `level` (0-100) or adjust by `delta` (volume "
          "points, e.g. 10 = +10). Requires confirm=true; VERIFIED by "
          "re-reading the volume after the change. Nothing is invented: "
          "'applied' means the re-read shows the requested state.",
          {"type": "object",
           "oneOf": [{"required": ["level", "confirm"]},
                     {"required": ["delta", "confirm"]}],
           "properties": {"level": {"type": "number", "minimum": 0,
                                    "maximum": 100},
                          "delta": {"type": "number", "minimum": -100,
                                    "maximum": 100},
                          "confirm": {"type": "boolean"}}},
          ["audio.volume"], 0.08, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM, timeout=90.0),
     volume_set),
    (_def("audio.mute.set", "Mute or unmute the desktop default audio "
          "output (mode: on/off/toggle). Requires confirm=true; "
          "VERIFIED by re-reading the MUTED state after the change.",
          {"type": "object", "required": ["mode", "confirm"],
           "properties": {"mode": {"type": "string",
                                   "enum": ["on", "off", "toggle"]},
                          "confirm": {"type": "boolean"}}},
          ["audio.volume"], 0.08, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM, timeout=90.0),
     mute_set),
    (_def("display.brightness", "Read the laptop backlight level "
          "(sysfs API). Read-only: honest availability report — "
          "'no sysfs backlight device' on machines whose panel is "
          "controlled another way.",
          {"type": "object", "properties": {}},
          ["display.brightness"], 0.03, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=15.0),
     brightness_read),
    (_def("display.brightness.set", "Set the laptop backlight to pct "
          "(1-100). Requires confirm=true; VERIFIED by re-reading the "
          "brightness file after the write. A permission-refused write "
          "is reported plainly — nothing is faked.",
          {"type": "object", "required": ["pct", "confirm"],
           "properties": {"pct": {"type": "number", "minimum": 1,
                                  "maximum": 100},
                          "confirm": {"type": "boolean"}}},
          ["display.brightness"], 0.15, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM, timeout=15.0),
     brightness_set),
    (_def("media.now_playing", "Read-only MPRIS report: every player "
          "that advertised the standard desktop interface on the user "
          "session bus, with its PlaybackStatus. Honest empty answer "
          "when nothing is playing — never fabricated.",
          {"type": "object", "properties": {}},
          ["media.playback"], 0.03, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=15.0),
     now_playing),
    (_def("media.playback", "Desktop media transport for ANY local "
          "player that exports the standard MPRIS interface (browser, "
          "music player, ...): play/pause/play_pause/stop verify by "
          "re-reading PlaybackStatus; next/previous are declared "
          "'sent' honestly (a track-id change is not verifiable "
          "through the status alone). Requires confirm=true.",
          {"type": "object", "required": ["action", "confirm"],
           "properties": {"action": {"type": "string",
                                     "enum": ["play_pause", "play",
                                              "pause", "next",
                                              "previous", "stop"]},
                          "confirm": {"type": "boolean"}}},
          ["media.playback"], 0.08, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM, timeout=15.0),
     playback_control),
    (_def("network.discover", "One bounded mDNS/DNS-SD discovery pass "
          "for ADVERTISED services on the local network (std lib "
          "legacy-unicast probe; .local domain only; no port scan; "
          "results are unverified device claims, never trust).",
          {"type": "object",
           "properties": {"services": {"type": "array",
                                       "items": {"type": "string"}}}},
          ["network.discover"], 0.05, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=90.0),
     network_discover),
    (_def("display.receivers", "Bounded mDNS discovery of advertised "
          "screen-receiver services (AirPlay/GCast/DIAL/RAOP). "
          "Records are unverified claims; stream creation stays "
          "unavailable (portal consent + receiver protocol stack).",
          {"type": "object", "properties": {}},
          ["display.config"], 0.05, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=90.0),
     display_receivers),
    (_def("system.health", "Instantaneous read-only laptop health: "
          "disk free/used percent, RAM total/available/used "
          "percent, 1/5/15-minute load, cores, uptime days. Plain "
          "facts from /proc and statvfs.",
          {"type": "object", "properties": {}}, ["system.health"],
          0.01, PermissionLevel.PUBLIC, RiskLevel.SAFE),
     system_health),
    (_def("system.top", "P5 ⑲ \"what's slowing my laptop\": ONE "
          "non-interactive process snapshot ranked over a short "
          "sampling window — per-process CPU delta (of total system "
          "cpu) and resident memory (RSS); names from cmdline (first "
          "arg, basename, bounded — full command lines deliberately "
          "NOT kept: a terminal's argv can embed secret tokens); "
          "kernel threads shown bracketed; vanished entries skipped "
          "AND counted. Read-only.",
          {"type": "object", "properties": {
              "sort": {"type": "string", "enum": ["cpu", "memory"]},
              "top_n": {"type": "integer"}}},
          ["system.health"], 0.1, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=90.0),
     system_top),
    (_def("system.connectivity", "P5 ⑳ \"is my internet actually "
          "working\": bounded layered probes beyond NIC-up state — "
          "keyless DNS resolution, raw TCP to allowlisted public "
          "relay endpoints, and an end-to-end HTTP(S) 204 check "
          "(DNS+TCP+TLS+HTTP; catches captive portals and proxies). "
          "Descriptive verdicts, measured latencies, bounded waits; "
          "read-only.",
          {"type": "object", "properties": {}},
          ["system.health"], 0.05, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=90.0),
     system_connectivity),
    (_def("system.battery", "Linux battery state",
          {"type": "object", "properties": {}},
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
    (_def("desktop.apps.list", "Launchable desktop applications "
          "(.desktop app_id + label, capped). Read-only discovery "
          "metadata — never an authorization. Linux-only.",
          {"type": "object", "properties": {}}, ["desktop.apps.list"],
          0.01, PermissionLevel.PUBLIC, RiskLevel.SAFE),
     desktop_apps_list),
    (_def("desktop.app.open", "Launch one installed desktop application "
          "by .desktop app_id via gtk-launch (detached, no shell). "
          "Verified by X11 window re-read; Wayland reports requested "
          "honestly. Linux-only.",
          {"type": "object", "required": ["app_id"],
           "additionalProperties": False,
           "properties": {"app_id": {
               "type": "string", "minLength": 1, "maxLength": 128,
               "description": "app_id from desktop.apps.list"}}},
          ["desktop.app.open"], 0.1,
          PermissionLevel.CONFIRM, RiskLevel.CONFIRM),
     desktop_app_open),
    (_def("desktop.window.active", "Active-window title via X11 probe.",
          {"type": "object", "properties": {}}, ["desktop.window.active"],
          0.01, PermissionLevel.RESTRICTED, RiskLevel.SAFE),
     desktop_window_active),
    (_def("desktop.window.list", "X11 window ids + titles via wmctrl. "
          "Read-only; titles are untrusted. Linux-only.",
          {"type": "object", "properties": {}}, ["desktop.window.list"],
          0.01, PermissionLevel.RESTRICTED, RiskLevel.SAFE),
     desktop_window_list),
    (_def("desktop.window.action", "Focus, minimize, or gracefully close "
          "one X11 window by id, verified by re-reading state. Close is "
          "graceful (wmctrl -i -c), never forced. Linux-only.",
          {"type": "object", "required": ["window_id", "action"],
           "additionalProperties": False,
           "properties": {"window_id": {
               "type": "string", "pattern": r"^0x[0-9a-fA-F]{1,16}$",
               "description": "window id from desktop.window.list"},
               "action": {"type": "string",
                          "enum": ["focus", "minimize", "close"]}}},
          ["desktop.window.action"], 0.1,
          PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK),
     desktop_window_action),
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
    (_def("filesystem.search",
          "Find files by name inside an allowed root (read-only, bounded)",
          {"type": "object", "required": ["pattern"],
           "properties": {"pattern": {"type": "string"},
                          "path": {"type": "string"},
                          "max_results": {"type": "integer"}}},
          ["filesystem.search"], 0.05), filesystem_search),
    (_def("filesystem.edit",
          "Replace one unambiguous substring in a scratch file "
          "(atomic, backed up, verified by re-read)",
          {"type": "object", "required": ["name", "old", "new"],
           "properties": {"name": {"type": "string"},
                          "old": {"type": "string"},
                          "new": {"type": "string"},
                          "backup": {"type": "boolean"}}},
          ["filesystem.write"], 0.05,
          perm=PermissionLevel.CONFIRM, risk=RiskLevel.CONFIRM), filesystem_edit),
    (_def("filesystem.extract",
          "Structured extract from one supported document (txt/md, "
          "json, csv); unsupported formats reported honestly",
          {"type": "object", "required": ["path"],
           "properties": {"path": {"type": "string"}}},
          ["filesystem.extract"], 0.04), filesystem_extract),
    (_def("filesystem.move",
          "Rename a scratch file within the scratch dir "
          "(atomic, conflict-safe, verified by re-check)",
          {"type": "object", "required": ["src", "dst"],
           "properties": {"src": {"type": "string"},
                          "dst": {"type": "string"},
                          "overwrite": {"type": "boolean"}}},
          ["filesystem.write"], 0.03,
          perm=PermissionLevel.CONFIRM, risk=RiskLevel.CONFIRM), filesystem_move),
    (_def("shell.safe_readonly", "Allowlisted read-only commands, no shell",
          {"type": "object", "required": ["command"],
           "properties": {"command": {"type": "string"}}},
          ["terminal.safe"], 0.05,
          perm=PermissionLevel.CONFIRM, risk=RiskLevel.CONFIRM), shell_safe_readonly),
]

# Stage 25: document intelligence + consent-based clipboard.
from .docs_linux import DOC_TOOLS
from .clipboard_linux import CLIPBOARD_TOOLS
# Stage 28: host-side Android inventory (detection only, adb-backed).
from .android_adb_linux import ANDROID_ADB_TOOLS
LINUX_TOOLS += DOC_TOOLS + CLIPBOARD_TOOLS + ANDROID_ADB_TOOLS

#: Capabilities the Linux agent advertises — derived from implemented tools.
LINUX_CAPABILITIES = sorted({c for d, _ in LINUX_TOOLS for c in d.required_capabilities})
