"""Tool registry — the tool contract. Handlers run only via ExecutionEngine."""
from __future__ import annotations
import threading
from typing import Callable, Any, Optional
from .models import ToolDefinition

Handler = Callable[[dict, dict], dict]

def _check_type(value: Any, t: str) -> bool:
    return {"string": str, "integer": int, "number": (int, float),
            "boolean": bool, "object": dict, "array": list}.get(t, object) and \
        isinstance(value, {"string": str, "integer": int, "number": (int, float),
            "boolean": bool, "object": dict, "array": list}[t])

def validate_against_schema(data: dict, schema: dict) -> list[str]:
    """Minimal JSON-Schema validation: object/required/properties/type/enum."""
    errors: list[str] = []
    if not schema:
        return errors
    if schema.get("type") == "object":
        if not isinstance(data, dict):
            return ["input must be an object"]
        for req in schema.get("required", []):
            if req not in data:
                errors.append(f"missing required field: {req}")
        for name, prop in schema.get("properties", {}).items():
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
        errors = validate_against_schema(inputs, tool.input_schema)
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
            out_errors = validate_against_schema(result, tool.output_schema)
            if out_errors:
                raise ValueError(f"tool {name} output failed verification: {out_errors}")
        return result
