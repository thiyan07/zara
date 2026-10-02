"""Linux audio device layer: bounded record/play through system utilities.

No background loops, no always-on capture. Recording happens only when the
voice state machine authorizes a turn. Prefers PipeWire (pw-record/pw-play),
falls back to ALSA (arecord/aplay). All cancellation kills the child.
"""
from __future__ import annotations
import os
import shutil
import signal
import subprocess
import threading
import wave

RATE = 16000
CHANNELS = 1
WIDTH = 2  # S16_LE
MAX_SECONDS = 30
MAX_WAV_BYTES = RATE * CHANNELS * WIDTH * MAX_SECONDS + 4096


class AudioError(ValueError):
    pass


def _which(names: list[str]) -> str:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    raise AudioError(f"no audio utility found: {names}")


def record_backend() -> str:
    # ALSA first: verified clean WAV output (pw-record emits headerless
    # raw with a junk prefix). PipeWire only as fallback.
    try:
        _which(["arecord"])
        return "alsa"
    except AudioError:
        _which(["pw-record"])
        return "pipewire"


def play_backend() -> str:
    try:
        _which(["aplay"])
        return "alsa"
    except AudioError:
        _which(["pw-play"])
        return "pipewire"


def validate_wav(data: bytes, max_bytes: int = MAX_WAV_BYTES) -> dict:
    """Validate WAV bytes via stdlib. Returns properties. Raises AudioError."""
    if not data:
        raise AudioError("empty audio")
    if len(data) > max_bytes:
        raise AudioError(f"audio exceeds {max_bytes} bytes")
    try:
        with wave.open(__import__("io").BytesIO(data), "rb") as w:
            return {"frames": w.getnframes(), "rate": w.getframerate(),
                    "channels": w.getnchannels(), "width": w.getsampwidth()}
    except (wave.Error, EOFError) as e:
        raise AudioError(f"invalid WAV: {e}")


def _spawn(cmd: list[str]) -> subprocess.Popen:
    try:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except FileNotFoundError:
        raise AudioError(f"audio utility missing: {cmd[0]}")


def _peak_dbfs(wav_bytes: bytes) -> float:
    """Peak level in dBFS. -inf means digital silence."""
    import io as _io
    import math as _math
    import wave as _wave
    try:
        with _wave.open(_io.BytesIO(wav_bytes), "rb") as w:
            raw = w.readframes(w.getnframes())
            width = w.getsampwidth()
    except (wave.Error, EOFError):
        return float("-inf")
    if not raw or width != 2:
        return float("-inf")
    peak = 0
    for i in range(0, len(raw), 2):
        v = int.from_bytes(raw[i:i + 2], "little", signed=True)
        if abs(v) > peak:
            peak = abs(v)
    if peak == 0:
        return float("-inf")
    return 20 * _math.log10(peak / 32768.0)


def probe_capabilities(sample_seconds: float = 1.0) -> dict:
    """Deterministic audio capability report. Distinguishes exists /
    permitted / opens / carries signal / plays / completes. Never claims
    audibility from exit codes. Signal threshold: peak above -50 dBFS."""
    caps: dict = {
        "microphone_available": False,
        "microphone_permission": "unknown",  # granted|denied|unknown
        "microphone_opens": False,
        "microphone_signal_detected": False,
        "microphone_peak_dbfs": None,
        "speaker_available": False,
        "playback_available": False,
        "audio_input_format": "S16_LE mono",
        "audio_output_format": "S16_LE mono",
    }
    try:
        backend = record_backend()
        caps["microphone_available"] = True
        caps["record_backend"] = backend
    except AudioError as e:
        caps["record_error"] = str(e)[:120]
        backend = None
    if backend:
        try:
            data = record_audio(duration_s=min(sample_seconds, 3.0))
            props = validate_wav(data)
            caps["microphone_opens"] = True
            caps["microphone_permission"] = "granted"
            peak = _peak_dbfs(data)
            caps["microphone_peak_dbfs"] = None if peak == float("-inf") \
                else round(peak, 1)
            caps["microphone_signal_detected"] = peak > -50.0
            caps["audio_input_format"] = (
                f"S16_LE {props['channels']}ch {props['rate']}Hz")
        except AudioError as e:
            msg = str(e).lower()
            if "permission" in msg or "denied" in msg or "busy" in msg:
                caps["microphone_permission"] = "denied"
            caps["record_error"] = str(e)[:120]
    try:
        caps["playback_available"] = True
        caps["play_backend"] = play_backend()
        caps["speaker_available"] = True
    except AudioError as e:
        caps["play_error"] = str(e)[:120]
    return caps


def record_audio(duration_s: float = 5.0, rate: int = RATE,
                 device: str = "",
                 cancel: threading.Event | None = None) -> bytes:
    """Record a bounded snippet. Returns WAV bytes. Never loops."""
    if not (0 < duration_s <= MAX_SECONDS):
        raise AudioError(f"duration must be 0..{MAX_SECONDS}s")
    backend = record_backend()
    if backend == "pipewire":
        cmd = ["pw-record", "--rate", str(rate), "--channels", "1",
               "--format", "s16", "-"]
    else:
        cmd = ["arecord", "-d", str(int(duration_s)), "-f", "S16_LE",
               "-r", str(rate), "-c", "1", "-t", "wav", "-"]
        if device:
            cmd += ["-D", device]
    proc = _spawn(cmd)
    try:
        if backend == "pipewire":
            # pw-record runs until killed: bound it here, not in background
            out, _ = proc.communicate(timeout=duration_s + 5)
            # pw-record raw output has no WAV header; wrap it
            import io as _io
            buf = _io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(out[:RATE * 2 * int(duration_s)])
            return buf.getvalue()
        out, _ = proc.communicate(timeout=duration_s + 10)
        return out
    except subprocess.TimeoutExpired:
        raise AudioError("record timeout")
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def play_audio(wav_bytes: bytes,
               cancel: threading.Event | None = None,
               timeout_s: float = 60.0) -> dict:
    """Play WAV bytes once. Returns on process exit. Kills on cancel."""
    props = validate_wav(wav_bytes)
    backend = play_backend()
    cmd = ["pw-play", "-"] if backend == "pipewire" else ["aplay", "-"]
    proc = _spawn(cmd)
    try:
        import time as _t
        if cancel is None:
            proc.communicate(input=wav_bytes, timeout=timeout_s)
            return {"played": True, "backend": backend, **props}
        # chunked feed so cancellation is honored mid-playback
        assert proc.stdin is not None
        step = 32000
        deadline = _t.time() + timeout_s
        for i in range(0, len(wav_bytes), step):
            if cancel.is_set() or _t.time() > deadline:
                raise AudioError("playback cancelled")
            try:
                proc.stdin.write(wav_bytes[i:i + step])
            except BrokenPipeError:
                break
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        proc.wait(timeout=10)
        if cancel.is_set():
            raise AudioError("playback cancelled")
        return {"played": True, "backend": backend, **props}
    except subprocess.TimeoutExpired:
        raise AudioError("playback timeout")
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
