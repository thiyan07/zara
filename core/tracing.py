"""Structured tracing across the whole pipeline. Correlation IDs in,
secrets never in. Operational metadata only."""
from __future__ import annotations
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

SECRET_LEAK = [re.compile(p, re.I) for p in
               (r"api[_-]?key\s*[:=]\s*\S+", r"password\s*[:=]\s*\S+",
                r"zara-dev-\S+", r"zara-pair-\S+", r"Bearer \S+",
                r"nvapi-[A-Za-z0-9_\-]{8,}", r"sk-ant-[A-Za-z0-9_\-]{8,}",
                r"sk-proj-[A-Za-z0-9_\-]{8,}", r"gsk_[A-Za-z0-9]{10,}",
                r"\bsk-[A-Za-z0-9]{20,}", r"xox[bpas]-[A-Za-z0-9\-]+")]

# Natural phrasing ("my api-key is X", "password = Y"): redact the VALUE
# only, keep the surrounding words. Tight by design — requires the marker
# plus an explicit is/:/= introducer so ordinary words ("token expired",
# "password reset steps") are untouched. History safety slightly favors
# over-redaction of genuinely introduced values.
SECRET_VALUE_LEAK = [(re.compile(p, re.I), r"\1\2\3[REDACTED]")
                      for p in
                      (r"(\bapi[ _-]?key|\bpassword|\bpasswd|\bprivate[ _-]?key|"
                       r"\bcredential|\bsecret)(\s+)(is\s+|:|=)\s*"
                       r"([^\s,;\"']+)",
                       r"(\btoken)(\s*)(is\s+|:|=)\s*"
                       r"([A-Za-z0-9_\-\.]{6,})")]


def redact(text: str) -> str:
    out = str(text)
    for p in SECRET_LEAK:
        out = p.sub("[REDACTED]", out)
    for p, repl in SECRET_VALUE_LEAK:
        out = p.sub(repl, out)
    return out[:2000]


@dataclass
class Span:
    trace_id: str
    name: str
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    attrs: dict = field(default_factory=dict)

    def finish(self, **attrs) -> "Span":
        self.ended = time.time()
        for k, v in attrs.items():
            self.attrs[k] = redact(v) if isinstance(v, str) else v
        return self

    @property
    def latency_s(self) -> float:
        return (self.ended or time.time()) - self.started


class Tracer:
    def __init__(self) -> None:
        self._spans: list[Span] = []
        self._lock = threading.Lock()

    def start(self, name: str, trace_id: str = "",
              **attrs) -> Span:
        span = Span(trace_id=trace_id or f"tr-{uuid.uuid4().hex[:12]}",
                    name=name)
        for k, v in attrs.items():
            span.attrs[k] = redact(v) if isinstance(v, str) else v
        with self._lock:
            self._spans.append(span)
        return span

    def for_trace(self, trace_id: str) -> list[Span]:
        with self._lock:
            return [s for s in self._spans if s.trace_id == trace_id]
