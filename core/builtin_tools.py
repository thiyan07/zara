"""Safe built-in tools. Small allowlisted set; everything else arrives in Stage 2/3."""
from __future__ import annotations
import platform
from .models import PermissionLevel, RiskLevel, ToolDefinition

def system_info(inputs: dict, ctx: dict) -> dict:
    return {"platform": platform.platform(), "python": platform.python_version()}

def echo(inputs: dict, ctx: dict) -> dict:
    return {"text": inputs.get("text", "")}

BUILTINS: list[tuple[ToolDefinition, object]] = [
    (ToolDefinition(
        name="system.info", description="Read-only system information",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "required": ["platform"],
                       "properties": {"platform": {"type": "string"}}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
        estimated_cost=0.01, timeout_s=5.0, success_criteria="returns platform string",
        failure_behavior="fail", verification="output_schema",
        supported_devices=["any"], reversible=True, version="1.0.0"), system_info),
    (ToolDefinition(
        name="util.echo", description="Echo text back (connectivity/dev test)",
        input_schema={"type": "object", "required": ["text"],
                      "properties": {"text": {"type": "string"}}},
        output_schema={"type": "object", "required": ["text"],
                       "properties": {"text": {"type": "string"}}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
        estimated_cost=0.0, timeout_s=5.0, success_criteria="echoes input",
        failure_behavior="fail", verification="output_schema",
        supported_devices=["any"], reversible=True, version="1.0.0"), echo),
]
