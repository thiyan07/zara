"""Model provider abstraction — the LLM is a replaceable reasoning component."""
from __future__ import annotations
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

from .llm import (ACTION_CONTRACT, LLMError, LLMErrorKind, LLMOutcome,
                  classify_http_error, parse_llm_action)


def _salvage_action(text: str):
    """Bounded salvage for reasoning-model prefix glitches (2026-10-22,
    nemotron-3-ultra live: the plan arrived as
    '{"type":"{"type":"response",\u2026}' — a truncated first attempt the
    model restarted; strings are unbalanced AT the glitch point, so
    candidates are anchored at each '{"' occurrence (rightmost-first,
    bounded) and accepted ONLY on a full strict parse (parse_llm_action).
    Never widens execution: a salvaged plan is a completely valid action
    object and runs through the same registration/policy/approval gates
    as every other plan."""
    s = text or ""
    if len(s) > 20000 or "{" not in s:
        return None
    starts = [i for i in range(len(s) - 2, -1, -1)
              if s[i] == "{" and s[i + 1] == '"'][:200]
    for i in starts:
        depth = 1
        in_str = False
        esc = False
        ok = False
        for j in range(i + 1, len(s)):
            ch = s[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    ok = True
                    break
        if ok and not in_str:
            cand = s[i:j + 1]
            try:
                return parse_llm_action(cand)
            except LLMError:
                continue
    return None


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
                      timeout: float = 60.0) -> Iterator:
        """Yield response chunks: raw text pieces, or a single LLMOutcome
        when the full action is already parsed (non-token streams).
        Fallback yields the complete parsed outcome (action preserved —
        yielding text alone would force an unsafe re-parse downstream)."""
        outcome = self.reason(system, user, tools, cancel, timeout)
        if outcome.text or outcome.action is not None:
            yield outcome

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
        import urllib.error
        body = json.dumps({"model": self.model,
                           "messages": [{"role": "user", "content": prompt}],
                           # 2026-10-22 (nemotron-3-ultra live): hosted
                           # intent parses must not have their output
                           # cut by the reasoning channel burn; same
                           # budget rule as the plan paths.
                           "max_tokens": kwargs.get("max_tokens", 1024),
                           "temperature": 0.2}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=kwargs.get("timeout", 60)) as r:
                try:
                    data = json.load(r)
                except ValueError as e:
                    raise LLMError(LLMErrorKind.MALFORMED,
                                   f"provider returned non-JSON: {e}")
        except urllib.error.HTTPError as e:
            raise classify_http_error(e.code, e.read().decode()[:500])
        except TimeoutError:
            raise LLMError(LLMErrorKind.TIMEOUT, "provider timed out")
        except OSError as e:
            raise LLMError(LLMErrorKind.NETWORK, f"provider network: {e}")
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(LLMErrorKind.MALFORMED,
                           f"provider response has no content: "
                           f"{type(e).__name__}")
        return ModelResponse(text=text, tool_calls=[], provider=self.name)

    def _post(self, payload: dict, timeout: float) -> dict:
        import json
        import urllib.request
        import urllib.error
        try:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}"})
        except (ValueError, KeyError, TypeError) as e:
            # Unusable endpoint configuration (e.g. empty base_url) must
            # surface as a safe provider error, never a raw exception that
            # crashes the turn.
            raise LLMError(LLMErrorKind.NETWORK,
                           f"provider misconfigured: {type(e).__name__}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                try:
                    return json.load(r)
                except ValueError as e:  # non-JSON 200 body
                    raise LLMError(LLMErrorKind.MALFORMED,
                                   f"provider returned non-JSON: {e}")
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
                   # 2026-10-22 (nemotron-3-ultra live): reasoning-class
                   # models spend tokens on the scratchpad BEFORE the
                   # JSON plan — 512 truncated it mid-object ("unreadable
                   # plan"). 1024 leaves room for reasoning + plan.
                   "max_tokens": 1024, "temperature": 0.2}
        if self.model:  # JSON mode where supported; strict parse regardless
            payload["response_format"] = {"type": "json_object"}
        start = time.time()
        last: Optional[LLMError] = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                data = self._post(payload, timeout)
                try:
                    text = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as e:
                    raise LLMError(LLMErrorKind.MALFORMED,
                                   f"provider response has no content: "
                                   f"{type(e).__name__}")
                usage = data.get("usage", {}) if isinstance(data, dict) \
                    else {}
                try:
                    action = parse_llm_action(text)
                except LLMError as e:
                    # Reasoning-model glitch absorption: try the bounded
                    # salvage BEFORE failing the whole turn.
                    action = _salvage_action(text)
                    if action is None:
                        raise e
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
                   # 2026-10-22 (NVIDIA gpt-oss-20b live): reasoning-class
                   # models burn the completion budget on the reasoning
                   # channel BEFORE any content — at 512 tokens the stream
                   # ended with reasoning-only chunks and an EMPTY visible
                   # reply. reasoning_effort=low caps that; 1024 leaves
                   # room for the actual answer after the scratchpad.
                   # Reasoning deltas are dropped below (content only),
                   # so the scratchpad never reaches the user.
                   "max_tokens": 1024, "temperature": 0.2,
                   "reasoning_effort": "low", "stream": True}
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        import codecs

        def _sse_lines(text: str):
            """Split decoded text into complete SSE lines, holding any
            trailing partial line in the closure buffer (TCP segments
            split lines arbitrarily, like multibyte characters)."""
            _sse_lines.buf += text
            *complete, _sse_lines.buf = _sse_lines.buf.split("\n")
            return [ln.strip() for ln in complete if ln.strip()]
        _sse_lines.buf = ""
        buf_done = [False]
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                buf = ""
                # Incremental UTF-8: TCP segments split multibyte
                # characters arbitrarily; naive per-chunk decode throws
                # mid-stream (observed live). Buffer partial sequences.
                decoder = codecs.getincrementaldecoder("utf-8")()
                it = iter(r)
                while True:
                    try:
                        raw = next(it)
                    except StopIteration:
                        break
                    except OSError:
                        # Some endpoints reset the connection after the
                        # final content chunk instead of sending [DONE].
                        # Tolerate the trailer reset ONLY when content
                        # already arrived: the assembled text still goes
                        # through strict parse, so truncation can never
                        # become a false success (partial JSON is
                        # MALFORMED). No content yet -> real failure below.
                        if not buf:
                            raise LLMError(
                                LLMErrorKind.NETWORK,
                                "provider stream failed before content")
                        break
                    if cancel is not None and cancel.is_set():
                        raise LLMError(LLMErrorKind.CANCELLED, "stream cancelled")
                    try:
                        text = decoder.decode(raw)
                    except UnicodeDecodeError as e:
                        raise LLMError(LLMErrorKind.MALFORMED,
                                       f"stream bytes not decodable: {e}")
                    for line in _sse_lines(text):
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            buf_done[0] = True
                            break
                        try:
                            payload = json.loads(chunk)
                            choices = payload["choices"]
                        except ValueError:
                            raise LLMError(LLMErrorKind.MALFORMED,
                                           "malformed stream chunk")
                        except (KeyError, TypeError, AttributeError):
                            raise LLMError(LLMErrorKind.MALFORMED,
                                           "stream chunk has no choices")
                        if not isinstance(choices, list) or not choices:
                            continue  # usage/control chunk, no content
                        delta = choices[0].get("delta") or {}
                        piece = (delta.get("content", "")
                                 if isinstance(delta, dict) else "")
                        if piece:
                            buf += piece
                            yield piece
                    if buf_done[0]:
                        break
                # Flush a final unterminated line, if any.
                tail = _sse_lines.buf.strip()
                if tail.startswith("data:"):
                    chunk = tail[5:].strip()
                    if chunk and chunk != "[DONE]":
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
