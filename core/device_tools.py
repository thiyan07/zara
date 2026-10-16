"""Device-backed tool contracts — declared in core, implemented on devices.

The core registry holds these definitions so routing/policy/governor work
uniformly. Their handler is a proxy: enqueue a DeviceJob for the target
device agent and wait for its result. Contract parity with each device
implementation (e.g. device/linux/tools_linux.py) is enforced by tests.
"""
from __future__ import annotations
from .models import PermissionLevel, RiskLevel, ToolDefinition


# Rung-1 pilot: exact lab packages the force-stop capability may target.
# Mirrored on-device (Android job runner refuses anything else even if a
# job arrives). No wildcards, no prefixes. New targets require a registry
# change + audit + fresh consent — never silent enrollment.
FORCE_STOP_LAB_TARGETS = frozenset({
    "dev.zara.lab.privtest",  # harmless lab probe APK, relaunchable
})


# Rung-2 pilot: exact lab packages the GUI capabilities may target.
# Mirrored on-device (Android job runner refuses anything else even if a
# job arrives). No wildcards, no prefixes. New targets require a registry
# change + audit + fresh consent — never silent enrollment.
GUI_LAB_TARGETS = frozenset({
    "dev.zara.zara_android",  # first-party lab app (Zara Android client)
    "dev.zara.lab.privtest",  # harmless lab probe APK, relaunchable
})

# App open targets: first-party packages that may be launched without
# additional user confirmation beyond the initial prompt. Third-party
# packages require explicit user confirmation via the system chooser.
APP_OPEN_TARGETS = frozenset({
    "dev.zara.zara_android",          # Zara Android client
    "dev.zara.lab.privtest",          # Lab probe APK
    "com.android.settings",           # System Settings
    "com.android.chrome",             # Chrome Browser
    "com.google.android.gm",          # Gmail
    "com.google.android.calendar",    # Google Calendar
})


def _def(name: str, desc: str, schema: dict, caps: list[str], cost: float,
         perm: PermissionLevel, risk: RiskLevel,
         timeout: float = 90.0,
         devices: list[str] | None = None) -> ToolDefinition:
    # Timing invariant: the proxy wait MUST exceed the worst-case device
    # poll gap (Android ticks every 30 s healthy, 60 s battery-throttled,
    # plus on-device execution + jitter). 20 s < 30 s expired healthy jobs
    # before the device ever polled (Rung-3 forensic report); 90 s gives a
    # healthy device >=2 poll opportunities and a throttled device >=1.
    # Longer waits hold ~2 threads per in-flight exec; offline devices fail
    # at this bound (never indefinite). Do NOT lower below 61 s without
    # also shortening the Android tick — see docs/research/JOB_TIMING_FIX.md.
    return ToolDefinition(name=name, description=desc, input_schema=schema,
                          output_schema={"type": "object"},
                          required_capabilities=caps, permission=perm, risk=risk,
                          estimated_cost=cost, timeout_s=timeout,
                          success_criteria="device returns structured result",
                          failure_behavior="fail", verification="none",
                          supported_devices=(list(devices) if devices else ["linux", "android"]),
                          reversible=True, version="2.0.0")


DEVICE_TOOL_DEFS: list[ToolDefinition] = [
    _def("system.battery", "Device battery state: one instantaneous reading "
         "(percent, charging flag, power source, health). Does NOT include "
         "time-to-full, and a single reading cannot estimate charge time — "
         "never promise or invent a recharge duration.",
         {"type": "object", "properties": {}}, ["system.battery"], 0.01,
         PermissionLevel.PUBLIC, RiskLevel.SAFE),
    _def("system.battery.charging_time", "Estimated time to full charge based on "
         "recent charging history. Returns null if unavailable or not charging. "
         "Uses recent current/power samples to estimate; honesty over precision.",
         {"type": "object", "properties": {}}, ["system.battery"], 0.01,
         PermissionLevel.PUBLIC, RiskLevel.SAFE),
    _def("system.network", "Device network state",
         {"type": "object", "properties": {}}, ["system.network"], 0.01,
         PermissionLevel.PUBLIC, RiskLevel.SAFE),
    _def("system.wifi.state", "WiFi state (enabled/disabled/connecting) and "
         "connected network SSID if available. Read-only, no state changes.",
         {"type": "object", "properties": {}}, ["system.wifi.state"], 0.01,
         PermissionLevel.PUBLIC, RiskLevel.SAFE),
    _def("system.wifi.settings", "Open the system WiFi settings screen. "
         "User must confirm; no automatic state change. Returns the launched activity.",
         {"type": "object", "properties": {}}, ["system.wifi.settings"], 0.01,
         PermissionLevel.CONFIRM, RiskLevel.CONFIRM),
    _def("system.bluetooth.state", "Bluetooth state (enabled/disabled/connecting) "
         "and connected device name if available. Read-only, no state changes.",
         {"type": "object", "properties": {}}, ["system.bluetooth.state"], 0.01,
         PermissionLevel.PUBLIC, RiskLevel.SAFE),
    _def("system.bluetooth.settings", "Open the system Bluetooth settings screen. "
         "User must confirm; no automatic state change. Returns the launched activity.",
         {"type": "object", "properties": {}}, ["system.bluetooth.settings"], 0.01,
         PermissionLevel.CONFIRM, RiskLevel.CONFIRM),
    _def("system.hotspot.state", "Mobile hotspot state (enabled/disabled) and "
         "connected client count if available. Read-only, no state changes.",
         {"type": "object", "properties": {}}, ["system.hotspot.state"], 0.01,
         PermissionLevel.PUBLIC, RiskLevel.SAFE),
    _def("system.hotspot.settings", "Open the system mobile hotspot settings screen. "
         "User must confirm; no automatic state change. Returns the launched activity.",
         {"type": "object", "properties": {}}, ["system.hotspot.settings"], 0.01,
         PermissionLevel.CONFIRM, RiskLevel.CONFIRM),
    _def("system.mobile_data.state", "Mobile data state (enabled/disabled) and "
         "network type if available. Read-only, no state changes.",
         {"type": "object", "properties": {}}, ["system.mobile_data.state"], 0.01,
         PermissionLevel.PUBLIC, RiskLevel.SAFE),
    _def("system.mobile_data.settings", "Open the system mobile data settings screen. "
         "User must confirm; no automatic state change. Returns the launched activity.",
         {"type": "object", "properties": {}}, ["system.mobile_data.settings"], 0.01,
         PermissionLevel.CONFIRM, RiskLevel.CONFIRM),
    _def("system.airplane_mode.state", "Airplane mode state (enabled/disabled). "
         "Read-only, no state changes.",
         {"type": "object", "properties": {}}, ["system.airplane_mode.state"], 0.01,
         PermissionLevel.PUBLIC, RiskLevel.SAFE),
    _def("system.airplane_mode.settings", "Open the system airplane mode settings screen. "
         "User must confirm; no automatic state change. Returns the launched activity.",
         {"type": "object", "properties": {}}, ["system.airplane_mode.settings"], 0.01,
         PermissionLevel.CONFIRM, RiskLevel.CONFIRM),
    _def("system.app.open", "Open a known application by package name. "
         "Only allowlisted packages may be launched. User confirmation required for "
         "third-party packages; first-party lab packages launch directly.",
         {"type": "object", "required": ["package"],
           "additionalProperties": False,
           "properties": {"package": {
               "type": "string", "minLength": 3, "maxLength": 255,
               "pattern": r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$",
               "enum": sorted(APP_OPEN_TARGETS)}}},
         ["system.app.open"], 0.05,
         PermissionLevel.CONFIRM, RiskLevel.CONFIRM),
    _def("system.wifi.set", "Change WiFi radio state. Direct change only "
         "where the deployment legitimately supports it (older platform or "
         "privileged tier); otherwise opens the WiFi settings screen for "
         "user confirmation and returns applied:false with a verify pointer. "
         "WARNING: disabling wifi on the executing device can sever its "
         "Core channel before the result is delivered.",
         {"type": "object", "required": ["enabled"],
           "additionalProperties": False,
           "properties": {"enabled": {"type": "boolean"}}},
         ["system.wifi.set"], 0.05,
         PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK),
    _def("system.bluetooth.set", "Change Bluetooth state. Enable uses the "
         "system consent dialog (user confirms in OS UI); disable attempts "
         "a direct call where permitted. Result verified by re-reading "
         "state; applied:true only on verified change.",
         {"type": "object", "required": ["enabled"],
           "additionalProperties": False,
           "properties": {"enabled": {"type": "boolean"}}},
         ["system.bluetooth.set"], 0.05,
         PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK),
    _def("system.hotspot.set", "Change mobile-hotspot state. Ordinary "
         "builds have no direct path (no public API): opens the tether "
         "settings screen and returns applied:false with a verify pointer. "
         "The lab privileged companion (T3) may apply directly; absence is "
         "deployment_blocked, never failure.",
         {"type": "object", "required": ["enabled"],
           "additionalProperties": False,
           "properties": {"enabled": {"type": "boolean"}}},
         ["system.hotspot.set"], 0.05,
         PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK),
    _def("system.mobile_data.set", "Change mobile-data state. No direct "
         "path for ordinary apps (system-only permission): opens the data "
         "settings screen and returns applied:false with a verify pointer.",
         {"type": "object", "required": ["enabled"],
           "additionalProperties": False,
           "properties": {"enabled": {"type": "boolean"}}},
         ["system.mobile_data.set"], 0.05,
         PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK),
    _def("system.airplane_mode.set", "Change airplane mode. Direct change "
         "only with the ADB-granted development permission (T1); otherwise "
         "opens settings and returns applied:false with a verify pointer. "
         "WARNING: enabling airplane mode on the executing device severs "
         "its Core channel; the approval card must name that hazard.",
         {"type": "object", "required": ["enabled"],
           "additionalProperties": False,
           "properties": {"enabled": {"type": "boolean"}}},
         ["system.airplane_mode.set"], 0.05,
         PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK),
    _def("desktop.session", "Graphical session type (x11/wayland/headless) "
         "from the environment. Pure read, Linux-only.",
         {"type": "object", "properties": {}}, ["desktop.session"], 0.01,
         PermissionLevel.PUBLIC, RiskLevel.SAFE, devices=["linux"]),
    _def("desktop.window.active", "Active-window title via X11 probe. "
         "Wayland honestly reports unsupported. Titles may be sensitive: "
         "treat as untrusted. Linux-only.",
         {"type": "object", "properties": {}}, ["desktop.window.active"],
         0.01, PermissionLevel.RESTRICTED, RiskLevel.SAFE,
         devices=["linux"]),
    _def("desktop.screenshot", "Bounded single screenshot to the scratch "
         "dir (path returned, never inline pixels). No backend = honest "
         "unsupported, never a blank image. Pixels are untrusted and may "
         "be sensitive. Linux-only.",
         {"type": "object", "properties": {}}, ["desktop.screenshot"],
         0.05, PermissionLevel.RESTRICTED, RiskLevel.CONFIRM,
         devices=["linux"]),
    _def("desktop.a11y.inspect", "Targeted AT-SPI2 UI-tree inspection "
         "(bounded nodes, snapshot id for follow-up actions). Read-only; "
         "names/labels are untrusted data. Linux-only.",
         {"type": "object",
          "properties": {"app": {"type": "string", "maxLength": 120},
                         "max_nodes": {"type": "integer"},
                         "include_text": {"type": "boolean"}}},
         ["desktop.a11y.inspect"], 0.05,
         PermissionLevel.RESTRICTED, RiskLevel.SAFE, devices=["linux"]),
    _def("desktop.click", "Click a snapshotted control: AT-SPI action "
         "first (any session), X11 pointer fallback on freshly re-read "
         "bounds. Stale/moved targets refuse. Verified by re-read. "
         "Linux-only.",
         {"type": "object", "required": ["snapshot_id", "node_id"],
          "additionalProperties": False,
          "properties": {"snapshot_id": {"type": "string"},
                         "node_id": {"type": "string"}}},
         ["desktop.click"], 0.1,
         PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK, devices=["linux"]),
    _def("desktop.type", "Type into a snapshotted control: AT-SPI edit "
         "first (content-verified), focused X11 typing fallback. Typed "
         "text is never echoed back. Wayland without an editable control "
         "refuses. Linux-only.",
         {"type": "object", "required": ["snapshot_id", "node_id", "text"],
          "additionalProperties": False,
          "properties": {"snapshot_id": {"type": "string"},
                         "node_id": {"type": "string"},
                         "text": {"type": "string", "minLength": 1,
                                  "maxLength": 500},
                         "mode": {"type": "string",
                                  "enum": ["replace", "insert"]}}},
         ["desktop.type"], 0.1,
         PermissionLevel.CONFIRM, RiskLevel.HIGH_RISK, devices=["linux"]),
    _def("system.processes.read", "Read-only process list",
         {"type": "object", "properties": {"limit": {"type": "integer"}}},
         ["process.read"], 0.05, PermissionLevel.RESTRICTED, RiskLevel.SAFE,
         devices=["linux"]),
    _def("filesystem.list", "List directory (device-scoped)",
         {"type": "object", "required": ["path"],
          "properties": {"path": {"type": "string"}}},
         ["filesystem.list"], 0.02, PermissionLevel.RESTRICTED, RiskLevel.SAFE,
         devices=["linux"]),
    _def("filesystem.read", "Read file (device-scoped, capped)",
         {"type": "object", "required": ["path"],
          "properties": {"path": {"type": "string"},
                         "max_bytes": {"type": "integer"}}},
         ["filesystem.read"], 0.03, PermissionLevel.RESTRICTED, RiskLevel.SAFE,
         devices=["linux"]),
    _def("filesystem.exists", "Check path existence (device-scoped)",
         {"type": "object", "required": ["path"],
          "properties": {"path": {"type": "string"}}},
         ["filesystem.read"], 0.01, PermissionLevel.RESTRICTED, RiskLevel.SAFE,
         devices=["linux"]),
    _def("filesystem.metadata", "File metadata (device-scoped)",
         {"type": "object", "required": ["path"],
          "properties": {"path": {"type": "string"}}},
         ["filesystem.read"], 0.01, PermissionLevel.RESTRICTED, RiskLevel.SAFE,
         devices=["linux"]),
    _def("filesystem.write", "Write text file (device scratch dir)",
         {"type": "object", "required": ["name", "text"],
          "properties": {"name": {"type": "string"},
                         "text": {"type": "string"}}},
         ["filesystem.write"], 0.05,
         PermissionLevel.CONFIRM, RiskLevel.CONFIRM,
         devices=["linux"]),
    _def("filesystem.delete", "Delete device scratch-dir file",
         {"type": "object", "required": ["name"],
          "properties": {"name": {"type": "string"}}},
         ["filesystem.write"], 0.02,
         PermissionLevel.CONFIRM, RiskLevel.CONFIRM,
         devices=["linux"]),
    _def("shell.safe_readonly", "Allowlisted read-only commands on device",
         {"type": "object", "required": ["command"],
          "properties": {"command": {"type": "string"}}},
         ["terminal.safe"], 0.05, PermissionLevel.CONFIRM, RiskLevel.CONFIRM,
         devices=["linux"]),
    _def("android.app.force_stop",
         "Force-stop one lab-allowlisted package (priv-app only, "
         "emulator lab). Destructive: kills process immediately.",
         {"type": "object", "required": ["target_package"],
          "additionalProperties": False,
          "properties": {"target_package": {
              "type": "string", "minLength": 3, "maxLength": 255,
              "pattern": r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$",
              "enum": sorted(FORCE_STOP_LAB_TARGETS)}}},
         ["android.app.force_stop"], 0.05,
         PermissionLevel.RESTRICTED, RiskLevel.HIGH_RISK,
         devices=["android"]),
    _def("gui.screen.inspect",
         "Inspect the foreground screen (bounded accessibility snapshot). "
         "Read-only: returns element text/bounds, never acts. "
         "Supports expected_package filter, staleness check, and field selection.",
         {"type": "object",
           "additionalProperties": False,
           "properties": {
               "expected_package": {
                   "type": "string",
                   "description": "Regex pattern for expected foreground package name"
               },
               "expected_snapshot_id": {
                   "type": "string",
                   "description": "Previous snapshot ID for staleness detection"
               },
               "max_elements": {
                   "type": "integer",
                   "minimum": 1,
                   "maximum": 50,
                   "description": "Maximum elements to return (1-50)"
               },
               "include_text": {
                   "type": "boolean",
                   "description": "Include element text field"
               },
               "include_content_description": {
                   "type": "boolean",
                   "description": "Include element content description field"
               }
           }},
         ["gui.screen.inspect"], 0.05,
         PermissionLevel.RESTRICTED, RiskLevel.CONFIRM,
         devices=["android"]),
    _def("gui.tap",
         "Tap a node inside one lab-allowlisted package (foreground, "
         "user-approved only). Acts on-device: changes UI state.",
         {"type": "object", "required": ["target_package"],
          "additionalProperties": False,
          "properties": {"target_package": {
              "type": "string", "minLength": 3, "maxLength": 255,
              "pattern": r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$",
              "enum": sorted(GUI_LAB_TARGETS)},
              "node_id": {"type": "string", "maxLength": 128}}},
         ["gui.tap"], 0.05,
         PermissionLevel.RESTRICTED, RiskLevel.HIGH_RISK,
         devices=["android"]),
]


def make_proxy_handler(queue, timeout_s: float):
    """Generic device proxy: dispatch job, block (server Condition) for result."""
    def handler(inputs: dict, ctx: dict) -> dict:
        device_id = (ctx or {}).get("device_id", "")
        execution_id = (ctx or {}).get("execution_id")
        if not device_id:
            raise ValueError("device proxy requires a target device_id")
        job = queue.enqueue(device_id, handler.tool_name, dict(inputs),
                            execution_id=execution_id, timeout_s=timeout_s)
        # Close the cancel-vs-dispatch race: engine.cancel sets
        # cancel_requested then sweeps the queue with cancel_by_execution.
        # If the sweep ran in the window between _run's loop-top check and
        # this enqueue, the fresh job would otherwise orphan (waiter blocks
        # to timeout_s despite the cancel). Re-check here: cancel the
        # just-enqueued job and fail fast instead. A cancel landing after
        # this check is caught by cancel_by_execution itself (the job is
        # already enqueued), which wakes the waiter below.
        cancelled = (ctx or {}).get("cancel_requested")
        try:
            want_cancel = bool(cancelled()) if callable(cancelled) else False
        except Exception:  # noqa: BLE001 — predicate failure fails open
            want_cancel = False
        if want_cancel:
            try:
                queue.cancel(job.id)
            except Exception:  # noqa: BLE001 — cancel is best-effort here
                pass
            raise InterruptedError(f"job {job.id} cancelled before wait")
        done = queue.wait_for_result(job.id, timeout=timeout_s)
        if done.state == "cancelled":
            raise InterruptedError(f"job {job.id} cancelled")
        if done.state != "done":
            raise RuntimeError(
                f"device job {done.state}: {(done.error or '')[:300]}")
        if not isinstance(done.result, dict):
            raise ValueError("device returned malformed result")
        return done.result
    return handler


def register_device_proxies(registry, queue) -> None:
    for definition in DEVICE_TOOL_DEFS:
        handler = make_proxy_handler(queue, definition.timeout_s)
        handler.tool_name = definition.name
        registry.register(definition, handler)
