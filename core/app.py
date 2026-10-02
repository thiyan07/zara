"""FastAPI gateway: REST + WebSocket. Device-token gate (Stage 2: per-device auth)."""
from __future__ import annotations
import os
from typing import Optional
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from pydantic import BaseModel

from .audit import AuditLog
from .builtin_tools import BUILTINS
from .context import ContextManager
from .devices import DeviceManager
from .events import EventBus
from .execution import ExecutionEngine
from .governor import ResourceGovernor, ResourceSnapshot
from .memory import MemoryStore
from .missions import MissionEngine, MissionState
from .models import DeviceState, MemoryItem
from .notifications import NotificationManager
from .policy import PolicyEngine
from .providers import EchoProvider
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

def build_stack():
    bus = EventBus()
    audit = AuditLog()
    policy = PolicyEngine()
    registry = ToolRegistry()
    for definition, handler in BUILTINS:
        registry.register(definition, handler)
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
    bus.subscribe("*", lambda ev: audit.record("bus", ev.type, ev.source, str(ev.payload)[:500]))
    return {"bus": bus, "audit": audit, "policy": policy, "registry": registry,
            "engine": engine, "missions": missions, "devices": devices,
            "memory": memory, "ctx": ctx, "sched": sched, "notifs": notifs,
            "gov": gov, "model": model, "events_log": events_log}

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
