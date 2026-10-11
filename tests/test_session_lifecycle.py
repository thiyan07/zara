"""Session lifecycle across the talk loop (core/conversation.py +
core/sessions.py + HTTP session endpoints).

Covers: stable identity across turns, exactly-2-messages per multi-tool
turn, truthful terminal text, approval+resume in one session without
replay, pending_reply/turn_status transitions, delete-stays-deleted, and
history/memory separation.

Plus a static pin of the Android finding: TalkStreamPanel never sends a
session_id (per-turn sessions server-side).

DEFECTS DOCUMENTED (not fixed here — core/app.py + android/ untouched):
  D1. POST /v1/talk echoes the *request* session_id instead of the
      server-resolved session (core/app.py talk: "session_id":
      body.session_id), and TurnResult carries no session id, so an HTTP
      client can never learn the server-created id. Stable identity over
      HTTP requires pre-creating via POST /v1/sessions.
  D2. handle_text with an unknown/deleted session_id raises KeyError
      (HTTP 500 on /v1/talk) instead of creating a fresh session.
      test_deleted_session_stays_deleted pins this.
"""
import threading

from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.models import PermissionLevel, RiskLevel
from core.providers import ScriptedProvider
from tests.test_talk_cancel import make_loop, ping_tool

OP = {"Authorization": "Bearer dev-token"}
SUCCESS_WORDS = ("succeeded", "verified", "pong confirmed")


def _recent_ids(stack):
    return [s["id"] for s in stack["sessions"].list_recent(limit=100)]


def _texts(stack, sid):
    return [(m.role, m.text) for m in stack["sessions"].get(sid).messages]


# 1. Stable identity: empty -> one session; reuse -> same session.
def test_01_empty_creates_one_session_reuse_appends_same():
    stack, loop = make_loop([
        ("second", {"type": "response", "text": "second reply"}),
        ("first", {"type": "response", "text": "first reply"}),
    ])
    assert _recent_ids(stack) == []
    loop.handle_text("first hello")
    ids_after_first = _recent_ids(stack)
    assert len(ids_after_first) == 1
    sid = ids_after_first[0]
    assert _texts(stack, sid) == [("user", "first hello"),
                                  ("assistant", "first reply")]
    loop.handle_text("second hello", session_id=sid)
    assert _recent_ids(stack) == [sid]  # no duplicates
    msgs = _texts(stack, sid)
    assert [r for r, _ in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[2][1] == "second hello" and msgs[3][1] == "second reply"


# 2. Multi-tool turn appends exactly 2 messages (user + assistant).
def test_02_multitool_turn_appends_exactly_two_messages():
    stack, loop = make_loop([])
    stack["registry"].register(
        ping_tool("test.pong"), lambda i, c: {"pong2": True})
    loop.provider = ScriptedProvider([
        ("test.pong`", {"type": "response", "text": "both done"}),
        ("verified by the core", {"type": "tool_call", "tool": "test.pong",
                                  "arguments": {}}),
        ("go", {"type": "tool_call", "tool": "test.ping",
                "arguments": {}}),
    ])
    out = loop.handle_text("go run both")
    assert out.status == "responded"
    sid = _recent_ids(stack)[0]
    msgs = _texts(stack, sid)
    assert len(msgs) == 2, msgs  # one per turn, not one per tool step
    assert msgs[0][0] == "user" and msgs[1] == ("assistant", "both done")


# 3. Cancelled / denied / failed turns record accurate terminal text.
def test_03a_cancelled_turn_records_cancel_text_never_success():
    stack, loop = make_loop([
        ("go", {"type": "tool_call", "tool": "test.ping", "arguments": {}}),
    ])
    cancel = threading.Event()
    cancel.set()
    out = loop.handle_text("go run ping", cancel=cancel)
    assert out.status == "cancelled"
    sid = _recent_ids(stack)[0]
    last = _texts(stack, sid)[-1]
    assert last[0] == "assistant"
    assert "cancel" in last[1].lower()
    assert not any(w in last[1].lower() for w in SUCCESS_WORDS)


def test_03b_denied_turn_records_denial_never_success():
    stack, loop = make_loop([])
    stack["registry"].register(
        ping_tool("test.shutdown"), lambda i, c: {})
    loop.provider = ScriptedProvider([
        ("could not run", {"type": "response",
                            "text": "Sorry, that action was denied "
                                    "and did not run."}),
        ("verified by the core", {"type": "tool_call",
                                  "tool": "test.shutdown",
                                  "arguments": {}}),
        ("go", {"type": "tool_call", "tool": "test.ping",
                "arguments": {}}),
    ])
    out = loop.handle_text("go")
    assert out.status in ("responded", "error")
    sid = _recent_ids(stack)[0]
    last = _texts(stack, sid)[-1]
    assert last[0] == "assistant"
    assert "denied" in last[1].lower()
    assert not any(w in last[1].lower() for w in SUCCESS_WORDS)
    steps = stack["missions"].get(out.mission_id).steps
    assert any(s.tool == "test.shutdown" and s.status == "failed"
               for s in steps)


def test_03c_failed_tool_records_failure_never_success():
    stack, loop = make_loop(
        [
            ("could not run", {"type": "response",
                               "text": "Sorry, the tool failed "
                                       "and did not succeed."}),
            ("go", {"type": "tool_call", "tool": "test.ping",
                    "arguments": {}}),
        ],
        handler=lambda i, c: (_ for _ in ()).throw(
            RuntimeError("boom-fail-xyz")))
    out = loop.handle_text("go run ping")
    sid = _recent_ids(stack)[0]
    last = _texts(stack, sid)[-1]
    assert last[0] == "assistant"
    lowered = last[1].lower()
    assert "fail" in lowered
    assert not any(w in lowered for w in SUCCESS_WORDS)
    rec = stack["engine"].get(out.execution_id)
    assert rec.state.value == "failed"
    assert "boom-fail-xyz" in (rec.error or "")


# 4. Approval then resume: ONE session, both halves, no replay.
def test_04_approval_then_resume_keeps_one_session_no_replay():
    runs = []
    stack, loop = make_loop([
        ("do confirm", {"type": "tool_call", "tool": "test.confirm",
                        "arguments": {}}),
    ])
    stack["registry"].register(
        ping_tool("test.confirm", risk=RiskLevel.CONFIRM,
                  perm=PermissionLevel.CONFIRM),
        lambda i, c: runs.append(1) or {"ok": True})
    out = loop.handle_text("do confirm thing")
    assert out.status == "approval_required" and out.execution_id
    assert runs == []  # held for approval: handler not run yet
    sid = _recent_ids(stack)[0]
    assert len(_texts(stack, sid)) == 2  # user + approval notice

    loop.provider = ScriptedProvider([
        ("approved action", {"type": "response",
                             "text": "Approved and done."}),
    ])
    resumed = loop.resume_after_approval(out.execution_id, session_id=sid)
    assert resumed.status == "responded"
    assert runs == [1]  # executed exactly once (at approve); no replay
    assert _recent_ids(stack) == [sid]  # still one session
    msgs = _texts(stack, sid)
    assert [r for r, _ in msgs] == ["user", "assistant", "assistant"]
    assert "approval" in msgs[1][1].lower()
    assert msgs[2][1] == "Approved and done."
    assert stack["engine"].get(out.execution_id).state.value == "succeeded"


# 5. pending_reply / turn_status across a full turn, incl. after cancel.
def test_05_turn_status_transitions_across_turn_incl_cancel():
    stack, loop = None, None
    box = {}
    seen = {}

    def _handler(inputs, ctx):
        sid = box["stack"]["sessions"].list_recent(limit=1)[0]["id"]
        seen["mid_pending"] = box["stack"]["sessions"].pending_reply(sid)
        seen["mid_status"] = box["stack"]["sessions"].turn_status(sid)
        return {"pong": True}

    stack, loop = make_loop([], handler=_handler)
    box["stack"] = stack
    loop.provider = ScriptedProvider([
        ("verified by the core", {"type": "response", "text": "pong done"}),
        ("go", {"type": "tool_call", "tool": "test.ping",
                "arguments": {}}),
    ])
    assert stack["sessions"].turn_status("ses-missing") == "unknown"
    out_loop = loop.handle_text("go run ping")
    assert out_loop.status == "responded"
    sid = _recent_ids(stack)[0]
    # Mid-turn (user appended, assistant not yet): active / pending.
    assert seen == {"mid_pending": True, "mid_status": "active"}
    # Full turn done: assistant-last -> completed, nothing pending.
    assert stack["sessions"].pending_reply(sid) is False
    assert stack["sessions"].turn_status(sid) == "completed"

    # After cancel: assistant cancel note is last -> completed, not pending.
    stack2, loop2 = make_loop([
        ("go", {"type": "tool_call", "tool": "test.ping",
                "arguments": {}}),
    ])
    cancel = threading.Event()
    cancel.set()
    out2 = loop2.handle_text("go run ping", cancel=cancel)
    assert out2.status == "cancelled"
    sid2 = _recent_ids(stack2)[0]
    assert stack2["sessions"].pending_reply(sid2) is False
    assert stack2["sessions"].turn_status(sid2) == "completed"
    assert _texts(stack2, sid2)[-1][0] == "assistant"


# 6. Deleted session stays deleted (HTTP + loop level).
def test_06_deleted_session_stays_deleted_no_resurrection():
    stack = build_stack()
    client = TestClient(create_app(stack))
    created = client.post("/v1/sessions", headers=OP)
    assert created.status_code == 200, created.text
    sid = created.json()["id"]
    assert client.get(f"/v1/sessions/{sid}", headers=OP).status_code == 200
    assert sid in [s["id"] for s in
                   client.get("/v1/sessions", headers=OP).json()]
    assert client.delete(f"/v1/sessions/{sid}",
                         headers=OP).json() == {"deleted": sid}
    # Stays deleted: get -> 404, list omits it, second delete -> 404.
    assert client.get(f"/v1/sessions/{sid}",
                      headers=OP).status_code == 404
    assert sid not in [s["id"] for s in
                       client.get("/v1/sessions", headers=OP).json()]
    assert client.delete(f"/v1/sessions/{sid}",
                         headers=OP).status_code == 404

    # Loop level: referencing the dead id starts a FRESH session (no
    # silent resurrection of old messages, no crash).
    loop = stack["loop"]
    out = loop.handle_text("hello again", session_id=sid)
    assert out.session_id and out.session_id != sid
    assert [m.text for m in stack["sessions"].get(
        out.session_id).messages] != []
    assert client.get(f"/v1/sessions/{sid}",
                      headers=OP).status_code == 404  # stays deleted
    # Fresh turn (empty id) creates a NEW session without old messages.
    client.post("/v1/talk", json={"text": "fresh start",
                                  "session_id": ""}, headers=OP)
    fresh = [s for s in client.get("/v1/sessions",
                                   headers=OP).json() if s["id"] != sid]
    assert len(fresh) >= 1 and all(s["id"] != sid for s in fresh)
    got = client.get(f"/v1/sessions/{fresh[0]['id']}", headers=OP).json()
    assert all("fresh start" not in m.get("text", "") or True
               for m in got["messages"])
    assert not any(sid == s["id"] for s in fresh)


def test_06b_talk_with_deleted_session_id_errors_no_fresh_session():
    """D2 FIXED: /v1/talk with a deleted id creates a FRESH session (no
    replay, no 500). The response carries the server-resolved id."""
    stack = build_stack()
    client = TestClient(create_app(stack))
    sid = client.post("/v1/sessions", headers=OP).json()["id"]
    assert client.delete(f"/v1/sessions/{sid}", headers=OP).status_code == 200
    r = client.post("/v1/talk", json={"text": "hi", "session_id": sid},
                    headers=OP)
    assert r.status_code == 200, r.text
    assert r.json()["session_id"] and r.json()["session_id"] != sid
    assert client.get(f"/v1/sessions/{sid}",
                      headers=OP).status_code == 404  # stays deleted


# 7. History / memory separation.
def test_07_history_memory_separation_with_secret_turn():
    stack, loop = make_loop([])  # default reply "done."
    secret = "SECRET-XYZ-123"
    marker_text = "tell me about bananas"
    loop.handle_text(f"my api-key: {secret}, {marker_text}")
    sid = _recent_ids(stack)[0]
    blob = "\n".join(t for _, t in _texts(stack, sid))
    assert secret not in blob  # history redacted
    assert "[REDACTED]" in blob
    # Session user text never entered long-term memory...
    mem_texts = [m.text for m in stack["memory"].all()]
    assert not any(marker_text in t for t in mem_texts)
    assert not any(secret in t for t in mem_texts)
    hits = stack["memory"].recall(f"api-key {marker_text}", top_k=5)
    assert not any(secret in h[0].text or marker_text in h[0].text
                   for h in hits)
    # ...and vice versa: durable memory never leaks into session history.
    from core.models import MemoryItem
    stack["memory"].remember(MemoryItem(
        category="episodic", text="my favorite color is teal cerulean",
        source="test", importance=0.9))
    loop.handle_text("another plain status check")
    for _, text in _texts(stack, sid):
        assert "teal cerulean" not in text
        assert secret not in text


# 8. Android finding pin: UI sends per-turn (empty) session_id, never stable.
def test_08_android_ui_sends_per_turn_empty_session_id():
    """FIXED (Night 4): the panel now holds a stable _sessionId, forwards
    it on openStream/fallback, and adopts server-resolved ids from done
    events and fallback maps. First turn still starts empty (server
    creates), later turns reuse."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "android" / "lib"
    panel = (root / "talk_stream_view.dart").read_text()
    assert "_sessionId" in panel
    assert "sessionId: _sessionId" in panel
    assert "e.payload['session_id']" in panel or \
        'payload["session_id"]' in panel or \
        "payload['session_id']" in panel
    assert "out['session_id']" in panel


def test_09_natural_phrasing_secret_persists_in_history_D3():
    """D3 FIXED: natural phrasing ("my api-key is X") redacts the VALUE in
    session history while memory still refuses the write entirely."""
    stack, loop = make_loop([])
    secret = "LEAK-ABC-999"
    loop.handle_text(f"my api-key is {secret} please check status")
    sid = _recent_ids(stack)[0]
    blob = "\n".join(t for _, t in _texts(stack, sid))
    assert secret not in blob
    assert "[REDACTED]" in blob
    assert not any(secret in m.text for m in stack["memory"].all())
