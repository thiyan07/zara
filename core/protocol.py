"""Device protocol — schema-validated messages between Zara Core and agents.

Core is authoritative: device-provided parameters are validated here and
again by policy/execution before anything runs.
"""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field

MessageType = Literal[
    "device_register", "device_auth", "heartbeat", "capability_update",
    "execution_request", "execution_started", "execution_progress",
    "execution_result", "execution_failed", "execution_cancel",
    "device_event", "device_disconnect",
]

PROTOCOL_VERSION = "2.0"


class DeviceMessage(BaseModel):
    v: str = PROTOCOL_VERSION
    type: MessageType
    device_id: str = Field(min_length=1, max_length=128)
    payload: dict = Field(default_factory=dict)


class HeartbeatPayload(BaseModel):
    battery_pct: float | None = Field(default=None, ge=0, le=100)
    charging: bool = False
    network: str = "unknown"
    online: bool = True


class CapabilityUpdate(BaseModel):
    capabilities: list[str]
    removed: list[str] = Field(default_factory=list)


class ExecutionRequest(BaseModel):
    job_id: str
    tool: str
    inputs: dict = Field(default_factory=dict)
    mission_id: str | None = None
    timeout_s: float = Field(default=30.0, gt=0, le=600)


class ExecutionResult(BaseModel):
    job_id: str
    ok: bool
    result: dict = Field(default_factory=dict)
    error: str = ""


def parse_message(raw: dict) -> DeviceMessage:
    """Strict parse: unknown types and bad shapes raise ValidationError."""
    msg = DeviceMessage(**raw)
    if msg.v != PROTOCOL_VERSION:
        raise ValueError(f"unsupported protocol version: {msg.v}")
    return msg
