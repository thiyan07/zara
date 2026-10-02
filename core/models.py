"""Shared state models — the state/context contract. All engines use these."""
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class RiskLevel(str, Enum):
    SAFE = "safe"
    CONFIRM = "confirm"
    HIGH_RISK = "high_risk"

class PermissionLevel(str, Enum):
    PUBLIC = "public"
    RESTRICTED = "restricted"
    CONFIRM = "confirm"
    HIGH_RISK = "high_risk"

class DeviceKind(str, Enum):
    ANDROID = "android"
    LINUX = "linux"
    CLOUD = "cloud"

class ExecutionState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_PERMISSION = "waiting_for_permission"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    DENIED = "denied"

class MissionState(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    WAITING_FOR_PERMISSION = "waiting_for_permission"
    EXECUTING = "executing"
    WAITING = "waiting"
    RETRYING = "retrying"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

TERMINAL_MISSIONS = {MissionState.COMPLETED, MissionState.FAILED, MissionState.CANCELLED}

class ToolDefinition(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.:\-]{1,63}$")
    description: str
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list)
    permission: PermissionLevel = PermissionLevel.RESTRICTED
    risk: RiskLevel = RiskLevel.SAFE
    estimated_cost: float = Field(default=0.1, ge=0.0, le=1.0)
    timeout_s: float = Field(default=30.0, gt=0, le=600)
    success_criteria: str = ""
    failure_behavior: str = "fail"  # fail | retry
    verification: str = "none"  # none | output_schema | explicit
    supported_devices: list[str] = Field(default_factory=lambda: ["any"])
    reversible: bool = False
    version: str = "1.0.0"

class PolicyDecision(BaseModel):
    allow: bool
    requires_approval: bool = False
    hard_deny: bool = False  # Stage 2: deny-pattern hits are never approvable
    grant_id: Optional[str] = None
    scope: str = ""
    expires_at: Optional[datetime] = None
    reason: str = ""

class ExecutionRecord(BaseModel):
    id: str
    tool: str
    inputs: dict = Field(default_factory=dict)
    device_id: str = "cloud"
    mission_id: Optional[str] = None
    auth: Optional[PolicyDecision] = None
    state: ExecutionState = ExecutionState.PENDING
    requested_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    verified: bool = False
    attempt: int = 0
    max_retries: int = 1
    cancel_requested: bool = False

class Event(BaseModel):
    id: str = ""
    type: str = ""
    source: str = ""
    device_id: Optional[str] = None
    mission_id: Optional[str] = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

class DeviceState(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    kind: DeviceKind = DeviceKind.CLOUD
    capabilities: list[str] = Field(default_factory=list)
    online: bool = True
    # Stage 2 presence: registered|online|offline|degraded|reconnecting
    status: str = "online"
    software_version: str = ""
    battery_pct: Optional[float] = None
    charging: bool = False
    network: str = "unknown"
    last_seen: datetime = Field(default_factory=utcnow)

class MissionStep(BaseModel):
    tool: str
    inputs: dict = Field(default_factory=dict)
    status: str = "pending"  # pending|running|ok|failed|skipped
    result: Optional[dict] = None
    error: Optional[str] = None

class Mission(BaseModel):
    id: str
    goal: str
    state: MissionState = MissionState.CREATED
    device_id: Optional[str] = None
    steps: list[MissionStep] = Field(default_factory=list)
    checkpoints: list[dict] = Field(default_factory=list)
    approvals: list[str] = Field(default_factory=list)
    retries: int = 0
    max_retries: int = 3
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

class MemoryItem(BaseModel):
    id: str = ""
    category: str = "episodic"  # preferences|project|task|episodic|documents|device
    text: str
    source: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    embedding: Optional[list[float]] = None
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    expires_at: Optional[datetime] = None

class ConversationTurn(BaseModel):
    role: str
    text: str
    created_at: datetime = Field(default_factory=utcnow)

class PermissionGrant(BaseModel):
    id: str
    who: str
    capability: str
    device_id: str = "*"
    resource: str = "*"
    expires_at: datetime
    used: bool = False

class Notification(BaseModel):
    id: str = ""
    title: str
    body: str = ""
    device_id: Optional[str] = None
    mission_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    delivered: bool = False

class AssistantState(BaseModel):
    started_at: datetime = Field(default_factory=utcnow)
    online: bool = True
    active_missions: int = 0
