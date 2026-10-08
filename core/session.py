"""Persistent conversation sessions + continuity (Stage 19) — context only.

A ConversationSession remembers WHAT was discussed (entities, pending
clarification/approval, recent turns) so follow-ups like "is it charging?"
resolve. It NEVER decides authority: every turn still flows through
intent parsing -> grounding -> resolver -> Policy/Governor/grants ->
execution. History is untrusted data: secret-shaped values are redacted
at rest, injected "authorization" in past messages changes nothing, and
approval continuity resolves through engine.approve (the existing Core
mechanism), never by remembering that someone once said "yes".

Kept separate from SessionStore (raw message log), Memory/RAG
(long-term retrieval), Missions (task state), and Audit (record).
"""
from __future__ import annotations
import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from .intent import (IntentProposal, IntentTurnService, ParserContext,
                     _clean_utterance, build_context, clarify,
                     ground_proposal, redact_params, redact_text)
from .intent import MAX_HOSTED_CTX as _MAX_HOSTED_CTX
from .models import utcnow

SESSION_IDLE_S = 1800  # idle expiry: 30 minutes
SESSION_MAX_LIFE_S = 86400  # absolute lifetime: 24 hours
APPROVAL_PENDING_S = 900  # pending approval horizon: 15 minutes
MAX_TURNS = 50  # hard cap of stored turns per session
CTX_TURNS = 6  # turns included in hosted context
MAX_ENTITIES = 20  # entities remembered
MAX_SUMMARY = 500  # session summary chars
MAX_DEVICE_ID = 128

STATES = ("idle", "active", "waiting_clarification", "waiting_approval",
          "executing", "completed", "expired")

TRANSITIONS = {
    "idle": {"active", "completed", "expired"},
    "active": {"active", "waiting_clarification", "waiting_approval",
               "executing", "completed", "expired"},
    "waiting_clarification": {"active", "waiting_clarification",
                              "waiting_approval", "executing", "completed",
                              "expired"},
    "waiting_approval": {"active", "waiting_approval", "executing",
                         "completed", "expired"},
    "executing": {"active", "completed", "expired"},
    "completed": {"active", "expired"},
    "expired": set(),
}

class SessionRejected(ValueError):
    """Invalid session transition or payload. Never stored."""


class TurnRecord(BaseModel):
    index: int = 0
    utterance: str = Field(default="", max_length=500)
    rewritten_from: str = ""
    capability: str = ""
    parameters: dict = Field(default_factory=dict)
    preferred_device: str = ""
    filled_from_session: bool = False
    resolution_status: str = ""
    resolution_device: Optional[str] = None
    executed: bool = False
    response: str = Field(default="", max_length=300)
    created_at: str = ""


class SessionEntity(BaseModel):
    kind: str = ""  # device | file | capability
    name: str = ""
    detail: str = ""
    turn: int = 0


class PendingClarification(BaseModel):
    question: str = ""
    missing: list[str] = Field(default_factory=list)
    capability: str = ""
    parameters: dict = Field(default_factory=dict)
    preferred_device: str = ""
    created_at: str = ""


class PendingApproval(BaseModel):
    execution_id: str = ""
    capability: str = ""
    device_id: str = ""
    parameters: dict = Field(default_factory=dict)
    reason: str = ""
    created_at: str = ""


class ConversationSession(BaseModel):
    id: str = ""
    owner: str = "user"
    device_id: str = ""
    state: str = "idle"
    turn_count: int = 0
    active_device: str = ""
    active_capability: str = ""
    entities: list[SessionEntity] = Field(default_factory=list)
    turns: list[TurnRecord] = Field(default_factory=list)
    pending_clarification: Optional[PendingClarification] = None
    pending_approval: Optional[PendingApproval] = None
    summary: str = ""
    created_at: str = ""
    updated_at: str = ""
    expires_at: str = ""

    def move(self, to: str) -> None:
        if to not in TRANSITIONS.get(self.state, set()):
            raise SessionRejected(
                f"illegal transition {self.state} -> {to}")
        self.state = to


class SessionStateStore:
    """SQLite-backed session persistence (memory mode when db is None).
    Time is injectable for deterministic expiry tests."""

    SCHEMA = """
CREATE TABLE IF NOT EXISTS conv_sessions (
  session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

    def __init__(self, db=None,
                 now: Optional[Callable[[], datetime]] = None) -> None:
        self._db = db
        self._now = now or utcnow
        if self._db is not None:
            self._db.execute(self.SCHEMA)

    def _now_iso(self) -> str:
        now = self._now()
        return now.isoformat() if isinstance(now, datetime) else str(now)

    def save(self, s: ConversationSession) -> None:
        s.updated_at = self._now_iso()
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO conv_sessions (session_id, state_json, "
            "updated_at) VALUES (?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "state_json=excluded.state_json, "
            "updated_at=excluded.updated_at",
            (s.id, s.model_dump_json(), s.updated_at))

    def load_from(self, db_row: str) -> ConversationSession:
        return ConversationSession.model_validate_json(db_row)

    def fetch(self, session_id: str) -> ConversationSession:
        if self._db is None:
            raise KeyError(session_id)
        rows = self._db.execute(
            "SELECT state_json FROM conv_sessions WHERE session_id=?",
            (session_id,))
        if not rows:
            raise KeyError(session_id)
        return self.load_from(rows[0][0])

    def delete(self, session_id: str) -> None:
        if self._db is None:
            return
        self._db.execute("DELETE FROM conv_sessions WHERE session_id=?",
                         (session_id,))

    def sweep(self, now_iso: Optional[str] = None) -> int:
        """Delete expired rows. Returns count removed."""
        if self._db is None:
            return 0
        now = now_iso or self._now_iso()
        # Database.execute returns fetched rows; count-then-delete.
        # A row is dead when past absolute expiry OR already terminally
        # expired (check_expiry flips state; save() refreshes updated_at,
        # so updated_at alone can never identify death).
        rows = self._db.execute(
            "SELECT session_id FROM conv_sessions "
            "WHERE json_extract(state_json, '$.expires_at') < ? "
            "OR json_extract(state_json, '$.state') = 'expired'",
            (now,))
        for (sid,) in rows:
            self._db.execute("DELETE FROM conv_sessions WHERE session_id=?",
                             (sid,))
        return len(rows)


_APPROVE_WORDS = re.compile(
    r"^(yes|approve|approved|ok|okay|do it|go ahead|confirmed|confirm"
    r"|proceed|sure|yeah|yep)\b[\s.!]*$", re.I)
_PRONOUN = re.compile(r"\b(it|that|there)\b", re.I)


class SessionTurnService:
    """Continuity orchestration over IntentTurnService. Supplies context
    (entity slot-filling, clarification merge, approval re-entry) and
    persists turns. Executes NOTHING itself: single intents delegate to
    IntentTurnService (same Core path); approvals resolve through
    engine.approve (existing mechanism); everything else is messages."""

    def __init__(self, fabric, resolver, parser=None, audit=None,
                 store: Optional[SessionStateStore] = None) -> None:
        from .intent import LocalIntentParser
        self.fabric = fabric
        self.resolver = resolver
        self.parser = parser or LocalIntentParser()
        self.audit = audit
        self.store = store or SessionStateStore()
        self.inner = IntentTurnService(fabric, resolver, self.parser,
                                       audit)
        # Live objects authoritative within this process; the store is
        # durability (restart/load) underneath.
        self._live: dict[str, ConversationSession] = {}

    def _audit(self, action: str, target: str = "",
               detail: str = "") -> None:
        try:
            if self.audit is not None:
                self.audit.record("session", action, target,
                                  detail[:500])
        except Exception:  # noqa: BLE001 — audit never breaks turns
            pass

    # -- lifecycle --

    def create(self, owner: str = "user",
               device_id: str = "") -> ConversationSession:
        if len(device_id) > MAX_DEVICE_ID:
            raise SessionRejected("device_id too long")
        now = self.store._now_iso()
        try:
            life = self.store._now() + timedelta(seconds=SESSION_MAX_LIFE_S)
            expiry = life.isoformat()
        except Exception:  # noqa: BLE001
            expiry = now
        s = ConversationSession(
            id=f"conv-{uuid.uuid4().hex[:12]}", owner=owner[:64],
            device_id=device_id[:MAX_DEVICE_ID], state="active",
            created_at=now, updated_at=now, expires_at=expiry)
        self._live[s.id] = s
        self.store.save(s)
        self._audit("session.created", s.id, f"owner={s.owner}")
        return s

    def get_session(self, session_id: str) -> ConversationSession:
        """Live object first, durable store second. Unknown -> KeyError."""
        if session_id in self._live:
            return self._live[session_id]
        loaded = self.store.fetch(session_id)
        self._live[session_id] = loaded
        return loaded

    def close(self, s: ConversationSession) -> ConversationSession:
        if s.state not in ("expired", "completed"):
            s.move("completed")
        s.pending_clarification = None
        s.pending_approval = None
        self.store.save(s)
        self._audit("session.closed", s.id, "")
        return s

    def check_expiry(self, s: ConversationSession) -> bool:
        """Apply idle/absolute expiry. Returns True if expired now."""
        if s.state == "expired":
            return True
        now = self.store._now_iso()
        try:
            expired = now >= s.expires_at
        except Exception:  # noqa: BLE001
            expired = False
        if not expired and s.updated_at:
            try:
                fmt = "%Y-%m-%dT%H:%M:%S"
                idle = (datetime.fromisoformat(now) -
                        datetime.fromisoformat(s.updated_at)
                        ).total_seconds()
                expired = idle > SESSION_IDLE_S
            except Exception:  # noqa: BLE001 — unparseable stays alive
                pass
        if expired:
            try:
                s.move("expired")
            except SessionRejected:
                s.state = "expired"
            s.pending_clarification = None
            s.pending_approval = None
            self.store.save(s)
            self._audit("session.expired", s.id, "")
        return expired

    # -- main turn --

    def turn(self, s: ConversationSession, utterance: str,
             who: str = "user") -> dict:
        """One continuity turn. Returns {message, proposal, resolution,
        executed, session}. Session is mutated + persisted."""
        if self.check_expiry(s):
            return {"message": "Our conversation expired — let's start "
                               "fresh. What would you like?",
                    "proposal": None, "resolution": None,
                    "executed": False, "session": s.model_dump()}
        clean = _clean_utterance(utterance)
        if s.state == "completed":
            s.move("active")
        # Approval answers route to the pending approval, not the parser.
        if s.state == "waiting_approval" and _APPROVE_WORDS.match(
                clean.strip()):
            return self._approve_turn(s, who)
        out = self.inner.handle_utterance(
            clean, who, s.device_id or s.active_device,
            session_context=self.hosted_context(s))
        proposal = out.get("proposal") or {}
        # Clarification continuation: a pending question (or pronoun-only
        # follow-up on the active capability) may be answerable from this
        # utterance — merge and continue through the normal path. A fresh
        # complete topic abandons the pending question instead.
        if out.get("resolution") is None and proposal.get(
                "needs_clarification"):
            if self._is_fresh_topic(s, proposal):
                s.pending_clarification = None
            else:
                merged, progressed = self._continue(s, proposal, clean)
                if merged is not None:
                    return self._run_proposal(s, merged, who, clean,
                                              filled_from_session=True)
                if progressed:
                    # Partial slot fills were kept on the pending
                    # question; record the turn but do NOT let the new
                    # generic clarification overwrite that progress.
                    self._record_turn(s, clean, proposal, out, True)
                    self.store.save(s)
                    out["session"] = s.model_dump()
                    return out
        self._record_turn(s, clean, proposal, out, False)
        self._sync_state(s, proposal, out)
        self.store.save(s)
        out["session"] = s.model_dump()
        return out

    def _run_proposal(self, s: ConversationSession, proposal: dict,
                      who: str, clean: str,
                      filled_from_session: bool) -> dict:
        from .intent import IntentProposal
        try:
            p = IntentProposal(**proposal)
        except Exception:  # noqa: BLE001 — merged proposal invalid
            out = {"message": "I couldn't use that — could you rephrase?",
                   "proposal": proposal, "resolution": None,
                   "executed": False}
            self._record_turn(s, clean, proposal, out, filled_from_session)
            self._sync_state(s, proposal, out)
            self.store.save(s)
            out["session"] = s.model_dump()
            return out
        if p.capability in ("device.list", "assistant.help"):
            # Builtins are answered locally by the inner service; the
            # filled values have no executable target, so delegate.
            out = self.inner.handle_utterance(
                clean, who, s.device_id or s.active_device,
                session_context=self.hosted_context(s))
            self._record_turn(s, clean, proposal, out, filled_from_session)
            self._sync_state(s, proposal, out)
            self.store.save(s)
            out["session"] = s.model_dump()
            return out
        res = self.resolver.resolve_proposal(
            {"preferred_capability": p.capability,
             "preferred_device": p.preferred_device,
             "constraints": {}}, who)
        resd = res.model_dump()
        if res.status == "resolved" and res.device_id:
            out = self.inner._maybe_execute(p, resd, who)
            out["proposal"] = p.model_dump()
            out["resolution"] = resd
        else:
            from .intent import _EXPLAIN
            msg = _EXPLAIN.get(res.status, "I cannot do that")
            if res.reason:
                msg += f": {res.reason}"
            out = {"message": msg + ".", "proposal": p.model_dump(),
                   "resolution": resd, "executed": False}
        self._record_turn(s, clean, p.model_dump(), out, filled_from_session)
        self._sync_state(s, p.model_dump(), out)
        self.store.save(s)
        out["session"] = s.model_dump()
        return out

    # -- continuity pieces --

    def _recent_entities(self, s: ConversationSession, kind: str,
                         limit: int = 5) -> list[SessionEntity]:
        return [e for e in reversed(s.entities)
                if e.kind == kind][:limit]

    @staticmethod
    def _is_fresh_topic(s: ConversationSession, proposal: dict) -> bool:
        """A new utterance naming a DIFFERENT complete capability abandons
        the pending question; answers and pronoun follow-ups continue it."""
        pend = s.pending_clarification
        if pend is None:
            return False
        cap = proposal.get("capability") or ""
        return bool(cap) and cap != pend.capability

    def _continue(self, s: ConversationSession, proposal: dict,
                  clean: str) -> tuple[Optional[dict], bool]:
        """Merge a clarification answer into the pending (or active)
        intent. Returns (grounded proposal or None, progressed): progressed
        means new slots were kept on the pending question even when the
        merge is still incomplete. Single-candidate fills only: ambiguity
        stays clarifying."""
        from .intent import (IntentProposal, extract_fill_values,
                             ground_proposal)
        pend = s.pending_clarification
        pcap = proposal.get("capability") or ""
        base_cap = pcap or (pend.capability if pend else "")
        if not base_cap and s.active_capability and _PRONOUN.search(clean):
            # Pronoun-only follow-up ("is it done?") continues the active
            # capability; device/file slots fill below if unambiguous.
            base_cap = s.active_capability
        if not base_cap:
            return None, False
        params = dict((pend.parameters if pend else {}) or {})
        for k, v in (proposal.get("parameters") or {}).items():
            if v not in (None, ""):
                params[k] = v
        preferred = (proposal.get("preferred_device")
                     or (pend.preferred_device if pend else "") or "")
        missing = set((pend.missing if pend else []) or [])
        if not missing and pend is None:
            missing = set(self._missing_from_reason(
                proposal.get("clarification_reason", "")))
        fills = extract_fill_values(clean)
        if "filename" in missing or "filename" not in params:
            if fills.get("filename"):
                params["filename"] = fills["filename"]
        if "path" in missing or ("path" not in params and
                                 base_cap in ("filesystem.list",
                                              "filesystem.read",
                                              "filesystem.exists",
                                              "filesystem.metadata")):
            if fills.get("quoted"):
                params["path"] = fills["quoted"]
        if "url" in missing and fills.get("url"):
            params["url"] = fills["url"]
        if not preferred:
            for d in self._known_devices():
                if d.lower() in clean.lower():
                    preferred = d
                    break
        if not preferred and (
                "device" in missing or "which one" in " ".join(missing)
                or _PRONOUN.search(clean)
                or "device" in (proposal.get("clarification_reason")
                                or "").lower()):
            devs = self._recent_entities(s, "device", 2)
            if len(devs) == 1:
                preferred = devs[0].name
        if not preferred and pend and pend.preferred_device:
            preferred = pend.preferred_device
        try:
            ctx = build_context(self.fabric)
            p = IntentProposal(
                request_id=proposal.get("request_id", ""),
                utterance=(proposal.get("utterance") or clean)[:500],
                capability=base_cap,
                parameters=params, preferred_device=preferred,
                confidence=min(float(proposal.get("confidence", 0.5)),
                               0.65),
                needs_clarification=False, source="session")
            grounded = ground_proposal(p, ctx, source="session")
        except Exception:  # noqa: BLE001 — merge failure stays clarifying
            return None, False
        if grounded.needs_clarification:
            # Partial progress still counts: keep the newly filled slots
            # on the pending question so the next answer continues.
            if pend is not None:
                new_slots = {k: v for k, v in params.items()
                             if k not in (pend.parameters or {})}
                if new_slots:
                    pend.parameters.update(new_slots)
                    pend.missing = [m for m in (pend.missing or [])
                                    if m not in new_slots and
                                    m not in params]
                    self.store.save(s)
                    return None, True
            return None, False
        return grounded.model_dump(), True

    def _known_devices(self) -> list[str]:
        try:
            return [v.device_id for v in self.fabric.list_devices()]
        except Exception:  # noqa: BLE001
            return []

    def _approve_turn(self, s: ConversationSession, who: str) -> dict:
        pend = s.pending_approval
        if pend is None:
            s.move("active")
            self.store.save(s)
            return {"message": "There's nothing waiting for approval.",
                    "proposal": None, "resolution": None,
                    "executed": False, "session": s.model_dump()}
        # Horizon + revocation re-checked at approve time (never cached).
        try:
            age = (self.store._now() - datetime.fromisoformat(
                pend.created_at)).total_seconds()
        except Exception:  # noqa: BLE001
            age = 0.0
        if age > APPROVAL_PENDING_S:
            s.pending_approval = None
            s.move("active")
            self.store.save(s)
            self._audit("session.approval.expired", s.id, pend.capability)
            return {"message": "That approval expired — tell me again if "
                               "you still want it.",
                    "proposal": None, "resolution": None,
                    "executed": False, "session": s.model_dump()}
        # Trust + presence are re-derived at approve time: revocation or
        # outage between request and approval refuses loudly.
        from .fabric import EXECUTABLE_TRUST, Presence
        try:
            trust = self.fabric.trust_of(pend.device_id)
            presence = self.fabric.presence_of(pend.device_id)
        except Exception:  # noqa: BLE001 — unknown device refuses
            trust, presence = "unknown", Presence.UNKNOWN
        if trust not in EXECUTABLE_TRUST or presence in (
                Presence.OFFLINE, Presence.UNKNOWN, Presence.STALE):
            s.pending_approval = None
            s.move("active")
            self.store.save(s)
            self._audit("session.approval.refused", s.id,
                        f"{pend.device_id} trust={trust} "
                        f"presence={presence}")
            return {"message": "I can't approve that anymore — the device "
                               "is no longer eligible. Tell me again if "
                               "you still want it.",
                    "proposal": None, "resolution": None,
                    "executed": False, "session": s.model_dump()}
        engine = self.fabric.engine
        try:
            rec = engine._records.get(pend.execution_id)
        except Exception:  # noqa: BLE001
            rec = None
        if rec is None or getattr(rec, "device_id", "") != pend.device_id:
            # Unknown, replayed, or cross-device approval: refuse loudly.
            s.pending_approval = None
            s.move("active")
            self.store.save(s)
            self._audit("session.approval.rejected", s.id,
                        "unknown or mismatched approval reference")
            return {"message": "I couldn't match that approval — tell me "
                               "again what you'd like.",
                    "proposal": None, "resolution": None,
                    "executed": False, "session": s.model_dump()}
        try:
            final = engine.approve(pend.execution_id, who)
        except Exception as e:  # noqa: BLE001 — Core decides, we report
            self._audit("session.approval.failed", s.id, str(e)[:200])
            return {"message": f"Approval didn't go through: "
                               f"{str(e)[:200]}.",
                    "proposal": None, "resolution": None,
                    "executed": False, "session": s.model_dump()}
        state = getattr(final.state, "value", final.state)
        self._audit("session.approval.resolved", s.id,
                    f"{pend.capability} -> {state}")
        s.pending_approval = None
        s.move("active")
        self._remember_result(s, pend.capability, pend.device_id,
                              dict(getattr(final, "result", None) or {}))
        self.store.save(s)
        ok = state in ("succeeded", "completed", "ok")
        return {"message": f"Approved and done: {pend.capability}."
                if ok else f"Approved, but it ended as {state}.",
                "proposal": None, "resolution": None, "executed": ok,
                "session": s.model_dump()}

    # -- bookkeeping --

    def _record_turn(self, s: ConversationSession, clean: str,
                     proposal: dict, out: dict,
                     filled: bool) -> None:
        res = out.get("resolution") or {}
        cap = (proposal or {}).get("capability", "")
        rec = TurnRecord(
            index=s.turn_count,
            utterance=redact_text(clean[:500]),
            capability=cap,
            parameters=redact_params((proposal or {}).get("parameters")),
            preferred_device=((proposal or {}).get("preferred_device")
                              or "")[:MAX_DEVICE_ID],
            filled_from_session=filled,
            resolution_status=res.get("status", ""),
            resolution_device=res.get("device_id"),
            executed=bool(out.get("executed", False)),
            response=str(out.get("message", ""))[:300],
            created_at=self.store._now_iso())
        s.turns.append(rec)
        if len(s.turns) > MAX_TURNS:
            s.turns = s.turns[-MAX_TURNS:]
        s.turn_count += 1
        # Entities: executed/resolved device + file params are remembered.
        dev = res.get("device_id") or rec.preferred_device
        if dev:
            self._remember_entity(s, "device", dev, "")
        fname = rec.parameters.get("filename") or \
            rec.parameters.get("path") or rec.parameters.get("url")
        if fname and cap:
            self._remember_entity(s, "file", str(fname)[:120], cap)
        if cap:
            self._remember_entity(s, "capability", cap, "")
            s.active_capability = cap
        if dev:
            s.active_device = dev
        self._refresh_summary(s)

    def _remember_entity(self, s: ConversationSession, kind: str,
                         name: str, detail: str) -> None:
        s.entities = [e for e in s.entities
                      if not (e.kind == kind and e.name == name)]
        s.entities.append(SessionEntity(kind=kind, name=name,
                                        detail=detail[:120],
                                        turn=s.turn_count))
        if len(s.entities) > MAX_ENTITIES:
            s.entities = s.entities[-MAX_ENTITIES:]

    def _remember_result(self, s: ConversationSession, cap: str,
                         device: str, result: dict) -> None:
        if device:
            self._remember_entity(s, "device", device, "")
            s.active_device = device
        if cap:
            self._remember_entity(s, "capability", cap, "")
            s.active_capability = cap
        self._refresh_summary(s)

    def _refresh_summary(self, s: ConversationSession) -> None:
        bits = []
        if s.active_device:
            bits.append(f"device={s.active_device}")
        if s.active_capability:
            bits.append(f"capability={s.active_capability}")
        last = [t for t in s.turns if t.capability][-1:] if s.turns \
            else []
        for t in last:
            bits.append(f"last={t.capability}@{t.resolution_device or '?'}"
                        f"/{t.resolution_status or '?'}")
        if s.pending_clarification:
            bits.append("awaiting clarification")
        if s.pending_approval:
            bits.append("awaiting approval")
        s.summary = ("turns=%d " % s.turn_count + "; ".join(bits))[:MAX_SUMMARY]

    def _sync_state(self, s: ConversationSession, proposal: dict,
                    out: dict) -> None:
        res = out.get("resolution") or {}
        # Clarification tracking.
        if (proposal or {}).get("needs_clarification"):
            missing = self._missing_from_reason(
                (proposal or {}).get("clarification_reason", ""))
            s.pending_clarification = PendingClarification(
                question=(proposal or {}).get("clarification_reason",
                                              "")[:300],
                missing=missing,
                capability=(proposal or {}).get("capability", ""),
                parameters=redact_params(
                    (proposal or {}).get("parameters")),
                preferred_device=((proposal or {}).get("preferred_device")
                                  or "")[:MAX_DEVICE_ID],
                created_at=self.store._now_iso())
            if s.state == "active":
                s.move("waiting_clarification")
            self._audit("session.clarification", s.id,
                        ",".join(missing)[:200])
        elif s.pending_clarification is not None and \
                not (proposal or {}).get("needs_clarification"):
            s.pending_clarification = None
            if s.state == "waiting_clarification":
                s.move("active")
        # Approval tracking (execution_id surfaces via approval_reference).
        ref = out.get("approval_reference", "")
        if res.get("status") == "requires_approval" and ref:
            s.pending_approval = PendingApproval(
                execution_id=str(ref)[:64],
                capability=(proposal or {}).get("capability", ""),
                device_id=str(res.get("device_id") or "")[:MAX_DEVICE_ID],
                parameters=redact_params(
                    (proposal or {}).get("parameters")),
                reason=str(res.get("reason", ""))[:300],
                created_at=self.store._now_iso())
            if s.state in ("active", "waiting_clarification"):
                s.move("waiting_approval")
            self._audit("session.approval.pending", s.id, ref[:64])
        if s.state == "idle":
            s.move("active")

    @staticmethod
    def _missing_from_reason(reason: str) -> list[str]:
        low = (reason or "").lower()
        missing = []
        for key, words in (("filename", ("file", "filename")),
                           ("device", ("device", "which one", "phone",
                                       "laptop")),
                           ("path", ("director", "path")),
                           ("url", ("url", "address"))):
            if any(w in low for w in words):
                missing.append(key)
        return missing[:4]

    # -- hosted context --

    def hosted_context(self, s: ConversationSession) -> str:
        """Bounded, secret-scrubbed context for the hosted prompt.
        Labeled untrusted at use site; turns are already redacted."""
        parts = [f"summary: {s.summary[:MAX_SUMMARY]}"]
        for t in s.turns[-CTX_TURNS:]:
            parts.append(f"turn{t.index} user: {t.utterance[:120]}")
            if t.capability:
                parts.append(f"turn{t.index} intent: {t.capability} "
                             f"-> {t.resolution_status or '?'}")
        for e in s.entities[-10:]:
            parts.append(f"entity {e.kind}: {e.name[:60]}")
        if s.pending_clarification:
            parts.append("pending clarification: " +
                         s.pending_clarification.question[:150])
        ctx = "\n".join(parts)
        return ctx[:_MAX_HOSTED_CTX]
