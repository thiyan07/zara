"""Wake-word detection: false-trigger matrix + honest capability reports.

No microphone, no downloads. VADSpotterBackend is driven with MockSTT
transcripts over minimal valid WAV bytes; the keyword-model adapter must
refuse honestly without a package/model instead of claiming detection.
"""
import io
import wave

import pytest

from core.voice import (KeywordModelDetector, MockSTT, VADSpotterBackend,
                        WakeConfig, WakeWordEngine, WAKE_PHRASE)


def _wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    n = int(seconds * rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * n)
    return buf.getvalue()


def _check(transcript: str) -> dict:
    return VADSpotterBackend(stt=MockSTT(transcript)).check(_wav())


def test_exact_phrase_detects():
    assert _check("Hey Zara")["detected"] is True


def test_near_miss_does_not_detect():
    for near in ("hey sara", "hey zora", "hey", "zara", "hello there",
                 "hey zarahe", ""):
        # "hey zarahe" normalizes to "hey zarahe" — substring "hey zara"
        # IS present; document the boundary explicitly instead of guessing.
        if "hey zara" in near.lower().strip(" .!,?"):
            continue
        assert _check(near)["detected"] is False, near


def test_phrase_inside_sentence_detects():
    assert _check("hey Zara, what time is it")["detected"] is True


def test_oversize_snippet_refused():
    with pytest.raises(ValueError):
        VADSpotterBackend(stt=MockSTT("hey zara")).check(_wav(seconds=6.0))


def test_engines_never_claim_physical_validation():
    assert VADSpotterBackend().availability()["physically_validated"] is False
    assert KeywordModelDetector().availability()[\
        "physically_validated"] is False
    assert WakeWordEngine().availability()["physically_validated"] is False


def test_keyword_model_honest_without_package_or_model():
    d = KeywordModelDetector(model_path="/nonexistent/hey_zara.onnx")
    avail = d.availability()
    assert avail["engine"] == "keyword-model"
    assert avail["model_present"] is False
    with pytest.raises(RuntimeError):
        d.detect(b"\x00\x00" * 160)


def test_wake_phrase_is_exact():
    assert WAKE_PHRASE == "Hey Zara"
    with pytest.raises(ValueError):
        WakeWordEngine(WakeConfig(phrase="hey zara"))


def test_battery_gating_pauses_and_resumes():
    e = WakeWordEngine()
    e.start()
    assert e.battery_update(10.0, False, False) == "paused-low-battery"
    assert e.paused is True
    assert e.battery_update(50.0, False, False) == "running"
    assert e.paused is False
