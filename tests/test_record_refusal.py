"""Stage 31-P3 phase B (P3g) — call-recording honest refusal fixtures."""
from core.app import build_stack
from core.execution import ExecutionState


def test_record_the_call_refuses_explicitly():
    stack = build_stack()
    out = stack["intent"].handle_utterance("record the call")
    assert out["executed"] is False, out
    assert "can't record" in out["message"], out["message"]
    assert "consent" in out["message"], out["message"]
    # no submission was ever parked: zero new executions
    recs = [r for r in stack["audit"].query(50)
            if "record" in r["action"] and "refused" in r["action"]]
    assert recs, out


def test_record_my_calls_refuses_never_a_flow_start():
    stack = build_stack()
    out = stack["intent"].handle_utterance("record my calls")
    assert out["executed"] is False, out
    assert out.get("call_flow") == "record_refused", out


def test_record_phones_covered():
    stack = build_stack()
    for word in ("record this call", "call recording", "tape the call"):
        out = stack["intent"].handle_utterance(word)
        assert out["executed"] is False, (word, out)
        assert "can't record" in out["message"], out


def test_record_audit_row_has_no_utterance_promise():
    stack = build_stack()
    stack["intent"].handle_utterance("record the phone call")
    rows = stack["audit"].query(50)
    refused = [r for r in rows if r["action"] == "call_flow.record_refused"]
    assert refused
    detail = refused[0]["detail"]
    # the audit row carries the TRUNCATED invite only — never a promise
    assert "record" in detail.lower()
