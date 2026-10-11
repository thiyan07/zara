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
