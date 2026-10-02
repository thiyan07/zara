"""Model provider abstraction — the LLM is a replaceable reasoning component."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class ModelResponse:
    text: str
    tool_calls: list[dict]  # proposed actions only — core decides
    provider: str

class ModelProvider:
    name: str = "base"

    def complete(self, prompt: str, **kwargs) -> ModelResponse:
        raise NotImplementedError

    def propose_tools(self, prompt: str, tools: list[dict]) -> ModelResponse:
        """Structured tool selection. Default: no proposal (safe)."""
        return ModelResponse(text="", tool_calls=[], provider=self.name)

class EchoProvider(ModelProvider):
    """Deterministic test/dev provider. Proposes nothing destructive."""
    name = "echo"

    def complete(self, prompt: str, **kwargs) -> ModelResponse:
        return ModelResponse(text=f"echo: {prompt[:200]}", tool_calls=[],
                             provider=self.name)

class OpenAICompatibleProvider(ModelProvider):
    """Stage 3 integration point: any OpenAI-compatible HTTP endpoint.
    No hard dependency — constructed only when configured."""
    name = "openai-compatible"

    def __init__(self, base_url: str, api_key: str = "", model: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def complete(self, prompt: str, **kwargs) -> ModelResponse:
        import json
        import urllib.request
        body = json.dumps({"model": self.model,
                           "messages": [{"role": "user", "content": prompt}],
                           "max_tokens": kwargs.get("max_tokens", 512)}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=kwargs.get("timeout", 60)) as r:
            data = json.load(r)
        text = data["choices"][0]["message"]["content"]
        return ModelResponse(text=text, tool_calls=[], provider=self.name)

PROVIDERS: dict[str, type[ModelProvider]] = {
    "echo": EchoProvider,
    "openai-compatible": OpenAICompatibleProvider,
}
