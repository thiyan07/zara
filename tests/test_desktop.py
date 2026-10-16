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
