"""Stage 22c — consent-driven screen-cast lifecycle tests.

All portal interaction is exercised against a deterministic FakeBackend.
The fake is a TEST DOUBLE, never presented as live-desktop evidence
(the live validation for the real consent dialog is separately gated on
an operator being present to see and answer the dialog).
"""
import threading
import time

import pytest

import device.linux.screencast_linux as sc
import device.linux.tools_linux as LT

REQ = "/org/freedesktop/portal/desktop/request"
SESSP = "/org/freedesktop/portal/desktop/session"


def _mk(backend, audit=None, create=2.0, consent=2.0, close=1.0):
    return sc.SessionLifecycle(backend=backend, audit=audit,
                               create_timeout_s=create,
                               consent_timeout_s=consent,
                               close_timeout_s=close)


def _resp_payload(code, results=None):
    return sc.FakeBackend.response(code, results)


def _lazy_inject(b, nth_call, path, payload):
    """Respond to the nth backend call by injecting a Response event."""
    def drive():
        for _ in range(2000):
            if len(b.calls) >= nth_call:
                b.inject(path, "org.freedesktop.portal.Request",
                         "Response", payload)
                return
            time.sleep(0.005)
    return drive


# --------------------------------------------------------------------------
# wiring + unit pieces
# --------------------------------------------------------------------------

def test_sv_args_count_style_and_objpath_parse():
    args = sc._sv_args([("handle_token", "s", "t1"), ("multiple", "b", False),
                        ("types", "u", 1)])
    assert args[0] == 3
    assert args[1:4] == ["handle_token", "s", "t1"]
    assert args[4:7] == ["multiple", "b", "false"]
    assert args[7:10] == ["types", "u", 1]
    assert sc.parse_objpath(
        'o "%s/1_9/req"' % REQ) == "%s/1_9/req" % REQ
    assert sc.parse_objpath("garbage") is None
    assert sc.parse_objpath(None) is None


def test_tools_registered_with_parity():
    core = __import__("core.device_tools",
                      fromlist=["DEVICE_TOOL_DEFS"])
    core_defs = {d.name: d for d in core.DEVICE_TOOL_DEFS}
    linux_defs = {d.name: d for d, _ in LT.LINUX_TOOLS}
    for name in ("display.cast_start", "display.cast_stop"):
        assert name in core_defs and name in linux_defs
        assert core_defs[name].risk == linux_defs[name].risk
        assert core_defs[name].input_schema == linux_defs[name].input_schema
        assert set(core_defs[name].required_capabilities) == \
            set(linux_defs[name].required_capabilities)
    # timing invariant: every def stays > 60 s on both sides
    for defs in (core_defs, linux_defs):
        for name in ("display.cast_start", "display.cast_stop",
                     "display.stream_status"):
            assert defs[name].timeout_s > 60.0
    assert core_defs["display.cast_start"].risk.value >= \
        core_defs["display.set_config"].risk.value


def test_intent_aliases_present():
    import core.intent as intent
    table = {t[0]: t[1] for t in intent._ALIASES}
    assert table.get("start screen sharing") == "display.cast_start"
    assert table.get("share my screen") == "display.cast_start"
    assert table.get("stop screen sharing") == "display.cast_stop"
    assert table.get("is my screen being shared") == "display.stream_status"


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def test_start_without_confirm_refused_and_nothing_sent():
    b = sc.FakeBackend()
    lc = _mk(b)
    out = lc.start(confirm=False)
    assert out["ok"] is False
    assert "confirm" in out["error"]
    assert b.calls == []
    assert out["state"] == "idle"


def _script_happy(b):
    b.expect(sc._IFACE_SCREENCAST, "CreateSession",
             'o "%s/1_9/reqCreate"' % REQ)
    b.expect(sc._IFACE_SCREENCAST, "SelectSources",
             'o "%s/1_9/reqPick"' % REQ)


def test_consent_granted_ends_in_verified_stop_with_media_refusal():
    b = sc.FakeBackend()
    _script_happy(b)
    lc = _mk(b)
    threading.Thread(target=_lazy_inject(
        b, 1, "%s/1_9/reqCreate" % REQ,
        _resp_payload(0, {"session_handle": "%s/1_9/sess" % SESSP})),
        daemon=True).start()
    threading.Thread(target=_lazy_inject(
        b, 2, "%s/1_9/reqPick" % REQ,
        _resp_payload(0, {"sources": {"type": "au", "data": [[7]]}})),
        daemon=True).start()
    out = lc.start(confirm=True)
    assert out["consent_granted"] is True
    assert out["media"] is False
    assert "no stream" in out["media_reason"]
    assert out["state"] == "stopped"
    assert out["closed"] is True
    assert out["cleanup_unverified"] is False
    # the session was really closed on the fake portal
    assert "%s/1_9/sess" % SESSP in b.closed_objects
    st = lc.status()
    assert st["media_ready"] is False
    assert st["session_handle"] is None


def test_user_cancel_consent_reports_cancelled_not_failed():
    b = sc.FakeBackend()
    _script_happy(b)
    lc = _mk(b)
    threading.Thread(target=_lazy_inject(
        b, 1, "%s/1_9/reqCreate" % REQ,
        _resp_payload(0, {"session_handle": "%s/1_9/sess" % SESSP})),
        daemon=True).start()
    threading.Thread(target=_lazy_inject(
        b, 2, "%s/1_9/reqPick" % REQ, _resp_payload(1)),
        daemon=True).start()
    out = lc.start(confirm=True)
    assert out["cancelled"] is True
    assert "user cancelled" in out["reason"]
    assert out["state"] == "stopped"
    assert lc.status()["session_handle"] is None


def test_portal_denial_fails_and_verifies_teardown():
    b = sc.FakeBackend()
    _script_happy(b)
    lc = _mk(b)
    threading.Thread(target=_lazy_inject(
        b, 1, "%s/1_9/reqCreate" % REQ,
        _resp_payload(0, {"session_handle": "%s/1_9/sess" % SESSP})),
        daemon=True).start()
    threading.Thread(target=_lazy_inject(
        b, 2, "%s/1_9/reqPick" % REQ, _resp_payload(2)),
        daemon=True).start()
    out = lc.start(confirm=True)
    assert out["ok"] is False
    assert "did not grant" in out["error"]
    assert out["state"] == "failed"
    # teardown of the consented session was still verified
    assert "%s/1_9/sess" % SESSP in b.closed_objects


def test_create_session_timeout_closes_pending_request():
    b = sc.FakeBackend()
    b.expect(sc._IFACE_SCREENCAST, "CreateSession",
             'o "%s/1_9/reqCreate"' % REQ)
    lc = _mk(b, create=0.3)
    out = lc.start(confirm=True)
    assert out["ok"] is True and out["cancelled"] is True
    assert "timed out" in out["reason"]
    closes = [c for c in b.calls if c[3] == "Close"]
    assert ("%s/1_9/reqCreate" % REQ) in {c[1] for c in closes}
    assert out["state"] == "stopped"
    assert lc.status()["session_handle"] is None


def test_select_request_timeout_closes_pending_request():
    b = sc.FakeBackend()
    _script_happy(b)
    lc = _mk(b, consent=0.3)
    threading.Thread(target=_lazy_inject(
        b, 1, "%s/1_9/reqCreate" % REQ,
        _resp_payload(0, {"session_handle": "%s/1_9/sess" % SESSP})),
        daemon=True).start()
    out = lc.start(confirm=True)
    assert out["ok"] is True and out["cancelled"] is True
    assert "did not answer the source-selection request" in out["reason"]
    closes = [c for c in b.calls if c[3] == "Close"]
    assert ("%s/1_9/reqPick" % REQ) in {c[1] for c in closes}
    assert ("%s/1_9/sess" % SESSP) in {c[1] for c in closes}


def test_zara_stop_during_consent_wakes_wait_and_closes_dialog():
    b = sc.FakeBackend()
    _script_happy(b)
    lc = _mk(b, consent=30.0)          # generous: must be woken, not expire
    threading.Thread(target=_lazy_inject(
        b, 1, "%s/1_9/reqCreate" % REQ,
        _resp_payload(0, {"session_handle": "%s/1_9/sess" % SESSP})),
        daemon=True).start()
    done = {}

    def run():
        done["out"] = lc.start(confirm=True)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    # wait until the engine is inside the consent wait, then stop
    for _ in range(200):
        if lc.status()["state"] == "awaiting_user_consent":
            break
        time.sleep(0.01)
    out_stop = lc.stop("test-cancel")
    t.join(timeout=5.0)
    assert not t.is_alive(), "start() must be cancellable promptly"
    out = done["out"]
    assert out["cancelled"] is True
    assert out["state"] == "stopped"
    assert out_stop["ok"] is True
    assert "%s/1_9/reqPick" % REQ in {c[1] for c in b.calls
                                      if c[3] == "Close"}


def test_dbus_disconnect_reports_portal_unreachable():
    b = sc.FakeBackend()      # no scripts: every call returns None
    lc = _mk(b)
    out = lc.start(confirm=True)
    assert out["ok"] is False
    assert "unreachable" in out["error"]
    assert out["state"] == "failed"
    assert lc.status()["session_handle"] is None


def test_missing_session_handle_refuses_to_proceed():
    b = sc.FakeBackend()
    b.expect(sc._IFACE_SCREENCAST, "CreateSession",
             'o "%s/1_9/reqCreate"' % REQ)
    lc = _mk(b)
    threading.Thread(target=_lazy_inject(
        b, 1, "%s/1_9/reqCreate" % REQ, _resp_payload(0)),
        daemon=True).start()
    out = lc.start(confirm=True)
    assert out["ok"] is False
    assert "session_handle" in out["error"]
    # never reached SelectSources
    assert not [c for c in b.calls if c[3] == "SelectSources"]


def test_select_sources_wire_failure_is_failed_state():
    b = sc.FakeBackend()
    b.expect(sc._IFACE_SCREENCAST, "CreateSession",
             'o "%s/1_9/reqCreate"' % REQ)
    # SelectSources has NO script -> returns None -> wire failure
    lc = _mk(b)
    threading.Thread(target=_lazy_inject(
        b, 1, "%s/1_9/reqCreate" % REQ,
        _resp_payload(0, {"session_handle": "%s/1_9/sess" % SESSP})),
        daemon=True).start()
    out = lc.start(confirm=True)
    assert out["ok"] is False
    assert "SelectSources call failed" in out["error"]
    assert out["state"] == "failed"
    # the consented session was still torn down and verified
    assert "%s/1_9/sess" % SESSP in b.closed_objects


def test_duplicate_start_refused_while_session_live():
    b = sc.FakeBackend()
    _script_happy(b)
    lc = _mk(b, consent=30.0)
    gate = threading.Event()

    def drive():
        for _ in range(2000):
            if len(b.calls) >= 1:
                b.inject("%s/1_9/reqCreate" % REQ,
                         "org.freedesktop.portal.Request", "Response",
                         _resp_payload(0,
                                       {"session_handle":
                                        "%s/1_9/sess" % SESSP}))
                return
            time.sleep(0.005)
    threading.Thread(target=drive, daemon=True).start()
    done = {}

    def run():
        done["out"] = lc.start(confirm=True)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(200):
        if lc.status()["state"] == "awaiting_user_consent":
            break
        time.sleep(0.01)
    dup = lc.start(confirm=True)
    assert dup["ok"] is False
    assert dup["duplicate"] is True
    assert "duplicate starts are refused" in dup["error"]
    # finish cleanly
    lc.stop("teardown")
    t.join(timeout=5.0)
    assert not t.is_alive()


def test_stop_is_idempotent_and_harmless_when_idle():
    b = sc.FakeBackend()
    lc = _mk(b)
    out = lc.stop()
    assert out == {"ok": True, "idempotent": True, "state": "idle",
                   "detail": "no live session to stop"}
    out2 = lc.stop()
    assert out2["ok"] is True and out2["idempotent"] is True


def test_cleanup_failure_reported_honestly_not_silently():
    b = sc.FakeBackend()
    _script_happy(b)
    b.closed_reply_ok = False          # Close "call" never completes
    b.objects.add("%s/1_9/sess" % SESSP)   # session still lives
    lc = _mk(b)
    threading.Thread(target=_lazy_inject(
        b, 1, "%s/1_9/reqCreate" % REQ,
        _resp_payload(0, {"session_handle": "%s/1_9/sess" % SESSP})),
        daemon=True).start()
    threading.Thread(target=_lazy_inject(
        b, 2, "%s/1_9/reqPick" % REQ,
        _resp_payload(0, {"sources": {"type": "au", "data": [[4]]}})),
        daemon=True).start()
    out = lc.start(confirm=True)
    assert out["ok"] is False
    assert out["cleanup_unverified"] is True
    assert out["closed"] is False
    assert out["state"] == "failed"
    assert out["session_handle"] == "%s/1_9/sess" % SESSP
    assert "may still exist" in out["error"]
    # lifecycle log keeps the safe diagnostic handle for recovery
    log = lc.status()["lifecycle"]
    assert any("cleanup unverified" in e["reason"] for e in log)


def test_restart_allowed_after_failed_and_after_stopped():
    b = sc.FakeBackend()      # immediate failure (portal unreachable)
    lc = _mk(b)
    first = lc.start(confirm=True)
    assert first["state"] == "failed"
    b.expect(sc._IFACE_SCREENCAST, "CreateSession",
             'o "%s/1_9/reqCreate"' % REQ)
    # without further scripted responses this runs to a timed-out abort
    second = lc.start(confirm=True)
    assert second["ok"] is True and second["cancelled"] is True
    assert second["state"] == "stopped"


def test_lifecycle_log_bounded_and_records_transitions():
    b = sc.FakeBackend()
    _script_happy(b)
    events = []
    lc = _mk(b, audit=lambda a, act, det="": events.append((a, act, det)))
    threading.Thread(target=_lazy_inject(
        b, 1, "%s/1_9/reqCreate" % REQ,
        _resp_payload(0, {"session_handle": "%s/1_9/sess" % SESSP})),
        daemon=True).start()
    threading.Thread(target=_lazy_inject(
        b, 2, "%s/1_9/reqPick" % REQ, _resp_payload(0, {})),
        daemon=True).start()
    lc.start(confirm=True)
    log = lc.status()["lifecycle"]
    states = [e["state"] for e in log]
    assert states[:3] == ["requesting_session", "session_created",
                          "awaiting_user_consent"]
    assert len(log) <= 50
    acts = {act for _a, act, _d in events}
    # GNOME form: the instant empty select ack is NOT consent — it is
    # acknowledged as selection-without-nodes; consent happens at Start
    # (fd-incapable fake refuses before Start is ever sent)
    assert "consent_granted" not in acts
    assert {"start_requested", "awaiting_user_consent",
            "selection_acknowledged_no_nodes",
            "teardown_verified"} <= acts


def test_no_media_ever_persisted_no_frames_touched():
    b = sc.FakeBackend()
    _script_happy(b)
    lc = _mk(b)
    threading.Thread(target=_lazy_inject(
        b, 1, "%s/1_9/reqCreate" % REQ,
        _resp_payload(0, {"session_handle": "%s/1_9/sess" % SESSP})),
        daemon=True).start()
    threading.Thread(target=_lazy_inject(
        b, 2, "%s/1_9/reqPick" % REQ, _resp_payload(0, {})),
        daemon=True).start()
    lc.start(confirm=True)
    # the fake portal saw ONLY portal lifecycle calls — no pipewire,
    # no fd acquisition, no media of any kind
    methods = {c[3] for c in b.calls}
    assert methods <= {"CreateSession", "SelectSources", "Close"}
    assert lc.status()["media_ready"] is False


def test_temp_dirs_untouched_by_lifecycle(tmp_path):
    b = sc.FakeBackend()
    _script_happy(b)
    lc = _mk(b)
    threading.Thread(target=_lazy_inject(
        b, 1, "%s/1_9/reqCreate" % REQ,
        _resp_payload(0, {"session_handle": "%s/1_9/sess" % SESSP})),
        daemon=True).start()
    threading.Thread(target=_lazy_inject(
        b, 2, "%s/1_9/reqPick" % REQ, _resp_payload(0, {})),
        daemon=True).start()
    lc.start(confirm=True)
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# tool handlers
# --------------------------------------------------------------------------

def test_handlers_route_through_engine(monkeypatch):
    sent = {}

    class StubLc:
        def start(self, confirm=False):
            sent["start"] = confirm
            if not confirm:
                return {"ok": False, "state": "idle",
                        "error": "confirm required"}
            return {"ok": True, "state": "stopped"}

        def stop(self):
            sent["stop"] = True
            return {"ok": True, "state": "stopped"}

        def status(self):
            return {"state": "idle"}

    stub = StubLc()
    monkeypatch.setattr(sc, "_LIFECYCLE", stub)
    r1 = LT.display_cast_start({"confirm": True}, {})
    assert r1["ok"] is True and sent["start"] is True
    r2 = LT.display_cast_start({}, {})
    assert r2["ok"] is False and sent["start"] is False
    r3 = LT.display_cast_stop({}, {})
    assert r3["ok"] is True and sent["stop"] is True
    monkeypatch.setattr(sc, "_LIFECYCLE", None)


def test_stream_status_carries_lifecycle_state(monkeypatch):
    import device.linux.display_linux as dl
    monkeypatch.setattr(dl, "stream_status",
                        lambda: {"available": True, "marker": 1})
    # engine() path: status requested from the real singleton (idle)
    out = LT.display_stream_status({}, {})
    assert out["available"] is True
    assert out["marker"] == 1
    assert out["screencast_session"]["state"] == "idle"


def test_real_wire_bare_scalar_variant_decode():
    """REGRESSION (Stage 24 live run): busctl --json=short serializes
    SCALAR variants with a bare `data` value — the exact payload
    captured live in the Stage-22c monitor capture:
      {"type":"s","data":"/org/freedesktop/portal/desktop/session/…"}
    The fakes previously always injected list-wrapped data, so this
    real shape never regressed before the live run."""
    ev = {"interface": "org.freedesktop.portal.Request",
          "member": "Response",
          "path": "%s/1_578/zaraCreate" % REQ,
          "payload": {"type": "ua{sv}",
                      "data": [0,
                               {"session_handle":
                                {"type": "s", "data":
                                 "%s/1_578/zaraSess" % SESSP},
                                "sources": {"type": "au", "data": [9]},
                                "uint_opt": {"type": "u", "data": 2},
                                "bool_opt": {"type": "b", "data": False},
                                "nested": {"type": "v", "data":
                                           {"type": "o", "data": "/obj"}}}]}}
    code, results = sc._extract_response(ev)
    assert code == 0
    assert results["session_handle"] == "%s/1_578/zaraSess" % SESSP
    assert results["sources"] == 9            # bare 'au' first element
    assert results["uint_opt"] == 2
    assert results["bool_opt"] is False
    assert results["nested"] == "/obj"
    # and the engine accepts the create response end to end:
    b = sc.FakeBackend()
    _script_happy(b)
    lc = _mk(b)
    threading.Thread(target=_lazy_inject(
        b, 1, "%s/1_9/reqCreate" % REQ, ev["payload"]), daemon=True).start()
    threading.Thread(target=_lazy_inject(
        b, 2, "%s/1_9/reqPick" % REQ,
        {"type": "ua{sv}",
         "data": [0, {"sources": {"type": "au", "data": [7]}}]}),
        daemon=True).start()
    out = lc.start(confirm=True)
    assert out["consent_granted"] is True
    assert out["state"] == "stopped"
    assert [e["state"] for e in lc.status()["lifecycle"]] == [
        "requesting_session", "session_created",
        "awaiting_user_consent", "consent_granted", "sources_selected",
        "stream_starting", "stopping", "stopped"]
