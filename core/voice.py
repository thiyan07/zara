"""Voice abstractions: STT/TTS providers, voice state machine, wake-word
engine, and the voice pipeline. All providers replaceable; deterministic
mocks for tests. Text mode always works without voice.
"""
from __future__ import annotations
import re as _re
import threading
import warnings as _warnings
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Optional

WAKE_PHRASE = "Hey Zara"

# ---------- voice language (BCP-47 plumbing, NOT an acoustic claim) ----------
# ZARA_VOICE_LANG selects the *requested* language tag threaded through
# STT/TTS providers and turn results. It does not conjure acoustic support:
# English-only local models still fail loudly on non-English (never silently
# transcribe English as Tamil), and Piper with only an en_US voice refuses
# non-English synthesis explicitly. No Tamil acoustic model exists locally.

DEFAULT_VOICE_LANG = "en-US"
_VOICE_LANG_RE = _re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


def parse_voice_lang(raw: str | None = None) -> str:
    """Validated BCP-47 voice language. Default "en-US"; invalid input ->
    safe fallback "en-US" + UserWarning, never a crash. Secret-free:
    the value is a short language tag, never key material."""
    import os as _os
    if raw is None:
        raw = _os.environ.get("ZARA_VOICE_LANG", DEFAULT_VOICE_LANG)
    raw = (raw or "").strip()
    if _VOICE_LANG_RE.match(raw):
        return raw
    _warnings.warn(
        f"invalid ZARA_VOICE_LANG {raw!r}; falling back to "
        f"{DEFAULT_VOICE_LANG!r}")
    return DEFAULT_VOICE_LANG


def _whisper_lang(lang: str) -> str:
    """BCP-47 ("ta-IN") -> whisper language code ("ta")."""
    return (lang or "en").split("-")[0].lower() or "en"


# EdgeTTS voice map for trivially supported languages. NOT acoustically
# validated here (no hardware/speakers available); the tag is threaded so
# the cloud voice matches the requested language instead of defaulting to
# English speech for a Tamil request.
EDGE_VOICE_DEFAULT = "en-US-AriaNeural"
EDGE_VOICES = {
    "en-US": "en-US-AriaNeural",
    "en": "en-US-AriaNeural",
    "ta-IN": "ta-IN-PallaviNeural",
    "ta": "ta-IN-PallaviNeural",
}


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
                # Overlapping turns on one pipeline may both settle IDLE
                # (or re-request their current stage); a self-move is a
                # no-op, never a state jump. Genuine illegal jumps below
                # still raise.
                if to == self.state:
                    return self.state
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


class _UnavailableLocal(STTProvider):
    """Fallback placeholder when local STT cannot be built: always fails
    so STTFallback re-raises the PRIMARY error instead of succeeding
    silently with a mock transcript."""

    name = "local-unavailable"

    def transcribe(self, audio: bytes) -> str:
        raise RuntimeError("local STT unavailable (faster-whisper missing)")


class MockTTS(TTSProvider):
    name = "mock-tts"

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stopped = False

    def speak(self, text: str) -> bytes:
        self.spoken.append(text)
        return f"audio:{text[:50]}".encode()

    def stream_speak(self, text: str,
                     cancel: Optional[threading.Event] = None) -> Iterator[bytes]:
        """Word-chunked synthesis honoring cancel per chunk, so barge-in
        tests exercise real mid-speech cancellation. The full reply is
        recorded in spoken only when synthesis completes; a cancelled turn
        records a partial marker, never the unspoken reply as fact."""
        if cancel is not None and cancel.is_set():
            return
        words = text.split() or [text]
        for i, word in enumerate(words):
            if cancel is not None and cancel.is_set():
                self.spoken.append("[cancelled-partial] "
                                   + " ".join(words[:i]))
                return
            yield f"audio:{word[:50]}".encode()
        self.spoken.append(text)

    def stop(self) -> None:
        self.stopped = True


class EdgeTTSProvider(TTSProvider):
    """Free, keyless TTS via the installed edge-tts package (network needed).
    Lazy import — constructed only when configured. Chunked synthesis for
    barge-in support. NOT the default: local PiperTTS is the default."""
    name = "edge-tts"

    def __init__(self, voice: str | None = None, rate: str = "+0%",
                 language: str = DEFAULT_VOICE_LANG) -> None:
        self.language = language
        if voice:
            self.voice = voice
        else:
            mapped = EDGE_VOICES.get(language) or \
                EDGE_VOICES.get(_whisper_lang(language))
            if mapped is None and _whisper_lang(language) != "en":
                raise RuntimeError(
                    f"edge-tts voice missing for language {language!r}: "
                    "no mapped Edge voice; set TTS_EDGE_VOICE explicitly")
            self.voice = mapped or EDGE_VOICE_DEFAULT
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
                 timeout_s: float = 120.0,
                 language: str = "en") -> None:
        self.model_name = model
        self.device = device
        self.timeout_s = timeout_s
        self.language = language or "en"
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
        base = _whisper_lang(self.language)
        if base != "en" and self.model_name.endswith(".en"):
            # Loud refusal BEFORE any audio validation/model load: an
            # English-only model must never silently transcribe English
            # audio as Tamil (or any other language).
            raise RuntimeError(
                f"language unsupported by local model: {self.language!r} "
                f"needs a multilingual faster-whisper model, have "
                f"{self.model_name!r}")
        from .audio import validate_wav
        props = validate_wav(audio)
        if props["frames"] == 0:
            raise ValueError("empty audio")
        model = self._load()
        pcm = self._to_mono16k(audio, props)
        segments, info = model.transcribe(pcm, beam_size=1, language=base,
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

    def __init__(self, model_path: str = "", timeout_s: float = 120.0,
                 language: str = DEFAULT_VOICE_LANG) -> None:
        import os as _os
        self.model_path = model_path or _os.path.expanduser(
            "~/.cache/zara-voice/en_US-lessac-medium.onnx")
        self.timeout_s = timeout_s
        self.language = language or DEFAULT_VOICE_LANG
        self._voice = None
        self._lock = threading.Lock()

    def _check_language(self) -> None:
        """Refuse wrong-language synthesis: with only the en_US voice
        installed, a non-English request is an explicit error naming the
        missing voice — never English audio served as another language.
        A user-supplied voice file whose name carries the requested base
        code (e.g. ta_IN-...) is attempted normally."""
        base = _whisper_lang(self.language)
        if base == "en":
            return
        import os as _os
        stem = _os.path.basename(self.model_path).lower()
        if stem.startswith(base) or f"{base}_" in stem or \
                f"{base}-" in stem:
            return
        raise RuntimeError(
            f"piper voice missing for language {self.language!r}: no "
            f"{base!r} voice installed (have {self.model_path!r}); "
            f"refusing to synthesize {base} with an English voice")

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
        self._check_language()
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
        self._check_language()
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


class KeywordModelDetector:
    """Modular on-device keyword-model adapter (openWakeWord ONNX, etc).

    Opt-in and inert without a model file: construction never downloads,
    inference never runs in a loop here. The caller arms one bounded
    snippet at a time; battery gating lives in WakeWordEngine. Missing
    package or model raises an explicit error — never silent non-detection
    reported as "no wake word".
    """

    DEFAULT_MODEL = "~/.cache/zara-wake/hey_zara.onnx"
    MAX_SECONDS = 5.0

    def __init__(self, model_path: str | None = None,
                 threshold: float = 0.5) -> None:
        import os as _os
        self.model_path = _os.path.expanduser(
            model_path or self.DEFAULT_MODEL)
        self.threshold = threshold
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            import openwakeword  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "openwakeword not installed: pip install openwakeword "
                "(keyword-model wake detection unavailable)")
        import os as _os
        if not _os.path.isfile(self.model_path):
            raise RuntimeError(
                f"wake model missing: {self.model_path} "
                f"(place an openWakeWord 'hey_zara' model there; "
                f"nothing is downloaded automatically)")
        raise RuntimeError(
            f"wake model present but no validated inference binding: "
            f"{self.model_path} (physical validation pending)")

    def detect(self, pcm16k_mono_s16: bytes) -> dict:
        """Score one bounded snippet. Raises when unavailable."""
        if len(pcm16k_mono_s16) > int(16000 * 2 * self.MAX_SECONDS):
            raise ValueError(
                f"snippet exceeds {self.MAX_SECONDS}s")
        self._load()  # honest failure before any claim
        return {"detected": False, "score": 0.0}  # unreachable

    def availability(self) -> dict:
        import os as _os
        try:
            import openwakeword  # noqa: F401
            installed = True
        except ImportError:
            installed = False
        return {"engine": "keyword-model",
                "phrase": WAKE_PHRASE,
                "installed": installed,
                "model_present": _os.path.isfile(self.model_path),
                "always_on_dsp": False,
                "physically_validated": False,
                "note": "opt-in adapter; inert without an installed model"}


@dataclass
class OnnxWakeConfig:
    """Conservative config for the ONNX keyword spotter. Env overrides:
    WAKE_ONNX_MODEL_INFO (model_info.json path), WAKE_ONNX_KEYWORD
    (keyword .onnx path), WAKE_ONNX_MEL (melspectrogram.onnx path),
    WAKE_ONNX_KEYWORD_NAME (default "Hey Zara"), WAKE_ONNX_THRESHOLD,
    WAKE_ONNX_COOLDOWN_S, WAKE_ONNX_MAX_SNIPPET_S."""
    model_info: str = ""
    keyword_model: str = ""
    mel_model: str = ""
    keyword_name: str = "Hey Zara"
    threshold: float = 0.4
    cooldown_s: float = 1.5
    max_snippet_s: float = 5.0
    sample_rate: int = 16000

    @staticmethod
    def from_env() -> "OnnxWakeConfig":
        import os as _os
        base = _os.path.expanduser("~/.cache/zara-wake")
        def _f(key: str, default: str) -> str:
            v = _os.path.expanduser(_os.environ.get(key, "") or default)
            return v
        def _g(key: str, default: float) -> float:
            try:
                return float(_os.environ.get(key, "") or default)
            except ValueError:
                return default
        return OnnxWakeConfig(
            model_info=_f("WAKE_ONNX_MODEL_INFO",
                          f"{base}/model_info.json"),
            keyword_model=_f("WAKE_ONNX_KEYWORD",
                             f"{base}/hey_zara.onnx"),
            mel_model=_f("WAKE_ONNX_MEL", f"{base}/melspectrogram.onnx"),
            keyword_name=(_os.environ.get("WAKE_ONNX_KEYWORD_NAME", "")
                          or "Hey Zara"),
            threshold=_g("WAKE_ONNX_THRESHOLD", 0.4),
            cooldown_s=_g("WAKE_ONNX_COOLDOWN_S", 1.5),
            max_snippet_s=_g("WAKE_ONNX_MAX_SNIPPET_S", 5.0))


class OnnxWakeSpotter:
    """Thin ONNX keyword-spotting adapter (onnx-wakeword model contract).

    Runtime design only — NO vendored engine code (upstream ships no
    license file) and NO model shipped: a compatible "Hey Zara" model
    must be trained separately (see docs/WAKE_ONNX_SETUP.md). Uses the
    already-installed onnxruntime; numpy for framing. One bounded
    16 kHz mono s16 snippet per call, never a capture loop, never STT,
    never network. Inference follows the validated upstream contract:
    float32 audio (int16 range) -> mel frontend -> last mel_time frames
    into (1, mel_time, n_mels, resolved from the model) -> sigmoid +
    background-class softmax -> threshold gate -> consecutive-frames
    confirmation -> cooldown. Sessions are created once and reused;
    ``close()`` releases them.
    Cooldown suppresses duplicate events; nothing is persisted (no raw
    audio retained). Missing runtime/model/keyword raises honestly —
    never silent non-detection.
    """

    MEL_HOP = 160
    MEL_WIN = 400
    RAW_MELS = 32

    def __init__(self, config: OnnxWakeConfig | None = None,
                 _sessions=None) -> None:
        self.config = config or OnnxWakeConfig.from_env()
        self._sessions = _sessions  # injected only by tests
        self._owns_sessions = _sessions is None
        self._keywords: list[str] = []
        self._n_mels = self.RAW_MELS
        self._mel_time = 98
        self._cons_frames = 2
        self._lock = threading.Lock()
        self._cons = 0
        self._cons_word = ""
        self._last_fire = 0.0

    def _read_model_info(self) -> dict:
        import json as _json
        import os as _os
        import zipfile as _zip
        path = self.config.model_info
        if path.lower().endswith(".zip"):
            if not _os.path.isfile(path):
                raise RuntimeError(
                    f"wake model missing: model bundle not found at "
                    f"{path} (see docs/WAKE_ONNX_SETUP.md; nothing is "
                    f"downloaded automatically)")
            try:
                with _zip.ZipFile(path, "r") as zf:
                    return _json.loads(zf.read("model_info.json"))
            except (KeyError, ValueError, _zip.BadZipFile) as e:
                raise RuntimeError(f"wake model bundle unreadable: {e}")
        if not _os.path.isfile(path):
            raise RuntimeError(
                f"wake model missing: model_info not found at {path} "
                f"(see docs/WAKE_ONNX_SETUP.md; nothing is downloaded "
                f"automatically)")
        try:
            with open(path, encoding="utf-8") as f:
                return _json.load(f)
        except (OSError, ValueError) as e:
            raise RuntimeError(f"wake model_info unreadable: {e}")

    def _session_from(self, ort, label: str, path: str, opts,
                      bundle=None):
        import zipfile as _zip
        if bundle is not None:
            try:
                with _zip.ZipFile(bundle, "r") as zf:
                    data = zf.read(path)
            except KeyError:
                raise RuntimeError(
                    f"wake model missing: {label} {path!r} not in bundle "
                    f"{bundle}")
            try:
                return ort.InferenceSession(data, opts)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"wake {label} failed to load: {e}")
        import os as _os
        if not path or not _os.path.isfile(path):
            raise RuntimeError(
                f"wake model missing: {label} not found at "
                f"{path or '(unset)'} (see docs/WAKE_ONNX_SETUP.md; "
                f"nothing is downloaded automatically)")
        try:
            return ort.InferenceSession(path, opts)
        except Exception as e:  # noqa: BLE001 — corrupt model is
            raise RuntimeError(  # unavailable, never partial init
                f"wake {label} failed to load: {e}")

    def _load(self):
        with self._lock:
            if self._sessions is not None:
                return self._sessions
            info = self._read_model_info()
            kws = info.get("keywords", [])
            if self.config.keyword_name not in kws:
                raise RuntimeError(
                    f"wake keyword {self.config.keyword_name!r} not in "
                    f"model_info keywords {kws!r} (a generic model never "
                    f"counts as {self.config.keyword_name!r})")
            self._keywords = list(kws)
            mel_time = int(info.get("mel_time", 98))
            self._cons_frames = int(info.get("cons_frames", 2))
            try:
                import onnxruntime as _ort
            except ImportError:
                raise RuntimeError(
                    "onnxruntime not installed: pip install onnxruntime "
                    "(ONNX wake detection unavailable)")
            opts = _ort.SessionOptions()
            opts.intra_op_num_threads = 1  # low-power: no thread pool
            opts.log_severity_level = 3  # errors only; no per-frame logs
            bundled = self.config.model_info.lower().endswith(".zip")
            bundle = self.config.model_info if bundled else None
            kw_path = info.get("model_file") or self.config.keyword_model
            if bundled:
                import os as _os
                kw_path = _os.path.basename(kw_path)
                mel_sess = self._session_from(
                    _ort, "mel model", "melspectrogram.onnx", opts,
                    bundle=bundle)
            else:
                mel_sess = self._session_from(
                    _ort, "mel model", self.config.mel_model, opts)
            kw_sess = self._session_from(
                _ort, "keyword model", kw_path, opts, bundle=bundle)
            # Resolve the classifier's real input width from its shape
            # ([batch, mel_time, cols]); the mel frontend always emits
            # RAW_MELS channels, zero-padded when the model takes more.
            shape = kw_sess.get_inputs()[0].shape
            mt = shape[-2] if len(shape) >= 2 else 0
            if isinstance(mt, int) and mt > 0 and mt != mel_time:
                raise RuntimeError(
                    f"wake mel_time mismatch: model_info mel_time="
                    f"{mel_time} but model input shape={shape}")
            last = shape[-1] if len(shape) >= 1 else 0
            self._n_mels = last if isinstance(last, int) and last > 0 \
                else self.RAW_MELS
            self._mel_time = mel_time
            self._sessions = {"mel": mel_sess, "keyword": kw_sess}
            return self._sessions

    def _audio_needed(self) -> int:
        return self._mel_time * self.MEL_HOP + self.MEL_WIN

    def spot(self, pcm16k: bytes) -> dict:
        """Score one bounded snippet. Returns {detected, word,
        confidence, cons_frames, cooldown}. Raises when unavailable;
        cooldown suppresses repeats (detected False + cooldown True).
        Confirmation needs consecutive hits, so feed overlapping
        windows like a streaming caller — a lone call rarely fires."""
        import time as _t
        cap = int(self.config.sample_rate * 2 * self.config.max_snippet_s)
        if len(pcm16k) > cap:
            raise ValueError(f"snippet exceeds {self.config.max_snippet_s}s")
        if len(pcm16k) == 0:
            raise ValueError("empty snippet")
        sessions = self._load()
        import numpy as _np
        audio = _np.frombuffer(pcm16k, dtype=_np.int16).astype(_np.float32)
        need = self._audio_needed()
        if len(audio) < need:
            audio = _np.pad(audio, (0, need - len(audio)))
        audio = audio[:need].reshape(1, -1)
        mel_in = sessions["mel"].get_inputs()[0].name
        kw_in = sessions["keyword"].get_inputs()[0].name
        mel = sessions["mel"].run(None, {mel_in: audio})[0]
        try:
            mel2d = _np.asarray(mel, dtype=_np.float32)
            # Frontend emits (batch, 1, frames, RAW_MELS); normalize the
            # same way the validated pipeline does.
            while mel2d.ndim > 2:
                mel2d = mel2d[0] if mel2d.shape[0] == 1 else mel2d[0]
            mel2d = mel2d / 10.0 + 2.0
        except (IndexError, TypeError, ValueError) as e:
            raise RuntimeError(f"wake mel frontend bad output: {e}")
        if mel2d.ndim == 1:
            frames, chans = mel2d.shape[0], 1
        elif mel2d.ndim == 2:
            frames, chans = mel2d.shape[0], mel2d.shape[1]
        else:
            raise RuntimeError("wake mel frontend bad output rank")
        window = _np.zeros((1, self._mel_time, self._n_mels),
                           dtype=_np.float32)
        start = max(0, frames - self._mel_time)
        take = min(self._mel_time, frames)
        width = min(self.RAW_MELS, self._n_mels, chans)
        if mel2d.ndim == 1:
            window[0, :take, :width] = \
                mel2d[start:start + take].reshape(-1, 1)
        else:
            window[0, :take, :width] = mel2d[start:start + take, :width]
        logits = sessions["keyword"].run(None, {kw_in: window})[0]
        try:
            data = _np.asarray(logits, dtype=_np.float64).flatten()
        except (TypeError, ValueError) as e:
            raise RuntimeError(f"wake model bad output shape: {e}")
        # Sigmoid + background class -> softmax (validated contract).
        probs = 1.0 / (1.0 + _np.exp(-_np.clip(data, -30.0, 30.0)))
        with _np.errstate(divide="ignore"):
            lns = _np.log(probs / (1.0 - probs + 1e-9))
        full = _np.concatenate([lns, _np.array([0.0])])
        full = full - full.max()
        sm = _np.exp(full)
        sm = sm / sm.sum()
        k = len(self._keywords)
        if k == 0 or len(sm) < k + 1:
            raise RuntimeError("wake model output/keyword mismatch")
        idx = int(_np.argmax(sm[:k]))
        conf = float(sm[idx])
        word = self._keywords[idx]
        hit = word == self.config.keyword_name and \
            conf >= self.config.threshold
        now = _t.monotonic()
        with self._lock:
            if hit and word == self._cons_word:
                self._cons += 1
            elif hit:
                self._cons_word = word
                self._cons = 1
            else:
                self._cons = 0
                self._cons_word = ""
            confirmed = hit and self._cons >= self._cons_frames
            if confirmed and (now - self._last_fire) < self.config.cooldown_s:
                return {"detected": False, "word": word,
                        "confidence": conf,
                        "cons_frames": self._cons_frames, "cooldown": True}
            if confirmed:
                self._last_fire = now
                self._cons = 0
                self._cons_word = ""
        return {"detected": bool(confirmed), "word": word,
                "confidence": conf, "cons_frames": self._cons_frames,
                "cooldown": False}

    def close(self) -> None:
        """Release sessions idempotently; late callbacks after close must
        not revive anything (sessions are dropped, not re-created)."""
        with self._lock:
            self._sessions = None
            self._keywords = []
            self._cons = 0
            self._cons_word = ""

    def availability(self) -> dict:
        import os as _os
        try:
            import onnxruntime  # noqa: F401
            runtime = True
        except ImportError:
            runtime = False
        files = {k: bool(_os.path.isfile(p)) for k, p in (
            ("model_info", self.config.model_info),
            ("keyword_model", self.config.keyword_model),
            ("mel_model", self.config.mel_model))}
        ready = runtime and all(files.values())
        return {"engine": "onnx-wakeword-contract",
                "phrase": self.config.keyword_name,
                "runtime": runtime,
                "files": files,
                "ready": ready,
                "always_on_dsp": False,
                "physically_validated": False,
                "note": "real detection needs a trained 'Hey Zara' model "
                        "(see docs/WAKE_ONNX_SETUP.md); unavailable until "
                        "model files exist" if not ready else
                        "model files present; detection still unvalidated "
                        "on hardware"}


def capture_snippet_alsa(seconds: float = 1.0, rate: int = 16000,
                         device: str | None = None,
                         timeout_s: float = 15.0) -> bytes:
    """Capture ONE bounded ALSA snippet (16-bit mono s16 raw PCM, no WAV
    header, nothing persisted). Single foreground subprocess, killed by
    timeout. No loop, no background thread, no recording beyond the
    returned bytes. Raises honestly: missing arecord, bad duration,
    no mic (arecord's stderr, truncated), timeout, or empty capture."""
    import shutil as _shutil
    import subprocess as _sp
    arecord = _shutil.which("arecord")
    if arecord is None:
        raise RuntimeError(
            "arecord not installed (alsa-utils missing): microphone "
            "capture unavailable")
    if not (1.0 <= seconds <= 5.0):
        raise ValueError("capture seconds must be 1.0..5.0")
    if rate != 16000:
        raise ValueError("capture rate must be 16000 (model contract)")
    argv = [arecord, "-q", "-t", "raw", "-f", "S16_LE", "-c", "1",
            "-r", str(rate), "-d", str(int(seconds))]
    if device:
        argv += ["-D", device]
    try:
        r = _sp.run(argv, capture_output=True, timeout=timeout_s)
    except _sp.TimeoutExpired:
        raise RuntimeError("microphone capture timed out")
    except OSError as e:
        raise RuntimeError(f"microphone capture failed: {e}")
    if r.returncode != 0:
        err = (r.stderr or b"").decode(errors="replace").strip()[:200]
        raise RuntimeError(f"no microphone audio ({err or 'arecord error'})")
    if not r.stdout:
        raise RuntimeError("microphone capture empty")
    return r.stdout


def wake_listen_once(spotter: OnnxWakeSpotter | None = None,
                     seconds: float = 1.0,
                     device: str | None = None,
                     pcm: bytes | None = None) -> dict:
    """Score ONE snippet: capture (unless bytes are injected) then run
    the spotter. No loop, no background work, no persistence. The model
    is validated BEFORE touching the microphone, so a missing model
    never costs a room recording. Returns the spot() dict; raises
    honestly on capture or inference failure."""
    sp = spotter if spotter is not None else OnnxWakeSpotter()
    sp._load()  # fail fast on model problems — same module, intentional
    raw = pcm if pcm is not None else capture_snippet_alsa(
        seconds=seconds, device=device)
    return sp.spot(raw)


class WakeListenSession:
    """Controlled opt-in listening session: ONE persistent capture
    stream, sequential bounded inference windows, no polled loops.

    OFF by default (constructor never starts; explicit ``start()``
    only). The model is validated before the microphone opens, so a
    missing model never costs a recording. A single capture owner:
    second ``start()`` while running is refused. ``stop()`` kills the
    capture promptly (bounded wait, then kill), joins the worker, bumps
    the generation so late results are dropped, and never auto-restarts.
    Recoverable window errors retry with bounded backoff (interruptible
    by stop); anything else parks at ``error`` with the cause kept in
    ``.error``. No audio is saved or uploaded; no resampling (16 kHz
    enforced end to end); no busy-waiting (blocking reads + event
    waits only). States: off | starting | listening | error.
    """

    def __init__(self, spotter: OnnxWakeSpotter | None = None,
                 on_event=None, window_s: float = 1.0,
                 device: str | None = None,
                 max_retries: int = 3, retry_backoff_s: float = 1.0,
                 spawn=None) -> None:
        self.spotter = spotter if spotter is not None \
            else OnnxWakeSpotter()
        self.on_event = on_event
        self.window_s = window_s
        self.device = device
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self._spawn = spawn  # injected stream factory (tests); None = arecord
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream = None
        self._proc = None
        self._generation = 0
        self.state = "off"
        self.error = ""
        self.windows = 0
        self.retries = 0

    # -- lifecycle --

    def start(self) -> bool:
        """Open the mic and begin scoring. Returns True only when the
        worker is confirmed running. Refuses when already running or
        when model/mic validation fails (state -> error, nothing held)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False  # single capture owner: no duplicates
            self.state = "starting"
            self.error = ""
            try:
                self.spotter._load()  # model first — same module
                stream, proc = self._open_stream()
            except Exception as e:  # noqa: BLE001 — nothing held yet
                self.state = "error"
                self.error = str(e)[:300]
                return False
            self._stream = stream
            self._proc = proc
            self._stop.clear()
            self._generation += 1
            self._thread = threading.Thread(target=self._loop,
                                            args=(self._generation,),
                                            daemon=True)
            self._thread.start()
            self.state = "listening"
            return True

    def stop(self) -> None:
        """End capture promptly: signal, kill the stream (bounded wait),
        join the worker (bounded), invalidate late results. Idempotent."""
        with self._lock:
            self._generation += 1
            self._stop.set()
            stream, proc, thread = self._stream, self._proc, self._thread
            self._stream = None
            self._proc = None
        self._close_stream(stream, proc)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        with self._lock:
            self._thread = None
            self.state = "off"

    # -- capture --

    def _open_stream(self):
        """One persistent arecord process streaming raw PCM on stdout,
        or the injected factory under tests. Returns (stream, proc)."""
        if self._spawn is not None:
            return self._spawn(), None
        import shutil as _shutil
        import subprocess as _sp
        arecord = _shutil.which("arecord")
        if arecord is None:
            raise RuntimeError(
                "arecord not installed (alsa-utils missing): microphone "
                "capture unavailable")
        argv = [arecord, "-q", "-t", "raw", "-f", "S16_LE", "-c", "1",
                "-r", "16000"]
        if self.device:
            argv += ["-D", self.device]
        try:
            proc = _sp.Popen(argv, stdout=_sp.PIPE, stderr=_sp.PIPE)
        except OSError as e:
            raise RuntimeError(f"microphone capture failed: {e}")
        # Fail fast on immediate ALSA errors (bad device, busy mic):
        # poll briefly instead of blocking forever on first read.
        import time as _t
        deadline = _t.monotonic() + 3.0
        while _t.monotonic() < deadline:
            if proc.poll() is not None:
                err = (proc.stderr.read() or b"").decode(
                    errors="replace").strip()[:200]
                raise RuntimeError(
                    f"no microphone audio ({err or 'arecord error'})")
            _t.sleep(0.05)
        return proc.stdout, proc

    @staticmethod
    def _close_stream(stream, proc) -> None:
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 — close is best-effort
                pass
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except Exception:  # noqa: BLE001 — escalate, bounded
                    proc.kill()
                    proc.wait(timeout=3.0)
            except Exception:  # noqa: BLE001
                pass

    # -- inference loop --

    def _window_bytes(self) -> int:
        return int(16000 * 2 * self.window_s)

    def _loop(self, generation: int) -> None:
        want = self._window_bytes()
        failures = 0
        while not self._stop.is_set():
            try:
                chunk = self._read_exact(want)
                out = self.spotter.spot(chunk)
                self.windows += 1
                failures = 0
            except Exception as e:  # noqa: BLE001 — bounded retries,
                failures += 1  # then park at error (never a tight loop)
                self.retries += 1
                if failures > self.max_retries or self._stop.is_set():
                    with self._lock:
                        self.state = "error"
                        self.error = str(e)[:300]
                    return
                self._stop.wait(self.retry_backoff_s)
                continue
            if generation != self._generation:
                return  # stopped meanwhile: late result dropped
            if out.get("detected") and self.on_event is not None:
                try:
                    self.on_event(out)
                except Exception:  # noqa: BLE001 — event sinks never
                    pass  # kill the loop

    def _read_exact(self, n: int) -> bytes:
        """Blocking read of exactly n bytes; short stream (EOF) is a
        device failure, never silent padding."""
        parts = []
        remaining = n
        stream = self._stream
        while remaining > 0:
            if self._stop.is_set():
                raise RuntimeError("listener stopped")
            data = stream.read(remaining)
            if not data:
                raise RuntimeError("microphone stream ended (device "
                                   "disconnected?)")
            parts.append(data)
            remaining -= len(data)
        return b"".join(parts)

    def availability(self) -> dict:
        import shutil as _shutil
        av = self.spotter.availability()
        av["capture"] = ("arecord" if _shutil.which("arecord") else
                         "unavailable")
        av["state"] = self.state
        av["always_on_dsp"] = False
        return av


def voice_capabilities(language: str | None = None) -> dict:
    """Honest per-language STT/TTS support matrix. Probe-only: imports
    and file-existence checks, never model loads or network calls.

    Returns {language, stt: {backend, supported, reason},
             tts: {backend, supported, reason}}. Unsupported languages
    are reported with the missing piece named — never silent fallback
    to English behavior presented as Tamil support."""
    import os as _os
    lang = (language or parse_voice_lang()).strip() or DEFAULT_VOICE_LANG
    base = _whisper_lang(lang)
    stt_model = _os.environ.get("STT_MODEL", "tiny.en")
    try:
        import faster_whisper  # noqa: F401
        fw_installed = True
    except ImportError:
        fw_installed = False
    if base == "en":
        if fw_installed:
            stt = {"backend": "local-whisper", "supported": True,
                   "reason": f"faster-whisper model {stt_model!r} handles "
                             f"{lang!r} locally"}
        else:
            stt = {"backend": "mock", "supported": False,
                   "reason": "faster-whisper not installed"}
    elif fw_installed and not stt_model.endswith(".en"):
        stt = {"backend": "local-whisper", "supported": True,
               "reason": f"multilingual model {stt_model!r} handles "
                         f"{lang!r} locally"}
    elif fw_installed:
        stt = {"backend": "local-whisper", "supported": False,
               "reason": f"language {lang!r} needs a multilingual "
                         f"faster-whisper model, have {stt_model!r}"}
    else:
        stt = {"backend": "none", "supported": False,
               "reason": f"faster-whisper not installed; {lang!r} "
                         f"unavailable offline"}
    try:
        import piper  # noqa: F401
        piper_installed = True
    except ImportError:
        piper_installed = False
    voice_path = _os.environ.get("TTS_MODEL", "") or _os.path.expanduser(
        "~/.cache/zara-voice/en_US-lessac-medium.onnx")
    stem = _os.path.basename(voice_path).lower()
    piper_matches = base == "en" or stem.startswith(base) or \
        f"{base}_" in stem or f"{base}-" in stem
    try:
        import edge_tts  # noqa: F401
        edge_installed = True
    except ImportError:
        edge_installed = False
    if piper_installed and _os.path.isfile(voice_path) and piper_matches:
        tts = {"backend": "local-piper", "supported": True,
               "reason": f"piper voice {voice_path!r} matches {lang!r}"}
    elif base in ("ta",) and edge_installed:
        tts = {"backend": "edge-tts", "supported": True,
               "reason": f"cloud voice {EDGE_VOICES.get(base)!r} for "
                         f"{lang!r} (network needed, acoustically "
                         f"unvalidated)"}
    elif base == "en" and edge_installed:
        tts = {"backend": "edge-tts", "supported": True,
               "reason": "cloud English voice (network needed)"}
    elif not piper_matches:
        tts = {"backend": "local-piper", "supported": False,
               "reason": f"piper voice missing for {lang!r}: no {base!r} "
                         f"voice installed (have {voice_path!r})"}
    elif not piper_installed:
        tts = {"backend": "none", "supported": False,
               "reason": "piper-tts not installed and no cloud TTS selected"}
    else:
        tts = {"backend": "none", "supported": False,
               "reason": f"no TTS voice for {lang!r}"}
    return {"language": lang, "stt": stt, "tts": tts}


def stt_from_env() -> STTProvider:
    """auto (default): local whisper when importable, else mock.
    nvidia: hosted Parakeet primary + local faster-whisper fallback.
    mock: force the mock. local: explicit local (raises if unavailable).
    Language: ZARA_VOICE_LANG (validated BCP-47, default en-US);
    NVIDIA_STT_LANGUAGE overrides it when explicitly set."""
    import os as _os
    which = _os.environ.get("STT_PROVIDER", "auto").strip().lower()
    voice_lang = parse_voice_lang()
    nvidia_lang = _os.environ.get("NVIDIA_STT_LANGUAGE", "").strip() \
        or voice_lang
    if which == "mock":
        return MockSTT()
    if which == "nvidia":
        from .stt_nvidia import NvidiaSTT, STTFallback
        primary = NvidiaSTT(
            server=_os.environ.get("NVIDIA_STT_SERVER",
                                   "grpc.nvcf.nvidia.com:443"),
            function_id=_os.environ.get(
                "NVIDIA_STT_FUNCTION_ID",
                "d3fe9151-442b-4204-a70d-5fcc597fd610"),
            model_label=_os.environ.get("NVIDIA_STT_MODEL",
                                        "parakeet-tdt-0_6b-v2"),
            language=nvidia_lang)
        try:
            fallback = FasterWhisperSTT(
                model=_os.environ.get("STT_MODEL", "tiny.en"),
                language=voice_lang)
        except (ImportError, RuntimeError):
            # No silent mock fallback: if local is unavailable the
            # PRIMARY error must propagate (STTFallback re-raises it).
            fallback = _UnavailableLocal()
        return STTFallback(primary, fallback)
    if which in ("auto", "local"):
        try:
            stt = FasterWhisperSTT(
                model=_os.environ.get("STT_MODEL", "tiny.en"),
                language=voice_lang)
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
    default; hosted backends run ONLY when explicitly selected.
    Language: ZARA_VOICE_LANG (validated BCP-47, default en-US) threads
    into every backend; TTS_EDGE_VOICE overrides the Edge voice map."""
    import os as _os
    which = _os.environ.get("TTS_PROVIDER", "auto").strip().lower()
    voice_lang = parse_voice_lang()
    if which == "mock":
        return MockTTS()
    if which == "edge":
        return EdgeTTSProvider(
            voice=_os.environ.get("TTS_EDGE_VOICE", "").strip() or None,
            language=voice_lang)
    if which in ("nvidia", "magpie"):
        from .tts_nvidia import MagpieTTS
        return MagpieTTS(
            server=_os.environ.get("TTS_NVIDIA_BASE_URL",
                                   "grpc.nvcf.nvidia.com:443"),
            function_id=_os.environ.get(
                "TTS_NVIDIA_FUNCTION_ID",
                "877104f7-e885-42b9-8de8-f6e4c6303969"),
            voice=_os.environ.get("TTS_NVIDIA_VOICE",
                                  "Magpie-Multilingual.EN-US.Aria"),
            language=voice_lang)
    if which in ("auto", "local", "piper"):
        try:
            import os as _oo
            default = _oo.path.expanduser(
                "~/.cache/zara-voice/en_US-lessac-medium.onnx")
            chosen = _os.environ.get("TTS_MODEL", "") or default
            if which in ("local", "piper") and not _oo.path.isfile(chosen):
                raise RuntimeError(f"piper voice missing: {chosen}")
            tts = PiperTTS(model_path=chosen, language=voice_lang)
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
                 loop, sessions,
                 language: str | None = None) -> None:
        self.stt = stt
        self.tts = tts
        self.loop = loop
        self.sessions = sessions
        # Effective session language (validated BCP-47). Additive: existing
        # 4-arg construction keeps working and resolves ZARA_VOICE_LANG.
        self.language = parse_voice_lang(language) \
            if language is not None else parse_voice_lang()
        self.states = VoiceStateMachine()
        self._speak_cancel = threading.Event()
        # Turn generation: each new turn (and every interrupt) invalidates
        # older in-flight speak loops, so a stale response can never keep
        # playing after a newer turn started. Checked per yielded chunk.
        self._generation = 0
        self._gen_lock = threading.Lock()

    def _next_generation(self) -> int:
        with self._gen_lock:
            self._generation += 1
            return self._generation

    def _current_generation(self) -> int:
        with self._gen_lock:
            return self._generation

    def handle_audio(self, audio: bytes, session_id: str = "",
                     device_id: str = "android-phone",
                     who: str = "user") -> dict:
        import uuid as _uuid
        # Pre-created turn id: voice turns join the same turn registry as
        # text turns, so clients can cancel a running voice turn via
        # POST /v1/talk/cancel and correlate streamed/text outcomes.
        turn_id = _uuid.uuid4().hex
        self.states.move(VoiceState.LISTENING)
        self.states.move(VoiceState.TRANSCRIBING)
        try:
            transcript = self.stt.transcribe(audio)
        except Exception as e:  # noqa: BLE001
            self.states.move(VoiceState.ERROR)
            return {"ok": False, "error": f"stt: {e}", "state": "error",
                    "language": self.language, "turn_id": turn_id}
        self.states.move(VoiceState.THINKING)
        import time as _t
        _think_start = _t.monotonic()
        try:
            result = self.loop.handle_text(transcript, session_id=session_id,
                                           device_id=device_id, who=who,
                                           turn_id=turn_id)
        except Exception as e:  # noqa: BLE001 — loop/LLM/provider/timeout
            self.states.move(VoiceState.ERROR)  # failure is loud, never a
            return {"ok": False, "error": f"loop: {e}",  # crash, never a
                    "transcript": transcript,  # hallucinated reply: the
                    "state": "error", "language": self.language,  # reply
                    "turn_id": turn_id}  # field is absent, not invented.
        loop_ms = int((_t.monotonic() - _think_start) * 1000)
        self.states.move(VoiceState.EXECUTING if result.tool
                         else VoiceState.SPEAKING)
        if result.tool:
            self.states.move(VoiceState.SPEAKING)
        gen = self._next_generation()
        self._speak_cancel.clear()
        chunks = []
        try:
            stream = self.tts.stream_speak(result.reply,
                                           cancel=self._speak_cancel)
            for chunk in stream:
                if gen != self._current_generation():
                    # Superseded: a newer turn owns the audio now. If this was
                    # an explicit interrupt (cancel set), resume the provider
                    # exactly once so it observes its own cancel and emits its
                    # partial marker deterministically; a bare newer turn
                    # breaks cleanly without the extra step.
                    if self._speak_cancel.is_set():
                        try:
                            next(stream)
                        except StopIteration:
                            pass
                        except Exception:  # noqa: BLE001 — teardown noise
                            pass
                    break  # superseded: newer turn or interrupt owns audio
                chunks.append(chunk)
        except Exception as e:  # noqa: BLE001 — TTS failure is loud,
            self.states.move(VoiceState.ERROR)  # never silent success,
            return {"ok": False, "error": f"tts: {e}",  # never a crash.
                    "transcript": transcript, "reply": result.reply,
                    "state": "error", "language": self.language,
                    "turn_id": getattr(result, "turn_id", "") or turn_id}
        audio_out = b"".join(chunks)
        self.states.move(VoiceState.IDLE)
        out = {"ok": True, "transcript": transcript, "reply": result.reply,
               "audio_bytes": len(audio_out), "status": result.status,
               "superseded": gen != self._current_generation(),
               "state": self.states.state.value,
               "language": self.language,
               "turn_id": getattr(result, "turn_id", "") or turn_id,
               "mission_id": result.mission_id,
               "execution_id": result.execution_id,
               "tool": result.tool, "device_id": result.device_id}
        # STT provenance (additive): which engine produced the transcript
        # and whether fallback fired. Never raw audio, never keys.
        snap = getattr(self.stt, "last_result", None)
        if snap is not None:
            out["stt_provider"] = snap.provider or self.stt.name
            out["stt_model"] = snap.model
            out["stt_fallback_used"] = snap.fallback_used
            out["stt_fallback_reason"] = snap.fallback_reason
            out["stt_wall_ms"] = snap.wall_ms
        else:
            out["stt_provider"] = self.stt.name
            out["stt_fallback_used"] = False
            out["stt_fallback_reason"] = ""
            out["stt_wall_ms"] = 0
        out["loop_ms"] = loop_ms
        # Deterministic transcript safety signals (advisory only;
        # enforcement stays in policy + approvals).
        from .voice_safety import assess_transcript
        try:
            assess = assess_transcript(transcript)
        except Exception:  # noqa: BLE001 — never break a turn on flags
            assess = {"flags": [], "needs_confirmation_hint": False,
                      "tokens": 0}
        out["transcript_flags"] = assess["flags"]
        out["transcript_tokens"] = assess["tokens"]
        return out

    def interrupt(self) -> dict:
        """Barge-in: stop TTS, cancel any in-flight hosted STT, return to
        listening. One session, no fork. Cancelled results are discarded."""
        self._next_generation()  # invalidate in-flight speak loops first
        self._speak_cancel.set()
        if hasattr(self.tts, "stop"):
            self.tts.stop()
        stop = getattr(self.stt, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001 — best effort
                pass
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
        # Per-turn effective language tags (mixed-language sessions keep
        # one tag per executed turn, in order).
        self.languages: list[str] = []

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
             device_id: str = "local-voice", who: str = "user",
             language: str | None = None) -> dict:
        """One bounded turn. Returns the pipeline result plus conversation
        metadata. Approval holds set pending_approval instead of ending.
        language: per-turn BCP-47 override (validated); defaults to the
        pipeline's effective language. Every result carries `language` so
        the UI can display it and mixed sessions keep per-turn tags."""
        allowed, reason = self._check_bounds()
        if not allowed:
            self.ended = True
            self.end_reason = reason
            return {"ok": False, "ended": True, "reason": reason,
                    "language": self.pipeline.language}
        out = self.pipeline.handle_audio(audio, session_id=session_id,
                                         device_id=device_id, who=who)
        effective = parse_voice_lang(language) \
            if language is not None else self.pipeline.language
        out["language"] = effective
        self.languages.append(effective)
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
        try:
            res = self.pipeline.loop.resume_after_approval(
                self.pending_approval, session_id)
        except Exception as e:  # noqa: BLE001 — resumed loop failure is
            return {"ok": False, "error": f"loop: {e}",  # loud, never a
                    "state": self.pipeline.states.state.value,  # crash.
                    "language": self.pipeline.language}
        self.pending_approval = ""
        self.last_activity = _time.time()
        # speak the resumed reply through the normal TTS path, walking
        # the legal pipeline stages (no state jumps)
        for stage in (VoiceState.LISTENING, VoiceState.TRANSCRIBING,
                      VoiceState.THINKING, VoiceState.SPEAKING):
            self.pipeline.states.move(stage)
        self.pipeline._speak_cancel.clear()
        gen = self.pipeline._next_generation()
        chunks = []
        try:
            for chunk in self.pipeline.tts.stream_speak(
                    res.reply, cancel=self.pipeline._speak_cancel):
                if gen != self.pipeline._current_generation():
                    break
                chunks.append(chunk)
        except Exception as e:  # noqa: BLE001 — resumed speech failure
            self.pipeline.states.move(VoiceState.ERROR)  # is loud, never
            return {"ok": False, "error": f"tts: {e}",  # silent, never a
                    "reply": res.reply, "state": "error",  # crash.
                    "mission_id": res.mission_id,
                    "language": self.pipeline.language}
        audio_out = b"".join(chunks)
        self.pipeline.states.move(VoiceState.IDLE)
        return {"ok": True, "reply": res.reply,
                "audio_bytes": len(audio_out),
                "superseded": gen != self.pipeline._current_generation(),
                "mission_id": res.mission_id,
                "language": self.pipeline.language}

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
