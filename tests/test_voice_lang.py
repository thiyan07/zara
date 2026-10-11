"""Voice language plumbing (ZARA_VOICE_LANG): parsing/validation, loud
local-language failures, per-turn language tags, secret-free errors.

Mock-only: no network, no model downloads, no hardware. The local
FasterWhisper/Piper language gates fire BEFORE any audio validation or
model load, so these tests never touch weights, mic, or disk voices.
"""
import warnings
from types import SimpleNamespace

import pytest

from core import voice as V
from core.voice import (EdgeTTSProvider, FasterWhisperSTT, MockSTT, MockTTS,
                        PiperTTS, VoiceConversation, VoicePipeline,
                        parse_voice_lang, stt_from_env, tts_from_env)


@pytest.fixture(autouse=True)
def clean_lang_env(monkeypatch):
    for var in ("ZARA_VOICE_LANG", "STT_PROVIDER", "TTS_PROVIDER",
                "NVIDIA_STT_LANGUAGE", "TTS_EDGE_VOICE", "TTS_MODEL",
                "STT_MODEL"):
        monkeypatch.delenv(var, raising=False)


# ---------- parsing / validation / fallback ----------

def test_default_lang_is_en_us(recwarn):
    assert parse_voice_lang() == "en-US"
    assert [w for w in recwarn.list
            if "ZARA_VOICE_LANG" in str(w.message)] == []


@pytest.mark.parametrize("raw", ["en-US", "ta-IN", "ta", "en-GB", "fr"])
def test_valid_langs_pass_through(monkeypatch, raw):
    monkeypatch.setenv("ZARA_VOICE_LANG", raw)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no warning on valid input
        assert parse_voice_lang() == raw


@pytest.mark.parametrize("raw", ["tamil", "EN-us", "en_us", "e", "en-USA",
                                 "", "eng-US", "en--US", "12", "en-uS1"])
def test_invalid_langs_fall_back_with_warning(raw):
    with pytest.warns(UserWarning, match="ZARA_VOICE_LANG"):
        assert parse_voice_lang(raw) == "en-US"


def test_invalid_env_lang_falls_back_with_warning(monkeypatch):
    monkeypatch.setenv("ZARA_VOICE_LANG", "tamil")
    with pytest.warns(UserWarning, match="ZARA_VOICE_LANG"):
        assert parse_voice_lang() == "en-US"


# ---------- loud local-language failures (never silent wrong language) ----------

def test_english_only_model_rejects_tamil_loudly():
    stt = FasterWhisperSTT(model="tiny.en", language="ta-IN")
    with pytest.raises(RuntimeError, match="language unsupported by "
                                           "local model"):
        stt.transcribe(b"whatever-bytes")


def test_english_still_uses_english_code():
    # Default construction is unchanged: English on tiny.en passes the
    # gate (it then fails on the fake wav — proving the gate, not the
    # model, is what guards language).
    stt = FasterWhisperSTT(model="tiny.en", language="en-US")
    with pytest.raises(Exception) as exc:
        stt.transcribe(b"not-a-wav")
    assert "language unsupported by local model" not in str(exc.value)


def test_multilingual_model_passes_language_gate():
    stt = FasterWhisperSTT(model="tiny", language="ta")
    with pytest.raises(Exception) as exc:
        stt.transcribe(b"not-a-wav")
    # Gate passed (no language refusal); failure is downstream audio
    # validation — no model was loaded, nothing downloaded.
    assert "language unsupported by local model" not in str(exc.value)


def test_piper_tamil_names_missing_voice():
    tts = PiperTTS(language="ta-IN")
    with pytest.raises(RuntimeError, match="piper voice missing for "
                                           "language 'ta-IN'"):
        tts.speak("vanakkam")
    with pytest.raises(RuntimeError, match="piper voice missing"):
        list(tts.stream_speak("vanakkam"))


def test_piper_english_unaffected():
    tts = PiperTTS(language="en-US")
    try:
        out = tts.speak("hello")
    except Exception as exc:  # noqa: BLE001 — no voice installed here
        assert "piper voice missing for language" not in str(exc)
    else:
        assert out  # English synthesis works; gate did not interfere


def test_edge_tamil_maps_to_tamil_voice():
    tts = EdgeTTSProvider(language="ta-IN")
    assert "ta-IN" in tts.voice


def test_edge_unmapped_non_english_fails_loudly():
    with pytest.raises(RuntimeError, match="edge-tts voice missing for "
                                           "language"):
        EdgeTTSProvider(language="xx-YY")


def test_edge_explicit_voice_wins():
    tts = EdgeTTSProvider(voice="custom-Voice", language="ta-IN")
    assert tts.voice == "custom-Voice"


# ---------- env threading ----------

def test_stt_from_env_threads_lang_to_local(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "local")
    monkeypatch.setenv("ZARA_VOICE_LANG", "ta-IN")
    stt = stt_from_env()
    assert isinstance(stt, FasterWhisperSTT)
    assert stt.language == "ta-IN"


def test_stt_from_env_nvidia_threads_lang(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "nvidia")
    monkeypatch.setenv("ZARA_VOICE_LANG", "ta-IN")
    combo = stt_from_env()
    assert combo.primary.language == "ta-IN"
    assert combo.fallback.language == "ta-IN"


def test_stt_nvidia_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "nvidia")
    monkeypatch.setenv("ZARA_VOICE_LANG", "ta-IN")
    monkeypatch.setenv("NVIDIA_STT_LANGUAGE", "en-US")
    combo = stt_from_env()
    assert combo.primary.language == "en-US"


def test_tts_from_env_threads_lang(monkeypatch):
    from core.tts_nvidia import MagpieTTS
    monkeypatch.setenv("TTS_PROVIDER", "nvidia")
    monkeypatch.setenv("ZARA_VOICE_LANG", "ta-IN")
    tts = tts_from_env()
    assert isinstance(tts, MagpieTTS)
    assert tts.language == "ta-IN"


# ---------- language tag propagation ----------

def _fake_loop():
    def handle_text(text, **kw):
        return SimpleNamespace(reply=f"heard:{text}", status="ok",
                               tool="", mission_id="m1", execution_id="e1",
                               device_id="d1")
    return SimpleNamespace(handle_text=handle_text)


def test_pipeline_result_carries_language():
    pipe = VoicePipeline(MockSTT("hi"), MockTTS(), _fake_loop(), None,
                         language="ta-IN")
    out = pipe.handle_audio(b"bytes")
    assert out["ok"] is True
    assert out["language"] == "ta-IN"


def test_pipeline_error_carries_language():
    class Boom(MockSTT):
        def transcribe(self, audio):
            raise RuntimeError("mic dead")
    pipe = VoicePipeline(Boom("x"), MockTTS(), _fake_loop(), None,
                         language="ta-IN")
    out = pipe.handle_audio(b"bytes")
    assert out["ok"] is False
    assert out["language"] == "ta-IN"


def test_conversation_mixed_language_tags():
    pipe = VoicePipeline(MockSTT("hi"), MockTTS(), _fake_loop(), None,
                         language="en-US")
    conv = VoiceConversation(pipe)
    first = conv.turn(b"a", language="en-US")
    second = conv.turn(b"b", language="ta-IN")
    assert first["language"] == "en-US"
    assert second["language"] == "ta-IN"
    assert conv.languages == ["en-US", "ta-IN"]


def test_conversation_default_tag_is_pipeline_language():
    pipe = VoicePipeline(MockSTT("hi"), MockTTS(), _fake_loop(), None,
                         language="ta-IN")
    conv = VoiceConversation(pipe)
    out = conv.turn(b"a")
    assert out["language"] == "ta-IN"
    assert conv.languages == ["ta-IN"]


# ---------- secret-free ----------

def test_errors_never_carry_key_material(monkeypatch):
    sentinel = "sentinel-key-xyz-123"
    monkeypatch.setenv("NVIDIA_STT_API_KEY", sentinel)
    monkeypatch.setenv("NVIDIA_TTS_API_KEY", sentinel)
    monkeypatch.setenv("LLM_API_KEY", sentinel)

    class Boom(MockSTT):
        def transcribe(self, audio):
            raise RuntimeError("mic dead")
    pipe = VoicePipeline(Boom("x"), MockTTS(), _fake_loop(), None,
                         language="ta-IN")
    out = pipe.handle_audio(b"bytes")
    assert sentinel not in repr(out)

    from core.stt_nvidia import NvidiaSTT
    from core.tts_nvidia import MagpieTTS
    assert sentinel not in repr(NvidiaSTT().availability())
    assert sentinel not in repr(MagpieTTS().availability())
    assert sentinel not in parse_voice_lang("ta-IN")
