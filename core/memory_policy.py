"""Deterministic memory-write policy + memory service. The LLM may SUGGEST
a memory; this policy DECIDES. Secrets are never stored, everything else
needs a category: AUTO_SAVE / CANDIDATE / SESSION_ONLY / NEVER_STORE.
"""
from __future__ import annotations
import re
from enum import Enum
from typing import Optional
from .memory import SECRET_MARKERS, MemoryStore
from .models import MemoryItem, utcnow

EXPLICIT_REMEMBER = [re.compile(p, re.I) for p in
                     (r"\bremember\s+(that|this)?", r"\bdon'?t forget\b",
                      r"\bkeep in mind\b", r"\bnote (that|this)?\b")]
PREFERENCE = [re.compile(p, re.I) for p in
              (r"\bi prefer\b", r"\bmy favo(u?)rite\b", r"\bi (usually|always)\b",
               r"\bi like\b.{0,40}\bfor\b")]
CANDIDATE_FACT = [re.compile(p, re.I) for p in
                  (r"\bmy \w+ is\b", r"\bi am\b", r"\bi work\b", r"\bwe use\b",
                   r"\b\w+ uses \w+", r"\bproject \w+ uses\b")]
SESSION_HINTS = [re.compile(p, re.I) for p in
                 (r"^\s*(hi|hey|hello|thanks|thank you|ok|okay|yes|no|sure)\s*[.!]?\s*$",
                  r"^(what|when|where|who|how|can you|could you|please|check|run|show|tell me|is|are|do)\b")]


class WriteDecision(str, Enum):
    AUTO_SAVE = "auto_save"
    CANDIDATE = "candidate"
    SESSION_ONLY = "session_only"
    NEVER_STORE = "never_store"


def classify_write(text: str) -> tuple[WriteDecision, str]:
    """Pure deterministic classifier. Returns (decision, reason)."""
    t = (text or "").strip()
    if not t:
        return WriteDecision.SESSION_ONLY, "empty"
    if any(p.search(t) for p in SECRET_MARKERS):
        return WriteDecision.NEVER_STORE, "secret material"
    if any(p.search(t) for p in EXPLICIT_REMEMBER):
        return WriteDecision.AUTO_SAVE, "explicit remember request"
    if any(p.search(t) for p in PREFERENCE):
        return WriteDecision.AUTO_SAVE, "stated preference"
    if any(p.search(t) for p in CANDIDATE_FACT):
        return WriteDecision.CANDIDATE, "potential durable fact"
    if any(p.search(t) for p in SESSION_HINTS) or len(t) < 40:
        return WriteDecision.SESSION_ONLY, "transient conversation"
    return WriteDecision.CANDIDATE, "long statement, needs confidence"


def _category(text: str) -> str:
    t = text.lower()
    if any(p.search(t) for p in PREFERENCE):
        return "preferences"
    if "project" in t or "repo" in t or "mission" in t:
        return "project"
    return "episodic"


class MemoryService:
    """Policy-gated writes + correction/supersede over a MemoryStore."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.pending_candidates: dict[str, MemoryItem] = {}

    def consider(self, text: str, source: str = "conversation",
                 importance: Optional[float] = None) -> dict:
        decision, reason = classify_write(text)
        if decision == WriteDecision.NEVER_STORE:
            return {"stored": False, "decision": decision.value,
                    "reason": reason}
        if decision == WriteDecision.SESSION_ONLY:
            return {"stored": False, "decision": decision.value,
                    "reason": reason}
        item = MemoryItem(category=_category(text), text=text.strip(),
                          source=source,
                          importance=importance or
                          (0.9 if decision == WriteDecision.AUTO_SAVE else 0.5),
                          metadata={"privacy": "user-private",
                                    "write_decision": decision.value})
        if decision == WriteDecision.CANDIDATE:
            item.id = f"cand-{abs(hash(text)) % 10**8:08d}"
            self.pending_candidates[item.id] = item
            return {"stored": False, "decision": decision.value,
                    "reason": reason, "candidate_id": item.id}
        stored = self.store.remember(item)
        return {"stored": stored is not None, "decision": decision.value,
                "reason": reason, "id": stored.id if stored else None}

    def confirm_candidate(self, candidate_id: str) -> dict:
        item = self.pending_candidates.pop(candidate_id, None)
        if item is None:
            return {"stored": False, "reason": "unknown candidate"}
        stored = self.store.remember(item)
        return {"stored": stored is not None,
                "id": stored.id if stored else None}

    def correct(self, old_query: str, new_text: str,
                source: str = "conversation") -> dict:
        """Supersede: old item stays as provenance, marked superseded_by."""
        hits = self.store.recall(old_query, top_k=1)
        if not hits:
            return self.consider(new_text, source)
        old = hits[0][0]
        new_item = MemoryItem(category=old.category, text=new_text.strip(),
                              source=source, importance=0.9,
                              metadata={"privacy": "user-private",
                                        "supersedes": old.id})
        stored = self.store.remember(new_item)
        if stored is not None:
            old.metadata["superseded_by"] = stored.id
            self.store.remember(old)  # re-persist the supersede marker
        return {"stored": stored is not None,
                "id": stored.id if stored else None,
                "supersedes": old.id}
