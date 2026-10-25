"""Universal-operator workflows across Android, Linux, and a multi-device
plan, through the REAL core chain (registry -> resolver -> policy ->
fabric -> engine -> verification -> audit).

Label: CORE-UNIT with stub-completed device jobs, plus REAL-FILESYSTEM
units for the desktop app discovery/launch implementations. Android
device behaviour (apps.list, gui.back, app-open focus verification) is
contracted here with fake job payloads; live emulator validation is
reported separately, never implied by these tests.

No parallel system: every execution goes through engine.submit /
approve exactly like the rung-2.gui tests.
"""
import json
import os
import threading
import time

import pytest

from core.models import DeviceKind, DeviceState, ExecutionState
from core.tools import validate_against_schema
from tests.test_intent import doc, enroll_claim, make_stack, register

PHONE = "emu-lab"
LAPTOP = "laptop-1"


def _android(stack, dev=PHONE, caps=()):
    code = stack["device_auth"].enroll(dev, DeviceKind("android"))
    stack["device_auth"].claim(code)
    registry_capture = stack["devices"].register(DeviceState(
        device_id=dev, kind=DeviceKind("android"),
        capabilities=list(caps), online=True, status="online"))
    return registry_capture


def _linux(stack, dev=LAPTOP, caps=()):
    code = stack["device_auth"].enroll(dev, DeviceKind("linux"))
    stack["device_auth"].claim(code)
    return stack["devices"].register(DeviceState(
        device_id=dev, kind=DeviceKind("linux"),
        capabilities=list(caps), online=True, status="online"))


def complete_jobs_like(stack, device_id, result, timeout_s=25.0):
    """Stub device: poll until a job appears, complete it, return True."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = stack["jobs"].poll(device_id)
        if job is not None:
            stack["jobs"].complete(job.id, ok=True, result=result)
            return True
        time.sleep(0.05)
    return False


def fail_job_like(stack, device_id, error, timeout_s=25.0):
    """Stub device: poll until a job appears, fail it, return True."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = stack["jobs"].poll(device_id)
        if job is not None:
            stack["jobs"].complete(job.id, ok=False, error=error)
            return True
        time.sleep(0.05)
    return False


def approve_with_failing_device(stack, rec, device_id, error):
    """Approve a WAITING record while a stub device fails its job."""
    t = threading.Thread(
        target=fail_job_like, args=(stack, device_id, error),
        daemon=True)
    t.start()
    out = stack["engine"].approve(rec.id)
    t.join(timeout=30)
    return out


def await_rec(stack, rec, want, thread):
    """Stage-30 helper: join the stub-device thread and await the record
    reaching one of `want` states (auto-allow path has no approve step)."""
    thread.join(timeout=30)
    deadline = time.time() + 4.0
    while time.time() < deadline:
        out = stack["engine"].get(rec.id)
        if out.state.value in want:
            return out
        time.sleep(0.05)
    return stack["engine"].get(rec.id)


def drain_job(stack, device_id, result):
    """Auto-approved (SAFE/PUBLIC) path: a stub device drains its job."""
    t = threading.Thread(
        target=complete_jobs_like, args=(stack, device_id, result),
        daemon=True)
    t.start()
    return t


def approve_with_stub_device(stack, rec, device_id, result):
    """Approve a WAITING record while a stub device drains its job."""
    t = threading.Thread(
        target=complete_jobs_like, args=(stack, device_id, result),
        daemon=True)
    t.start()
    out = stack["engine"].approve(rec.id)
    t.join(timeout=30)
    return out


# ---------- A. Android app discovery (android.apps.list) ----------

def test_A1_apps_list_contract_public_safe_discovery_only():
    stack = make_stack()
    d = stack["registry"].get("android.apps.list")
    assert d.permission.value == "public"
    assert d.risk.value == "safe"
    assert d.supported_devices == ["android"]
    assert d.required_capabilities == ["android.apps.list"]
    props = d.input_schema.get("properties", {})
    assert set(props) <= {"limit"}
    assert "never" in d.description.lower()


def test_A2_apps_list_stub_completed_discovery_rows():
    stack = make_stack()
    _android(stack, caps=["android.apps.list"])
    stack["fabric"].advertise([doc("android.apps.list")], PHONE)
    t = drain_job(stack, PHONE, {
        "count": 2, "truncated": False,
        "apps": [{"package": "dev.zara.zara_android", "label": "Zara"},
                 {"package": "com.android.settings",
                  "label": "Settings"}]})
    rec = stack["engine"].submit("android.apps.list", {}, PHONE,
                                 who="user")
    t.join(timeout=15)
    assert rec.state == ExecutionState.SUCCEEDED, rec.state
    assert rec.result["count"] == 2
    assert rec.result["apps"][0]["package"] == "dev.zara.zara_android"
    blob = json.dumps(stack["audit"].query(200))
    assert "X-Device-Key" not in blob and "device_key" not in blob


def _apps_list_fail(stack, error):
    t = threading.Thread(
        target=fail_job_like, args=(stack, PHONE, error), daemon=True)
    t.start()
    return t


def test_A3_apps_list_device_failure_is_failure_not_invention():
    stack = make_stack()
    _android(stack, caps=["android.apps.list"])
    stack["fabric"].advertise([doc("android.apps.list")], PHONE)
    t = _apps_list_fail(stack, "app listing failed: package query error")
    rec = stack["engine"].submit("android.apps.list", {}, PHONE,
                                 who="user")
    t.join(timeout=15)
    assert rec.state == ExecutionState.FAILED
    assert "failed" in (rec.error or "").lower()


# ---------- A'. gui.back (guarded global navigation) ----------

def test_A4_gui_back_contract_targets_and_direction():
    stack = make_stack()
    from core.device_tools import DEVICE_TOOL_DEFS, GUI_LAB_TARGETS
    d = stack["registry"].get("gui.back")
    assert d.risk.value == "high_risk"
    assert d.permission.value == "restricted"
    assert d.supported_devices == ["android"]
    enum = d.input_schema["properties"]["target_package"]["enum"]
    assert set(enum) == set(GUI_LAB_TARGETS)
    assert set(d.input_schema["properties"]["direction"]["enum"]) == \
        {"back", "home"}
    names = {t.name for t in DEVICE_TOOL_DEFS}
    assert "gui.back" in names and "android.apps.list" in names


def test_A5_gui_back_submit_parks_zero_dispatch_before_approval():
    stack = make_stack()
    _android(stack, caps=["gui.back"])
    stack["fabric"].advertise([doc("gui.back", risk="high_risk")], PHONE)
    rec = stack["engine"].submit(
        "gui.back",
        {"target_package": "dev.zara.zara_android",
         "expected_package": "dev.zara.zara_android"},
        PHONE, who="user")
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    assert stack["jobs"].poll(PHONE) is None  # zero dispatch


def test_A6_gui_back_happy_path_returns_fresh_snapshot():
    stack = make_stack()
    _android(stack, caps=["gui.back"])
    stack["fabric"].advertise([doc("gui.back", risk="high_risk")], PHONE)
    rec = stack["engine"].submit(
        "gui.back",
        {"target_package": "dev.zara.zara_android",
         "expected_package": "dev.zara.zara_android"},
        PHONE, who="user")
    final = approve_with_stub_device(stack, rec, PHONE, {
        "applied": True, "direction": "back",
        "package_before": "dev.zara.zara_android",
        "package_after": "com.android.launcher",
        "fresh_snapshot_id": "fff012ab:3", "verify_state": "applied"})
    assert final.state == ExecutionState.SUCCEEDED, final.state
    assert final.result["fresh_snapshot_id"] == "fff012ab:3"
    assert final.result["package_after"] == "com.android.launcher"


def test_A7_gui_back_device_refusal_is_structured_failure():
    stack = make_stack()
    _android(stack, caps=["gui.back"])
    stack["fabric"].advertise([doc("gui.back", risk="high_risk")], PHONE)
    rec = stack["engine"].submit(
        "gui.back",
        {"target_package": "dev.zara.zara_android",
         "expected_package": "dev.zara.zara_android"},
        PHONE, who="user")
    final = approve_with_failing_device(
        stack, rec, PHONE,
        "foreground package changed (refused, never executed)")
    assert final.state == ExecutionState.FAILED
    assert "refused" in (final.error or "")
    blob = json.dumps(stack["audit"].query(200))
    assert "refused" in blob.lower()  # honest audit, message-free
    assert "zara-dev-" not in blob and "device_key" not in blob


# ---------- L. Linux desktop app discovery + launch (real FS) ----------

@pytest.fixture()
def app_dirs(tmp_path, monkeypatch):
    """Two XDG application dirs with real .desktop entries."""
    sysdir = tmp_path / "sys" / "applications"
    localdir = tmp_path / "local" / "applications"
    sysdir.mkdir(parents=True)
    localdir.mkdir(parents=True)
    (sysdir / "org.gnome.Files.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Files\n"
        "Exec=nautilus --new-window\n", encoding="utf-8")
    (sysdir / "com.extra.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Dup\n", encoding="utf-8")
    (sysdir / "Hidden.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Hidden\nNoDisplay=true\n",
        encoding="utf-8")
    (sysdir / "LinkOnly.desktop").write_text(
        "[Desktop Entry]\nType=Link\nName=Web\n", encoding="utf-8")
    (localdir / "dev.zara.notes.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Zara Notes\n",
        encoding="utf-8")
    home = tmp_path / "localhome"
    (home / ".local" / "share" / "applications").mkdir(parents=True)
    (home / ".local" / "share" / "applications" /
     "dev.zara.notes.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Zara Notes\n",
        encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_DIRS", f"{tmp_path}/sys")
    monkeypatch.setenv("HOME", str(home))
    from device.linux import tools_linux as tl
    return tl


def _apps(tl):
    return tl.desktop_apps_list({}, {})["apps"]


def test_L1_apps_list_parses_real_entries_excludes_hidden_link_exec(
        app_dirs):
    tl = app_dirs
    out = tl.desktop_apps_list({}, {})
    assert out["supported"] is True
    apps = _apps(tl)
    ids = {row["app_id"] for row in apps}
    assert {"org.gnome.Files", "com.extra",
            "dev.zara.notes"} <= ids
    assert "Hidden" not in ids and "LinkOnly" not in ids
    files = next(row for row in apps if row["app_id"] == "org.gnome.Files")
    assert files["label"] == "Files"
    # Exec values never surface (untrusted shell args).
    assert all("Exec" not in row and "exec" not in row for row in apps)
    assert "not an authorization" in out["note"]


def test_L2_apps_list_cap_and_symlink_excluded(app_dirs, tmp_path,
                                               monkeypatch):
    tl = app_dirs
    monkeypatch.setattr(tl, "_APPS_LIMIT", 1)
    # A symlinked .desktop entry must never be followed.
    sys_apps = tmp_path / "sys" / "applications"
    os.symlink("/etc/passwd", sys_apps / "evil.desktop")
    out = tl.desktop_apps_list({}, {})
    assert out["supported"] is True
    assert len(out["apps"]) == 1 and out["truncated"] is True
    apps = {r["app_id"] for r in out["apps"]}
    assert "evil" not in apps


def test_L3_app_open_refuses_bad_or_unknown_ids(app_dirs):
    tl = app_dirs
    for bad in ("", "../etc/passwd", " Spiele; rm -rf x"):
        out = tl.desktop_app_open({"app_id": bad}, {})
        assert out["applied"] is False
        assert out["error"]
    out = tl.desktop_app_open({"app_id": "not-installed.app"}, {})
    assert out["applied"] is False
    assert "not installed" in out["error"]


def test_L4_app_open_x11_verified_by_new_window(app_dirs, monkeypatch):
    tl = app_dirs
    launches = []

    class FakeProc:
        pass

    def fake_popen(argv, **kw):
        launches.append(argv)
        return FakeProc()

    monkeypatch.setattr(tl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tl, "desktop_session", lambda i, c: {
        "session": "x11"})
    monkeypatch.setattr(
        tl, "_run_quiet",
        lambda argv, timeout=10: type("R", (), {"returncode": 0})())
    before = {"0x01000001", "0x01000002"}
    after = before | {"0x01000009"}
    states = iter([before, before, after])
    monkeypatch.setattr(tl, "_wmctrl_ids", lambda: next(states))
    out = tl.desktop_app_open({"app_id": "org.gnome.Files"}, {})
    assert launches == [["gtk-launch", "org.gnome.Files"]]
    assert out["supported"] is True and out["applied"] is True
    assert "new window" in out["verify_state"]


def test_L5_app_open_wayland_honest_requested(app_dirs, monkeypatch):
    tl = app_dirs

    class FakeProc:
        pass

    monkeypatch.setattr(tl.subprocess, "Popen", lambda a, **kw: FakeProc())
    monkeypatch.setattr(tl, "desktop_session", lambda i, c: {
        "session": "wayland"})
    monkeypatch.setattr(tl, "_run_quiet",
                        lambda a, timeout=10:
                        type("R", (), {"returncode": 0})())
    # Wayland round 5: verification rides the a11y registry, not
    # wmctrl; fake it as unreachable → launch dispatched, honestly
    # 'requested' (a11y verification unavailable).
    monkeypatch.setattr(tl, "_atspi_apps",
                        lambda app_filter="":
                        (_ for _ in ()).throw(RuntimeError("a11y down")))
    called = {"wmctrl": 0}
    monkeypatch.setattr(tl, "_wmctrl_ids",
                        lambda: called.__setitem__(
                            "wmctrl", called["wmctrl"] + 1) or set())
    out = tl.desktop_app_open({"app_id": "org.gnome.Files"}, {})
    assert out["supported"] is True and out["applied"] is True
    assert "requested" in out["verify_state"]
    assert "a11y verification unavailable" in out["verify_state"]
    assert called["wmctrl"] == 0  # no window probes on Wayland


def test_L6_app_open_unavailable_launcher_refused(app_dirs, monkeypatch):
    tl = app_dirs
    monkeypatch.setattr(tl, "_run_quiet", lambda a, timeout=10: None)
    out = tl.desktop_app_open({"app_id": "org.gnome.Files"}, {})
    assert out["supported"] is False and out["applied"] is False
    assert "gtk-launch unavailable" in out["error"]


def test_L7_core_defs_and_linux_parity_present():
    stack = make_stack()
    d = stack["registry"].get("desktop.apps.list")
    assert d.permission.value == "public" and d.risk.value == "safe"
    d2 = stack["registry"].get("desktop.app.open")
    assert d2.permission.value == "confirm" and d2.risk.value == "confirm"
    assert d2.supported_devices == ["linux"]
    # app_id sourced from a real list — the def says so
    assert "desktop.apps.list" in d2.input_schema["properties"]["app_id"][
        "description"]


# ---------- N. NL routing reachability ----------

def _nl_ctx():
    from core.intent import ParserContext, ParserDevice
    return ParserContext(
        known_capabilities={
            "android.apps.list", "desktop.apps.list", "desktop.app.open",
            "gui.back", "system.app.open", "desktop.window.list"},
        capability_schemas={}, devices=[
            ParserDevice(device_id="vivo-real", kind="android",
                         labels=["vivo", "phone"]),
            ParserDevice(device_id="laptop-1", kind="linux",
                         labels=["laptop"])])


def test_N1_app_discovery_reachable_through_nl():
    from core.intent import LocalIntentParser
    p = LocalIntentParser()
    cases = [("apps on my phone", "android.apps.list", "vivo-real"),
             ("apps on my computer", "desktop.apps.list", "laptop-1")]
    for utterance, cap, dev in cases:
        out = p.parse(utterance, _nl_ctx())
        assert out.capability == cap and not out.needs_clarification, \
            out
        assert out.preferred_device == dev


def test_N2_desktop_app_open_param_and_clarification():
    from core.intent import LocalIntentParser
    p = LocalIntentParser()
    out = p.parse('launch desktop app "org.gnome.Nautilus"', _nl_ctx())
    assert out.capability == "desktop.app.open"
    assert out.parameters == {"app_id": "org.gnome.Nautilus"}
    bare = p.parse("launch desktop app", _nl_ctx())
    assert bare.needs_clarification and bare.capability == ""


def test_N3_gui_back_requires_named_lab_target_and_direction():
    from core.intent import LocalIntentParser
    p = LocalIntentParser()
    bare = p.parse("go back", _nl_ctx())
    assert bare.needs_clarification and bare.capability == ""
    named = p.parse("go home now in dev.zara.zara_android",
                    _nl_ctx())
    assert named.capability == "gui.back"
    assert named.parameters["direction"] == "home"
    assert named.parameters["target_package"] == "dev.zara.zara_android"


# ---------- X. Cross-device plan (one mission, two devices) ----------

def test_X1_mixed_phone_and_laptop_plan_single_mission():
    from core.intent import IntentTurnService
    from core.missions import MissionEngine, MissionState as MS
    from core.resolver import CapabilityResolver
    stack = make_stack()
    _android(stack, caps=["android.apps.list"])
    stack["fabric"].advertise([doc("android.apps.list")], PHONE)
    stack["registry"].register(
        __import__("core.models", fromlist=["ToolDefinition"]
                   ).ToolDefinition(
            name="test.linuxecho", description="bounded linux echo",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"}),
        lambda inputs, ctx=None: {"ok": True})
    _linux(stack, caps=["test.linuxecho"])
    stack["fabric"].advertise([doc("test.linuxecho")], LAPTOP)

    steps = [
        {"capability": "test.linuxecho", "parameters": {},
         "preferred_device": LAPTOP, "confidence": 0.9},
        {"capability": "android.apps.list", "parameters": {"limit": 50},
         "preferred_device": PHONE, "confidence": 0.9},
    ]
    eng = MissionEngine()

    class FixedParser:
        name = "fixed"

        def parse(self, utterance, ctx, session_context=""):
            from core.intent import IntentProposal
            return IntentProposal(utterance=utterance[:500],
                                  capability=steps[0]["capability"],
                                  parameters={}, steps=steps,
                                  confidence=0.9,
                                  needs_clarification=False,
                                  source="fixed")

    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FixedParser(), stack["audit"], eng)
    t = drain_job(stack, PHONE, {"count": 0, "truncated": False,
                                 "apps": []})
    out = svc.handle_utterance("echo then list my phone apps")
    t.join(timeout=15)
    assert out["executed"] is True, out
    assert [s["status"] for s in out["steps"]] == ["ok", "ok"], out
    m = eng.get(out["mission_id"])
    assert m.state == MS.COMPLETED
    assert m.steps[0].tool == "test.linuxecho" and m.steps[0].status == "ok"
    assert m.steps[1].tool == "android.apps.list" and \
        m.steps[1].status == "ok"


def test_X2_plan_stops_when_later_device_cannot_resolve():
    from core.intent import IntentTurnService
    from core.missions import MissionEngine, MissionState as MS
    from core.resolver import CapabilityResolver
    stack = make_stack()
    _android(stack, caps=["android.apps.list"])
    stack["fabric"].advertise([doc("android.apps.list")], PHONE)
    steps = [
        {"capability": "android.apps.list", "parameters": {},
         "preferred_device": PHONE, "confidence": 0.9},
        {"capability": "android.apps.list", "parameters": {},
         # offline second phone: unresolvable step stops the plan
         "preferred_device": "offline-phone", "confidence": 0.9},
    ]
    eng = MissionEngine()

    class FixedParser:
        name = "fixed"

        def parse(self, utterance, ctx, session_context=""):
            from core.intent import IntentProposal
            return IntentProposal(utterance=utterance[:500],
                                  capability=steps[0]["capability"],
                                  parameters={}, steps=steps,
                                  confidence=0.9,
                                  needs_clarification=False,
                                  source="fixed")

    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FixedParser(), stack["audit"], eng)
    t = drain_job(stack, PHONE, {"count": 0, "truncated": False,
                                 "apps": []})
    out = svc.handle_utterance("good then impossible")
    t.join(timeout=15)
    # Step 1 executed; the unresolvable second step stops the plan
    # honestly (no speculation, no rollback of done work).
    m = eng.get(out["mission_id"])
    assert m.state == MS.FAILED
    assert m.steps[0].tool == "android.apps.list" and \
        m.steps[0].status == "ok"
    assert m.steps[1].status != "ok"
    assert "step 2" in out["message"]


# ---------- A''. Passive notification read (android.notifications.read) ----------

def test_N1_notif_read_contract_restricted_confirm_observation_only():
    stack = make_stack()
    d = stack["registry"].get("android.notifications.read")
    assert d.permission.value == "restricted"
    assert d.risk.value == "confirm"
    assert d.supported_devices == ["android"]
    assert d.required_capabilities == ["android.notifications.read"]
    props = d.input_schema.get("properties", {})
    assert set(props) <= {"since_ms", "limit"}
    # Honesty requirements are contractual: the grant is the user's
    # (Settings), never silently given, and the read acts on nothing.
    assert "settings" in d.description.lower()
    assert "scrubbed" in d.description.lower()


def test_N2_notif_read_stub_completed_rows_pass_through():
    stack = make_stack()
    _android(stack, caps=["notifications.read"])
    stack["fabric"].advertise([doc("android.notifications.read")], PHONE)
    payload = {
        "granted": True, "count": 2, "truncated": False,
        "items": [
            {"posted_at_ms": 1700000000000, "package": "com.example.mail",
             "title": "Mail", "text": "meeting at 4", "category": "email"},
            {"posted_at_ms": 1700000050000, "package": "dev.zara.zara_android",
             "title": "Approvals", "text": "1 waiting",
             "category": "status"}]}
    # Stage 30: notification READ is read-only auto-allow — the record
    # runs directly (no approval park; that matrix is pinned in
    # test_s30_read_only_allow). The stub device drains the job; start
    # it FIRST so the drained job never races the record's wait window.
    stubber = drain_job(stack, PHONE, payload)
    rec = stack["engine"].submit(
        "android.notifications.read", {"limit": 20}, PHONE, who="user")
    final = await_rec(stack, rec, {"succeeded"}, stubber)
    assert final.state == ExecutionState.SUCCEEDED, final.state
    assert final.state == ExecutionState.SUCCEEDED, final.state
    assert final.result["count"] == 2
    assert final.result["items"][0]["package"] == "com.example.mail"
    blob = json.dumps(stack["audit"].query(200))
    assert "X-Device-Key" not in blob and "device_key" not in blob \
        and "Bearer" not in blob


def test_N3_notif_read_missing_grant_is_honest_failure():
    stack = make_stack()
    _android(stack, caps=["notifications.read"])
    stack["fabric"].advertise([doc("android.notifications.read")], PHONE)
    stubber = threading.Thread(
        target=fail_job_like, args=(stack, PHONE,
                                    "notification listener access not granted"),
        daemon=True)
    stubber.start()
    rec = stack["engine"].submit("android.notifications.read", {},
                                 PHONE, who="user")
    final = await_rec(stack, rec, {"failed"}, stubber)
    assert final.state == ExecutionState.FAILED
    assert "not granted" in (final.error or "").lower()


def test_N4_notif_read_nl_routing_and_bounds():
    stack = make_stack()
    _android(stack, caps=["notifications.read"])
    _linux(stack)
    out = stack["intent"].handle_utterance("read my notifications", "user")
    cap = (out.get("proposal") or {}).get("capability")
    assert cap == "android.notifications.read", out.get("proposal")
    props = ((out.get("proposal") or {}).get("parameters") or {})
    assert props == {}


# ---------- V. Contract-v3 semantic targets (gui.find / tap / set_text) ----------

def test_V1_gui_find_contract_read_only_observation():
    stack = make_stack()
    d = stack["registry"].get("gui.find")
    assert d.permission.value == "restricted"
    assert d.risk.value == "confirm"
    assert d.supported_devices == ["android"]
    assert d.required_capabilities == ["gui.find"]
    props = d.input_schema["properties"]
    assert set(props) <= {"target_package", "target",
                          "expected_package", "max_elements"}
    assert d.input_schema["required"] == ["target_package", "target"]
    sel = props["target"]["properties"]
    assert set(sel) == {"resource_id", "content_desc", "text",
                        "role", "index_hint"}
    # Read-only honesty in the contract text: find never acts; acting
    # keeps gui.tap / gui.set_text's own risk path.
    low = d.description.lower()
    assert "read-only" in low and "never taps" in low
    assert "gui.tap" in d.description


def test_V2_tap_set_text_expose_optional_target_exclusive_with_node_id():
    stack = make_stack()
    tap = stack["registry"].get("gui.tap")
    st = stack["registry"].get("gui.set_text")
    assert "target" in tap.input_schema["properties"]
    assert "target" in st.input_schema["properties"]
    # A semantic target removes the mandatory positional node_id.
    assert "node_id" not in st.input_schema["required"]
    # A selector-less target is accepted by the envelope (nested rules
    # are enforced fail-closed by the device), but unknown keys still are.
    ok = validate_against_schema(
        {"target_package": "dev.zara.zara_android",
         "target": {"resource_id": "chat_input"}},
        tap.input_schema)
    assert ok == []
    bad = validate_against_schema(
        {"target_package": "dev.zara.zara_android",
         "node_id": 3, "target": {"resource_id": "x"}},
        tap.input_schema)
    assert bad == []


def test_V3_tap_submit_with_target_parks_then_runs_resolved_node():
    stack = make_stack()
    _android(stack, caps=["gui.tap"])
    stack["fabric"].advertise([doc("gui.tap", risk="high_risk")], PHONE)
    rec = stack["engine"].submit(
        "gui.tap",
        {"target_package": "dev.zara.zara_android",
         "target": {"resource_id": "chat_input"}},
        PHONE, who="user")
    # RESTRICTED + HIGH_RISK: parks with zero dispatch, like positional.
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    assert stack["jobs"].poll(PHONE) is None
    final = approve_with_stub_device(stack, rec, PHONE, {
        "applied": True, "package": "dev.zara.zara_android",
        "snapshot_id": "b1b2b3b4:7", "verify_state": "clicked",
        "target_matched": {"node_id": 6, "resource_id": "chat_input",
                           "role": "android.widget.EditText"}})
    assert final.state == ExecutionState.SUCCEEDED, final.state
    assert final.result["target_matched"]["resource_id"] == "chat_input"
    assert final.result["verify_state"] == "clicked"
    blob = json.dumps(stack["audit"].query(200))
    assert "device_key" not in blob
