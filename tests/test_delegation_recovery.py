"""Delegated-session persistence + safe restart recovery.

Hermetic: fake processes with fake PIDs, injectable identity seams, tmp
SQLite stores. A "restart" is simulated by dropping module state (the way
a fresh process starts empty) and calling recover_delegated_sessions().
No sleeps-as-sync (events + deadlines). The pre-existing live-session
tests in test_opencode_session.py are untouched.
"""
import sqlite3
import threading
import time

import pytest

import core.opencode_tools as ot
from core.models import ExecutionState


@pytest.fixture()
def rec_env(tmp_path, monkeypatch):
    """Tmp HOME workspace + tmp delegation store + fake identity world."""
    home = tmp_path / "home"
    ws = home / "proj"
    ws.mkdir(parents=True)
    monkeypatch.setattr(ot, "HOME", str(home))
    store = ot.DelegationStore(str(tmp_path / "deleg.db"))
    assert store.durable is True
    old_store = ot.configure_delegation(store)
    world = {"ident": {}, "kills": []}  # pid -> token

    def fake_identity(pid):
        return world["ident"].get(pid)

    def fake_alive(pid, ident):
        if pid is None or ident is None:
            return False
        return world["ident"].get(pid) == ident

    def fake_term(pid):
        world["kills"].append(("TERM", pid))
        world["ident"].pop(pid, None)

    def fake_kill(pid):
        world["kills"].append(("KILL", pid))
        world["ident"].pop(pid, None)

    monkeypatch.setattr(ot, "_proc_identity", fake_identity)
    monkeypatch.setattr(ot, "_proc_alive", fake_alive)
    monkeypatch.setattr(ot, "_proc_terminate", fake_term)
    monkeypatch.setattr(ot, "_proc_kill", fake_kill)
    with ot._SESSIONS_LOCK:
        ot._SESSIONS.clear()
        ot._WORKSPACE_OWNER.clear()
    yield {"ws": ws, "store": store, "world": world, "tmp": tmp_path}
    with ot._SESSIONS_LOCK:
        ot._SESSIONS.clear()
        ot._WORKSPACE_OWNER.clear()
    ot.configure_delegation(old_store)


class FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read1(self, n):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class FakeProc:
    def __init__(self, pid, chunks=(), rc=0, run_s=0.2):
        self.pid = pid
        self.stdout = FakeStdout(chunks)
        self._rc = rc
        self._run_s = run_s
        self._done = threading.Event()
        self._end = None
        self.returncode = None
        self.terminated = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        if self._end is None:
            self._end = time.time() + self._run_s
        while time.time() < self._end and not self._done.is_set():
            time.sleep(0.01)
        if not self._done.is_set():
            self.returncode = self._rc
            self._done.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self._done.wait(timeout=timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self):
        self.returncode = -9
        self._done.set()


def _spawn_proc(monkeypatch, world, proc):
    def fake_spawn(cmd, cwd):
        assert cmd[0] == "opencode" and cmd[1] == "run"
        world["ident"][proc.pid] = (f"tick-{proc.pid}", "boot-1",
                                    "/usr/bin/opencode")
        return proc

    monkeypatch.setattr(ot, "SPAWN", fake_spawn)


def _submit(env, monkeypatch, proc, task="tracked specialist work"):
    _spawn_proc(monkeypatch, env["world"], proc)
    return ot.code_session_submit(
        {"workspace": str(env["ws"]), "task": task}, {})


def _poll(sid, ws):
    return ot.code_session_poll({"session_id": sid, "workspace": str(ws)},
                                {})


def _await_state(env, sid, want=("succeeded", "failed", "cancelled",
                                 "timed_out", "orphaned"), deadline=15.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        view = _poll(sid, env["ws"])
        if view["state"] in want:
            return view
        time.sleep(0.05)
    return _poll(sid, env["ws"])


def _restart():
    """Simulate a Core restart: drop in-memory state, recover from store."""
    with ot._SESSIONS_LOCK:
        ot._SESSIONS.clear()
        ot._WORKSPACE_OWNER.clear()
    return ot.recover_delegated_sessions()


# ---------- persist -> restart -> recover a known worker ----------

def test_recover_live_worker_reattaches_without_rerun(rec_env, monkeypatch):
    env = rec_env
    proc = FakeProc(pid=1001, run_s=30.0)
    calls = {"n": 0}
    real_spawn = ot.SPAWN

    def counting_spawn(cmd, cwd):
        calls["n"] += 1
        world = env["world"]
        world["ident"][proc.pid] = ("tick-1001", "boot-1",
                                    "/usr/bin/opencode")
        return proc

    monkeypatch.setattr(ot, "SPAWN", counting_spawn)
    out = ot.code_session_submit({"workspace": str(env["ws"]),
                                  "task": "long tracked work here"}, {})
    sid = out["session_id"]
    assert calls["n"] == 1
    summary = _restart()
    assert summary == {"loaded": 1, "reattached": 1, "orphaned": 0}
    assert calls["n"] == 1, "recovery must never respawn the worker"
    view = _poll(sid, env["ws"])
    assert view["state"] == "running"
    assert view.get("reattached") is True
    assert "reattached after Core restart" in view.get("output_note", "")
    # Workspace lock reconstructed: duplicate submit refused.
    with pytest.raises(ValueError, match="workspace busy"):
        ot.code_session_submit({"workspace": str(env["ws"]),
                                "task": "duplicate after restart"}, {})


def test_worker_exit_during_recovery_becomes_orphaned(rec_env, monkeypatch):
    env = rec_env
    proc = FakeProc(pid=1002, run_s=30.0)
    out = _submit(env, monkeypatch, proc)
    sid = out["session_id"]
    _restart()
    # Worker dies while reattached: outcome unknown -> orphaned, freed.
    env["world"]["ident"].pop(1002, None)
    view = _await_state(env, sid, want=("orphaned",))
    assert view["state"] == "orphaned"
    assert view["returncode"] is None
    # Freed workspace accepts new work (no duplicate of the old task).
    proc2 = FakeProc(pid=1003, rc=0)
    out2 = _submit(env, monkeypatch, proc2, task="fresh work after loss")
    assert out2["session_id"] != sid
    assert _await_state(env, out2["session_id"])["state"] == "succeeded"


def test_restart_with_missing_worker_orphans(rec_env, monkeypatch):
    env = rec_env
    proc = FakeProc(pid=1004, run_s=30.0)
    out = _submit(env, monkeypatch, proc)
    sid = out["session_id"]
    env["world"]["ident"].pop(1004, None)  # dead before restart
    summary = _restart()
    assert summary["orphaned"] == 1
    assert summary["reattached"] == 0
    view = _poll(sid, env["ws"])
    assert view["state"] == "orphaned"
    assert _poll(sid, env["ws"])["state"] == "orphaned"  # stable


def test_pid_reuse_never_reattaches_nor_kills(rec_env, monkeypatch):
    env = rec_env
    proc = FakeProc(pid=1005, run_s=30.0)
    out = _submit(env, monkeypatch, proc)
    sid = out["session_id"]
    _restart()
    # Same PID, different identity (recycled by an unrelated process).
    env["world"]["ident"][1005] = ("tick-9999", "boot-1", "/usr/bin/other")
    summary = ot.recover_delegated_sessions()
    # Already loaded as running? No — first recover reattached (ident
    # matched then); now identity changed mid-flight: monitor orphans.
    view = _await_state(env, sid, want=("orphaned",))
    assert view["state"] == "orphaned"
    assert ("TERM", 1005) not in env["world"]["kills"]
    assert ("KILL", 1005) not in env["world"]["kills"]
    # Cancel after orphaning: no kill attempted, terminal view returned.
    cancelled = ot.code_session_cancel(
        {"session_id": sid, "workspace": str(env["ws"])}, {})
    assert cancelled["state"] == "orphaned"
    assert env["world"]["kills"] == []


def test_pid_reuse_at_recover_time(rec_env, monkeypatch):
    env = rec_env
    proc = FakeProc(pid=1006, run_s=30.0)
    out = _submit(env, monkeypatch, proc)
    sid = out["session_id"]
    # Recycle the PID with a mismatched identity BEFORE restart.
    env["world"]["ident"][1006] = ("tick-other", "boot-2", "/bin/mystery")
    summary = _restart()
    assert summary == {"loaded": 1, "reattached": 0, "orphaned": 1}
    assert _poll(sid, env["ws"])["state"] == "orphaned"


def test_completed_session_survives_restart(rec_env, monkeypatch):
    env = rec_env
    proc = FakeProc(pid=1007, chunks=[b"final answer\n"], rc=0)
    out = _submit(env, monkeypatch, proc)
    sid = out["session_id"]
    assert _await_state(env, sid)["state"] == "succeeded"
    summary = _restart()
    assert summary["reattached"] == 0
    view = _poll(sid, env["ws"])
    assert view["state"] == "succeeded"
    assert view["returncode"] == 0
    assert "final answer" in view["output_tail"]
    assert view.get("reattached") is not True


def test_cancel_reattached_kills_verified_worker_only(rec_env, monkeypatch):
    env = rec_env
    proc = FakeProc(pid=1008, run_s=30.0)
    out = _submit(env, monkeypatch, proc)
    sid = out["session_id"]
    _restart()
    assert _poll(sid, env["ws"])["state"] == "running"
    view = ot.code_session_cancel(
        {"session_id": sid, "workspace": str(env["ws"])}, {})
    assert view["state"] == "cancelled"
    assert ("TERM", 1008) in env["world"]["kills"]
    # Workspace freed by the cancel.
    proc2 = FakeProc(pid=1009, rc=0)
    out2 = _submit(env, monkeypatch, proc2, task="work after cancel")
    assert _await_state(env, out2["session_id"])["state"] == "succeeded"


def test_corrupt_persisted_state_fails_closed(rec_env):
    env = rec_env
    env["store"].save({"id": "sess-bad", "workspace": str(env["ws"]),
                       "binary": "opencode", "pid": None,
                       "proc_start": None, "boot_id": None, "exe": None,
                       "state": "flying", "returncode": None,
                       "timeout_s": 600.0, "started_at": time.time(),
                       "finished_at": None, "updated_at": time.time(),
                       "output_tail": "", "cancel_requested": 0})
    summary = _restart()
    assert summary["loaded"] == 1
    view = _poll("sess-bad", env["ws"])
    assert view["state"] == "orphaned"


def test_storage_failure_degrades_safely(tmp_path, monkeypatch):
    store = ot.DelegationStore(str(tmp_path / "no-such-dir" / "d.db"))
    assert store.durable is False
    old = ot.configure_delegation(store)
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    monkeypatch.setattr(ot, "HOME", str(home))
    proc = FakeProc(pid=1010, rc=0)
    monkeypatch.setattr(ot, "SPAWN", lambda cmd, cwd: proc)
    try:
        out = ot.code_session_submit(
            {"workspace": str(home / "proj"), "task": "works degraded"}, {})
        assert out["state"] == "running"
        end = time.monotonic() + 10
        while time.monotonic() < end:
            v = ot.code_session_poll(
                {"session_id": out["session_id"], "workspace": "w"}, {})
            if v["state"] == "succeeded":
                break
            time.sleep(0.05)
        assert v["state"] == "succeeded"
        assert ot.recover_delegated_sessions() == {"loaded": 0,
                                                  "reattached": 0,
                                                  "orphaned": 0}
    finally:
        with ot._SESSIONS_LOCK:
            ot._SESSIONS.clear()
            ot._WORKSPACE_OWNER.clear()
        ot.configure_delegation(old)


def test_prompts_never_persisted(rec_env, monkeypatch):
    env = rec_env
    proc = FakeProc(pid=1011, rc=0)
    out = _submit(env, monkeypatch, proc,
                  task="ultrasecret-marker-xyz api_key=AAA")
    _await_state(env, out["session_id"])
    con = sqlite3.connect(str(env["tmp"] / "deleg.db"))
    try:
        blob = "\n".join(r[0] for r in con.execute(
            "SELECT COALESCE(output_tail,'') FROM delegated_sessions"))
        cols = [r[1] for r in con.execute(
            "PRAGMA table_info(delegated_sessions)").fetchall()]
    finally:
        con.close()
    assert "ultrasecret-marker-xyz" not in blob
    for banned in ("prompt", "task", "secret", "env", "environ"):
        assert banned not in cols, cols


def test_bounded_retention(rec_env, monkeypatch):
    env = rec_env
    monkeypatch.setattr(ot, "SESSION_TERMINAL_KEEP", 3)
    sids = []
    for i in range(5):
        sub = env["ws"].parent / f"w{i}"
        sub.mkdir(exist_ok=True)
        proc = FakeProc(pid=2000 + i, rc=0)
        _spawn_proc(monkeypatch, env["world"], proc)
        # distinct trees: retention is global, the one-tree lock is not
        # what this test exercises (see the busy-workspace test above).
        out = ot.code_session_submit(
            {"workspace": str(sub), "task": f"quick job {i} done"}, {})
        sids.append(out["session_id"])
    for sid in sids:
        _await_state(env, sid)
    # Pruning is lazy (submit/recover time): one more submit forces it.
    # The pruner itself stays running, so the bound reads exactly.
    sub6 = env["ws"].parent / "w6"
    sub6.mkdir(exist_ok=True)
    proc6 = FakeProc(pid=2999, run_s=30.0)
    _spawn_proc(monkeypatch, env["world"], proc6)
    extra = ot.code_session_submit(
        {"workspace": str(sub6), "task": "prune trigger work"}, {})
    with ot._SESSIONS_LOCK:
        terminal = [s for s in ot._SESSIONS.values()
                    if s.state != "running"]
    assert len(terminal) <= 3, len(terminal)
    con = sqlite3.connect(str(env["tmp"] / "deleg.db"))
    try:
        n = con.execute("SELECT COUNT(*) FROM delegated_sessions WHERE "
                        "state != 'running'").fetchone()[0]
    finally:
        con.close()
    assert n <= 3, n
    ot.code_session_cancel(
        {"session_id": extra["session_id"],
         "workspace": str(sub6)}, {})


def test_build_stack_wires_delegation_store(tmp_path, monkeypatch):
    from core.app import build_stack
    db = str(tmp_path / "deleg.db")
    monkeypatch.setenv("ZARA_DELEGATION_DB", db)
    old = ot.configure_delegation(None)
    try:
        stack = build_stack(fabric_db="")
        assert "delegation" in stack
        assert stack["delegation"].durable is True
        assert stack["delegation"].path == db
    finally:
        monkeypatch.delenv("ZARA_DELEGATION_DB", raising=False)
        with ot._SESSIONS_LOCK:
            ot._SESSIONS.clear()
            ot._WORKSPACE_OWNER.clear()
        ot.configure_delegation(old)
