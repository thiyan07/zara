"""Context manager — six separated contexts, minimal prompt assembly."""
from __future__ import annotations
from dataclasses import dataclass, field
from .models import ConversationTurn, DeviceState, MemoryItem, Mission

MAX_TURNS = 20
MAX_PROMPT_CHARS = 6000

@dataclass
class ContextBundle:
    conversation: list[ConversationTurn] = field(default_factory=list)
    working: dict = field(default_factory=dict)
    mission: Mission | None = None
    world: dict = field(default_factory=dict)
    memories: list[MemoryItem] = field(default_factory=list)
    device: DeviceState | None = None

class ContextManager:
    def __init__(self) -> None:
        self._conversation: list[ConversationTurn] = []
        self._working: dict = {}
        self._world: dict = {}

    # -- conversation --
    def add_turn(self, role: str, text: str) -> None:
        self._conversation.append(ConversationTurn(role=role, text=text))
        self._conversation = self._conversation[-MAX_TURNS:]

    # -- working (short-term scratch) --
    def set_working(self, key: str, value) -> None:
        self._working[key] = value

    def clear_working(self) -> None:
        self._working = {}

    # -- world state --
    def update_world(self, patch: dict) -> None:
        self._world.update(patch)

    def bundle(self, mission: Mission | None = None,
               memories: list[MemoryItem] | None = None,
               device: DeviceState | None = None) -> ContextBundle:
        return ContextBundle(conversation=list(self._conversation),
                             working=dict(self._working), mission=mission,
                             world=dict(self._world),
                             memories=list(memories or []), device=device)

    def build_prompt(self, bundle: ContextBundle, task: str) -> str:
        """Assemble ONLY what this reasoning step needs, with a hard cap.
        Secret-looking material is redacted — secrets never enter LLM context."""
        from .tracing import redact
        parts = [f"Task: {redact(task)}"]
        if bundle.world:
            parts.append("World: " + redact(str(bundle.world))[:800])
        if bundle.mission:
            parts.append(redact(f"Mission[{bundle.mission.state.value}]: "
                                f"{bundle.mission.goal}")[:400])
        if bundle.memories:
            mem = "\n".join(f"- {redact(m.text[:200])}"
                            for m in bundle.memories[:5])
            parts.append(f"Relevant memory:\n{mem}")
        if bundle.device:
            d = bundle.device
            parts.append(f"Device: {d.device_id} ({d.kind.value}) online={d.online} "
                         f"battery={d.battery_pct} net={d.network}")
        if bundle.conversation:
            conv = "\n".join(f"{t.role}: {redact(t.text[:300])}"
                             for t in bundle.conversation[-6:])
            parts.append(f"Recent conversation:\n{conv}")
        if bundle.working:
            parts.append("Working: " + redact(str(bundle.working))[:800])
        prompt = "\n\n".join(parts)
        return prompt[:MAX_PROMPT_CHARS]
