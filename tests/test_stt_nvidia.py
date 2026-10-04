"""NVIDIA STT tests. Transport boundary is mocked (fake ASR service);
no network, no key, no audio files committed. Fixtures are synthesized
in-memory (silent WAVs) — never private recordings."""
import io
import wave

import pytest

from core.stt_nvidia import (NO_FALLBACK_CATEGORIES, NvidiaSTT,
                             NvidiaSTTError, STTFallback, STTResult,
                             TRANSIENT_CATEGORIES, _api_key)
from core.voice import _UnavailableLocal, stt_from_env


def silent_wav(seconds=1, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * rate * seconds)
    return buf.getvalue()


class FakeAlt:
    def __init__(self, transcript):
        self.transcript = transcript


class FakeRes:
    def __init__(self, transcripts):
        self.alternatives = [FakeAlt(t) for t in transcripts]


class FakeResp:
    def __init__(self, transcripts):
        self.results = [FakeRes(transcripts)]


class FakeGrpcError(Exception):
    def __init__(self, name, msg="boom"):
        super().__init__(msg)
        self._name = name
        self._msg = msg

    def code(self):
        return type("C", (), {"name": self._name})()

    def details(self):
        return self._msg


class FakeService:
    def __init__(self, transcripts=None, error=None):
        self.transcripts = transcripts
        self.error = error
        self.calls = 0

    def offline_recognize(self, pcm, config):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return FakeResp(self.transcripts or ["hello world"])


def _provider(svc, **kw):
    p = NvidiaSTT(api_key="test-key", **kw)
    p._service = svc
    return p


# ---------- success path ----------

def test_transcribe_returns_text_and_result():
    p = _provider(FakeService(["Hello Zara"]))
    assert p.transcribe(silent_wav()) == "Hello Zara"
    r = p.last_result
    assert isinstance(r, STTResult)
    assert r.provider == "nvidia-parakeet"
    assert r.model == "parakeet-tdt-0_6b-v2"
    assert r.audio_ms == 1000
    assert r.fallback_used is False
    assert r.error_category is None


def test_empty_transcript_is_error_not_silence():
    p = _provider(FakeService(["   "]))
    with pytest.raises(NvidiaSTTError) as e:
        p.transcribe(silent_wav())
    assert e.value.category == "EMPTY_TRANSCRIPT"


def test_malformed_response_is_invalid_response():
    class Bad:
        def offline_recognize(self, pcm, config):
            return object()  # no .results
    p = _provider(Bad())
    with pytest.raises(NvidiaSTTError) as e:
        p.transcribe(silent_wav())
    assert e.value.category == "INVALID_RESPONSE"


# ---------- bounds (no network touched) ----------

def test_oversize_audio_rejected_before_transmit():
    p = _provider(FakeService())
    with pytest.raises(NvidiaSTTError) as e:
        p.transcribe(b"RIFF" + b"\x00" * (2 * 1024 * 1024 + 1))
    assert e.value.category == "INVALID_AUDIO"
    assert p._service.calls == 0


def test_overlong_audio_rejected():
    p = _provider(FakeService())
    with pytest.raises(NvidiaSTTError) as e:
        p.transcribe(silent_wav(seconds=31))
    assert e.value.category == "INVALID_AUDIO"
    assert p._service.calls == 0


def test_invalid_wav_rejected():
    p = _provider(FakeService())
    with pytest.raises(NvidiaSTTError) as e:
        p.transcribe(b"not-a-wav-at-all" * 10)
    assert e.value.category == "INVALID_AUDIO"


def test_empty_bytes_rejected():
    p = _provider(FakeService())
    with pytest.raises(NvidiaSTTError):
        p.transcribe(b"")


# ---------- error categories ----------

def test_auth_error_category_and_value():
    p = _provider(FakeService(error=FakeGrpcError("UNAUTHENTICATED")))
    with pytest.raises(NvidiaSTTError) as e:
        p.transcribe(silent_wav())
    assert e.value.category == "AUTH_ERROR"
    assert "test-key" not in str(e.value)


def test_rate_limited_category():
    p = _provider(FakeService(error=FakeGrpcError("RESOURCE_EXHAUSTED")))
    with pytest.raises(NvidiaSTTError) as e:
        p.transcribe(silent_wav())
    assert e.value.category == "RATE_LIMITED"


def test_transient_retried_once_then_succeeds():
    svc = FakeService(error=FakeGrpcError("UNAVAILABLE", "reset"))
    p = _provider(svc)
    svc.error = None  # second attempt succeeds (calls counted below)
    # NOTE: error cleared before call -> single call; retry path tested next
    assert p.transcribe(silent_wav()) == "hello world"
    assert svc.calls == 1


def test_transient_retry_then_success():
    calls = {"n": 0}

    class Flaky:
        def offline_recognize(self, pcm, config):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FakeGrpcError("DEADLINE_EXCEEDED")
            return FakeResp(["recovered"])

    p = _provider(Flaky())
    assert p.transcribe(silent_wav()) == "recovered"
    assert calls["n"] == 2


def test_auth_never_retried():
    svc = FakeService(error=FakeGrpcError("PERMISSION_DENIED"))
    p = _provider(svc)
    with pytest.raises(NvidiaSTTError):
        p.transcribe(silent_wav())
    assert svc.calls == 1


# ---------- configuration / secrets ----------

def test_missing_key_is_configuration_error(monkeypatch):
    monkeypatch.delenv("NVIDIA_STT_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(NvidiaSTTError) as e:
        _api_key("")
    assert e.value.category == "CONFIGURATION_ERROR"


def test_key_never_in_errors(monkeypatch):
    monkeypatch.setenv("NVIDIA_STT_API_KEY", "nvapi-SUPERSECRETKEY123")
    p = NvidiaSTT()  # no service: connect path exercises key handling
    with pytest.raises(NvidiaSTTError) as e:
        p.transcribe(silent_wav())
    assert "SUPERSECRETKEY" not in str(e.value)
    assert p.availability()["configured"] is True
    assert "SUPERSECRETKEY" not in str(p.availability())


def test_availability_reports_not_configured(monkeypatch):
    monkeypatch.delenv("NVIDIA_STT_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    p = NvidiaSTT()
    assert p.availability()["configured"] is False


def test_stop_closes_service():
    closed = []

    class Svc(FakeService):
        pass
    p = _provider(Svc(["hi"]))
    p._service = None  # force lazy path? no: test explicit close
    p._service = Svc(["hi"])
    p.stop()  # must not raise; service dropped
    assert p._service is None


# ---------- fallback wrapper ----------

class LocalOk:
    name = "local-whisper"
    model_name = "tiny.en"

    def __init__(self, text="local text"):
        self.text = text
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return self.text


class LocalFail:
    name = "local-whisper"

    def transcribe(self, audio):
        raise RuntimeError("local exploded")


def test_fallback_used_on_transient_with_reason():
    primary = _provider(FakeService(error=FakeGrpcError("UNAVAILABLE")))
    fb = STTFallback(primary, LocalOk())
    assert fb.transcribe(silent_wav()) == "local text"
    assert fb.last_result.fallback_used is True
    assert fb.last_result.fallback_reason == "NETWORK_ERROR"
    assert fb.last_result.provider == "local-whisper"


def test_no_fallback_on_auth():
    primary = _provider(FakeService(error=FakeGrpcError("UNAUTHENTICATED")))
    local = LocalOk()
    fb = STTFallback(primary, local)
    with pytest.raises(NvidiaSTTError) as e:
        fb.transcribe(silent_wav())
    assert e.value.category == "AUTH_ERROR"
    assert local.calls == 0


def test_no_fallback_on_invalid_audio():
    primary = _provider(FakeService())
    local = LocalOk()
    fb = STTFallback(primary, local)
    with pytest.raises(NvidiaSTTError):
        fb.transcribe(b"garbage-not-wav")
    assert local.calls == 0


def test_primary_error_surfaced_when_local_also_fails():
    primary = _provider(FakeService(error=FakeGrpcError("UNAVAILABLE")))
    fb = STTFallback(primary, LocalFail())
    with pytest.raises(NvidiaSTTError) as e:
        fb.transcribe(silent_wav())
    assert e.value.category == "NETWORK_ERROR"


def test_primary_success_records_no_fallback():
    primary = _provider(FakeService(["direct hit"]))
    fb = STTFallback(primary, LocalOk())
    assert fb.transcribe(silent_wav()) == "direct hit"
    assert fb.last_result.fallback_used is False
    assert fb.last_result.provider == "nvidia-parakeet"


def test_unavailable_local_never_succeeds_silently():
    with pytest.raises(RuntimeError):
        _UnavailableLocal().transcribe(silent_wav())


def test_fallback_availability_shape():
    from core.stt_nvidia import STTFallback
    fb = STTFallback(_provider(FakeService()), LocalOk())
    a = fb.availability()
    assert a["primary"]["backend"] == "nvidia-parakeet"
    assert "fallback" in a


# ---------- factory ----------

def test_stt_from_env_nvidia_builds_wrapper(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "nvidia")
    monkeypatch.setenv("NVIDIA_STT_API_KEY", "test-key")
    from core.stt_nvidia import STTFallback as FB
    from core.stt_nvidia import NvidiaSTT as NS
    p = stt_from_env()
    assert isinstance(p, FB)
    assert isinstance(p.primary, NS)
    assert p.primary.function_id == "d3fe9151-442b-4204-a70d-5fcc597fd610"


def test_stt_from_env_local_unchanged(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "local")
    p = stt_from_env()
    assert p.name == "local-whisper"


def test_categories_partitioned():
    assert "AUTH_ERROR" in NO_FALLBACK_CATEGORIES
    assert "INVALID_AUDIO" in NO_FALLBACK_CATEGORIES
    assert "CONFIGURATION_ERROR" in NO_FALLBACK_CATEGORIES
    assert "TIMEOUT" in TRANSIENT_CATEGORIES
    assert "NETWORK_ERROR" in TRANSIENT_CATEGORIES
    assert not (TRANSIENT_CATEGORIES & NO_FALLBACK_CATEGORIES)
