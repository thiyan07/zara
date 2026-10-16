"""Tracked specialist sessions (code.session.submit/poll/cancel).

Hermetic: process spawning is injected through opencode_tools.SPAWN with
fakes — no real OpenCode runs, no network. Workspaces are tmp dirs with
the module HOME pointed at them. No sleeps-as-sync (events + deadlines).
"""
import threading
import time

import pytest

import core.opencode_tools as ot
from core.models import ExecutionState


@pytest.fixture()
def home_ws(tmp_path, monkeypatch):
    monkeypatch.setattr(ot, "HOME", str(tmp_path))
    (tmp_path / "proj").mkdir()
    with ot._SESSIONS_LOCK:
        ot._SESSIONS.clear()
        ot._WORKSPACE_OWNER.clear()
    yield tmp_path / "proj"
    with ot._SESSIONS_LOCK:
        ot._SESSIONS.clear()
        ot._WORKSPACE_OWNER.clear()


class FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read1(self, n):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class FakeProc:
    """Exit with rc after run_s; terminate()/kill() end it early."""

    def __init__(self, chunks=(), rc=0, run_s=0.2):
        self.stdout = FakeStdout(chunks)
        self._rc = rc
        self._run_s = run_s
        self._done = threading.Event()
        self._end = None
        self.returncode = None
        self.terminated = False
        self.killed = False
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
        self.killed = True
        self.returncode = -9
        self._done.set()


def _spawned(monkeypatch, proc):
    made = []

    def fake_spawn(cmd, cwd):
        assert cmd[0] == "opencode" and cmd[1] == "run"
        made.append((cmd, cwd))
        return proc

    monkeypatch.setattr(ot, "SPAWN", fake_spawn)
    return made


def _await_state(sid, want=("succeeded", "failed", "cancelled", "timed_out"),
                 deadline=10.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        view = ot.code_session_poll({"session_id": sid,
                                     "workspace": "w"}, {})
        if view["state"] in want:
            return view
        time.sleep(0.02)
    return ot.code_session_poll({"session_id": sid, "workspace": "w"}, {})


def test_submit_poll_lifecycle(home_ws, monkeypatch):
    proc = FakeProc(chunks=[b"did the thing\n"], rc=0)
    _spawned(monkeypatch, proc)
    out = ot.code_session_submit(
        {"workspace": str(home_ws), "task": "do the thing please"}, {})
    assert out["state"] == "running"
    assert out["session_id"].startswith("sess-")
    mid = ot.code_session_poll(
        {"session_id": out["session_id"], "workspace": str(home_ws)}, {})
    assert mid["state"] in ("running", "succeeded")
    final = _await_state(out["session_id"])
    assert final["state"] == "succeeded"
    assert final["returncode"] == 0
    assert "did the thing" in final["output_tail"]
    assert "untrusted" in final["note"]


def test_second_submit_to_live_workspace_refused(home_ws, monkeypatch):
    proc = FakeProc(run_s=5.0)
    _spawned(monkeypatch, proc)
    first = ot.code_session_submit(
        {"workspace": str(home_ws), "task": "first long task here"}, {})
    with pytest.raises(ValueError, match="workspace busy"):
        ot.code_session_submit(
            {"workspace": str(home_ws), "task": "second task collides"}, {})
    ot.code_session_cancel(
        {"session_id": first["session_id"], "workspace": str(home_ws)}, {})
    end = time.monotonic() + 10
    while time.monotonic() < end:
        if ot.code_session_poll(
                {"session_id": first["session_id"],
                 "workspace": str(home_ws)}, {})["state"] == "cancelled":
            break
        time.sleep(0.02)
    # Workspace freed: a new submit proceeds.
    proc2 = FakeProc(rc=0)
    _spawned(monkeypatch, proc2)
    again = ot.code_session_submit(
        {"workspace": str(home_ws), "task": "after cancel retries"}, {})
    assert again["state"] == "running"
    ot.code_session_cancel(
        {"session_id": again["session_id"], "workspace": str(home_ws)}, {})


def test_parallel_sessions_in_independent_workspaces(home_ws, monkeypatch):
    other = home_ws.parent / "other"
    other.mkdir()
    procs = [FakeProc(run_s=1.0), FakeProc(run_s=1.0)]
    made = []

    def fake_spawn(cmd, cwd):
        made.append(cwd)
        return procs[len(made) - 1]

    monkeypatch.setattr(ot, "SPAWN", fake_spawn)
    a = ot.code_session_submit(
        {"workspace": str(home_ws), "task": "task A runs here"}, {})
    b = ot.code_session_submit(
        {"workspace": str(other), "task": "task B runs there"}, {})
    assert a["session_id"] != b["session_id"]
    assert _await_state(a["session_id"])["state"] == "succeeded"
    assert _await_state(b["session_id"])["state"] == "succeeded"


def test_cancel_terminates_and_wins(home_ws, monkeypatch):
    proc = FakeProc(run_s=30.0)
    _spawned(monkeypatch, proc)
    out = ot.code_session_submit(
        {"workspace": str(home_ws), "task": "endless task to stop"}, {})
    view = ot.code_session_cancel(
        {"session_id": out["session_id"], "workspace": str(home_ws)}, {})
    assert view["state"] == "cancelled"
    assert proc.terminated is True
    # Idempotent: second cancel returns the terminal view, no error.
    again = ot.code_session_cancel(
        {"session_id": out["session_id"], "workspace": str(home_ws)}, {})
    assert again["state"] == "cancelled"


def test_timeout_kills_and_unlocks(home_ws, monkeypatch):
    proc = FakeProc(run_s=600.0)
    _spawned(monkeypatch, proc)
    out = ot.code_session_submit(
        {"workspace": str(home_ws), "task": "hangs past its budget",
         "timeout_s": 10.0}, {})
    final = _await_state(out["session_id"], deadline=20.0)
    assert final["state"] == "timed_out"
    assert proc.terminated is True


def test_unknown_session_fail_closed(home_ws):
    with pytest.raises(ValueError, match="unknown session"):
        ot.code_session_poll({"session_id": "sess-nope",
                              "workspace": str(home_ws)}, {})
    with pytest.raises(ValueError, match="unknown session"):
        ot.code_session_cancel({"session_id": "sess-nope",
                                "workspace": str(home_ws)}, {})


def test_task_text_never_stored(home_ws, monkeypatch):
    proc = FakeProc(rc=0)
    _spawned(monkeypatch, proc)
    out = ot.code_session_submit(
        {"workspace": str(home_ws),
         "task": "ultrasecret-marker-xyz task content"}, {})
    with ot._SESSIONS_LOCK:
        sess = ot._SESSIONS[out["session_id"]]
        blob = repr(sess.__dict__)
    assert "ultrasecret-marker-xyz" not in blob
    assert "ultrasecret-marker-xyz" not in ot.code_session_poll(
        {"session_id": out["session_id"], "workspace": str(home_ws)}, {}
    )["output_tail"]
    ot.code_session_cancel(
        {"session_id": out["session_id"], "workspace": str(home_ws)}, {})


def test_output_bounded(home_ws, monkeypatch):
    big = [b"x" * 20000, b"y" * 20000]
    proc = FakeProc(chunks=big, rc=0)
    _spawned(monkeypatch, proc)
    out = ot.code_session_submit(
        {"workspace": str(home_ws), "task": "verbose task output"}, {})
    final = _await_state(out["session_id"])
    assert final["state"] == "succeeded"
    assert len(final["output_tail"]) <= ot.SESSION_OUTPUT_TAIL


def test_validation_and_workspace_guards(home_ws, monkeypatch):
    _spawned(monkeypatch, FakeProc())
    with pytest.raises(ValueError, match="task must be"):
        ot.code_session_submit({"workspace": str(home_ws), "task": "short"},
                               {})
    with pytest.raises(ValueError, match="timeout_s"):
        ot.code_session_submit(
            {"workspace": str(home_ws), "task": "valid task text here",
             "timeout_s": 5.0}, {})
    with pytest.raises(PermissionError):
        ot.code_session_submit({"workspace": "/etc", "task": "valid task x"},
                               {})
    with pytest.raises(ValueError, match="session_id"):
        ot.code_session_poll({"session_id": "", "workspace": "w"}, {})


def test_approval_gates_preserved():
    import shutil
    from core.app import build_stack
    stack = build_stack(fabric_db="")
    home = home_ws_root()
    try:
        rec = stack["engine"].submit_async(
            "code.session.submit",
            {"workspace": home, "task": "gated specialist work"},
            "cloud")
        assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
        rec2 = stack["engine"].submit_async(
            "code.session.cancel",
            {"workspace": home, "session_id": "sess-x"}, "cloud")
        assert rec2.state == ExecutionState.WAITING_FOR_PERMISSION
        # Poll is read-only SAFE: runs (and fails closed on unknown id).
        rec3 = stack["engine"].submit_async(
            "code.session.poll",
            {"workspace": home, "session_id": "sess-x"}, "cloud")
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if stack["engine"].get(rec3.id).state \
                    == ExecutionState.FAILED:
                break
            time.sleep(0.02)
        assert stack["engine"].get(rec3.id).state == ExecutionState.FAILED
    finally:
        with ot._SESSIONS_LOCK:
            ot._SESSIONS.clear()
            ot._WORKSPACE_OWNER.clear()
        shutil.rmtree(home, ignore_errors=True)


def home_ws_root():
    import os
    import tempfile
    d = tempfile.mkdtemp(prefix="zara-sess-", dir=os.path.expanduser("~"))
    return d
