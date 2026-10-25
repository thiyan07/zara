"""Stage 31-P4 close-out (2026-10-22, user request): the talk loop's
deterministic fastpath.

User-visible bug: typing 'what is next today?' in the phone's
conversation view reached the LLM (default = EchoProvider, which
echoes the prompt skeleton — the operator saw "echo ... RELEVANT
LONG-TERM MEMORY ..."). Root cause: the deterministic flows lived
only on /v1/intent; the talk path never consulted them.

Fix: ConversationLoop.intent_fastpath — the read-only laptop flows
(calendar, weather) answer BEFORE any model call; everything else
falls through to the provider unchanged. Device flows stay OUT
(their approval missions live on /v1/intent).
"""
import os
import tempfile

from core.app import build_stack
from core.providers import ScriptedProvider


class CountingProvider:
    name = "counting"

    def __init__(self) -> None:
        self.reason_calls = 0

    def reason(self, system, user, tools, cancel=None, timeout=60.0):
        self.reason_calls += 1
        from core.llm import LLMAction, LLMOutcome
        return LLMOutcome(action=LLMAction(type="response",
                                           text="LLM-WAS-CALLED"),
                          text="LLM-WAS-CALLED", provider=self.name)


def make_looped_stack():
    stack = build_stack()
    loop = stack["loop"]
    loop.provider = CountingProvider()
    return stack, loop


def test_fastpath_answers_before_any_model_call():
    _, loop = make_looped_stack()
    loop.intent_fastpath = lambda t, w: (
        {"message": "flow-reply", "executed": True}
        if "next" in t.lower() else None)
    out = loop.handle_text("what is next today?")
    assert out.reply == "flow-reply", out.reply
    assert loop.provider.reason_calls == 0  # model never consulted
    assert out.status == "responded"


def test_fastpath_miss_falls_through_to_the_provider():
    _, loop = make_looped_stack()
    loop.intent_fastpath = lambda t, w: None
    loop.provider = _EchoLike()  # the default dev stub
    out = loop.handle_text("hi")
    assert "echo mode" in out.reply
    assert "language model" in out.reply
    assert "mission" not in out.reply.lower() or "-> responded" \
        not in out.reply  # internals never leak to the user
    assert out.status == "responded"


class _EchoLike:
    name = "echo"

    def reason(self, system, user, tools, cancel=None, timeout=60.0):
        from core.llm import LLMAction, LLMOutcome
        return LLMOutcome(action=LLMAction(type="response",
                                           text=f"echo: {user[:200]}"),
                          text=f"echo: {user[:200]}", provider="echo")


def test_fastpath_miss_on_a_real_provider_still_reaches_the_model():
    """scripted/mock providers (tests, previews) are NOT intercepted —
    only the default echo stub gets the honest placeholder."""
    _, loop = make_looped_stack()
    counting = CountingProvider()
    loop.provider = counting
    loop.intent_fastpath = lambda t, w: None
    out = loop.handle_text("tell me a joke")
    assert out.reply == "LLM-WAS-CALLED", out.reply
    assert counting.reason_calls == 1


def test_real_intent_flows_answer_in_conversation_end_to_end():
    """The REAL bridge: the injected IntentTurnService fastpath with
    an actual .ics on disk answers the calendar invite inside the
    talk loop — the utterance class that showed the echo bug."""
    stack, loop = make_looped_stack()
    loop.intent_fastpath = stack["intent"].conversational_fastpath
    tmp = tempfile.mkdtemp()
    # P5 ⑨ tz-honest: the old fixture stamped LOCAL now+2h with a Z
    # suffix — true only on a UTC host; on IST that instant lies
    # 3.5h in the past. An ALL-DAY calendar-TODAY event is clock
    # independent (runs its whole local day) and never flakes.
    today = __import__("datetime").date.today()
    p = os.path.join(tmp, "cal.ics")
    open(p, "w").write("BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
                       "SUMMARY:Fastpath lab event\n"
                       f"DTSTART;VALUE=DATE:{today.strftime('%Y%m%d')}\n"
                       "END:VEVENT\nEND:VCALENDAR\n")
    os.environ["ZARA_ICS_PATH"] = p
    try:
        out = loop.handle_text("what is next today?")
        assert "Fastpath lab event" in out.reply, out.reply
        assert loop.provider.reason_calls == 0, "LLM must stay unused"
        assert out.status == "responded"
    finally:
        os.environ.pop("ZARA_ICS_PATH", None)


def test_diary_flows_join_talk_fastpath_batch32():
    """Batch-32 live fix (2026-10-26, phone defect): the talk stream
    sent 'mark gym done' to the model, which answered 'which mission
    do you want to mark as completed?' and the user's habit never got
    marked. The diary flows (ledger, capture, habits) now answer on
    the talk path exactly like /v1/intent — data writes only, no
    device authority, so the fastpath posture is unchanged."""
    stack, loop = make_looped_stack()
    loop.intent_fastpath = stack["intent"].conversational_fastpath
    out = loop.handle_text("add habit gym every day")
    assert "Habit set: 'gym'" in out.reply, out.reply
    assert loop.provider.reason_calls == 0   # model never consulted
    out = loop.handle_text("mark gym done")
    assert "Marked 'gym' done" in out.reply, out.reply
    out = loop.handle_text("mark gym done")
    assert "chain does not grow twice" in out.reply, out.reply
    out = loop.handle_text("show my habits")
    assert "Habits — 'gym'" in out.reply, out.reply
    out = loop.handle_text("budget status")
    assert "No budget set yet" in out.reply, out.reply
    # a conversational non-diary ask still reaches the model
    out = loop.handle_text("tell me a joke")
    assert out.reply == "LLM-WAS-CALLED", out.reply
