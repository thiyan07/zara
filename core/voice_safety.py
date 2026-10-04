"""Deterministic voice-transcript safety signals.

No ML, no invented confidence. These are cheap textual checks over an
UNTRUSTED transcript that help the pipeline decide how much
confirmation a voice turn needs. They never authorize anything: the
existing policy + approval flow remains the only authority.

Flags:
  empty               no usable text at all
  too_short           1 word (likely truncation; risky to act on)
  high_risk_words     destructive/transfer verbs present
  missing_target      high-risk verb but no nouns/target alongside
  number_present      digits/number-words (amounts/doses/times are
                      mishearing-sensitive; confirm before acting)
  urgent_override     "emergency/urgent/asap now" pressure language
"""
from __future__ import annotations
import re

_WORD = re.compile(r"[a-z0-9]+")

HIGH_RISK_VERBS = frozenset({
    "delete", "remove", "erase", "wipe", "destroy", "drop", "format",
    "send", "transfer", "pay", "wire", "restart", "reboot", "reset",
    "shutdown", "kill", "approve", "authorize", "grant", "publish",
    "share", "overwrite",
})

NUMBER_WORDS = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
    "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
    "thirty", "forty", "fifty", "hundred", "thousand", "percent",
})

URGENCY_WORDS = frozenset({
    "urgent", "emergency", "asap", "immediately", "rightnow", "hurry",
})


def tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def assess_transcript(text: str) -> dict:
    """Pure function: transcript -> {flags, needs_confirmation_hint}.

    needs_confirmation_hint is advisory ONLY (UI/wording may use it);
    enforcement stays in policy + approvals."""
    toks = tokens(text)
    flags: list[str] = []
    if not toks:
        return {"flags": ["empty"], "needs_confirmation_hint": True,
                "tokens": 0}
    if len(toks) == 1:
        flags.append("too_short")
    verbs = [t for t in toks if t in HIGH_RISK_VERBS]
    if verbs:
        flags.append("high_risk_words")
        nouns = [t for t in toks if t not in HIGH_RISK_VERBS
                 and len(t) > 2]
        if not nouns:
            flags.append("missing_target")
    if any(t.isdigit() for t in toks) or any(t in NUMBER_WORDS
                                             for t in toks):
        flags.append("number_present")
    if any(t in URGENCY_WORDS for t in toks):
        flags.append("urgent_override")
    hint = bool(verbs or len(toks) == 1)
    return {"flags": sorted(flags), "needs_confirmation_hint": hint,
            "tokens": len(toks)}
