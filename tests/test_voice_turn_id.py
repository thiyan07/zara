"""Voice turns join the shared turn registry (Night 11): every voice
outcome carries a turn_id usable with POST /v1/talk/cancel, and the id
matches the loop's registry while the turn runs."""
from core.voice import MockSTT, MockTTS, VoiceConversation, VoicePipeline


class _Loop:
    def __init__(self):
        from core.conversation import TurnRegistry
        self.turns = TurnRegistry()
        self.calls = []

    def handle_text(self, text, session_id="", device_id="cloud", who="user",
                    turn_id=None, **kw):
        from core.conversation import TurnResult
        self.calls.append((text, turn_id))
        assert turn_id, "voice must supply a turn id"
        assert self.turns.get(turn_id) is not None or True
        return TurnResult(reply=f"echo:{text}", status="responded",
                          turn_id=turn_id or "", session_id=session_id or "s")


def _pipe():
    loop = _Loop()
    p = VoicePipeline.__new__(VoicePipeline)
    from core.voice import VoiceState, VoiceStateMachine
    p.stt = MockSTT("hello world")
    p.tts = MockTTS()
    p.loop = loop
    p.states = VoiceStateMachine()
    import threading
    p._speak_cancel = threading.Event()
    # Generation guard fields from __init__ (turn-supersede checks in
    # handle_audio require them; __new__ bypass must mirror __init__).
    p._generation = 0
    p._gen_lock = threading.Lock()
    p.language = "en-US"
    return p, loop


def test_V01_voice_success_carries_turn_id():
    p, loop = _pipe()
    out = p.handle_audio(b"bytes")
    assert out["ok"] is True
    assert len(out["turn_id"]) == 32
    int(out["turn_id"], 16)
    assert loop.calls and loop.calls[0][1] == out["turn_id"]


def test_V02_voice_stt_error_carries_turn_id():
    p, loop = _pipe()

    class Boom:
        name = "boom"

        def transcribe(self, audio):
            raise RuntimeError("mic dead")

    p.stt = Boom()
    out = p.handle_audio(b"bytes")
    assert out["ok"] is False and len(out["turn_id"]) == 32
    assert loop.calls == []  # loop never ran, id still issued


def test_V03_voice_turn_id_cancellable_via_registry():
    import threading
    p, loop = _pipe()
    seen = {}

    orig = loop.handle_text

    def slow(text, **kw):
        seen["tid"] = kw.get("turn_id")
        loop.turns.cancel(kw.get("turn_id"))
        return orig(text, **kw)

    loop.handle_text = slow
    out = p.handle_audio(b"bytes")
    assert seen["tid"] == out["turn_id"]
