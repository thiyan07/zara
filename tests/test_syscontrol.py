"""Phase-2 syscontrol set tools: contracts, validation, verify logic.

Every subprocess/state interaction is mocked — these tests must never touch
real radios. Live behavior is validated on hardware only.
"""
import pytest

import device.linux.tools_linux as LT
from core.device_tools import DEVICE_TOOL_DEFS
from core.intent import LocalIntentParser, ParserContext
from core.models import PermissionLevel, RiskLevel

SET_TOOLS = ["system.wifi.set", "system.bluetooth.set",
             "system.hotspot.set", "system.mobile_data.set",
             "system.airplane_mode.set"]

SET_SCHEMA = {"type": "object", "required": ["enabled"],
              "additionalProperties": False,
              "properties": {"enabled": {"type": "boolean"}}}


def test_core_set_defs_gated_and_typed():
    defs = {d.name: d for d in DEVICE_TOOL_DEFS}
    for name in SET_TOOLS:
        d = defs[name]
        assert d.permission == PermissionLevel.CONFIRM, name
        assert d.risk == RiskLevel.HIGH_RISK, name
        assert d.input_schema == SET_SCHEMA, name
        assert d.required_capabilities == [name], name


def test_core_linux_parity_for_set_tools():
    linux = {d.name: d for d, _ in LT.LINUX_TOOLS}
    core = {d.name: d for d in DEVICE_TOOL_DEFS}
    for name in SET_TOOLS:
        assert name in linux, name
        assert set(core[name].required_capabilities) == \
            set(linux[name].required_capabilities)
        assert core[name].risk == linux[name].risk
        assert core[name].input_schema == linux[name].input_schema


def test_need_enabled_strict():
    assert LT._need_enabled({"enabled": True}) is True
    assert LT._need_enabled({"enabled": False}) is False
    for bad in ({}, {"enabled": 1}, {"enabled": "on"},
                {"enabled": None}, {"enabled": [True]}):
        with pytest.raises(ValueError):
            LT._need_enabled(bad)


def _ok_rc(stdout=""):
    class R:
        returncode = 0
        stderr = ""
    r = R()
    r.stdout = stdout
    return r


def test_wifi_set_verified(monkeypatch):
    calls = []
    monkeypatch.setattr(
        LT, "_run_quiet",
        lambda argv, timeout=10: calls.append(argv) or _ok_rc())
    monkeypatch.setattr(LT, "system_wifi_state",
                        lambda i, c: {"state": "enabled"})
    out = LT.system_wifi_set({"enabled": True}, {})
    assert out["applied"] is True and out["state"] == "enabled"
    assert calls[0][:3] == ["nmcli", "radio", "wifi"] and calls[0][3] == "on"
    assert out["verify"] == "system.wifi.state"


def test_wifi_set_mismatch_not_applied(monkeypatch):
    monkeypatch.setattr(LT, "_run_quiet", lambda argv, timeout=10: _ok_rc())
    monkeypatch.setattr(LT, "system_wifi_state",
                        lambda i, c: {"state": "enabled"})
    out = LT.system_wifi_set({"enabled": False}, {})
    assert out["applied"] is False  # asked off, still on: honest


def test_wifi_set_refusal_and_missing(monkeypatch):
    class R:
        returncode = 10
        stdout = ""
        stderr = "Error: not authorized"
    monkeypatch.setattr(LT, "_run_quiet", lambda argv, timeout=10: R())
    out = LT.system_wifi_set({"enabled": True}, {})
    assert out["applied"] is False and "authorized" in out["error"]
    monkeypatch.setattr(LT, "_run_quiet", lambda argv, timeout=10: None)
    out = LT.system_wifi_set({"enabled": True}, {})
    assert out["supported"] is False


def test_bluetooth_set_verified(monkeypatch):
    seen = []
    monkeypatch.setattr(
        LT, "_run_quiet",
        lambda argv, timeout=10: seen.append(argv) or _ok_rc())
    monkeypatch.setattr(LT, "system_bluetooth_state",
                        lambda i, c: {"state": "disabled"})
    out = LT.system_bluetooth_set({"enabled": False}, {})
    assert out["applied"] is True
    assert seen[0] == ["bluetoothctl", "power", "off"]


def test_hotspot_set_refuses_without_profile(monkeypatch):
    monkeypatch.setattr(
        LT, "_run_quiet",
        lambda argv, timeout=10: _ok_rc("HomeWifi:802-11-wireless\n"))
    out = LT.system_hotspot_set({"enabled": True}, {})
    assert out["applied"] is False
    assert "profile" in out["error"]


def test_hotspot_set_uses_profile_and_verifies(monkeypatch):
    seen = []

    def fake(argv, timeout=10):
        seen.append(argv)
        if argv[:3] == ["nmcli", "-t", "-f"]:
            return _ok_rc("MyHotspot:802-11-wireless\n")
        return _ok_rc()

    monkeypatch.setattr(LT, "_run_quiet", fake)
    monkeypatch.setattr(LT, "system_hotspot_state",
                        lambda i, c: {"state": "connected"})
    out = LT.system_hotspot_set({"enabled": True}, {})
    assert out["applied"] is True and out["profile"] == "MyHotspot"
    assert ["nmcli", "connection", "up", "MyHotspot"] in seen


def test_mobile_data_set_blocked_without_modem(monkeypatch):
    monkeypatch.setattr(LT, "_run_quiet", lambda argv, timeout=10: None)
    out = LT.system_mobile_data_set({"enabled": True}, {})
    assert out["supported"] is False
    assert out["deployment"] == "blocked"
    monkeypatch.setattr(LT, "_run_quiet",
                        lambda argv, timeout=10: _ok_rc("no modems found"))
    out = LT.system_mobile_data_set({"enabled": True}, {})
    assert out["applied"] is False
    assert out["deployment"] == "blocked"


def test_airplane_set_verified_and_errors(monkeypatch):
    monkeypatch.setattr(LT, "_run_quiet",
                        lambda argv, timeout=10: _ok_rc())
    monkeypatch.setattr(LT, "_rfkill_blocked", lambda: True)
    out = LT.system_airplane_mode_set({"enabled": True}, {})
    assert out["applied"] is True and out["state"] == "enabled"

    class R:
        returncode = 1
        stdout = ""
        stderr = "Operation not permitted"
    monkeypatch.setattr(LT, "_run_quiet", lambda argv, timeout=10: R())
    out = LT.system_airplane_mode_set({"enabled": True}, {})
    assert out["applied"] is False and "permitted" in out["error"]

    monkeypatch.setattr(LT, "_run_quiet", lambda argv, timeout=10: None)
    out = LT.system_airplane_mode_set({"enabled": True}, {})
    assert out["supported"] is False


# ---------- intent ----------

def _set_ctx():
    caps = set(SET_TOOLS) | {"system.mobile_data.state"}
    schemas = {c: {"properties": {"enabled": {"type": "boolean"}},
                   "required": ["enabled"]} for c in SET_TOOLS}
    schemas["system.mobile_data.state"] = {"properties": {}}
    return ParserContext(devices=[], default_device="",
                         known_capabilities=caps,
                         capability_schemas=schemas)


LP = LocalIntentParser()


def test_intent_set_aliases_with_direction():
    ctx = _set_ctx()
    cases = {
        "turn on wifi": ("system.wifi.set", True),
        "turn off wifi": ("system.wifi.set", False),
        "enable bluetooth": ("system.bluetooth.set", True),
        "disable bluetooth": ("system.bluetooth.set", False),
        "turn on hotspot": ("system.hotspot.set", True),
        "turn off mobile data": ("system.mobile_data.set", False),
        "enable mobile data": ("system.mobile_data.set", True),
        "turn on airplane mode": ("system.airplane_mode.set", True),
        "turn off airplane mode": ("system.airplane_mode.set", False),
        "flight mode on": ("system.airplane_mode.set", True),
        "flight mode off": ("system.airplane_mode.set", False),
    }
    for u, (cap, want) in cases.items():
        p = LP.parse(u, ctx)
        assert p.capability == cap, u
        assert not p.needs_clarification, u
        assert p.parameters.get("enabled") is want, u


def test_intent_ambiguous_toggle_clarifies_for_enabled():
    p = LP.parse("toggle wifi", _set_ctx())
    assert p.needs_clarification  # no direction: never guess a radio change


def test_intent_mobile_data_state_regression():
    ctx = _set_ctx()
    p = LP.parse("mobile data status", ctx)
    assert p.capability == "system.mobile_data.state"
    assert not p.needs_clarification
