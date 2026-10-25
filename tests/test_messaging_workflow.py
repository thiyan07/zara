"""Mission-bound single-approval messaging workflow tests (SIMULATED).

No messaging app or account exists on the lab emulator (the package scan
confirmed it), so these tests use realistic scripted GUI handlers: a
conversation list, a composer with an editable field, and a send button.
Every execution still flows through the REAL fabric -> engine path
(identity -> trust -> presence -> capability -> governor -> HIGH_RISK
re-validation), so the tests exercise the actual authorization plumbing,
not a re-implementation. Live-device validation is documented as pending
in docs/UI_SNAPSHOT_CONTRACT.md.

Covered: one approval for the whole multi-step HIGH_RISK sequence,
binding to mission / recipient / exact content / device / action
sequence, invalidation on material change or tamper, denial / expiry /
cancel, device disconnect after approval, conversation change at
execution, stale snapshot, missing send button, unverifiable outcome,
duplicate requests, retry-after-uncertain, restart resume, and the
guarantee that message content never reaches audit.
"""
import pytest

from core import messaging as M
from core.app import build_stack
from core.models import DeviceKind, DeviceState, ExecutionState
from tests.test_intent import doc, enroll_claim, register

PKG = "dev.zara.zara_android"  # lab-allowlisted GUI target (schema enum)


def _n(i, **kw):
    base = {"node_id": i, "clickable": False, "editable": False,
            "scrollable": False}
    base.update(kw)
    return base


def _snap(sid, nodes):
    return {"snapshot_id": sid, "package": PKG, "contract_version": 2,
            "nodes": nodes}


def _chat_list(sid="s0:1"):
    return _snap(sid, [
        _n(0, clickable=True, content_desc="Chats"),
        _n(1, clickable=True, content_desc="Asha Kumar"),
        _n(2, clickable=True, content_desc="Dev Team"),
    ])


def _composer(sid="s1:1"):
    return _snap(sid, [
        _n(3, clickable=True, content_desc="Back"),
        _n(4, editable=True, content_desc="Message"),
        _n(5, clickable=True, content_desc="Send"),
    ])


def _with_sent(sid, text):
    return _snap(sid, [
        _n(3, clickable=True, content_desc="Back"),
        _n(4, editable=True, content_desc="Message"),
        _n(5, clickable=True, content_desc="Send"),
        _n(9, content_desc=text),
    ])


def _gui_stack():
    stack = build_stack()
    enroll_claim(stack, "phone", kind="android")
    register(stack, "phone", kind="android")
    stack["fabric"].advertise([
        doc(cap="gui.screen.inspect", risk="confirm"),
        doc(cap="gui.tap", risk="high_risk"),
        doc(cap="gui.set_text", risk="high_risk"),
    ], "phone")
    return stack


class Scripted:
    """Installs scripted gui.* handlers on the real registry (test-only),
    recording every call so tests can prove what did and did not run."""

    def __init__(self, stack):
        self.stack = stack
        self.inspects = []
        self._i = 0
        self.taps = []
        self.sets = []
        self.on_tap = None       # callable(inputs) -> dict | raise
        self.on_set = None       # callable(inputs) -> dict | raise
        reg = stack["registry"]
        reg._handlers["gui.screen.inspect"] = self._inspect
        reg._handlers["gui.tap"] = self._tap
        reg._handlers["gui.set_text"] = self._set_text

    def _inspect(self, inputs, ctx=None):
        i = self._i
        self._i += 1
        if i < len(self.inspects):
            return self.inspects[i]
        return self.inspects[-1] if self.inspects else {"nodes": []}

    def _tap(self, inputs, ctx=None):
        self.taps.append(dict(inputs))
        if self.on_tap is not None:
            return self.on_tap(inputs)
        return {"applied": True, "action": "tap"}

    def _set_text(self, inputs, ctx=None):
        self.sets.append(dict(inputs))
        if self.on_set is not None:
            return self.on_set(inputs)
        return {"applied": True, "verify_state": "text_matched"}


def _workflow(stack):
    return M.MessagingWorkflow(stack["fabric"], missions=stack["missions"],
                               audit=stack["audit"])


def _request(wf, snapshot, conversation="Dev Team", text="running late",
             device_id="phone"):
    return wf.request_send(snapshot=snapshot, conversation=conversation,
                           text=text, device_id=device_id)


# ---------- request: one approval, exact binding, nothing runs ----------

def test_request_presents_one_bound_approval_and_runs_nothing():
    stack = _gui_stack()
    sc = Scripted(stack)
    wf = _workflow(stack)
    out = _request(wf, _chat_list(), text="running 5 mins late")
    assert out["status"] == "pending_approval", out
    assert out["recipient"] == "Dev Team"
    assert out["message_text"] == "running 5 mins late"
    assert out["actions"] == list(M.ACTION_SEQUENCE)
    b = out["binding"]
    assert b["conversation"] == "Dev Team" and b["device_id"] == "phone"
    assert b["mission_id"]
    assert "Approval id" in out["message"]
    # Nothing executed at request time.
    assert sc.taps == [] and sc.sets == []
    # Mission parked on the single approval.
    assert stack["missions"].get(b["mission_id"]).state.value == \
        "waiting_for_permission"


def test_request_refuses_ambiguous_and_missing_and_empty():
    stack = _gui_stack()
    Scripted(stack)
    wf = _workflow(stack)
    assert _request(wf, _chat_list(), conversation="a")["status"] == "ambiguous"
    assert _request(wf, _chat_list(), conversation="nobody")["status"] == "missing"
    assert _request(wf, _chat_list(), text="   ")["status"] == "refused"


# ---------- happy path: one approval covers the whole sequence ----------

def test_single_approval_sends_and_verifies_all_steps_once():
    stack = _gui_stack()
    sc = Scripted(stack)
    sc.inspects = [_chat_list(), _composer(), _composer(),
                   _with_sent("s3:1", "running 5 mins late")]
    wf = _workflow(stack)
    req = _request(wf, _chat_list(), text="running 5 mins late")
    out = wf.approve(req["approval_id"])
    assert out["status"] == "succeeded", out
    # open + send taps: exactly two taps, two inspects framing compose.
    assert len(sc.taps) == 2
    assert len(sc.sets) == 1
    assert sc.sets[0]["text"] == "running 5 mins late"
    # Every executed step succeeded without a per-step approval hold.
    for eid in out["executions"]:
        assert stack["engine"].get(eid).state == ExecutionState.SUCCEEDED
    # Ledger committed; mission completed.
    assert wf.ledger.seen(M.send_fingerprint("Dev Team", "running 5 mins late"))
    m = stack["missions"].get(req["binding"]["mission_id"])
    assert m.state.value == "completed"


def test_high_risk_gui_still_parks_without_the_workflow():
    """Control: the mission approval does NOT weaken central policy. A
    direct HIGH_RISK submit still parks for approval."""
    stack = _gui_stack()
    Scripted(stack)
    out = stack["fabric"].execute_on_device(
        "gui.tap", {"target_package": PKG, "node_id": 2}, "phone")
    assert out["stage"] == "waiting_for_permission", out


# ---------- denial / expiry / cancel / double approve ----------

def test_cancel_before_send_prevents_all_execution():
    stack = _gui_stack()
    sc = Scripted(stack)
    wf = _workflow(stack)
    req = _request(wf, _chat_list())
    assert wf.cancel(req["approval_id"])["status"] == "cancelled"
    out = wf.approve(req["approval_id"])
    assert out["status"] == "cancelled", out
    assert sc.taps == [] and sc.sets == []


def test_cancel_during_compose_prevents_the_send():
    """A cancel that lands mid-sequence (during compose) must stop before
    the send tap — nothing may be sent after the user withdraws."""
    stack = _gui_stack()
    sc = Scripted(stack)
    sc.inspects = [_chat_list(), _composer(), _composer()]
    wf = _workflow(stack)
    req = _request(wf, _chat_list())
    aid = req["approval_id"]

    def _cancel_mid(inputs):
        wf.approvals.cancel(aid)  # operator withdraws while composing
        return {"applied": True, "verify_state": "text_matched"}

    sc.on_set = _cancel_mid
    out = wf.approve(aid)
    assert out["status"] == "cancelled", out
    assert len(sc.taps) == 1  # only the open tap; the send tap never ran
    assert not wf.ledger.seen(
        M.send_fingerprint("Dev Team", "running late"))


def test_expired_approval_is_refused():
    stack = _gui_stack()
    Scripted(stack)
    wf = _workflow(stack)
    wf.approvals.ttl_s = 0.0  # expire immediately
    req = _request(wf, _chat_list())
    out = wf.approve(req["approval_id"])
    assert out["status"] == "expired", out


def test_double_approve_second_is_not_approvable():
    stack = _gui_stack()
    sc = Scripted(stack)
    sc.inspects = [_chat_list(), _composer(), _composer(),
                   _with_sent("s3:1", "running late")]
    wf = _workflow(stack)
    req = _request(wf, _chat_list())
    first = wf.approve(req["approval_id"])
    assert first["status"] == "succeeded"
    second = wf.approve(req["approval_id"])
    assert second["status"] in ("completed", "not_approvable"), second
    # No extra taps from the refused second approval.
    assert len(sc.taps) == 2


def test_unknown_approval_is_missing():
    stack = _gui_stack()
    Scripted(stack)
    wf = _workflow(stack)
    assert wf.approve("msgappr-none")["status"] == "missing"
    assert wf.status("msgappr-none")["status"] == "missing"


# ---------- interruption matrix ----------

def test_device_offline_after_approval_blocks_without_sending():
    stack = _gui_stack()
    sc = Scripted(stack)
    wf = _workflow(stack)
    req = _request(wf, _chat_list())
    stack["devices"].register(DeviceState(device_id="phone",
                                          kind=DeviceKind("android"),
                                          online=False, status="offline"))
    out = wf.approve(req["approval_id"])
    assert out["status"] == "blocked", out
    assert sc.taps == [] and sc.sets == []
    assert not wf.ledger.seen(
        M.send_fingerprint("Dev Team", "running late"))


def test_conversation_changes_at_execution_blocks():
    stack = _gui_stack()
    sc = Scripted(stack)
    # At execution the list no longer contains "Dev Team".
    sc.inspects = [_snap("s0:1", [_n(1, clickable=True,
                                     content_desc="Asha Kumar")])]
    wf = _workflow(stack)
    req = _request(wf, _chat_list(), conversation="Dev Team")
    out = wf.approve(req["approval_id"])
    assert out["status"] in ("blocked", "ambiguous"), out
    assert sc.taps == [] and sc.sets == []


def test_stale_snapshot_aborts_without_sending():
    stack = _gui_stack()
    sc = Scripted(stack)
    sc.inspects = [_chat_list(), _composer()]

    def _stale(inputs):
        raise RuntimeError("refused: STALE_SNAPSHOT, re-inspect")

    sc.on_tap = _stale
    wf = _workflow(stack)
    req = _request(wf, _chat_list())
    out = wf.approve(req["approval_id"])
    assert out["status"] == "stale", out
    assert not wf.ledger.seen(
        M.send_fingerprint("Dev Team", "running late"))


def test_send_button_unavailable_blocks_and_ledger_clean():
    stack = _gui_stack()
    sc = Scripted(stack)
    # Third inspect (the send check) has no send button.
    sc.inspects = [_chat_list(), _composer(),
                   _snap("s2:1", [_n(4, editable=True,
                                     content_desc="Message")])]
    wf = _workflow(stack)
    req = _request(wf, _chat_list())
    out = wf.approve(req["approval_id"])
    assert out["status"] == "blocked", out
    assert len(sc.sets) == 1  # typed, but never sent
    assert not wf.ledger.seen(
        M.send_fingerprint("Dev Team", "running late"))


def test_unverifiable_send_is_uncertain_and_never_resent():
    stack = _gui_stack()
    sc = Scripted(stack)
    sc.inspects = [_chat_list(), _composer(), _composer(),
                   _composer()]  # verification shows no new message node
    wf = _workflow(stack)
    req = _request(wf, _chat_list(), text="still there?")
    out = wf.approve(req["approval_id"])
    assert out["status"] == "uncertain", out
    fp = M.send_fingerprint("Dev Team", "still there?")
    assert wf.ledger.seen(fp)  # submitted: recorded before verifying
    # A retry must refuse rather than duplicate the send.
    again = _request(wf, _chat_list(), text="still there?")
    assert again["status"] == "duplicate", again


def test_policy_hard_deny_in_content_refused_without_sending():
    stack = _gui_stack()
    sc = Scripted(stack)
    sc.inspects = [_chat_list(), _composer()]
    wf = _workflow(stack)
    req = _request(wf, _chat_list(), text="please rm -rf / now")
    out = wf.approve(req["approval_id"])
    assert out["status"] == "blocked", out
    assert sc.sets == []          # compose was deny-pattern refused
    assert len(sc.taps) == 1      # only the (harmless) open tap ran
    assert not wf.ledger.seen(
        M.send_fingerprint("Dev Team", "please rm -rf / now"))


# ---------- material change / tamper invalidation ----------

def test_content_change_produces_a_new_approval():
    stack = _gui_stack()
    Scripted(stack)
    wf = _workflow(stack)
    a = _request(wf, _chat_list(), text="first")
    b = _request(wf, _chat_list(), text="second")
    assert a["approval_id"] != b["approval_id"]
    assert a["binding"]["content_fingerprint"] != \
        b["binding"]["content_fingerprint"]


def test_recipient_change_produces_a_new_approval():
    stack = _gui_stack()
    Scripted(stack)
    wf = _workflow(stack)
    a = _request(wf, _chat_list(), conversation="Dev Team")
    b = _request(wf, _chat_list(), conversation="Asha Kumar")
    assert a["approval_id"] != b["approval_id"]
    assert a["binding"]["conversation"] != b["binding"]["conversation"]


def test_tampered_binding_is_refused():
    stack = _gui_stack()
    sc = Scripted(stack)
    wf = _workflow(stack)
    req = _request(wf, _chat_list())
    rec = wf.approvals.get(req["approval_id"])
    rec["conversation_label"] = "Someone Else"  # material tamper
    out = wf.approve(req["approval_id"])
    assert out["status"] == "blocked", out
    assert sc.taps == [] and sc.sets == []


def test_duplicate_request_returns_same_pending_approval():
    stack = _gui_stack()
    Scripted(stack)
    wf = _workflow(stack)
    a = _request(wf, _chat_list())
    b = _request(wf, _chat_list())
    assert b["duplicate"] is True
    assert a["approval_id"] == b["approval_id"]


# ---------- restart / durability ----------

def test_snapshotless_request_binds_label_and_resolves_at_execution():
    """With no caller snapshot, the approval binds the recipient label and
    the conversation is resolved from a FRESH snapshot inside the approved
    execution — one approval covers the read too."""
    stack = _gui_stack()
    sc = Scripted(stack)
    sc.inspects = [_chat_list(), _composer(), _composer(),
                   _with_sent("s3:1", "running late")]
    wf = _workflow(stack)
    req = wf.request_send(snapshot={}, conversation="Dev Team",
                          text="running late", device_id="phone")
    assert req["status"] == "pending_approval", req
    out = wf.approve(req["approval_id"])
    assert out["status"] == "succeeded", out
    assert sc.taps[0]["node_id"] == 2  # "Dev Team" node, resolved fresh


def test_snapshotless_unresolvable_at_execution_blocks():
    stack = _gui_stack()
    sc = Scripted(stack)
    sc.inspects = [_snap("s0:1", [_n(1, clickable=True,
                                     content_desc="Asha Kumar")])]
    wf = _workflow(stack)
    req = wf.request_send(snapshot={}, conversation="Dev Team",
                          text="hello", device_id="phone")
    assert req["status"] == "pending_approval"
    out = wf.approve(req["approval_id"])
    assert out["status"] in ("blocked", "ambiguous"), out
    assert sc.taps == [] and sc.sets == []


def test_ledger_survives_restart(tmp_path):
    path = str(tmp_path / "ledger.db")
    fp = M.send_fingerprint("Dev Team", "hi")
    led1 = M.SendLedger(path=path)
    assert led1.record(fp) is True
    led2 = M.SendLedger(path=path)          # simulated process restart
    assert led2.seen(fp) is True
    # A fresh workflow sharing the durable ledger refuses the resend.
    stack = _gui_stack()
    Scripted(stack)
    wf = M.MessagingWorkflow(stack["fabric"], missions=stack["missions"],
                             audit=stack["audit"], ledger=led2)
    out = _request(wf, _chat_list(), text="hi")
    assert out["status"] == "duplicate", out


def test_restart_loses_pending_approval_so_user_must_reapprove():
    """In-memory pending approvals do not survive a restart; the durable
    ledger is what prevents a resend. Re-requesting yields a new, un-
    approved record (nothing auto-executes)."""
    stack = _gui_stack()
    Scripted(stack)
    wf1 = _workflow(stack)
    req = _request(wf1, _chat_list())
    wf2 = _workflow(stack)  # fresh workflow: empty approval store
    assert wf2.status(req["approval_id"])["status"] == "missing"
    assert wf2.approve(req["approval_id"])["status"] == "missing"


# ---------- privacy ----------

def test_message_content_never_reaches_audit():
    stack = _gui_stack()
    sc = Scripted(stack)
    secret = "meet me at the pier at midnight"
    sc.inspects = [_chat_list(), _composer(), _composer(),
                   _with_sent("s3:1", secret)]
    wf = _workflow(stack)
    req = _request(wf, _chat_list(), text=secret)
    assert wf.approve(req["approval_id"])["status"] == "succeeded"
    rows = stack["audit"].query(limit=2000)
    blob = " ".join(str(r.get("detail", "")) for r in rows)
    assert secret not in blob
    # The approval view never carries the plaintext either.
    assert "message_text" not in wf.status(req["approval_id"])
    assert secret not in str(wf.status(req["approval_id"]))


# ---------- HTTP surface ----------

OP = {"Authorization": "Bearer dev-token"}


def _client(stack):
    from fastapi.testclient import TestClient
    from core.app import create_app
    return TestClient(create_app(stack))


def test_http_send_then_approve_roundtrip():
    stack = _gui_stack()
    sc = Scripted(stack)
    sc.inspects = [_chat_list(), _composer(), _composer(),
                   _with_sent("s3:1", "running late")]
    c = _client(stack)
    r = c.post("/v1/messaging/send",
               json={"snapshot": _chat_list(), "conversation": "Dev Team",
                     "text": "running late", "device_id": "phone"},
               headers=OP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_approval"
    aid = body["approval_id"]
    st = c.get(f"/v1/messaging/approvals/{aid}", headers=OP)
    assert st.status_code == 200 and st.json()["state"] == "pending"
    ap = c.post(f"/v1/messaging/approvals/{aid}/approve", json={}, headers=OP)
    assert ap.status_code == 200 and ap.json()["status"] == "succeeded", ap.text
    assert c.get(f"/v1/messaging/approvals/{aid}", headers=OP).json()["state"] \
        == "completed"


def test_http_pending_listing_is_redacted_and_bounded():
    stack = _gui_stack()
    Scripted(stack)
    c = _client(stack)
    # Two different requests -> two distinct pending approvals.
    for text in ("first", "second"):
        r = c.post("/v1/messaging/send",
                   json={"snapshot": _chat_list(),
                         "conversation": "Dev Team", "text": text,
                         "device_id": "phone"},
                   headers=OP)
        assert r.status_code == 200, r.text
    ls = c.get("/v1/messaging/approvals", headers=OP)
    assert ls.status_code == 200, ls.text
    body = ls.json()
    assert body["count"] == 2 and len(body["approvals"]) == 2
    for row in body["approvals"]:
        assert row["state"] == "pending" and row["conversation"] == "Dev Team"
        listing = str(row).lower()
        assert "first" not in listing and "second" not in listing, row
    # Approving one removes it from the pending list (one-time surface).
    aid = body["approvals"][0]["approval_id"]
    Scripted(stack)  # fresh (empty) UI script: the approved plan may fail
    # honestly on inspect — that is fine, the approval is consumed.
    c.post(f"/v1/messaging/approvals/{aid}/approve", json={}, headers=OP)
    ls2 = c.get("/v1/messaging/approvals", headers=OP).json()
    assert ls2["count"] == 1
    assert ls2["approvals"][0]["approval_id"] != aid
    # Unauthenticated is refused like the rest of the surface.
    assert c.get("/v1/messaging/approvals").status_code in (401, 403)


def test_http_send_validation_and_missing_approval():
    stack = _gui_stack()
    Scripted(stack)
    c = _client(stack)
    assert c.post("/v1/messaging/send", json={"text": "y"},
                  headers=OP).status_code == 400
    assert c.post("/v1/messaging/send",
                  json={"snapshot": _chat_list(), "conversation": "Dev Team"},
                  headers=OP).status_code == 400
    assert c.get("/v1/messaging/approvals/ghost",
                 headers=OP).status_code == 404
    # Unauthenticated is refused by the existing auth dependency.
    assert c.post("/v1/messaging/send",
                  json={"snapshot": _chat_list(), "conversation": "x",
                        "text": "y"}).status_code in (401, 403)
    # A snapshot-less request is valid (the read moves into the approval).
    r = c.post("/v1/messaging/send",
               json={"conversation": "Dev Team", "text": "hi",
                     "device_id": "phone"}, headers=OP)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"


# ---------- natural-language routing ----------

def test_local_parser_matches_messaging_shapes():
    from core.intent import LocalIntentParser, ParserContext
    ctx = ParserContext(
        known_capabilities={"messaging.send"},
        capability_schemas={"messaging.send": {
            "type": "object", "required": ["conversation", "text"],
            "properties": {"conversation": {"type": "string"},
                           "text": {"type": "string"}}}})
    lp = LocalIntentParser()
    a = lp.parse('send "running late" to "Dev Team"', ctx)
    assert a.capability == "messaging.send", a
    assert a.parameters["conversation"] == "Dev Team"
    assert a.parameters["text"] == "running late"
    b = lp.parse('message "Asha" saying "hello there"', ctx)
    assert b.capability == "messaging.send"
    assert b.parameters["conversation"] == "Asha"
    assert b.parameters["text"] == "hello there"
    c = lp.parse("tell 'Dev Team' 'on my way'", ctx)
    assert c.capability == "messaging.send"
    assert c.parameters["conversation"] == "Dev Team"
    assert c.parameters["text"] == "on my way"


def test_intent_turn_presents_one_messaging_approval():
    stack = _gui_stack()
    sc = Scripted(stack)
    out = stack["intent"].handle_utterance(
        'send "running late" to "Dev Team"', "user", "phone")
    assert out["proposal"]["capability"] == "messaging.send", out
    assert out["executed"] is False
    assert out["approval_id"]
    assert "Dev Team" in out["message"]
    assert sc.taps == [] and sc.sets == []  # nothing executed yet


def test_intent_then_approve_sends_end_to_end():
    stack = _gui_stack()
    sc = Scripted(stack)
    sc.inspects = [_chat_list(), _composer(), _composer(),
                   _with_sent("s3:1", "running late")]
    out = stack["intent"].handle_utterance(
        'send "running late" to "Dev Team"', "user", "phone")
    ap = stack["messaging"].approve(out["approval_id"])
    assert ap["status"] == "succeeded", ap
    assert len(sc.taps) == 2 and len(sc.sets) == 1


def test_intent_messaging_without_workflow_is_honest():
    """A service built without a messaging workflow answers honestly and
    never pretends to send."""
    from core.intent import (IntentTurnService, LocalIntentParser)
    from core.resolver import CapabilityResolver
    stack = _gui_stack()
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            LocalIntentParser(), stack["audit"],
                            stack["missions"], None)
    out = svc.handle_utterance('send "hi" to "Dev Team"', "user", "phone")
    assert out["executed"] is False
    assert "isn't available" in out["message"].lower()


def test_intent_messaging_without_a_capable_device_is_honest():
    """No device can act on the UI → honest message, nothing executed."""
    from core.app import build_stack
    stack = build_stack()
    out = stack["intent"].handle_utterance(
        'send "hi" to "Dev Team"', "user")
    assert out["executed"] is False
    assert out["proposal"]["capability"] == "messaging.send"
    assert "no phone is available" in out["message"].lower()


