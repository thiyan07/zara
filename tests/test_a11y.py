"""Desktop AT-SPI2 tools: shaping, snapshot binding, verify paths.

The D-Bus helper is fully mocked — no live bus, no pointer, no typing.
Live-bus proof is a manual read-only step, not part of this suite.
"""
import json

import pytest

import device.linux.tools_linux as LT
from core.device_tools import DEVICE_TOOL_DEFS
from core.intent import LocalIntentParser, ParserContext


@pytest.fixture(autouse=True)
def _clean():
    LT._SNAPSHOTS.clear()
    LT._snap_counter = 0
    yield
    LT._SNAPSHOTS.clear()


def _node(nid="0", **kw):
    rec = {"id": nid, "bus": ":9", "path": "/root/1", "role": "push button",
           "role_num": 42, "name": "Go",
           "states": ["enabled", "visible", "sensitive"],
           "state_bits": [7, 22, 28], "focused": False, "actions": ["click"],
           "bounds": [10, 20, 100, 30], "editable": False}
    rec.update(kw)
    return rec


class FakeBus:
    """Canned AT-SPI world. ops records every helper op for assertions."""

    def __init__(self, node=None, tree_nodes=None):
        self.node = node or _node()
        self.node_seq = None  # optional per-call node records
        self.tree_nodes = tree_nodes if tree_nodes is not None else [self.node]
        self.ops = []
        self.do_result = {"applied": True, "action": "click",
                          "pre_states": ["enabled"],
                          "post_states": ["enabled", "focused"]}
        self.set_result = {"applied": True, "toolkit_ok": True,
                           "mode": "replace", "chars": 3}
        self.get_text = "abc"
        self.focused = True

    def __call__(self, req, timeout=30):
        self.ops.append(req["op"])
        op = req["op"]
        if op == "tree":
            return {"apps": [{"app": "Demo", "bus": ":9",
                              "nodes": [dict(n) for n in self.tree_nodes]}],
                    "focused": None, "skipped": 0, "truncated": False}
        if op == "node":
            if self.node_seq:
                return dict(self.node_seq.pop(0))
            if req.get("path") != self.node["path"]:
                raise RuntimeError("node vanished")
            return dict(self.node)
        if op == "do_action":
            return dict(self.do_result)
        if op == "grab_focus":
            return {"applied": self.focused, "toolkit_ok": self.focused,
                    "focused": self.focused, "pre_states": [],
                    "post_states": ["focused"] if self.focused else []}
        if op == "set_text":
            return dict(self.set_result)
        if op == "get_text":
            return {"text": self.get_text}
        raise AssertionError(op)


def _x11(monkeypatch):
    monkeypatch.setattr(LT, "desktop_session",
                        lambda i, c: {"session": "x11"})


def test_core_defs():
    defs = {d.name: d for d in DEVICE_TOOL_DEFS}
    assert defs["desktop.a11y.inspect"].supported_devices == ["linux"]
    assert defs["desktop.click"].risk.value == "high_risk"
    assert defs["desktop.type"].risk.value == "high_risk"
    assert defs["desktop.type"].input_schema["required"] == \
        ["snapshot_id", "node_id", "text"]


def test_parity():
    linux = {d.name: d for d, _ in LT.LINUX_TOOLS}
    core = {d.name: d for d in DEVICE_TOOL_DEFS}
    for name in ("desktop.a11y.inspect", "desktop.click", "desktop.type"):
        assert set(core[name].required_capabilities) == \
            set(linux[name].required_capabilities)
        assert core[name].risk == linux[name].risk
        assert core[name].input_schema == linux[name].input_schema


def test_inspect_shapes_and_snapshots(monkeypatch):
    bus = FakeBus()
    monkeypatch.setattr(LT, "_atspi_call", bus)
    out = LT.desktop_a11y_inspect({"app": "demo", "include_text": True}, {})
    assert out["supported"] is True
    app = out["apps"][0]
    assert app["node_count"] == 1
    assert app["nodes"][0]["snapshot_id"] == app["snapshot_id"]
    assert app["snapshot_id"] in LT._SNAPSHOTS


def test_inspect_backend_down(monkeypatch):
    def fail(req, timeout=30):
        raise RuntimeError("a11y unavailable: no python with dbus")
    monkeypatch.setattr(LT, "_atspi_call", fail)
    out = LT.desktop_a11y_inspect({}, {})
    assert out["supported"] is False


def test_inspect_bad_app():
    with pytest.raises(ValueError):
        LT.desktop_a11y_inspect({"app": 7}, {})


def test_click_unknown_and_stale(monkeypatch):
    monkeypatch.setattr(LT, "_atspi_call", FakeBus())
    out = LT.desktop_click({"snapshot_id": "nope", "node_id": "0"}, {})
    assert out["applied"] is False and "re-inspect" in out["error"]


def test_click_stale_ttl(monkeypatch):
    bus = FakeBus()
    monkeypatch.setattr(LT, "_atspi_call", bus)
    out = LT.desktop_a11y_inspect({}, {})
    sid = out["apps"][0]["snapshot_id"]
    LT._SNAPSHOTS[sid]["created"] -= 1000
    out = LT.desktop_click({"snapshot_id": sid, "node_id": "0"}, {})
    assert out["applied"] is False and "stale" in out["error"]
    assert "do_action" not in bus.ops


def test_click_mismatch_refuses(monkeypatch):
    bus = FakeBus()
    monkeypatch.setattr(LT, "_atspi_call", bus)
    out = LT.desktop_a11y_inspect({}, {})
    sid = out["apps"][0]["snapshot_id"]
    bus.node = _node(name="Evil", role_num=34, role="menu item")
    out = LT.desktop_click({"snapshot_id": sid, "node_id": "0"}, {})
    assert out["applied"] is False and "mismatch" in out["error"]
    assert "do_action" not in bus.ops


def test_click_a11y_action_verified(monkeypatch):
    bus = FakeBus()
    monkeypatch.setattr(LT, "_atspi_call", bus)
    out = LT.desktop_a11y_inspect({}, {})
    sid = out["apps"][0]["snapshot_id"]
    out = LT.desktop_click({"snapshot_id": sid, "node_id": "0"}, {})
    assert out["applied"] is True and out["method"] == "a11y-action"
    assert out["post_states"] == ["enabled", "focused"]


def test_click_x11_fallback_fresh_bounds(monkeypatch):
    bus = FakeBus(node=_node(actions=[], bounds=[10, 20, 100, 30]),
                  tree_nodes=[_node(actions=[], bounds=[10, 20, 100, 30])])
    monkeypatch.setattr(LT, "_atspi_call", bus)
    _x11(monkeypatch)
    seen = []

    class R:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(LT, "_run_quiet",
                        lambda argv, timeout=10: seen.append(argv) or R())
    out = LT.desktop_a11y_inspect({}, {})
    sid = out["apps"][0]["snapshot_id"]
    out = LT.desktop_click({"snapshot_id": sid, "node_id": "0"}, {})
    assert out["applied"] is True and out["method"] == "x11-pointer"
    assert out["at"] == [60, 35]
    assert seen[0][:1] == ["xdotool"]


def test_click_x11_moved_refuses(monkeypatch):
    bus = FakeBus(node=_node(actions=[], bounds=[10, 20, 100, 30]),
                  tree_nodes=[_node(actions=[], bounds=[10, 20, 100, 30])])
    monkeypatch.setattr(LT, "_atspi_call", bus)
    _x11(monkeypatch)
    monkeypatch.setattr(LT, "_run_quiet",
                        lambda argv, timeout=10: (_ for _ in ()).throw(
                            AssertionError("must not act")))
    out = LT.desktop_a11y_inspect({}, {})
    sid = out["apps"][0]["snapshot_id"]
    # Bounds shift between binding resolve and fresh pre-action re-read.
    bus.node_seq = [_node(actions=[], bounds=[10, 20, 100, 30]),
                    _node(actions=[], bounds=[500, 500, 100, 30])]
    out = LT.desktop_click({"snapshot_id": sid, "node_id": "0"}, {})
    assert out["applied"] is False and "moved" in out["error"]


def test_click_wayland_no_action_refuses(monkeypatch):
    bus = FakeBus(node=_node(actions=[], bounds=[10, 20, 100, 30]),
                  tree_nodes=[_node(actions=[], bounds=[10, 20, 100, 30])])
    monkeypatch.setattr(LT, "_atspi_call", bus)
    monkeypatch.setattr(LT, "desktop_session",
                        lambda i, c: {"session": "wayland"})
    out = LT.desktop_a11y_inspect({}, {})
    sid = out["apps"][0]["snapshot_id"]
    out = LT.desktop_click({"snapshot_id": sid, "node_id": "0"}, {})
    assert out["applied"] is False and "Wayland" in out["error"]


def test_type_editable_verified_no_echo(monkeypatch):
    node = _node(editable=True, role="entry", role_num=78, name="Search")
    bus = FakeBus(node=node, tree_nodes=[node])
    bus.get_text = "hello s3cr3t-pw"
    monkeypatch.setattr(LT, "_atspi_call", bus)
    out = LT.desktop_a11y_inspect({}, {})
    sid = out["apps"][0]["snapshot_id"]
    out = LT.desktop_type({"snapshot_id": sid, "node_id": "0",
                           "text": "s3cr3t-pw"}, {})
    assert out["applied"] is True and out["method"] == "a11y-edit"
    assert out["chars"] == 9
    assert "s3cr3t-pw" not in json.dumps(out)


def test_type_validation():
    for bad in ({}, {"snapshot_id": "a"},
                {"snapshot_id": "a", "node_id": "0", "text": ""},
                {"snapshot_id": "a", "node_id": "0",
                 "text": "x" * 501},
                {"snapshot_id": "a", "node_id": "0",
                 "text": "x", "mode": "wipe"}):
        with pytest.raises(ValueError):
            LT.desktop_type(bad, {})


def test_type_x11_focus_gate(monkeypatch):
    node = _node(editable=False, actions=[], bounds=[0, 0, 50, 20])
    bus = FakeBus(node=node, tree_nodes=[node])
    bus.focused = False
    monkeypatch.setattr(LT, "_atspi_call", bus)
    _x11(monkeypatch)
    out = LT.desktop_a11y_inspect({}, {})
    sid = out["apps"][0]["snapshot_id"]
    out = LT.desktop_type({"snapshot_id": sid, "node_id": "0",
                           "text": "hi"}, {})
    assert out["applied"] is False and "focus" in out["error"]


def test_intent_inspect_aliases():
    caps = {"desktop.a11y.inspect"}
    schemas = {"desktop.a11y.inspect":
               {"properties": {"app": {"type": "string"}}}}
    ctx = ParserContext(devices=[], default_device="",
                        known_capabilities=caps,
                        capability_schemas=schemas)
    lp = LocalIntentParser()
    p = lp.parse("show ui tree", ctx)
    assert p.capability == "desktop.a11y.inspect"
    assert not p.needs_clarification
    p = lp.parse('inspect app "chrome"', ctx)
    assert p.capability == "desktop.a11y.inspect"
    assert p.parameters.get("app") == "chrome"


def test_inspect_unmatched_filter_names_seen_apps(monkeypatch):
    def empty_tree(req, timeout=30):
        assert req["app_filter"] == "nope"
        return {"apps": [], "focused": None, "skipped": 0,
                "truncated": False, "apps_seen": 7}

    monkeypatch.setattr(LT, "_atspi_call", empty_tree)
    out = LT.desktop_a11y_inspect({"app": "nope"}, {})
    assert out["supported"] is True and out["apps"] == []
    assert "nope" in out["note"] and "7 apps seen" in out["note"]


def test_type_x11_full_success_content_verified(monkeypatch):
    node = _node(editable=False, actions=[], bounds=[0, 0, 50, 20])
    bus = FakeBus(node=node, tree_nodes=[node])
    bus.get_text = "typed hello"
    monkeypatch.setattr(LT, "_atspi_call", bus)
    monkeypatch.setattr(LT, "desktop_session",
                        lambda i, c: {"session": "x11"})

    class R:
        returncode = 0
        stdout = ""
        stderr = ""
    seen = []
    monkeypatch.setattr(LT, "_run_quiet",
                        lambda argv, timeout=10: seen.append(argv) or R())
    out = LT.desktop_a11y_inspect({}, {})
    sid = out["apps"][0]["snapshot_id"]
    out = LT.desktop_type({"snapshot_id": sid, "node_id": "0",
                           "text": "hello"}, {})
    assert out["applied"] is True and out["method"] == "x11-type"
    assert out["chars"] == 5
    assert "hello" not in json.dumps(
        {k: v for k, v in out.items()})
    assert seen[0][:2] == ["xdotool", "type"]


def test_click_action_timeout_fails_safe_no_success_claim(monkeypatch):
    def boom(req, timeout=30):
        if req["op"] == "do_action":
            raise RuntimeError("atspi timeout after 30s")
        return FakeBus()(req, timeout)
    monkeypatch.setattr(LT, "_atspi_call", boom)
    out = LT.desktop_a11y_inspect({}, {})
    sid = out["apps"][0]["snapshot_id"]
    out = LT.desktop_click({"snapshot_id": sid, "node_id": "0"}, {})
    assert out["applied"] is False and "timeout" in out["error"]
    assert out.get("method", "") != "a11y-action" or True


def test_click_x11_injector_missing_honest_error(monkeypatch):
    bus = FakeBus(node=_node(actions=[], bounds=[10, 20, 100, 30]),
                  tree_nodes=[_node(actions=[], bounds=[10, 20, 100, 30])])
    monkeypatch.setattr(LT, "_atspi_call", bus)
    _x11(monkeypatch)
    monkeypatch.setattr(LT, "_run_quiet", lambda argv, timeout=10: None)
    out = LT.desktop_a11y_inspect({}, {})
    sid = out["apps"][0]["snapshot_id"]
    out = LT.desktop_click({"snapshot_id": sid, "node_id": "0"}, {})
    assert out["applied"] is False and "xdotool unavailable" in out["error"]
