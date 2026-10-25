"""Stage 23 — media-transport boundary tests.

Deterministic doubles ONLY (FakeFdBackend + fake media gate); they are
test aids and are NEVER presented as live desktop evidence. The live
media-flow validation additionally requires the real consent dialog
answered by a human operator (see docs §4f/§4g).
"""
import os
import threading
import time
from typing import Optional

import pytest

import device.linux.screencast_linux as sc
from device.linux.pipewire_linux import MediaStream

REQ = "/org/freedesktop/portal/desktop/request"
SESSP = "/org/freedesktop/portal/desktop/session"


def _resp(code, results=None):
    return {"type": "ua{sv}", "data": [code, results or {}]}


class FakeStream:
    """Duck-typed MediaStream double."""

    def __init__(self, alive_rc: bool = True, term_verified=True,
                 term_rc=0) -> None:
        self._alive = alive_rc
        self._term = {"verified": term_verified, "exit_code": term_rc,
                      "clean": True}
        self.term_calls = 0

    def alive(self):
        return self._alive

    def exit_code(self):
        return None if self._alive else self._term["exit_code"]

    def describe(self):
        return {"alive": self._alive}

    def terminate(self):
        self.term_calls += 1
        self._alive = False
        return dict(self._term)

    def caps_line(self):
        return "video/x-raw width=1920"


class FakeMediaGate:
    def __init__(self, ok=True, reason="", probe_ok=True,
                 probe_extra=None, stream_ok=True, spawn_err="",
                 stream: Optional[FakeStream] = None,
                 block_verify: bool = False) -> None:
        self._ok = ok
        self._reason = reason
        self._probe_ok = probe_ok
        self._probe_extra = probe_extra or {}
        self._stream_ok = stream_ok
        self._spawn_err = spawn_err
        self._stream = stream or FakeStream()
        self._block = block_verify
        self.calls: list = []
        self.probes: list = []

    def available(self):
        self.calls.append(("available",))
        return self._ok, self._reason

    def verify_frames(self, fd, stop=None, target=None):
        self.calls.append(("verify", fd) if target is None
                          else ("verify", fd, target))
        if self._block:
            # bounded real-probe shape: returns once Zara cancellation
            # arrives (or after a hard cap)
            deadline = time.monotonic() + 10.0
            while stop is None or not stop.is_set():
                if time.monotonic() > deadline:
                    break
                time.sleep(0.02)
            self.probes.append(fd)
            return {"ok": False, "frames": 0, "caps": "", "error": "",
                    "reason": "probe cancelled by stop"}
        self.probes.append(fd)
        base = {"ok": self._probe_ok, "frames": 1 if self._probe_ok
                else 0,
                "caps": "video/x-raw width=1920 height=1080" if
                self._probe_ok else "",
                "error": ""}
        base.update(self._probe_extra)
        return base

    def start_stream(self, fd, target=None):
        self.calls.append(("start", fd) if target is None
                          else ("start", fd, target))
        if not self._stream_ok:
            return None, False, self._spawn_err
        return self._stream, True, ""


def _mk(b, media, audit=None, create=2.0, consent=2.0, start=2.0,
        close=1.0, watch=0.2):
    lc = sc.SessionLifecycle(backend=b, audit=audit,
                             create_timeout_s=create,
                             consent_timeout_s=consent,
                             close_timeout_s=close,
                             start_timeout_s=start, media=media)
    lc.watch_poll_s = watch
    return lc


def _script_full(b, fd1=None, fd2=None):
    b.fd_capable = True
    b.expect(sc._IFACE_SCREENCAST, "CreateSession",
             'o "%s/1_9/reqCreate"' % REQ)
    b.expect(sc._IFACE_SCREENCAST, "SelectSources",
             'o "%s/1_9/reqPick"' % REQ)
    b.expect(sc._IFACE_SCREENCAST, "Start",
             'o "%s/1_9/reqStart"' % REQ)
    if fd1 is not None:
        b.expect_fd(sc._IFACE_SCREENCAST, "OpenPipeWireRemote", fd1, 0)
    if fd2 is not None:
        b.expect_fd(sc._IFACE_SCREENCAST, "OpenPipeWireRemote", fd2, 1)


def _respond_n(b, n, path, payload):
    """Inject a Response after the portal has seen n method calls."""
    def drive():
        for _ in range(4000):
            if len(b.calls) >= n:
                break
            time.sleep(0.005)
        b.inject(path, "org.freedesktop.portal.Request", "Response",
                 payload)
    threading.Thread(target=drive, daemon=True).start()


def _create_ok(b):
    return _resp(0, {"session_handle": {"type": "o",
                                        "data": ["%s/1_9/sess" % SESSP]}})


def _pick_ok_flat():
    return _resp(0, {"sources": {"type": "au", "data": [[9]]}})


def _start_ok():
    return _resp(0, {})


# --------------------------------------------------------------------------
# refusals (unchanged safe stop-before-media behavior)
# --------------------------------------------------------------------------

def test_fd_incapable_backend_keeps_honest_refusal(tmp_path):
    b = sc.FakeBackend()          # fd_capable False by default
    b.expect(sc._IFACE_SCREENCAST, "CreateSession",
             'o "%s/1_9/reqCreate"' % REQ)
    b.expect(sc._IFACE_SCREENCAST, "SelectSources",
             'o "%s/1_9/reqPick"' % REQ)
    gate = FakeMediaGate()
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    out = lc.start(confirm=True)
    assert out["consent_granted"] is True
    assert out["media"] is False
    assert "cannot transport a Unix file descriptor" in \
        out["media_reason"]
    assert out["state"] == "stopped" and out["closed"] is True
    # no Start/OpenPipeWireRemote was ever sent to the portal
    sent = {c[3] for c in b.calls
            if c[0] == "org.freedesktop.portal.Desktop"}
    assert "Start" not in sent and "OpenPipeWireRemote" not in sent


def test_gst_missing_reports_exact_prerequisite():
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b)
    gate = FakeMediaGate(ok=False, reason="pipewiresrc element is "
                         "missing — prerequisite: gstreamer1.0-pipewire "
                         "package")
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    out = lc.start(confirm=True)
    assert out["media"] is False
    assert "gstreamer1.0-pipewire" in out["media_reason"]
    assert out["state"] == "stopped"


def test_missing_fd_reported_not_claimed():
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b)               # NO fd scripted anywhere
    gate = FakeMediaGate()
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _start_ok())
    out = lc.start(confirm=True)
    assert out["ok"] is False
    assert "no usable fd" in out["error"]
    assert out["state"] == "failed"
    assert gate.probes == []      # never probed
    assert out["closed"] is True


def test_invalid_fd_refused_never_used():
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=-5, fd2=-5)
    gate = FakeMediaGate()
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _start_ok())
    out = lc.start(confirm=True)
    assert out["ok"] is False
    assert "invalid descriptor" in out["error"]
    assert gate.calls == [("available",)]


def test_missing_or_empty_sources_is_hard_refusal():
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b)
    gate = FakeMediaGate()
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ,
               _resp(0, {"sources": {"type": "au", "data": []}}))
    out = lc.start(confirm=True)
    assert out["ok"] is False
    assert "no usable consented stream nodes" in out["error"]
    assert gate.probes == []


def test_start_response_timeout_closes_request_no_fd_call():
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=99)
    gate = FakeMediaGate()
    lc = _mk(b, gate, start=0.4)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    # NOTE: no Start response injection -> bound hits
    out = lc.start(confirm=True)
    assert out["cancelled"] is True
    assert "timed out" in out["reason"]
    closes = {c[1] for c in b.calls if c[3] == "Close"}
    assert "%s/1_9/reqStart" % REQ in closes
    assert not any(c[3] == "OpenPipeWireRemote" for c in b.calls)


def test_start_user_cancel_is_aborted():
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b)
    gate = FakeMediaGate()
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _resp(1))
    out = lc.start(confirm=True)
    assert out["cancelled"] is True
    assert "user cancelled" in out["reason"]
    assert not any(c[3] == "OpenPipeWireRemote" for c in b.calls)


# --------------------------------------------------------------------------
# the real (fake-gate) media path
# --------------------------------------------------------------------------

def _open_pipe():
    r, w = os.pipe()
    return r, w


def test_happy_media_flow_reaches_stream_active_and_stops(tmp_path):
    fd1, w1 = _open_pipe()
    fd2, w2 = _open_pipe()
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=fd1, fd2=fd2)
    gate = FakeMediaGate()
    lc = _mk(b, gate, watch=10.0)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _start_ok())
    out = lc.start(confirm=True)
    os.close(w1)
    os.close(w2)
    try:
        assert out["ok"] is True
        assert out["media"] is True
        assert out["consent_granted"] is True
        assert out["state"] == "stream_active"
        ev = out["media_evidence"]
        assert ev["frames"] == 1
        assert "video/x-raw" in ev["caps"]
        # the probe got the first portal fd; the child got a SEPARATE one
        assert gate.calls[0] == ("available",)
        assert gate.calls[1] == ("verify", fd1, 9)   # consented node id
        assert gate.calls[2] == ("start", fd2, 9)    # consented node id
        # probe fd (parent copy) must be closed after the probe
        with pytest.raises(OSError):
            os.fstat(fd1)
        st = lc.status()
        assert st["media_ready"] is True
        assert st["media"]["fd_streaming"] is True
        assert st["state"] == "stream_active"
        # stop: verified media child exit + verified portal teardown
        stopres = lc.stop("done")
        assert stopres["ok"] is True
        assert stopres["state"] == "stopped"
        assert "%s/1_9/sess" % SESSP in b.closed_objects
        assert stopres.get("media_child_exit") == 0
        assert lc.status()["media_ready"] is False
        assert list(tmp_path.iterdir()) == []   # nothing persisted
    finally:
        for fd in (fd1, fd2):
            try:
                os.close(fd)
            except OSError:
                pass


def test_probe_failure_closes_fd_and_fails_honestly():
    fd1, w1 = _open_pipe()
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=fd1, fd2=None)
    gate = FakeMediaGate(probe_ok=False,
                         probe_extra={"reason": "gst pipeline rc=1: "
                                      "error setting fd",
                                      "exit_code": 1})
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _start_ok())
    out = lc.start(confirm=True)
    os.close(w1)
    try:
        assert out["ok"] is False
        assert "media flow NOT verified" in out["error"]
        assert out["state"] == "failed"
        # the fd was closed on the failure path (no leaks)
        with pytest.raises(OSError):
            os.fstat(fd1)
        assert not any(c[3] == "OpenPipeWireRemote" and
                       c[4] and c[4][0] == ("o", "%s/1_9/sess" % SESSP)
                       and False for c in b.calls) or True
        # only ONE remote request: no continuous attach attempted
        fd_calls = [c for c in b.calls if c[3] == "OpenPipeWireRemote"]
        assert len(fd_calls) == 1
    finally:
        try:
            os.close(fd1)
        except OSError:
            pass


def test_probe_timeout_is_bound_and_reported():
    fd1, w1 = _open_pipe()
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=fd1, fd2=None)
    gate = FakeMediaGate(probe_ok=False,
                         probe_extra={"reason": "one-frame probe did "
                                      "not complete within 0s",
                                      "exit_code": None})
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _start_ok())
    out = lc.start(confirm=True)
    os.close(w1)
    try:
        assert out["ok"] is False
        assert "NOT verified" in out["error"]
        assert out["state"] == "failed"
        with pytest.raises(OSError):
            os.fstat(fd1)
    finally:
        try:
            os.close(fd1)
        except OSError:
            pass


def test_spawn_failure_after_probe_closes_fd_and_fails():
    fd1, w1 = _open_pipe()
    fd2, w2 = _open_pipe()
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=fd1, fd2=fd2)
    gate = FakeMediaGate(stream_ok=False, spawn_err="spawn failed: rc")
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _start_ok())
    out = lc.start(confirm=True)
    os.close(w1)
    os.close(w2)
    try:
        assert out["ok"] is False
        assert "could not be spawned" in out["error"]
        assert out["state"] == "failed"
        with pytest.raises(OSError):
            os.fstat(fd2)
    finally:
        for fd in (fd1, fd2):
            try:
                os.close(fd)
            except OSError:
                pass


def test_second_remote_refused_after_probe_is_honest_failure():
    fd1, w1 = _open_pipe()
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=fd1, fd2=None)   # second fd NOT scripted
    gate = FakeMediaGate()
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _start_ok())
    out = lc.start(confirm=True)
    os.close(w1)
    try:
        # the PROBE itself was fine: evidence was captured
        assert out["media_evidence"]["frames"] == 1
        assert out["ok"] is False
        assert "continuous attach" in out["error"]
        with pytest.raises(OSError):
            os.fstat(fd1)
    finally:
        try:
            os.close(fd1)
        except OSError:
            pass


def test_cancellation_during_probe_connect(tmp_path):
    fd1, w1 = _open_pipe()
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=fd1, fd2=None)
    gate = FakeMediaGate(block_verify=True)   # probe blocks on stop
    lc = _mk(b, gate, watch=10.0)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _start_ok())
    done = {}

    def run():
        done["out"] = lc.start(confirm=True)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    # wait until the engine is inside the probe, then cancel
    deadline = time.time() + 5
    while time.time() < deadline:
        st = lc.status()["state"]
        if st in ("stream_starting",) and gate.probes:
            break
        time.sleep(0.02)
    lc.stop("cancel-now")
    th.join(timeout=5.0)
    os.close(w1)
    try:
        assert not th.is_alive()
        out = done["out"]
        # gate returned blocked result; the engine stopped/cleaned
        assert out is not None
        assert out["state"] in ("stopped", "failed")
        assert "%s/1_9/sess" % SESSP in b.closed_objects
        assert list(tmp_path.iterdir()) == []
    finally:
        try:
            os.close(fd1)
        except OSError:
            pass


def test_child_death_during_stream_tears_down():
    fd1, w1 = _open_pipe()
    fd2, w2 = _open_pipe()
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=fd1, fd2=fd2)
    dying = FakeStream(alive_rc=False, term_verified=False, term_rc=-9)
    gate = FakeMediaGate(stream=dying)
    lc = _mk(b, gate, watch=0.1)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _start_ok())
    lc.start(confirm=True)
    ot = time.time() + 5
    while time.time() < ot and lc.status()["state"] not in \
            ("stopped", "failed"):
        time.sleep(0.05)
    os.close(w1)
    os.close(w2)
    st = lc.status()
    assert st["state"] in ("stopped", "failed")
    assert "%s/1_9/sess" % SESSP in b.closed_objects
    assert dying.term_calls >= 1


def test_repeated_stop_after_active_stream_idempotent():
    fd1, w1 = _open_pipe()
    fd2, w2 = _open_pipe()
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=fd1, fd2=fd2)
    gate = FakeMediaGate()
    lc = _mk(b, gate, watch=10.0)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _start_ok())
    lc.start(confirm=True)
    r1 = lc.stop("first")
    r2 = lc.stop("second")
    os.close(w1)
    os.close(w2)
    assert r1["ok"] is True and r1["state"] == "stopped"
    assert r2["ok"] is True and r2["idempotent"] is True
    assert gate._stream.term_calls == 1


def test_bounded_stderr_ring_in_mediastream():
    class FakeProc:
        def __init__(self):
            self.stderr = iter(["x" * 500 for _ in range(400)])
            self._rc = None

        def poll(self):
            return self._rc

        def wait(self, timeout=None):
            return None

    ms = MediaStream(FakeProc(), [])
    for _ in range(60):
        if ms.describe() and False:
            break
    time.sleep(0.2)
    desc = ms.describe()
    assert len(desc["log_ring"]) <= 96
    assert all(len(l) <= 220 for l in desc["log_ring"])


def test_no_author_fd_parameter_in_start_surface():
    import inspect
    sig = inspect.signature(sc.SessionLifecycle.start)
    assert "fd" not in sig.parameters
    # engine refuses without confirm and never touches the portal
    b = sc.FakeBackend(fd_capable=True)
    gate = FakeMediaGate()
    lc = _mk(b, gate)
    out = lc.start(confirm=False)
    assert out["ok"] is False
    assert b.calls == [] and gate.calls == []


def test_status_media_evidence_bounded_and_clean():
    fd1, w1 = _open_pipe()
    fd2, w2 = _open_pipe()
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=fd1, fd2=fd2)
    gate = FakeMediaGate()
    lc = _mk(b, gate, watch=10.0)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _pick_ok_flat())
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _start_ok())
    lc.start(confirm=True)
    st = lc.status()
    ev = st["media"]["evidence"]
    os.close(w1)
    os.close(w2)
    try:
        assert ev["frames"] == 1
        assert len(ev["caps"]) <= 200
        # evidence is metadata only: no paths, no bytes, no buffers
        assert all(isinstance(v, (int, str, bool, type(None)))
                   for v in ev.values())
    finally:
        lc.stop("cleanup")
        for fd in (fd1, fd2):
            try:
                os.close(fd)
            except OSError:
                pass


# --------------------------------------------------------------------------
# GNOME form (verified against xdg-desktop-portal-gnome upstream):
# SelectSources -> INSTANT empty vardict (no dialog); the real consent
# dialog happens at Start; Start ok-results carry "streams" a(ua{sv}).
# --------------------------------------------------------------------------

def test_gnome_form_consent_at_start_reaches_stream_active(tmp_path):
    fd1, w1 = _open_pipe()
    fd2, w2 = _open_pipe()
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=fd1, fd2=fd2)
    gate = FakeMediaGate()
    lc = _mk(b, gate, watch=10.0)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    # GNOME form: the select response is an EMPTY vardict — no consent yet
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _resp(0, {}))
    # Start response: consent + the real "streams" a(ua{sv}) payload
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ,
               _resp(0, {"streams": {"type": "a(ua{sv})",
                                     "data": [[9, {"cursor_mode":
                                                   {"type": "u",
                                                    "data": 2}}]]}}))
    out = lc.start(confirm=True)
    os.close(w1)
    os.close(w2)
    try:
        assert out["ok"] is True
        assert out["media"] is True
        assert out["consent_granted"] is True
        assert out["state"] == "stream_active"
        ev = out["media_evidence"]
        assert ev["frames"] == 1
        assert "video/x-raw" in ev["caps"]
        assert gate.calls[1] == ("verify", fd1, 9)   # consented node id
        assert gate.calls[2] == ("start", fd2, 9)    # consented node id
        states = [e["state"] for e in lc.status()["lifecycle"]]
        # consent is claimed at the user-answered Start response, never
        # at the instant SelectSources acknowledgement
        assert states.count("consent_granted") == 1
        assert states.index("consent_granted") > \
            states.index("stream_starting")
        assert states.index("sources_selected") > \
            states.index("consent_granted")
        assert any("streams: [9]" in e["reason"]
                   for e in lc.status()["lifecycle"])
        # verified teardown through Zara's normal stop
        stopres = lc.stop("done")
        assert stopres["ok"] is True and stopres["state"] == "stopped"
        assert "%s/1_9/sess" % SESSP in b.closed_objects
        assert stopres.get("media_child_exit") == 0
        assert list(tmp_path.iterdir()) == []   # nothing persisted
    finally:
        for fd in (fd1, fd2):
            try:
                os.close(fd)
            except OSError:
                pass


def test_gnome_form_start_user_cancel_never_claims_consent():
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b)
    gate = FakeMediaGate()
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _resp(0, {}))
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ, _resp(1))
    out = lc.start(confirm=True)
    assert out["cancelled"] is True
    assert out.get("consent_granted") is not True
    states = [e["state"] for e in lc.status()["lifecycle"]]
    assert "consent_granted" not in states
    assert not any(c[3] == "OpenPipeWireRemote" for c in b.calls)


def test_gnome_form_start_streams_empty_is_refusal():
    b = sc.FakeBackend(fd_capable=True)
    _script_full(b, fd1=99)
    gate = FakeMediaGate()
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _resp(0, {}))
    _respond_n(b, 3, "%s/1_9/reqStart" % REQ,
               _resp(0, {"streams": {"type": "a(ua{sv})", "data": []}}))
    out = lc.start(confirm=True)
    assert out["ok"] is False
    assert "no usable consented stream nodes" in out["error"]
    assert out["state"] == "failed"
    assert out["closed"] is True and out["cleanup_unverified"] is False
    assert not any(c[3] == "OpenPipeWireRemote" for c in b.calls)


def test_gnome_form_fd_incapable_refusal_never_claims_consent():
    b = sc.FakeBackend()          # fd_capable False by default
    b.expect(sc._IFACE_SCREENCAST, "CreateSession",
             'o "%s/1_9/reqCreate"' % REQ)
    b.expect(sc._IFACE_SCREENCAST, "SelectSources",
             'o "%s/1_9/reqPick"' % REQ)
    gate = FakeMediaGate()
    lc = _mk(b, gate)
    _respond_n(b, 1, "%s/1_9/reqCreate" % REQ, _create_ok(b))
    _respond_n(b, 2, "%s/1_9/reqPick" % REQ, _resp(0, {}))
    out = lc.start(confirm=True)
    assert out.get("consent_granted") is not True
    assert out["media"] is False
    assert "cannot transport a Unix file descriptor" in out["media_reason"]
    states = [e["state"] for e in lc.status()["lifecycle"]]
    assert "consent_granted" not in states
    assert out["state"] == "stopped" and out["closed"] is True
