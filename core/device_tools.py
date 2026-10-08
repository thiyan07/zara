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


def _def(name: str, desc: str, schema: dict, caps: list[str], cost: float,
         perm: PermissionLevel, risk: RiskLevel,
         timeout: float = 20.0) -> ToolDefinition:
    return ToolDefinition(name=name, description=desc, input_schema=schema,
                          output_schema={"type": "object"},
                          required_capabilities=caps, permission=perm, risk=risk,
                          estimated_cost=cost, timeout_s=timeout,
                          success_criteria="device returns structured result",
                          failure_behavior="fail", verification="none",
                          supported_devices=["linux", "android"],
                          reversible=True, version="2.0.0")


DEVICE_TOOL_DEFS: list[ToolDefinition] = [
    _def("system.battery", "Device battery state",
         {"type": "object", "properties": {}}, ["system.battery"], 0.01,
         PermissionLevel.PUBLIC, RiskLevel.SAFE),
    _def("system.network", "Device network state",
         {"type": "object", "properties": {}}, ["system.network"], 0.01,
         PermissionLevel.PUBLIC, RiskLevel.SAFE),
    _def("system.processes.read", "Read-only process list",
         {"type": "object", "properties": {"limit": {"type": "integer"}}},
         ["process.read"], 0.05, PermissionLevel.RESTRICTED, RiskLevel.SAFE),
    _def("filesystem.list", "List directory (device-scoped)",
         {"type": "object", "required": ["path"],
          "properties": {"path": {"type": "string"}}},
         ["filesystem.list"], 0.02, PermissionLevel.RESTRICTED, RiskLevel.SAFE),
    _def("filesystem.read", "Read file (device-scoped, capped)",
         {"type": "object", "required": ["path"],
          "properties": {"path": {"type": "string"},
                         "max_bytes": {"type": "integer"}}},
         ["filesystem.read"], 0.03, PermissionLevel.RESTRICTED, RiskLevel.SAFE),
    _def("filesystem.exists", "Check path existence (device-scoped)",
         {"type": "object", "required": ["path"],
          "properties": {"path": {"type": "string"}}},
         ["filesystem.read"], 0.01, PermissionLevel.RESTRICTED, RiskLevel.SAFE),
    _def("filesystem.metadata", "File metadata (device-scoped)",
         {"type": "object", "required": ["path"],
          "properties": {"path": {"type": "string"}}},
         ["filesystem.read"], 0.01, PermissionLevel.RESTRICTED, RiskLevel.SAFE),
    _def("shell.safe_readonly", "Allowlisted read-only commands on device",
         {"type": "object", "required": ["command"],
          "properties": {"command": {"type": "string"}}},
         ["terminal.safe"], 0.05, PermissionLevel.CONFIRM, RiskLevel.CONFIRM),
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
         PermissionLevel.RESTRICTED, RiskLevel.HIGH_RISK),
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
         PermissionLevel.RESTRICTED, RiskLevel.CONFIRM),
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
         PermissionLevel.RESTRICTED, RiskLevel.HIGH_RISK),
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
