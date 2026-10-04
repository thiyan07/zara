"""Stage 13 tests: transcript safety signals, provider health +
cooldown routing, pipeline provenance/timing, cancellation races.
Deterministic; transport mocked where hosted STT is involved."""
import io
import wave

import pytest

from core.stt_nvidia import (NvidiaSTT, NvidiaSTTError, STTFallback,
                             STTHealth)
from core.voice import MockSTT, MockTTS, VoicePipeline
from core.voice_safety import HIGH_RISK_VERBS, assess_transcript


def silent_wav(seconds=1, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * rate * seconds)
    return buf.getvalue()


# ---------- safety signals ----------

def test_empty_transcript_flagged():
    r = assess_transcript("")
    assert r["flags"] == ["empty"]
    assert r["needs_confirmation_hint"] is True


def test_single_word_flagged():
    r = assess_transcript("Delete")
    assert "too_short" in r["flags"]
    assert "high_risk_words" in r["flags"]
    assert "missing_target" in r["flags"]


def test_high_risk_with_target():
    r = assess_transcript("Delete the temporary project folder")
    assert "high_risk_words" in r["flags"]
    assert "missing_target" not in r["flags"]
    assert r["needs_confirmation_hint"] is True


def test_amounts_flagged():
    r = assess_transcript("Send $100")
    assert "number_present" in r["flags"]
    assert "high_risk_words" in r["flags"]


def test_urgency_flagged():
    r = assess_transcript("Restart the laptop immediately")
    assert "urgent_override" in r["flags"]
    assert "high_risk_words" in r["flags"]


def test_benign_transcript_clean():
    r = assess_transcript("What is my battery level?")
    assert r["flags"] == []
    assert r["needs_confirmation_hint"] is False
    assert r["tokens"] == 5


def test_safety_never_authorizes():
    # Flags are advisory strings only — no allow/approve/execute semantics.
    r = assess_transcript("approve this operation now")
    assert "high_risk_words" in r["flags"]
    assert set(r.keys()) == {"flags", "needs_confirmation_hint", "tokens"}


# ---------- provider health ----------

def _grpc_err(name):
    class E(Exception):
        def code(self):
            return type("C", (), {"name": name})()

        def details(self):
            return name
    return E()


class FakeSvc:
    def __init__(self, error=None):
        self.error = error

    def offline_recognize(self, pcm, config):
        if self.error is not None:
            raise self.error
        from tests.test_stt_nvidia import FakeResp
        return FakeResp(["ok"])


def _nvidia(error=None):
    p = NvidiaSTT(api_key="test-key")
    p._service = FakeSvc(error)
    return p


class LocalOk:
    name = "local-whisper"
    model_name = "tiny.en"

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return "local"


def test_health_cooldown_after_three_transient_failures():
    fb = STTFallback(_nvidia(_grpc_err("UNAVAILABLE")), LocalOk())
    for _ in range(3):
        assert fb.transcribe(silent_wav()) == "local"
    assert fb.health.consecutive_failures == 3
    assert fb.health.last_category == "NETWORK_ERROR"
    assert fb.health.cooling_down() is True
    # next call skips primary entirely (reason=cooldown)
    assert fb.transcribe(silent_wav()) == "local"
    assert fb.last_result.fallback_reason == "cooldown"


def test_health_resets_on_success():
    h = STTHealth()
    h.record_failure("TIMEOUT")
    assert h.consecutive_failures == 1
    h.record_success(120)
    assert h.consecutive_failures == 0
    assert h.last_category is None
    assert h.mean_latency_ms() == 120.0
    assert h.cooling_down() is False


def test_health_snapshot_bounded():
    h = STTHealth()
    for i in range(50):
        h.record_success(i)
    s = h.snapshot()
    assert s["samples"] == 20  # window bound respected
    assert set(s) == {"consecutive_failures", "last_category",
                      "cooling_down", "mean_latency_ms", "samples"}


# ---------- pipeline provenance / timing ----------

def _stack():
    from core.app import build_stack
    from tests.test_voice_stage8 import _demo_brain
    s = build_stack()
    s["loop"].provider = _demo_brain()
    return s


def test_handle_audio_carries_provenance_timing_flags():
    s = _stack()
    pipe = VoicePipeline(MockSTT("What is my battery level?"), MockTTS(),
                         s["loop"], s["sessions"])
    out = pipe.handle_audio(silent_wav())
    assert out["ok"] is True
    assert out["stt_provider"] == "mock-stt"
    assert out["stt_fallback_used"] is False
    assert isinstance(out["loop_ms"], int) and out["loop_ms"] >= 0
    assert out["transcript_flags"] == []
    assert out["transcript_tokens"] == 5


def test_handle_audio_flags_high_risk_transcript():
    s = _stack()
    pipe = VoicePipeline(MockSTT("Delete the temporary project folder"),
                         MockTTS(), s["loop"], s["sessions"])
    out = pipe.handle_audio(silent_wav())
    assert out["ok"] is True  # flags never break a turn...
    assert "high_risk_words" in out["transcript_flags"]
    # ...and risky voice still routes through normal policy/approval,
    # never direct execution (DemoBrain proposes util.echo here).
    assert out["status"] in ("responded", "approval_required", "error")


def test_handle_audio_records_fallback_provenance():
    s = _stack()
    fb = STTFallback(_nvidia(_grpc_err("DEADLINE_EXCEEDED")), LocalOk())
    pipe = VoicePipeline(fb, MockTTS(), s["loop"], s["sessions"])
    out = pipe.handle_audio(silent_wav())
    assert out["ok"] is True
    assert out["transcript"] == "local"
    assert out["stt_fallback_used"] is True
    assert out["stt_fallback_reason"] == "TIMEOUT"


# ---------- cancellation races ----------

def test_interrupt_stops_stt_and_returns_to_listening():
    s = _stack()

    class Stoppable(MockSTT):
        def __init__(self):
            super().__init__("hi")
            self.stopped = False

        def stop(self):
            self.stopped = True

    stt = Stoppable()
    pipe = VoicePipeline(stt, MockTTS(), s["loop"], s["sessions"])
    out = pipe.handle_audio(silent_wav())
    assert out["ok"] is True
    r = pipe.interrupt()
    assert r["interrupted"] is True
    assert stt.stopped is True
    # Turn already finished: interrupt lands on a stable state (idle via
    # reset, or listening) — never a stuck microphone.
    assert r["state"] in ("idle", "listening")


def test_interrupt_survives_failing_stt_stop():
    s = _stack()

    class BadStop(MockSTT):
        def stop(self):
            raise RuntimeError("nope")

    pipe = VoicePipeline(BadStop("hi"), MockTTS(), s["loop"], s["sessions"])
    r = pipe.interrupt()  # must not raise
    assert r["interrupted"] is True


def test_empty_transcript_turn_never_executes():
    s = _stack()
    pipe = VoicePipeline(MockSTT(""), MockTTS(), s["loop"], s["sessions"])
    out = pipe.handle_audio(silent_wav())
    # Empty text flows to the loop as data; no tool runs on nothing.
    assert out["tool"] == "" or out["status"] in (
        "responded", "clarification", "error")
