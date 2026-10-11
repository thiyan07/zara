"""Linux device agent notices server-side job cancellation."""
from core.jobs import JobQueue
from device.linux.agent import DeviceAgent


def _agent(device_id="lap-1", checker=None):
    a = DeviceAgent("http://127.0.0.1:9", device_id,
                    key_file="/tmp/zara-test.key",
                    cancel_checker=checker)
    a._key = "k"
    return a


def _stub_agent(job, checker, tool_fn=None, posts=None):
    posts = posts if posts is not None else []
    a = _agent(checker=checker)
    a.poll_job = lambda: job  # noqa: E731 — stub network poll
    if tool_fn is not None:
        a._tools = {"t.echo": tool_fn}
    def _post(path, payload, timeout=15):
        posts.append((path, dict(payload)))
        return {}
    a._post = _post  # noqa: E731 — capture result reports offline
    return a, posts


def test_job_state_for_owner_other_unknown():
    q = JobQueue()
    job = q.enqueue("lap-1", "t.echo", {})
    assert q.job_state_for("lap-1", job.id) == "pending"
    q.poll("lap-1")
    assert q.job_state_for("lap-1", job.id) == "claimed"
    assert q.job_state_for("other-1", job.id) is None
    assert q.job_state_for("lap-1", "job-nope") is None


def test_agent_skips_execution_when_pre_cancelled():
    calls = []
    def tool(inputs, ctx):
        calls.append(1)
        return {"x": 1}
    job = {"job_id": "job-1", "tool": "t.echo", "inputs": {}}
    a, posts = _stub_agent(job, checker=lambda jid: True, tool_fn=tool)
    assert a.run_one_job() is True
    assert calls == []  # execution skipped
    assert posts and posts[0][0] == "/v1/agent/jobs/result"
    assert posts[0][1] == {"job_id": "job-1", "ok": False,
                           "error": "cancelled by operator"}


def test_agent_downgrades_success_when_cancelled_mid_run():
    calls = []
    state = {"cancelled": False}
    def tool(inputs, ctx):
        calls.append(1)
        state["cancelled"] = True  # operator cancels during long tool
        return {"x": 1}
    job = {"job_id": "job-2", "tool": "t.echo", "inputs": {}}
    a, posts = _stub_agent(job, checker=lambda jid: state["cancelled"],
                           tool_fn=tool)
    assert a.run_one_job() is True
    assert calls == [1]
    assert posts[0][1] == {"job_id": "job-2", "ok": False,
                           "error": "cancelled by operator"}


def test_agent_reports_success_when_not_cancelled():
    def tool(inputs, ctx):
        return {"x": 1}
    job = {"job_id": "job-3", "tool": "t.echo", "inputs": {}}
    a, posts = _stub_agent(job, checker=lambda jid: False, tool_fn=tool)
    assert a.run_one_job() is True
    assert posts[0][1] == {"job_id": "job-3", "ok": True,
                           "result": {"x": 1}}


def test_server_terminal_states_respected():
    q = JobQueue()
    job = q.enqueue("lap-1", "t.echo", {})
    q.poll("lap-1")
    assert q.cancel(job.id) is True
    assert q.job_state_for("lap-1", job.id) == "cancelled"
    # Late agent report must not resurrect: complete() is no-op-safe.
    out = q.complete(job.id, ok=True, result={"x": 1})
    assert out.state == "cancelled"
    assert q.get(job.id).state == "cancelled"


def test_probe_failure_fails_closed_to_not_cancelled():
    def bad(jid):
        raise OSError("net down")
    a = _agent(checker=bad)
    assert a.is_job_cancelled("job-x") is False
    # Default (no stub, unreachable core) also fails closed, no raise.
    b = _agent(checker=None)
    b.core_url = "http://127.0.0.1:9"
    assert b.is_job_cancelled("job-x") is False
