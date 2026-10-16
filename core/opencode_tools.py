"""OpenCode integration — a SPECIALIST coding tool, never a security authority.

Zara proposes scoped coding tasks; every operation below runs through the
same validation -> policy -> governor -> router -> execution -> verification
chain. The OpenCode CLI is invoked via `code.session` (synchronous, bounded)
or the tracked trio `code.session.submit` / `code.session.poll` /
`code.session.cancel` (spawn now, read progress, terminate; one live session
per workspace tree). All subprocesses run workspace-scoped, scrubbed env,
bounded; routine inspect/search/test/diff run as deterministic local
implementations.

Refusals: paths outside the workspace, sensitive paths, destructive git
(history rewrites, branch -D, stash drop/clear), test disabling, and any
command outside the allowlist.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional
from .models import PermissionLevel, RiskLevel, ToolDefinition

HOME = str(Path.home())
SENSITIVE = [re.compile(p, re.I) for p in
             (r"\.ssh", r"\.gnupg", r"\.pki", r"credential", r"secret",
              r"private[ _-]?key", r"\btoken\b")]
DESTRUCTIVE_GIT = [re.compile(p) for p in
                   (r"\breset\s+--hard\b", r"\bbranch\s+-D\b",
                    r"\bstash\s+(drop|clear)\b", r"\bclean\s+-fd",
                    r"\bpush\s+--force\b", r"\bfilter-branch\b",
                    r"\brm\b")]


def _sandbox(workspace: str, timeout: float):
    from .sandbox import SandboxPolicy
    return SandboxPolicy(
        roots=(os.path.realpath(workspace),), timeout_s=timeout,
        allowed_binaries=("pytest", "python", "flutter", "npm", "go", "git",
                          "opencode"),
        extra_path_dirs=(os.path.expanduser("~/.local/bin"),))


def _workspace(path: str, workspace: str) -> str:
    ws = os.path.realpath(workspace)
    if not ws.startswith(os.path.realpath(HOME) + os.sep) and ws != os.path.realpath(HOME):
        raise PermissionError("workspace must live under the user HOME")
    real = os.path.realpath(os.path.join(ws, path or "."))
    if real != ws and not real.startswith(ws + os.sep):
        raise PermissionError(f"path escapes workspace: {path}")
    if any(p.search(real) for p in SENSITIVE):
        raise PermissionError(f"sensitive path refused: {path}")
    return real


def _def(name: str, desc: str, schema: dict, cap: str, cost: float,
         perm: PermissionLevel, risk: RiskLevel,
         timeout: float = 60.0, max_input_bytes: int = 4096) -> ToolDefinition:
    props = dict(schema.get("properties", {}))
    props["workspace"] = {"type": "string"}
    schema = dict(schema, required=["workspace"] + schema.get("required", []),
                  properties=props)
    return ToolDefinition(name=name, description=desc, input_schema=schema,
                          output_schema={"type": "object"},
                          required_capabilities=[cap], permission=perm,
                          risk=risk, estimated_cost=cost, timeout_s=timeout,
                          success_criteria="scoped result returned",
                          failure_behavior="fail", verification="none",
                          supported_devices=["cloud", "linux"],
                          reversible=True, version="4.0.0",
                          max_input_bytes=max_input_bytes)


def code_inspect(inputs: dict, ctx: dict) -> dict:
    path = _workspace(inputs.get("path", ""), inputs["workspace"])
    if os.path.isdir(path):
        return {"path": path, "entries": sorted(os.listdir(path))[:200]}
    with open(path, "rb") as f:
        data = f.read(65537)
    return {"path": path, "text": data[:65536].decode("utf-8", "replace"),
            "truncated": len(data) > 65536}


def code_search(inputs: dict, ctx: dict) -> dict:
    root = _workspace("", inputs["workspace"])
    pattern = inputs["pattern"]
    if not isinstance(pattern, str) or not pattern or len(pattern) > 200:
        raise ValueError("pattern must be a short non-empty string")
    rx = re.compile(re.escape(pattern))
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        if any(p.search(dirpath) for p in SENSITIVE):
            continue
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > 1_000_000:
                    continue
                with open(fp, errors="replace") as f:
                    for i, line in enumerate(f):
                        if rx.search(line):
                            hits.append(f"{fp}:{i + 1}:{line.strip()[:200]}")
                            if len(hits) >= 50:
                                return {"hits": hits, "truncated": True}
            except OSError:
                continue
    return {"hits": hits, "truncated": False}


def code_test(inputs: dict, ctx: dict) -> dict:
    """Run an allowlisted test command in the workspace. Executes code, so
    CONFIRM risk: human approval required via the normal gate."""
    root = _workspace("", inputs["workspace"])
    cmd = inputs.get("command", "pytest -q")
    parts = cmd.strip().split()
    if not parts:
        raise ValueError("empty command")
    first = parts[0]
    rest = parts[1:]
    if first == "pytest":
        pass
    elif parts[:3] == ["python", "-m", "pytest"]:
        rest = parts[3:]
    elif parts[:2] == ["flutter", "test"] and len(parts) == 2:
        rest = []
    elif parts[:2] in (["npm", "test"], ["go", "test"]) and len(parts) == 2:
        rest = []
    else:
        raise PermissionError(f"test runner not allowlisted: {first}")
    for tok in rest:
        if tok in ("-q", "-x", "--tb=short"):
            continue
        if tok.startswith("-"):
            raise PermissionError(f"flag refused: {tok}")
        _workspace(tok, inputs["workspace"])  # target must stay inside
    binary = shutil.which(first, path=os.pathsep.join(
        ("/usr/bin", "/bin", os.path.expanduser("~/.local/bin"))))
    if binary is None:
        raise RuntimeError(f"test runner not installed: {first}")
    parts[0] = os.path.basename(binary)
    target = inputs.get("target", "")
    if target:
        t = _workspace(target, inputs["workspace"])
        parts.append(os.path.relpath(t, root))
    from .sandbox import run as _sbrun
    r = _sbrun(parts, _sandbox(inputs["workspace"], 300.0), cwd=root)
    return {"command": " ".join(parts), "returncode": r.returncode,
            "passed": r.returncode == 0, "output": r.output}


def code_diff(inputs: dict, ctx: dict) -> dict:
    root = _workspace("", inputs["workspace"])
    from .sandbox import run as _sbrun
    sb = _sandbox(inputs["workspace"], 30.0)
    for args in (["git", "-C", root, "diff", "--stat"],
                 ["git", "-C", root, "diff"]):
        p = _sbrun(args, sb, cwd=root)
        if p.returncode != 0:
            raise RuntimeError(f"git diff failed: {p.output[:200]}")
    stat = _sbrun(["git", "-C", root, "diff", "--stat"], sb,
                  cwd=root).output[:2000]
    full = _sbrun(["git", "-C", root, "diff"], sb, cwd=root).output[:20000]
    return {"stat": stat, "diff": full}


def code_apply_patch(inputs: dict, ctx: dict) -> dict:
    """Apply a unified diff, HIGH_RISK (approval). Atomic via git apply;
    every touched path must stay inside the workspace."""
    root = _workspace("", inputs["workspace"])
    patch = inputs.get("patch", "")
    if not isinstance(patch, str) or not patch.strip() or len(patch) > 100000:
        raise ValueError("patch must be a non-empty unified diff (<=100KB)")
    touched = re.findall(r"^[+-]{3} [ab]/(.+)$", patch, re.M)
    if not touched:
        raise ValueError("no files found in patch")
    for rel in set(touched):
        _workspace(rel, inputs["workspace"])  # raises on escape/sensitive
    for line in patch.splitlines():
        if line.startswith("command ") or line.startswith("GIT binary patch"):
            raise ValueError("patch contains non-diff directives")
    from .sandbox import run as _sbrun
    sb = _sandbox(inputs["workspace"], 30.0)
    check = _sbrun(["git", "-C", root, "apply", "--check", "-"], sb,
                   cwd=root, input_text=patch)
    if check.returncode != 0:
        raise ValueError(f"patch does not apply: {check.output[:300]}")
    ap = _sbrun(["git", "-C", root, "apply", "-"], sb, cwd=root,
                input_text=patch)
    if ap.returncode != 0:
        raise RuntimeError(f"git apply failed: {ap.output[:300]}")
    return {"applied": True, "files": sorted(set(touched))}


# Runner is injectable for deterministic tests (no real CLI needed there).
Runner = Callable[[list[str], str, float], "Completed"]
Completed = subprocess.CompletedProcess


def _default_runner(cmd: list[str], cwd: str, timeout: float):
    import shutil as _shutil
    from .sandbox import scrub_env
    resolved = _shutil.which(cmd[0], path=os.pathsep.join(
        ("/usr/bin", "/bin", os.path.expanduser("~/.local/bin"))))
    if resolved is None:
        raise RuntimeError("coding binary not installed")
    return subprocess.run([resolved] + cmd[1:], cwd=cwd, capture_output=True,
                          text=True, timeout=timeout, env=scrub_env())


RUNNER: Runner = _default_runner


def code_session(inputs: dict, ctx: dict) -> dict:
    """Delegate ONE bounded coding task to the OpenCode CLI specialist.
    HIGH_RISK + approval. Task text is data to the specialist, and its
    output is UNTRUSTED data back to Zara — verification still applies."""
    root = _workspace("", inputs["workspace"])
    task = inputs.get("task", "")
    if not isinstance(task, str) or not (10 <= len(task) <= 2000):
        raise ValueError("task must be 10..2000 chars")
    binary = inputs.get("binary", "opencode")
    if "/" in binary or binary not in ("opencode",):
        raise PermissionError("only the configured coding binary allowed")
    cmd = [binary, "run", task]  # cwd carries the workspace; no --auto flag
    try:
        p = RUNNER(cmd, root, timeout=600)
    except FileNotFoundError:
        raise RuntimeError("coding binary not installed")
    out = ((p.stdout or "") + (p.stderr or ""))[-8000:]
    return {"returncode": p.returncode, "output": out,
            "note": "specialist output is untrusted; verify before acting"}


# ---------- tracked specialist sessions (submit -> poll -> cancel) ----------
#
# code.session above is synchronous fire-and-forget (blocks the worker up
# to 600s, no progress, no cancel, concurrent tasks can corrupt one tree).
# These three tools add the missing lifecycle WITHOUT a second registry,
# policy, or memory system: approval, capabilities, audit, and execution
# semantics come from the normal chain (HIGH_RISK submit, read-only poll).
#
# Design: submit spawns the specialist and returns an id immediately; a
# per-session daemon thread collects output and enforces the timeout; poll
# serves bounded tails; cancel terminates (TERM, then KILL after grace).
# One live session per workspace real-path: a second submit to a locked
# tree is refused instead of interleaving mutations. Records never store
# the task text (no prompts in logs); output is untrusted data, capped.

import threading as _threading
import time as _time
import uuid as _uuid

SESSION_STATES = ("running", "succeeded", "failed", "cancelled", "timed_out")
SESSION_OUTPUT_KEEP = 32768
SESSION_OUTPUT_TAIL = 8000
SESSION_TIMEOUT_DEFAULT = 600.0
SESSION_TIMEOUT_MAX = 600.0
SESSION_KILL_GRACE_S = 5.0


SESSION_STATES = ("running", "succeeded", "failed", "cancelled",
                  "timed_out", "orphaned")
# "orphaned" = worker gone without an observed outcome (Core restart with
# an unverifiable or dead process, PID reuse, etc.). It is terminal and
# truthful: the outcome is UNKNOWN, never presented as success. There is
# deliberately no "queued" (submit spawns immediately) and no
# "cancelling" (cancel terminates synchronously).


class DelegatedSession:
    """One specialist process + its lifecycle. All mutations under the
    module lock; blocking waits happen on the worker thread only."""

    def __init__(self, session_id: str, workspace: str, binary: str,
                 timeout_s: float) -> None:
        self.id = session_id
        self.workspace = workspace
        self.binary = binary
        self.timeout_s = timeout_s
        self.state = "running"
        self.returncode: Optional[int] = None
        self.started_at = _time.time()
        self.finished_at: Optional[float] = None
        self.cancel_requested = False
        # Cross-restart process identity (PID alone is never trusted):
        # pid + (proc_start, boot_id, exe) recorded at spawn. Reattached
        # sessions (recovery) verify all three before tracking resumes.
        self.pid: Optional[int] = None
        self.proc_start: Optional[str] = None
        self.boot_id: Optional[str] = None
        self.exe: Optional[str] = None
        self.reattached = False
        self._output = ""
        self._proc = None

    def ident(self) -> Optional[tuple]:
        if self.pid is None or self.proc_start is None:
            return None
        return (self.proc_start, self.boot_id, self.exe)

    def append_output(self, text: str) -> None:
        self._output = (self._output + text)[-SESSION_OUTPUT_KEEP:]

    def tail(self) -> str:
        return self._output[-SESSION_OUTPUT_TAIL:]

    def view(self) -> dict:
        v = {"session_id": self.id, "workspace": self.workspace,
             "state": self.state, "returncode": self.returncode,
             "output_tail": self.tail(),
             "started_at": self.started_at,
             "finished_at": self.finished_at,
             "note": "specialist output is untrusted; verify before acting"}
        if self.reattached:
            v["reattached"] = True
            v["output_note"] = ("reattached after Core restart: live output "
                                "streaming unavailable; last persisted tail "
                                "shown; outcome verified only while the "
                                "recorded worker is positively identified")
        return v

    def _row(self) -> dict:
        """Persisted metadata ONLY: no task text, no env, bounded tail."""
        return {"id": self.id, "workspace": self.workspace,
                "binary": self.binary, "pid": self.pid,
                "proc_start": self.proc_start, "boot_id": self.boot_id,
                "exe": self.exe, "state": self.state,
                "returncode": self.returncode, "timeout_s": self.timeout_s,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "updated_at": _time.time(),
                "output_tail": self.tail(),
                "cancel_requested": 1 if self.cancel_requested else 0}

    @staticmethod
    def from_row(row: dict) -> "DelegatedSession":
        sess = DelegatedSession(row["id"], row["workspace"],
                                row.get("binary", "opencode"),
                                float(row.get("timeout_s") or
                                      SESSION_TIMEOUT_DEFAULT))
        sess.pid = row.get("pid")
        sess.proc_start = row.get("proc_start")
        sess.boot_id = row.get("boot_id")
        sess.exe = row.get("exe")
        sess.state = row.get("state", "running")
        sess.returncode = row.get("returncode")
        sess.started_at = float(row.get("started_at") or _time.time())
        sess.finished_at = row.get("finished_at")
        sess.cancel_requested = bool(row.get("cancel_requested", 0))
        sess._output = (row.get("output_tail") or "")[-SESSION_OUTPUT_KEEP:]
        if sess.state not in SESSION_STATES:
            sess.state = "orphaned"  # corrupt persisted state fails closed
        return sess


_SESSIONS: dict[str, DelegatedSession] = {}
_SESSIONS_LOCK = _threading.Lock()
_WORKSPACE_OWNER: dict[str, str] = {}  # workspace real-path -> live session id


def _spawn_proc(cmd: list[str], cwd: str):
    """Process-spawn indirection (test seam): production uses Popen with a
    scrubbed environment; tests inject fakes. Binary pipes (the worker
    decodes); never shell=True."""
    from .sandbox import scrub_env
    resolved = shutil.which(cmd[0], path=os.pathsep.join(
        ("/usr/bin", "/bin", os.path.expanduser("~/.local/bin"))))
    if resolved is None:
        raise RuntimeError("coding binary not installed")
    return subprocess.Popen([resolved] + cmd[1:], cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=False, env=scrub_env())


SPAWN = _spawn_proc


def _proc_identity(pid: int) -> Optional[tuple]:
    """Test seam: (proc_start_tick, boot_id, exe) for a PID, or None when
    unreadable/gone. Production reads /proc; a PID match alone is never
    accepted as proof — start-tick + boot-id defeat PID reuse, and the exe
    guards against an unrelated process holding a recycled identity."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            after_comm = f.read().rsplit(")", 1)[1].split()
        # fields after pid+comm start at field 3 (state); starttime is
        # field 22 -> index 19.
        start_tick = after_comm[19]
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot = f.read().strip()
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            exe = f.read().split(b"\0")[0].decode(errors="replace")
        if not start_tick or not boot:
            return None
        return (start_tick, boot, exe)
    except Exception:  # noqa: BLE001 — gone/unreadable fails closed
        return None


def _proc_alive(pid: Optional[int], ident: Optional[tuple]) -> bool:
    """Test seam: True only when the PID exists AND its identity matches
    the recorded one exactly."""
    if pid is None or ident is None:
        return False
    try:
        return _proc_identity(pid) == ident
    except Exception:  # noqa: BLE001
        return False


def _proc_terminate(pid: int) -> None:
    """Test seam: SIGTERM a reattached worker (no Popen handle survives a
    Core restart). Callers must re-verify identity immediately before."""
    import signal as _signal
    os.kill(pid, _signal.SIGTERM)


def _proc_kill(pid: int) -> None:
    """Test seam: SIGKILL a reattached worker."""
    import signal as _signal
    os.kill(pid, _signal.SIGKILL)


class DelegationStore:
    """Durable session metadata (SQLite, AuditLog-style). :memory: when no
    path is configured. NEVER raises: storage failure degrades to
    in-memory-only operation so delegation keeps working and recovery is
    simply unavailable. Methods are internally locked; callers must still
    snapshot session state under _SESSIONS_LOCK first and never hold that
    lock across these calls."""

    SCHEMA = """
CREATE TABLE IF NOT EXISTS delegated_sessions (
  id TEXT PRIMARY KEY, workspace TEXT NOT NULL, binary TEXT NOT NULL,
  pid INTEGER, proc_start TEXT, boot_id TEXT, exe TEXT,
  state TEXT NOT NULL, returncode INTEGER, timeout_s REAL NOT NULL,
  started_at REAL NOT NULL, finished_at REAL, updated_at REAL NOT NULL,
  output_tail TEXT DEFAULT '', cancel_requested INTEGER DEFAULT 0
);
"""

    def __init__(self, path: str = ":memory:") -> None:
        import sqlite3 as _sqlite3
        self.path = path
        self._db = None
        self._lock = _threading.Lock()
        try:
            self._db = _sqlite3.connect(path, check_same_thread=False)
            with self._lock, self._db:
                self._db.executescript(self.SCHEMA)
        except Exception:  # noqa: BLE001 — degraded, see class docstring
            self._db = None

    @property
    def durable(self) -> bool:
        return self._db is not None

    def save(self, row: dict) -> None:
        if self._db is None:
            return
        try:
            with self._lock, self._db:
                self._db.execute(
                    "INSERT INTO delegated_sessions (id, workspace, binary,"
                    " pid, proc_start, boot_id, exe, state, returncode,"
                    " timeout_s, started_at, finished_at, updated_at,"
                    " output_tail, cancel_requested) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET workspace=excluded.workspace,"
                    " binary=excluded.binary, pid=excluded.pid,"
                    " proc_start=excluded.proc_start, boot_id=excluded.boot_id,"
                    " exe=excluded.exe, state=excluded.state,"
                    " returncode=excluded.returncode,"
                    " timeout_s=excluded.timeout_s,"
                    " started_at=excluded.started_at,"
                    " finished_at=excluded.finished_at,"
                    " updated_at=excluded.updated_at,"
                    " output_tail=excluded.output_tail,"
                    " cancel_requested=excluded.cancel_requested",
                    (row["id"], row["workspace"], row.get("binary",
                                                           "opencode"),
                     row.get("pid"), row.get("proc_start"),
                     row.get("boot_id"), row.get("exe"), row["state"],
                     row.get("returncode"), row.get("timeout_s",
                                                    SESSION_TIMEOUT_DEFAULT),
                     row.get("started_at"), row.get("finished_at"),
                     row.get("updated_at"),
                     (row.get("output_tail") or "")[:SESSION_OUTPUT_TAIL],
                     row.get("cancel_requested", 0)))
        except Exception:  # noqa: BLE001 — degrade, never break delegation
            pass

    def load_all(self) -> list[dict]:
        if self._db is None:
            return []
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, workspace, binary, pid, proc_start, boot_id,"
                    " exe, state, returncode, timeout_s, started_at,"
                    " finished_at, updated_at, output_tail,"
                    " cancel_requested FROM delegated_sessions"
                    " ORDER BY started_at").fetchall()
        except Exception:  # noqa: BLE001
            return []
        keys = ("id", "workspace", "binary", "pid", "proc_start", "boot_id",
                "exe", "state", "returncode", "timeout_s", "started_at",
                "finished_at", "updated_at", "output_tail",
                "cancel_requested")
        return [dict(zip(keys, r)) for r in rows]

    def delete_old_terminals(self, keep: int) -> int:
        """Bounded growth: keep only the newest `keep` terminal rows."""
        if self._db is None or keep < 0:
            return 0
        try:
            with self._lock, self._db:
                cur = self._db.execute(
                    "SELECT id FROM delegated_sessions WHERE state != "
                    "'running' ORDER BY COALESCE(finished_at, updated_at)"
                    " DESC LIMIT -1 OFFSET ?", (keep,))
                ids = [r[0] for r in cur.fetchall()]
                for sid in ids:
                    self._db.execute(
                        "DELETE FROM delegated_sessions WHERE id = ?", (sid,))
                return len(ids)
        except Exception:  # noqa: BLE001
            return 0


_STORE: Optional[DelegationStore] = None
SESSION_TERMINAL_KEEP = 100


def configure_delegation(store: Optional[DelegationStore]) -> Optional[DelegationStore]:
    """Attach the durable store (called once from build_stack; tests pass
    tmp stores). Returns the previous store. None = in-memory only."""
    global _STORE
    old = _STORE
    _STORE = store
    return old


def _store_save(sess: DelegatedSession) -> None:
    """Write-through one session snapshot. Snapshots under the module
    lock, writes outside it — no I/O under locks. Never raises."""
    store = _STORE
    if store is None:
        return
    with _SESSIONS_LOCK:
        row = sess._row()
    try:
        store.save(row)
    except Exception:  # noqa: BLE001
        pass


def _prune_sessions_locked(max_terminal: Optional[int] = None) -> None:
    """Bounded retention for the in-memory map (caller holds the lock).
    Store pruning happens outside the lock via the store handle."""
    if max_terminal is None:
        max_terminal = SESSION_TERMINAL_KEEP
    terminal = [(sid, s.finished_at or s.started_at)
                for sid, s in _SESSIONS.items()
                if s.state != "running"]
    if len(terminal) <= max_terminal:
        return
    terminal.sort(key=lambda item: item[1])
    for sid, _ in terminal[:len(terminal) - max_terminal]:
        _SESSIONS.pop(sid, None)


def _prune_store() -> int:
    """Bounded persisted growth, outside all locks. Never raises."""
    store = _STORE
    if store is None:
        return 0
    try:
        return store.delete_old_terminals(SESSION_TERMINAL_KEEP)
    except Exception:  # noqa: BLE001
        return 0


def _finish_locked(sess: DelegatedSession, state: str,
                   returncode: Optional[int]) -> None:
    sess.state = state
    sess.returncode = returncode
    sess.finished_at = _time.time()
    if _WORKSPACE_OWNER.get(sess.workspace) == sess.id:
        del _WORKSPACE_OWNER[sess.workspace]


def _finalize_and_persist(sess: DelegatedSession, state: str,
                          returncode: Optional[int]) -> None:
    """Locked finish + write-through persistence outside the lock. Crash
    between the two leaves a persisted `running` row for a finished worker;
    restart reconciliation then finds a dead process and marks it orphaned
    (outcome unknown) — never a false success."""
    with _SESSIONS_LOCK:
        if sess.state != "running":
            return
        _finish_locked(sess, state, returncode)
    _store_save(sess)


def _watch_session(sess: DelegatedSession) -> None:
    """Worker threads: one drains the pipe (blocking short reads on a
    daemon thread — pipes return available bytes, no deadline to miss),
    the other enforces the timeout and finalizes + unlocks. No select
    games, no lock held across process waits."""
    proc = sess._proc
    assert proc is not None and proc.stdout is not None

    def _drain() -> None:
        try:
            read1 = getattr(proc.stdout, "read1",
                            lambda n: proc.stdout.read(n))
            while True:
                data = read1(65536)
                if not data:
                    return
                with _SESSIONS_LOCK:
                    sess.append_output(
                        data.decode("utf-8", "replace")
                        if isinstance(data, bytes) else str(data))
        except Exception:  # noqa: BLE001 — pipe gone, monitor finalizes
            return

    drainer = _threading.Thread(target=_drain, daemon=True)
    drainer.start()
    try:
        deadline = sess.started_at + sess.timeout_s
        while proc.poll() is None:
            if _time.time() >= deadline:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=SESSION_KILL_GRACE_S)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:  # noqa: BLE001 — already gone
                            pass
                except Exception:  # noqa: BLE001 — already gone
                    pass
                break
            _time.sleep(0.2)
        try:
            proc.wait(timeout=SESSION_KILL_GRACE_S)
        except Exception:  # noqa: BLE001
            pass
        drainer.join(timeout=SESSION_KILL_GRACE_S)
        rc = proc.returncode
        with _SESSIONS_LOCK:
            live = sess.state == "running"
            was_cancelled = sess.cancel_requested
        # Finalize via the persisting helper (re-locks; never hold the
        # module lock across it). A cancel landing here still wins: the
        # helper no-ops once cancel finalized first, and cancel's flag
        # steers a first finalize to `cancelled`.
        if live:
            if was_cancelled:
                _finalize_and_persist(sess, "cancelled", rc)
            elif rc is None:
                _finalize_and_persist(sess, "failed", None)
            elif _time.time() >= deadline and rc != 0:
                _finalize_and_persist(sess, "timed_out", rc)
            else:
                _finalize_and_persist(
                    sess, "succeeded" if rc == 0 else "failed", rc)
    except Exception as e:  # noqa: BLE001 — worker must never die silent
        with _SESSIONS_LOCK:
            sess.append_output(f"\n[session monitor error: {e}]\n")
            live = sess.state == "running"
        if live:
            _finalize_and_persist(sess, "failed", None)


def _session_timeout(inputs: dict) -> float:
    try:
        timeout_s = float(inputs.get("timeout_s",
                                     SESSION_TIMEOUT_DEFAULT))
    except (TypeError, ValueError):
        raise ValueError("timeout_s must be a number")
    if not 10.0 <= timeout_s <= SESSION_TIMEOUT_MAX:
        raise ValueError(
            f"timeout_s must be 10..{SESSION_TIMEOUT_MAX:.0f}")
    return timeout_s


def code_session_submit(inputs: dict, ctx: dict) -> dict:
    """Spawn a tracked specialist session; returns an id immediately.
    HIGH_RISK + approval (same bar as code.session). Refuses when the
    workspace tree already hosts a live session."""
    root = _workspace("", inputs["workspace"])
    task = inputs.get("task", "")
    if not isinstance(task, str) or not (10 <= len(task) <= 2000):
        raise ValueError("task must be 10..2000 chars")
    binary = inputs.get("binary", "opencode")
    if "/" in binary or binary not in ("opencode",):
        raise PermissionError("only the configured coding binary allowed")
    timeout_s = _session_timeout(inputs)
    cmd = [binary, "run", task]
    with _SESSIONS_LOCK:
        owner = _WORKSPACE_OWNER.get(root)
        if owner is not None and _SESSIONS.get(owner) is not None \
                and _SESSIONS[owner].state == "running":
            raise ValueError(
                f"workspace busy: session {owner} is running there; "
                f"poll or cancel it first instead of interleaving mutations")
        try:
            proc = SPAWN(cmd, root)
        except FileNotFoundError:
            raise RuntimeError("coding binary not installed")
        sess = DelegatedSession(f"sess-{_uuid.uuid4().hex[:12]}", root,
                                binary, timeout_s)
        sess._proc = proc
        sess.pid = getattr(proc, "pid", None)
        _SESSIONS[sess.id] = sess
        _WORKSPACE_OWNER[root] = sess.id
        _prune_sessions_locked()
    # Process identity for cross-restart reconciliation (fast /proc reads,
    # outside the module lock). A PID alone is never trusted: start-tick +
    # boot-id defeat reuse, exe guards recycled identities.
    if sess.pid is not None:
        try:
            ident = _proc_identity(sess.pid)
        except Exception:  # noqa: BLE001 — seamed in tests
            ident = None
        if ident is not None:
            with _SESSIONS_LOCK:
                sess.proc_start, sess.boot_id, sess.exe = ident
    _store_save(sess)
    _prune_store()
    t = _threading.Thread(target=_watch_session, args=(sess,), daemon=True)
    t.start()
    return {"session_id": sess.id, "workspace": root, "state": "running",
            "timeout_s": timeout_s}


def code_session_poll(inputs: dict, ctx: dict) -> dict:
    """Read-only progress/result for a session id. Unknown ids fail closed
    (no listing: session output may contain code excerpts)."""
    sid = inputs.get("session_id", "")
    if not isinstance(sid, str) or not sid:
        raise ValueError("session_id must be a non-empty string")
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(sid)
        if sess is None:
            raise ValueError(f"unknown session: {sid[:64]}")
        return sess.view()


def code_session_cancel(inputs: dict, ctx: dict) -> dict:
    """Terminate a live session (TERM, KILL after grace). Terminal sessions
    are a no-op returning current state (idempotent). Frees the workspace."""
    sid = inputs.get("session_id", "")
    if not isinstance(sid, str) or not sid:
        raise ValueError("session_id must be a non-empty string")
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(sid)
        if sess is None:
            raise ValueError(f"unknown session: {sid[:64]}")
        if sess.state != "running":
            return sess.view()
        sess.cancel_requested = True  # cancel wins over worker finalize
        proc = sess._proc
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(sid)
        if sess is None:
            raise ValueError(f"unknown session: {sid[:64]}")
        if sess.state != "running":
            return sess.view()
        sess.cancel_requested = True  # cancel wins over worker finalize
        proc = sess._proc
        pid = sess.pid
        ident = sess.ident()
    if proc is not None:
        # Live session: terminate via the process handle, outside the lock.
        try:
            proc.terminate()
            try:
                proc.wait(timeout=SESSION_KILL_GRACE_S)
            except Exception:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001 — already gone
                    pass
        except Exception:  # noqa: BLE001 — already gone
            pass
        _finalize_and_persist(sess, "cancelled",
                              getattr(proc, "returncode", None))
        with _SESSIONS_LOCK:
            return sess.view()
    # Reattached session (post-restart): no handle survives. Re-verify
    # identity IMMEDIATELY before signalling — never kill on PID alone.
    # A recycled identity (or any verification failure) fails safe to
    # `orphaned`: no unrelated process is ever signalled.
    try:
        verified = _proc_alive(pid, ident)
    except Exception:  # noqa: BLE001 — seamed in tests
        verified = False
    if not verified:
        _finalize_and_persist(sess, "orphaned", None)
        with _SESSIONS_LOCK:
            return sess.view()
    try:
        _proc_terminate(pid)
        end = _time.time() + SESSION_KILL_GRACE_S
        while _time.time() < end:
            try:
                if not _proc_alive(pid, ident):
                    break
            except Exception:  # noqa: BLE001
                break
            _time.sleep(0.2)
        try:
            if _proc_alive(pid, ident):
                _proc_kill(pid)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 — gone or unkillable
        pass
    _finalize_and_persist(sess, "cancelled", None)
    with _SESSIONS_LOCK:
        return sess.view()


def _watch_reattached(sess: DelegatedSession) -> None:
    """Liveness monitor for a restart-reattached session: no output pipe
    survives a Core restart, so this only tracks verified liveness (by
    recorded identity, never PID alone), the timeout deadline, and the
    cancel flag. Worker exit -> `orphaned` (outcome unknown, never a
    false success). Tick 1s, daemon, exits once terminal."""
    deadline = sess.started_at + sess.timeout_s
    while True:
        _time.sleep(1.0)
        with _SESSIONS_LOCK:
            if sess.state != "running":
                return
            cancelled = sess.cancel_requested
            pid, ident = sess.pid, sess.ident()
        if cancelled:
            _finalize_and_persist(sess, "cancelled", None)
            return
        try:
            alive = _proc_alive(pid, ident)
        except Exception:  # noqa: BLE001
            alive = False
        if not alive:
            _finalize_and_persist(sess, "orphaned", None)
            return
        if _time.time() >= deadline:
            # Overdue with no output pipe to drain: signal by verified
            # identity only, then record timed_out.
            try:
                _proc_terminate(pid)
            except Exception:  # noqa: BLE001
                pass
            _finalize_and_persist(sess, "timed_out", None)
            return


def recover_delegated_sessions(
        store: Optional[DelegationStore] = None) -> dict:
    """Core-startup reconciliation (called once from build_stack when a
    delegation DB is configured; tests call it directly). Loads persisted
    sessions, resumes tracking ONLY for positively identified workers,
    orphans the rest. Never restarts or reruns a task, never kills
    anything. Returns a summary {loaded, reattached, orphaned}."""
    target = store if store is not None else _STORE
    summary = {"loaded": 0, "reattached": 0, "orphaned": 0}
    if target is None:
        return summary
    try:
        rows = target.load_all()
    except Exception:  # noqa: BLE001 — storage failure fails safe
        return summary
    summary["loaded"] = len(rows)
    for row in rows:
        try:
            sess = DelegatedSession.from_row(row)
        except Exception:  # noqa: BLE001 — corrupt row skipped, never loaded
            continue
        if sess.state != "running":
            with _SESSIONS_LOCK:
                if sess.id not in _SESSIONS:
                    _SESSIONS[sess.id] = sess
            continue
        try:
            alive = _proc_alive(sess.pid, sess.ident())
        except Exception:  # noqa: BLE001
            alive = False
        if not alive:
            sess.state = "orphaned"
            sess.finished_at = _time.time()
            with _SESSIONS_LOCK:
                _SESSIONS[sess.id] = sess
            _store_save(sess)
            summary["orphaned"] += 1
            continue
        sess.reattached = True
        with _SESSIONS_LOCK:
            if sess.id in _SESSIONS and \
                    _SESSIONS[sess.id].state == "running":
                # A live session with the same id already exists in this
                # process (double-boot into one store): keep the live one,
                # leave the persisted row untouched.
                continue
            _SESSIONS[sess.id] = sess
            _WORKSPACE_OWNER[sess.workspace] = sess.id
        t = _threading.Thread(target=_watch_reattached, args=(sess,),
                              daemon=True)
        t.start()
        summary["reattached"] += 1
    with _SESSIONS_LOCK:
        _prune_sessions_locked()
    _prune_store()
    return summary


OPENCODE_TOOLS: list[tuple[ToolDefinition, object]] = [
    (_def("code.inspect", "Read file / list dir inside a workspace",
          {"type": "object",
           "properties": {"path": {"type": "string"}}},
          "code.read", 0.05, PermissionLevel.RESTRICTED, RiskLevel.SAFE),
     code_inspect),
    (_def("code.search", "Literal code search inside a workspace",
          {"type": "object", "required": ["pattern"],
           "properties": {"pattern": {"type": "string"}}},
          "code.search", 0.1, PermissionLevel.RESTRICTED, RiskLevel.SAFE),
     code_search),
    (_def("code.test", "Run allowlisted tests in a workspace (executes code)",
          {"type": "object",
           "properties": {"command": {"type": "string"},
                          "target": {"type": "string"}}},
          "code.test", 0.4, PermissionLevel.CONFIRM, RiskLevel.CONFIRM,
          timeout=320.0), code_test),
    (_def("code.diff", "Git diff of a workspace",
          {"type": "object", "properties": {}}, "code.read", 0.05,
          PermissionLevel.RESTRICTED, RiskLevel.SAFE), code_diff),
    (_def("code.apply_patch", "Apply unified diff in workspace (gated)",
          {"type": "object", "required": ["patch"],
           "properties": {"patch": {"type": "string"}}},
          # Envelope mirrors the handler's own 100KB contract
          # (code_apply_patch enforces length itself); validator default
          # would otherwise shrink a shipped contract. Never authority.
          "code.patch", 0.2, PermissionLevel.HIGH_RISK, RiskLevel.HIGH_RISK,
          max_input_bytes=102400),
     code_apply_patch),
    (_def("code.session", "Bounded specialist coding session (gated)",
          {"type": "object", "required": ["task"],
           "properties": {"task": {"type": "string"}}},
          "code.session", 0.6, PermissionLevel.HIGH_RISK, RiskLevel.HIGH_RISK,
          timeout=590.0), code_session),
    (_def("code.session.submit", "Spawn a tracked specialist session; "
          "returns an id immediately (gated)",
          {"type": "object", "required": ["task"],
           "properties": {"task": {"type": "string"},
                          "timeout_s": {"type": "number"}}},
          "code.session", 0.6, PermissionLevel.HIGH_RISK,
          RiskLevel.HIGH_RISK, timeout=30.0), code_session_submit),
    (_def("code.session.poll", "Read-only progress/result for a session id",
          {"type": "object", "required": ["session_id"],
           "properties": {"session_id": {"type": "string"}}},
          "code.session.read", 0.01, PermissionLevel.RESTRICTED,
          RiskLevel.SAFE, timeout=30.0), code_session_poll),
    (_def("code.session.cancel", "Terminate a live specialist session",
          {"type": "object", "required": ["session_id"],
           "properties": {"session_id": {"type": "string"}}},
          "code.session", 0.05, PermissionLevel.CONFIRM,
          RiskLevel.CONFIRM, timeout=30.0), code_session_cancel),
]
