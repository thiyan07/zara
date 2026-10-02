"""NVIDIA Magpie TTS — OPTIONAL hosted backend behind TTSProvider.

Contract (verified live against the service, 2026-10-02):
- gRPC `grpc.nvcf.nvidia.com:443`, metadata: function-id + Bearer key.
- Model `magpie_tts_ensemble-Magpie-Multilingual` (function id is
  configurable: TTS_NVIDIA_FUNCTION_ID — NVIDIA rotates it on redeploy).
- Voice `Magpie-Multilingual.EN-US.Aria` default (override TTS_NVIDIA_VOICE).
- LINEAR_PCM mono, 22050 Hz. Per-sentence SynthesizeOnline calls, chunks
  <=200 chars (matches Piper's barge-in granularity; no fake streaming).

Piper REMAINS the default. This backend runs only when
TTS_PROVIDER=nvidia|magpie is explicitly selected AND a key is configured.
Missing key/failure => loud configuration/TTS error, never silent Piper.
"""
from __future__ import annotations
import os
import threading
from typing import Iterator, Optional

from .voice import TTSProvider

DEFAULT_SERVER = "grpc.nvcf.nvidia.com:443"
DEFAULT_FUNCTION_ID = "877104f7-e885-42b9-8de8-f6e4c6303969"
DEFAULT_VOICE = "Magpie-Multilingual.EN-US.Aria"
SAMPLE_RATE = 22050
MAX_CHARS = 2000
MAX_CHUNK = 200


class MagpieError(RuntimeError):
    """TTS failure. Never carries key material."""


def _api_key(explicit: str = "") -> str:
    key = explicit or os.environ.get("NVIDIA_TTS_API_KEY", "") or \
        os.environ.get("LLM_API_KEY", "")
    if not key:
        raise MagpieError(
            "TTS_PROVIDER=nvidia needs LLM_API_KEY (or NVIDIA_TTS_API_KEY)")
    return key


class MagpieTTS(TTSProvider):
    """Hosted Magpie TTS via NVIDIA NIM gRPC. Network-dependent by design."""

    name = "nvidia-magpie"

    def __init__(self, api_key: str = "", server: str = DEFAULT_SERVER,
                 function_id: str = DEFAULT_FUNCTION_ID,
                 voice: str = DEFAULT_VOICE, timeout_s: float = 60.0) -> None:
        self._explicit_key = api_key
        self.server = server
        self.function_id = function_id
        self.voice = voice
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._service = None
        self._active_call = None

    # ---------- connection (lazy; nothing on boot) ----------

    def _connect(self):
        with self._lock:
            if self._service is not None:
                return self._service
            try:
                import riva.client
            except ImportError:
                raise MagpieError(
                    "nvidia-riva-client not installed: pip install "
                    "nvidia-riva-client grpcio")
            key = _api_key(self._explicit_key)
            try:
                auth = riva.client.Auth(
                    None, True, self.server,
                    [["function-id", self.function_id],
                     ["authorization", "Bearer " + key]])
                self._service = riva.client.SpeechSynthesisService(auth)
            except Exception as e:  # noqa: BLE001 — never leak key
                raise MagpieError(f"magpie connect failed: {type(e).__name__}")
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

    # ---------- synthesis ----------

    @staticmethod
    def _sentences(text: str) -> list[str]:
        import re as _re
        parts = _re.split(r"(?<=[.!?])\s+", text.strip())
        chunks: list[str] = []
        for p in (s.strip() for s in parts):
            if not p:
                continue
            while len(p) > MAX_CHUNK:
                cut = p.rfind(" ", 0, MAX_CHUNK)
                cut = cut if cut > 0 else MAX_CHUNK
                chunks.append(p[:cut])
                p = p[cut:].strip()
            if p:
                chunks.append(p)
        return chunks

    def _synth_sentence(self, service, text: str) -> bytes:
        try:
            import riva.client.proto.riva_audio_pb2 as raudio
            import riva.client.proto.riva_tts_pb2 as rtts
        except ImportError:
            raise MagpieError("nvidia-riva-client not installed")
        req = rtts.SynthesizeSpeechRequest(
            text=text, language_code="en-US", sample_rate_hz=SAMPLE_RATE,
            encoding=raudio.AudioEncoding.LINEAR_PCM, voice_name=self.voice)
        call = None
        try:
            call = service.stub.SynthesizeOnline(
                iter([req]),
                metadata=service.auth.get_auth_metadata(),
                timeout=self.timeout_s)
            with self._lock:
                self._active_call = call
            out = b"".join(resp.audio for resp in call)
        except Exception as e:  # noqa: BLE001
            msg = getattr(e, "details", lambda: str(e))()
            code = getattr(e, "code", lambda: None)()
            status = code.name if code and hasattr(code, "name") else \
                type(e).__name__
            if "UNAUTHENTICATED" in status or "PERMISSION_DENIED" in status:
                raise MagpieError("magpie authentication rejected (gRPC "
                                  + status + ")")
            raise MagpieError(f"magpie synthesis failed ({status}): "
                              f"{str(msg)[:200]}")
        finally:
            with self._lock:
                if self._active_call is call:
                    self._active_call = None
        if not out:
            raise MagpieError("magpie returned empty audio")
        if len(out) > 10_000_000:
            raise MagpieError("magpie audio exceeds 10 MB bound")
        return out

    @staticmethod
    def _wrap_wav(pcm: bytes) -> bytes:
        import io as _io
        import wave as _wave
        buf = _io.BytesIO()
        with _wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)
        return buf.getvalue()

    def _check_text(self, text: str) -> None:
        if not text or not text.strip():
            raise ValueError("empty text")
        if len(text) > MAX_CHARS:
            raise ValueError(f"text exceeds {MAX_CHARS} chars")

    def speak(self, text: str) -> bytes:
        self._check_text(text)
        service = self._connect()
        try:
            pcm = b"".join(self._synth_sentence(service, s)
                           for s in self._sentences(text))
        except MagpieError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MagpieError(f"magpie synthesis failed: {type(e).__name__}")
        if not pcm:
            raise MagpieError("magpie returned empty audio")
        return self._wrap_wav(pcm)

    def stream_speak(self, text: str,
                     cancel: Optional[threading.Event] = None
                     ) -> Iterator[bytes]:
        self._check_text(text)
        service = self._connect()
        for sent in self._sentences(text):
            if cancel is not None and cancel.is_set():
                with self._lock:
                    call, self._active_call = self._active_call, None
                if call is not None and hasattr(call, "cancel"):
                    try:
                        call.cancel()
                    except Exception:  # noqa: BLE001 — best effort
                        pass
                return
            yield self._wrap_wav(self._synth_sentence(service, sent))

    def stop(self) -> None:
        self._close()

    def availability(self) -> dict:
        try:
            import riva.client  # noqa: F401
            installed = True
        except ImportError:
            installed = False
        return {"backend": "magpie-nim", "model": "magpie_tts_ensemble-"
                "Magpie-Multilingual", "voice": self.voice,
                "offline": False, "installed": installed,
                "key_configured": bool(self._explicit_key or
                                       os.environ.get("NVIDIA_TTS_API_KEY",
                                                      "") or
                                       os.environ.get("LLM_API_KEY", "")),
                "note": "hosted; Piper remains the default"}
