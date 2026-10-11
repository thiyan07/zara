"""Tamil voice path: honest capability matrix, no acoustic claims."""
from core.voice import voice_capabilities


def test_english_local_supported_when_installed():
    caps = voice_capabilities("en-US")
    assert caps["language"] == "en-US"
    assert caps["stt"]["backend"] == "local-whisper"
    assert caps["stt"]["supported"] is True


def test_tamil_local_stt_refused_with_english_only_model(monkeypatch):
    monkeypatch.setenv("STT_MODEL", "tiny.en")
    caps = voice_capabilities("ta-IN")
    assert caps["stt"]["supported"] is False
    assert "multilingual" in caps["stt"]["reason"]


def test_tamil_local_stt_supported_with_multilingual_model(monkeypatch):
    monkeypatch.setenv("STT_MODEL", "tiny")
    caps = voice_capabilities("ta-IN")
    assert caps["stt"]["supported"] is True
    assert "tiny" in caps["stt"]["reason"]


def test_tamil_tts_never_english_voice_as_tamil(monkeypatch):
    monkeypatch.delenv("TTS_MODEL", raising=False)
    caps = voice_capabilities("ta-IN")
    tts = caps["tts"]
    if tts["supported"]:
        # Only the cloud Tamil voice may serve Tamil — never local English.
        assert tts["backend"] == "edge-tts"
        assert "unvalidated" in tts["reason"]
    else:
        assert "ta" in tts["reason"]


def test_unsupported_language_reported_honestly():
    caps = voice_capabilities("xx-YY")
    assert caps["stt"]["supported"] is False
    assert caps["tts"]["supported"] is False
    assert caps["stt"]["reason"] and caps["tts"]["reason"]
