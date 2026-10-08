"""Validator regression tests (post-Rung-1 forensic review).

Pins exact boundaries of core/tools.py validate_against_schema, documents
characterized (not enforced) behaviors, and proves the two restored
contracts: code.apply_patch 100KB handler envelope and MCP 8KiB transport
envelope. No device, no network.
"""
import json

import pytest

from core.app import build_stack
from core.models import PermissionLevel, RiskLevel, ToolDefinition
from core.tools import ToolRegistry, validate_against_schema

CAP = "android.app.force_stop"
VICTIM = "dev.zara.lab.privtest"


def flat_tool():
    return ToolDefinition(name="t.flat", description="flat",
                          input_schema={"type": "object",
                                        "properties": {"x": {"type": "string"}}},
                          permission=PermissionLevel.PUBLIC,
                          risk=RiskLevel.SAFE)


def reg_with(*defs):
    r = ToolRegistry()
    for d, h in defs:
        r.register(d, h)
    return r


def pad_to(payload: dict, size: int) -> dict:
    out = dict(payload)
    cur = len(json.dumps(out))
    out["pad"] = "x" * max(0, size - cur - len('"pad": ""') - 2)
    assert len(json.dumps(out)) == size, len(json.dumps(out))
    return out


# ---------- exact boundaries ----------

def test_count_16_ok_17_fail():
    r = reg_with((flat_tool(), lambda i, c: {"n": len(i)}))
    ok = {"x": "hi", **{f"k{i}": "v" for i in range(15)}}
    assert len(ok) == 16
    assert r.call("t.flat", ok) == {"n": 16}
    bad = dict(ok, k15="v")
    assert len(bad) == 17
    with pytest.raises(ValueError, match="too many input fields"):
        r.call("t.flat", bad)


def test_bytes_4096_ok_4097_fail():
    r = reg_with((flat_tool(), lambda i, c: {"ok": True}))
    assert r.call("t.flat", pad_to({"x": "hi"}, 4096)) == {"ok": True}
    with pytest.raises(ValueError, match="input too large"):
        r.call("t.flat", pad_to({"x": "hi"}, 4097))


def test_depth_4_ok_5_fail():
    r = reg_with((ToolDefinition(
        name="t.nest", description="nest",
        input_schema={"type": "object", "required": ["nest"],
                      "properties": {"nest": {"type": "object"}}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE),
        lambda i, c: {"ok": True}))
    assert r.call("t.nest", {"nest": {"a": {"b": {"c": "v"}}}}) == \
        {"ok": True}
    with pytest.raises(ValueError, match="too deeply nested"):
        r.call("t.nest", {"nest": {"a": {"b": {"c": {"d": "v"}}}}})


# ---------- nested additionalProperties is top-level-only (pinned) ----------

def test_nested_additional_properties_not_enforced():
    r = reg_with((ToolDefinition(
        name="t.wrap", description="wrap",
        input_schema={"type": "object",
                      "properties": {
                          "x": {"type": "string"},
                          "outer": {"type": "object",
                                    "additionalProperties": False,
                                    "properties": {"known": {"type": "integer"}}}}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE),
        lambda i, c: {"ok": True}))
    # Documents current behavior: nested flag is not enforced.
    assert r.call("t.wrap", {"x": "hi",
                             "outer": {"known": 1, "rogue": 2}}) == \
        {"ok": True}


# ---------- authority-shaped registry inputs on CAP ----------

def test_authority_shaped_inputs_rejected_on_cap():
    stack = build_stack()
    for f in ("grant_id", "approved", "trust", "authorized", "allow"):
        with pytest.raises(ValueError, match="unknown field"):
            stack["registry"].call(
                CAP, {"target_package": VICTIM, f: "x"},
                {"device_id": "x"})


# ---------- permissive side pinned (non-flag tools accept extras) ----------

def test_unknown_extra_accepted_without_flag():
    r = reg_with((flat_tool(), lambda i, c: {"got": i["x"]}))
    assert r.call("t.flat",
                  {"x": "hi", "note": "legit extra"}) == {"got": "hi"}


# ---------- restored contracts ----------

def test_apply_patch_large_patch_passes_validation():
    stack = build_stack()
    d = stack["registry"].get("code.apply_patch")
    assert d.max_input_bytes == 102400
    blob = ("diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n"
            + "x" * 50000)
    assert len(blob) > 4096  # would fail the default envelope
    assert validate_against_schema(
        {"workspace": "/tmp/w", "patch": blob}, d.input_schema,
        d.max_input_bytes) == []


def test_mcp_transport_envelope_restored():
    import os as _os
    import sys as _sys
    from core.mcp import MCPRegistry, MCPServerConfig
    stack = build_stack()
    reg = MCPRegistry()
    try:
        # Absolute path: the fixture server spawns with cwd=$HOME.
        fixture = _os.path.join(
            _os.path.dirname(__file__), "fixtures", "mcp_test_server.py")
        names = reg.add_server(MCPServerConfig(
            name="test", command=[_sys.executable, fixture]),
            stack["registry"])
        assert "mcp.test.echo" in names
        d = stack["registry"].get("mcp.test.echo")
        assert d.max_input_bytes == 8192
        big = {"text": "x" * 6000}
        assert 4096 < len(json.dumps(big)) <= 8192
        assert validate_against_schema(big, d.input_schema,
                                       d.max_input_bytes) == []
        over = {"text": "x" * 9000}
        errs = validate_against_schema(over, d.input_schema,
                                       d.max_input_bytes)
        assert any("too large" in e for e in errs)
    finally:
        reg.close_all()


# ---------- unknown type fails closed, no crash ----------

def test_unknown_type_name_fails_closed():
    errs = validate_against_schema(
        {"x": 1},
        {"type": "object", "properties": {"x": {"type": "file"}}})
    assert errs and all(isinstance(e, str) for e in errs)


# ---------- envelope applies without schema ----------

def test_bounds_apply_to_schemaless_tools():
    assert validate_against_schema({f"k{i}": "v" for i in range(17)},
                                   {}) == ["too many input fields (max 16)"]
    assert validate_against_schema({"x": "hi"}, {}) == []


# ---------- malformed nested registry input (characterization) ----------

def test_nested_contents_unchecked_registry_path():
    r = reg_with((ToolDefinition(
        name="t.arr", description="arr",
        input_schema={"type": "object",
                      "properties": {"items": {"type": "array"}}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE),
        lambda i, c: {"ok": True}))
    # Depth/size/count only; nested element contents are not type-checked.
    assert r.call("t.arr", {"items": [{"a": [1, 2]}]}) == {"ok": True}
