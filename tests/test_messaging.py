"""Messaging workflow contract (SIMULATED — no messaging app or account
exists on the lab emulator; realistic fakes model conversation lists,
fields, and send buttons. Live-device validation against a real app is
documented as pending in docs/UI_SNAPSHOT_CONTRACT.md).

Covers: discovery (found/ambiguous/missing), composition-field and
send-button location, plan building with snapshot bindings, send
verification (sent vs uncertain, never auto-retry), duplicate-send
prevention via SendLedger, and primitive intent parsing.
"""
from core import messaging as M
from core.intent import LocalIntentParser


def _node(i, **kw):
    n = {"node_id": i, "clickable": False, "editable": False,
         "scrollable": False}
    n.update(kw)
    return n


def _chat_snapshot():
    return {
        "snapshot_id": "aaaa1111:8", "package": "com.example.chat",
        "contract_version": 2,
        "nodes": [
            _node(0, clickable=True, content_desc="Chats"),
            _node(1, clickable=True, content_desc="Asha Kumar"),
            _node(2, clickable=True, content_desc="Asha Menon"),
            _node(3, clickable=True, content_desc="Dev Team"),
            _node(4, editable=True, content_desc="Message"),
            _node(5, clickable=True, content_desc="Send"),
            _node(6, content_desc="hey are we still on for lunch"),
            _node(7, content_desc="yes! noon at the usual place"),
        ],
    }


def test_discovery_exact_match_found():
    out = M.find_conversations(_chat_snapshot(), "Dev Team")
    assert out["status"] == "found"
    assert out["candidates"][0] == {"node_id": 3, "label": "Dev Team"}


def test_discovery_ambiguous_never_guesses():
    out = M.find_conversations(_chat_snapshot(), "asha")
    assert out["status"] == "ambiguous"
    assert len(out["candidates"]) == 2
    assert "which one" in out["detail"]


def test_discovery_missing_structured():
    out = M.find_conversations(_chat_snapshot(), "nobody here")
    assert out["status"] == "missing"


def test_discovery_empty_query_lists():
    out = M.find_conversations(_chat_snapshot(), "")
    assert out["status"] == "listed"
    assert len(out["candidates"]) == 5  # clickable labeled nodes


def test_field_and_send_found():
    snap = _chat_snapshot()
    f = M.find_text_field(snap)
    assert f["status"] == "found" and f["field"]["node_id"] == 4
    s = M.find_send_button(snap)
    assert s["status"] == "found" and s["button"]["node_id"] == 5


def test_field_missing_and_send_ambiguous():
    snap = {"nodes": [_node(0, clickable=True, content_desc="Nope")]}
    assert M.find_text_field(snap)["status"] == "missing"
    assert M.find_send_button(snap)["status"] == "missing"
    two = {"nodes": [_node(0, clickable=True, content_desc="Send"),
                     _node(1, clickable=True, content_desc="Send photo")]}
    assert M.find_send_button(two)["status"] == "ambiguous"


def test_read_messages_marks_untrusted():
    out = M.read_messages(_chat_snapshot())
    assert out["status"] == "ok"
    assert all(m["untrusted"] for m in out["messages"])
    assert any("lunch" in m["text"] for m in out["messages"])


def test_build_send_plan_snapshot_bound():
    out = M.build_send_plan(_chat_snapshot(), "Dev Team",
                            "running 5 mins late")
    assert out["status"] == "planned", out
    assert len(out["steps"]) == 3
    tools = [s["tool"] for s in out["steps"]]
    assert tools == ["gui.tap", "gui.set_text", "gui.tap"]
    for s in out["steps"]:
        assert s["inputs"]["expected_snapshot_id"] == "aaaa1111:8"
        assert s["inputs"]["expected_package"] == "com.example.chat"
    assert out["steps"][1]["inputs"]["text"] == "running 5 mins late"
    assert len(out["fingerprint"]) == 64


def test_build_plan_refuses_ambiguous_and_empty():
    assert M.build_send_plan(_chat_snapshot(), "asha",
                             "hi")["status"] == "ambiguous"
    assert M.build_send_plan(_chat_snapshot(), "nobody",
                             "hi")["status"] == "missing"
    assert M.build_send_plan(_chat_snapshot(), "Dev Team",
                             "  ")["status"] == "refused"


def test_verify_send_sent_vs_uncertain():
    before = _chat_snapshot()
    after = {"nodes": list(before["nodes"]) + [
        _node(8, content_desc="running 5 mins late, sorry!")]}
    ok = M.verify_send(before, after, "running 5 mins late, sorry!")
    assert ok["status"] == "sent", ok
    same = M.verify_send(before, before, "running 5 mins late")
    assert same["status"] == "uncertain"
    assert "retry" in same["detail"]  # never auto-retry guidance
    moved = {"nodes": [_node(0, content_desc="something else")]}
    assert M.verify_send(before, moved, "hello")["status"] == "uncertain"


def test_ledger_blocks_duplicates():
    led = M.SendLedger(ttl_s=60, max_entries=10)
    fp = M.send_fingerprint("Dev Team", "hello")
    assert led.check_and_record(fp) is False  # first sight proceeds
    assert led.check_and_record(fp) is True  # repeat refuses
    assert led.check_and_record(M.send_fingerprint("Dev", "hi")) is False
    # TTL expiry releases the fingerprint.
    import time as _t
    led._seen[fp] = _t.time() - 61
    assert led.check_and_record(fp) is False


def test_fingerprint_stable_and_distinct():
    a = M.send_fingerprint("Dev Team", "hello")
    assert a == M.send_fingerprint("Dev Team", "hello")
    assert a != M.send_fingerprint("Dev Team", "hello!")
    assert a != M.send_fingerprint("Dev", "hello")


def test_primitive_intent_parsing():
    from core.intent import ParserContext
    ctx = ParserContext(
        known_capabilities={"gui.set_text", "gui.scroll"},
        capability_schemas={})
    lp = LocalIntentParser()
    p = lp.parse('type "hello there" into 5', ctx)
    assert p.capability == "gui.set_text", p
    assert p.parameters["text"] == "hello there"
    assert p.parameters["node_id"] == 5
    q = lp.parse("type hello", ctx)  # unquoted, no node
    assert q.needs_clarification
    s = lp.parse("scroll down node 9", ctx)
    assert s.capability == "gui.scroll", s
    assert s.parameters["direction"] == "forward"
    assert s.parameters["node_id"] == 9
    u = lp.parse("scroll up node 9", ctx)
    assert u.parameters["direction"] == "backward"
    n = lp.parse("scroll somewhere", ctx)  # no node -> clarify
    assert n.needs_clarification


def test_core_gui_primitive_defs():
    """Core registry carries the v2 primitives (android-only, gated)."""
    from core.device_tools import DEVICE_TOOL_DEFS
    defs = {d.name: d for d in DEVICE_TOOL_DEFS}
    for name in ("gui.set_text", "gui.scroll"):
        assert defs[name].supported_devices == ["android"], name
        assert defs[name].risk.value == "high_risk", name
        assert defs[name].input_schema["additionalProperties"] is False
    props = defs["gui.set_text"].input_schema["properties"]
    assert props["node_id"]["type"] == "integer"
    assert props["text"] == {"type": "string", "minLength": 1,
                             "maxLength": 500}
    assert defs["gui.scroll"].input_schema["properties"]["direction"] == {
        "type": "string", "enum": ["forward", "backward"]}
    # Text never validates as anything but a bounded string.
    from core.tools import validate_against_schema
    schema = defs["gui.set_text"].input_schema
    good = {"target_package": "dev.zara.zara_android", "node_id": 4,
            "text": "hi"}
    # The validator enforces shape (types/required/no-extra); length
    # bounds are declared in-schema and enforced at the intent, runner,
    # and native layers (each tested separately).
    assert validate_against_schema(good, schema) == []
    assert validate_against_schema(dict(good, node_id="4"),
                                   schema) != []
    assert validate_against_schema(dict(good, bogus=1), schema) != []
    assert validate_against_schema({"target_package": "x"}, schema) != []


def test_plan_stops_when_messaging_step_unresolvable():
    """A plan reaching for a messaging primitive with no capable device
    stops honestly: earlier safe steps stand, later steps never run."""
    from core.missions import MissionState
    from tests.test_intent_plans import FakePlanParser, _echo_steps, _echo_stack, _svc
    stack = _echo_stack()
    svc, eng = _svc(stack, _echo_steps(1) + [
        {"capability": "gui.set_text",
         "parameters": {"target_package": "dev.zara.zara_android",
                        "node_id": 4, "text": "hi"},
         "preferred_device": "", "confidence": 0.8}])
    out = svc.handle_utterance("echo then type")
    assert out["executed"] is True  # step 1 ran
    assert len(out["steps"]) == 2
    assert out["steps"][0]["status"] == "ok"
    assert out["steps"][1]["status"] != "ok"  # no android here
    assert eng.get(out["mission_id"]).state == MissionState.FAILED
    assert "step 2" in out["message"]


def test_plan_parks_when_messaging_step_needs_approval():
    """A HIGH_RISK messaging step parks for approval mid-plan with a
    reference; nothing after it attempts."""
    from core.missions import MissionState
    from core.models import ToolDefinition
    from tests.test_intent import doc, enroll_claim, make_stack, register
    from tests.test_intent_plans import FakePlanParser, _svc
    from core.models import PermissionLevel, RiskLevel
    stack = make_stack()
    stack["registry"].register(
        ToolDefinition(name="test.echo", description="echo",
                       input_schema={"type": "object", "properties": {}},
                       output_schema={"type": "object"}),
        lambda inputs, ctx=None: {"ok": True})
    stack["registry"].register(
        ToolDefinition(name="test.gated_text", description="type",
                       input_schema={"type": "object", "properties": {}},
                       output_schema={"type": "object"},
                       permission=PermissionLevel.CONFIRM,
                       risk=RiskLevel.CONFIRM),
        lambda inputs, ctx=None: {"ok": True})
    enroll_claim(stack, "d1")
    register(stack, "d1")
    raw = doc(cap="test.gated_text")
    raw["risk"] = "confirm"
    stack["fabric"].advertise(
        [doc(cap="test.echo"), raw], "d1")
    svc, eng = _svc(stack, [
        {"capability": "test.echo", "parameters": {},
         "preferred_device": "", "confidence": 0.9},
        {"capability": "test.gated_text",
         "parameters": {"target_package": "d1", "node_id": 4,
                        "text": "do not send yet"},
         "preferred_device": "", "confidence": 0.8}])
    out = svc.handle_utterance("echo then type gated")
    assert out["steps"][0]["status"] == "ok"
    assert out["steps"][1]["status"] == "waiting_for_permission", out
    assert out["approval_reference"]
    assert eng.get(out["mission_id"]).state == \
        MissionState.WAITING_FOR_PERMISSION


def test_plan_stops_when_messaging_device_offline():
    """Preferred device unknown/offline: the messaging step cannot
    resolve, the plan stops, nothing executes speculatively."""
    from core.missions import MissionState
    from tests.test_intent_plans import _echo_stack, _svc
    stack = _echo_stack()
    svc, eng = _svc(stack, [
        {"capability": "gui.set_text",
         "parameters": {"target_package": "ghost-phone", "node_id": 4,
                        "text": "hi"},
         "preferred_device": "ghost-phone", "confidence": 0.8}])
    out = svc.handle_utterance("type into the void")
    assert out["executed"] is False
    assert out["steps"][0]["status"] != "ok"
    assert eng.get(out["mission_id"]).state == MissionState.FAILED
