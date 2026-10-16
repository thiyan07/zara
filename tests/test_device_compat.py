"""Capability-to-device compatibility: Core metadata must match reality.

Table-driven: every Core device-tool definition declares supported_devices
that agree with actual implementations (Linux registry here; Android lab
namespaces by convention). Prevents silent overclaiming where Core would
route to a device that cannot execute.
"""
import device.linux.tools_linux as LT
from core.device_tools import DEVICE_TOOL_DEFS

VALID_DEVICES = {"linux", "android"}


def _linux_names():
    return {d.name for d, _ in LT.LINUX_TOOLS}


def test_devices_declared_valid_and_nonempty():
    for d in DEVICE_TOOL_DEFS:
        assert set(d.supported_devices) <= VALID_DEVICES, d.name
        assert d.supported_devices, d.name


def test_linux_registry_matches_linux_claim():
    linux = _linux_names()
    for d in DEVICE_TOOL_DEFS:
        if "linux" in d.supported_devices:
            assert d.name in linux, f"claims linux, not implemented: {d.name}"
        elif not d.name.startswith(("android.", "gui.")):
            assert d.name not in linux, \
                f"implemented but not claimed: {d.name}"


def test_android_lab_namespaces_never_on_linux():
    linux = _linux_names()
    for d in DEVICE_TOOL_DEFS:
        if d.name.startswith(("android.", "gui.")):
            assert d.name not in linux, d.name
            assert d.supported_devices == ["android"], d.name


def test_linux_only_tools_not_advertised_for_android():
    defs = {d.name: d for d in DEVICE_TOOL_DEFS}
    for name in ("system.processes.read", "filesystem.list",
                 "filesystem.read", "filesystem.exists",
                 "filesystem.metadata", "filesystem.write",
                 "filesystem.delete", "shell.safe_readonly",
                 "desktop.session", "desktop.window.active",
                 "desktop.screenshot", "desktop.a11y.inspect",
                 "desktop.click", "desktop.type"):
        assert defs[name].supported_devices == ["linux"], name


def test_shared_system_tools_claim_both():
    defs = {d.name: d for d in DEVICE_TOOL_DEFS}
    for name in ("system.battery", "system.network", "system.wifi.state",
                 "system.wifi.set", "system.bluetooth.state",
                 "system.app.open", "system.airplane_mode.set"):
        assert set(defs[name].supported_devices) == {"linux", "android"}, \
            name
