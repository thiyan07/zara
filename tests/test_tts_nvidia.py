"""NVIDIA Magpie TTS tests. Transport boundary is mocked (fake riva
service); no network, no key, no audio files committed."""
import threading

import pytest

from core.tts_nvidia import (DEFAULT_FUNCTION_ID, DEFAULT_VOICE, MagpieError,
                             MagpieTTS, _api_key)
from core.voice import tts_from_env


class FakeCall:
    def __init__(self, chunks=None, error=None):
        self.chunks = chunks or []
        self.error = error
        self.cancelled = False

    def __iter__(self):
        if self.error is not None:
            raise self.error
        return iter(self.chunks)

    def cancel(self):
        self.cancelled = True


class FakeResp:
    def __init__(self, audio):
        self.audio = audio


class FakeStub:
    def __init__(self, pcm=b"\x01\x00" * 8000, error=None):
        self.pcm = pcm
        self.error = error
        self.calls = []

    def SynthesizeOnline(self, reqs, metadata=None, timeout=None):
        self.calls.append((list(reqs), metadata, timeout))
        return FakeCall(chunks=[FakeResp(self.pcm)], error=self.error)


class FakeService:
    def __init__(self, stub):
        self.stub = stub
        self.auth = FakeAuth()


class FakeAuth:
    def get_auth_metadata(self):
        return [["function-id", DEFAULT_FUNCTION_ID],
                ["authorization", "Bearer test-key"]]


def _provider(stub):
    p = MagpieTTS(api_key="test-key")
    p._service = FakeService(stub)
    return p


def test_factory_defaults_to_piper():
    import os
    os.environ.pop("TTS_PROVIDER", None)
    assert type(tts_from_env()).__name__ == "PiperTTS"


def test_factory_selects_nvidia_and_alias():
    import os
    os.environ["TTS_PROVIDER"] = "nvidia"
    try:
        assert type(tts_from_env()).__name__ == "MagpieTTS"
        os.environ["TTS_PROVIDER"] = "magpie"
        assert type(tts_from_env()).__name__ == "MagpieTTS"
        os.environ["TTS_PROVIDER"] = "piper"
        assert type(tts_from_env()).__name__ == "PiperTTS"
    finally:
        del os.environ["TTS_PROVIDER"]


def test_factory_env_wiring():
    import os
    os.environ["TTS_PROVIDER"] = "nvidia"
    os.environ["TTS_NVIDIA_VOICE"] = "Custom.Voice"
    os.environ["TTS_NVIDIA_FUNCTION_ID"] = "fid-123"
    try:
        p = tts_from_env()
        assert p.voice == "Custom.Voice" and p.function_id == "fid-123"
        assert p.server == "grpc.nvcf.nvidia.com:443"
    finally:
        del os.environ["TTS_PROVIDER"]
        del os.environ["TTS_NVIDIA_VOICE"]
        del os.environ["TTS_NVIDIA_FUNCTION_ID"]


def test_speak_wraps_valid_wav():
    from core.audio import validate_wav
    p = _provider(FakeStub())
    data = p.speak("Hello, I am Zara.")
    props = validate_wav(data)
    assert props["rate"] == 22050 and props["channels"] == 1
    assert props["frames"] == 8000
    reqs, metadata, timeout = p._service.stub.calls[0]
    assert reqs[0].voice_name == DEFAULT_VOICE
    assert reqs[0].sample_rate_hz == 22050
    assert dict(metadata)["function-id"] == DEFAULT_FUNCTION_ID
    assert timeout == 60.0


def test_request_text_bounded_and_chunked():
    p = _provider(FakeStub())
    long_text = "Hello world. " * 60  # >200 chars -> multiple requests
    p.speak(long_text)
    assert len(p._service.stub.calls) >= 2
    for reqs, _, _ in p._service.stub.calls:
        assert len(reqs[0].text) <= 200
    with pytest.raises(ValueError):
        p.speak("")
    with pytest.raises(ValueError):
        p.speak("x" * 2001)


def test_grpc_error_mapping_no_key_leak():
    class GrpcErr(Exception):
        def code(self):
            class C:
                name = "UNAUTHENTICATED"
            return C()

        def details(self):
            return "bad nvapi-SECRETKEY123 key"

    p = _provider(FakeStub(error=GrpcErr()))
    with pytest.raises(MagpieError) as e:
        p.speak("hi")
    assert "nvapi-SECRETKEY123" not in str(e.value)
    assert "UNAUTHENTICATED" in str(e.value) or "authentication" in str(e.value)


def test_empty_audio_rejected():
    p = _provider(FakeStub(pcm=b""))
    with pytest.raises(MagpieError):
        p.speak("hi")


def test_cancel_stops_stream():
    p = _provider(FakeStub())
    cancel = threading.Event()
    cancel.set()
    assert list(p.stream_speak("Hello. World.", cancel=cancel)) == []
    chunks = list(p.stream_speak("Hello. World."))
    assert len(chunks) >= 2  # sentence-chunked like Piper


def test_missing_key_and_model_config():
    import os
    saved = {v: os.environ.pop(v, None)
             for v in ("NVIDIA_TTS_API_KEY", "LLM_API_KEY")}
    try:
        with pytest.raises(MagpieError):
            _api_key()
    finally:
        for v, val in saved.items():
            if val is not None:
                os.environ[v] = val


def test_offline_failure_is_truthful():
    p = MagpieTTS(api_key="k")
    p._service = FakeService(FakeStub(error=OSError("network down")))
    with pytest.raises(MagpieError) as e:
        p.speak("hi")
    assert "network down" in str(e.value) or "failed" in str(e.value)


def test_availability_reports_honestly():
    p = MagpieTTS(api_key="k")
    a = p.availability()
    assert a["offline"] is False and a["backend"] == "magpie-nim"
    assert "k" != a.get("key_configured")  # boolean, never the value
    assert a["key_configured"] is True
    q = MagpieTTS()
    import os
    saved = {v: os.environ.pop(v, None)
             for v in ("NVIDIA_TTS_API_KEY", "LLM_API_KEY")}
    try:
        assert q.availability()["key_configured"] is False
    finally:
        for v, val in saved.items():
            if val is not None:
                os.environ[v] = val


def test_no_key_in_trace_or_audit():
    from core.tracing import redact
    assert "nvapi-SECRETKEY123" not in redact("Bearer nvapi-SECRETKEY123")
