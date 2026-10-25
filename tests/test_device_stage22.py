"""Stage 22 — bluetooth / audio-routing / MTP-GVFS / display-touch /
stream-availability tests. All actions run against mocked runners;
live read-only smokes are marked and never mutate state. Nothing here
is physical-hardware evidence — the real phone/pendrive/display
validations remain explicitly pending."""
from __future__ import annotations

import pytest

from device.linux import audio_linux as AL
from device.linux import display_linux as dl
import device.linux.discovery_linux as D
from device.linux.tools_linux import (audio_sinks, audio_set_output,
                                      bluetooth_connect,
                                      bluetooth_devices,
                                      display_stream_status)
from core.discovery import PeripheralRegistry, stable_id

# ------------------------------------------------------------------ fakes

_BT_DEVICES = "Device E4:57:68:DC:CD:48 vivo T3x 5G\n"

_BT_INFO = """Device E4:57:68:DC:CD:48 (public)
	Name: vivo T3x 5G
	Paired: yes
	Bonded: yes
	Trusted: yes
	Blocked: no
	Connected: no
	UUID: Audio Source              (0000110a-0000-1000-8000-00805f9b34fb)
	UUID: OBEX Object Push          (00001105-0000-1000-8000-00805f9b34fb)
	UUID: Vendor specific           (2c042b0a-7f57-4c0a-afcf-1762af70257c)
"""

_BT_INFO_CONNECTED = _BT_INFO.replace("Connected: no", "Connected: yes")

_GIO_FIXTURE = """Drive(0): SAMSUNG MZVL8512HELU-00BTW
  Type: GProxyDrive (GProxyVolumeMonitorUDisks2)
Mount(2): vivo T3x 5G
  Type: GDaemonMount (GProxyVolumeMonitorMTP)
  Id: mtp%3A%2F%2F
  Default location: mtp://[usb:003,004]
Mount(3): Nikon DSC
  Type: GProxyVolume (GProxyVolumeMonitorPTP)
  Default location: gphoto2://[usb:003,005]/
Mount(4): System (/)
  Type: GUnixMount (GUnixVolume)
  Id: unix-run-media-run-OS
"""

_WPCTL = """PipeWire 'pipewire-0' [1.6.2, t@tuf]
 └─ Clients: hidden

Audio
 ├─ Devices:
 │      53. Ryzen HD Audio Controller           [alsa]
 │
 ├─ Sinks:
 │  *   57. Ryzen HD Audio Controller Analog Stereo [vol: 0.63]
 │      71. HDMI Audio Output              [alsa]
 │
 ├─ Sources:
 │  *   58. Ryzen HD Audio Analog [vol: 0.59]
 │
 ├─ Filters:
 │
 └─ Streams:
"""

_WPCTL_AFTER_SET = _WPCTL.replace(
    " │  *   57. Ryzen HD Audio Controller Analog Stereo [vol: 0.63]",
    " │      57. Ryzen HD Audio Controller Analog Stereo [vol: 0.63]"
).replace(
    " │      71. HDMI Audio Output              [alsa]",
    " │  *   71. HDMI Audio Output              [alsa]")

_TOUCH = 'N: Name="HDMI touchscreen"\nN: Name="ELAN Touchpad"\n'


@pytest.fixture
def discovery_env(monkeypatch):
    """Scripted _run_env for discovery adapters: key = argv[1]."""
    calls = []

    def install(tbl: dict):
        def fake(argv, timeout=6.0):
            calls.append(list(argv))
            key = argv[1] if len(argv) > 1 else ""
            out = tbl.get(key, tbl.get("__rest__"))
            return out  # None = command refused/absent
        monkeypatch.setattr(D, "_run_env", fake)
        return calls

    return install


# --------------------------------------------------- bluetooth discovery

def test_bluetooth_items_parse_services_honestly(monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda n: "/usr/bin/x")

    def fake(argv, timeout=6.0):
        if argv[1:] == ["devices"]:
            return _BT_DEVICES
        if argv[1:] == ["info", "E4:57:68:DC:CD:48"]:
            return _BT_INFO
        return None
    monkeypatch.setattr(D, "_run_env", fake)
    items, meta = D._bluetooth_items()
    assert meta == "ok" and len(items) == 1
    caps = items[0]["capabilities"]
    assert caps["paired"] is True and caps["connected"] is False
    assert "audio-source (device can stream audio out)" in \
        caps["advertised_services"]
    assert "obex-object-push (file push protocol)" in \
        caps["advertised_services"]
    assert caps["unidentified_service_uuids"] == 1
    assert caps["service_use_inferred_only"] is True
    assert items[0]["identity"]["durable"].startswith("bt-E4:")


def test_bluetooth_absent_is_honest(monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda n: None)
    items, meta = D._bluetooth_items()
    assert items == [] and "absent" in meta


def test_registry_accepts_bluetooth_class():
    reg = PeripheralRegistry()
    item = {"device_class": "bluetooth",
            "label": "Bluetooth: vivo T3x 5G",
            "identity": {"durable": "bt-E4:57:68:DC:CD:48"},
            "connection": "bluetooth"}
    diff = reg.update_host_snapshot("laptop", [item])
    assert diff["connected"] == [stable_id(
        "bluetooth", {"durable": "bt-E4:57:68:DC:CD:48"})]

# ----------------------------------------------------------- MTP / PTP


def test_gio_mtp_and_ptp_classified_from_os_ids(monkeypatch):
    monkeypatch.setattr(D, "_run_env", lambda argv, timeout=8.0:
                        _GIO_FIXTURE if argv[1:] == ["mount", "-li"]
                        else None)
    monkeypatch.setattr(D.shutil, "which", lambda n: "/usr/bin/gio")
    items, meta = D._gio_mtp_items()
    assert meta == "ok" and len(items) == 2
    protos = sorted(i["capabilities"]["protocol_confirmed"]
                    for i in items)
    assert protos == ["mtp", "ptp"]
    # plain UDisks mounts are NOT misclassified
    assert not any("System" in i["label"] for i in items)
    assert items[0]["capabilities"]["io_backend"].startswith("gvfs")


def test_gio_mtp_absent_is_not_an_error(monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda n: None)
    items, meta = D._gio_mtp_items()
    assert items == [] and "absent" in meta


def test_network_meta_never_port_scans(monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda n: None)
    assert "deliberately does not port-scan" in D._network_services_meta()

# your --------------------------- bluetooth connect (handler)


def test_connect_requires_confirm(monkeypatch):
    monkeypatch.setattr(__import__(
        "device.linux.tools_linux", fromlist=["x"]).shutil, "which",
        lambda n: "/usr/bin/x")
    out = bluetooth_connect({"mac": "E4:57:68:DC:CD:48"}, {})
    assert out["ok"] is False and "confirm=true" in out["error"]


def test_connect_validates_mac_and_refuses_unknown(monkeypatch):
    import device.linux.tools_linux as T
    monkeypatch.setattr(T.shutil, "which", lambda n: "/usr/bin/x")
    assert bluetooth_connect({"mac": "nope", "confirm": True},
                             {})["nothing_changed"] is True
    seen = []

    def hook(argv):
        seen.append(argv)
        return "Device not available"   # unknown device info output
    T._BT_HOOK = hook
    try:
        out = bluetooth_connect(
            {"mac": "E4:57:68:DC:CD:48", "confirm": True}, {})
    finally:
        T._BT_HOOK = None
    assert out["ok"] is False and "not a known" in out["error"]


def test_connect_refuses_unpaired_and_blocked(monkeypatch):
    import device.linux.tools_linux as T
    monkeypatch.setattr(T.shutil, "which", lambda n: "/usr/bin/x")
    unpaired = _BT_INFO.replace("Paired: yes", "Paired: no")
    T._BT_HOOK = lambda argv: unpaired
    out = bluetooth_connect({"mac": "E4:57:68:DC:CD:48",
                             "confirm": True}, {})
    assert "not paired" in out["error"] and \
        out.get("nothing_changed") is True
    blocked = _BT_INFO.replace("Blocked: no", "Blocked: yes")
    T._BT_HOOK = lambda argv: blocked
    out2 = bluetooth_connect({"mac": "E4:57:68:DC:CD:48",
                              "confirm": True}, {})
    assert "blocked" in out2["error"]


def test_connect_already_connected_is_idempotent(monkeypatch):
    import device.linux.tools_linux as T
    monkeypatch.setattr(T.shutil, "which", lambda n: "/usr/bin/x")
    connect_calls = []

    def hook(argv):
        if argv[1] == "connect":
            connect_calls.append(argv)
        return _BT_INFO_CONNECTED
    T._BT_HOOK = hook
    out = bluetooth_connect({"mac": "E4:57:68:DC:CD:48",
                             "confirm": True}, {})
    assert out["ok"] is True and out["verified"] is True
    assert not connect_calls   # honest no-op, nothing to do


def test_connect_verified_by_re_read(monkeypatch):
    import device.linux.tools_linux as T
    monkeypatch.setattr(T.shutil, "which", lambda n: "/usr/bin/x")
    state = {"connected": False}

    def hook(argv):
        if argv[1] == "connect":
            state["connected"] = True
        return _BT_INFO_CONNECTED if state["connected"] else _BT_INFO
    T._BT_HOOK = hook
    out = bluetooth_connect({"mac": "E4:57:68:DC:CD:48", "confirm":
                             True}, {})
    assert out["ok"] is True and out["verified"] is True


def test_connect_did_not_verify_reports_honestly(monkeypatch):
    import device.linux.tools_linux as T
    monkeypatch.setattr(T.shutil, "which", lambda n: "/usr/bin/x")
    T._BT_HOOK = lambda argv: _BT_INFO    # never becomes connected
    out = bluetooth_connect({"mac": "E4:57:68:DC:CD:48",
                             "confirm": True}, {})
    assert out["ok"] is False and out["verified"] is False
    assert "no success claim" in out["detail"]

# -------------------------------------------------------- audio routing


def _fake_audio(monkeypatch, status_txt, set_default=None):
    calls = []

    def fake(argv, timeout=6.0):
        calls.append(list(argv))
        if argv[1] == "status":
            return status_txt
        if argv[1] == "set-default":
            if set_default is None:
                return ""
            return set_default
        return None
    monkeypatch.setattr(AL, "_RUN", fake)
    return calls


def test_audio_status_parses_sinks_and_default(monkeypatch):
    _fake_audio(monkeypatch, _WPCTL)
    st = audio_sinks({}, {})
    assert st["available"] is True
    assert [s["id"] for s in st["sinks"]] == [57, 71]
    assert st["default_sink"]["id"] == 57
    assert st["devices"] == [{"id": 53,
                              "name": "Ryzen HD Audio Controller",
                              "default": False, "backend": "alsa"}]


def test_audio_set_output_refusals(monkeypatch):
    _fake_audio(monkeypatch, _WPCTL)
    out = audio_set_output({"sink_id": 71}, {})          # no confirm
    assert out["ok"] is False and "confirm=true" in out["error"]
    out2 = audio_set_output({"sink_id": 9999, "confirm": True}, {})
    assert out2["ok"] is False and "unknown sink id" in out2["error"]
    assert out2.get("nothing_changed") is True


def test_audio_set_output_verified(monkeypatch):
    calls = []

    def with_set(argv, timeout=6.0):
        calls.append(list(argv))
        if argv[1] == "set-default":
            return ""
        return _WPCTL_AFTER_SET if len(calls) >= 2 else _WPCTL
    monkeypatch.setattr(AL, "_RUN", with_set)
    out = audio_set_output({"sink_id": 71, "confirm": True}, {})
    assert out["ok"] is True and out["verified"] is True
    assert out["before"] == 57 and out["after"] == 71
    assert ["wpctl", "set-default", "71"] == calls[1]


def test_audio_set_output_verify_failure_honest(monkeypatch):
    calls = []

    def fake(argv, timeout=6.0):
        calls.append(argv[1])
        if argv[1] == "set-default":
            return ""           # command "succeeds"...
        return _WPCTL           # ...but default marker never moves
    monkeypatch.setattr(AL, "_RUN", fake)
    out = audio_set_output({"sink_id": 71, "confirm": True}, {})
    assert out["ok"] is False and out["verified"] is False
    assert "no success claim" in out["detail"]


def test_audio_set_default_refused_by_backend(monkeypatch):
    def fake(argv, timeout=6.0):
        if argv[1] == "set-default":
            return None         # backend refused
        return _WPCTL
    monkeypatch.setattr(AL, "_RUN", fake)
    out = audio_set_output({"sink_id": 71, "confirm": True}, {})
    assert out["ok"] is False and "routing unchanged" in out["error"]

# ------------------------------------------------- display touch/stream


def test_describe_includes_touch_and_streaming(monkeypatch):
    calls = []

    def fake(argv, timeout=8.0):
        calls.append(argv)
        return _XR_EXT_QUICK
    monkeypatch.setattr(dl, "_RUN", fake)
    monkeypatch.setattr(dl, "_INPUT_DEVICES", "/dev/null-fixture")
    monkeypatch.setattr("builtins.open", _fake_open(_TOUCH))
    monkeypatch.setattr(dl, "_NDISCOVER", lambda **kw: {"available": True,
                                                        "advertisements":
                                                        [{"instance": "TV"}]})
    desc = dl.describe_config()
    assert desc["touch"]["detectable"] is True
    assert "HDMI touchscreen" in desc["touch"]["touchscreens"]
    # a touchpad is never claimed as display touch
    assert desc["touch"]["touchpads"] == ["ELAN Touchpad"]
    assert desc["touch"]["external_touch_confirmed"] is False
    assert desc["streaming"]["supported_now"] is False
    assert desc["streaming"]["known_receivers_reported"] == 1


_XR_EXT_QUICK = """Screen 0: 1920x1080
eDP-1 connected primary 1920x1080+0+0
"""


def _fake_open(text):
    import io
    def opener(path, *a, **k):
        return io.StringIO(text)
    return opener


def test_stream_status_reports_unavailable_with_reason(monkeypatch):
    monkeypatch.setattr(dl, "_NDISCOVER", lambda **kw:
                        {"available": True, "advertisements": []})
    calls = []

    def fake(argv, timeout=8.0):
        calls.append(argv)
        if argv[:2] == ["busctl", "--user"]:
            return ("org.gnome.Mutter.ScreenCast   :1.24   gnome-shell\n"
                    "org.freedesktop.portal.Desktop :1.25  xdg-desktop")
        return None
    monkeypatch.setattr(dl, "_RUN", fake)
    st = display_stream_status({}, {})
    assert st["mutter_screencast_interface"] is True
    assert st["portal_bus_present"] is True
    assert st["supported_now"] is False
    assert "consent" in st["limitation"]

    monkeypatch.setattr(dl, "_RUN", lambda argv, timeout=8.0: None)
    st2 = display_stream_status({}, {})
    assert st2["mutter_screencast_interface"] is False
    assert st2["portal_bus_present"] is False
    assert st2["supported_now"] is False
    assert "present: no" in st2["limitation"]

# -------------------------------------------------------- NL routing


def test_intent_routes_stage22_phrases():
    from core.intent import LocalIntentParser, ParserContext
    ctx = ParserContext(requester_device="d1", devices=[],
                        known_capabilities={
                            "bluetooth.devices", "audio.sinks",
                            "display.stream_status",
                            "display.preview_config", "device.list"})
    p = LocalIntentParser()
    assert p.parse("list bluetooth devices", ctx).capability == \
        "bluetooth.devices"
    assert p.parse("audio output devices", ctx).capability == \
        "audio.sinks"
    assert p.parse("can you mirror my screen", ctx).capability == \
        "display.stream_status"


def test_discovery_snapshot_runs_new_adapters(monkeypatch):
    from device.linux.discovery_linux import discover_snapshot
    # make adapter binaries absent so the test never touches live state
    monkeypatch.setattr(D.shutil, "which", lambda n: None)
    monkeypatch.setattr(D, "ADB", "/nonexistent/adb")
    monkeypatch.setattr(D, "_run", lambda argv, timeout=6.0: None)
    monkeypatch.setattr(D, "_run_env", lambda argv, timeout=6.0: None)
    items, meta = discover_snapshot("wayland")
    assert "bluetooth" in meta["adapters"]
    assert "mtp" in meta["adapters"]
    assert "network_services" in meta["adapters"]
    # no adapter absence becomes an error entry — only honest meta
    assert all(i.get("device_class") for i in items)
