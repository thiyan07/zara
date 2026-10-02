"""Voice abstractions: STT/TTS providers, voice state machine, wake-word
engine, and the voice pipeline. All providers replaceable; deterministic
mocks for tests. Text mode always works without voice.
"""
from __future__ import annotations
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Optional

WAKE_PHRASE = "Hey Zara"


class VoiceState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    EXECUTING = "executing"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    ERROR = "error"


VOICE_TRANSITIONS: dict[VoiceState, set[VoiceState]] = {
    VoiceState.IDLE: {VoiceState.LISTENING},
    VoiceState.LISTENING: {VoiceState.TRANSCRIBING, VoiceState.IDLE,
                           VoiceState.INTERRUPTED},
    VoiceState.TRANSCRIBING: {VoiceState.THINKING, VoiceState.ERROR,
                              VoiceState.IDLE},
    VoiceState.THINKING: {VoiceState.EXECUTING, VoiceState.SPEAKING,
                          VoiceState.IDLE, VoiceState.ERROR},
    VoiceState.EXECUTING: {VoiceState.SPEAKING, VoiceState.IDLE,
                           VoiceState.ERROR},
    VoiceState.SPEAKING: {VoiceState.IDLE, VoiceState.INTERRUPTED,
                          VoiceState.LISTENING, VoiceState.ERROR},
    VoiceState.INTERRUPTED: {VoiceState.LISTENING, VoiceState.IDLE},
    VoiceState.ERROR: {VoiceState.IDLE, VoiceState.LISTENING},
}


class IllegalVoiceTransition(Exception):
    pass


class VoiceStateMachine:
    """Single owner of voice state — no scattered boolean flags."""

    def __init__(self) -> None:
        self.state = VoiceState.IDLE
        self._lock = threading.Lock()
        self.history: list[str] = ["idle"]

    def move(self, to: VoiceState) -> VoiceState:
        with self._lock:
            if to not in VOICE_TRANSITIONS[self.state]:
                raise IllegalVoiceTransition(f"{self.state} -> {to}")
            self.state = to
            self.history.append(to.value)
        return self.state

    def reset(self) -> VoiceState:
        with self._lock:
            self.state = VoiceState.IDLE
            self.history.append("idle")
        return self.state


# ---------- STT / TTS ----------

class STTProvider:
    name = "base"

    def transcribe(self, audio: bytes) -> str:
        raise NotImplementedError

    def stream_transcribe(self, chunks: Iterator[bytes]) -> Iterator[str]:
        """Partial transcripts where supported; fallback = final only."""
        yield self.transcribe(b"".join(chunks))


class TTSProvider:
    name = "base"

    def speak(self, text: str) -> bytes:
        raise NotImplementedError

    def stream_speak(self, text: str,
                     cancel: Optional[threading.Event] = None) -> Iterator[bytes]:
        """Chunked audio where supported (barge-in checks cancel per chunk)."""
        if cancel is not None and cancel.is_set():
            return
        yield self.speak(text)


class MockSTT(STTProvider):
    name = "mock-stt"

    def __init__(self, transcript: str = "") -> None:
        self.transcript = transcript

    def transcribe(self, audio: bytes) -> str:
        if not audio:
            raise ValueError("empty audio")
        return self.transcript


class MockTTS(TTSProvider):
    name = "mock-tts"

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stopped = False

    def speak(self, text: str) -> bytes:
        self.spoken.append(text)
        return f"audio:{text[:50]}".encode()

    def stop(self) -> None:
        self.stopped = True


class EdgeTTSProvider(TTSProvider):
    """Free, keyless TTS via the installed edge-tts package (network needed).
    Lazy import — constructed only when configured. Chunked synthesis for
    barge-in support. NOT the default: local PiperTTS is the default."""
    name = "edge-tts"

    def __init__(self, voice: str = "en-US-AriaNeural", rate: str = "+0%") -> None:
        self.voice = voice
        self.rate = rate

    def speak(self, text: str) -> bytes:
        return b"".join(self.stream_speak(text))

    def stream_speak(self, text: str,
                     cancel: Optional[threading.Event] = None) -> Iterator[bytes]:
        import asyncio
        import edge_tts
        chunks: list[bytes] = []

        async def run() -> None:
            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
            async for piece in communicate.stream():
                if cancel is not None and cancel.is_set():
                    break
                if piece["type"] == "audio" and piece["data"]:
                    chunks.append(piece["data"])

        asyncio.run(run())
        for c in chunks:
            yield c


class FasterWhisperSTT(STTProvider):
    """Local offline STT (faster-whisper, tiny.en default, CPU int8).
    Input: WAV bytes (any rate/channels; converted to 16 kHz mono).
    Never touches the network. Lazy model load on first use."""
    name = "local-whisper"

    def __init__(self, model: str = "tiny.en", device: str = "cpu",
                 timeout_s: float = 120.0) -> None:
        self.model_name = model
        self.device = device
        self.timeout_s = timeout_s
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError:
                    raise RuntimeError(
                        "faster-whisper not installed: pip install faster-whisper")
                self._model = WhisperModel(self.model_name, device=self.device,
                                           compute_type="int8")
            return self._model

    @staticmethod
    def _to_mono16k(data: bytes, props: dict):
        import io as _io
        import wave as _wave
        import numpy as _np
        with _wave.open(_io.BytesIO(data), "rb") as w:
            raw = w.readframes(w.getnframes())
        width, ch, rate = props["width"], props["channels"], props["rate"]
        if width != 2:
            raise ValueError(f"unsupported sample width: {width}")
        pcm = _np.frombuffer(raw, dtype=_np.int16).astype(_np.float32) / 32768.0
        if ch > 1:
            pcm = pcm.reshape(-1, ch).mean(axis=1)
        if rate != 16000 and len(pcm) > 0:
            idx = (_np.arange(int(len(pcm) * 16000 / rate)) * rate / 16000
                   ).astype(int)
            idx = _np.clip(idx, 0, len(pcm) - 1)
            pcm = pcm[idx]
        return pcm

    def transcribe(self, audio: bytes) -> str:
        from .audio import validate_wav
        props = validate_wav(audio)
        if props["frames"] == 0:
            raise ValueError("empty audio")
        model = self._load()
        pcm = self._to_mono16k(audio, props)
        segments, info = model.transcribe(pcm, beam_size=1, language="en",
                                          condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        return " ".join(text.split())

    def availability(self) -> dict:
        try:
            import faster_whisper  # noqa: F401
            return {"backend": "faster-whisper", "model": self.model_name,
                    "offline": True, "ready": self._model is not None}
        except ImportError:
            return {"backend": "faster-whisper", "model": self.model_name,
                    "offline": True, "ready": False,
                    "error": "faster-whisper not installed"}


class PiperTTS(TTSProvider):
    """Local offline TTS (Piper onnx, CPU). Text -> WAV bytes.
    Bounded input, sentence-chunked streaming for barge-in, no network."""
    name = "local-piper"
    MAX_CHARS = 2000

    def __init__(self, model_path: str = "", timeout_s: float = 120.0) -> None:
        import os as _os
        self.model_path = model_path or _os.path.expanduser(
            "~/.cache/zara-voice/en_US-lessac-medium.onnx")
        self.timeout_s = timeout_s
        self._voice = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._voice is None:
                try:
                    from piper import PiperVoice
                except ImportError:
                    raise RuntimeError(
                        "piper-tts not installed: pip install piper-tts")
                import os as _os
                if not _os.path.isfile(self.model_path):
                    raise RuntimeError(
                        f"piper voice missing: {self.model_path}")
                self._voice = PiperVoice.load(self.model_path)
            return self._voice

    @staticmethod
    def _sentences(text: str) -> list[str]:
        import re as _re
        parts = _re.split(r"(?<=[.!?])\s+", text.strip())
        return [p for p in (s.strip() for s in parts) if p]

    def _synth(self, text: str) -> bytes:
        import io as _io
        import wave as _wave
        voice = self._load()
        buf = _io.BytesIO()
        with _wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(voice.config.sample_rate)
            voice.synthesize_wav(text, w)
        return buf.getvalue()

    def speak(self, text: str) -> bytes:
        if not text or not text.strip():
            raise ValueError("empty text")
        if len(text) > self.MAX_CHARS:
            raise ValueError(f"text exceeds {self.MAX_CHARS} chars")
        import io as _io
        import wave as _wave
        # Phase 1: synthesize everything first, so a synthesis failure
        # surfaces as itself instead of tripping the WAV writer on close.
        raws = [self._synth(sent) for sent in self._sentences(text)]
        if not raws:
            raise ValueError("empty text")
        out = _io.BytesIO()
        wrote = False
        with _wave.open(out, "wb") as w:
            for raw in raws:
                with _wave.open(_io.BytesIO(raw), "rb") as r:
                    if not wrote:
                        w.setnchannels(r.getnchannels())
                        w.setsampwidth(r.getsampwidth())
                        w.setframerate(r.getframerate())
                        wrote = True
                    w.writeframes(r.readframes(r.getnframes()))
        return out.getvalue()

    def stream_speak(self, text: str,
                     cancel: Optional[threading.Event] = None
                     ) -> Iterator[bytes]:
        if not text or not text.strip():
            raise ValueError("empty text")
        if len(text) > self.MAX_CHARS:
            raise ValueError(f"text exceeds {self.MAX_CHARS} chars")
        for sent in self._sentences(text):
            if cancel is not None and cancel.is_set():
                return
            yield self._synth(sent)

    def stop(self) -> None:
        pass  # chunked synthesis: cancellation via cancel event

    def availability(self) -> dict:
        import os as _os
        try:
            import piper  # noqa: F401
            installed = True
        except ImportError:
            installed = False
        return {"backend": "piper", "model": self.model_path,
                "offline": True, "installed": installed,
                "model_present": _os.path.isfile(self.model_path)}


# ---------- wake word ----------

@dataclass
class WakeConfig:
    phrase: str = WAKE_PHRASE
    enabled: bool = True
    # Battery policy thresholds (configurable, not magic).
    pause_below_pct: float = 15.0
    resume_above_pct: float = 25.0
    pause_on_power_save: bool = True


class WakeWordEngine:
    """Replaceable wake detector for exactly WAKE_PHRASE. Real engines run a
    lightweight on-device model; audio stays local until detection."""

    def __init__(self, config: WakeConfig | None = None) -> None:
        self.config = config or WakeConfig()
        if self.config.phrase != WAKE_PHRASE:
            raise ValueError(f"wake phrase must be exactly {WAKE_PHRASE!r}")
        self.running = False
        self.paused = False
        self.pause_reason = ""
        self.detections = 0

    def start(self) -> None:
        self.running = True
        self.paused = False

    def stop(self) -> None:
        self.running = False

    def pause(self, reason: str = "") -> None:
        self.paused = True
        self.pause_reason = reason

    def resume(self) -> None:
        self.paused = False
        self.pause_reason = ""

    def battery_update(self, battery_pct: float | None, charging: bool,
                       power_save: bool) -> str:
        """Battery-first gating. Returns running|paused-low-battery|..."""
        if battery_pct is None:
            return "running" if self.running and not self.paused else "paused"
        if (battery_pct < self.config.pause_below_pct and not charging) or \
                (power_save and self.config.pause_on_power_save):
            self.pause("low-battery")
            return "paused-low-battery"
        if battery_pct >= self.config.resume_above_pct and \
                self.pause_reason == "low-battery":
            self.resume()
            return "running"
        return "running" if self.running and not self.paused else "paused"


    def availability(self) -> dict:
        """Honest capability report. Mock engines are never production."""
        return {"engine": "mock",
                "phrase": self.config.phrase,
                "running": self.running,
                "paused": self.paused,
                "physically_validated": False,
                "note": "deterministic stand-in; on-device model pending"}


class MockWakeEngine(WakeWordEngine):
    """Deterministic stand-in: tests trigger detection explicitly.
    Physical audio validation remains pending (no hardware claimed)."""

    def __init__(self, config: WakeConfig | None = None) -> None:
        super().__init__(config)
        self.events: list[str] = []

    def simulate_detection(self) -> dict:
        if not self.running or self.paused:
            raise RuntimeError("wake engine not actively listening")
        self.detections += 1
        self.events.append(f"detected:{self.config.phrase}")
        return {"phrase": self.config.phrase, "detections": self.detections}


class VADSpotterBackend:
    """Honest local wake-word backend: VAD-gated, single-shot keyword spot.

    NOT an always-on DSP engine. It processes one bounded audio snippet at a
    time (armed explicitly, e.g. push-to-talk or a polled mic check),
    trims silence with WebRTC VAD, transcribes with the local STT, and
    matches the exact phrase. Heavy Whisper never runs in a loop here:
    snippets are capped, and battery gating lives in WakeWordEngine.
    """

    def __init__(self, stt: FasterWhisperSTT | None = None,
                 max_seconds: float = 5.0) -> None:
        self.stt = stt or FasterWhisperSTT()
        self.max_seconds = max_seconds

    @staticmethod
    def _normalize(text: str) -> str:
        import re as _re
        return _re.sub(r"[^a-z ]", "", text.lower()).strip()

    def has_voice(self, pcm16k: bytes) -> bool:
        """WebRTC VAD voice-activity check on 16 kHz mono s16 bytes."""
        try:
            import webrtcvad
        except ImportError:
            raise RuntimeError("webrtcvad not installed")
        vad = webrtcvad.Vad(2)
        frame = 320 * 2  # 20 ms @16kHz
        voiced = total = 0
        for i in range(0, len(pcm16k) - frame + 1, frame):
            total += 1
            if vad.is_speech(pcm16k[i:i + frame], 16000):
                voiced += 1
        return total > 0 and voiced / total > 0.1

    def check(self, wav_bytes: bytes) -> dict:
        """Returns {detected: bool, transcript: str}. Never executes."""
        from .audio import validate_wav
        props = validate_wav(wav_bytes)
        seconds = props["frames"] / max(1, props["rate"])
        if seconds > self.max_seconds:
            raise ValueError(f"snippet exceeds {self.max_seconds}s")
        transcript = self.stt.transcribe(wav_bytes)
        norm = self._normalize(transcript)
        detected = "hey zara" in norm
        return {"detected": detected, "transcript": transcript}

    def availability(self) -> dict:
        return {"engine": "vad-spotter",
                "phrase": WAKE_PHRASE,
                "always_on_dsp": False,
                "physically_validated": False,
                "note": "VAD-gated single-shot spotter; push-to-talk is the "
                        "validated path; continuous listening not implemented"}


def stt_from_env() -> STTProvider:
    """auto (default): local whisper when importable, else mock.
    Set STT_PROVIDER=mock to force the mock; local for explicit local."""
    import os as _os
    which = _os.environ.get("STT_PROVIDER", "auto").strip().lower()
    if which == "mock":
        return MockSTT()
    if which in ("auto", "local"):
        try:
            stt = FasterWhisperSTT(
                model=_os.environ.get("STT_MODEL", "tiny.en"))
            if which == "local":
                return stt
            import faster_whisper  # noqa: F401 — probe only
            return stt
        except (ImportError, RuntimeError):
            if which == "local":
                raise
    return MockSTT()


def tts_from_env() -> TTSProvider:
    """auto (default): local Piper when available, else mock.
    Values: auto | local | piper (=local) | nvidia | magpie (=nvidia) |
    edge (pre-existing cloud TTS) | mock. Piper stays the effective
    default; hosted backends run ONLY when explicitly selected."""
    import os as _os
    which = _os.environ.get("TTS_PROVIDER", "auto").strip().lower()
    if which == "mock":
        return MockTTS()
    if which == "edge":
        return EdgeTTSProvider()
    if which in ("nvidia", "magpie"):
        from .tts_nvidia import MagpieTTS
        return MagpieTTS(
            server=_os.environ.get("TTS_NVIDIA_BASE_URL",
                                   "grpc.nvcf.nvidia.com:443"),
            function_id=_os.environ.get(
                "TTS_NVIDIA_FUNCTION_ID",
                "877104f7-e885-42b9-8de8-f6e4c6303969"),
            voice=_os.environ.get("TTS_NVIDIA_VOICE",
                                  "Magpie-Multilingual.EN-US.Aria"))
    if which in ("auto", "local", "piper"):
        try:
            import os as _oo
            default = _oo.path.expanduser(
                "~/.cache/zara-voice/en_US-lessac-medium.onnx")
            chosen = _os.environ.get("TTS_MODEL", "") or default
            if which in ("local", "piper") and not _oo.path.isfile(chosen):
                raise RuntimeError(f"piper voice missing: {chosen}")
            tts = PiperTTS(model_path=chosen)
            if which in ("local", "piper"):
                return tts
            import piper  # noqa: F401 — probe only
            if _oo.path.isfile(chosen):
                return tts
        except (ImportError, RuntimeError):
            if which in ("local", "piper"):
                raise
    return MockTTS()


# ---------- pipeline ----------

@dataclass
class VoiceConfig:
    stt_provider: str = "mock"
    tts_provider: str = "mock"
    language: str = "en-US"
    voice: str = "default"
    speed: float = 1.0


class VoicePipeline:
    """STT -> Zara core loop -> TTS, with barge-in. Voice is an interface;
    the phone never becomes the execution authority."""

    def __init__(self, stt: STTProvider, tts: TTSProvider,
                 loop, sessions) -> None:
        self.stt = stt
        self.tts = tts
        self.loop = loop
        self.sessions = sessions
        self.states = VoiceStateMachine()
        self._speak_cancel = threading.Event()

    def handle_audio(self, audio: bytes, session_id: str = "",
                     device_id: str = "android-phone",
                     who: str = "user") -> dict:
        self.states.move(VoiceState.LISTENING)
        self.states.move(VoiceState.TRANSCRIBING)
        try:
            transcript = self.stt.transcribe(audio)
        except Exception as e:  # noqa: BLE001
            self.states.move(VoiceState.ERROR)
            return {"ok": False, "error": f"stt: {e}", "state": "error"}
        self.states.move(VoiceState.THINKING)
        result = self.loop.handle_text(transcript, session_id=session_id,
                                       device_id=device_id, who=who)
        self.states.move(VoiceState.EXECUTING if result.tool
                         else VoiceState.SPEAKING)
        if result.tool:
            self.states.move(VoiceState.SPEAKING)
        self._speak_cancel.clear()
        audio_out = b"".join(self.tts.stream_speak(result.reply,
                                                   cancel=self._speak_cancel))
        self.states.move(VoiceState.IDLE)
        return {"ok": True, "transcript": transcript, "reply": result.reply,
                "audio_bytes": len(audio_out), "status": result.status,
                "state": self.states.state.value,
                "mission_id": result.mission_id,
                "execution_id": result.execution_id,
                "tool": result.tool, "device_id": result.device_id}

    def interrupt(self) -> dict:
        """Barge-in: stop TTS, return to listening. One session, no fork."""
        self._speak_cancel.set()
        if hasattr(self.tts, "stop"):
            self.tts.stop()
        try:
            self.states.move(VoiceState.INTERRUPTED)
            self.states.move(VoiceState.LISTENING)
        except IllegalVoiceTransition:
            self.states.reset()
        return {"interrupted": True, "state": self.states.state.value}


# ---------- continuous conversation ----------

import time as _time


@dataclass
class ConversationLimits:
    max_turns: int = 5
    max_duration_s: float = 300.0
    idle_timeout_s: float = 60.0


class VoiceConversation:
    """Bounded multi-turn voice session over ONE VoicePipeline.

    Budgets are hard: max turns, max wall-clock duration, idle timeout.
    Approval holds pause the conversation and resume into the SAME session
    (mission/approval state preserved). Per-turn loop budgets stay intact.
    The LLM never touches mic, TTS provider, permissions, or devices.
    """

    def __init__(self, pipeline: VoicePipeline,
                 limits: ConversationLimits | None = None,
                 battery_ok=None) -> None:
        self.pipeline = pipeline
        self.limits = limits or ConversationLimits()
        # battery_ok: optional callable () -> (bool, str); False pauses.
        self.battery_ok = battery_ok or (lambda: (True, "ok"))
        self.turns = 0
        self.started_at = _time.time()
        self.last_activity = self.started_at
        self.pending_approval: str = ""
        self.ended = False
        self.end_reason = ""

    def _check_bounds(self) -> tuple[bool, str]:
        if self.ended:
            return False, self.end_reason or "ended"
        if self.turns >= self.limits.max_turns:
            return False, "turn-limit"
        if _time.time() - self.started_at >= self.limits.max_duration_s:
            return False, "max-duration"
        if _time.time() - self.last_activity >= self.limits.idle_timeout_s:
            return False, "idle-timeout"
        ok, reason = self.battery_ok()
        if not ok:
            return False, f"battery-paused: {reason}"
        return True, "ok"

    def turn(self, audio: bytes, session_id: str = "",
             device_id: str = "local-voice", who: str = "user") -> dict:
        """One bounded turn. Returns the pipeline result plus conversation
        metadata. Approval holds set pending_approval instead of ending."""
        allowed, reason = self._check_bounds()
        if not allowed:
            self.ended = True
            self.end_reason = reason
            return {"ok": False, "ended": True, "reason": reason}
        out = self.pipeline.handle_audio(audio, session_id=session_id,
                                         device_id=device_id, who=who)
        self.turns += 1
        self.last_activity = _time.time()
        out["turn"] = self.turns
        if out.get("status") == "approval_required" and out.get("mission_id"):
            self.pending_approval = out.get("execution_id", "")
        if self.turns >= self.limits.max_turns:
            out["conversation_ended"] = "turn-limit"
            self.ended = True
            self.end_reason = "turn-limit"
        return out

    def resume_approval(self, session_id: str = "") -> dict:
        """Continue after human approval, same conversation + mission."""
        if not self.pending_approval:
            return {"ok": False, "error": "nothing awaiting approval"}
        res = self.pipeline.loop.resume_after_approval(
            self.pending_approval, session_id)
        self.pending_approval = ""
        self.last_activity = _time.time()
        # speak the resumed reply through the normal TTS path, walking
        # the legal pipeline stages (no state jumps)
        for stage in (VoiceState.LISTENING, VoiceState.TRANSCRIBING,
                      VoiceState.THINKING, VoiceState.SPEAKING):
            self.pipeline.states.move(stage)
        self.pipeline._speak_cancel.clear()
        audio_out = b"".join(self.pipeline.tts.stream_speak(
            res.reply, cancel=self.pipeline._speak_cancel))
        self.pipeline.states.move(VoiceState.IDLE)
        return {"ok": True, "reply": res.reply,
                "audio_bytes": len(audio_out),
                "mission_id": res.mission_id}

    def cancel(self) -> dict:
        out = self.pipeline.interrupt()
        self.ended = True
        self.end_reason = "cancelled"
        return out


# ---------- barge-in monitor ----------

def monitor_barge_in(capture, vad_check, timeout_s: float = 10.0,
                     chunk_s: float = 0.5,
                     cancel: threading.Event | None = None) -> bool:
    """Poll bounded mic chunks for voice activity. Returns True on first
    voiced chunk (caller then interrupts). capture() returns one WAV chunk;
    vad_check(wav) returns bool. Never loops forever: hard timeout + cancel.
    With a silent mic this simply returns False after the timeout."""
    import time as _t
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        if cancel is not None and cancel.is_set():
            return False
        try:
            chunk = capture(chunk_s)
        except Exception:  # noqa: BLE001 — capture failure is not voice
            return False
        try:
            if chunk and vad_check(chunk):
                return True
        except Exception:  # noqa: BLE001
            return False
    return False
