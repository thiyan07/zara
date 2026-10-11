"""SSE framing for talk streaming (Night 2 §2).

Event contract (also mirrored by the Flutter client — keep in sync):
  turn     {"turn_id": str}                        stream opened
  delta    {"text": str}                           model text chunk (partial)
  tool     {"tool": str, "phase": started|ok|denied|deferred|error|approval,
            "execution_id": str, "device_id": str, "message": str}
  approval {"execution_id": str, "reason": str}    human grant required
  error    {"message": str}                        terminal failure text
  done     {"reply": str, "status": str, "turn_id": str,
            "mission_id": str, "execution_id": str} terminal outcome
  (cancel) client disconnect aborts the turn; the cancelled turn emits no
  further events (the pre-disconnect stream simply ends).

Framing: `event: <name>\\ndata: <compact-json>\\n\\n`. data is always a
single-line JSON object (no internal newlines). Never emits prompts,
API keys, provider payloads, or fingerprints — deltas are model output
addressed to the requesting user, same content class as /v1/talk reply.
"""
from __future__ import annotations
import json

EVENT_NAMES = ("turn", "delta", "tool", "approval", "error", "done")


def format_event(name: str, data: dict) -> str:
    """Render one SSE event. Raises ValueError on unknown names or
    non-JSON data (fail loudly at the source, never emit malformed)."""
    if name not in EVENT_NAMES:
        raise ValueError(f"unknown SSE event: {name}")
    try:
        blob = json.dumps(data, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, default=str)
    except (TypeError, ValueError) as e:
        raise ValueError(f"SSE payload not serializable: {e}")
    if "\n" in blob or "\r" in blob:
        raise ValueError("SSE payload must be single-line")
    return f"event: {name}\ndata: {blob}\n\n"


def parse_events(text: str) -> list[tuple[str, dict]]:
    """Parse an SSE byte/text stream into (name, payload) pairs. Tolerates
    chunk splits (caller buffers), blank lines, and `:comment` lines;
    raises ValueError on malformed data lines (never silently accepts)."""
    out: list[tuple[str, dict]] = []
    name: str | None = None
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line.strip() or line.startswith(":"):
            continue
        if line.startswith("event:"):
            name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            if name is None:
                raise ValueError("data without preceding event")
            try:
                payload = json.loads(line[len("data:"):].strip())
            except json.JSONDecodeError as e:
                raise ValueError(f"malformed SSE data: {e}")
            if not isinstance(payload, dict):
                raise ValueError("SSE payload must be an object")
            out.append((name, payload))
            name = None
        # Other fields (id:, retry:) ignored per SSE spec tolerance.
    return out
