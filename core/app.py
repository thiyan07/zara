"""FastAPI gateway: REST + WebSocket. Device-token gate (Stage 2: per-device auth)."""
from __future__ import annotations
import os
from typing import Optional
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from pydantic import BaseModel

from .audit import AuditLog
from .builtin_tools import BUILTINS
from .context import ContextManager
from .device_auth import DeviceAuthStore
from .device_tools import register_device_proxies
from .devices import DeviceManager
from .events import EventBus
from .execution import ExecutionEngine, PolicyDenied
from .governor import ResourceGovernor, ResourceSnapshot
from .jobs import JobQueue
from .memory import MemoryStore
from .missions import MissionEngine, MissionState
from .models import DeviceKind, DeviceState, MemoryItem
from .notifications import NotificationManager
from .policy import PolicyEngine
from .protocol import CapabilityUpdate, ExecutionResult, HeartbeatPayload
from .providers import EchoProvider
from .routing import Router
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

def build_stack():
    bus = EventBus()
    audit = AuditLog()
    policy = PolicyEngine()
    registry = ToolRegistry()
    for definition, handler in BUILTINS:
        registry.register(definition, handler)
    jobs = JobQueue()
    register_device_proxies(registry, jobs)
    device_auth = DeviceAuthStore()
    events_log: list[dict] = []
    bus.subscribe("*", lambda ev: events_log.append(ev.model_dump()))
    engine = ExecutionEngine(registry, policy,
                             on_event=lambda t, p: bus.publish(t, source="execution", payload=p))
    missions = MissionEngine(on_event=lambda t, p: bus.publish(t, source="missions", payload=p))
    devices = DeviceManager(on_event=lambda t, p: bus.publish(t, source="devices", payload=p))
    memory = MemoryStore()
    ctx = ContextManager()
    sched = Scheduler()
    notifs = NotificationManager(on_event=lambda t, p: bus.publish(t, source="notifications", payload=p))
    gov = ResourceGovernor()
    model = EchoProvider()
    router = Router(devices, registry, policy, gov)
    bus.subscribe("*", lambda ev: audit.record("bus", ev.type, ev.source, str(ev.payload)[:500]))
    return {"bus": bus, "audit": audit, "policy": policy, "registry": registry,
            "engine": engine, "missions": missions, "devices": devices,
            "memory": memory, "ctx": ctx, "sched": sched, "notifs": notifs,
            "gov": gov, "model": model, "events_log": events_log,
            "jobs": jobs, "device_auth": device_auth, "router": router}

def create_app(stack=None) -> FastAPI:
    s = stack or build_stack()
    app = FastAPI(title="Personal Assistant Core", version="0.1.0")
    app.state.stack = s

    async def auth(authorization: Optional[str] = Header(None)):
        if authorization != f"Bearer {TOKEN}":
            raise HTTPException(status_code=401, detail="invalid token")
        return True

    @app.get("/v1/health")
    def health():
        return {"ok": True, "version": "0.1.0"}

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
        rec = s["engine"].submit(body.tool, body.inputs, body.device_id,
                                 mission_id=body.mission_id)
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
        s["audit"].record("device", "device_claim", device_id, "")
        return {"device_id": device_id, "device_key": key}

    @app.post("/v1/agent/register")
    def agent_register(body: AgentRegisterIn, device_id: str = Depends(agent_auth)):
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
                        "inputs": job.inputs, "timeout_s": job.timeout_s}}

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
        s["audit"].record("operator", "device_revoke", device_id, "")
        return {"revoked": device_id}

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
        await ws.accept()
        idx = 0
        try:
            while True:
                await ws.send_json({"ping": idx})
                idx += 1
                import asyncio
                await asyncio.sleep(30)
        except Exception:  # noqa: BLE001
            pass

    return app

app = create_app()
