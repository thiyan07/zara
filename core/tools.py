"""Tool registry — the tool contract. Handlers run only via ExecutionEngine."""
from __future__ import annotations
import threading
from typing import Callable, Any, Optional
from .models import ToolDefinition

Handler = Callable[[dict, dict], dict]

def _check_type(value: Any, t: str) -> bool:
    types = {"string": str, "integer": int, "number": (int, float),
             "boolean": bool, "object": dict, "array": list}
    # Unknown type names fail closed (never crash the caller).
    if t not in types:
        return False
    return isinstance(value, types[t])

def validate_against_schema(data: dict, schema: dict,
                             max_bytes: int = 4096) -> list[str]:
    """Minimal JSON-Schema validation: object/required/properties/type/enum,
    plus envelope bounds (Rung-1: oversized inputs must fail pre-policy).
    Envelope bounds apply even to schemaless tools; shape checks need a
    schema. max_bytes may be raised per-tool to mirror that tool handler's
    own documented contract — never to bypass authorization."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["input must be an object"]
    if len(data) > 16:
        errors.append("too many input fields (max 16)")
    try:
        import json as _json
        if len(_json.dumps(data)) > max_bytes:
            errors.append(f"input too large (max {max_bytes} bytes)")
    except Exception:  # noqa: BLE001 — unserializable is itself invalid
        errors.append("input not serializable")
    def _depth(node, level=0):
        if isinstance(node, dict):
            return max([level] + [_depth(v, level + 1)
                                  for v in node.values()])
        if isinstance(node, list):
            return max([level] + [_depth(v, level + 1) for v in node])
        return level
    if _depth(data) > 4:
        errors.append("input too deeply nested (max 4)")
    if not schema:
        return errors
    if schema.get("type") == "object":
        for req in schema.get("required", []):
            if req not in data:
                errors.append(f"missing required field: {req}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for name in data:
                if name not in props:
                    errors.append(f"unknown field: {name}")
        for name, prop in props.items():
            if name in data:
                if "type" in prop and not _check_type(data[name], prop["type"]):
                    errors.append(f"field '{name}' must be {prop['type']}")
                if "enum" in prop and data[name] not in prop["enum"]:
                    errors.append(f"field '{name}' not in {prop['enum']}")
    return errors

class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, Handler] = {}
        self._lock = threading.Lock()

    def register(self, definition: ToolDefinition, handler: Handler) -> None:
        if not definition.input_schema and not definition.output_schema:
            pass  # schemas optional but encouraged
        with self._lock:
            if definition.name in self._tools:
                raise ValueError(f"tool already registered: {definition.name}")
            self._tools[definition.name] = definition
            self._handlers[definition.name] = handler

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name}")

    def list(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def call(self, name: str, inputs: dict, ctx: Optional[dict] = None) -> dict:
        """Direct call with schema validation + timeout. Policy is enforced
        by ExecutionEngine, not here — never call tools from LLM paths."""
        tool = self.get(name)
        errors = validate_against_schema(inputs, tool.input_schema,
                                         tool.max_input_bytes)
        if errors:
            raise ValueError(f"invalid inputs for {name}: {errors}")
        ctx = ctx or {}
        handler = self._handlers[name]
        result: dict = {}
        exc: list[BaseException] = []
        def run() -> None:
            try:
                result.update(handler(dict(inputs), ctx) or {})
            except BaseException as e:  # noqa: BLE001
                exc.append(e)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=tool.timeout_s)
        if t.is_alive():
            raise TimeoutError(f"tool {name} timed out after {tool.timeout_s}s")
        if exc:
            raise exc[0]
        if tool.verification == "output_schema":
            out_errors = validate_against_schema(result, tool.output_schema,
                                                 tool.max_input_bytes)
            if out_errors:
                raise ValueError(f"tool {name} output failed verification: {out_errors}")
        return result
