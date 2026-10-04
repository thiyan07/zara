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
        notifs.create(f"Approval required: {rec.tool}",
                      f"Execution {rec.id} on {rec.device_id} needs a decision.",
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
    return {"bus": bus, "audit": audit, "policy": policy, "registry": registry,
            "engine": engine, "missions": missions, "devices": devices,
            "memory": memory, "memory_service": memory_service,
            "ctx": ctx, "sched": sched, "notifs": notifs,
            "gov": gov, "model": model, "events_log": events_log,
            "jobs": jobs, "device_auth": device_auth, "router": router,
            "sessions": sessions, "tracer": tracer, "loop": loop,
            "voice": voice, "wake": wake, "limits": limits, "push": push,
            "fabric": fabric}

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
        s["audit"].record("operator", "device_revoke", device_id, "")
        return {"revoked": device_id}

    # ---- Stage 10: device voice turn (same bounds as operator turn,
    # device-scoped auth; the operator endpoint is unchanged).

    @app.post("/v1/agent/capabilities/describe")
    def agent_capabilities_describe(body: dict,
                                   device_id: str = Depends(agent_auth)):
        """Device-authenticated capability advertisement with schemas.
        Validated + stored; unknown names recorded unmapped (never
        executable by themselves). Identity comes from auth, not body."""
        recs = body.get("records", []) if isinstance(body, dict) else []
        if not isinstance(recs, list) or len(recs) > 100:
            raise HTTPException(status_code=400,
                                detail="records must be a list (<=100)")
        for r in recs:
            if isinstance(r, dict):
                r["device_id"] = device_id
        return s["fabric"].advertise(recs)

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
