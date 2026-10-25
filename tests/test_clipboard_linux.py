"""Consent-based clipboard access on the Linux agent (Stage 25).

Fixture-based: backend commands, session detection and read-back are
faked via the module's monkeypatch targets (_DETECT, _WHICH,
_run_bounded) — NO test ever reads or writes the user's REAL clipboard
(the task forbids that and the design never polls). Backend availability
statements about this machine are real, because shutil.which only
inspects PATH — it never touches clipboard content.
"""
import pytest

from device.linux import clipboard_linux as C


def _cancel_ctx(cancelled=False):
    return {"cancel_requested": (lambda: cancelled)}


# ---------------- session/backend honesty ----------------

def test_headless_session_reports_unsupported_honestly(monkeypatch):
    monkeypatch.setattr(C, "_DETECT", lambda: "headless")
    r = C.clipboard_read({}, {})
    assert r["supported"] is False
    assert r["text"] is None
    assert "no graphical session" in r["reason"]
    w = C.clipboard_write({"text": "hi"}, {})
    assert w["supported"] is False
    assert w["verified"] is None


def test_missing_backend_reports_unsupported_on_wayland(monkeypatch):
    monkeypatch.setattr(C, "_DETECT", lambda: "wayland")
    monkeypatch.setattr(C, "_WHICH", lambda name: None)
    r = C.clipboard_read({}, {})
    assert r["supported"] is False
    assert "wl-paste" in r["reason"]
    w = C.clipboard_write({"text": "hi"}, {})
    assert w["supported"] is False
    assert "wl-copy" in w["reason"]
    assert w["verified"] is None


def test_real_backend_availability_on_this_host():
    # Real PATH inspection only (never touches clipboard content).
    r = C.clipboard_read({}, {})
    session = C._DETECT()
    if session == "headless":
        assert r["supported"] is False
    else:
        # wl-copy/wl-paste/xclip/xsel exist in this deployment image;
        # if that ever changes the tool must report, not pretend.
        backend_expected = ("wl-paste" if session == "wayland"
                            else "xclip")
        assert (r["supported"] is True) or (backend_expected in r["reason"])


# ---------------- reads ----------------

def test_read_happy_path_x11_backend(monkeypatch):
    monkeypatch.setattr(C, "_DETECT", lambda: "x11")
    monkeypatch.setattr(C, "_WHICH", lambda name: "/usr/bin/xclip")
    seen = {}

    def fake_run(cmd, timeout, input_bytes=b""):
        seen["cmd"] = cmd
        return 0, "text from a fake clipboard".encode("utf-8"), b""

    monkeypatch.setattr(C, "_run_bounded", fake_run)
    r = C.clipboard_read({}, {})
    assert r["supported"] is True
    assert r["backend"] == "x11"
    assert r["text"] == "text from a fake clipboard"
    assert r["empty"] is False
    assert seen["cmd"][0] == "xclip" and "-selection" in seen["cmd"]
    assert not seen["cmd"][2].startswith("sh")   # shell=False by contract


def test_read_empty_clipboard_is_honest_not_a_success_pretense(monkeypatch):
    monkeypatch.setattr(C, "_DETECT", lambda: "x11")
    monkeypatch.setattr(C, "_WHICH", lambda name: "/usr/bin/xclip")
    monkeypatch.setattr(C, "_run_bounded",
                        lambda cmd, timeout, input_bytes=b"":
                        (1, b"", b""))
    r = C.clipboard_read({}, {})
    assert r["supported"] is True
    assert r["empty"] is True
    assert r["text"] is None


def test_read_backend_failure_reports_content_free_reason(monkeypatch):
    monkeypatch.setattr(C, "_DETECT", lambda: "wayland")
    monkeypatch.setattr(C, "_WHICH", lambda name: "/usr/bin/wl-paste")

    def fake_run(cmd, timeout, input_bytes=b""):
        return 1, b"", b"wayland compositor unavailable"

    monkeypatch.setattr(C, "_run_bounded", fake_run)
    r = C.clipboard_read({}, {})
    assert r["empty"] is True                  # rc<=1 + no output -> honest

    monkeypatch.setattr(C, "_run_bounded",
                        lambda cmd, timeout, input_bytes=b"":
                        (124, b"", b"clipboard backend timed out"))
    # A timeout is NOT "empty clipboard": it is an honest failure state,
    # never a fake success and never carrying logged content.
    r2 = C.clipboard_read({}, {})
    assert r2["empty"] is None
    assert r2.get("failed") is True
    assert r2["text"] is None
    assert "124" in r2["reason"]
    assert "timed out" not in r2["reason"]   # stderr never echoed


def test_read_caps_bounded_output(monkeypatch):
    monkeypatch.setattr(C, "_DETECT", lambda: "x11")
    monkeypatch.setattr(C, "_WHICH", lambda name: "/usr/bin/xclip")
    blob = ("x" * (C.MAX_CLIPBOARD_TEXT + 5000)).encode()
    monkeypatch.setattr(C, "_run_bounded",
                        lambda cmd, timeout, input_bytes=b"":
                        (0, blob, b""))
    r = C.clipboard_read({}, {})
    assert r["truncated"] is True
    assert r["chars"] == C.MAX_CLIPBOARD_TEXT
    assert len(r["text"]) == C.MAX_CLIPBOARD_TEXT


# ---------------- writes ----------------

def test_write_roundtrip_verified(monkeypatch):
    monkeypatch.setattr(C, "_DETECT", lambda: "wayland")
    monkeypatch.setattr(C, "_WHICH", lambda name: "/usr/bin/wl-copy")
    state = {"board": None}

    def fake_run(cmd, timeout, input_bytes=b""):
        if cmd[0] == "wl-copy":
            state["board"] = input_bytes.decode("utf-8")
            return 0, b"", b""
        if cmd[0] == "wl-paste":
            out = (state["board"] or "").encode("utf-8")
            rc = 0 if state["board"] is not None else 1
            return rc, out, b""
        raise AssertionError(cmd)

    monkeypatch.setattr(C, "_run_bounded", fake_run)
    r = C.clipboard_write({"text": "verified copy"}, {})
    assert r["supported"] is True
    assert r["verified"] is True
    assert r["chars"] == len("verified copy")


def test_write_readback_mismatch_reports_unverified(monkeypatch):
    monkeypatch.setattr(C, "_DETECT", lambda: "wayland")
    monkeypatch.setattr(C, "_WHICH", lambda name: "/usr/bin/wl-copy")

    def fake_run(cmd, timeout, input_bytes=b""):
        return 0, b"stale content", b""        # paste returns the OLD text

    monkeypatch.setattr(C, "_run_bounded", fake_run)
    r = C.clipboard_write({"text": "new text"}, {})
    assert r["supported"] is True
    assert r["verified"] is False             # reported, never faked


def test_write_backend_failure_honest(monkeypatch):
    monkeypatch.setattr(C, "_DETECT", lambda: "wayland")
    monkeypatch.setattr(C, "_WHICH", lambda name: "/usr/bin/wl-copy")
    monkeypatch.setattr(C, "_run_bounded",
                        lambda cmd, timeout, input_bytes=b"":
                        (1, b"", b"compositor refused"))
    r = C.clipboard_write({"text": "some content that is secret"}, {})
    assert r["supported"] is True
    assert r["verified"] is False
    # Content is never echoed back through failure paths.
    assert "secret" not in str(r)


def test_write_oversized_refused_not_truncated(monkeypatch):
    monkeypatch.setattr(C, "_DETECT", lambda: "wayland")
    monkeypatch.setattr(C, "_WHICH", lambda name: "/usr/bin/wl-copy")

    def refuse(_cmd, _t, input_bytes=b""):
        raise AssertionError("backend must never be called")

    monkeypatch.setattr(C, "_run_bounded", refuse)
    with pytest.raises(C.ClipboardError, match="cap"):
        C.clipboard_write({"text": "y" * (C.MAX_CLIPBOARD_TEXT + 1)}, {})


def test_write_rejects_nonstring_and_empty():
    with pytest.raises(C.ClipboardError):
        C.clipboard_write({"text": 123}, {})
    with pytest.raises(C.ClipboardError):
        C.clipboard_write({"text": ""}, {})


# ---------------- schema / envelope ----------------

def test_tool_contracts_shape():
    names = {d.name for d, _ in C.CLIPBOARD_TOOLS}
    assert names == {"desktop.clipboard.read", "desktop.clipboard.write"}
    for d, _ in C.CLIPBOARD_TOOLS:
        assert d.supported_devices == ["linux"]
        assert d.risk.value == "confirm"
        assert d.permission.value == "restricted"
    write_def = [d for d, _ in C.CLIPBOARD_TOOLS
                 if d.name == "desktop.clipboard.write"][0]
    # Envelope mirrors the handler cap: 100k chars + JSON overhead,
    # never authority beyond the handler's own refusal.
    assert write_def.max_input_bytes == 131072
