"""Device-backed tool contracts — declared in core, implemented on devices.

The core registry holds these definitions so routing/policy/governor work
uniformly. Their handler is a proxy: enqueue a DeviceJob for the target
device agent and wait for its result. Contract parity with each device
implementation (e.g. device/linux/tools_linux.py) is enforced by tests.
"""
from __future__ import annotations
from .models import PermissionLevel, RiskLevel, ToolDefinition


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
