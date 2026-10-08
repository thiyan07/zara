"""FastAPI gateway: REST + WebSocket. Device-token gate (Stage 2: per-device auth)."""
from __future__ import annotations
import os
from typing import Optional
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from pydantic import BaseModel

from .audit import AuditLog
from .builtin_tools import BUILTINS
from .context import ContextManager
from .conversation import ConversationLoop
from .device_auth import DeviceAuthStore
from .device_tools import register_device_proxies
from .devices import DeviceManager
from .events import EventBus
from .execution import ExecutionEngine, PolicyDenied
from .governor import ResourceGovernor, ResourceSnapshot
from .jobs import JobQueue
from .memory import MemoryStore, PersistentMemoryStore
from .memory_policy import MemoryService
from .missions import MissionEngine, MissionState
from .models import DeviceKind, DeviceState, MemoryItem
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


class ChatIn(BaseModel):
    text: str
    device_id: str = "cloud"


class ExecIn(BaseModel):
    tool: str
    inputs: dict = {}
    device_id: str = "cloud"
    mission_id: Optional[str] = None


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


class AgentRegisterIn(BaseModel):
    capabilities: list[str]
    software_version: str = ""
    kind: DeviceKind = DeviceKind.LINUX


class JobResultIn(BaseModel):
    job_id: str
    ok: bool
    result: dict = {}
    error: str = ""


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
                mission_db: str = "", fabric_db: str = ""):
    bus = EventBus()
    audit = AuditLog(audit_db)
    policy = PolicyEngine()
    registry = ToolRegistry()
    for definition, handler in BUILTINS:
        registry.register(definition, handler)
    jobs = JobQueue()
    register_device_proxies(registry, jobs)
    from .opencode_tools import OPENCODE_TOOLS as _OT
    for _d, _h in _OT:
        registry.register(_d, _h)
    from .browser_tools import BROWSER_TOOLS as _BT
    for _d, _h in _BT:
        registry.register(_d, _h)
    device_auth = DeviceAuthStore()
    events_log: list[dict] = []
    bus.subscribe("*", lambda ev: events_log.append(ev.model_dump()))
    engine = ExecutionEngine(registry, policy,
                             on_event=lambda t, p: bus.publish(t, source="execution", payload=p))
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
    sched = Scheduler()
    notifs = NotificationManager(on_event=lambda t, p: bus.publish(t, source="notifications", payload=p))
    from .push import PushRegistry as _PushRegistry
    push = _PushRegistry()
    gov = ResourceGovernor()
    model = provider_from_env()
    router = Router(devices, registry, policy, gov)
    sessions = SessionStore()
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
    # Stage 18: natural-language intent service (proposal only; the
    # resolver + policy/governor/execution stay authoritative). Hosted
    # parsing is enabled only when a real provider is configured —
    # otherwise the deterministic local parser serves offline.
    from .intent import (CompositeIntentParser, HostedIntentParser,
                         IntentTurnService, LocalIntentParser)
    _hosted = None
    try:
        if getattr(model, "name", "echo") not in ("echo",):
            _hosted = HostedIntentParser(model)
    except Exception:  # noqa: BLE001 — local-only fallback
        _hosted = None
    intent_service = IntentTurnService(
        fabric, resolver,
        CompositeIntentParser(LocalIntentParser(), _hosted), audit)
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
    return {"bus": bus, "audit": audit, "policy": policy, "registry": registry,
            "engine": engine, "missions": missions, "devices": devices,
            "memory": memory, "memory_service": memory_service,
            "ctx": ctx, "sched": sched, "notifs": notifs,
            "gov": gov, "model": model, "events_log": events_log,
            "jobs": jobs, "device_auth": device_auth, "router": router,
            "sessions": sessions, "tracer": tracer, "loop": loop,
            "voice": voice, "wake": wake, "limits": limits, "push": push,
            "fabric": fabric, "xfer": xfer, "resolver": resolver,
            "intent": intent_service, "session": session_service}

def create_app(stack=None) -> FastAPI:
    s = stack or build_stack()
    app = FastAPI(title="Personal Assistant Core", version="0.1.0")
    app.state.stack = s

    SENSITIVE_PATHS = ("/v1/agent/enroll", "/v1/agent/claim")

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
            from fastapi.responses import JSONResponse
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
                "checks": checks}

    @app.post("/v1/chat")
    def chat(body: ChatIn, _=Depends(auth)):
        """Propose-only loop: model text + optional memory context. No auto-execute."""
        s["ctx"].add_turn("user", body.text)
        hits = s["memory"].recall(body.text, top_k=3)
        bundle = s["ctx"].bundle(memories=[h[0] for h in hits])
        prompt = s["ctx"].build_prompt(bundle, body.text)
        resp = s["model"].complete(prompt)
        s["ctx"].add_turn("assistant", resp.text)
        s["bus"].publish("user_message", source="api",
                         payload={"text": body.text[:500]})
        return {"reply": resp.text, "memory_hits": len(hits)}

    @app.post("/v1/exec")
    def exec_tool(body: ExecIn, _=Depends(auth)):
        tool = s["registry"].get(body.tool)
        dev = None
        try:
            dev = s["devices"].get(body.device_id)
        except KeyError:
            dev = None
        snap = ResourceSnapshot(
            battery_pct=getattr(dev, "battery_pct", None),
            charging=getattr(dev, "charging", False),
            network=getattr(dev, "network", "unknown"),
            device_kind=getattr(getattr(dev, "kind", "cloud"), "value", "cloud"))
        gate = s["gov"].check(snap, tool.estimated_cost, tool.risk.value)
        if gate.action == "defer":
            return {"status": "deferred", "reason": gate.reason}
        try:
            rec = s["engine"].submit(body.tool, body.inputs, body.device_id,
                                     mission_id=body.mission_id)
        except PolicyDenied as e:
            # Hard-deny must be a truthful 403, never an unhandled 500.
            s["audit"].record("api", "exec_deny", body.tool, str(e)[:300])
            raise HTTPException(status_code=403, detail=str(e)[:300])
        s["audit"].record("api", "exec", body.tool, rec.state.value)
        return {"id": rec.id, "status": rec.state.value, "result": rec.result,
                "error": rec.error, "verified": rec.verified}

    @app.post("/v1/exec/{exec_id}/approve")
    def approve_exec(exec_id: str, _=Depends(auth)):
        rec = s["engine"].approve(exec_id)
        return {"id": rec.id, "status": rec.state.value, "result": rec.result}

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

    @app.post("/v1/missions/{mid}/transition")
    def mission_transition(mid: str, to: MissionState, _=Depends(auth)):
        return s["missions"].transition(mid, to).model_dump()

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
        """Full intent turn: parse -> resolve -> message, executing ONLY
        safe resolved intents through the existing fabric execution path.
        Approval-gated or blocked intents return honest messages."""
        if not isinstance(body.utterance, str) or \
                len(body.utterance) > 2000:
            raise HTTPException(status_code=400,
                                detail="utterance must be text (<=2000)")
        out = s["intent"].handle_utterance(
            body.utterance[:2000], body.who or "user",
            body.requester_device or "")
        return out

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
        stored = s["memory"].remember(item)
        return {"stored": stored is not None, "id": stored.id if stored else None}

    @app.get("/v1/memory/search")
    def search_memory(q: str, top_k: int = 5, _=Depends(auth)):
        return [{"text": m.text, "category": m.category, "score": sc}
                for m, sc in s["memory"].recall(q, top_k)]

    @app.get("/v1/events")
    def events(type: Optional[str] = None, limit: int = 50, _=Depends(auth)):
        return [e.model_dump() for e in s["bus"].history(type, limit)]

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
        result = s["loop"].handle_text(body.text, session_id=body.session_id,
                                       device_id=body.device_id, who=body.who)
        return {"reply": result.reply, "status": result.status,
                "session_id": body.session_id,
                "mission_id": result.mission_id,
                "execution_id": result.execution_id,
                "device_id": result.device_id, "tool": result.tool,
                "trace_id": result.trace_id}

    @app.post("/v1/talk/resume")
    def talk_resume(body: ResumeIn, _=Depends(auth)):
        """Continue a turn held for approval after human approval."""
        result = s["loop"].resume_after_approval(body.execution_id,
                                                 body.session_id)
        return {"reply": result.reply, "status": result.status,
                "mission_id": result.mission_id,
                "execution_id": result.execution_id}

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

    @app.post("/v1/memory/correct")
    def memory_correct(body: MemoryCorrectIn, _=Depends(auth)):
        return s["memory_service"].correct(body.old_query, body.new_text)

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

    async def agent_auth(x_device_id: Optional[str] = Header(None),
                        x_device_key: Optional[str] = Header(None)):
        if not x_device_id or not x_device_key:
            raise HTTPException(status_code=401, detail="device credentials required")
        if not s["device_auth"].verify(x_device_id, x_device_key):
            raise HTTPException(status_code=403, detail="invalid/revoked device identity")
        return x_device_id

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
            device_id, key = s["device_auth"].claim(body.pairing_code)
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
            d = s["devices"].heartbeat(device_id, battery_pct=body.battery_pct,
                                       charging=body.charging,
                                       network=body.network, online=body.online)
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
    def agent_poll(device_id: str = Depends(agent_auth)):
        try:
            s["devices"].heartbeat(device_id)  # poll counts as presence
        except KeyError:
            raise HTTPException(status_code=404, detail="device not registered")
        job = s["jobs"].poll(device_id)
        if job is None:
            return {"job": None}
        if s["jobs"].is_cancelled(job.id):
            return {"job": None}
        return {"job": {"job_id": job.id, "tool": job.tool,
                        "inputs": job.inputs, "timeout_s": job.timeout_s,
                        "execution_id": job.execution_id,
                        "mission_id": job.mission_id}}

    @app.post("/v1/agent/jobs/result")
    def agent_result(body: JobResultIn, device_id: str = Depends(agent_auth)):
        try:
            job = s["jobs"].complete(body.job_id, body.ok, body.result, body.error)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown job")
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
        s["audit"].record("operator", "device_revoke", device_id, "")
        return {"revoked": device_id}

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
    return build_stack(memory_db=_db_path("ZARA_MEMORY_DB"),
                       audit_db=_db_path("ZARA_AUDIT_DB", ":memory:"),
                       mission_db=_db_path("ZARA_MISSION_DB"))


app = create_app(_stack_from_env())
