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
    barge-in support."""
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
                "mission_id": result.mission_id}

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
