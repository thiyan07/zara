"""Stage 31-P3 phase B (P3d) — call with contact resolution.

Implements the user's standing verbatim spec (2026-10-20 decisions +
the "call to x" phrasing) as a DETERMINISTIC turn flow inside
IntentTurnService, exactly like the reminder control path:

- "call to x" runs a bounded contacts lookup on the phone; ZERO or
  MULTIPLE matches are answers, never guesses.
- No match -> Zara ASKS "saved under another name?"; the user's
  confirmatory answer ("saved as ...") drives the SECOND bounded
  lookup; on a hit the alias fact is written to memory — CONFIRMED by
  that ask, never inferred — and the confirm step follows.
- Exactly one match -> "Calling <name> (<tail>) — correct?" (the
  CORRECTNESS CONFIRM the user decided 2026-10-20). "yes" submits
  android.call.place, which STILL requires its own per-call approval
  in the app; Zara never dials without both gates.
- Raw phone numbers from the utterance are refused (contacts and
  confirmed matches only).

Every execution goes through engine.submit — policy, trust, presence,
approval, and audit all apply; this module introduces NO new
authorization path. Multi-turn state is a bounded in-process ring
(single pending call conversation, 10 min TTL) — the same honesty
trade as the clipboard-anchor ring: nothing persists or is guessed
after a restart, and a pending flow NEVER ties to a stored approval.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import timedelta

from .models import ExecutionState, MemoryItem, utcnow

_FLOW_TTL_S = 600.0  # 10 min: the pairing-code precedent for pending asks
_CONFIRM_WORDS = ("yes", "yep", "yeah", "yess", "ok", "okay",
                  "go ahead", "call", "dial", "please call",
                  "yes call", "call yes", "place it")
_CANCEL_WORDS = ("no", "nope", "cancel", "forget it", "stop", "never mind",
                 "nvm", "don't call", "do not call", "dont call", "abort")
_DEVICE_TAIL_RE = re.compile(
    r"\s+on\s+(?:my\s+)?(?:phone|mobile|android|cell|cellphone)\s*$",
    re.I)
_RECORD_RE = re.compile(
    r"^\s*(?:record|tape)\s+(?:the\s+|this\s+)?(?:phone\s+)?call\b|"
    r"^\s*(?:call\s+recording?|record\s+my\s+calls)\b", re.I)
_INVITE_RE = re.compile(r"^\s*(?:call|dial)\s+(.+?)\s*$", re.I)
# "saved as Kate" / "yes — saved as Kate" / "it's Kate" / "under Kate"
_ALIAS_RE = re.compile(
    r"^\s*(?:.*?\b)?saved\s+as\s+(.{1,60}?)[,.;:! ]*$", re.I)
_ALIAS_FORM_RE = re.compile(
    r"^\s*(?:it'?s\s+|it\s+is\s+|under\s+|name\s+is\s+)"
    r"(.{1,60}?)[,.;:! ]*$", re.I)
_DIGIT_RE = re.compile(r"^\s*([1-5])\s*$")
_NUMBER_RE = re.compile(r"^[\s+()\-0-9]+$")
_YES_RE = re.compile(
    r"^\s*(?:yes|yep|yeah|sure|ok|okay|go ahead|call|dial|place\b[^\s]*)"
    r"\b[,.! ]*$", re.I)
_NO_RE = re.compile(
    r"^\s*(?:no|nope|no thanks|nvm|no thank you|cancel)\b[,.! ]*$", re.I)
_SMS_SEND_WORDS = ("send", "send it", "send now", "send it now",
                   "yes send", "send that", "go")


def _tail(number: str) -> str:
    """Results and asks carry the number's TAIL only; full digits live
    exclusively in the approval prompt's bound inputs."""
    n = (number or "").strip()
    return ("*" * max(0, len(n) - 4)) + n[-4:] if len(n) > 4 else n


def _clean_name(raw: str) -> str:
    """Bounded capture cleanup: strip device tails, padding quotes and
    trailing politeness."""
    s = (raw or "").strip().strip('"\'')
    s = _DEVICE_TAIL_RE.sub("", s).strip()
    s = re.sub(r"\s+(?:please|for me)$", "", s, flags=re.I).strip()
    return s


class _PendingFlow:
    """One in-flight call conversation (ring-of-one state)."""

    __slots__ = ("phase", "asked", "matches", "number", "contact_name",
                 "device_id", "created_at", "alias_source", "exec_id",
                 "draft")

    def __init__(self, device_id: str) -> None:
        self.phase = "lookup_done"
        self.exec_id = ""
        self.draft = ""
        self.asked = ""          # the name the user first asked for
        self.matches: list[dict] = []
        self.number = ""
        self.contact_name = ""
        self.device_id = device_id
        self.created_at = utcnow()
        self.alias_source = ""   # the alias the user CONFIRMED (if any)

    def expired(self) -> bool:
        try:
            return (utcnow() - self.created_at) > timedelta(
                seconds=_FLOW_TTL_S)
        except Exception:  # noqa: BLE001 — a broken clock closes the flow
            return True


class CallFlow:
    """Deterministic P3d turn flow (see module docstring)."""

    def __init__(self, fabric, resolver, engine, memory_store, audit,
                 devices) -> None:
        self.fabric = fabric
        self.resolver = resolver
        self.engine = engine
        self.memory_store = memory_store
        self.audit = audit
        self.devices = devices          # DeviceManager (kind data only)
        # Ring-of-one (bounded): a newer call conversation REPLACES the
        # older pending one — never a queue that grows unattended.
        self._pending: _PendingFlow | None = None
        self._lock = threading.Lock()

    def _audit(self, action: str, who: str, detail: str = "") -> None:
        """Bounded audit rows for the flow's own decisions (the engine
        audits every execution separately; this row covers the flow's
        own side). Never raises."""
        try:
            self.audit.record("call_flow", action, who, detail[:300])
        except Exception:  # noqa: BLE001 — audit is never load-bearing
            pass

    # ---------------- invite detection ----------------

    def wants_turn(self, clean: str) -> bool:
        s = (clean or "").strip()
        low = s.lower()
        if _RECORD_RE.match(s):
            # Stage 31-P3 phase B (P3g): recording demands get an
            # HONEST refusal from the deterministic flow, never a
            # fall-through the LLM could inflate into a promise.
            return True
        if self._pending is not None and not self._pending.expired():
            # A pending flow owns generic yes/no/alias-answer/number
            # turns UNTIL its TTL — same ownership rule as the
            # continue-the-task resume phrases.
            return True
        if _INVITE_RE.match(s) and not low.startswith(("call log",
                                                       "call history",
                                                       "call details")):
            return True
        return False

    # ---------------- turn entry ----------------

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        """One turn of the call flow. Returns a turn-shaped dict or
        None when the utterance is NOT a call-flow turn (fall-through
        to the normal parser)."""
        s = (clean or "").strip()
        pend = self._pending
        if pend is not None and not pend.expired():
            try:
                out = self._continuation(s, pend, who, requester_device)
                if out is not None:
                    return out
            finally:
                if self._pending is not None and self._pending.expired():
                    self._pending = None
            # not a continuation phrase: fall through — unless the
            # utterance is a NEW invitation, which replaces the flow.
        # P3g: recording demands get the honest refusal BEFORE any
        # invite parsing — "call recording" is otherwise a plausible
        # contact name for the lookup spine (caught live in fixtures).
        mrec = _RECORD_RE.match(s)
        if mrec is not None:
            self._audit("call_flow.record_refused", who, s[:60])
            return {
                "message":
                    "I deliberately can't record calls: on this "
                    "OS recording needs a privileged role this "
                    "build refuses to seek (it's also a two-party "
                    "consent matter in many regions — I won't "
                    "promise a legal grey path). Notes during or "
                    "after the call are welcome.",
                "proposal": None, "resolution": None,
                "executed": False, "call_flow": "record_refused"}
        m = _INVITE_RE.match(s)
        if m is None:
            self._audit("call_flow.fallthrough", who, s[:120])
            return None
        name = _clean_name(m.group(1))
        if not name or _NUMBER_RE.match(name):
            self._audit("call_flow.raw_number_refused", who, name[:60])
            return {
                "message": "I don't dial raw numbers from chat text — "
                           "numbers must come from a contact match you "
                           "confirmed. Say who to call and I'll look "
                           "them up.",
                "proposal": None, "resolution": None, "executed": False,
                "refused": True}
        # Memory first: a CONFIRMED alias resolves BEFORE any device
        # lookup (the user's standing spec: future calls resolve from
        # the alias note).
        alias_hit = self._alias_lookup(name)
        if alias_hit is not None:
            self._audit("call_flow.alias_resolved", who,
                        f"{name[:60]} -> {alias_hit[:60]}")
            name = alias_hit
        return self._do_lookup(name, who, requester_device,
                               alias_source=name if alias_hit else "")

    # ---------------- lookups ----------------

    def _pick_phone(self, requester_device: str) -> dict:
        """The flow's executor: an ONLINE Android. Exactly one = use
        it; 0 = honest none; >1 = ask (no guessing, no ordering).
        Policy/trust/president: the ENGINE gates every submission this
        flow makes — the picker only chooses a target, never an
        authorization."""
        cands = []
        for v in self.fabric.list_devices():
            dtype = str(getattr(v, "device_type",
                                getattr(v, "kind", ""))).lower()
            if "android" not in dtype:
                continue
            if str(getattr(v, "presence", "")) != "online":
                continue
            cands.append(v.device_id)
        if requester_device and requester_device in cands:
            return {"device_id": requester_device}
        if len(cands) == 1:
            return {"device_id": cands[0]}
        if len(cands) == 0:
            return {"error":
                    "No phone is online right now — I can't reach the "
                    "contacts or place a call. (It picks this up when "
                    "it next connects.)"}
        return {"error":
                f"{len(cands)} phones are online — say which one "
                "(e.g. 'call Kate on emu-pixel8')."[:300]}

    def _lookup(self, name: str, device_id: str, who: str, limit: int = 5):
        rec = self.engine.submit(
            "android.contacts.lookup", {"name": name, "limit": limit},
            device_id, who=who)
        self._audit("call_flow.lookup_submitted", who, rec.id)
        if rec.state == ExecutionState.WAITING_FOR_PERMISSION:
            return {"parked": rec, "matches": None}
        if rec.state == ExecutionState.SUCCEEDED:
            out = rec.result or {}
            return {"matches": out.get("matches") or [],
                    "limit": out.get("count")}
        if rec.state == ExecutionState.DENIED:
            return {"matches": None, "denied": True}
        return {"matches": None, "error":
                (rec.error or "contacts lookup failed")[:300]}

    def _do_lookup(self, name: str, who: str, requester_device: str,
                   alias_source: str = "") -> dict:
        phone = self._pick_phone(requester_device)
        if "error" in phone:
            self._audit("call_flow.no_phone", who, phone["error"][:100])
            with self._lock:
                self._pending = None
            return {"message": phone["error"], "proposal": None,
                    "resolution": None, "executed": False}
        res = self._lookup(name, phone["device_id"], who)
        if res.get("parked") is not None:
            rec = res["parked"]
            with self._lock:
                pend = _PendingFlow(phone["device_id"])
                pend.asked = name
                pend.alias_source = alias_source or name
                pend.phase = "lookup_pending"
                pend.exec_id = rec.id
                self._pending = pend
            self._audit("call_flow.lookup_parked", who, rec.id)
            return {"message":
                    "I need your approval to read the contacts on "
                    f"{phone['device_id']} for '{name}'. Approve it "
                    "there, then tell me to continue (any message "
                    "works).",
                    "proposal": None, "resolution": None,
                    "executed": True,
                    "execution_id": rec.id,
                    "approval_required": True,
                    "call_flow": "lookup_pending"}
        if res.get("denied"):
            self._audit("call_flow.lookup_denied", who, name[:60])
            return {"message": "The contacts lookup on "
                               f"{phone['device_id']} was denied — no "
                               "contacts were read.",
                    "proposal": None, "resolution": None,
                    "executed": False}
        matches = res.get("matches")
        if matches is None:
            self._audit("call_flow.lookup_failed", who, name[:60])
            return {"message": "The contacts lookup failed: "
                               f"{res.get('error', 'unknown')}",
                    "proposal": None, "resolution": None,
                    "executed": False}
        matches = [x for x in matches if isinstance(x, dict) and
                   x.get("number") and x.get("display_name")][:5]
        with self._lock:
            pend = _PendingFlow(phone["device_id"])
            pend.asked = name
            pend.alias_source = alias_source or name
            pend.matches = matches
            pend.draft = getattr(self, "_draft", "")
            self._pending = pend
        self._audit("call_flow.lookup_result", who,
                    f"{name[:60]}: {len(matches)} match(es)")
        return self._offer(pend, who)

    def _offer(self, pend: _PendingFlow, who: str) -> dict:
        if not pend.matches:
            # The ask is now the pending phase: any next turn owns it.
            pend.phase = "alias_ask"
            return {"message":
                    f"I don't see '{pend.asked}' in your phone's "
                    "contacts under that name. Is it saved under a "
                    "different name? (Say 'saved as ...' — or 'no' to "
                    "stop.)",
                    "proposal": None, "resolution": None,
                    "executed": True,
                    "call_flow": "alias_ask"}
        if len(pend.matches) == 1:
            return self._confirm_offer(pend)
        pend.phase = "disambiguate"
        opts = []
        for i, x in enumerate(pend.matches[:5], start=1):
            opts.append(f"  {i}. {x['display_name']} "
                        f"({_tail(str(x['number']))})")
        return {"message":
                "I found " + str(len(pend.matches)) + " contacts for '"
                + pend.asked + "':\n" + "\n".join(opts)
                + "\nWhich one? Say its number (e.g. '2').",
                "proposal": None, "resolution": None, "executed": True,
                "call_flow": "disambiguate"}

    def _adopt_match(self, pend: _PendingFlow) -> None:
        """Shared: pin the single match into the pending state and
        write the CONFIRMED alias fact when this lookup resolved an
        alias (the user's alias answer just paid off, once)."""
        x = pend.matches[0]
        pend.number = str(x["number"])
        pend.contact_name = str(x["display_name"])
        if pend.alias_source and pend.alias_source.lower() != \
                pend.contact_name.lower():
            self._write_alias(pend.alias_source, pend.contact_name,
                              pend.device_id, "user")

    def _confirm_offer(self, pend: _PendingFlow) -> dict:
        """The user's correctness confirm (2026-10-20 decision): ONE
        match is shown with a TAIL-masked number; the flow never dials
        on the confirm alone — 'yes' submits the call, whose OWN
        per-call approval still parks in the app."""
        self._adopt_match(pend)
        pend.phase = "confirm"
        return {"message": self._confirm_line(pend),
                "proposal": None, "resolution": None, "executed": True,
                "call_flow": "confirm"}

    def _confirm_line(self, pend: _PendingFlow) -> str:
        return (f"Calling {pend.contact_name} ({_tail(pend.number)}) — "
                "correct? Say 'yes' to place the call, or 'no' to "
                "cancel. The call itself still needs one approval in "
                "the app — I never dial without it.")

    def _act(self, pend: _PendingFlow, who: str) -> dict:
        return self._place_call(pend, who)

    # ---------------- continuations ----------------

    def _continuation(self, s: str, pend: _PendingFlow, who: str,
                      requester_device: str) -> dict | None:
        low = s.lower().strip()
        # A fresh invitation replaces the pending flow (confirm-phase
        # invites "call X" are dangerous double senses — the confirm
        # rules own plain yes/no; a NEW name INVITE still replaces).
        # The ONLY phrase that stays inside a lookup_pending flow is a
        # continuation signal ("continue"/"ok"/anything): the lookup
        # result has to land first.
        m_new = _INVITE_RE.match(s)
        if m_new is not None:
            new_name = _clean_name(m_new.group(1))
            if new_name and not _NUMBER_RE.match(new_name) and \
                    not _YES_RE.match(low) and not _NO_RE.match(low) and \
                    not any(x in low for x in ("saved as", "it's", "under",
                                               "name is")):
                if pend.phase == "lookup_pending":
                    return None
                return None  # caller handles it as a new invite
        # Phase 0: drain the record the flow parked. All lookups and
        # calls arrived here through approvals whose completion is
        # asynchronous; the pending flow carries its executor id and
        # consumes the TERMINAL record at the first turn that follows.
        if pend.phase in ("lookup_pending", "call_pending",
                          "send_pending"):
            try:
                rec = self.engine.get(pend.exec_id)
            except Exception:  # noqa: BLE001 — record lost: close honestly
                self._pending = None
                return {"message": "The contacts lookup record is no "
                                   "longer available (Core restarted?) — "
                                   "say 'call <name>' again.",
                        "proposal": None, "resolution": None,
                        "executed": False, "call_flow": "closed"}
            if rec.state == ExecutionState.WAITING_FOR_PERMISSION:
                return {"message":
                        "The lookup is still parked for approval.",
                        "proposal": None, "resolution": None,
                        "executed": False, "call_flow": pend.phase}
            if pend.phase == "lookup_pending":
                if rec.state in (ExecutionState.RUNNING,
                                 ExecutionState.PENDING) or \
                        rec.state.name in ("RUNNING",):
                    # Device work in flight (approval → job claim → run
                    # is throttled by the poll gap): the flow STAYS
                    # pending (TTL-bounded) instead of declaring
                    # failure — the only "didn't run" close paths are
                    # terminal refusals below.
                    return {"message":
                            "Still working — the contacts lookup is "
                            "running on the phone now (the poll gap "
                            "can take up to a minute). Say anything "
                            "in a moment and I'll report it.",
                            "proposal": None, "resolution": None,
                            "executed": False, "call_flow": "wait"}
                if rec.state != ExecutionState.SUCCEEDED:
                    self._pending = None
                    self._audit("call_flow.lookup_not_run", who,
                                str(rec.state))
                    return {"message":
                            "The contacts lookup didn't run "
                            f"({str(rec.state)}) — no contacts were "
                            "read.",
                            "proposal": None, "resolution": None,
                            "executed": False, "call_flow": "closed"}
                out = rec.result or {}
                pend.matches = [x for x in (out.get("matches") or [])
                                if isinstance(x, dict) and
                                x.get("number") and
                                x.get("display_name")][:5]
                pend.phase = "lookup_done"
                self._audit("call_flow.lookup_drained", who,
                            f"{pend.asked[:40]}: {len(pend.matches)}")
                # The result lands NOW; this turn BOTH reports the new
                # phase and, if the current phrase is already a directive
                # for it (the user approved and answered in one breath),
                # follows the directive — never double-guesses. Directives
                # that need the phase offer confirm via the offer.
                offer = self._offer(pend, who)
                directive = self._phase_phrase(
                    s, pend, who, requester_device, from_drain=True)
                return directive or offer
            # call_pending/send_pending: report the act honestly via
            # the flow hook (each surface owns its own verdict text).
            return self._report_parked(rec, pend, who)
        # Non-drain phases: the phrase machine owns this turn.
        return self._phase_phrase(s, pend, who, requester_device)

    def _report_parked(self, rec, pend, who) -> dict:
        """CallFlow hook: the approved call's terminal state, reported
        honestly — a confirmed call screen is verified; anything else
        is data, never a manufactured 'placed'."""
        out = rec.result or {}
        verdict = str(out.get("verdict", ""))
        self._pending = None
        if rec.state == ExecutionState.SUCCEEDED and \
                verdict == "confirmed" and \
                out.get("verified") is True:
            return {"message":
                    f"Calling {pend.contact_name} — the call screen "
                    "came up (verified by the a11y re-read).",
                    "proposal": None, "resolution": None,
                    "executed": True, "verified": True,
                    "call_flow": "placed"}
        if rec.state == ExecutionState.SUCCEEDED:
            return {"message":
                    f"The dialer fired for {pend.contact_name}, but no "
                    "call screen appeared in the window — reported as "
                    "NOT placed (never guessed).",
                    "proposal": None, "resolution": None,
                    "executed": True, "verified": False,
                    "call_flow": "closed"}
        self._audit("call_flow.call_not_run", who, str(rec.state))
        return {"message":
                f"The call to {pend.contact_name} didn't run "
                f"({str(rec.state)}) — nothing was dialed.",
                "proposal": None, "resolution": None,
                "executed": False, "call_flow": "closed"}

    def _phase_phrase(self, s: str, pend: _PendingFlow, who: str,
                      requester_device: str,
                      from_drain: bool = False) -> dict | None:
        """Phrase handling for the CURRENT pending phase (alias names,
        disambiguation picks, yes/no confirmations)."""
        low = s.lower().strip()
        if pend.phase == "alias_ask":
            m = _ALIAS_RE.match(s) or _ALIAS_FORM_RE.match(s)
            if m is not None and m.group(1):
                alias = _clean_name(m.group(1))
                if alias:
                    self._audit("call_flow.alias_second_lookup", who,
                                f"{pend.asked[:40]} -> {alias[:60]}")
                    return self._do_lookup(
                        alias, who, requester_device,
                        alias_source=pend.alias_source)
            if _YES_RE.match(low):
                return {"message": "Saved as what? Say the name it's "
                                   "under (e.g. 'saved as Kate').",
                        "proposal": None, "resolution": None,
                        "executed": True, "call_flow": "alias_ask"}
            if _NO_RE.match(low):
                self._pending = None
                self._audit("call_flow.alias_declined", who,
                            pend.asked[:60])
                return {"message": "Understood — nothing saved. Tell "
                                   "me again later as 'call <name>'.",
                        "proposal": None, "resolution": None,
                        "executed": False, "call_flow": "closed"}
            return None
        if pend.phase == "disambiguate":
            d = _DIGIT_RE.match(s)
            if d is not None and d.group(1) is not None:
                idx = int(d.group(1)) - 1
                if idx < len(pend.matches):
                    pend.matches = [pend.matches[idx]]
                    return self._confirm_offer(pend)
                return {"message": f"I only have "
                                   f"{len(pend.matches)} matches — say "
                                   "a number between 1 and "
                                   f"{len(pend.matches)}.",
                        "proposal": None, "resolution": None,
                        "executed": False, "call_flow": "disambiguate"}
            # A bare name among the shown matches.
            if s.strip() and _NUMBER_RE.match(
                    _clean_name(s)) is None:
                hit = [x for x in pend.matches if
                       str(x["display_name"]).lower() ==
                       s.strip().lower()]
                if not hit:
                    hit = [x for x in pend.matches if
                           s.strip().lower() in
                           str(x["display_name"]).lower()]
                if len(hit) == 1:
                    pend.matches = hit
                    return self._confirm_offer(pend)
            return None
        if pend.phase == "confirm":
            if _YES_RE.match(low):
                self._audit("call_flow.confirmed_action", who,
                            f"{pend.contact_name[:40]}")
                return self._act(pend, who)
            if _NO_RE.match(low):
                self._pending = None
                self._audit("call_flow.call_cancelled", who,
                            pend.contact_name[:40])
                return {"message": "Cancelled — no call was placed "
                                   "and nothing was dialed.",
                        "proposal": None, "resolution": None,
                        "executed": False, "call_flow": "closed"}
            return None
        return None

    # ---------------- the call ----------------

    def _place_call(self, pend: _PendingFlow, who: str) -> dict:
        """Submit the confirmed call. Per-call approval is REQUIRED by
        policy (HIGH-risk surface): the parked record's approval lives
        in the app and can still be DENIED there."""
        rec = self.engine.submit(
            "android.call.place",
            {"number": pend.number, "wait_ms": 4000, "confirm": True},
            pend.device_id, who=who)
        self._audit("call_flow.place_submitted", who, rec.id)
        if rec.state == ExecutionState.WAITING_FOR_PERMISSION:
            with self._lock:
                pend.phase = "call_pending"
                pend.exec_id = rec.id
            return {"message":
                    f"Submitted the call to {pend.contact_name} "
                    f"({_tail(pend.number)}) — approve it on "
                    f"{pend.device_id}; I'll report what actually "
                    "happened when you next message me.",
                    "proposal": None, "resolution": None,
                    "executed": True, "execution_id": rec.id,
                    "approval_required": True, "call_flow": "call_pending"}
        self._pending = None
        out = rec.result or {}
        verdict = str(out.get("verdict", ""))
        if rec.state == ExecutionState.SUCCEEDED and verdict == "confirmed" \
                and out.get("verified") is True:
            return {"message":
                    f"Calling {pend.contact_name} — the call screen "
                    "came up, verified by the accessibility re-read.",
                    "proposal": None, "resolution": None,
                    "executed": True, "execution_id": rec.id,
                    "verified": True, "call_flow": "placed"}
        if rec.state == ExecutionState.SUCCEEDED:
            return {"message":
                    f"The dialer fired for {pend.contact_name}, but "
                    "the call screen never appeared within the 4 s "
                    "window — reported as NOT placed (never guessed).",
                    "proposal": None, "resolution": None,
                    "executed": True, "execution_id": rec.id,
                    "verified": False, "call_flow": "placed"}
        if rec.state == ExecutionState.DENIED:
            return {"message": "The call was denied before dispatch — "
                               "nothing was dialed.",
                    "proposal": None, "resolution": None,
                    "executed": False, "call_flow": "denied"}
        return {"message":
                f"The call didn't go through: "
                f"{rec.error or 'device refused'}",
                "proposal": None, "resolution": None,
                "executed": False, "call_flow": "failed"}

    # ---------------- alias memory ----------------

    def _alias_lookup(self, name: str) -> str | None:
        """CONFIRMED alias facts only: metadata type 'contact_alias',
        never a fuzzy guess from ordinary text."""
        store = self.memory_store
        if store is None:
            return None
        q = (name or "").strip().lower()
        if not q:
            return None
        try:
            hits = store.recall(f"alias '{q}' contacts",
                                top_k=20, category="preferences")
        except Exception:  # noqa: BLE001 — memory is advisory here
            return None
        for item, _score in hits or []:
            md = getattr(item, "metadata", {}) or {}
            if md.get("type") != "contact_alias":
                continue
            if str(md.get("alias", "")).strip().lower() == q and \
                    not md.get("superseded_by"):
                actual = str(md.get("contact_name", "")).strip()
                return actual or None
        return None

    def _write_alias(self, alias: str, actual: str, device_id: str,
                     who: str) -> dict:
        """The CONFIRMED memory fact: '<alias>' resolves to '<actual>'
        on the phone — written AFTER the user supplied the alias in the
        asking turn (the confirmation the standing spec decided)."""
        store = self.memory_store
        if store is None:
            return {"stored": False, "reason": "memory unavailable"}
        text = (f"Phone contact alias: '{alias.strip()}' is saved as "
                f"'{actual.strip()}' in the user's phone contacts — "
                "confirmed by the user during the call-flow alias ask.")
        item = MemoryItem(
            category="preferences", text=text[:400], source="call_flow",
            confidence=0.9, importance=0.6,
            metadata={"privacy": "user-private", "type": "contact_alias",
                      "alias": alias.strip()[:60],
                      "contact_name": actual.strip()[:60],
                      "device_id": device_id[:64]})
        try:
            stored = store.remember(item)
        except ValueError as e:  # secrets guarding — never expected here
            self._audit("call_flow.alias_write_refused", who, str(e)[:100])
            return {"stored": False, "reason": str(e)[:120]}
        self._audit("call_flow.alias_written", who,
                    f"{'alias'[:20]}->{actual[:40]}"
                    if stored else "rejected_by_policy")
        return {"stored": stored is not None,
                "id": stored.id if stored else None}


def is_call_utterance(clean: str) -> bool:
    """Cheap structural check used by IntentTurnService's wants-turn
    branch (mirrors is_reminder_utterance)."""
    s = (clean or "").strip().lower()
    return s.startswith("call ") or s.startswith("dial ")


# ------------------------------------------------------------------
# Stage 31-P3 phase B (P3e): native carrier SMS on the SAME
# contact-resolution spine — the final step differs (SmManager send
# instead of the call), and the draft is first-class privacy: it echoes
# in the conversation + approval card but never in audit.
# ------------------------------------------------------------------

_SMS_INVITE_RE = re.compile(r"^\s*(?:text|sms)\s+(.+?)\s*$", re.I)
# "text sarah saying I'm late" / "text sarah: running late" / "text
# sarah that I'm running late".
_DRAFT_SEP_RE = re.compile(r"\s+(?:saying|that|about)\s+|\s*:\s+", re.I)
_TO_HEAD_RE = re.compile(r"^\s*to\s+", re.I)


class SmsFlow(CallFlow):
    """P3e: one carrier SMS per approval, recipients resolved through
    the SAME bounded contacts lookup + confirmed alias memory as the
    call flow. The draft is captured up-front ('text <name> saying
    <draft>'), or asked for in a dedicated draft phase ('text <name>' →
    "what should it say?"). Cancel words never become a draft."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pending: _PendingFlow | None = None

    def wants_turn(self, clean: str) -> bool:
        s = (clean or "").strip()
        if self._pending is not None and not self._pending.expired():
            return True
        if _SMS_INVITE_RE.match(s.replace("text me", "", 1)) and \
                not _INVITE_RE.match(s):
            return True
        return False

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        s = _TO_HEAD_RE.sub(" ", (clean or "").strip())
        low = s.lower().strip()
        pend = self._pending
        if pend is not None and not pend.expired():
            out = self._continuation(s, pend, who, requester_device)
            if out is not None:
                if self._pending is not None and \
                        self._pending.expired():
                    self._pending = None
                return out
            # A fresh SMS invitation replaces the pending flow; any
            # other utterance falls through to the parser (the pending
            # flow does NOT own unrelated work).
            if not _SMS_INVITE_RE.match(s):
                return None
        m = _SMS_INVITE_RE.match(s)
        if m is None or _INVITE_RE.match(s) is not None:
            self._audit("sms_flow.fallthrough", who, s[:80])
            return None
        rest = _clean_name(m.group(1))
        if not rest or _NUMBER_RE.match(rest):
            self._audit("sms_flow.raw_number_refused", who, rest[:60])
            return {"message":
                    "I don't text raw numbers from chat — the recipient "
                    "must come from a contact match you confirmed. Say "
                    "who to text (a name).",
                    "proposal": None, "resolution": None,
                    "executed": False, "refused": True}
        msep = _DRAFT_SEP_RE.search(rest)
        if msep is not None:
            name = _clean_name(rest[:msep.start()])
            draft = rest[msep.end():].strip()
        else:
            # A bare "text <name>": the draft is asked next turn —
            # cancel words are checked BEFORE they could become a draft.
            name, draft = rest, ""
        if not name or _NUMBER_RE.match(name):
            # A number riding text (or half-splitted) is STILL a raw
            # number: refused at the invite mouth, per the spec.
            self._audit("sms_flow.raw_number_refused", who, name[:60])
            return {"message":
                    "I don't text raw numbers from chat — the "
                    "recipient must come from a contact match you "
                    "confirmed. Say who to text (a name).",
                    "proposal": None, "resolution": None,
                    "executed": False, "refused": True}
        if not name:
            return {"message": "Who should the text go to? Say it as "
                               "'text <name> saying <your message>'.",
                    "proposal": None, "resolution": None,
                    "executed": False}
        if len(draft) > 400:
            # NO truncation guesses: an overlong draft is refused (the
            # native surface refuses it too — one rule, both layers).
            self._audit("sms_flow.draft_refused_len", who, len(draft))
            return {"message":
                    "The draft is longer than 400 characters — say it "
                    "shorter (I never truncate a message and hope "
                    "for the best).",
                    "proposal": None, "resolution": None,
                    "executed": False, "refused": True}
        # Alias memory resolves BEFORE any device lookup (shared spine).
        alias_hit = self._alias_lookup(name)
        if alias_hit is not None:
            self._audit("sms_flow.alias_resolved", who,
                        f"{name[:40]} -> {alias_hit[:40]}")
            name = alias_hit
        self._draft = draft
        self._cap_note = ""
        return self._do_lookup(name, who, requester_device,
                               alias_source=name if alias_hit else "")

    def _do_lookup(self, name, who, requester_device, alias_source=""):
        return super()._do_lookup(name, who, requester_device,
                                  alias_source=alias_source)

    # ---- SMS-specific phase surfaces ----

    def _offer(self, pend, who):
        pend.draft, self._cap_note = self._draft, getattr(
            self, "_cap_note", "")
        out = super()._offer(pend, who)
        if pend.phase == "alias_ask" and pend.draft:
            out["message"] += (f" Your draft is kept: '{pend.draft}'"
                               f"{self._cap_note}. Once we find the "
                               "contact, the usual approval follows.")
        return out

    def _confirm_line(self, pend):
        base = (f"Send this SMS to {pend.contact_name} "
                f"({_tail(pend.number)}): '{pend.draft}' — correct? "
                "Say 'yes' to send, or 'no' to cancel. The send still "
                "needs one approval in the app — I never text without "
                "it.")
        return base

    def _act(self, pend, who):
        rec = self.engine.submit(
            "android.sms.send",
            {"number": pend.number, "text": pend.draft, "confirm": True},
            pend.device_id, who=who)
        self._audit("sms_flow.send_submitted", who, rec.id)
        if rec.state == ExecutionState.WAITING_FOR_PERMISSION:
            with self._lock:
                pend.phase = "send_pending"
                pend.exec_id = rec.id
            return {"message":
                    f"Submitted the SMS to {pend.contact_name} "
                    f"('{pend.draft[:120]}') — approve it on "
                    f"{pend.device_id}; the card shows the full draft, "
                    "and I'll report what the radio says next turn.",
                    "proposal": None, "resolution": None,
                    "executed": True, "execution_id": rec.id,
                    "approval_required": True, "call_flow": "send_pending"}
        return self._report_send(rec, pend, who)

    def _report_parked(self, rec, pend, who) -> dict:
        if pend.phase == "send_pending":
            return self._report_send(rec, pend, who)
        return super()._report_parked(rec, pend, who)

    def _report_send(self, rec, pend, who):
        self._pending = None
        out = rec.result or {}
        if rec.state == ExecutionState.SUCCEEDED and \
                out.get("verdict") == "sent":
            return {"message":
                    f"Sent the SMS to {pend.contact_name} — the radio "
                    "accepted the send. Carrier delivery is not "
                    "confirmable by a bounded re-read, so I report it "
                    "as SENT-UNCONFIRMED, honestly.",
                    "proposal": None, "resolution": None,
                    "executed": True, "verified": False,
                    "call_flow": "sent"}
        if rec.state == ExecutionState.DENIED:
            return {"message": "The SMS send was denied before "
                               "dispatch — nothing was sent.",
                    "proposal": None, "resolution": None,
                    "executed": False, "call_flow": "denied"}
        return {"message": f"The SMS didn't send: "
                           f"{rec.error or 'device refused'}",
                "proposal": None, "resolution": None,
                "executed": False, "call_flow": "failed"}

    def _refuse_overlong(self, s, who):
        """>400-char drafts are refused (never truncated and hoped) —
        the SAME rule the native surface enforces."""
        if len(s.strip()) > 400:
            self._audit("sms_flow.draft_refused_len", who, len(s))
            return {"message":
                    "The draft is longer than 400 characters — say it "
                    "shorter (I never truncate a message and hope for "
                    "the best).",
                    "proposal": None, "resolution": None,
                    "executed": False, "refused": True}
        return None

    def _phase_phrase(self, s, pend, who, requester_device,
                      from_drain: bool = False):
        # The draft-ask phase comes BEFORE the shared phases: its
        # capture is strict about cancel words (they never become a
        # draft). Free text is a draft ONLY on a turn where the user
        # has SEEN the ask (from_drain turns report the offer first —
        # their phrase is never guessed onto a phase it just created).
        if pend.phase == "draft_ask":
            if from_drain:
                return None
            low = s.lower().strip()
            ref = self._refuse_overlong(s, who)
            if ref is not None:
                return ref
            if _NO_RE.match(low) or low in _CANCEL_WORDS:
                self._pending = None
                self._audit("sms_flow.draft_cancelled", who, "")
                return {"message": "Cancelled — no draft was written "
                                   "and nothing was sent.",
                        "proposal": None, "resolution": None,
                        "executed": False, "call_flow": "closed"}
            if _YES_RE.match(low) or low in _CONFIRM_WORDS:
                return {"message": "Tell me what to say (just type or "
                                   "say the message).",
                        "proposal": None, "resolution": None,
                        "executed": True, "call_flow": "draft_ask"}
            draft = s.strip().strip('"\'')
            if not draft:
                return None
            self._audit("sms_flow.draft_captured", who,
                        f"len={len(draft)}")
            self._draft = draft
            self._cap_note = ""
            pend.phase = "draft_ready"
            return {"message": f"Draft captured: '{draft[:160]}' — say "
                               "'send it' once you're happy with it "
                               "(or retype to change). After that, "
                               "the contact confirm + the in-app "
                               "approval follow.",
                    "proposal": None, "resolution": None,
                    "executed": True, "call_flow": "draft_ready"}
        # draft_ready: 'send it' → the confirm; retype → replace the
        # draft; cancel → close.
        if pend.phase == "draft_ready":
            low = s.lower().strip()
            ref = self._refuse_overlong(s, who)
            if ref is not None:
                return ref
            if _YES_RE.match(low) or low in _CONFIRM_WORDS or \
                    low in _SMS_SEND_WORDS:
                pend.phase = "confirm"
                return {"message": self._confirm_line(pend),
                        "proposal": None, "resolution": None,
                        "executed": True, "call_flow": "confirm"}
            if _NO_RE.match(low) or low in _CANCEL_WORDS:
                self._pending = None
                self._audit("sms_flow.draft_discarded", who, "")
                return {"message": "Draft discarded — nothing was "
                                   "sent.",
                        "proposal": None, "resolution": None,
                        "executed": False, "call_flow": "closed"}
            self._draft = s.strip()
            self._audit("sms_flow.draft_retyped", who,
                        f"len={len(s)}")
            pend.phase = "draft_ready"
            return {"message": f"Draft replaced: '{self._draft[:160]}' "
                               "— say 'send it' when ready.",
                    "proposal": None, "resolution": None,
                    "executed": True, "call_flow": "draft_ready"}
        # send_pending: the drain branch in _continuation already
        # reports it; a confirmation phrase here does nothing.
        return super()._phase_phrase(s, pend, who, requester_device)

    def _confirm_offer(self, pend):
        """With a draft: the standard confirm (draft echoed). Without
        one: adopt the match (alias memory still records), then ASK
        for the draft — cancel words are never a draft."""
        pend.draft, self._cap_note = self._draft, getattr(
            self, "_cap_note", "")
        self._adopt_match(pend)
        if not pend.draft:
            pend.phase = "draft_ask"
            return {"message": f"I found {pend.contact_name} "
                               f"({_tail(pend.number)}) — what should "
                               "the text say? (Say 'never mind' to "
                               "cancel.)",
                    "proposal": None, "resolution": None,
                    "executed": True, "call_flow": "draft_ask"}
        return super()._confirm_offer(pend)
