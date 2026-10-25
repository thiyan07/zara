"""Stage 21 — display-config tests.

Every apply runs against MOCKED command runners: no real desktop
layout is ever changed by these tests. The mutter fixture is a REAL
GetCurrentState capture from this laptop (built-in eDP-1 only, no
external display attached at capture time)."""
from __future__ import annotations

from pathlib import Path

import pytest

from device.linux import display_linux as dl

FIXTURE = Path(__file__).parent / "fixtures" / \
    "mutter_get_current_state.json"

_XR_EXT = """Screen 0: minimum 320 x 200, current 3840 x 1080
eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 344mm x 194mm
   1920x1080     144.00*+
HDMI-A-0 connected 1920x1080+1920+0 (normal left inverted right x axis y axis) 600mm x 340mm
   1920x1080     60.00*+
"""
_XR_DUP = """Screen 0: minimum 320 x 200, current 1920 x 1080
eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 344mm x 194mm
   1920x1080     144.00*+
HDMI-A-0 connected 1920x1080+0+0 (normal left inverted right x axis y axis) 600mm x 340mm
   1920x1080     60.00*+
"""
_XR_ONE = """Screen 0: minimum 320 x 200, current 1920 x 1080
eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 344mm x 194mm
   1920x1080     144.00*+
HDMI-A-0 disconnected (normal left inverted right x axis y axis)
"""

_MUT = FIXTURE.read_text()


@pytest.fixture
def script(monkeypatch):
    """Scripted _RUN: positional outputs; the LAST output repeats.
    Returns (install_fn, calls_list)."""
    calls: list[list] = []

    def install(*outputs):
        queue: list = list(outputs) or [None]

        def fake(argv, timeout=8.0):
            calls.append(list(argv))
            out = queue.pop(0) if len(queue) > 1 else queue[0]
            return out
        monkeypatch.setattr(dl, "_RUN", fake)
        return calls

    return install, calls


# ------------------------------------------------------------- describe


def test_describe_xrandr_extended(script):
    install, _ = script
    install(_XR_EXT)
    desc = dl.describe_config()
    assert desc["available"] is True
    assert desc["backend"] == "xrandr"
    assert desc["layout_inferred"] == "extended"
    assert {(m["name"], m["x"]) for m in desc["monitors"]} == {
        ("eDP-1", 0), ("HDMI-A-0", 1920)}


def test_describe_xrandr_duplicated_and_single(script):
    install, _ = script
    install(_XR_DUP)
    assert dl.describe_config()["layout_inferred"] == "duplicated"
    install(_XR_ONE)
    assert dl.describe_config()["layout_inferred"] == "single"


def test_describe_mutter_fixture_real_capture(script):
    install, _ = script
    install(_MUT)          # single real capture: mutter wins the probe
    desc = dl.describe_config()
    assert desc["backend"] == "mutter"
    assert desc["layout_inferred"] == "single"
    mon = desc["monitors"][0]
    assert mon["name"] == "eDP-1"
    assert mon["w"] == 1920 and mon["h"] == 1080
    assert mon["primary"] is True


def test_describe_headless_is_honest(script):
    install, _ = script
    install(None, None)
    desc = dl.describe_config()
    assert desc["available"] is False
    assert "no reachable" in desc["detail"] or "answered" in desc["detail"]


# -------------------------------------------------------------- preview


def test_preview_builds_exact_commands_runs_nothing(script):
    install, calls = script
    install(_XR_EXT)
    p = dl.preview_config("extend_right")
    assert p["no_changes_run"] is True
    assert p["command"] == ["xrandr", "--output", "eDP-1", "--auto",
                            "--primary", "--output", "HDMI-A-0",
                            "--auto", "--right-of", "eDP-1"]
    # only read-only queries ran, never a mode-changing call
    assert not any("--right-of" in a or "--same-as" in a
                   for a in calls)


def test_preview_refuses_duplicate_without_second_display(script):
    install, _ = script
    install(_XR_ONE)
    p = dl.preview_config("duplicate")
    assert p["ok"] is False
    assert "second active display" in p["error"]
    assert p.get("limitation") == "hardware/connection state"


def test_preview_invalid_mode(script):
    install, _ = script
    install(_XR_EXT)
    assert dl.preview_config("teleport")["ok"] is False


# ---------------------------------------------------------------- apply


def test_apply_requires_explicit_confirm(script):
    install, _ = script
    install(_MUT)
    out = dl.apply_config("duplicate")
    assert out["ok"] is False
    assert "confirm=true" in out["error"]
    install, _ = script
    install(_MUT)
    out = dl.apply_config("television", confirm=True)
    assert out["ok"] is False and "mode must be" in out["error"]


def _role_runner(monkeypatch, mutter=None, query=None, apply_out=None):
    """Keyed by role, not call order: 'busctl' probe, query (xrandr
    --query), apply_out (any mode-changing xrandr call)."""
    calls: list[list] = []

    def fake(argv, timeout=8.0):
        calls.append(list(argv))
        if argv[0] == "busctl":
            return mutter
        if "--right-of" in argv or "--same-as" in argv or \
                ("--output" in argv and "--off" in argv):
            return apply_out
        return query
    monkeypatch.setattr(dl, "_RUN", fake)
    return calls


def test_apply_extend_verified_by_geometry_re_read(monkeypatch):
    calls = _role_runner(monkeypatch, mutter=None,
                         query=_XR_EXT, apply_out="")
    out = dl.apply_config("extend_right", confirm=True)
    assert out["verified"] is True and out["ok"] is True
    assert out["after"] == "extended"
    write = [a for a in calls if "--right-of" in a]
    assert write == [["xrandr", "--output", "eDP-1", "--auto",
                      "--primary", "--output", "HDMI-A-0", "--auto",
                      "--right-of", "eDP-1"]]


def test_apply_verify_failure_never_claims_success(monkeypatch):
    # pre/before reads say DUP, but the verification re-read reads
    # SINGLE: the requested layout never materialized and the result
    # honestly reports failure
    import itertools
    reads = itertools.chain([_XR_DUP, _XR_DUP], itertools.repeat(_XR_ONE))

    def fake(argv, timeout=8.0):
        if argv[0] == "busctl":
            return None
        if "--right-of" in argv or "--same-as" in argv:
            return ""                      # apply "succeeds"
        return next(reads)
    monkeypatch.setattr(dl, "_RUN", fake)
    out = dl.apply_config("duplicate", confirm=True)
    assert out["verified"] is False and out["ok"] is False
    assert "no success claim" in (out.get("detail") or "")
    assert out["before"] == "duplicated"


def test_apply_backend_refusal_reports_honest(monkeypatch):
    # the mode-changing call returns None (backend refused) while the
    # read-only probes keep answering — refusal wins, no fake success
    calls = _role_runner(monkeypatch, mutter=None, query=_XR_DUP,
                         apply_out=None)
    out = dl.apply_config("duplicate", confirm=True)
    assert out["ok"] is False and out["verified"] is False
    assert "layout unchanged" in out["error"]
    assert dl.describe_config()["layout_inferred"] == "duplicated"


def test_apply_needs_second_display_is_precise(script):
    install, _ = script
    install(_XR_ONE)
    out = dl.apply_config("extend_right", confirm=True)
    assert out["ok"] is False
    assert "second active display" in out["error"]
    assert out.get("verified") is False


def test_mutter_apply_refused_until_validated(script):
    install, _ = script
    install(_MUT, _MUT)     # mutter answers both pre and verify
    out = dl.apply_config("extend_right", confirm=True)
    # honest: the mutter gdbus path is designed but NOT validated on a
    # real external display, so it refuses rather than guessing
    assert out["ok"] is False
    assert "validated on a real external display" in out["error"]
    # second display absence also refuses precisely on mutter
