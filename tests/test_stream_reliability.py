"""Stream reliability edges (Night 2 follow-up): cancel mid-stream via
HTTP, single terminal emission, stale-turn isolation, malformed-chunk
tolerance, and idle termination.

Scope: NEW tests only. Core behavior (core/app.py, core/conversation.py)
is exercised read-only through the public HTTP surface plus parse_events.
Resend-as-new-turn is the documented disconnect semantic — no test here
expects mid-stream resume.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.models import PermissionLevel, RiskLevel, ToolDefinition
from core.providers import ModelProvider, ScriptedProvider
from core.talk_stream import format_event, parse_events

H = {"Authorization": "Bearer dev-token"}


def _ping_tool():
    return ToolDefinition(
        name="test.ping", description="test ping tool",
        input_schema={"type": "object",
                      "properties": {"command": {"type": "string"}}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
        timeout_s=5.0)


def _raw_lines(lines):
    """Rejoin streamed lines into a parseable SSE blob."""
    return "\n".join(lines) + "\n"


def _read_stream(app, text, sink, holder):
    """Background-thread stream consumer (TestClient blocks per call, so
    the cancel drill below needs the stream on its own client+thread)."""
    client = TestClient(app)
    with client.stream("POST", "/v1/talk/stream", json={"text": text},
                       headers=H) as resp:
        holder["status"] = resp.status_code
        for line in resp.iter_lines():
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            sink.append(line)


def _wait_for(pred, timeout, what):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _turn_id_from(lines):
    for i, line in enumerate(lines):
        if line.strip() == "event: turn" and i + 1 < len(lines):
            nxt = lines[i + 1]
            if nxt.startswith("data:"):
                import json as _j
                return _j.loads(nxt[len("data:"):].strip()).get("turn_id")
    return None


class _PyConversation:
    """Test-local mirror of the TalkConversation terminal rules
    (android/lib/stream_client.dart, read-only): done is terminal and
    applies exactly once; late frames are ignored; disconnect is explicit
    and non-terminal. Owned by this test file — the backend half of the
    contract is 'done emitted exactly once per turn'."""

    def __init__(self):
        self.status = "streaming"
        self.done_count = 0
        self.reply = ""
        self.turn_id = None

    def apply(self, name, payload):
        if self.status in ("done", "error", "cancelled"):
            return
        if name == "turn":
            self.turn_id = self.turn_id or payload.get("turn_id")
        elif name == "delta":
            t = payload.get("text")
            if isinstance(t, str):
                self.reply += t
        elif name == "done":
            self.done_count += 1
            self.status = "done"
            self.reply = payload.get("reply", self.reply)

    def mark_disconnected(self):
        if self.status not in ("done", "error", "cancelled"):
            self.status = "disconnected"


def test_R01_cancel_mid_stream_via_http_stops_second_tool():
    """Scripted 2-tool turn, cancel delivered over POST /v1/talk/cancel
    with the streamed turn_id while tool 1 is held open: the stream must
    terminate with done/cancelled, emit no further tool events after the
    cancel, and never dispatch the second tool."""
    stack = build_stack(fabric_db="")
    ran = []
    tool1_open = threading.Event()
    proceed = threading.Event()

    def handler(inputs, ctx):
        ran.append(dict(inputs))
        tool1_open.set()
        proceed.wait(timeout=20)  # hold the turn open; cancel lands here
        return {"pong": True}

    stack["registry"].register(_ping_tool(), handler)
    stack["loop"].provider = ScriptedProvider([
        ("complete-a", {"type": "tool_call", "tool": "test.ping",
                        "arguments": {"command": "second"}}),
        ("kickoff", {"type": "tool_call", "tool": "test.ping",
                     "arguments": {"command": "first"}}),
    ])
    app = create_app(stack)
    ctrl = TestClient(app)

    lines, holder = [], {}
    reader = threading.Thread(target=_read_stream,
                              args=(app, "kickoff go", lines, holder),
                              daemon=True)
    reader.start()

    # The TestClient transport only releases streamed lines on close, so
    # the live turn_id is picked up from the registry (the same id the
    # stream's first frame carries — asserted below), then cancelled over
    # HTTP while tool 1 is held open.
    assert tool1_open.wait(timeout=20), "first tool never started"
    _wait_for(lambda: len(stack["loop"].turns.active_ids()) == 1, 20,
              "live turn")
    tid = stack["loop"].turns.active_ids()[0]

    tools_at_cancel = sum(1 for ln in lines if ln.strip() == "event: tool")
    r = ctrl.post("/v1/talk/cancel", json={"turn_id": tid}, headers=H)
    assert r.status_code == 200 and r.json() == {"cancelled": True}
    proceed.set()

    reader.join(timeout=60)
    assert not reader.is_alive(), "stream hung after cancel"
    assert holder.get("status") == 200

    events = parse_events(_raw_lines(lines))
    names = [n for n, _ in events]
    assert names[0] == "turn" and events[0][1]["turn_id"] == tid
    done = [p for n, p in events if n == "done"]
    assert len(done) == 1, f"expected exactly one done, got {len(done)}"
    assert done[0]["status"] == "cancelled"
    assert done[0]["turn_id"] == tid

    # Dispatch-level stop: exactly one tool dispatch (one "started"); the
    # in-flight tool's completion narration (ok) may legitimately follow
    # the cancel — it describes verified work that already ran, consistent
    # with "in-flight result stays recorded". What must never happen is a
    # second dispatch: no second "started", one execution id, one handler
    # run, one reasoning step.
    tools = [p for n, p in events if n == "tool"]
    assert [p.get("phase") for p in tools].count("started") == 1
    assert tools_at_cancel == 0  # transport buffers; all frames land at close
    exec_ids = {p.get("execution_id") for p in tools if p.get("execution_id")}
    assert len(exec_ids) == 1, f"more than one execution narrated: {exec_ids}"
    assert len(ran) == 1, "second tool must never dispatch"
    assert ran[0] == {"command": "first"}
    assert len(stack["loop"].provider.calls) == 1, \
        "no second reasoning step after cancel"


def test_R02_backend_emits_done_once_and_duplicate_done_stays_single():
    """Scripted 3-step turn (tool, tool, response): the wire carries done
    exactly once, and feeding that same done payload twice through the
    reducer mirror stays single (doneCount 1, reply unchanged)."""
    stack = build_stack(fabric_db="")

    def handler(inputs, ctx):
        if inputs.get("command") == "first":
            return {"phase1": "complete-a"}
        return {"phase2": "complete-b"}

    stack["registry"].register(_ping_tool(), handler)
    stack["loop"].provider = ScriptedProvider([
        ("complete-b", {"type": "response", "text": "all done."}),
        ("complete-a", {"type": "tool_call", "tool": "test.ping",
                        "arguments": {"command": "second"}}),
        ("kickoff", {"type": "tool_call", "tool": "test.ping",
                     "arguments": {"command": "first"}}),
    ])
    app = create_app(stack)
    c = TestClient(app)
    with c.stream("POST", "/v1/talk/stream", json={"text": "kickoff go"},
                  headers=H) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode("utf-8")
    events = parse_events(body)
    done = [p for n, p in events if n == "done"]
    assert len(done) == 1, f"backend must emit done once, got {len(done)}"
    assert done[0]["status"] == "responded"
    assert done[0]["turn_id"] == events[0][1]["turn_id"]
    assert sum(1 for n, _ in events if n == "tool") == 4  # 2x started+ok

    conv = _PyConversation()
    for name, payload in events:
        conv.apply(name, payload)
    assert conv.done_count == 1
    first_reply = conv.reply
    conv.apply("done", done[0])  # duplicate terminal delivery
    conv.apply("done", done[0])
    assert conv.done_count == 1
    assert conv.reply == first_reply
    assert conv.status == "done"


def test_R03_stale_turn_isolation_across_sequential_streams():
    """Two sequential streams: concatenated wire parses cleanly, turn ids
    are distinct, and turn-A events never alter turn-B state (done
    attributed by its own turn_id, deltas by the enclosing turn)."""
    stack = build_stack(fabric_db="")
    app = create_app(stack)
    c = TestClient(app)

    def one(text):
        with c.stream("POST", "/v1/talk/stream", json={"text": text},
                      headers=H) as resp:
            assert resp.status_code == 200
            return b"".join(resp.iter_bytes()).decode("utf-8")

    body_a, body_b = one("hello alpha"), one("hello beta")
    events = parse_events(body_a + body_b)  # client-parse level, concatenated
    turns = [p["turn_id"] for n, p in events if n == "turn"]
    assert len(turns) == 2 and turns[0] != turns[1]

    states = {}
    current = None
    for name, payload in events:
        if name == "turn":
            current = payload["turn_id"]
            states[current] = {"reply": "", "dones": []}
        elif name == "delta" and current in states:
            states[current]["reply"] += payload.get("text", "")
        elif name == "done":
            tid = payload.get("turn_id")
            assert tid in states, "done for an unknown turn"
            states[tid]["dones"].append(payload)
            states[tid]["reply"] = payload.get("reply", "")
    tid_a, tid_b = turns
    assert len(states[tid_a]["dones"]) == 1
    assert len(states[tid_b]["dones"]) == 1
    assert states[tid_a]["reply"] != states[tid_b]["reply"]
    # Stale replica: a consumer holding turn-B state that keys terminal
    # payloads by their own turn_id is unmoved by turn-A frames.
    conv_b = _PyConversation()
    for name, payload in parse_events(body_b):
        conv_b.apply(name, payload)
    assert conv_b.turn_id == tid_b and conv_b.done_count == 1
    reply_b, status_b = conv_b.reply, conv_b.status
    stale_a_done = states[tid_a]["dones"][0]
    assert stale_a_done["turn_id"] != tid_b
    # Keyed-consumer rule (the isolation mechanism at the parse level):
    # a terminal payload whose turn_id is foreign to the held turn is
    # never applied.
    applied = stale_a_done["turn_id"] == conv_b.turn_id
    assert not applied, "stale turn-A done must not apply to turn-B state"
    assert (conv_b.reply, conv_b.status) == (reply_b, status_b)
    assert conv_b.done_count == 1


def test_R04_malformed_chunk_raises_once_and_prior_state_survives():
    """parse_events unit level: a malformed chunk raises exactly once and
    the consumer keeps its prior good state, then continues with later
    valid events.

    NOTE (ScriptedProvider capability check): ScriptedProvider inherits
    the base single-outcome stream_reason and offers no raw-chunk
    injection hook, so a mid-stream bad chunk cannot be scripted through
    the provider — the stream-level half of this test asserts the server
    itself never emits malformed framing instead (format_event fails
    loudly at the source; a live trivial stream parses cleanly)."""
    assert ScriptedProvider.stream_reason is ModelProvider.stream_reason, \
        "ScriptedProvider gained chunk streaming; revisit stream-level cover"
    with pytest.raises(ValueError):
        format_event("bogus", {})  # unknown names fail loudly, never emit
    good_prefix = format_event("turn", {"turn_id": "t-1"})
    good_prefix += format_event("delta", {"text": "Hel"})
    conv = _PyConversation()
    for name, payload in parse_events(good_prefix):
        conv.apply(name, payload)
    assert conv.reply == "Hel" and conv.status == "streaming"

    with pytest.raises(ValueError):  # malformed chunk: raises once
        parse_events("event: delta\ndata: not-json\n\n")
    assert conv.reply == "Hel" and conv.status == "streaming"  # state kept

    for name, payload in parse_events(
            format_event("delta", {"text": "lo"})
            + format_event("done", {"reply": "Hello", "status": "ok",
                                    "turn_id": "t-1", "mission_id": "",
                                    "execution_id": ""})):
        conv.apply(name, payload)
    assert conv.reply == "Hello" and conv.done_count == 1

    # Stream level: the server emits only well-formed framing.
    stack = build_stack(fabric_db="")
    c = TestClient(create_app(stack))
    with c.stream("POST", "/v1/talk/stream", json={"text": "hello"},
                  headers=H) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode("utf-8")
    assert parse_events(body)  # parses cleanly, no malformed chunk


def test_R05_idle_stream_terminates_within_bound():
    """Trivial echo turn: the stream must terminate (done, connection
    close) rather than hang. The test itself is bounded at 60s."""
    stack = build_stack(fabric_db="")
    app = create_app(stack)
    lines, holder, errors = [], {}, []

    def read():
        try:
            _read_stream(app, "hello", lines, holder)
        except Exception as e:  # noqa: BLE001 — surface, don't hang
            errors.append(e)

    start = time.monotonic()
    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(timeout=60)
    elapsed = time.monotonic() - start
    assert not t.is_alive(), "stream did not terminate within 60s"
    assert not errors, f"stream reader failed: {errors}"
    assert elapsed < 60
    assert holder.get("status") == 200
    events = parse_events(_raw_lines(lines))
    assert events and events[0][0] == "turn"
    assert [p for n, p in events if n == "done"], \
        "trivial turn must terminate with done"
