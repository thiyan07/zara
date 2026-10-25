"""P5 feature-hunt ②: Do-Not-Disturb state + verified toggle.

Pins the core contracts for android.dnd.get (SAFE, read-only) and
android.dnd.set (CONFIRM, schema-required `enabled` + approval-satisfied
`confirm` receipt — see test_p6_timer_confirm_chatpath.py for the
receipt mechanics). Android-side behavior (Kotlin grant check, filter
re-read, skip_ui structural refusal) is pinned in the Dart stage31
tests; a real grant screen answer is interactive-user-gated.
"""
from core.device_tools import DEVICE_TOOL_DEFS
from core.intent import LocalIntentParser, ParserContext
from core.models import PermissionLevel, RiskLevel

DND_GET = "android.dnd.get"
DND_SET = "android.dnd.set"

DND_SET_SCHEMA = {
    "type": "object", "required": ["enabled", "confirm"],
    "properties": {"enabled": {"type": "boolean"},
                   "skip_ui": {"type": "boolean"},
                   "confirm": {"type": "boolean"}},
}


def _defs_by_name():
    return {d.name: d for d in DEVICE_TOOL_DEFS}


def test_dnd_get_is_safe_read_only():
    d = _defs_by_name()[DND_GET]
    # SAFE reads use the PUBLIC permission tier (system.wifi.state
    # precedent): never prompts, never changes anything.
    assert d.permission == PermissionLevel.PUBLIC
    assert d.risk == RiskLevel.SAFE
    assert d.input_schema.get("required", []) == []
    assert d.supported_devices == ["android"]


def test_dnd_set_is_confirm_with_enabled_required():
    d = _defs_by_name()[DND_SET]
    assert d.permission == PermissionLevel.CONFIRM
    assert d.risk == RiskLevel.CONFIRM
    assert d.input_schema == DND_SET_SCHEMA
    assert d.supported_devices == ["android"]
    assert "Do-Not-Disturb" in d.description


def _ctx():
    return ParserContext(
        known_capabilities={DND_GET, DND_SET},
        capability_schemas={DND_GET: {"type": "object",
                                      "properties": {}},
                            DND_SET: DND_SET_SCHEMA},
        devices=[])


def test_dnd_toggle_direction_words_map_enabled():
    p = LocalIntentParser().parse(
        "turn on do not disturb", _ctx())
    assert p.capability == DND_SET
    assert p.parameters.get("enabled") is True
    p2 = LocalIntentParser().parse(
        "turn off do not disturb", _ctx())
    assert p2.capability == DND_SET
    assert p2.parameters.get("enabled") is False


def test_dnd_state_invite_maps_read_only():
    p = LocalIntentParser().parse("is dnd on", _ctx())
    assert p.capability == DND_GET
    assert p.needs_clarification is False
