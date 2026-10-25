"""Stage 31-P2 — laptop system control (P2a volume/mute/brightness,
P2b MPRIS playback). Fixture tests through the single _run seam.

Claimed discipline, tested here:
1. Every SET verifies by re-read — the verdict is honest; exit status
   alone is NEVER claimed as "applied".
2. Without confirm=true nothing changes (refusal is structural).
3. Absent interfaces (no wpctl / no backlight / no MPRIS players) are
   reported honestly, never faked.
4. Plain read-only reads stay SAFE/PUBLIC; sets stay CONFIRM and park
   without a human grant (prompts are a currency: these change
   user-perceptible state, so spending a prompt is CORRECT).
"""
from __future__ import annotations

import os
from unittest.mock import patch

import device.linux.syscontrol_linux as sc
from core.policy import PolicyEngine


def canned(stdout_by_cmd):
    """Fake _run answering canned stdout for the matching argv prefix."""
    def fake(argv, timeout=6.0):
        for key, out in stdout_by_cmd.items():
            parts = key.split()
            if argv[:len(parts)] == parts:
                return (0, out)
        return (0, "")
    return fake


# ---------------- P2a: volume ------------------------------------------

def test_volume_set_applies_and_verifies_by_reread():
    """Re-read returns the target → applied (before + after re-reads
    both happened)."""
    rereads = iter(["Volume: 0.50", "Volume: 0.70"])
    sets = []

    def fake(argv, timeout=6.0):
        if argv[:2] == ["wpctl", "get-volume"]:
            return (0, next(rereads))
        if argv[:2] == ["wpctl", "set-volume"]:
            sets.append(argv[3])
            return (0, "")
        return (0, "")
    with patch.object(sc, "_run", fake):
        out = sc.volume_set({"level": 70, "confirm": True}, None)
    assert out["ok"] is True
    assert out["verdict"] == "applied"
    assert out["before_volume_pct"] == 50.0
    assert out["target_volume_pct"] == 70.0
    assert out["after_volume_pct"] == 70.0
    assert sets == ["0.700"]


def test_volume_not_applied_when_reread_disagrees():
    """The honest path: re-read shows the OLD value → not-applied even
    though the set command exited 0 (exit status is never proof)."""
    def fake(argv, timeout=6.0):
        if argv[:2] == ["wpctl", "get-volume"]:
            return (0, "Volume: 0.50")  # never changes in this fake
        if argv[:2] == ["wpctl", "set-volume"]:
            return (0, "")  # "succeeded"
        return (0, "")
    with patch.object(sc, "_run", fake):
        out = sc.volume_set({"level": 90, "confirm": True}, None)
    assert out["ok"] is False
    assert out["verdict"] == "not-applied"
    assert out["nothing_changed"] is True


def test_volume_up_delta_math():
    state = {"vol": 0.63}
    sent = {}

    def fake(argv, timeout=6.0):
        if argv[:2] == ["wpctl", "get-volume"]:
            return (0, f"Volume: {state['vol']}")
        if argv[:2] == ["wpctl", "set-volume"]:
            sent["v"] = float(argv[3])
            state["vol"] = sent["v"]
            return (0, "")
        return (0, "")
    with patch.object(sc, "_run", fake):
        out = sc.volume_set({"delta": 10, "confirm": True}, None)
    # +10 points from 0.63 → wpctl asked to set 0.73; re-read honored it
    assert sent["v"] == 0.73
    assert out["verdict"] == "applied"
    assert out["target_volume_pct"] == 73.0


def test_volume_set_refuses_without_confirm():
    with patch.object(sc, "_run",
                      canned({"wpctl get-volume": "Volume: 0.63"})):
        out = sc.volume_set({"level": 90, "confirm": False}, None)
    assert out["ok"] is False and out["nothing_changed"] is True


def test_volume_unavailable_honest():
    with patch.object(sc, "_run", lambda a, timeout=6.0: (127, "no wpctl")):
        out = sc.volume_set({"level": 50, "confirm": True}, None)
    assert out["available"] is False and out["ok"] is False


# ---------------- P2a: mute --------------------------------------------

def test_mute_on_verifies_by_reread():
    dest = {"out": "Volume: 0.63"}

    def fake(argv, timeout=6.0):
        if argv[:2] == ["wpctl", "get-volume"]:
            return (0, dest["out"])
        if argv[:2] == ["wpctl", "set-mute"]:
            dest["out"] = "Volume: 0.63 [MUTED]"
            return (0, "")
        return (0, "")
    with patch.object(sc, "_run", fake):
        out = sc.mute_set({"mode": "on", "confirm": True}, None)
    assert out["ok"] is True and out["verdict"] == "applied"
    assert out["before_muted"] is False and out["after_muted"] is True


def test_mute_unmute_reports_plain():
    dest = {"out": "Volume: 0.63 [MUTED]"}

    def fake(argv, timeout=6.0):
        if argv[:2] == ["wpctl", "get-volume"]:
            return (0, dest["out"])
        if argv[:2] == ["wpctl", "set-mute"]:
            dest["out"] = "Volume: 0.63"
            return (0, "")
        return (0, "")
    with patch.object(sc, "_run", fake):
        out = sc.mute_set({"mode": "off", "confirm": True}, None)
    assert out["ok"] is True and out["after_muted"] is False


def test_mute_toggle_verifies_flip():
    dest = {"out": "Volume: 0.63"}

    def fake(argv, timeout=6.0):
        if argv[:2] == ["wpctl", "get-volume"]:
            return (0, dest["out"])
        if argv[:2] == ["wpctl", "set-mute"] \
                and argv[3] == "toggle":
            dest["out"] = "Volume: 0.63 [MUTED]"
            return (0, "")
        return (0, "")
    with patch.object(sc, "_run", fake):
        out = sc.mute_set({"mode": "toggle", "confirm": True}, None)
    assert out["ok"] is True  # flag flipped: False → True


# ---------------- P2a: brightness ---------------------------------------

def _fake_backlight(monkeypatch, dev):
    monkeypatch.setattr(sc, "glob", type(
        "g", (), {"glob": staticmethod(
            lambda p: [str(dev / "brightness")])}))


def test_brightness_applies_and_verifies(tmp_path, monkeypatch):
    dev = tmp_path / "bl"
    dev.mkdir()
    (dev / "brightness").write_text("10240")
    (dev / "max_brightness").write_text("61680")
    _fake_backlight(monkeypatch, dev)
    out = sc.brightness_set({"pct": 50, "confirm": True}, None)
    assert out["ok"] is True and out["verdict"] == "applied"
    assert out["before_pct"] == round(10240 * 100 / 61680, 1)
    assert (dev / "brightness").read_text() == str(int(61680 * 0.5))


def test_brightness_permission_refused_honest(tmp_path, monkeypatch):
    """A real EACCES through real file permissions (0444 = owner cannot
    write) — no pathlib patching needed."""
    dev = tmp_path / "bl"
    dev.mkdir()
    b = dev / "brightness"
    b.write_text("10240")
    (dev / "max_brightness").write_text("61680")
    os.chmod(b, 0o444)
    _fake_backlight(monkeypatch, dev)
    out = sc.brightness_set({"pct": 75, "confirm": True}, None)
    assert out["ok"] is False
    assert out["verdict"] == "permission-refused"
    assert "not writable" in out["detail"]


def test_brightness_no_device_honest(monkeypatch):
    monkeypatch.setattr(sc, "glob", type(
        "g", (), {"glob": staticmethod(lambda p: [])}))
    out = sc.brightness_set({"pct": 50, "confirm": True}, None)
    assert out["available"] is False and out["ok"] is False


# ---------------- P2b: MPRIS playback -----------------------------------

def test_now_playing_honest_empty():
    with patch.object(sc, "_run", lambda a, timeout=6.0: (0, "")):
        out = sc.now_playing({}, None)
    assert out["count"] == 0 and out["players"] == []


def test_playback_pause_verifies_status_change():
    state = {"status": "Playing", "gets": 0}

    def fake(argv, timeout=6.0):
        if argv[0] == "busctl" and "list" in argv:
            return (0, ":1.9 1234 org.mpris.MediaPlayer2.mpv ? thiyan")
        if argv[-1] == "PlayPause":
            state["status"] = "Paused"
            return (0, "")
        if "PlaybackStatus" in argv:
            state["gets"] += 1  # before-observation, then re-read
            return (0, f's "{state["status"]}"')
        return (0, "")
    with patch.object(sc, "_run", fake):
        out = sc.playback_control({"action": "play_pause", "confirm": True},
                                  None)
    assert out["ok"] is True
    assert state["gets"] == 2  # before + verified re-read
    r = out["players"][0]
    assert r["before_status"] == "Playing"
    assert r["after_status"] == "Paused"
    assert r["verdict"] == "applied"


def test_playback_next_declared_sent_honestly():
    def fake(argv, timeout=6.0):
        if "list" in argv:
            return (0, ":1.9 1234 org.mpris.MediaPlayer2.mpv ? thiyan")
        if argv[-1] == "Next":
            return (0, "")
        if "PlaybackStatus" in argv:
            return (0, 's "Playing"')  # unchanged — skips can't move it
        return (0, "")
    with patch.object(sc, "_run", fake):
        out = sc.playback_control({"action": "next", "confirm": True}, None)
    r = out["players"][0]
    assert r["verdict"] == "sent"  # honest: NO 'applied' overclaim
    assert "'sent'" in out["verification"]


def test_playback_no_player_honest():
    with patch.object(sc, "_run", lambda a, timeout=6.0: (0, "")):
        out = sc.playback_control({"action": "play_pause", "confirm": True},
                                  None)
    assert out["available"] is False and out["ok"] is False


# ---------------- wiring, policy, parity -------------------------------

def test_registered_on_both_sides():
    from core.device_tools import DEVICE_TOOL_DEFS as core_defs
    from device.linux.tools_linux import LINUX_TOOLS
    core = {d.name for d in core_defs}
    dev = {d.name for d, _ in LINUX_TOOLS}
    names = ("audio.volume.set", "audio.mute.set", "display.brightness",
             "display.brightness.set", "media.now_playing",
             "media.playback")
    for name in names:
        assert name in core, name
        assert name in dev, name


def test_policy_matrix_sets_park_reads_allow():
    eng = PolicyEngine()
    for cap, risk in (("audio.volume.set", "confirm"),
                      ("audio.mute.set", "confirm"),
                      ("display.brightness.set", "confirm"),
                      ("media.playback", "confirm"),
                      ("display.brightness", "safe"),
                      ("media.now_playing", "safe")):
        d = eng.decide("u", cap, risk=risk)
        if risk == "confirm":
            assert d.requires_approval is True, cap
        else:
            assert d.allow is True, cap


def test_intent_table_routes_phrases():
    import core.intent as it
    src = open(it.__file__).read()
    for phrase, tool in (("louder", "audio.volume.set"),
                         ("volume down", "audio.volume.set"),
                         ("mute the laptop", "audio.mute.set"),
                         ("dim the screen", "display.brightness.set"),
                         ("pause the music", "media.playback"),
                         ("what's playing", "media.now_playing")):
        assert f'("{phrase}", "{tool}"' in src


def test_live_laptop_nondestructive_roundtrip():
    """Real machine, NON-destructive: set volume to its CURRENT value
    (no audible change) and expect an honest 'applied' verdict from
    the re-read."""
    from device.linux.syscontrol_linux import volume_set as vs
    before = sc._read_volume()
    if not before["available"]:
        import pytest
        pytest.skip("no wpctl on this machine")
    out = vs({"level": round(before["volume_pct"], 1), "confirm": True},
             None)
    assert out["ok"] is True
    assert out["verdict"] == "applied"
    assert abs((out["after_volume_pct"] or 0) - before["volume_pct"]) <= 0.1
