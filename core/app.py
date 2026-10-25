"""FastAPI gateway: REST + WebSocket. Device-token gate (Stage 2: per-device auth)."""
from __future__ import annotations
import hashlib
import os
import queue
import threading
import time
import uuid
from typing import Optional
from fastapi import (Depends, FastAPI, Header, HTTPException, Query,
                     Request, WebSocket)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .audit import AuditLog
from .builtin_tools import BUILTINS
from .context import ContextManager
from .conversation import ConversationLoop
from .device_auth import DeviceAuthStore
from .device_tools import register_device_proxies
from .talk_stream import format_event
from .devices import DeviceManager
from .events import EventBus
from .execution import ExecutionEngine, PolicyDenied
from .governor import (ResourceGovernor, ResourceSnapshot,
                         evaluate_policy, governor_shadow_enabled,
                         governor_v2_enabled)
from .jobs import JOB_ERROR_MAX_CHARS, JOB_RESULT_MAX_BYTES, JobQueue
from .llm import LLMError, LLMErrorKind
from .memory import MemoryStore, PersistentMemoryStore
from .memory_policy import MemoryService
from .missions import MissionEngine, MissionState
from .models import DeviceKind, DeviceState, MemoryItem, utcnow
from .notifications import NotificationManager
from .policy import PolicyEngine
from .protocol import CapabilityUpdate, ExecutionResult, HeartbeatPayload
from .providers import provider_from_env
from .ratelimit import RateLimiter
from .routing import Router
from .scheduler import Scheduler
from .sessions import SessionStore
from .tools import ToolDefinition, ToolRegistry
from .tracing import Tracer
from .voice import (MockWakeEngine, VoicePipeline, stt_from_env,
                      tts_from_env)
from .scheduler import Scheduler
from .tools import ToolDefinition, ToolRegistry

TOKEN = os.environ.get("ASSISTANT_TOKEN", "dev-token")

# Server-side job long poll: max hold per request. Bounded below the
# Android battery-saver poll gap (60 s) so a held poll can never delay
# that device's other tick work by more than the deliberate cadence; and
# below the 90 s drone proxy wait (invariant: proxy wait > poll gap —
# tests/test_job_timing.py::test_T0).
AGENT_JOB_LONGPOLL_MAX_S = 30.0


class ChatIn(BaseModel):
    text: str
    device_id: str = "cloud"


class ExecIn(BaseModel):
    tool: str
    inputs: dict = {}
    device_id: str = "cloud"
    mission_id: Optional[str] = None


class ExecutionSubmitIn(BaseModel):
    tool: str
    inputs: dict = {}
    device_id: str = "cloud"
    mission_id: Optional[str] = None
    who: str = "user"
    idempotency_key: str = ""


class ExecutionResolveIn(BaseModel):
    capability: str
    inputs: dict = {}
    device_id: str = ""
    constraints: Optional[dict] = None
    preferred_device: str = ""
    allow_fallback: bool = True
    who: str = "user"
    mission_id: Optional[str] = None
    idempotency_key: str = ""
    explain: bool = False


# ---- Stage 14: Device Fabric request models (module level: FastAPI
# requires importable body models for validation) ----

class FabricDiscoverIn(BaseModel):
    device_id: str
    device_type: str = "unknown"
    display_name: str = ""
    transport: str = "unknown"
    metadata: dict = {}


class FabricRouteIn(BaseModel):
    capability: str
    device_id: str = ""
    who: str = "user"


class FabricGrantIn(BaseModel):
    device_id: str
    capabilities: list[str]
    max_risk: str = "confirm"
    ttl_s: int = 1800


class FabricTransferIn(BaseModel):
    source_device: str
    dest_device: str
    capability: str
    metadata: dict = {}


# ---- Stage 15: byte-transfer request models (module level) ----

class XferRequestIn(BaseModel):
    sender_device: str = ""  # operator sets; device endpoint ignores
    recipient_device: str = ""
    filename: str = ""
    size_bytes: int = 0
    sha256: str = ""
    content_type: str = ""
    grant_id: str = ""
    metadata: dict = {}


class XferChunkIn(BaseModel):
    seq: int = 0
    data_base64: str = ""


class XferAckIn(BaseModel):
    sha256: str = ""


class FabricResolveIn(BaseModel):
    capability: str = ""
    device_id: str = ""
    who: str = "user"
    preferred_device: str = ""
    constraints: dict = {}
    allow_fallback: bool = True


class FabricRelationshipIn(BaseModel):
    capability: str = ""
    related: str = ""
    relation: str = "fallback_for"


class IntentParseIn(BaseModel):
    utterance: str = ""
    requester_device: str = ""
    who: str = "user"


class IntentTurnIn(BaseModel):
    utterance: str = ""
    requester_device: str = ""
    who: str = "user"


class PlanStepIn(BaseModel):
    capability: str
    parameters: dict = Field(default_factory=dict)
    fetch: dict = Field(default_factory=dict)
    preferred_device: str = ""


class PlanIn(BaseModel):
    """Typed multi-step plan submission. This is a structured input
    surface for the SAME deterministic plan chain the NL path uses —
    never a second engine: every step is independently resolved and run
    through resolver -> policy -> approval -> fabric execution."""
    utterance: str = ""
    requester_device: str = ""
    who: str = "user"
    steps: list[PlanStepIn]


class SessionCreateIn(BaseModel):
    device_id: str = ""
    who: str = "user"


class SessionTurnIn(BaseModel):
    utterance: str = ""
    who: str = "user"


class FabricPrefIn(BaseModel):
    device_id: str
    key: str
    value: str


class MissionIn(BaseModel):
    goal: str
    steps: list[dict] = []
    device_id: Optional[str] = None


class MessagingSendIn(BaseModel):
    snapshot: dict = {}
    conversation: str = ""
    text: str = ""
    device_id: str = ""
    mission_id: str = ""
    who: str = "user"


class MessagingApproveIn(BaseModel):
    who: str = "user"


class DispatchIn(BaseModel):
    tool: str
    inputs: dict = {}
    who: str = "user"
    mission_id: Optional[str] = None


class EnrollIn(BaseModel):
    device_id: str
    kind: DeviceKind = DeviceKind.LINUX


class ClaimIn(BaseModel):
    pairing_code: str
    pubkey: str = ""  # optional Ed25519 public key (hex) for challenge auth


class PubkeyIn(BaseModel):
    pubkey: str = ""  # Ed25519 public key (hex); empty unbinds


class AgentRegisterIn(BaseModel):
    capabilities: list[str]
    software_version: str = ""
    kind: DeviceKind = DeviceKind.LINUX


class JobResultIn(BaseModel):
    job_id: str
    ok: bool
    result: dict = {}
    error: str = Field(default="", max_length=JOB_ERROR_MAX_CHARS)

    @field_validator("result")
    @classmethod
    def _bound_result(cls, v: dict) -> dict:
        # Task 9 (T3): mirror JobQueue.complete's ingest bound at the HTTP
        # boundary (422, fail-closed). Served verbatim on status polls, so
        # oversize payloads are rejected, never stored, never truncated.
        import json as _json
        try:
            size = len(_json.dumps(v))
        except Exception:  # noqa: BLE001 — unserializable is invalid
            raise ValueError("result not serializable")
        if size > JOB_RESULT_MAX_BYTES:
            raise ValueError(
                f"result exceeds {JOB_RESULT_MAX_BYTES} bytes")
        return v


class NotifAckIn(BaseModel):
    notification_id: str


class PushRegisterIn(BaseModel):
    token: str
    provider: str = "mock"


class ApprovalIn(BaseModel):
    decision: str  # approve|deny


class TalkIn(BaseModel):
    text: str
    session_id: str = ""
    device_id: str = "cloud"
    who: str = "user"


class TalkCancelIn(BaseModel):
    turn_id: str


class ResumeIn(BaseModel):
    execution_id: str
    session_id: str = ""


class VoiceTurnIn(BaseModel):
    audio_base64: str = ""
    session_id: str = ""
    device_id: str = "android-phone"
    who: str = "user"


class TTSIn(BaseModel):
    text: str
    session_id: str = ""


class WakeEventIn(BaseModel):
    phrase: str


class WakeBatteryIn(BaseModel):
    battery_pct: float | None = None
    charging: bool = False
    power_save: bool = False


class MemoryCorrectIn(BaseModel):
    old_query: str
    new_text: str


class MemoryConfirmIn(BaseModel):
    candidate_id: str
    ok: bool = True


class MemoryPinIn(BaseModel):
    pinned: bool = True

def _make_embedder():
    from .memory import EmbeddingProvider, LocalEmbeddingProvider
    if os.environ.get("ZARA_EMBEDDINGS", "hash").strip().lower() == "local":
        try:
            return LocalEmbeddingProvider(
                model=os.environ.get("ZARA_EMBED_MODEL",
                                     "BAAI/bge-small-en-v1.5"))
        except Exception as e:  # noqa: BLE001 — loud fallback, never crash boot
            print(f"ZARA_EMBEDDINGS=local unavailable ({e}); using hash")
    return EmbeddingProvider()


def build_stack(memory_db: str = "", audit_db: str = ":memory:",
                mission_db: str = "", fabric_db: str = "",
                device_auth_db: str = "",
                scheduler_db: str = ""):
    bus = EventBus()
    audit = AuditLog(audit_db)
    policy = PolicyEngine()
    registry = ToolRegistry()
    for definition, handler in BUILTINS:
        registry.register(definition, handler)
    # Stage 20: universal connected-device awareness. Registry seeded
    # here; a bounded background poller (ZARA_PERIPHERAL_POLL_S,
    # default off for tests, 12 s typical for the hosted Core) keeps
    # discovery fresh. Detection never grants trust or executes
    # anything; it only updates the registry and emits events.
    from .discovery import PeripheralRegistry, seed_registry
    jobs = JobQueue()
    register_device_proxies(registry, jobs)
    from .opencode_tools import OPENCODE_TOOLS as _OT
    for _d, _h in _OT:
        registry.register(_d, _h)
    from .browser_tools import BROWSER_TOOLS as _BT
    for _d, _h in _BT:
        registry.register(_d, _h)
    # Per-device credential persistence (ZARA_DEVICE_AUTH_DB): a paired
    # device's stored key keeps verifying across a Core restart, so the
    # secure-storage key on the device reconnects WITHOUT re-pairing.
    # Empty = in-memory (tests / ephemeral servers).
    if device_auth_db:
        from .device_auth import PersistentDeviceAuthStore as _PDA
        device_auth = _PDA(device_auth_db)
    else:
        device_auth = DeviceAuthStore()
    # Single-use proof-of-possession challenges (memory only; a Core
    # restart invalidates them and devices re-challenge or fall back).
    from .device_auth import ChallengeStore as _CS
    auth_challenges = _CS()
    events_log: list[dict] = []
    bus.subscribe("*", lambda ev: events_log.append(ev.model_dump()))
    engine = ExecutionEngine(registry, policy,
                             on_event=lambda t, p: bus.publish(t, source="execution", payload=p),
                             jobs=jobs)
    missions = MissionEngine(on_event=lambda t, p: bus.publish(t, source="missions", payload=p))
    if mission_db:
        from .missions import PersistentMissionEngine as _PME
        missions = _PME(mission_db,
                        on_event=lambda t, p: bus.publish(t, source="missions", payload=p))
    devices = DeviceManager(on_event=lambda t, p: bus.publish(t, source="devices", payload=p))
    if memory_db:
        memory = PersistentMemoryStore(memory_db, embedder=_make_embedder())
    else:
        memory = MemoryStore(embedder=_make_embedder())
    memory_service = MemoryService(memory)
    ctx = ContextManager()
    def _sched_fire(job: dict) -> None:
        # Stage 24i first: a reminder job payload is handled by the
        # ReminderService (notification delivery + recurring re-arm),
        # NOT the exec announce/dispatch path below. The service keeps
        # its own delivery ledger, so hook availability is never a
        # correctness dependency (recovery re-delivers from the
        # reminders table).
        _payload0 = job.get("payload") or {}
        if isinstance(_payload0.get("reminder"), dict):
            try:
                reminders.on_fire(job)
            except Exception:  # noqa: BLE001 — hook kills nothing
                pass
            return
        # Announce + authorized dispatch requests. The timer itself never
        # executes anything: a fired job with an "exec" payload is offered
        # to the NORMAL engine submit path (policy re-decided, approvals
        # preserved, revocation rechecked at dispatch). Time is never
        # approval: CONFIRM/HIGH_RISK requests park as WAITING_FOR_
        # PERMISSION exactly like a manual submit. No "exec" payload =
        # announce-only timer (historical behavior, unchanged).
        jid = job.get("id", "")
        try:
            bus.publish("scheduled_event", source="scheduler",
                        payload={"job_id": jid,
                                 "name": job.get("name", "")})
        except Exception:  # noqa: BLE001 — hook failure kills nothing
            pass
        try:
            audit.record("scheduler", "timer_fired", jid,
                         str(job.get("name", ""))[:200])
        except Exception:  # noqa: BLE001
            pass
        payload = job.get("payload") or {}
        req = payload.get("exec")
        if not isinstance(req, dict):
            return
        if sched.is_cancelled(jid):
            try:
                audit.record("scheduler", "dispatch_suppressed_cancelled",
                             jid, str(job.get("name", ""))[:200])
            except Exception:  # noqa: BLE001
                pass
            return
        tool = str(req.get("tool", ""))
        device_id = str(req.get("device_id", "cloud"))
        inputs = req.get("inputs", {})
        if not isinstance(inputs, dict):
            inputs = {}
        if not tool:
            try:
                audit.record("scheduler", "dispatch_rejected", jid,
                             "scheduled exec payload has no tool")
            except Exception:  # noqa: BLE001
                pass
            return
        # Fire-once + restart-safe: the key derives from the job id, so a
        # repeated fire or a post-restart re-announce collapses into the
        # SAME execution instead of duplicating side effects.
        key = str(req.get("idempotency_key", "") or f"sched-{jid}")
        binding = {"scheduled_job_id": jid,
                   "scheduled_name": str(job.get("name", ""))[:200],
                   "resolution_status": "scheduled-dispatch"}
        try:
            rec = engine.submit_async_idem(
                tool, inputs, device_id,
                who=str(req.get("who", "scheduler") or "scheduler"),
                mission_id=req.get("mission_id"),
                capability=req.get("capability"),
                binding=binding, idempotency_key=key)
        except Exception as e:  # noqa: BLE001 — unknown tool, bad
            try:  # inputs, hard-deny: loud audit, never a false submit.
                audit.record("scheduler", "dispatch_rejected", jid,
                             f"{tool}@{device_id}: {e}"[:300])
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            audit.record("scheduler", "dispatch_submitted", jid,
                         f"{rec.id} {rec.state.value} {tool}@{device_id}"
                         [:300])
        except Exception:  # noqa: BLE001
            pass
        try:
            bus.publish("scheduled_dispatch", source="scheduler",
                        payload={"job_id": jid, "execution_id": rec.id,
                                 "state": rec.state.value,
                                 "tool": tool, "device_id": device_id})
        except Exception:  # noqa: BLE001
            pass
    sched = Scheduler(on_fire=_sched_fire, db_path=scheduler_db)
    notifs = NotificationManager(on_event=lambda t, p: bus.publish(t, source="notifications", payload=p))
    # Stage 24i: NL reminders on the EXISTING scheduler + notifications.
    # ZARA_REMINDERS_DB names a file for restart persistence; unset =
    # process-local (tests). The service re-delivers/le-arms from its
    # own table at construction, so hook availability is never a
    # correctness dependency.
    from .reminders import ReminderService as _ReminderService
    reminders = _ReminderService(
        sched, notifs, audit,
        db_path=_db_path("ZARA_REMINDERS_DB"))
    from .push import PushRegistry as _PushRegistry
    push = _PushRegistry()
    gov = ResourceGovernor()
    model = provider_from_env()
    router = Router(devices, registry, policy, gov)
    sessions = SessionStore(
        db_path=os.environ.get("ZARA_CHAT_DB", ""))
    tracer = Tracer()
    loop = ConversationLoop(provider=model, ctx=ctx, memory=memory_service,
                            registry=registry, policy=policy, engine=engine,
                            router=router, devices=devices, governor=gov,
                            missions=missions, audit=audit, bus=bus,
                            sessions=sessions, tracer=tracer)
    voice = VoicePipeline(stt_from_env(), tts_from_env(), loop, sessions)
    wake = MockWakeEngine()
    limits = {"general": RateLimiter(600, 60.0),
              "sensitive": RateLimiter(30, 60.0)}
    bus.subscribe("*", lambda ev: audit.record("bus", ev.type, ev.source, str(ev.payload)[:500]))
    # Stage 9: approval holds on a device notify THAT device (with the
    # execution ID so its Approve/Deny buttons resolve through Core).
    def _approval_notify(payload):
        try:
            exec_id = (payload or {}).get("execution_id")
            rec = engine.get(exec_id) if exec_id else None
        except (KeyError, TypeError):
            rec = None
        if rec is None or not rec.device_id:
            return
        try:
            devices.get(rec.device_id)
        except KeyError:
            return  # unknown device: nobody to notify
        # Rung-1: approval UX must show the concrete effect (what package),
        # not just the capability name — redacted + truncated like all
        # notification bodies.
        from .tracing import redact as _redact
        try:
            effect = _redact(", ".join(
                f"{k}={v}" for k, v in dict(rec.inputs or {}).items()
                if isinstance(v, (str, int, float, bool))))[:200]
        except Exception:  # noqa: BLE001 — inputs never break notify
            effect = ""
        detail = (f"Execution {rec.id} on {rec.device_id} needs a decision."
                  + (f" Effect: {effect}." if effect else ""))
        notifs.create(f"Approval required: {rec.tool}", detail[:500],
                      device_id=rec.device_id, mission_id=rec.mission_id,
                      execution_id=rec.id)
    bus.subscribe("permission_required", lambda ev: _approval_notify(ev.payload))
    # Stage 14: Device Fabric adapter over devices/auth/registry/policy/
    # governor/jobs/engine. ZARA_FABRIC_DB enables sqlite persistence
    # (revocation tombstones survive restart); empty = in-memory.
    import os as _os
    from .fabric import FabricRegistry, FabricStore
    from .db import Database as _Database
    _fabric_db_path = fabric_db or _os.environ.get("ZARA_FABRIC_DB", "")
    _fabric_store = FabricStore(_Database(_fabric_db_path)
                                if _fabric_db_path else None)
    fabric = FabricRegistry(devices, device_auth, registry, policy, gov,
                            jobs, engine, audit, bus, _fabric_store)
    # Stage 15: byte-transfer engine over the fabric registry.
    # Storage root from ZARA_TRANSFER_DIR (default
    # ~/.local/share/zara/transfers). Transfer records persist in the
    # same fabric sqlite DB when ZARA_FABRIC_DB is set.
    from .transfer import engine_from_env as _xfer_from_env
    xfer = _xfer_from_env(fabric)
    # Stage 17: deterministic resolver over the fabric registry
    # (orchestrates authorize/route; owns no authority itself).
    from .resolver import CapabilityResolver
    resolver = CapabilityResolver(fabric)
    # Resolver→async binding + approval re-confirmation (Part A/B):
    # re-derive trust/presence/policy/capability freshness at approve
    # time and pre-dispatch for HIGH_RISK tools. Fail-closed: any check
    # failing denies the execution; success never grants by itself.
    def _engine_revalidate(rec):
        try:
            from .fabric import EXECUTABLE_TRUST
            # Device legs apply only to device-dispatched tools
            # (supported_devices ["linux","android"], never ["any"]).
            # Local tools (conversation/session/talk flows, device "cloud",
            # "", "local-core", …) skip them; policy + tool-exists below
            # still apply. Any other unknown device with a device tool
            # fails closed at trust (no arbitrary-device submit).
            try:
                _dev_tool = registry.get(rec.tool)
                _needs_device = "any" not in list(
                    _dev_tool.supported_devices or [])
            except Exception:
                _needs_device = True
            if _needs_device:
                trust = fabric.trust_of(rec.device_id)
                if trust not in EXECUTABLE_TRUST:
                    return (False, f"device trust {trust} not executable")
                presence = fabric.presence_of(rec.device_id)
                if presence in ("offline", "unknown", "stale"):
                    return (False, f"device presence {presence}")
                # Capability check: resolve-path executions carry a real
                # capability id; direct submits (POST /v1/executions) carry
                # none, so authorize the TOOL's required capabilities —
                # never the tool name itself. The fabric catalog holds
                # advertised capability ids (e.g. "shell.exec.readonly"),
                # not tool names, so a tool-name lookup fails closed even
                # for fully-advertised devices (this broke every direct-
                # submitted device approval in production while tests
                # passed only because they advertise tool names).
                try:
                    required = list(registry.get(rec.tool)
                                    .required_capabilities or [])
                except Exception:
                    required = []
                caps = [rec.capability] if rec.capability else required
                if not caps:
                    caps = [rec.tool]
                for cap in caps:
                    verdict = fabric.authorize_capability(cap, rec.device_id)
                    if not verdict.get("authorized", False):
                        return (False,
                                f"capability unauthorized: "
                                f"{verdict.get('reason', '?')}"[:200])
                # Descriptor staleness: a re-registered-without-describe
                # snapshot must not authorize fresh dispatches.
                # (required[] reused from the check above.)
                try:
                    descs = {c.name: c for c in fabric.capabilities_for(
                        rec.device_id)}
                except Exception:
                    descs = {}
                for need in required:
                    d = descs.get(need)
                    if d is not None and getattr(d, "stale", False):
                        return (False,
                                f"capability snapshot stale: {need}")
            try:
                definition = registry.get(rec.tool)
                risk = definition.risk
            except KeyError:
                return (False, "tool withdrawn from registry")
            decision = policy.decide(
                "user", capability=f"tool:{rec.tool}",
                device_id=rec.device_id, resource=rec.tool, risk=risk,
                context={"command": str(rec.inputs)})
            if decision.hard_deny or (not decision.allow
                                      and not decision.requires_approval):
                return (False,
                        f"policy now denies: {decision.reason}"[:200])
            return (True, "revalidated")
        except Exception as e:
            return (False, f"revalidate error: {e}"[:200])
    engine.set_revalidate(_engine_revalidate)
    # Stage 18: natural-language intent service (proposal only; the
    # resolver + policy/governor/execution stay authoritative). Hosted
    # parsing is enabled only when a real provider is configured —
    # otherwise the deterministic local parser serves offline.
    from .intent import (CompositeIntentParser, HostedIntentParser,
                         IntentTurnService, LocalIntentParser)
    from .call_flow import CallFlow, SmsFlow
    from .otp_flow import OtpFlow
    from .photo_flow import PhotoFlow
    from .clipboard_flow import ClipboardFlow
    from .calendar_flow import CalendarFlow
    from .weather_flow import WeatherFlow
    from .morning_brief import MorningBriefFlow
    _hosted = None
    try:
        if getattr(model, "name", "echo") not in ("echo",):
            _hosted = HostedIntentParser(model)
    except Exception:  # noqa: BLE001 — local-only fallback
        _hosted = None
    # Mission-bound single-approval messaging: extends the existing
    # execution/policy/audit stack (never a parallel one). The send ledger
    # persists across restarts only when ZARA_MESSAGING_DB names a file.
    from .messaging import (MessagingApprovalStore as _MsgApprovals,
                            MessagingWorkflow as _MsgWorkflow,
                            SendLedger as _SendLedger)
    _msg_db = _os.environ.get("ZARA_MESSAGING_DB", "")
    messaging = _MsgWorkflow(
        fabric, missions=missions, audit=audit, resolver=resolver,
        ledger=_SendLedger(path=_msg_db) if _msg_db else _SendLedger(),
        approvals=_MsgApprovals())
    intent_service = IntentTurnService(
        fabric, resolver,
        CompositeIntentParser(LocalIntentParser(), _hosted), audit,
        missions, messaging, reminders, CallFlow(
            fabric, resolver, engine, memory_service, audit, devices),
        SmsFlow(fabric, resolver, engine, memory_service, audit,
                devices), OtpFlow(fabric, engine, audit),
                PhotoFlow(fabric, engine, audit),
                ClipboardFlow(fabric, engine, audit),
                CalendarFlow(audit),
                WeatherFlow(audit))
    # P5 ⑮: the brief is composed FROM the two read flows above plus
    # system_health — pass the live instances so sections reuse the
    # exact tested reads (never a parallel implementation).
    intent_service.morning_brief_flow = MorningBriefFlow(
        audit, intent_service.calendar_flow,
        intent_service.weather_flow, reminders=reminders)
    # P6-b27: named recurring routines on the SAME scheduler +
    # notifications as reminders (one live job each, audited), plus
    # the weekly review — a read-only composite that reuses the
    # calendar flow's tested week digest, conflicts_on_date, the
    # reminder store, and the routine list. Nothing here executes.
    from .routines import RoutineService as _RoutineService, \
        RoutineFlow as _RoutineFlow
    routines = _RoutineService(
        sched, notifs, audit, db_path=_db_path("ZARA_ROUTINES_DB"))
    intent_service.routines_flow = _RoutineFlow(routines)
    from .weekly_review import WeeklyReviewFlow as _WeeklyReviewFlow
    intent_service.weekly_review_flow = _WeeklyReviewFlow(
        audit, intent_service.calendar_flow, reminders=reminders,
        routines=routines)
    # P6-b28: chat ledgers (water/expenses/grocery/meals/habits/
    # medication) — the user's own diary in one additive sqlite table.
    # Writes are data-only (deterministic parse, audited, no
    # execution authority); the weekly review's chat-logs line reads
    # the SAME service so the totals can never drift.
    from .ledgers import LedgerService as _LedgerService, \
        LedgerFlow as _LedgerFlow
    ledgers = _LedgerService(audit,
                             db_path=_db_path("ZARA_LEDGERS_DB"))
    intent_service.ledger_flow = _LedgerFlow(ledgers)
    intent_service.weekly_review_flow.ledgers = ledgers
    # P6-b29: capture routing (GTD bridge: capture → file → buckets)
    # + the READ-ONLY mail wire (dormant until ZARA_IMAP_* config;
    # never marks \Seen, never drafts — user rule).
    from .capture import CaptureStore as _CaptureStore, \
        CaptureFlow as _CaptureFlow
    capture = _CaptureStore(audit,
                            db_path=_db_path("ZARA_CAPTURE_DB"))
    intent_service.capture_flow = _CaptureFlow(capture)
    intent_service.weekly_review_flow.capture = capture
    # batch-32 (2026-10-26): habit chains adopt the uhabits streak
    # design (marks on days, scheduled-day runs, honest closed-chain
    # reporting); persist in their own WAL sqlite so restarts keep
    # the chains (start_core.sh exports ZARA_HABITS_DB).
    from .habits import HabitService as _HabitService, \
        HabitsFlow as _HabitsFlow
    habits = _HabitService(audit,
                           db_path=_db_path("ZARA_HABITS_DB"))
    intent_service.habits_flow = _HabitsFlow(habits)
    intent_service.weekly_review_flow.habits = habits
    from .mail_wire import MailWire as _MailWire, MailFlow as _MailFlow
    intent_service.mail_flow = _MailFlow(_MailWire(audit))
    # P6-b30: the meeting scheduler — proposes CalendarFlow's tested
    # free windows, books ONLY on an explicit pick, and every booking
    # still goes through engine.submit + the CONFIRM approval.
    from .schedule_flow import ScheduleFlow as _ScheduleFlow
    intent_service.schedule_flow = _ScheduleFlow(
        engine, intent_service.calendar_flow, devices, audit)
    loop.intent_fastpath = intent_service.conversational_fastpath
    # Stage 24h: mission-bound record adoption. When a mission-bound
    # execution reaches SUCCEEDED, the parked plan adopts the outcome
    # immediately (bookkeeping only) so a Core restart never loses the
    # fact that the approved step ran. Records stay in-memory; the
    # mission store persists.
    engine.set_mission_adopt(intent_service.adopt_mission_record)
    # Stage 19: persistent conversation sessions (context only; the
    # resolver + policy/governor/execution stay authoritative). DB path
    # from ZARA_SESSION_DB; memory mode when unset (tests).
    from .session import SessionStateStore, SessionTurnService
    from .db import Database as _SessionDatabase
    _session_db_path = _os.environ.get("ZARA_SESSION_DB", "")
    session_service = SessionTurnService(
        fabric, resolver, intent_service.parser, audit,
        SessionStateStore(
            _SessionDatabase(_session_db_path)
            if _session_db_path else None))
    # Delegated specialist sessions: durable tracking + safe restart
    # recovery. ZARA_DELEGATION_DB unset = in-memory only (tests); file =
    # write-through metadata (no prompts, bounded tails) + startup
    # reconciliation that never reruns tasks or kills strangers.
    from .opencode_tools import (DelegationStore as _DelegationStore,
                                 configure_delegation as _configure_deleg,
                                 recover_delegated_sessions as _recover_deleg)
    _delegation_path = _os.environ.get("ZARA_DELEGATION_DB", "")
    delegation = _DelegationStore(_delegation_path or ":memory:")
    _configure_deleg(delegation)
    _recover_deleg(delegation)
    peripherals = PeripheralRegistry(
        on_event=lambda t, p, b: bus.publish(
            t, source="peripherals", payload=b))
    seed_registry(peripherals)

    class _PeripheralPoller:
        """Bounded polling loop for host discovery. Runs only when
        ZARA_PERIPHERAL_POLL_S > 0; daemon thread, quiet errors."""
        def __init__(self, interval_s: float) -> None:
            self.interval = max(2.0, interval_s)
            self._stop = threading.Event()
            self._t = threading.Thread(target=self._loop, daemon=True)

        def _loop(self) -> None:
            while not self._stop.is_set():
                refresh_host(peripherals)
                self._stop.wait(self.interval)

        def start(self) -> None:
            self._t.start()

        def stop(self) -> None:
            self._stop.set()
            self._t.join(timeout=2.0)

    _poll_s = 0.0
    try:
        _poll_s = float(_os.environ.get("ZARA_PERIPHERAL_POLL_S", "0"))
    except ValueError:
        _poll_s = 0.0
    poller = None
    if _poll_s > 0:
        poller = _PeripheralPoller(_poll_s)
        poller.start()
    # ---- Stage 21: cross-device file-transfer missions ----
    from .transfer_missions import FileTransferMissionRunner
    from .transfer_state import set_runner

    def _tm_execute(tool, inputs, device_id, mission_id=""):
        """The production funnel: every hop goes through the same
        ExecutionEngine.submit the natural-language path uses (policy
        re-decided per hop, approval parking surfaced, fabric
        dispatch, audit). Bytes never ride this call — hops stay
        under the input-size envelope by design."""
        from .execution import ExecutionState
        rec = engine.submit(tool, inputs, device_id=device_id,
                            who="user",
                            mission_id=mission_id or None)
        if rec.state == ExecutionState.WAITING_FOR_PERMISSION:
            return {"ok": False, "parked": True, "exec_id": rec.id,
                    "error": rec.error or "approval required"}
        if rec.state == ExecutionState.FAILED:
            return {"ok": False, "error": rec.error or "failed",
                    "detail": rec.result if isinstance(
                        rec.result, dict) else {}}
        if rec.state not in (ExecutionState.COMPLETED,
                             ExecutionState.RUNNING,
                             ExecutionState.PENDING):
            return {"ok": False, "error": rec.error or "no result"}
        out = rec.result if isinstance(rec.result, dict) else {}
        return out

    def _tm_resolve(hint: str):
        hint = (hint or "").strip().lower()
        try:
            devs = devices.list()
        except Exception:
            return None, False
        for d in devs:
            did = (d.device_id or "").lower()
            if hint and (did == hint or hint in did):
                return d, d.status == "online"
        want = {"laptop": "LINUX", "phone": "ANDROID"}.get(hint)
        if not want:
            return None, False
        for d in devs:
            if d.kind.value == want:
                return d, d.status == "online" or d.online
        return None, False

    def _tm_audit(action, target, detail=""):
        audit.record("system", action, target,
                     f"{detail}"[:280])
        bus.publish("transfer_mission_" + action.split("_")[-1],
                    source="transfer-missions", payload={
                        "mission_id": target, "detail": detail})

    runner = FileTransferMissionRunner(
        missions, xfer, bus=bus, execute=_tm_execute,
        resolve_device=_tm_resolve, audit=_tm_audit)
    set_runner(runner)

    return {"bus": bus, "audit": audit, "policy": policy, "registry": registry,
            "auth_challenges": auth_challenges,
            "engine": engine, "missions": missions, "devices": devices,
            "memory": memory, "memory_service": memory_service,
            "ctx": ctx, "sched": sched, "notifs": notifs,
            "reminders": reminders,
            "gov": gov, "model": model, "events_log": events_log,
            "jobs": jobs, "device_auth": device_auth, "router": router,
            "sessions": sessions, "tracer": tracer, "loop": loop,
            "voice": voice, "wake": wake, "limits": limits, "push": push,
            "fabric": fabric, "xfer": xfer, "resolver": resolver,
            "intent": intent_service, "session": session_service,
            "messaging": messaging,
            "delegation": delegation, "peripherals": peripherals,
            "peripheral_poller": poller,
            "transfer_runner": runner}


def create_app(stack=None) -> FastAPI:
    s = stack or build_stack()
    app = FastAPI(title="Personal Assistant Core", version="0.1.0")
    app.state.stack = s

    SENSITIVE_PATHS = ("/v1/agent/enroll", "/v1/agent/claim")

    # Global request-body cap (OCI small-VM guard): the largest legitimate
    # body is voice audio at 2 MiB (per-endpoint 413); transfer chunks are
    # <= 64 KiB. Default 5 MiB via ZARA_MAX_BODY_BYTES; enforced on
    # Content-Length before any parsing. Chunked bodies without a length
    # still hit the per-endpoint caps.
    try:
        _max_body = int(os.environ.get("ZARA_MAX_BODY_BYTES", "5242880"))
    except ValueError:
        _max_body = 5242880

    # Insecure-transport rejection (production). When ZARA_REQUIRE_HTTPS=1,
    # any non-loopback plain-HTTP connection is refused: Core explicitly
    # refuses to downgrade. HTTPS (direct or via a local TLS-terminating
    # proxy announcing x-forwarded-proto: https) is required; loopback
    # http stays allowed for same-host development clients. Signature
    # proofs protect device identity, never wire confidentiality — that
    # is TLS's job (docs/TRANSPORT_TLS.md).
    _require_https = os.environ.get("ZARA_REQUIRE_HTTPS", "0") == "1"

    @app.middleware("http")
    async def strict_transport(request, call_next):
        if _require_https:
            host = request.client.host if request.client else ""
            loopback = host in ("127.0.0.1", "::1", "localhost", "testclient")
            scheme = request.url.scheme
            fwd = request.headers.get("x-forwarded-proto", "")
            secure = scheme == "https" or (
                loopback and fwd == "") or fwd == "https"
            # A spoofable forwarded-proto header flagging non-HTTPS is a
            # downgrade attempt — reject rather than trust it.
            if not secure or (fwd and fwd != "https"):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "insecure transport refused: "
                                       "TLS required (ZARA_REQUIRE_HTTPS=1);"
                                       " see docs/TRANSPORT_TLS.md"})
        return await call_next(request)

    @app.middleware("http")
    async def body_limit(request, call_next):
        try:
            clen = request.headers.get("content-length", "")
            if clen and int(clen) > _max_body:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "request body too large"})
        except ValueError:
            pass
        return await call_next(request)

    @app.middleware("http")
    async def rate_limit(request, call_next):
        if request.url.path == "/v1/health":
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        bucket = "sensitive" if request.url.path in SENSITIVE_PATHS \
            else "general"
        allowed, retry_after = s["limits"][bucket].check(
            f"{bucket}:{client}")
        if not allowed:
            return JSONResponse(status_code=429,
                                content={"detail": "rate limited",
                                         "retry_after_s": round(retry_after, 1)})
        return await call_next(request)

    async def auth(authorization: Optional[str] = Header(None)):
        if authorization != f"Bearer {TOKEN}":
            raise HTTPException(status_code=401, detail="invalid token")
        return True

    @app.get("/v1/health")
    def health():
        return {"ok": True, "version": "0.1.0"}

    @app.get("/v1/ready")
    def ready():
        """Readiness: registry loaded, memory writable, bus alive."""
        checks: dict = {"registry": len(s["registry"].list()) > 0}
        try:
            probe = MemoryItem(text="readiness probe", importance=0.0)
            checks["memory_write"] = True
        except Exception:  # noqa: BLE001
            checks["memory_write"] = False
        try:
            s["bus"].publish("scheduled_event", source="ready-probe",
                             payload={"probe": True})
            checks["event_bus"] = True
        except Exception:  # noqa: BLE001
            checks["event_bus"] = False
        checks["provider"] = s["model"].name
        return {"ready": all(v is True or isinstance(v, str)
                             for v in checks.values()),
                "checks": checks,
                "governor_v2": governor_v2_enabled(),
                "governor_shadow": governor_shadow_enabled()}

    @app.post("/v1/chat")
    def chat(body: ChatIn, _=Depends(auth)):
        """Propose-only loop: model text + optional memory context. No auto-execute."""
        s["ctx"].add_turn("user", body.text)
        hits = s["memory"].recall(body.text, top_k=3)
        bundle = s["ctx"].bundle(memories=[h[0] for h in hits])
        prompt = s["ctx"].build_prompt(bundle, body.text)
        try:
            resp = s["model"].complete(prompt)
        except LLMError as e:
            # Propose-only path fails safe (never a 500 with raw internals).
            raise HTTPException(status_code=502,
                                detail=f"provider {e.kind.value}: unavailable")
        s["ctx"].add_turn("assistant", resp.text)
        s["bus"].publish("user_message", source="api",
                         payload={"text": body.text[:500]})
        return {"reply": resp.text, "memory_hits": len(hits)}

    @app.post("/v1/exec")
    def exec_tool(body: ExecIn, _=Depends(auth)):
        try:
            tool = s["registry"].get(body.tool)
        except KeyError:
            s["audit"].record("api", "exec_reject", body.tool,
                              "unknown tool"[:300])
            raise HTTPException(status_code=404,
                                detail=f"unknown tool: {body.tool}"[:200])
        dev = None
        try:
            dev = s["devices"].get(body.device_id)
        except KeyError:
            dev = None
        # Fast reject: a revoked target must fail clearly at submit time
        # instead of parking a job that can never be claimed (observed
        # live: post-revoke submit sat pending until proxy expiry).
        # Unknown devices keep the existing fail-open behavior.
        try:
            if s["fabric"].trust_of(body.device_id) == "revoked":
                s["audit"].record("api", "exec_reject", body.tool,
                                  f"revoked target: {body.device_id}"[:300])
                raise HTTPException(
                    status_code=409,
                    detail=f"device revoked: {body.device_id} "
                           f"(re-enroll to repair)")
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — trust read never blocks submit
            pass
        # Canonical provider: one snapshot source for every exec path.
        # Unknown devices yield the bare default (fail-open to unknown).
        try:
            _did = dev.device_id
        except AttributeError:
            _did = body.device_id
        snap = s["fabric"].resource_snapshot(_did)
        gate = s["gov"].check(snap, tool.estimated_cost, tool.risk.value,
                               demands=tool.resource_demands)
        if gate.action == "defer":
            return {"status": "deferred", "reason": gate.reason}
        try:
            rec = s["engine"].submit(body.tool, body.inputs, body.device_id,
                                     mission_id=body.mission_id)
        except PolicyDenied as e:
            # Hard-deny must be a truthful 403, never an unhandled 500.
            s["audit"].record("api", "exec_deny", body.tool, str(e)[:300])
            raise HTTPException(status_code=403, detail=str(e)[:300])
        except ValueError as e:
            # Task 9: input-envelope rejection (oversize/unserializable
            # inputs are refused before any record exists) is a 422, never
            # a 500 and never a retained record.
            s["audit"].record("api", "exec_reject", body.tool, str(e)[:300])
            raise HTTPException(status_code=422, detail=str(e)[:300])
        s["audit"].record("api", "exec", body.tool, rec.state.value)
        return {"id": rec.id, "status": rec.state.value, "result": rec.result,
                "error": rec.error, "verified": rec.verified}

    @app.post("/v1/exec/{exec_id}/approve")
    def approve_exec(exec_id: str, _=Depends(auth)):
        try:
            rec = s["engine"].approve(exec_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown execution")
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)[:300])
        return {"id": rec.id, "status": rec.state.value, "result": rec.result}

    def _execution_view(rec, jobs) -> dict:
        """Status envelope for async polling: record fields safe to expose
        (auth/grant internals never leave Core) plus a read-time job join.
        Result content is untrusted device data, never authority."""
        job = jobs.get_by_execution(rec.id)
        # Binding summary echoed for resolver-driven callers. The approval
        # fingerprint itself is integrity material and never exposed.
        binding = dict(rec.binding or {})
        summary = {k: binding.get(k) for k in (
            "resolution_status", "fallback_used", "substituted")
            if k in binding}
        return {"id": rec.id, "tool": rec.tool, "device_id": rec.device_id,
                "mission_id": rec.mission_id, "status": rec.state.value,
                "capability": rec.capability, "binding": summary,
                "job_id": job.id if job is not None else None,
                "job_state": job.state if job is not None else None,
                "result": rec.result, "error": rec.error,
                "verified": rec.verified,
                "approval_required": rec.state.value == "waiting_for_permission",
                "cancel_requested": rec.cancel_requested, "attempt": rec.attempt,
                "requested_at": rec.requested_at.isoformat(),
                "started_at": rec.started_at.isoformat()
                if rec.started_at else None,
                "ended_at": rec.ended_at.isoformat() if rec.ended_at else None}

    @app.post("/v1/executions", status_code=201)
    def create_execution(body: ExecutionSubmitIn, _=Depends(auth)):
        """Async submit: same gates as /v1/exec, but RUNNING records execute
        on a daemon thread and the id returns immediately (201 running,
        202 parked-for-approval). Poll GET for completion."""
        try:
            tool = s["registry"].get(body.tool)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown tool")
        try:
            dev = s["devices"].get(body.device_id)
        except KeyError:
            dev = None
        # Fast reject: a revoked target must fail clearly at submit time
        # instead of parking a job that can never be claimed (observed
        # live: post-revoke submit sat pending until proxy expiry).
        # Unknown devices keep the existing fail-open behavior.
        try:
            if s["fabric"].trust_of(body.device_id) == "revoked":
                s["audit"].record("api", "exec_reject", body.tool,
                                  f"revoked target: {body.device_id}"[:300])
                raise HTTPException(
                    status_code=409,
                    detail=f"device revoked: {body.device_id} "
                           f"(re-enroll to repair)")
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — trust read never blocks submit
            pass
        # Canonical provider: one snapshot source for every exec path.
        # Unknown devices yield the bare default (fail-open to unknown).
        try:
            _did = dev.device_id
        except AttributeError:
            _did = body.device_id
        snap = s["fabric"].resource_snapshot(_did)
        gate = s["gov"].check(snap, tool.estimated_cost, tool.risk.value,
                               demands=tool.resource_demands)
        if gate.action == "defer":
            return {"status": "deferred", "reason": gate.reason}
        try:
            rec = s["engine"].submit_async_idem(
                body.tool, body.inputs, body.device_id, who=body.who,
                mission_id=body.mission_id,
                idempotency_key=body.idempotency_key)
        except PolicyDenied as e:
            s["audit"].record("api", "exec_deny", body.tool, str(e)[:300])
            raise HTTPException(status_code=403, detail=str(e)[:300])
        except ValueError as e:
            # Task 9: input-envelope rejection before any record exists.
            s["audit"].record("api", "exec_reject", body.tool, str(e)[:300])
            raise HTTPException(status_code=422, detail=str(e)[:300])
        s["audit"].record("api", "exec_submit", body.tool, rec.state.value)
        view = _execution_view(rec, s["jobs"])
        return JSONResponse(
            content=view,
            status_code=202
            if rec.state.value == "waiting_for_permission" else 201)

    @app.get("/v1/executions/{exec_id}")
    def get_execution(exec_id: str, _=Depends(auth)):
        try:
            rec = s["engine"].get(exec_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown execution")
        return _execution_view(rec, s["jobs"])

    @app.post("/v1/executions/{exec_id}/approve")
    def approve_execution(exec_id: str, _=Depends(auth)):
        """Non-blocking twin of /v1/exec/{id}/approve for the async flow:
        grants then runs on a daemon thread; poll GET for completion."""
        try:
            rec = s["engine"].approve_async(exec_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown execution")
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)[:300])
        s["audit"].record("api", "exec_approve", rec.tool, rec.state.value)
        return _execution_view(rec, s["jobs"])

    @app.post("/v1/executions/{exec_id}/cancel")
    def cancel_execution(exec_id: str, _=Depends(auth)):
        try:
            rec = s["engine"].get(exec_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown execution")
        if rec.state.value in ("succeeded", "failed", "timed_out",
                               "denied", "cancelled"):
            raise HTTPException(
                status_code=409,
                detail=f"already terminal: {rec.state.value}")
        rec = s["engine"].cancel(exec_id)
        s["audit"].record("api", "exec_cancel", rec.tool, rec.state.value)
        return _execution_view(rec, s["jobs"])

    @app.post("/v1/executions/resolve", status_code=201)
    def create_resolved_execution(body: ExecutionResolveIn,
                                  _=Depends(auth)):
        """Canonical external path: resolve a capability deterministically,
        bind the resolution into the execution, enter async lifecycle.

        Flow: resolver.resolve → authorized concrete target → submit_async
        with capability+binding → 201/202 view + resolution echo. Denied /
        offline / unknown targets return 403/409 with reason and create NO
        execution. The binding is advisory record, never authority: policy
        re-decides at submit, and approve/dispatch re-validate.
        No silent substitution: the execution stays on the resolved device
        unless the resolver itself performed an explicit fallback (echoed).
        """
        resolution = s["resolver"].resolve(
            body.capability, body.device_id or "", who=body.who,
            constraints=body.constraints,
            preferred_device=body.preferred_device,
            allow_fallback=body.allow_fallback)
        if resolution.status not in ("resolved", "requires_approval"):
            raise HTTPException(
                status_code=409 if resolution.status in (
                    "offline", "governor_blocked") else 403,
                detail=f"{resolution.status}: {resolution.reason}"[:300])
        target_device = resolution.device_id
        if not target_device:
            raise HTTPException(
                status_code=409,
                detail=f"{resolution.status}: {resolution.reason}"[:300])
        try:
            s["registry"].get(body.capability)
        except KeyError:
            raise HTTPException(
                status_code=409,
                detail=f"capability {body.capability} has no executable "
                "tool implementation")
        binding = {"resolution_status": resolution.status,
                   "fallback_used": resolution.fallback_used,
                   "substituted": resolution.substituted,
                   "reason": resolution.reason[:300]}
        try:
            rec = s["engine"].submit_async_idem(
                body.capability, body.inputs, target_device, who=body.who,
                mission_id=body.mission_id, capability=body.capability,
                binding=binding, idempotency_key=body.idempotency_key)
        except PolicyDenied as e:
            s["audit"].record("api", "exec_deny", body.capability,
                               str(e)[:300])
            raise HTTPException(status_code=403, detail=str(e)[:300])
        except ValueError as e:
            # Task 9: input-envelope rejection before any record exists.
            s["audit"].record("api", "exec_reject", body.capability,
                               str(e)[:300])
            raise HTTPException(status_code=422, detail=str(e)[:300])
        s["audit"].record("api", "exec_resolve_submit", body.capability,
                           f"{target_device} {rec.state.value}")
        view = _execution_view(rec, s["jobs"])
        view["resolution"] = {"status": resolution.status,
                              "device_id": target_device,
                              "fallback_used": resolution.fallback_used,
                              "substituted": resolution.substituted}
        if body.explain:
            # Opt-in diagnostic only: bounded non-authoritative record
            # built from the already-computed resolution (no re-resolve).
            # Never alters the verdict, the execution, or any gate.
            try:
                view["explanation"] = s["resolver"].explain_resolution(
                    resolution, body.constraints)
            except Exception:  # noqa: BLE001 — diagnostics never break it
                pass
        return JSONResponse(
            content=view,
            status_code=202
            if rec.state.value == "waiting_for_permission" else 201)

    @app.get("/v1/tools")
    def list_tools(_=Depends(auth)):
        return [t.model_dump() for t in s["registry"].list()]

    @app.post("/v1/tools")
    def register_tool(definition: ToolDefinition, _=Depends(auth)):
        s["registry"].register(definition, lambda inputs, ctx: {"ok": True})
        return {"registered": definition.name}

    @app.post("/v1/missions")
    def create_mission(body: MissionIn, _=Depends(auth)):
        m = s["missions"].create(body.goal, body.steps, body.device_id)
        s["missions"].transition(m.id, MissionState.PLANNING)
        return m.model_dump()

    @app.get("/v1/missions")
    def list_missions(_=Depends(auth)):
        return [m.model_dump() for m in s["missions"].list()]

    @app.get("/v1/missions/{mid}")
    def get_mission(mid: str, _=Depends(auth)):
        try:
            return s["missions"].get(mid).model_dump()
        except KeyError:
            raise HTTPException(status_code=404,
                                detail=f"unknown mission: {mid}")

    @app.post("/v1/missions/{mid}/transition")
    def mission_transition(mid: str, to: MissionState, _=Depends(auth)):
        return s["missions"].transition(mid, to).model_dump()

    # ---- Plan continuation (Stage 24g) ----

    @app.post("/v1/missions/{mid}/continue")
    def continue_mission(mid: str, _=Depends(auth)):
        """Continue a plan parked at WAITING_FOR_PERMISSION. Authority
        stays exactly where it was: the continuation re-enters the
        same per-step pipeline (resolver -> policy -> governor ->
        fabric.execute -> verification), the held step is never
        re-executed (its real execution record is inspected first),
        and a mid-continuation approval parks the mission again."""
        out = s["intent"].resume_plan(mid, "api")
        code = 404 if "No mission with reference" in (out.get("message")
                                                     or "") else 200
        s["audit"].record("api", "mission_continue", mid,
                          str(out.get("executed", False)))
        if code != 200:
            raise HTTPException(status_code=code, detail=out["message"])
        return out

    @app.post("/v1/devices")
    def register_device(dev: DeviceState, _=Depends(auth)):
        return s["devices"].register(dev).model_dump()

    @app.get("/v1/devices")
    def list_devices(_=Depends(auth)):
        return [d.model_dump() for d in s["devices"].list()]

    @app.get("/v1/devices/select")
    def select_device(capability: str = "", _=Depends(auth)):
        caps = [c for c in capability.split(",") if c]
        d = s["devices"].select(caps)
        return d.model_dump() if d else {"selected": None}

    # ---- Stage 14: Device Fabric API (dry-run routing; execution stays
    # on the existing /v1/exec + device-job paths) ----

    @app.get("/v1/fabric/devices")
    def fabric_list(_=Depends(auth)):
        return [v.as_dict() for v in s["fabric"].list_devices()]

    @app.get("/v1/fabric/devices/{device_id}")
    def fabric_get(device_id: str, _=Depends(auth)):
        v = s["fabric"].device_view(device_id)
        if v.trust == "unknown" and v.presence == "unknown":
            raise HTTPException(status_code=404, detail="unknown device")
        out = v.as_dict()
        out["capabilities_detail"] = [
            r.model_dump() for r in s["fabric"].capabilities_for(device_id)]
        out["grants"] = [g.model_dump()
                         for g in s["fabric"].list_grants(device_id)]
        out["prefs"] = s["fabric"].get_prefs(device_id)
        return out

    @app.post("/v1/fabric/discover")
    def fabric_discover(body: FabricDiscoverIn, _=Depends(auth)):
        try:
            return s["fabric"].discover(
                body.device_id, body.device_type, body.display_name,
                body.transport, body.metadata)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)[:300])

    @app.post("/v1/fabric/devices/{device_id}/restrict")
    def fabric_restrict(device_id: str, _=Depends(auth)):
        from .fabric import TrustViolation
        try:
            trust = s["fabric"].set_restricted(device_id, True)
        except TrustViolation as e:
            raise HTTPException(status_code=409, detail=str(e)[:300])
        return {"device_id": device_id, "trust": trust}

    @app.post("/v1/fabric/devices/{device_id}/unrestrict")
    def fabric_unrestrict(device_id: str, _=Depends(auth)):
        trust = s["fabric"].set_restricted(device_id, False)
        return {"device_id": device_id, "trust": trust}

    @app.post("/v1/fabric/route")
    def fabric_route(body: FabricRouteIn, _=Depends(auth)):
        r = s["fabric"].route_capability(
            body.capability, body.who, body.device_id or None)
        return {"device_id": r.device_id, "action": r.action,
                "reason": r.reason, "substituted": r.substituted,
                "checks": r.checks}

    @app.post("/v1/fabric/grants")
    def fabric_grant(body: FabricGrantIn, _=Depends(auth)):
        from .fabric import TrustViolation
        try:
            g = s["fabric"].create_grant(
                body.device_id, body.capabilities, body.max_risk,
                body.ttl_s)
        except (TrustViolation, ValueError) as e:
            raise HTTPException(status_code=409, detail=str(e)[:300])
        return g.model_dump()

    @app.get("/v1/fabric/grants")
    def fabric_grants(device_id: str = "", _=Depends(auth)):
        return [g.model_dump()
                for g in s["fabric"].list_grants(device_id)]

    @app.post("/v1/fabric/grants/{grant_id}/revoke")
    def fabric_grant_revoke(grant_id: str, _=Depends(auth)):
        if not s["fabric"].revoke_grant(grant_id):
            raise HTTPException(status_code=404, detail="unknown grant")
        return {"revoked": grant_id}

    @app.post("/v1/fabric/transfers")
    def fabric_transfer(body: FabricTransferIn, _=Depends(auth)):
        from .fabric import TrustViolation
        try:
            t = s["fabric"].create_transfer(
                body.source_device, body.dest_device, body.capability,
                body.metadata)
        except (TrustViolation, KeyError) as e:
            raise HTTPException(status_code=409, detail=str(e)[:300])
        return t.model_dump()

    @app.post("/v1/fabric/transfers/{transfer_id}/transition")
    def fabric_transfer_go(transfer_id: str, state: str = "",
                           verification: str = "", _=Depends(auth)):
        from .fabric import TrustViolation
        try:
            t = s["fabric"].transition_transfer(transfer_id, state,
                                               verification)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown transfer")
        except TrustViolation as e:
            raise HTTPException(status_code=409, detail=str(e)[:300])
        return t.model_dump()

    # ---- Stage 15: byte-transfer endpoints (operator) ----

    def _xfer_err(e: Exception):
        from .transfer import TransferRejected
        if isinstance(e, KeyError):
            raise HTTPException(status_code=404, detail="unknown transfer")
        if isinstance(e, TransferRejected):
            raise HTTPException(status_code=e.status, detail=str(e)[:300])
        raise HTTPException(status_code=400, detail=str(e)[:300])

    @app.post("/v1/fabric/xfer/request")
    def fabric_xfer_request(body: XferRequestIn, _=Depends(auth)):
        from .transfer import TransferRejected
        try:
            t = s["xfer"].request(
                body.sender_device, body.recipient_device, body.filename,
                body.size_bytes, body.sha256, body.content_type,
                body.grant_id, who="operator", metadata=body.metadata)
        except (TransferRejected, KeyError, ValueError) as e:
            _xfer_err(e)
        return t.model_dump()

    @app.get("/v1/fabric/xfer")
    def fabric_xfer_list(device_id: str = "", _=Depends(auth)):
        return [t.model_dump() for t in s["xfer"].list(device_id)]

    @app.get("/v1/fabric/xfer/{transfer_id}")
    def fabric_xfer_get(transfer_id: str, _=Depends(auth)):
        try:
            return s["xfer"].get(transfer_id).model_dump()
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown transfer")

    @app.post("/v1/fabric/xfer/{transfer_id}/cancel")
    def fabric_xfer_cancel(transfer_id: str, _=Depends(auth)):
        from .transfer import TransferRejected
        try:
            return s["xfer"].cancel(transfer_id, "operator").model_dump()
        except (TransferRejected, KeyError) as e:
            _xfer_err(e)

    @app.post("/v1/fabric/xfer/sweep")
    def fabric_xfer_sweep(_=Depends(auth)):
        return {"expired": s["xfer"].sweep()}

    # ---- Stage 16: capability discovery API (read-only; no execution,
    # no authorization granted — describe remains the only write path) ----

    @app.get("/v1/fabric/capabilities")
    def fabric_cap_index(_=Depends(auth)):
        """Fleet-wide capability index: capability -> devices with trust,
        presence, availability, version, staleness. Facts for matching;
        authorization still happens per-request at route/execute time."""
        out = []
        for v in s["fabric"].list_devices():
            for r in s["fabric"].capabilities_for(v.device_id):
                if r.capability_id.startswith("legacy:"):
                    continue
                out.append({
                    "capability": r.capability_id,
                    "device_id": v.device_id,
                    "trust": v.trust, "presence": v.presence,
                    "availability": r.availability,
                    "availability_reason": r.availability_reason,
                    "usable": r.usable(), "version": r.version,
                    "risk": r.risk.value,
                    "descriptor_version": r.descriptor_version,
                    "stale": r.stale, "advertised_at": r.advertised_at,
                    "source": r.source})
        return sorted(out, key=lambda e: (e["capability"], e["device_id"]))

    @app.post("/v1/fabric/resolve")
    def fabric_resolve(body: FabricResolveIn, _=Depends(auth)):
        """Deterministic resolution (Stage 17 resolver): ranked candidates
        with per-device trust/presence/availability/policy/governor
        verdicts, a terminal status, and the ladder rung required when
        nothing resolves. Read-only: no execution, no authorization
        granted. `capability` + `candidates` keys keep Stage 16 shape."""
        if not isinstance(body.constraints, dict) or \
                len(body.constraints) > 8:
            raise HTTPException(status_code=400,
                                detail="constraints must be an object (<=8)")
        r = s["resolver"].resolve(
            body.capability, body.device_id, body.who,
            body.constraints, body.preferred_device,
            body.allow_fallback)
        out = r.model_dump()
        # Stage 16-compatible candidate rows (superset: status/reason kept).
        out["candidates"] = [
            {**c, "authorization":
             ("approve" if c["approval_required"]
              else ("allow" if c["authorized"] else "deny"))}
            for c in out["candidates"]]
        return out

    @app.post("/v1/fabric/resolve/proposal")
    def fabric_resolve_proposal(body: dict, _=Depends(auth)):
        """Validate an untrusted LLM proposal. Security-claim fields in
        the proposal are IGNORED; Core derives everything. Read-only."""
        if not isinstance(body, dict) or len(body) > 20:
            raise HTTPException(status_code=400,
                                detail="proposal must be an object (<=20)")
        return s["resolver"].resolve_proposal(body).model_dump()

    @app.get("/v1/fabric/relationships")
    def fabric_relationships(_=Depends(auth)):
        """Explicit Core-controlled fallback/relationship metadata."""
        return s["resolver"].fallbacks.as_dict()

    @app.post("/v1/fabric/relationships")
    def fabric_relationship_add(body: FabricRelationshipIn,
                                _=Depends(auth)):
        """Register a fallback/relationship (operator only). Validated:
        known references, known type, no cycles, risk discipline.
        Metadata only — never authority, never executable."""
        from .resolver import RelationshipRejected
        try:
            out = s["resolver"].register_fallback(
                body.capability, body.related, body.relation)
        except RelationshipRejected as e:
            raise HTTPException(status_code=400, detail=str(e)[:300])
        s["audit"].record("operator", "capability_relationship",
                          body.capability,
                          f"{body.relation} -> {body.related}"[:300])
        return out

    @app.get("/v1/fabric/ladder")
    def fabric_ladder(_=Depends(auth)):
        """Escalation ladder representation: rung per known capability.
        GUI/HUMAN rungs are planning outputs, never executable."""
        from .resolver import EscalationLevel, level_of
        reg = s["registry"]
        levels = {lv.name: [] for lv in EscalationLevel}
        seen: set[str] = set()
        for v in s["fabric"].list_devices():
            for r in s["fabric"].capabilities_for(v.device_id):
                if r.capability_id.startswith("legacy:") or \
                        r.capability_id in seen:
                    continue
                seen.add(r.capability_id)
                levels[EscalationLevel.NATIVE.name].append(
                    r.capability_id)
        try:
            for d in reg.list():
                if d.name in seen:
                    continue
                seen.add(d.name)
                levels[level_of(d.name, reg).name].append(d.name)
        except Exception:  # noqa: BLE001 — registry unreadable
            pass
        return {"levels": {k: sorted(v) for k, v in levels.items()},
                "note": "GUI is future-only and HUMAN is terminal: "
                        "neither rung is executable."}

    # ---- Stage 18: natural-language intent (proposal only, never exec) ----

    @app.post("/v1/intent/parse")
    def intent_parse(body: IntentParseIn, _=Depends(auth)):
        """Parse an utterance into a grounded IntentProposal. Read-only:
        validates, grounds, and returns the proposal — never executes."""
        if not isinstance(body.utterance, str) or \
                len(body.utterance) > 2000:
            raise HTTPException(status_code=400,
                                detail="utterance must be text (<=2000)")
        from .intent import build_context
        try:
            proposal = s["intent"].parser.parse(
                body.utterance[:500],
                build_context(s["fabric"], body.requester_device))
        except Exception as e:  # noqa: BLE001 — parser failure is 400
            raise HTTPException(status_code=400, detail=str(e)[:300])
        return proposal.model_dump()

    @app.post("/v1/intent/turn")
    def intent_turn(body: IntentTurnIn, _=Depends(auth)):
        """Full intent turn: parse -> resolve -> message, executing safe
        resolved intents through the existing fabric execution path.
        Multi-step plans run step-by-step with per-step authorization;
        approval-gated or blocked steps stop the plan with an honest
        message (later steps never run speculatively)."""
        if not isinstance(body.utterance, str) or \
                len(body.utterance) > 2000:
            raise HTTPException(status_code=400,
                                detail="utterance must be text (<=2000)")
        out = s["intent"].handle_utterance(
            body.utterance[:2000], body.who or "user",
            body.requester_device or "")
        return out

    @app.post("/v1/intent/plan")
    def intent_plan(body: PlanIn, _=Depends(auth)):
        """Execute a STRUCTURED multi-step plan through the deterministic
        chain the NL path uses: per-step resolve -> policy -> approval
        holds -> fabric execution + verification, tracked as ONE mission.
        Typed result bindings ("fetch") let a step consume validated
        values from an earlier step's result; the registry schema and the
        per-step determinstic checks remain final. Bounded by the same
        MAX_STEPS / fetch limits as parsed plans."""
        from core.intent import (MAX_STEPS, MAX_UTTERANCE,
                                 IntentRejected, IntentProposal,
                                 build_context)
        steps = [st.model_dump() for st in (body.steps or [])]
        if not steps:
            raise HTTPException(status_code=400,
                                detail="plan needs at least one step")
        if len(steps) > MAX_STEPS:
            raise HTTPException(status_code=400,
                                detail=f"plan too long (max {MAX_STEPS})")
        proposal = IntentProposal(
            request_id=uuid.uuid4().hex[:12],
            utterance=(body.utterance or "structured plan")[:MAX_UTTERANCE],
            capability=steps[0]["capability"],
            preferred_device=steps[0].get("preferred_device", ""),
            steps=steps, confidence=0.9, needs_clarification=False,
            source="structured")
        try:
            proposal.validate_proposal()
        except IntentRejected as e:
            raise HTTPException(status_code=400,
                                detail=f"plan rejected: {e}") from e
        ctx = build_context(s["fabric"], body.requester_device or "")
        return s["intent"]._run_plan(
            proposal, ctx, body.who or "user", body.requester_device or "")

    # ---- Mission-bound single-approval messaging ----

    @app.post("/v1/messaging/send")
    def messaging_send(body: MessagingSendIn, _=Depends(auth)):
        """Present ONE approval for the whole send sequence. Read-only:
        executes nothing. The approval binds mission, recipient, exact
        text, device, and the action sequence; any change invalidates it.
        `snapshot` is optional: when supplied, the conversation is
        pre-verified; when absent, it is resolved from a fresh snapshot
        inside the approved execution (still one approval)."""
        if not isinstance(body.conversation, str) or not body.conversation:
            raise HTTPException(status_code=400,
                                detail="conversation is required")
        if not isinstance(body.text, str) or not body.text.strip():
            raise HTTPException(status_code=400, detail="text is required")
        return s["messaging"].request_send(
            snapshot=body.snapshot if isinstance(body.snapshot, dict) else {},
            conversation=body.conversation[:200], text=body.text[:500],
            device_id=body.device_id[:128], mission_id=body.mission_id[:64],
            who=body.who or "user")

    @app.post("/v1/messaging/approvals/{approval_id}/approve")
    def messaging_approve(approval_id: str, body: MessagingApproveIn,
                          _=Depends(auth)):
        """Approve and execute ONLY the bound plan: open conversation ->
        compose -> send -> verify, each through the existing fabric path.
        Reports succeeded / uncertain / failed / blocked / stale /
        cancelled / duplicate — never a fake success, never an auto-retry
        after a potentially-successful send."""
        return s["messaging"].approve(approval_id, body.who or "user")

    @app.post("/v1/messaging/approvals/{approval_id}/cancel")
    def messaging_cancel(approval_id: str, _=Depends(auth)):
        return s["messaging"].cancel(approval_id)

    @app.get("/v1/messaging/approvals/{approval_id}")
    def messaging_status(approval_id: str, _=Depends(auth)):
        out = s["messaging"].status(approval_id)
        if out.get("status") == "missing":
            raise HTTPException(status_code=404, detail="unknown approval")
        return out

    @app.get("/v1/messaging/approvals")
    def messaging_pending(_=Depends(auth)):
        """Redacted pending messaging approvals (companion surface):
        never plaintext, never the binding fingerprint; bounded list."""
        pend = s["messaging"].approvals.list_pending()
        return {"approvals": pend, "count": len(pend)}

    # ---- Stage 19: persistent conversation sessions (context only) ----

    def _get_session(session_id: str):
        if not isinstance(session_id, str) or not session_id or \
                len(session_id) > 64:
            raise HTTPException(status_code=400, detail="bad session_id")
        try:
            return s["session"].get_session(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown session")

    @app.post("/v1/session")
    def session_create(body: SessionCreateIn, _=Depends(auth)):
        try:
            sess = s["session"].create(body.who or "user",
                                       body.device_id or "")
        except Exception as e:  # noqa: BLE001
            from .session import SessionRejected
            raise HTTPException(status_code=400, detail=str(e)[:200])
        return sess.model_dump()

    @app.get("/v1/session/{session_id}")
    def session_get(session_id: str, _=Depends(auth)):
        return _get_session(session_id).model_dump()

    @app.post("/v1/session/{session_id}/turn")
    def session_turn(session_id: str, body: SessionTurnIn,
                     _=Depends(auth)):
        if not isinstance(body.utterance, str) or \
                len(body.utterance) > 2000:
            raise HTTPException(status_code=400,
                                detail="utterance must be text (<=2000)")
        sess = _get_session(session_id)
        return s["session"].turn(sess, body.utterance[:2000],
                                 body.who or "user")

    @app.post("/v1/session/{session_id}/close")
    def session_close(session_id: str, _=Depends(auth)):
        sess = _get_session(session_id)
        return s["session"].close(sess).model_dump()

    @app.get("/v1/fabric/devices/{device_id}/capabilities")
    def fabric_device_caps(device_id: str, _=Depends(auth)):
        """Validated descriptor snapshot for one device (advertised facts
        + live availability + staleness). Read-only; authorization stays
        per-request at route/execute time."""
        try:
            recs = s["fabric"].capabilities_for(device_id)
        except Exception:  # noqa: BLE001 — unknown device reads empty
            recs = []
        out = []
        for r in recs:
            if r.capability_id.startswith("legacy:"):
                continue
            out.append({
                "capability": r.capability_id, "version": r.version,
                "description": r.description,
                "risk": r.risk.value,
                "requires": list(r.required_permissions),
                "platforms": list(r.platforms), "execution": r.execution,
                "requires_foreground": r.requires_foreground,
                "requires_network": r.requires_network,
                "battery_sensitive": r.battery_sensitive,
                "supports_cancellation": r.supports_cancellation,
                "supports_streaming": r.supports_streaming,
                "timeout_s": r.timeout_s,
                "input_schema": r.input_schema,
                "output_schema": r.output_schema,
                "availability": r.availability,
                "availability_reason": r.availability_reason,
                "usable": r.usable(), "stale": r.stale,
                "advertised_at": r.advertised_at, "source": r.source,
                "aliases": list(r.aliases), "examples": list(r.examples),
                "keywords": list(r.keywords)})
        return sorted(out, key=lambda e: e["capability"])

    @app.get("/v1/fabric/devices/{device_id}/capabilities/{capability_id}")
    def fabric_device_cap_one(device_id: str, capability_id: str,
                              _=Depends(auth)):
        """Single validated descriptor. 404 when the device does not
        advertise it (supported-but-absent is not silently invented)."""
        for r in s["fabric"].capabilities_for(device_id):
            if r.capability_id == capability_id or r.name == capability_id:
                if r.capability_id.startswith("legacy:"):
                    break
                return {
                    "capability": r.capability_id, "version": r.version,
                    "description": r.description,
                    "risk": r.risk.value,
                    "availability": r.availability,
                    "availability_reason": r.availability_reason,
                    "usable": r.usable(), "stale": r.stale,
                    "advertised_at": r.advertised_at}
        raise HTTPException(status_code=404, detail="unknown capability")

    @app.post("/v1/fabric/prefs")
    def fabric_pref(body: FabricPrefIn, _=Depends(auth)):
        from .fabric import TrustViolation
        try:
            s["fabric"].set_pref(body.device_id, body.key, body.value)
        except TrustViolation as e:
            raise HTTPException(status_code=409, detail=str(e)[:300])
        return {"device_id": body.device_id, "key": body.key}

    @app.post("/v1/memory")
    def remember(item: MemoryItem, _=Depends(auth)):
        try:
            stored = s["memory"].remember(item)
        except ValueError as e:
            # Secret/credential refusal (never store) is a 422, not a 500.
            raise HTTPException(status_code=422, detail=str(e)[:300])
        return {"stored": stored is not None, "id": stored.id if stored else None}

    @app.get("/v1/memory/search")
    def search_memory(q: str, top_k: int = 5, _=Depends(auth)):
        return [{"id": m.id, "text": m.text, "category": m.category,
                 "score": sc}
                for m, sc in s["memory"].recall(q, top_k)]

    @app.get("/v1/memory/export")
    def export_memory(_=Depends(auth)):
        """Documented complete export of ordinary memory:
        `{"format": "zara-memory-export-1", "exported_at": <utc iso>,
        "items": [ {id, category, text, source, confidence, importance,
        metadata, created_at, updated_at, expires_at} ... sorted ] }.
        Secrets/refused credentials never appear (they never enter RAG
        memory); embeddings are derived data, excluded by design; a
        forgotten item is gone from every later export. This file is
        the user-visible truth of what Zara remembers."""
        out = s["memory"].export()
        out["exported_at"] = utcnow().isoformat()
        return out

    @app.get("/v1/events")
    def events(type: Optional[str] = None, limit: int = 50, _=Depends(auth)):
        return [e.model_dump() for e in s["bus"].history(type, limit)]

    # ---- Stage 20: connected-peripheral awareness (read-only) ----

    @app.get("/v1/peripherals")
    def peripherals_list(state: str = "present", _=Depends(auth)):
        reg = s["peripherals"]
        if state not in ("present", "removed", "all"):
            return {"error": "state must be present/removed/all"}
        return {"devices": [d.to_public() for d in reg.list(
            None if state == "all" else state)],
            "trust_note": "detection is not trust"}

    @app.post("/v1/peripherals/refresh")
    def peripherals_refresh(_=Depends(auth)):
        from .discovery import refresh_host
        diff, meta = refresh_host(s["peripherals"])
        return {"diff": diff, "meta": meta}

    @app.get("/v1/peripherals/events")
    def peripherals_events(limit: int = 50, _=Depends(auth)):
        return s["peripherals"].events(limit=limit)

    # ---- Stage 21: cross-device file-transfer missions -------------
    _tmr = s.get("transfer_runner")

    @app.post("/v1/transfer-missions", status_code=201)
    def transfer_missions_start(body: dict, _=Depends(auth)):
        from .transfer_missions import MissionSpecError as _MSE
        if _tmr is None:
            return {"error": "transfer missions not wired in this build"}
        spec = body.get("spec") if isinstance(body, dict) else None
        if spec is not None and not isinstance(spec, dict):
            from fastapi import HTTPException
            raise HTTPException(400, "spec must be an object")
        try:
            return _tmr.start(spec)
        except _MSE as e:
            from fastapi import HTTPException
            raise HTTPException(400, str(e)[:300])

    @app.get("/v1/transfer-missions")
    def transfer_missions_list(_=Depends(auth)):
        try:
            from .builtin_tools import transfer_status
            return transfer_status({}, {})
        except Exception as e:  # noqa: BLE001 — status is read-only
            return {"error": str(e)[:200]}

    @app.get("/v1/transfer-missions/{mission_id}")
    def transfer_mission_get(mission_id: str, _=Depends(auth)):
        try:
            return _tmr._missions.get(mission_id).model_dump(mode="json")
        except KeyError:
            return {"error": "unknown mission"}

    @app.post("/v1/transfer-missions/{mission_id}/resume")
    def transfer_mission_resume(mission_id: str, _=Depends(auth),
                                body: dict | None = None):
        if _tmr is None:
            return {"error": "transfer missions not wired in this build"}
        patch = (body or {}).get("spec_patch")
        if patch is not None and not isinstance(patch, dict):
            from fastapi import HTTPException
            raise HTTPException(400, "spec_patch must be an object")
        return _tmr.resume(mission_id, spec_patch=patch)

    @app.post("/v1/transfer-missions/{mission_id}/cancel")
    def transfer_mission_cancel(mission_id: str, _=Depends(auth)):
        if _tmr is None:
            return {"error": "transfer missions not wired in this build"}
        return _tmr.cancel(mission_id)


    @app.get("/v1/audit")
    def audit(limit: int = 50, _=Depends(auth)):
        return s["audit"].query(limit)

    @app.get("/v1/notifications")
    def notifications(_=Depends(auth)):
        return [n.model_dump() for n in s["notifs"].pending()]

    # ---- Stage 3: Zara reasoning loop, memory service, voice, wake ----

    @app.post("/v1/talk")
    def talk(body: TalkIn, _=Depends(auth)):
        """Full NL loop: context+memory -> LLM proposal -> core -> device ->
        verified result -> NL response. LLM never executes directly."""
        # Turn registered first so a concurrent POST /v1/talk/cancel can
        # abort it mid-flight; finished in finally (unknown ids stay put).
        tid, _ = s["loop"].turns.create()
        try:
            result = s["loop"].handle_text(body.text, session_id=body.session_id,
                                           device_id=body.device_id, who=body.who,
                                           turn_id=tid)
        finally:
            s["loop"].turns.finish(tid)
        return {"reply": result.reply, "status": result.status,
                "session_id": result.session_id or body.session_id,
                "mission_id": result.mission_id,
                "execution_id": result.execution_id,
                "device_id": result.device_id, "tool": result.tool,
                "trace_id": result.trace_id, "turn_id": result.turn_id or tid}

    @app.post("/v1/talk/cancel")
    def talk_cancel(body: TalkCancelIn, _=Depends(auth)):
        """Abort an in-flight turn: sets its event; the loop stops before
        the next model call or tool execution. Unknown ids are a safe
        no-op (false); already-finished turns report false."""
        return {"cancelled": s["loop"].turns.cancel(body.turn_id)}

    # ---- Live provider check (guarded diagnostic, Night 10) ----
    _diag_check = {"running": False, "event": None}
    _diag_lock = threading.Lock()

    @app.post("/v1/diagnostics/provider-check")
    def diag_provider_check(_=Depends(auth)):
        """One-tap live provider check. Explicit user action only — never
        automatic, never polled. Runs a single bounded non-streaming model
        call with a fixed innocuous prompt and NO tools: it cannot create
        missions, executions, memories, or device commands. Concurrent
        callers get 409 busy. Errors map to safe categories (no URLs,
        keys, or raw payloads). Mock providers (echo/scripted) get 422:
        configuration existing is not health."""
        import time as _time
        with _diag_lock:
            if _diag_check["running"]:
                raise HTTPException(status_code=409,
                                    detail="provider check already running")
            _diag_check["running"] = True
            ev = threading.Event()
            _diag_check["event"] = ev
        try:
            provider = s["model"]
            name = getattr(provider, "name", "unknown")
            if name in ("echo", "scripted", "mock"):
                raise HTTPException(
                    status_code=422,
                    detail=f"not a live provider: {name}")
            t0 = _time.time()
            try:
                out = provider.reason(
                    "You are a connectivity probe. Reply with exactly: "
                    "pinecone",
                    "Reply with exactly: pinecone", [], cancel=ev,
                    timeout=25.0)
            except LLMError as e:
                raise HTTPException(
                    status_code=504 if e.kind == LLMErrorKind.TIMEOUT
                    else 502,
                    detail=f"provider {e.kind.value}: safe failure, "
                    "nothing executed")
            elapsed = round(_time.time() - t0, 2)
            if not out.text:
                raise HTTPException(status_code=502,
                                    detail="provider empty: no content")
            return {"ok": True, "provider": name,
                    "model": getattr(provider, "model", ""),
                    "elapsed_s": elapsed}
        finally:
            with _diag_lock:
                _diag_check["running"] = False
                _diag_check["event"] = None

    @app.post("/v1/diagnostics/provider-check/cancel")
    def diag_provider_check_cancel(_=Depends(auth)):
        """Abort an in-flight provider check (releases promptly)."""
        with _diag_lock:
            ev = _diag_check.get("event")
            if ev is None:
                return {"cancelled": False}
            ev.set()
            return {"cancelled": True}

    @app.post("/v1/talk/stream")
    def talk_stream(body: TalkIn, _=Depends(auth)):
        """Streaming twin of /v1/talk: Server-Sent Events carrying turn,
        delta (real model text chunks), tool, approval, error, and done
        events (see core/talk_stream.py for the contract). Tool execution
        still flows exclusively through router + engine + approvals; the
        stream only narrates. Cancel by disconnecting (best-effort) or by
        POST /v1/talk/cancel with the turn_id from the first event."""
        q: queue.Queue = queue.Queue()
        tid, _ = s["loop"].turns.create()
        started = time.time()
        stop = object()

        def run() -> None:
            def emit(name: str, data: dict) -> None:
                try:
                    q.put(format_event(name, data))
                except Exception:  # noqa: BLE001 - emitter never breaks turn
                    pass
            try:
                s["loop"].handle_text(
                    body.text, session_id=body.session_id,
                    device_id=body.device_id, who=body.who, turn_id=tid,
                    emit=emit)
            except Exception as e:  # noqa: BLE001 - terminal error event
                try:
                    q.put(format_event("error", {"message": str(e)[:300]}))
                except Exception:  # noqa: BLE001
                    pass
            finally:
                s["loop"].turns.finish(tid)
                q.put(stop)

        threading.Thread(target=run, daemon=True).start()

        def gen():
            # Hard Stop: never hold a stream past the turn budget + margin.
            while time.time() - started < 150.0:
                try:
                    item = q.get(timeout=1.0)
                except Exception:  # noqa: BLE001 - idle heartbeat
                    yield ": ping\n\n"
                    continue
                if item is stop:
                    return
                try:
                    yield item
                except Exception:  # noqa: BLE001 - client went away
                    s["loop"].turns.cancel(tid)
                    return

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/v1/talk/resume")
    def talk_resume(body: ResumeIn, _=Depends(auth)):
        """Continue a turn held for approval after human approval."""
        tid, _ = s["loop"].turns.create()
        try:
            result = s["loop"].resume_after_approval(body.execution_id,
                                                     body.session_id,
                                                     turn_id=tid)
        finally:
            s["loop"].turns.finish(tid)
        return {"reply": result.reply, "status": result.status,
                "session_id": result.session_id or body.session_id,
                "mission_id": result.mission_id,
                "execution_id": result.execution_id,
                "turn_id": result.turn_id or tid}

    @app.post("/v1/sessions")
    def create_session(device_id: str = "cloud", _=Depends(auth)):
        session = s["sessions"].create(device_id=device_id)
        return session.model_dump()

    @app.get("/v1/sessions/{session_id}")
    def get_session(session_id: str, _=Depends(auth)):
        try:
            return s["sessions"].get(session_id).model_dump()
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown session")

    @app.get("/v1/sessions")
    def list_sessions(limit: int = 20, _=Depends(auth)):
        """Recent conversations, newest first (bounded, redacted)."""
        return s["sessions"].list_recent(limit=min(max(limit, 1), 100))

    @app.delete("/v1/sessions/{session_id}")
    def delete_session(session_id: str, _=Depends(auth)):
        if not s["sessions"].delete(session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        return {"deleted": session_id}

    @app.post("/v1/memory/correct")
    def memory_correct(body: MemoryCorrectIn, _=Depends(auth)):
        return s["memory_service"].correct(body.old_query, body.new_text)

    @app.delete("/v1/memory/{memory_id}")
    def memory_forget(memory_id: str, _=Depends(auth)):
        """Delete one memory (consent/retention). Unknown ids -> 404."""
        if not s["memory"].forget(memory_id):
            raise HTTPException(status_code=404, detail="unknown memory")
        return {"forgotten": memory_id}

    @app.post("/v1/memory/candidates/confirm")
    def memory_candidate_confirm(body: MemoryConfirmIn, _=Depends(auth)):
        """Accept (store) or reject (drop) a held memory candidate."""
        return s["memory_service"].confirm_candidate(body.candidate_id,
                                                     ok=body.ok)

    @app.get("/v1/memory/candidates")
    def memory_candidates(_=Depends(auth)):
        """Held memory candidates awaiting explicit confirmation. Text is
        redacted; confirm or reject via POST .../candidates/confirm."""
        from .tracing import redact as _redact
        out = []
        for cid, item in s["memory_service"].pending_candidates.items():
            out.append({"candidate_id": cid,
                        "text": _redact(item.text)[:300],
                        "category": item.category,
                        "source": item.source})
        return out

    @app.post("/v1/memory/purge")
    def memory_purge(_=Depends(auth)):
        """Delete expired memories (expires_at past). Returns count."""
        return {"purged": s["memory"].purge_expired()}

    @app.post("/v1/memory/{memory_id}/pin")
    def memory_pin(memory_id: str, body: MemoryPinIn, _=Depends(auth)):
        """Pin/unpin a memory against automatic deletion. Pinned rows
        survive sweeps and purges; explicit forget still removes them.
        Unknown ids -> 404."""
        if not s["memory"].set_pinned(memory_id, body.pinned):
            raise HTTPException(status_code=404, detail="unknown memory")
        return {"pinned": memory_id, "retained": body.pinned}

    @app.get("/v1/trace/{trace_id}")
    def get_trace(trace_id: str, _=Depends(auth)):
        return [{"name": sp.name, "latency_s": round(sp.latency_s, 3),
                 "attrs": sp.attrs}
                for sp in s["tracer"].for_trace(trace_id)]

    @app.post("/v1/admin/backup")
    def admin_backup(_=Depends(auth)):
        """Operator backup of file-backed state (memory/mission DBs)."""
        from .backup import backup_sqlite
        results = []
        for key, env in (("memory", "ZARA_MEMORY_DB"),
                         ("missions", "ZARA_MISSION_DB"),
                         ("audit", "ZARA_AUDIT_DB")):
            path = os.environ.get(env, "")
            if path and os.path.isfile(path):
                results.append({"store": key,
                                **backup_sqlite(path, "backups")})
        return {"backups": results}

    @app.post("/v1/voice/turn")
    def voice_turn(body: VoiceTurnIn, _=Depends(auth)):
        """Audio in -> STT -> core loop -> TTS. Mock providers by default;
        real providers plug in via configuration (Stage 3 docs)."""
        import base64
        try:
            audio = base64.b64decode(body.audio_base64) if body.audio_base64 \
                else b"mock-audio"
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=422, detail="invalid audio_base64")
        if len(audio) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="audio too large")
        out = s["voice"].handle_audio(audio, session_id=body.session_id,
                                      device_id=body.device_id, who=body.who)
        return out

    @app.post("/v1/voice/interrupt")
    def voice_interrupt(_=Depends(auth)):
        return s["voice"].interrupt()

    @app.get("/v1/wake")
    def wake_status(_=Depends(auth)):
        w = s["wake"]
        out = {"phrase": w.config.phrase, "running": w.running,
               "paused": w.paused, "detections": w.detections,
               "pause_below_pct": w.config.pause_below_pct}
        out["availability"] = w.availability()
        return out

    @app.post("/v1/wake/event")
    def wake_event(body: WakeEventIn, _=Depends(auth)):
        """Deterministic mock detection path. Physical audio validation
        remains pending — see docs/WAKE_WORD.md."""
        from .voice import WAKE_PHRASE
        if body.phrase != WAKE_PHRASE:
            raise HTTPException(status_code=422, detail="unknown wake phrase")
        try:
            return s["wake"].simulate_detection()
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @app.post("/v1/wake/battery")
    def wake_battery(body: WakeBatteryIn, _=Depends(auth)):
        return {"state": s["wake"].battery_update(body.battery_pct,
                                                  body.charging,
                                                  body.power_save)}

    # ---- Stage 2: device protocol (additive) ----

    async def agent_auth(request: Request,
                         x_device_id: Optional[str] = Header(None),
                         x_device_key: Optional[str] = Header(None),
                         x_device_nonce: Optional[str] = Header(None),
                         x_device_proof: Optional[str] = Header(None)):
        """Two additive modes, same trust outcome:
        - raw key (unchanged default): X-Device-Id + X-Device-Key,
        - challenge proof (replay-resistant): X-Device-Id + a one-time
          server nonce + an Ed25519 signature from the device's
          registered key over the canonical message
          v2: "{METHOD} {path} {nonce} {sha256_hex(body)}"
          v1 (compat fallback): "{METHOD} {path} {nonce}"
          The body hash binds the proof to exact request bytes; the path
          excludes any query string. The raw key itself never travels on
          these calls. Mixing both credential styles in one request is
          refused (ambiguous, not weaker)."""
        if not x_device_id:
            raise HTTPException(status_code=401,
                                detail="device credentials required")
        if x_device_proof and x_device_key:
            raise HTTPException(
                status_code=401, detail="ambiguous device credentials: "
                "send either X-Device-Key or a challenge proof, not both")
        if x_device_proof or x_device_nonce:
            if not s["device_auth"].challenge_ready(x_device_id):
                raise HTTPException(
                    status_code=403,
                    detail="challenge auth unavailable for this device "
                           "(no signing key bound): use X-Device-Key once, "
                           "then POST /v1/agent/pubkey")
            if not (x_device_proof and x_device_nonce):
                raise HTTPException(status_code=401,
                                    detail="challenge proof incomplete")
            if not s["auth_challenges"].consume(x_device_id,
                                                x_device_nonce):
                raise HTTPException(
                    status_code=401,
                    detail="challenge expired, used, or unknown: request "
                           "a fresh one (POST /v1/agent/challenge)")
            # Consume happens BEFORE proof verification: fail-closed —
            # a bad proof still burns the nonce, so an attacker (or a
            # buggy client) can never retry the same one.
            raw_body = await request.body()
            body_sha = hashlib.sha256(raw_body).hexdigest()
            device_auth = s["device_auth"]
            msg_v2 = f"{request.method} {request.url.path} " \
                     f"{x_device_nonce} {body_sha}"
            msg_v1 = f"{request.method} {request.url.path} {x_device_nonce}"
            verified = (device_auth.verify_challenge(
                            x_device_id, x_device_nonce, x_device_proof,
                            msg_v2)
                        or device_auth.verify_challenge(
                            x_device_id, x_device_nonce, x_device_proof,
                            msg_v1))
            if not verified:
                raise HTTPException(
                    status_code=403, detail="invalid challenge proof")
            return x_device_id
        if not x_device_key:
            raise HTTPException(status_code=401,
                                detail="device credentials required")
        if not s["device_auth"].verify(x_device_id, x_device_key):
            raise HTTPException(status_code=403,
                                detail="invalid/revoked device identity")
        return x_device_id

    @app.post("/v1/agent/challenge")
    def agent_challenge(device_id: str = Depends(agent_auth)):
        """Mint ONE single-use auth challenge (the nonce consumed by this
        request's own proof) plus a chained SPARE the device can use to
        fetch the NEXT challenge by proof — so the raw device key needs
        to travel exactly once per process lifetime (or again only after
        expiry/revocation/restart). Returns {"nonce", "next_nonce"}; the
        calls sign f"{METHOD} {path} {nonce} {sha256(body)}"
        (v2 canonical; "{METHOD} {path} {nonce}" v1 still accepted for
        compatibility). Path binding scoping plus the body-hash binding
        (v2) make each nonce valid for exactly one request shape."""
        if not s["device_auth"].challenge_ready(device_id):
            raise HTTPException(
                status_code=409,
                detail="no signing key bound for challenge auth: "
                       "POST /v1/agent/pubkey first")
        nonce = s["auth_challenges"].mint(device_id)
        s["audit"].record("device", "auth_challenge", device_id, "")
        return {"device_id": device_id, "nonce": nonce,
                "next_nonce": s["auth_challenges"].mint(device_id),
                "ttl_s": s["auth_challenges"].TTL_S,
                "message_format":
                    "{METHOD} {path} {nonce} {body_sha256_hex}"}

    @app.post("/v1/agent/pubkey")
    def agent_pubkey(device_id: str = Depends(agent_auth),
                     x_device_key: Optional[str] = Header(None),
                     body: PubkeyIn = None):
        """Bind (or upgrade to) the device's Ed25519 challenge-signing
        key. Raw-key auth is REQUIRED for this binding — a proof cannot
        prove an unregistered key — so possession of the secret device
        key authorizes the binding, exactly like every raw-key request."""
        if device_id and x_device_key is None:
            raise HTTPException(
                status_code=401,
                detail="pubkey binding requires raw-key auth (the "
                       "parameter proves possession of the device key)")
        try:
            ok = s["device_auth"].set_pubkey(device_id, body.pubkey or "")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if not ok:
            raise HTTPException(status_code=403,
                                detail="unknown/revoked/expired device")
        s["audit"].record("device", "pubkey_bound", device_id, "")
        return {"device_id": device_id, "challenge_auth": bool(body.pubkey)}

    @app.post("/v1/agent/enroll")
    def enroll(body: EnrollIn, _=Depends(auth)):
        """Operator step: returns a one-time pairing code (shown once)."""
        try:
            code = s["device_auth"].enroll(body.device_id, body.kind)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        s["audit"].record("operator", "device_enroll", body.device_id, "")
        return {"device_id": body.device_id, "pairing_code": code}

    @app.post("/v1/agent/claim")
    def claim(body: ClaimIn):
        """Device step: exchanges pairing code for a device key (once)."""
        try:
            device_id, key = s["device_auth"].claim(body.pairing_code,
                                                    pubkey=body.pubkey)
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))
        # Stage 14: a FRESH credential is an explicit re-pair — it lifts
        # the revocation tombstone (old keys stay dead; only the new key
        # verifies). Trust derives from the new credential, never restored.
        try:
            s["fabric"].clear_revocation(device_id)
        except Exception:  # noqa: BLE001 — claim already succeeded
            pass
        s["audit"].record("device", "device_claim", device_id, "")
        return {"device_id": device_id, "device_key": key}

    @app.post("/v1/agent/rotate")
    def agent_rotate(device_id: str = Depends(agent_auth),
                     x_device_key: Optional[str] = Header(None)):
        """Credential rotation: caller proves the current key, receives a new
        one (once). The old key dies immediately."""
        try:
            new_key = s["device_auth"].rotate(device_id, x_device_key or "")
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))
        s["audit"].record("device", "credential_rotated", device_id, "")
        return {"device_id": device_id, "device_key": new_key}

    @app.post("/v1/agent/register")
    def agent_register(body: AgentRegisterIn,
                       device_id: str = Depends(agent_auth)):
        s["devices"].register(DeviceState(
            device_id=device_id, kind=body.kind,
            capabilities=body.capabilities, online=True, status="online",
            software_version=body.software_version))
        # Stage 16: re-registration marks the old descriptor snapshot
        # stale (routing still works; resolve reports staleness). The
        # device clears it by describing — no re-pair needed.
        try:
            s["fabric"].mark_stale(device_id)
        except Exception:  # noqa: BLE001 — register already done
            pass
        s["audit"].record("device", "device_register", device_id,
                          f"{len(body.capabilities)} caps")
        return {"registered": device_id, "capabilities": body.capabilities}

    @app.post("/v1/agent/heartbeat")
    def agent_heartbeat(body: HeartbeatPayload, device_id: str = Depends(agent_auth)):
        try:
            d = s["devices"].heartbeat(
                device_id, battery_pct=body.battery_pct,
                charging=body.charging,
                network=body.network, online=body.online,
                cpu_pct=body.cpu_pct, mem_pct=body.mem_pct,
                low_power_mode=body.low_power_mode,
                storage_free_mb=body.storage_free_mb,
                storage_total_mb=body.storage_total_mb,
                battery_temp_c=body.battery_temp_c,
                thermal_state=body.thermal_state)
        except KeyError:
            raise HTTPException(status_code=404, detail="device not registered")
        if (body.battery_pct is not None and body.battery_pct < 15
                and not body.charging):
            s["devices"].set_status(device_id, "degraded")
            d = s["devices"].get(device_id)
        elif d.status == "degraded" and (
                body.battery_pct is None or body.battery_pct >= 25):
            # recovery: healthy readings clear the degraded flag
            s["devices"].set_status(device_id, "online")
            d = s["devices"].get(device_id)
        return {"ok": True, "status": d.status}

    @app.post("/v1/agent/capabilities")
    def agent_capabilities(body: CapabilityUpdate,
                           device_id: str = Depends(agent_auth)):
        try:
            d = s["devices"].update_capabilities(device_id, body.capabilities)
        except KeyError:
            raise HTTPException(status_code=404, detail="device not registered")
        s["audit"].record("device", "capability_update", device_id,
                          ",".join(body.capabilities)[:300])
        return {"capabilities": d.capabilities}

    @app.post("/v1/agent/jobs/poll")
    def agent_poll(device_id: str = Depends(agent_auth),
                   wait_secs: float = Query(default=0.0, ge=0.0,
                                            le=AGENT_JOB_LONGPOLL_MAX_S)):
        """Optional bounded server-side long poll (opt-in via wait_secs;
        default 0 keeps the previous always-instant contract). Devices
        that pass wait_secs get sub-second claim dispatch while idle
        request rate stays ~1 per wait window (battery-neutral)."""
        try:
            s["devices"].heartbeat(device_id)  # poll counts as presence
        except KeyError:
            raise HTTPException(status_code=404, detail="device not registered")
        if wait_secs > 0:
            job = s["jobs"].wait_for_job(device_id, wait_secs)
        else:
            job = s["jobs"].poll(device_id)
        if job is None:
            return {"job": None}
        # Linearize revoke vs claim: revoke may land between agent_auth
        # verify and poll. A revoked device must not receive a payload —
        # cancel the just-claimed job (claimed→cancelled wakes the proxy
        # waiter) and fail closed. Result-time verify remains the backstop.
        if s["device_auth"].is_revoked(device_id):
            try:
                s["jobs"].cancel(job.id)
            except Exception:  # noqa: BLE001 — revocation already done
                pass
            raise HTTPException(status_code=403,
                                detail="invalid/revoked device identity")
        if s["jobs"].is_cancelled(job.id):
            return {"job": None}
        return {"job": {"job_id": job.id, "tool": job.tool,
                        "inputs": job.inputs, "timeout_s": job.timeout_s,
                        "execution_id": job.execution_id,
                        "mission_id": job.mission_id}}

    @app.post("/v1/agent/jobs/result")
    def agent_result(body: JobResultIn, device_id: str = Depends(agent_auth)):
        try:
            job = s["jobs"].get(body.job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown job")
        # Device binding: a device may only complete its own jobs. Without
        # this, any authenticated device could forge another device's
        # completion (fail-closed 403 + audit, mirroring approvals).
        if job.device_id != device_id:
            s["audit"].record("device", "job_result_scope_denied", body.job_id,
                              f"{device_id} != {job.device_id}", ok=False)
            raise HTTPException(status_code=403,
                                detail="job not targeted at this device")
        # complete_once() decides first-delivery ATOMICALLY with the
        # open->terminal transition (same lock): concurrent duplicates
        # cannot both observe True, so exactly one delivery emits bus +
        # audit. Emission itself stays outside the queue lock (no I/O
        # under lock); duplicates still return idempotent {"ok": True}.
        job, first_delivery = s["jobs"].complete_once(
            body.job_id, body.ok, body.result, body.error)
        if first_delivery:
            s["bus"].publish("execution_finished", source="device",
                             device_id=device_id,
                             payload={"job_id": body.job_id, "ok": body.ok,
                                      "error": body.error[:300]})
            s["audit"].record("device", "job_result", body.job_id,
                              f"{job.tool} ok={body.ok}", ok=body.ok)
        return {"ok": True}

    @app.post("/v1/agent/jobs/{job_id}/cancel")
    def agent_cancel(job_id: str, _=Depends(auth)):
        return {"cancelled": s["jobs"].cancel(job_id)}

    @app.get("/v1/agent/jobs/{job_id}/state")
    def agent_job_state(job_id: str, device_id: str = Depends(agent_auth)):
        """Owner-only job state read so a device holding a job can notice
        server-side cancellation (long tools, revocation sweeps). Unknown
        ids, other devices' jobs, and pruned jobs all read as 404 with the
        same detail — no cross-device probing."""
        state = s["jobs"].job_state_for(device_id, job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {"job_id": job_id, "state": state}

    @app.post("/v1/agent/events/sync")
    def agent_sync(body: dict, device_id: str = Depends(agent_auth)):
        for ev in body.get("events", [])[:200]:
            s["bus"].publish("device_event", source="device",
                             device_id=device_id, payload=ev)
        return {"accepted": len(body.get("events", [])[:200])}

    @app.post("/v1/agent/disconnect")
    def agent_disconnect(device_id: str = Depends(agent_auth)):
        try:
            s["devices"].mark_offline(device_id)
        except KeyError:
            pass
        s["audit"].record("device", "device_disconnect", device_id, "")
        return {"ok": True}

    @app.post("/v1/agent/revoke")
    def agent_revoke(device_id: str, _=Depends(auth)):
        s["device_auth"].revoke(device_id)
        # Linearize revocation: cancel queued jobs FIRST, before presence /
        # tombstone / transfer steps, so no poll/result slips between steps
        # and observes mixed state. Pending-only; claimed jobs stay
        # device-owned (result endpoint still auth-gates them).
        try:
            n_queued = s["jobs"].cancel_device_jobs(device_id)
        except Exception:  # noqa: BLE001 — revoke itself already done
            n_queued = 0
        try:
            s["devices"].mark_offline(device_id)
        except KeyError:
            pass
        # Stage 14: fabric tombstone (revocation survives Core restart).
        try:
            s["fabric"].revoke_device(device_id, "operator revoke")
        except Exception:  # noqa: BLE001 — revoke itself already done
            pass
        # Stage 15: eager transfer cancel (revoked endpoints never move
        # again; chunk-time trust checks remain as defense in depth).
        try:
            s["xfer"].revoke_device(device_id)
        except Exception:  # noqa: BLE001 — revoke itself already done
            pass
        s["audit"].record("operator", "device_revoke", device_id,
                           f"queued_jobs_cancelled={n_queued}")
        return {"revoked": device_id, "queued_jobs_cancelled": n_queued}

    # ---- Stage 10: device voice turn (same bounds as operator turn,
    # device-scoped auth; the operator endpoint is unchanged).

    @app.post("/v1/agent/capabilities/describe")
    def agent_capabilities_describe(body: dict,
                                   device_id: str = Depends(agent_auth)):
        """Device-authenticated capability advertisement with schemas.
        Strict Stage 16 validation: versioned descriptors, bounded,
        no authority fields. Identity comes ONLY from auth (passed as
        the device_id parameter); documents carrying their own device_id
        are rejected outright."""
        recs = body.get("records", []) if isinstance(body, dict) else []
        if not isinstance(recs, list) or len(recs) > 100:
            raise HTTPException(status_code=400,
                                detail="records must be a list (<=100)")
        return s["fabric"].advertise(recs, device_id)

    # ---- Stage 15: device byte-transfer endpoints (identity from auth;
    # sender/recipient binding enforced by the engine, never the body) ----

    @app.post("/v1/agent/xfer/request")
    def agent_xfer_request(body: XferRequestIn,
                           device_id: str = Depends(agent_auth)):
        from .transfer import TransferRejected
        try:
            t = s["xfer"].request(
                device_id, body.recipient_device, body.filename,
                body.size_bytes, body.sha256, body.content_type,
                body.grant_id, who=f"device:{device_id}",
                metadata=body.metadata)
        except (TransferRejected, KeyError, ValueError) as e:
            _xfer_err(e)
        return t.model_dump()

    @app.post("/v1/agent/xfer/{transfer_id}/chunk")
    def agent_xfer_chunk(transfer_id: str, body: XferChunkIn,
                         device_id: str = Depends(agent_auth)):
        import base64 as _b64
        from .transfer import MAX_CHUNK_BYTES, TransferRejected
        if len(body.data_base64) > MAX_CHUNK_BYTES // 3 * 4 + 128:
            raise HTTPException(status_code=400, detail="chunk too large")
        try:
            data = _b64.b64decode(body.data_base64, validate=True)
        except Exception:  # noqa: BLE001 — malformed base64, not bytes
            raise HTTPException(status_code=400, detail="bad base64")
        try:
            return s["xfer"].post_chunk(
                transfer_id, device_id, body.seq, data).model_dump()
        except (TransferRejected, KeyError) as e:
            _xfer_err(e)

    @app.get("/v1/agent/xfer/pending")
    def agent_xfer_pending(device_id: str = Depends(agent_auth)):
        return s["xfer"].pending_for(device_id)

    @app.get("/v1/agent/xfer/{transfer_id}/bytes")
    def agent_xfer_bytes(transfer_id: str, offset: int = 0,
                         length: int = 65536,
                         device_id: str = Depends(agent_auth)):
        import base64 as _b64
        from .transfer import TransferRejected
        try:
            data, rec = s["xfer"].read(transfer_id, device_id,
                                       offset, length)
        except (TransferRejected, KeyError) as e:
            _xfer_err(e)
        return {"transfer_id": transfer_id, "offset": offset,
                "length": len(data), "size_bytes": rec.size_bytes,
                "sha256": rec.sha256,
                "data_base64": _b64.b64encode(data).decode()}

    @app.post("/v1/agent/xfer/{transfer_id}/ack")
    def agent_xfer_ack(transfer_id: str, body: XferAckIn,
                       device_id: str = Depends(agent_auth)):
        from .transfer import TransferRejected
        try:
            ok, rec = s["xfer"].ack(transfer_id, device_id, body.sha256)
        except (TransferRejected, KeyError) as e:
            _xfer_err(e)
        return {"transfer_id": transfer_id, "verified": ok,
                "state": rec.state}

    @app.post("/v1/agent/xfer/{transfer_id}/cancel")
    def agent_xfer_cancel(transfer_id: str,
                          device_id: str = Depends(agent_auth)):
        from .transfer import TransferRejected
        try:
            # Sender, recipient, or operator path: the engine records who.
            rec = s["xfer"].get(transfer_id)
            if device_id not in (rec.source_device, rec.dest_device):
                raise TransferRejected("only transfer parties may cancel")
            return s["xfer"].cancel(
                transfer_id, f"device:{device_id}").model_dump()
        except (TransferRejected, KeyError) as e:
            _xfer_err(e)

    @app.post("/v1/agent/voice/turn")
    def agent_voice_turn(body: VoiceTurnIn,
                         device_id: str = Depends(agent_auth)):
        import base64 as _b64
        try:
            audio = _b64.b64decode(body.audio_base64) if body.audio_base64 \
                else b"mock-audio"
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=422, detail="invalid audio_base64")
        if len(audio) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="audio too large")
        return s["voice"].handle_audio(audio,
                                       session_id=body.session_id,
                                       device_id=device_id, who=body.who)

    @app.post("/v1/agent/voice/interrupt")
    def agent_voice_interrupt(device_id: str = Depends(agent_auth)):
        s["audit"].record(device_id, "voice_interrupt", "", "")
        return s["voice"].interrupt()

    # ---- Stage 10: device TTS (unprivileged synthesis for paired bodies).
    # Text -> provider WAV. Bounded like the provider itself; device-scoped
    # auth; no policy implications (synthesis is not execution).

    @app.post("/v1/agent/tts")
    def agent_tts(body: TTSIn, device_id: str = Depends(agent_auth)):
        import base64 as _b64
        text = (body.text or "")[:2000]
        if not text.strip():
            raise HTTPException(status_code=400, detail="empty text")
        try:
            wav = s["voice"].tts.speak(text)
        except Exception as e:  # noqa: BLE001 — provider failure is 502
            raise HTTPException(status_code=502,
                                detail=f"tts failed: {str(e)[:200]}")
        s["audit"].record(device_id, "agent_tts", body.session_id,
                          f"{len(wav)}B")
        return {"audio_base64": _b64.b64encode(wav).decode(),
                "bytes": len(wav),
                "provider": getattr(s["voice"].tts, "name", "unknown")}

    # ---- Stage 9: device notifications pull/ack (poll fallback for push) ----

    @app.post("/v1/agent/notifications")
    def agent_notifications(device_id: str = Depends(agent_auth)):
        """Pending notifications for THIS device (broadcast + addressed).
        Bodies truncated; secrets never belong in notifications."""
        out = []
        for n in s["notifs"].pending(device_id):
            out.append({"id": n.id, "title": n.title[:120],
                        "body": (n.body or "")[:500],
                        "mission_id": n.mission_id,
                        "execution_id": n.execution_id,
                        "created_at": (n.created_at.isoformat()
                                      if hasattr(n.created_at, "isoformat")
                                      else str(n.created_at))})
        return {"notifications": out}

    @app.post("/v1/agent/notifications/ack")
    def agent_notifications_ack(body: NotifAckIn,
                                device_id: str = Depends(agent_auth)):
        """Acknowledge delivery. Only own or broadcast notifications."""
        found = None
        for n in s["notifs"].pending(None):
            if n.id == body.notification_id and n.device_id in (None, device_id):
                found = n
                break
        if found is None:
            raise HTTPException(status_code=404,
                                detail="unknown notification for this device")
        s["notifs"].mark_delivered(found.id)
        return {"acked": found.id}

    # ---- Stage 9: push registration (mock transport; FCM deferred) ----

    @app.post("/v1/agent/push/register")
    def agent_push_register(body: PushRegisterIn,
                            device_id: str = Depends(agent_auth)):
        try:
            s["push"].register(device_id, body.token, body.provider)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        s["audit"].record("device", "push_register", device_id, body.provider)
        return {"registered": device_id, "provider": body.provider}

    @app.post("/v1/agent/push/invalidate")
    def agent_push_invalidate(device_id: str = Depends(agent_auth)):
        s["push"].invalidate(device_id)
        try:
            s["devices"].mark_offline(device_id)
        except KeyError:
            pass
        return {"invalidated": device_id}

    # ---- Stage 9: device-scoped approval (same human, phone surface) ----
    # A device may approve/deny ONLY executions targeted at itself.
    # Cross-device approvals stay operator-only. Both paths audited.

    @app.post("/v1/agent/approvals/{exec_id}")
    def agent_approve(exec_id: str, body: ApprovalIn,
                      device_id: str = Depends(agent_auth)):
        if body.decision not in ("approve", "deny"):
            raise HTTPException(status_code=400,
                                detail="decision must be approve|deny")
        try:
            rec = s["engine"].get(exec_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown execution")
        if rec.device_id != device_id:
            s["audit"].record(device_id, "approval_scope_denied", exec_id,
                              f"target={rec.device_id}", ok=False)
            raise HTTPException(status_code=403,
                                detail="execution not targeted at this device")
        try:
            if body.decision == "deny":
                rec = s["engine"].cancel(exec_id)
            else:
                rec = s["engine"].approve(exec_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        s["audit"].record(device_id, "approval_" + body.decision, exec_id,
                          rec.state.value)
        return {"id": rec.id, "status": rec.state.value}

    @app.post("/v1/dispatch")
    def dispatch(body: DispatchIn, _=Depends(auth)):
        """Core-decided routing: capability -> device -> execution -> verify."""
        decision = s["router"].route(body.tool, body.who)
        if decision.action != "route":
            s["audit"].record(body.who, "dispatch_" + decision.action,
                              body.tool, decision.reason, ok=False)
            return {"status": decision.action, "reason": decision.reason}
        try:
            rec = s["engine"].submit(body.tool, body.inputs, decision.device_id,
                                     who=body.who, mission_id=body.mission_id)
        except PolicyDenied as e:
            s["audit"].record(body.who, "dispatch_deny", body.tool,
                              str(e)[:300], ok=False)
            return {"status": "deny", "reason": str(e)[:300]}
        except ValueError as e:
            # Task 9: input-envelope rejection before any record exists.
            s["audit"].record(body.who, "dispatch_reject", body.tool,
                              str(e)[:300], ok=False)
            raise HTTPException(status_code=422, detail=str(e)[:300])
        s["audit"].record(body.who, "dispatch_route", body.tool,
                          f"{decision.device_id} {rec.state.value}")
        return {"id": rec.id, "status": rec.state.value,
                "device_id": decision.device_id, "result": rec.result,
                "error": rec.error, "verified": rec.verified}

    @app.websocket("/v1/stream")
    async def stream(ws: WebSocket):
        """Authenticated realtime event delivery. Read-only transport:
        ping/ack only — NO action may be requested over this socket.
        Bounded per-connection queue (100, drop-oldest + counter),
        server heartbeat, ?since= replay for reconnects."""
        import asyncio
        await ws.accept()
        params = dict(ws.query_params)
        authed = False
        identity = "unknown"
        if params.get("token") == TOKEN:
            authed, identity = True, "operator"
        elif params.get("device_id") and params.get("device_key"):
            if s["device_auth"].verify(params["device_id"],
                                       params["device_key"]):
                authed, identity = True, f"device:{params['device_id']}"
        if not authed:
            await ws.close(code=4401)
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        dropped = 0

        def _push(item: dict) -> None:
            nonlocal dropped
            try:
                queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                pass
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                dropped += 1
                return
            dropped += 1
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                dropped += 1

        def cb(ev) -> None:
            loop.call_soon_threadsafe(_push, ev.model_dump(mode="json"))

        s["bus"].subscribe("*", cb)
        try:
            await ws.send_json({"type": "welcome", "identity": identity,
                                "dropped": 0})
            since = params.get("since")
            if since:
                seen = False
                for ev in s["bus"].history(limit=200):
                    if seen:
                        await ws.send_json(ev.model_dump(mode="json"))
                    if ev.id == since:
                        seen = True
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                    await ws.send_json(msg)
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "heartbeat",
                                        "dropped": dropped})
                # drain any client frames without blocking the event flow
                try:
                    while True:
                        client = await asyncio.wait_for(ws.receive_json(),
                                                        timeout=0.01)
                        if client.get("type") in ("ping", "ack"):
                            continue
                        await ws.send_json(
                            {"type": "error",
                             "error": "only ping/ack accepted; "
                                      "actions are never taken over events"})
                except asyncio.TimeoutError:
                    pass
        except Exception:  # noqa: BLE001 — disconnects end the stream
            pass
        finally:
            s["bus"].unsubscribe("*", cb)

    return app


def _db_path(name: str, default: str = "") -> str:
    """Env DB path only if its directory exists (docker .env works locally
    by falling back instead of crashing the import)."""
    path = os.environ.get(name, default)
    if not path or path == ":memory:":
        return path
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        print(f"{name}={path} unreachable; using {default or 'in-memory'}")
        return default
    return path


def _stack_from_env():
    if not os.environ.get("ASSISTANT_TOKEN"):
        # Startup validation (production path only — tests build stacks
        # directly): operator auth wide open must be unmissable.
        print("ZARA WARNING: ASSISTANT_TOKEN unset — using default "
              "dev-token; set a long random value before exposing this "
              "service. See docs/OCI_BUDGET.md.", flush=True)
    return build_stack(memory_db=_db_path("ZARA_MEMORY_DB"),
                       audit_db=_db_path("ZARA_AUDIT_DB", ":memory:"),
                       mission_db=_db_path("ZARA_MISSION_DB"),
                       device_auth_db=_db_path("ZARA_DEVICE_AUTH_DB"),
                       scheduler_db=_db_path("ZARA_SCHEDULER_DB"))


app = create_app(_stack_from_env())
