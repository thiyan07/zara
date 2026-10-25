"""Wayland-native launch verification for desktop.app.open.

Covers the a11y-registry `apps` read op (atspi_probe), the tool-layer
helpers (`_atspi_apps`, `_app_matches_name`), and the bounded
verification flow of `desktop_app_open` on both X11 and Wayland.
All bus/timing effects are faked; no launch is ever dispatched here.
"""
import types

import pytest

from device.linux import tools_linux as tl


# ------------------------------------------------------------------
# _app_matches_name — the pure matcher
# ------------------------------------------------------------------

@pytest.mark.parametrize("app_id,name,want", [
    ("firefox", "Firefox Web Browser", True),
    ("firefox", "Firefox", True),
    ("org.gnome.TextEditor", "Text Editor", True),   # dot-suffix, spaces
    ("org.mozilla.firefox", "Firefox Web Browser", True),
    ("org.gnome.Snapshot", "Camera", False),         # no false positive
    ("gnome-calculator", "Calculator", True),        # name contained in id
    ("thing", "Something Else", False),              # foobar containment guard
])
def test_app_matches_name_ladder(app_id, name, want):
    assert tl._app_matches_name(app_id, name) is want


def test_app_matches_name_guards_empty_inputs():
    assert tl._app_matches_name("", "Firefox") is False
    assert tl._app_matches_name("firefox", "") is False
    assert tl._app_matches_name("", "") is False


# ------------------------------------------------------------------
# _atspi_apps — thin helper over the read op
# ------------------------------------------------------------------

def test_atspi_apps_passes_read_op_and_returns_result(monkeypatch):
    calls = []
    monkeypatch.setattr(tl, "_atspi_call",
                        lambda req, timeout=None: calls.append(req)
                        or {"apps": ["Firefox"], "apps_seen": 1})
    out = tl._atspi_apps()
    assert out["apps"] == ["Firefox"]
    assert calls == [{"op": "apps", "app_filter": "", "max_apps": 40}]


# ------------------------------------------------------------------
# desktop_app_open flows (session / bus / spawn all faked)
# ------------------------------------------------------------------

class _Ctx(dict):
    pass


@pytest.fixture()
def launch_env(monkeypatch):
    """Common fakes: app installed, gtk-launch present, spawn recorded,
    real sleeps removed, and read-helper call counts recorded the
    tests assert on (immune to leaked global time stubs)."""
    state = {"spawned": [], "session": "wayland", "wmctrl": 0,
             "atspi_apps": 0}

    monkeypatch.setattr(tl, "_desktop_app_ids", lambda: {"firefox"})
    monkeypatch.setattr(tl, "_run_quiet",
                        lambda argv, timeout=10:
                        types.SimpleNamespace(returncode=0, stdout=""))

    def fake_ds(_inputs, _ctx):
        return {"session": state["session"]}

    # desktop_session is a handler referenced inside tools_linux;
    # patch the name used there
    monkeypatch.setattr(tl, "desktop_session", fake_ds)

    def fake_wmctrl():
        state["wmctrl"] += 1
        w = state.get("windows")
        if w is None:
            return None
        try:
            return next(w)
        except StopIteration:
            raise AssertionError("test exhausted its window sequence")

    state["fake_wmctrl"] = fake_wmctrl

    def fake_popen(argv, **kw):
        state["spawned"].append(list(argv))
        return types.SimpleNamespace(pid=4242)

    monkeypatch.setattr(tl.subprocess, "Popen", fake_popen)

    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    return state


def test_app_open_bad_and_unknown_ids_refuse_first(launch_env):
    out = tl.desktop_app_open({"app_id": "bad id!"}, _Ctx())
    assert out["applied"] is False and "bad app_id" in out["error"]
    out2 = tl.desktop_app_open({"app_id": "not-installed"}, _Ctx())
    assert out2["applied"] is False and "not installed" in out2["error"]
    assert launch_env["spawned"] == []  # nothing was ever dispatched


def test_app_open_wayland_verified_on_new_matching_registry_entry(
        monkeypatch, launch_env):
    # BEFORE snapshot omits the app; poll 2 returns it
    seq = [{"apps": ["gnome-shell"], "registry_apps": 1},
           {"apps": ["gnome-shell", "Firefox Web Browser"],
            "registry_apps": 2}]

    def fake_apps(app_filter=""):
        launch_env["atspi_apps"] += 1
        return seq.pop(0) if seq else seq[-1]

    monkeypatch.setattr(tl, "_atspi_apps", fake_apps)
    out = tl.desktop_app_open({"app_id": "firefox"}, _Ctx())
    assert out["applied"] is True
    assert "verified" in out["verify_state"]
    assert out["app_name"] == "Firefox Web Browser"
    assert launch_env["spawned"] == [["gtk-launch", "firefox"]]
    # verified on poll 1: 1 BEFORE read + 1 post-launch poll
    assert launch_env["atspi_apps"] == 2


def test_app_open_wayland_requested_when_no_new_entry(monkeypatch,
                                                     launch_env):
    def fake_apps(app_filter=""):
        launch_env["atspi_apps"] += 1
        return {"apps": ["gnome-shell"], "registry_apps": 1}

    monkeypatch.setattr(tl, "_atspi_apps", fake_apps)
    out = tl.desktop_app_open({"app_id": "firefox"}, _Ctx())
    assert out["applied"] is True
    assert "requested" in out["verify_state"]
    assert "no matching registry entry" in out["verify_state"]
    # 1 BEFORE read + bounded 3 polls — then honest stop
    assert launch_env["atspi_apps"] == 4


def test_app_open_wayland_honest_when_a11y_down(monkeypatch, launch_env):
    def dead(app_filter=""):
        raise RuntimeError("a11y bus unreachable")

    monkeypatch.setattr(tl, "_atspi_apps", dead)
    out = tl.desktop_app_open({"app_id": "firefox"}, _Ctx())
    assert out["applied"] is True                  # launch still dispatched
    assert "requested" in out["verify_state"]
    assert "a11y verification unavailable" in out["verify_state"]
    # the BEFORE read is the only attempt and it raised; no polls
    assert launch_env["atspi_apps"] == 0


def test_app_open_x11_verified_by_new_window(monkeypatch, launch_env):
    launch_env["session"] = "x11"
    monkeypatch.setattr(tl, "_wmctrl_ids", launch_env["fake_wmctrl"])
    launch_env["windows"] = iter([{"0x1"}, {"0x1", "0x2"}])
    out = tl.desktop_app_open({"app_id": "firefox"}, _Ctx())
    assert out["applied"] is True
    assert "verified (a new window appeared)" in out["verify_state"]
    # 1 BEFORE read + 1 poll that saw the new window
    assert launch_env["wmctrl"] == 2


def test_app_open_x11_requested_when_no_new_window(monkeypatch, launch_env):
    launch_env["session"] = "x11"
    launch_env["windows"] = iter([{"0x1"}] * 20)   # never a new window
    monkeypatch.setattr(tl, "_wmctrl_ids", launch_env["fake_wmctrl"])
    out = tl.desktop_app_open({"app_id": "firefox"}, _Ctx())
    assert out["applied"] is True
    assert "requested" in out["verify_state"]
    # 1 BEFORE read + 5 polls + 1 final unreadable-probe check
    assert launch_env["wmctrl"] == 7


def test_app_open_x11_requested_when_window_list_unreadable(
        monkeypatch, launch_env):
    launch_env["session"] = "x11"
    # BEFORE read also unreadable → 'before = None' → the
    # unreadable-list honest outcome
    launch_env["windows"] = None
    monkeypatch.setattr(tl, "_wmctrl_ids", launch_env["fake_wmctrl"])
    out = tl.desktop_app_open({"app_id": "firefox"}, _Ctx())
    assert out["applied"] is True
    assert "requested (window list unreadable)" in out["verify_state"]


def test_app_open_launch_failure_refused_honestly(monkeypatch, launch_env):
    def broken(argv, **kw):
        raise OSError("exec failed")

    monkeypatch.setattr(tl.subprocess, "Popen", broken)
    out = tl.desktop_app_open({"app_id": "firefox"}, _Ctx())
    assert out["applied"] is False and "launch failed" in out["error"]


# ------------------------------------------------------------------
# op_apps (probe layer) — registry read with per-entry containment
# ------------------------------------------------------------------

def _probe_with_dbus_stub():
    stub = types.ModuleType("dbus")
    stub.Interface = lambda obj, iface: obj
    stub.SessionBus = object
    import sys as _s
    _s.modules.setdefault("dbus", stub)
    import device.linux.atspi_probe as ap
    return ap


def test_op_apps_reads_names_only_and_contains_failures():
    ap = _probe_with_dbus_stub()
    registry_children = [("app1", "/org/a11y/atspi/accessible/app1"),
                         ("app2", "/org/a11y/atspi/accessible/app2"),
                         ("app3", "/org/a11y/atspi/accessible/app3")]
    name_reads = {"app1": "Firefox Web Browser", "app2": "Broken App",
                  "app3": "Text Editor"}

    class FakeBus:
        def get_object(self, bname, path):
            return (bname, path)

    class FakeAcc:
        def __init__(self, obj):
            self.obj = obj

        def GetChildren(self, timeout=None):
            return registry_children

    class FakeProps:
        def __init__(self, obj):
            self.bname = obj[0]

        def Get(self, iface, prop, timeout=None):
            if self.bname == "app2":
                raise RuntimeError("name unreadable")
            return name_reads[self.bname]

    ap.acc = lambda bus, b, p: FakeAcc(bus.get_object(b, p))
    ap.props = lambda bus, b, p: FakeProps(bus.get_object(b, p))
    out = ap.op_apps(FakeBus(), {"max_apps": 40})
    # app2's read failure is contained per-entry; the other two survive
    assert out["apps"] == ["Firefox Web Browser", "Text Editor"]
    assert out["registry_apps"] == 3 and "error" not in out


def test_op_apps_filter_and_cap():
    ap = _probe_with_dbus_stub()
    children = [(f"app{i}", f"/p{i}") for i in range(70)]

    class FakeBus:
        def get_object(self, bname, path):
            return (bname, path)

    class FakeAcc:
        def __init__(self, obj):
            self.obj = obj

        def GetChildren(self, timeout=None):
            return children

    class FakeProps:
        def __init__(self, obj):
            self.i = int(obj[0][3:])

        def Get(self, iface, prop, timeout=None):
            return f"App {self.i}"

    ap.acc = lambda bus, b, p: FakeAcc(bus.get_object(b, p))
    ap.props = lambda bus, b, p: FakeProps(bus.get_object(b, p))
    out = ap.op_apps(FakeBus(), {"app_filter": "app 4", "max_apps": 3})
    assert out["apps"] == ["App 4", "App 40", "App 41"]
    assert out["registry_apps"] == 70
    out2 = ap.op_apps(FakeBus(), {})
    assert len(out2["apps"]) == 40  # default cap
    assert out2["registry_apps"] == 70


def test_op_apps_registry_failure_is_honest_data():
    ap = _probe_with_dbus_stub()

    class FakeBus:
        def get_object(self, bname, path):
            return (bname, path)

    class FakeAcc:
        def __init__(self, obj):
            self.obj = obj

        def GetChildren(self, timeout=None):
            raise RuntimeError("registry gone")

    ap.acc = lambda bus, b, p: FakeAcc(bus.get_object(b, p))
    out = ap.op_apps(FakeBus(), {})
    assert out == {"apps": [], "registry_apps": 0,
                   "error": "registry gone"}
