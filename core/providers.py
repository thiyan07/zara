"""Model provider abstraction — the LLM is a replaceable reasoning component."""
from __future__ import annotations
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

from .llm import (ACTION_CONTRACT, LLMError, LLMErrorKind, LLMOutcome,
                  classify_http_error, parse_llm_action)

@dataclass
class ModelResponse:
    text: str
    tool_calls: list[dict]  # proposed actions only — core decides
    provider: str

class ModelProvider:
    name: str = "base"
    model: str = ""

    def complete(self, prompt: str, **kwargs) -> ModelResponse:
        raise NotImplementedError

    def propose_tools(self, prompt: str, tools: list[dict]) -> ModelResponse:
        """Structured tool selection. Default: no proposal (safe)."""
        return ModelResponse(text="", tool_calls=[], provider=self.name)

    # ---- Stage 3: structured reasoning interface (defaults are safe) ----

    def reason(self, system: str, user: str, tools: list[dict],
               cancel: Optional[threading.Event] = None,
               timeout: float = 60.0) -> LLMOutcome:
        """One structured reasoning step. Override in real providers."""
        raise NotImplementedError

    def stream_reason(self, system: str, user: str, tools: list[dict],
                      cancel: Optional[threading.Event] = None,
                      timeout: float = 60.0) -> Iterator[str]:
        """Yield response text chunks; fallback = single complete chunk."""
        outcome = self.reason(system, user, tools, cancel, timeout)
        if outcome.text:
            yield outcome.text

    def metadata(self) -> dict:
        return {"provider": self.name, "model": self.model}

class EchoProvider(ModelProvider):
    """Deterministic test/dev provider. Proposes nothing destructive."""
    name = "echo"

    def complete(self, prompt: str, **kwargs) -> ModelResponse:
        return ModelResponse(text=f"echo: {prompt[:200]}", tool_calls=[],
                             provider=self.name)

    def reason(self, system: str, user: str, tools: list[dict],
               cancel: Optional[threading.Event] = None,
               timeout: float = 60.0) -> LLMOutcome:
        from .llm import LLMAction
        start = time.time()
        return LLMOutcome(action=LLMAction(type="response",
                                           text=f"echo: {user[:200]}"),
                          text=f"echo: {user[:200]}", provider=self.name,
                          model=self.model, latency_s=time.time() - start)


class ScriptedProvider(ModelProvider):
    """Deterministic stand-in: scripted (match -> action) steps for tests
    and keyless end-to-end validation. Never used as a real brain."""
    name = "scripted"

    def __init__(self, script: list[tuple[str, dict]] | None = None,
                 default_text: str = "done.") -> None:
        self.script = list(script or [])
        self.default_text = default_text
        self.calls: list[str] = []

    def reason(self, system: str, user: str, tools: list[dict],
               cancel: Optional[threading.Event] = None,
               timeout: float = 60.0) -> LLMOutcome:
        from .llm import LLMAction
        if cancel is not None and cancel.is_set():
            raise LLMError(LLMErrorKind.CANCELLED, "cancelled before reasoning")
        start = time.time()
        self.calls.append(user)
        hay = user.lower()  # match the request/result, not contract boilerplate
        for needle, action in self.script:
            if needle.lower() in hay:
                return LLMOutcome(action=LLMAction(**action),
                                  text=action.get("text", ""),
                                  provider=self.name,
                                  latency_s=time.time() - start)
        return LLMOutcome(action=LLMAction(type="response",
                                           text=self.default_text),
                          text=self.default_text, provider=self.name,
                          latency_s=time.time() - start)

class OpenAICompatibleProvider(ModelProvider):
    """Any OpenAI-compatible HTTP endpoint (local model, free API, or other
    provider). Constructed only when configured via environment; stdlib HTTP,
    no extra dependency. Retries transient failures; classifies errors."""
    name = "openai-compatible"
    MAX_RETRIES = 2

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

    def _post(self, payload: dict, timeout: float) -> dict:
        import json
        import urllib.request
        import urllib.error
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise classify_http_error(e.code, e.read().decode()[:500])
        except TimeoutError:
            raise LLMError(LLMErrorKind.TIMEOUT, "provider timed out")
        except OSError as e:
            raise LLMError(LLMErrorKind.NETWORK, f"provider network: {e}")

    def reason(self, system: str, user: str, tools: list[dict],
               cancel: Optional[threading.Event] = None,
               timeout: float = 60.0) -> LLMOutcome:
        import json
        if cancel is not None and cancel.is_set():
            raise LLMError(LLMErrorKind.CANCELLED, "cancelled before reasoning")
        tool_names = [t["name"] for t in tools]
        prompt = (f"{system}\n\nAVAILABLE TOOLS: "
                  f"{json.dumps(tool_names)}\n\nUSER REQUEST:\n{user}")
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": ACTION_CONTRACT},
                                {"role": "user", "content": prompt}],
                   "max_tokens": 512, "temperature": 0.2}
        if self.model:  # JSON mode where supported; strict parse regardless
            payload["response_format"] = {"type": "json_object"}
        start = time.time()
        last: Optional[LLMError] = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                data = self._post(payload, timeout)
                text = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                action = parse_llm_action(text)
                return LLMOutcome(action=action, text=action.text or text,
                                  provider=self.name, model=self.model,
                                  latency_s=time.time() - start,
                                  usage=usage if isinstance(usage, dict) else {})
            except LLMError as e:
                last = e
                if e.kind not in (LLMErrorKind.RATE_LIMITED,
                                  LLMErrorKind.UNAVAILABLE,
                                  LLMErrorKind.NETWORK):
                    raise
                time.sleep(min(2 ** attempt, 8))
        raise last  # type: ignore[misc]

    def stream_reason(self, system: str, user: str, tools: list[dict],
                      cancel: Optional[threading.Event] = None,
                      timeout: float = 60.0) -> Iterator[str]:
        import json
        import urllib.request
        import urllib.error
        tool_names = [t["name"] for t in tools]
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": ACTION_CONTRACT},
                                {"role": "user",
                                 "content": f"{system}\n\nAVAILABLE TOOLS: "
                                            f"{json.dumps(tool_names)}\n\nUSER REQUEST:\n{user}"}],
                   "max_tokens": 512, "temperature": 0.2, "stream": True}
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                buf = ""
                for raw in r:
                    if cancel is not None and cancel.is_set():
                        raise LLMError(LLMErrorKind.CANCELLED, "stream cancelled")
                    line = raw.decode().strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        delta = json.loads(chunk)["choices"][0]["delta"]
                        piece = delta.get("content", "")
                    except (ValueError, KeyError):
                        raise LLMError(LLMErrorKind.MALFORMED,
                                       "malformed stream chunk")
                    if piece:
                        buf += piece
                        yield piece
        except urllib.error.HTTPError as e:
            raise classify_http_error(e.code, e.read().decode()[:500])
        except TimeoutError:
            raise LLMError(LLMErrorKind.TIMEOUT, "provider stream timed out")
        except OSError as e:
            raise LLMError(LLMErrorKind.NETWORK, f"provider network: {e}")


def provider_from_env() -> ModelProvider:
    """LLM_PROVIDER: echo (default) | scripted | openai-compatible.
    Reads LLM_BASE_URL / LLM_API_KEY / LLM_MODEL. Never logs the key."""
    which = os.environ.get("LLM_PROVIDER", "echo").strip().lower()
    if which == "openai-compatible":
        return OpenAICompatibleProvider(
            base_url=os.environ.get("LLM_BASE_URL", ""),
            api_key=os.environ.get("LLM_API_KEY", ""),
            model=os.environ.get("LLM_MODEL", ""))
    if which == "scripted":
        return ScriptedProvider()
    return EchoProvider()


PROVIDERS: dict[str, type[ModelProvider]] = {
    "echo": EchoProvider,
    "scripted": ScriptedProvider,
    "openai-compatible": OpenAICompatibleProvider,
}
