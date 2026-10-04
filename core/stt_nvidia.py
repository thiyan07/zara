"""NVIDIA hosted STT — OPTIONAL backend behind STTProvider.

Contract (verified live against the service, 2026-10-04):
- gRPC `grpc.nvcf.nvidia.com:443`, metadata: function-id + Bearer key
  (same transport as Magpie TTS, mirrored from core/tts_nvidia.py).
- Default function `ai-parakeet-tdt-0_6b-v2` (English, streaming CTC;
  function id configurable via NVIDIA_STT_FUNCTION_ID — NVIDIA rotates
  ids on redeploy). Riva ASR protocol: LINEAR_PCM mono, 16 kHz expected
  (other rates accepted by server; we send what capture produced).
- Live proof: Piper-synthesized "Hello, I am Zara. What is my battery
  level?" -> "hello i am zara what is my battery level".

Local faster-whisper REMAINS available as offline fallback. This backend
runs only when STT_PROVIDER=nvidia is explicitly selected AND a key is
configured. Missing key/failure => loud configuration error or
transient-category error for fallback, never silent wrong text.

Key ownership: backend Core only. NEVER embed in APK, logs, audit,
exceptions, or responses. Errors never carry key material.
"""
from __future__ import annotations
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .voice import STTProvider

DEFAULT_SERVER = "grpc.nvcf.nvidia.com:443"
# ai-parakeet-tdt-0_6b-v2 (English). Multilingual alt (NOT default):
# ai-whisper-large-v3 b702f636-f60c-4a3d-a6f4-f3568c13bd7d (untested here).
DEFAULT_FUNCTION_ID = "d3fe9151-442b-4204-a70d-5fcc597fd610"
DEFAULT_LANGUAGE = "en-US"
DEFAULT_MODEL_LABEL = "parakeet-tdt-0_6b-v2"

# Audio contract: bounded before any network transmission. Mirrors the
# voice-turn endpoint cap (2 MB) and the 30 s capture bound.
MAX_STT_SECONDS = 30.0
MAX_STT_BYTES = 2 * 1024 * 1024

# Transient => may retry once + may fall back to local. Never retry or
# fall back on auth/config/invalid-audio (retrying those is wrong).
TRANSIENT_CATEGORIES = frozenset({
    "RATE_LIMITED", "TIMEOUT", "NETWORK_ERROR", "SERVER_ERROR"})
NO_FALLBACK_CATEGORIES = frozenset({
    "AUTH_ERROR", "CONFIGURATION_ERROR", "INVALID_AUDIO"})


class NvidiaSTTError(RuntimeError):
    """Hosted STT failure with a deterministic category. Never carries
    key material — constructed only from status names and short msgs."""

    def __init__(self, category: str, detail: str = "") -> None:
        super().__init__(f"nvidia-stt {category}: {detail[:200]}")
        self.category = category


def _api_key(explicit: str = "") -> str:
    key = explicit or os.environ.get("NVIDIA_STT_API_KEY", "") or \
        os.environ.get("LLM_API_KEY", "")
    if not key:
        raise NvidiaSTTError(
            "CONFIGURATION_ERROR",
            "STT_PROVIDER=nvidia needs NVIDIA_STT_API_KEY (or LLM_API_KEY)")
    return key


@dataclass
class STTResult:
    """Honest transcription outcome. Confidence/language are absent unless
    the provider actually reports them (this path reports neither)."""
    text: str = ""
    provider: str = ""
    model: str = ""
    audio_ms: int = 0
    wall_ms: int = 0
    fallback_used: bool = False
    fallback_reason: str = ""
    error_category: Optional[str] = None


class NvidiaSTT(STTProvider):
    """Hosted Parakeet STT via NVIDIA NVCF gRPC. Network-dependent."""

    name = "nvidia-parakeet"

    def __init__(self, api_key: str = "", server: str = DEFAULT_SERVER,
                 function_id: str = DEFAULT_FUNCTION_ID,
                 model_label: str = DEFAULT_MODEL_LABEL,
                 language: str = DEFAULT_LANGUAGE,
                 timeout_s: float = 60.0) -> None:
        self._explicit_key = api_key
        self.server = server
        self.function_id = function_id
        self.model_label = model_label
        self.language = language
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._service = None
        self.last_result: Optional[STTResult] = None

    # ---------- connection (lazy; nothing on boot) ----------

    def _connect(self):
        with self._lock:
            if self._service is not None:
                return self._service
            try:
                import riva.client
            except ImportError:
                raise NvidiaSTTError(
                    "CONFIGURATION_ERROR",
                    "nvidia-riva-client not installed: pip install "
                    "nvidia-riva-client grpcio")
            key = _api_key(self._explicit_key)
            try:
                auth = riva.client.Auth(
                    None, True, self.server,
                    [["function-id", self.function_id],
                     ["authorization", "Bearer " + key]])
                self._service = riva.client.ASRService(auth)
            except NvidiaSTTError:
                raise
            except Exception as e:  # noqa: BLE001 — never leak key
                raise NvidiaSTTError(
                    f"connect failed: {type(e).__name__}")
            return self._service

    def _close(self) -> None:
        with self._lock:
            svc, self._service = self._service, None
        if svc is not None:
            try:
                channel = getattr(getattr(svc, "auth", None), "channel", None)
                if channel is not None and hasattr(channel, "close"):
                    channel.close()
            except Exception:  # noqa: BLE001 — best effort
                pass

    def stop(self) -> None:
        """Barge-in/cancel: drop the channel so an in-flight RPC fails
        fast; the caller discards any result. Never executes anything."""
        self._close()

    # ---------- transcription ----------

    @staticmethod
    def _categorize(status: str, msg: str) -> NvidiaSTTError:
        s = (status or "").upper()
        if "UNAUTHENTICATED" in s or "PERMISSION_DENIED" in s:
            return NvidiaSTTError("AUTH_ERROR", status)
        if "RESOURCE_EXHAUSTED" in s:
            return NvidiaSTTError("RATE_LIMITED", status)
        if "DEADLINE_EXCEEDED" in s:
            return NvidiaSTTError("TIMEOUT", status)
        if "UNAVAILABLE" in s or "connection" in msg.lower():
            return NvidiaSTTError("NETWORK_ERROR", f"{status} {msg}"[:200])
        if "INVALID_ARGUMENT" in s or "OUT_OF_RANGE" in s:
            return NvidiaSTTError("INVALID_AUDIO", f"{status} {msg}"[:200])
        if "CANCELLED" in s:
            return NvidiaSTTError("CANCELLED", status)
        if "NOT_FOUND" in s:
            return NvidiaSTTError("CONFIGURATION_ERROR",
                                  f"function id rejected: {status}")
        return NvidiaSTTError("SERVER_ERROR", f"{status} {msg}"[:200])

    def _recognize_once(self, pcm: bytes, rate: int) -> str:
        import riva.client
        service = self._connect()
        try:
            import riva.client.proto.riva_audio_pb2 as raudio
        except ImportError:
            raise NvidiaSTTError("CONFIGURATION_ERROR",
                                 "nvidia-riva-client not installed")
        config = riva.client.RecognitionConfig(
            encoding=raudio.AudioEncoding.LINEAR_PCM,
            sample_rate_hertz=rate, language_code=self.language,
            max_alternatives=1)
        try:
            resp = service.offline_recognize(pcm, config)
        except Exception as e:  # noqa: BLE001 — map, never leak
            details = getattr(e, "details", lambda: "")()
            code = getattr(e, "code", lambda: None)()
            status = code.name if code is not None and hasattr(code, "name") \
                else type(e).__name__
            raise self._categorize(status, str(details or e))
        try:
            alts = [a.transcript for r in resp.results
                    for a in r.alternatives]
        except Exception:  # noqa: BLE001 — malformed response
            raise NvidiaSTTError("INVALID_RESPONSE", "unparseable response")
        text = " ".join(t.strip() for t in alts).strip()
        text = " ".join(text.split())
        if not text:
            raise NvidiaSTTError("EMPTY_TRANSCRIPT", "no speech recognized")
        return text

    def transcribe(self, audio: bytes) -> str:
        from .audio import validate_wav
        if not audio:
            raise NvidiaSTTError("INVALID_AUDIO", "empty audio")
        if len(audio) > MAX_STT_BYTES:
            raise NvidiaSTTError(
                "INVALID_AUDIO",
                f"audio {len(audio)}B exceeds {MAX_STT_BYTES}B bound")
        try:
            props = validate_wav(audio)
        except Exception as e:  # noqa: BLE001
            raise NvidiaSTTError("INVALID_AUDIO", f"bad wav: {e}")
        seconds = props["frames"] / max(1, props["rate"])
        if seconds > MAX_STT_SECONDS:
            raise NvidiaSTTError(
                "INVALID_AUDIO", f"audio {seconds:.1f}s exceeds "
                f"{MAX_STT_SECONDS:.0f}s bound")
        if props["frames"] == 0:
            raise NvidiaSTTError("INVALID_AUDIO", "empty audio")
        import io as _io
        import wave as _wave
        with _wave.open(_io.BytesIO(audio), "rb") as w:
            pcm = w.readframes(w.getnframes())
        rate = props["rate"]
        started = time.monotonic()
        # One retry for transient failures only (bounded backoff 1s).
        # Auth/config/invalid-audio are never retried.
        attempt = 0
        while True:
            try:
                text = self._recognize_once(pcm, rate)
                break
            except NvidiaSTTError as e:
                if e.category in TRANSIENT_CATEGORIES and attempt == 0:
                    attempt += 1
                    time.sleep(1.0)
                    continue
                raise
        wall_ms = int((time.monotonic() - started) * 1000)
        self.last_result = STTResult(
            text=text, provider=self.name, model=self.model_label,
            audio_ms=int(seconds * 1000), wall_ms=wall_ms)
        return text

    def availability(self) -> dict:
        try:
            _api_key(self._explicit_key)
            configured = True
        except NvidiaSTTError:
            configured = False
        try:
            import riva.client  # noqa: F401
            transport = True
        except ImportError:
            transport = False
        return {"backend": "nvidia-parakeet", "model": self.model_label,
                "function_id": self.function_id,
                "language": self.language,
                "configured": configured, "transport": transport,
                "offline": False}


class STTFallback(STTProvider):
    """Primary (hosted) + local fallback. IS an STTProvider so the voice
    pipeline needs no changes. Fallback happens ONLY on transient
    primary failures and is recorded on last_result (never hidden).
    Auth/config/invalid-audio errors propagate untouched."""

    name = "stt-fallback"

    def __init__(self, primary: STTProvider,
                 fallback: STTProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.last_result: Optional[STTResult] = None

    def _snapshot(self, text: str, fallback_used: bool,
                  reason: str) -> STTResult:
        base = getattr(self.primary, "last_result", None)
        if base is not None and not fallback_used:
            return base
        return STTResult(text=text,
                         provider=getattr(self.fallback, "name", "local"),
                         model=getattr(self.fallback, "model_name",
                                       getattr(self.fallback, "name",
                                               "local")),
                         fallback_used=fallback_used,
                         fallback_reason=reason)

    def transcribe(self, audio: bytes) -> str:
        try:
            text = self.primary.transcribe(audio)
            self.last_result = self._snapshot(text, False, "")
            return text
        except Exception as e:  # noqa: BLE001
            cat = getattr(e, "category", None)
            if cat is not None and cat not in TRANSIENT_CATEGORIES:
                raise
            reason = cat or type(e).__name__
            try:
                text = self.fallback.transcribe(audio)
            except Exception:
                # Local also failed: surface the PRIMARY error (the
                # configured path), not the fallback's.
                raise e
            self.last_result = self._snapshot(text, True, reason)
            return text

    def stop(self) -> None:
        for p in (self.primary, self.fallback):
            stop = getattr(p, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:  # noqa: BLE001 — best effort
                    pass

    def availability(self) -> dict:
        out = {"backend": "stt-fallback"}
        for key, p in (("primary", self.primary),
                       ("fallback", self.fallback)):
            avail = getattr(p, "availability", None)
            out[key] = avail() if callable(avail) else {"name": p.name}
        return out
