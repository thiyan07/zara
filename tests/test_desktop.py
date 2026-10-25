"""Linux desktop reads: session detect, active window, bounded screenshot.

Backends and environment are fully mocked except where the real scratch
dir mechanics are the subject (redirected to tmp_path). No real screen
capture ever runs here.
"""
import os

import device.linux.tools_linux as LT
from core.device_tools import DEVICE_TOOL_DEFS
from core.intent import LocalIntentParser, ParserContext

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


def test_core_desktop_defs_linux_only():
    defs = {d.name: d for d in DEVICE_TOOL_DEFS}
    assert defs["desktop.session"].supported_devices == ["linux"]
    assert defs["desktop.window.active"].supported_devices == ["linux"]
    assert defs["desktop.screenshot"].supported_devices == ["linux"]
    assert defs["desktop.screenshot"].risk.value == "confirm"


def test_core_linux_parity_desktop():
    linux = {d.name: d for d, _ in LT.LINUX_TOOLS}
    core = {d.name: d for d in DEVICE_TOOL_DEFS}
    for name in ("desktop.session", "desktop.window.active",
                 "desktop.screenshot"):
        assert name in linux, name
        assert set(core[name].required_capabilities) == \
            set(linux[name].required_capabilities)
        assert core[name].risk == linux[name].risk
        assert core[name].input_schema == linux[name].input_schema


def _clean_env(monkeypatch):
    for k in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(k, raising=False)


def test_session_detect(monkeypatch):
    _clean_env(monkeypatch)
    assert LT.desktop_session({}, {})["session"] == "headless"
    monkeypatch.setenv("DISPLAY", ":0")
    assert LT.desktop_session({}, {})["session"] == "x11"
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert LT.desktop_session({}, {})["session"] == "wayland"
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert LT.desktop_session({}, {})["session"] == "x11"


def test_window_active_wayland_honest(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    out = LT.desktop_window_active({}, {})
    assert out["supported"] is False


def test_window_active_x11(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")

    class R:
        returncode = 0
        stdout = "Terminal - zsh\n"
        stderr = ""
    monkeypatch.setattr(LT, "_run_quiet", lambda argv, timeout=10: R())
    out = LT.desktop_window_active({}, {})
    assert out["title"] == "Terminal - zsh"

    monkeypatch.setattr(LT, "_run_quiet", lambda argv, timeout=10: None)
    out = LT.desktop_window_active({}, {})
    assert out["supported"] is False


def test_screenshot_no_backend(monkeypatch):
    monkeypatch.setattr(LT, "_have", lambda b: False)
    out = LT.desktop_screenshot({}, {})
    assert out["supported"] is False


def test_screenshot_success_to_scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(LT, "HOME", str(tmp_path))
    monkeypatch.setattr(LT, "_have",
                        lambda b: b == "gnome-screenshot")

    def fake(argv, timeout=10):
        assert argv[0] == "gnome-screenshot"
        with open(argv[-1], "wb") as f:
            f.write(PNG)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(LT, "_run_quiet", fake)
    out = LT.desktop_screenshot({}, {})
    assert out["supported"] is True
    assert out["bytes"] == len(PNG)
    assert out["backend"] == "gnome-screenshot"
    assert os.path.isfile(out["path"])
    with open(out["path"], "rb") as f:
        assert f.read() == PNG


def test_screenshot_refuses_non_png(monkeypatch, tmp_path):
    monkeypatch.setattr(LT, "HOME", str(tmp_path))
    monkeypatch.setattr(LT, "_have", lambda b: b == "grim")

    def fake(argv, timeout=10):
        with open(argv[-1], "wb") as f:
            f.write(b"not an image")

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(LT, "_run_quiet", fake)
    out = LT.desktop_screenshot({}, {})
    assert "error" in out and "PNG" in out["error"]
    assert "path" not in out


def test_intent_desktop_aliases():
    caps = {"desktop.screenshot", "desktop.window.active"}
    schemas = {c: {"properties": {}} for c in caps}
    ctx = ParserContext(devices=[], default_device="",
                        known_capabilities=caps,
                        capability_schemas=schemas)
    lp = LocalIntentParser()
    p = lp.parse("take a screenshot", ctx)
    assert p.capability == "desktop.screenshot"
    assert not p.needs_clarification
    p = lp.parse("what window is active", ctx)
    assert p.capability == "desktop.window.active"
    assert not p.needs_clarification


def test_window_list_defs_linux_only():
    defs = {d.name: d for d in DEVICE_TOOL_DEFS}
    assert defs["desktop.window.list"].supported_devices == ["linux"]
    assert defs["desktop.window.action"].supported_devices == ["linux"]
    assert defs["desktop.window.action"].risk.value == "high_risk"
    linux = {d.name: d for d, _ in LT.LINUX_TOOLS}
    for name in ("desktop.window.list", "desktop.window.action"):
        assert name in linux, name
        assert set(defs[name].required_capabilities) == \
            set(linux[name].required_capabilities)
        assert defs[name].risk == linux[name].risk
        assert defs[name].input_schema == linux[name].input_schema


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _x11(monkeypatch):
    for k in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("DISPLAY", ":0")


def test_window_list_wayland_honest(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    out = LT.desktop_window_list({}, {})
    assert out["supported"] is False


def test_window_list_parses_wmctrl(monkeypatch):
    _x11(monkeypatch)
    monkeypatch.setattr(
        LT, "_run_quiet",
        lambda argv, timeout=10: _R(0, "0x01200003  0 host Terminal\n"
                                    "0x01200004  1 host Firefox\n"))
    out = LT.desktop_window_list({}, {})
    assert out["supported"] is True
    assert [w["window_id"] for w in out["windows"]] == \
        ["0x01200003", "0x01200004"]
    assert out["windows"][0]["title"] == "Terminal"


def test_window_action_wayland_refuses(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    out = LT.desktop_window_action(
        {"window_id": "0x01200003", "action": "focus"}, {})
    assert out["supported"] is False and out["applied"] is False


def test_window_action_bad_id_and_action(monkeypatch):
    _x11(monkeypatch)
    out = LT.desktop_window_action(
        {"window_id": "nope", "action": "focus"}, {})
    assert out["applied"] is False and "window_id" in out["error"]
    out = LT.desktop_window_action(
        {"window_id": "0x01200003", "action": "explode"}, {})
    assert out["applied"] is False and "action" in out["error"]


def test_window_action_focus_verified(monkeypatch):
    _x11(monkeypatch)
    calls = []

    def _fake(argv, timeout=10):
        calls.append(argv)
        if argv[:2] == ["wmctrl", "-l"]:
            return _R(0, "0x01200003  0 host Terminal\n")
        if argv[:2] == ["xdotool", "windowactivate"]:
            return _R(0, "")
        if argv[:2] == ["xdotool", "getactivewindow"]:
            return _R(0, "18874371\n")  # == 0x1200003
        raise AssertionError(argv)

    monkeypatch.setattr(LT, "_run_quiet", _fake)
    out = LT.desktop_window_action(
        {"window_id": "0x01200003", "action": "focus"}, {})
    assert out["applied"] is True, out
    assert "focused" in out["verify_state"]


def test_window_action_close_verified_absent(monkeypatch):
    _x11(monkeypatch)
    state = {"listed": True}

    def _fake(argv, timeout=10):
        if argv[:2] == ["wmctrl", "-l"]:
            return _R(0, "0x01200003  0 host T\n" if state["listed"]
                      else "")
        if argv[:3] == ["wmctrl", "-i", "-c"]:
            state["listed"] = False
            return _R(0, "")
        raise AssertionError(argv)

    monkeypatch.setattr(LT, "_run_quiet", _fake)
    out = LT.desktop_window_action(
        {"window_id": "0x01200003", "action": "close"}, {})
    assert out["applied"] is True, out
    assert "closed" in out["verify_state"]


def test_window_action_close_unverified_when_still_listed(monkeypatch):
    _x11(monkeypatch)

    def _fake(argv, timeout=10):
        if argv[:2] == ["wmctrl", "-l"]:
            return _R(0, "0x01200003  0 host T\n")
        return _R(0, "")

    monkeypatch.setattr(LT, "_run_quiet", _fake)
    out = LT.desktop_window_action(
        {"window_id": "0x01200003", "action": "close"}, {})
    assert out["applied"] is False
    assert "unverified" in out["error"]


def test_window_action_missing_target_refuses(monkeypatch):
    _x11(monkeypatch)
    monkeypatch.setattr(
        LT, "_run_quiet",
        lambda argv, timeout=10: _R(0, "0x09999999  0 host Other\n")
        if argv[:2] == ["wmctrl", "-l"] else _R(0, ""))
    out = LT.desktop_window_action(
        {"window_id": "0x01200003", "action": "minimize"}, {})
    assert out["applied"] is False
    assert "not present" in out["error"]


def test_window_action_minimize_present(monkeypatch):
    _x11(monkeypatch)

    def _fake(argv, timeout=10):
        if argv[:2] == ["wmctrl", "-l"]:
            return _R(0, "0x01200003  0 host T\n")
        if argv[:2] == ["xdotool", "windowminimize"]:
            return _R(0, "")
        raise AssertionError(argv)

    monkeypatch.setattr(LT, "_run_quiet", _fake)
    out = LT.desktop_window_action(
        {"window_id": "0x01200003", "action": "minimize"}, {})
    assert out["applied"] is True, out


def test_intent_window_aliases_and_params():
    from core.intent import ParserContext
    from tests.test_intent import phone_ctx
    ctx = phone_ctx()
    ctx = ParserContext(
        devices=list(ctx.devices),
        known_capabilities=set(ctx.known_capabilities)
        | {"desktop.window.list", "desktop.window.action"},
        capability_schemas=dict(ctx.capability_schemas))
    lp = LocalIntentParser()
    p = lp.parse("list windows", ctx)
    assert p.capability == "desktop.window.list", p
    p = lp.parse("minimize window 0x01200003", ctx)
    assert p.capability == "desktop.window.action", p
    assert p.parameters["action"] == "minimize"
    assert p.parameters["window_id"] == "0x01200003"
    # No id -> honest clarification, never a guessed target.
    q = lp.parse("close window", ctx)
    assert q.needs_clarification and q.capability == ""
