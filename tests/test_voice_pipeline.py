"""Voice pipeline end-to-end: mock STT transcript -> ConversationLoop ->
mock TTS speech. Proves the existing wire (VoicePipeline.handle_audio /
VoiceConversation.turn reusing ConversationLoop.handle_text); no hardware,
no network, no new dependencies, no always-on microphone."""
import threading
import time

from core.app import build_stack
from core.llm import LLMAction, LLMOutcome
from core.providers import ModelProvider
from core.voice import MockSTT, MockTTS, VoiceConversation, VoicePipeline


class BranchBrain(ModelProvider):
    """Scripted provider: the reply depends on the transcript content, so
    a content-correct reply proves the transcript reached the loop."""

    name = "branch"

    def reason(self, system, user, tools, cancel=None, timeout=60.0):
        # Branch on the actual request only: the prompt also carries
        # conversation history, which must not decide the reply.
        request = user.split("USER REQUEST:")[-1].lower()
        if "long reply" in request:
            text = " ".join(["spokenword"] * 60)
        elif "battery" in request:
            text = "Battery is 87 percent."
        elif "lights" in request:
            text = "Turning the lights on now."
        else:
            text = "I heard you."
        action = LLMAction(type="response", text=text)
        return LLMOutcome(action=action, text=text, provider="branch")


def _stack():
    s = build_stack()
    s["loop"].provider = BranchBrain()
    return s


def test_transcript_reaches_loop_and_shapes_reply():
    s = _stack()
    pipe = VoicePipeline(MockSTT("what is my battery level"), MockTTS(),
                         s["loop"], s["sessions"])
    out = pipe.handle_audio(b"fake-audio-bytes")
    assert out["ok"] is True
    assert out["transcript"] == "what is my battery level"
    assert out["reply"] == "Battery is 87 percent."
    assert out["state"] == "idle"
    for stage in ("listening", "transcribing", "thinking", "speaking"):
        assert stage in pipe.states.history

    # A different transcript through the same loop gets a different reply:
    # the reply is content-dependent, not a canned constant.
    pipe2 = VoicePipeline(MockSTT("please turn on the lights"), MockTTS(),
                          s["loop"], s["sessions"])
    out2 = pipe2.handle_audio(b"fake-audio-bytes")
    assert out2["transcript"] == "please turn on the lights"
    assert out2["reply"] == "Turning the lights on now."
    assert out2["reply"] != out["reply"]


def test_reply_reaches_tts_speak():
    s = _stack()
    tts = MockTTS()
    pipe = VoicePipeline(MockSTT("what is my battery level"), tts,
                         s["loop"], s["sessions"])
    out = pipe.handle_audio(b"fake-audio-bytes")
    assert out["reply"] in tts.spoken
    assert out["audio_bytes"] > 0


def test_conversation_turn_orchestrates_end_to_end():
    s = _stack()
    tts = MockTTS()
    pipe = VoicePipeline(MockSTT("what is my battery level"), tts,
                         s["loop"], s["sessions"])
    conv = VoiceConversation(pipe)
    out = conv.turn(b"fake-audio-bytes")
    assert out["ok"] is True and out["turn"] == 1
    assert out["transcript"] == "what is my battery level"
    assert out["reply"] == "Battery is 87 percent."
    assert out["reply"] in tts.spoken


class SlowTTS(MockTTS):
    """Word-chunked TTS with per-chunk delay so a mid-speech interrupt
    lands deterministically inside the stream."""

    name = "slow-mock-tts"

    def __init__(self, delay: float = 0.005) -> None:
        super().__init__()
        self.delay = delay
        self.chunks = 0

    def stream_speak(self, text, cancel=None):
        if cancel is not None and cancel.is_set():
            return
        words = text.split() or [text]
        for i, word in enumerate(words):
            if cancel is not None and cancel.is_set():
                self.spoken.append("[cancelled-partial] "
                                   + " ".join(words[:i]))
                return
            time.sleep(self.delay)
            self.chunks += 1
            yield f"audio:{word[:50]}".encode()
        self.spoken.append(text)


def test_interrupt_cancels_in_flight_speech():
    s = _stack()
    tts = SlowTTS()
    pipe = VoicePipeline(MockSTT("give me the long reply please"), tts,
                         s["loop"], s["sessions"])
    full_reply = " ".join(["spokenword"] * 60)
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.update(
            pipe.handle_audio(b"fake-audio-bytes")))
    worker.start()
    deadline = time.time() + 10
    while tts.chunks < 3 and time.time() < deadline:
        time.sleep(0.005)
    assert tts.chunks >= 3, "speech never started"
    interrupted = pipe.interrupt()
    worker.join(timeout=15)
    assert not worker.is_alive()
    assert interrupted["interrupted"] is True
    assert tts.stopped is True  # TTS stop flag raised
    assert interrupted["state"] in ("idle", "listening")
    assert "interrupted" in pipe.states.history
    assert tts.chunks < 60  # truncated mid-stream, never fully spoken
    assert full_reply not in tts.spoken
    assert any("[cancelled-partial]" in s for s in tts.spoken)


class BoomSTT(MockSTT):
    name = "boom-stt"

    def transcribe(self, audio: bytes) -> str:
        raise RuntimeError("mic dead")


def test_stt_failure_yields_error_without_hallucination():
    s = _stack()
    tts = MockTTS()
    calls: list = []
    orig = s["loop"].handle_text

    def spy(text, **kw):
        calls.append(text)
        return orig(text, **kw)

    s["loop"].handle_text = spy
    try:
        pipe = VoicePipeline(BoomSTT("ignored"), tts,
                             s["loop"], s["sessions"])
        out = pipe.handle_audio(b"fake-audio-bytes")
    finally:
        s["loop"].handle_text = orig
    assert out["ok"] is False
    assert "transcript" not in out  # nothing invented
    assert out["state"] == "error"
    assert "stt:" in out["error"]
    assert calls == []  # the loop never ran on nothing
    assert tts.spoken == []  # nothing spoken on STT failure


def test_secret_in_transcript_never_becomes_memory_fact():
    from core.memory_policy import WriteDecision, classify_write
    s = _stack()
    secret = "hunter2-voicetoken-xyz"
    transcript = f"remember that my password is {secret}"
    # Existing refusal, reused (not reimplemented): secrets never store.
    decision, _ = classify_write(transcript)
    assert decision == WriteDecision.NEVER_STORE

    pipe = VoicePipeline(MockSTT(transcript), MockTTS(),
                         s["loop"], s["sessions"])
    out = pipe.handle_audio(b"fake-audio-bytes")
    assert out["ok"] is True
    assert secret not in out["reply"]
    hits = s["memory"].recall(secret, top_k=5)
    assert all(secret not in h[0].text for h in hits)


def test_text_only_fallback_matches_voice_reply():
    # /v1/talk path: same transcript straight to the loop, no mic/TTS.
    s = _stack()
    tts = MockTTS()
    pipe = VoicePipeline(MockSTT("what is my battery level"), tts,
                         s["loop"], s["sessions"])
    voice_out = pipe.handle_audio(b"fake-audio-bytes")
    text_result = s["loop"].handle_text("what is my battery level")
    assert text_result.reply == voice_out["reply"] == "Battery is 87 percent."


def test_overlapping_turn_supersedes_older_speech():
    s = _stack()
    tts = SlowTTS(delay=0.01)
    pipe1 = VoicePipeline(MockSTT("long reply please"), tts,
                          s["loop"], s["sessions"])
    # Both turns run on one pipeline (shared generation domain, like a
    # real session): the older in-flight speech must truncate.
    results = {}

    def run_a():
        results["a"] = pipe1.handle_audio(b"audio-a")

    t = threading.Thread(target=run_a)
    t.start()
    # Let turn A enter TTS streaming (60 words * 10ms); interrupt it with
    # a newer turn on the same pipeline halfway through.
    time.sleep(0.15)
    results["b"] = pipe1.handle_audio(b"audio-b")
    t.join(timeout=15)
    assert results["a"]["ok"] is True and results["b"]["ok"] is True
    assert results["a"]["superseded"] is True
    assert results["b"]["superseded"] is False
    assert results["a"]["audio_bytes"] < 60 * 8  # 60-word reply, cut short


def test_interrupt_truncates_and_marks_superseded():
    s = _stack()
    tts = SlowTTS(delay=0.01)
    pipe = VoicePipeline(MockSTT("long reply please"), tts,
                         s["loop"], s["sessions"])
    results = {}

    def run():
        results["out"] = pipe.handle_audio(b"audio")

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.15)
    pipe.interrupt()
    t.join(timeout=15)
    out = results["out"]
    assert out["ok"] is True
    assert out["superseded"] is True
    assert out["audio_bytes"] < 60 * 8  # 60-word reply, truncated


def test_sequential_turns_both_complete_clean():
    s = _stack()
    pipe = VoicePipeline(MockSTT("what is my battery level"), MockTTS(),
                         s["loop"], s["sessions"])
    first = pipe.handle_audio(b"a")
    second = pipe.handle_audio(b"b")
    assert first["superseded"] is False
    assert second["superseded"] is False
    assert first["audio_bytes"] > 0 and second["audio_bytes"] > 0


class BoomTTS(MockTTS):
    name = "boom-tts"

    def stream_speak(self, text, cancel=None):
        raise RuntimeError("speaker dead")
        yield b"never"


def test_tts_failure_fails_loudly_no_silent_text():
    s = _stack()
    pipe = VoicePipeline(MockSTT("what is my battery level"), BoomTTS(),
                         s["loop"], s["sessions"])
    out = pipe.handle_audio(b"fake-audio-bytes")
    assert out["ok"] is False
    assert "tts" in out["error"]
    assert out["state"] == "error"


def test_interrupt_when_idle_is_safe_noop():
    s = _stack()
    pipe = VoicePipeline(MockSTT("hello"), MockTTS(),
                         s["loop"], s["sessions"])
    pipe.interrupt()  # no in-flight speech: must not raise or corrupt
    out = pipe.handle_audio(b"fake-audio-bytes")
    assert out["ok"] is True and out["superseded"] is False


class BoomLoop:
    def handle_text(self, *a, **k):
        raise TimeoutError("provider timed out after 60s")


def test_loop_failure_loud_no_crash_no_invented_reply():
    from core.voice import VoicePipeline
    from core.voice import MockSTT, MockTTS
    s = _stack()
    pipe = VoicePipeline(MockSTT("hello there"), MockTTS(),
                         BoomLoop(), s["sessions"])
    out = pipe.handle_audio(b"fake-audio-bytes")
    assert out["ok"] is False
    assert out["error"].startswith("loop:")
    assert "timed out" in out["error"]
    assert out["transcript"] == "hello there"  # STT result preserved
    assert "reply" not in out  # nothing invented
    assert out["state"] == "error"


def test_resume_approval_unknown_id_loud():
    from core.voice import VoiceConversation, VoicePipeline
    from core.voice import MockSTT, MockTTS
    s = _stack()
    pipe = VoicePipeline(MockSTT("hi"), MockTTS(), s["loop"], s["sessions"])
    conv = VoiceConversation(pipe)
    conv.pending_approval = "exec-nonexistent"
    out = conv.resume_approval()
    assert out["ok"] is False  # unknown approval or loop failure: loud
    assert out["error"].startswith("loop:")


def test_tamil_local_model_refusal_before_audio_or_load():
    # tiny.en must never silently transcribe as Tamil: loud refusal
    # before validation, load, or inference (no model download here).
    from core.voice import FasterWhisperSTT
    s = FasterWhisperSTT(model="tiny.en", language="ta-IN")
    with __import__("pytest").raises(RuntimeError, match="unsupported"):
        s.transcribe(b"not-even-wav")


def test_local_whisper_empty_audio_refused():
    import io as _io
    import wave as _wave
    from core.voice import FasterWhisperSTT
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"")
    s = FasterWhisperSTT(model="tiny.en")
    with __import__("pytest").raises(ValueError, match="empty audio"):
        s.transcribe(buf.getvalue())


def test_tamil_tts_voice_mapping_and_unmapped_refusal():
    # Config-level integration only (no network): ta-IN resolves to a
    # real Edge voice; unmapped languages refuse loudly instead of
    # silently falling back to English speech.
    import pytest as _pytest
    from core.voice import EdgeTTSProvider
    assert EdgeTTSProvider(language="ta-IN").voice == \
        "ta-IN-PallaviNeural"
    assert EdgeTTSProvider(language="en-US").voice == "en-US-AriaNeural"
    with _pytest.raises(RuntimeError, match="voice missing"):
        EdgeTTSProvider(language="xx-YY")


def test_tanglish_transcript_preserved_through_voice_turn():
    # A Tanglish STT transcript travels the voice turn untouched (no
    # mangling, no translation at the audio layer); understanding
    # happens downstream in the intent layer (see test_tanglish.py).
    s = _stack()
    tts = MockTTS()
    pipe = VoicePipeline(MockSTT("phone battery evlo irukku"), tts,
                         s["loop"], s["sessions"])
    conv = VoiceConversation(pipe)
    out = conv.turn(b"fake-audio-bytes")
    assert out["ok"] is True and out["turn"] == 1
    assert out["transcript"] == "phone battery evlo irukku"
    assert out["state"] == "idle"
