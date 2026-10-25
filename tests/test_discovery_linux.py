"""Fixture-driven tests for the Linux discovery adapters."""
from __future__ import annotations

import json

import device.linux.discovery_linux as dl


def test_xrandr_parse():
    xrandr = ("Screen 0: minimum 320 x 200, current 3286 x 1080, "
              "preferred 3286 x 1080\n"
              "eDP-1 connected primary 1920x1080+0+0 (normal left)\n"
              "HDMI-A-1 connected 1366x768+1920+0\n"
              "DP-2 disconnected\n")
    dl._run = lambda a, timeout=6.0: xrandr
    items = dl._displays("x11")
    assert {i["identity"]["durable"] for i in items} == {
        "xrandr-eDP-1", "xrandr-HDMI-A-1", "xrandr-DP-2"}
    hdmi = [i for i in items if i["identity"]["durable"] == "xrandr-HDMI-A-1"][0]
    assert hdmi["capabilities"]["width"] == 1366
    assert hdmi["capabilities"]["geometry_offset"] == "+1920+0"
    dp = [i for i in items if i["identity"]["durable"] == "xrandr-DP-2"][0]
    assert "disconnected" in dp["label"] and dp["capabilities"] == {}


def test_lsblk_removable_with_mounts():
    lsblk = ('{"blockdevices":[{"name":"nvme0n1","type":"disk",'
             '"rm":false,"ro":false,"size":500107862016,"fstype":"","'
             'mountpoint":null,"children":[{"name":"nvme0n1p2","type":'
             '"part","mountpoint":"/","fstype":"ext4"}]},'
             '{"name":"sdb","type":"disk","rm":true,"ro":false,'
             '"size":64023257088,"fstype":"vfat","serial":"ABC123",'
             '"mountpoint":"/media/thiyan/USB","model":"Cruzer Glide"}]}')
    dl._run = lambda a, timeout=6.0: lsblk
    items = dl._block_items()
    assert len(items) == 1  # internal disk is NOT a peripheral
    it = items[0]
    assert it["device_class"] == "usb_storage"
    assert it["identity"]["durable"] == "ABC123"
    assert it["capabilities"]["size_bytes"] == 64023257088
    assert it["capabilities"]["write"] is True
    assert it["mounts"][0]["mountpoint"] == "/media/thiyan/USB"
    assert "Cruzer" in it["label"]


def test_lsblk_absent_is_honest():
    dl._run = lambda a, timeout=6.0: None
    assert dl._block_items() == []
    assert dl._lsusb_items() == []


def test_adb_states():
    dl._run = lambda a, timeout=5.0: (""  # noqa
        "List of devices attached\n"
        "abc123\tunauthorized usb:1-2\n"
        "def456\tdevice usb:1-3 product:racer model:Pixel_7A\n")
    items = dl._adb_phones()
    assert [i["capabilities"].get("authorized") for i in items] == \
        [False, True]
    assert items[1]["label"].endswith("Pixel_7A")
    assert items[0]["identity"]["durable"] == "adb-abc123"


def test_mutter_json_parse():
    flags = {"type": "b", "data": True}
    mutter = json.dumps({
        "type": "ua",
        "data": [1,
                 [[["eDP-1", "CMN", "0x1521", "0x0"],
                   [["1920x1080@144.003", 1920, 1080, 144.003, 1.0,
                     [1.0], {"is-preferred": flags}],
                    ["1920x1080@60.004", 1920, 1080, 60.004, 1.0,
                     [1.0], {"is-current": flags}]],
                   "geom", {}]],
                 "sz", {}]})
    dl._run_env = lambda a, timeout=6.0: mutter
    items = dl._displays("wayland")
    assert len(items) == 1
    cap = items[0]["capabilities"]
    assert cap["width"] == 1920 and cap["height"] == 1080
    assert cap["current_mode"] == "1920x1080@60.004"
    assert cap["session"] == "wayland"


def test_audio_both_directions():
    dl._run = lambda a, timeout=6.0: (
        "card 1: Generic [HD-Audio Generic], device 0: ALC256 Analog\n")
    items = dl._audio_cards()
    assert len(items) == 2
    assert {i["capabilities"] and list(i["capabilities"])[0] for i in items} \
        == {"output", "input"}
