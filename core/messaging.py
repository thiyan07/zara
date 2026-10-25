"""Messaging workflow contract over snapshot-bound GUI primitives.

Two layers, both app-independent and extending existing components:

1. **Pure contract** (`find_conversations`, `find_text_field`,
   `find_send_button`, `read_messages`, `build_send_plan`, `verify_send`,
   `send_fingerprint`, `SendLedger`): analysis over `gui.screen.inspect`
   snapshots. Never executes, authorizes, or touches the network.

2. **Mission-bound approval workflow** (`MessagingWorkflow`,
   `MessagingApprovalStore`): ONE bounded human approval for a whole
   send sequence (resolve conversation -> inspect -> compose -> send ->
   verify). The approval binds the mission, recipient/conversation
   identity, exact message content, target device, and the ordered
   semantic action sequence. Any material change invalidates it. After
   approval, each action runs through the EXISTING fabric execution path
   (identity -> trust -> presence -> capability -> governor -> engine ->
   verification); the workflow only supplies the scoped authorization
   the single approval minted (via policy.grant), never a bypass.

Security rules enforced by construction:
- Labels come from untrusted UI text; matching is exact-substring on a
  caller-supplied query, never fuzzy, never model-inferred.
- Ambiguous recipients resolve to `ambiguous`, never a guess.
- Typed message text is capped (500) and never written to audit or logs
  (audit carries only safe fingerprints). The in-memory approval record
  holds the plaintext so approval can execute; `view()` never exposes
  it, and the record is never persisted.
- Send results are `sent` only on positive evidence; anything else is
  `uncertain` (never auto-retry: a retry could duplicate the message).
- Node ids are resolved from FRESH snapshots at execution time, never
  frozen into the approval: positional indices are volatile across
  navigation. The approval binds the semantic plan, not the layout.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

MAX_TEXT = 500
MAX_NODES_SCANNED = 200
LEDGER_TTL_S = 3600
LEDGER_MAX = 500
APPROVAL_TTL_S = 300.0
APPROVAL_MAX = 200

# The ordered semantic actions a bound send plan always contains. The
# approval fingerprint includes this skeleton, so an approval can never
# authorize a different action sequence.
ACTION_SEQUENCE = ("open_conversation", "compose", "send", "verify")

_SEND_LABELS = ("send", "sent", "deliver")


def _nodes(snapshot: Any) -> list:
    if isinstance(snapshot, dict):
        nodes = snapshot.get("nodes")
        if isinstance(nodes, list):
            return nodes[:MAX_NODES_SCANNED]
    return []


def _label(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    for k in ("content_desc", "text"):
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _node_id(node: Any) -> int:
    try:
        i = int(node.get("node_id", -1))
    except (TypeError, ValueError):
        return -1
    return i if i >= 0 else -1


def find_conversations(snapshot: Any, query: str = "") -> dict:
    """Discover conversations: clickable labeled nodes, filtered by an
    exact-substring query. Empty query lists candidates (capped)."""
    q = (query or "").strip().lower()
    cands = []
    for n in _nodes(snapshot):
        if not isinstance(n, dict) or not n.get("clickable"):
            continue
        label = _label(n)
        if not label:
            continue
        if q and q not in label.lower():
            continue
        cands.append({"node_id": _node_id(n), "label": label[:120]})
        if len(cands) >= 20:
            break
    cands = [c for c in cands if c["node_id"] >= 0]
    if not cands:
        return {"status": "missing",
                "detail": f"no conversation matching {query!r}"[:200]}
    if q and len(cands) > 1:
        return {"status": "ambiguous",
                "detail": f"{len(cands)} conversations match "
                          f"{query!r} — which one?"[:200],
                "candidates": cands}
    return {"status": "found" if q else "listed", "candidates": cands}


def find_text_field(snapshot: Any) -> dict:
    """Locate the message-composition field (editable node)."""
    fields = [{"node_id": _node_id(n), "label": _label(n)[:120]}
              for n in _nodes(snapshot)
              if isinstance(n, dict) and n.get("editable")]
    fields = [f for f in fields if f["node_id"] >= 0]
    if not fields:
        return {"status": "missing",
                "detail": "no editable text field in this snapshot"}
    if len(fields) > 1:
        return {"status": "ambiguous",
                "detail": "several editable fields — which one?"[:200],
                "candidates": fields[:10]}
    return {"status": "found", "field": fields[0]}


def find_send_button(snapshot: Any) -> dict:
    """Locate the send action (clickable node with a send-like label)."""
    cands = []
    for n in _nodes(snapshot):
        if not isinstance(n, dict) or not n.get("clickable"):
            continue
        label = _label(n).lower()
        if any(s in label for s in _SEND_LABELS):
            cands.append({"node_id": _node_id(n),
                          "label": _label(n)[:120]})
    cands = [c for c in cands if c["node_id"] >= 0]
    if not cands:
        return {"status": "missing",
                "detail": "no send button in this snapshot"}
    if len(cands) > 1:
        return {"status": "ambiguous",
                "detail": "several send buttons — which one?"[:200],
                "candidates": cands[:10]}
    return {"status": "found", "button": cands[0]}


def read_messages(snapshot: Any, limit: int = 30) -> dict:
    """Read accessible message texts. Content is UNTRUSTED UI data (never
    policy, never authority); caller must treat it as such."""
    items = []
    for n in _nodes(snapshot):
        label = _label(n)
        if label:
            items.append({"node_id": _node_id(n),
                          "text": label[:500], "untrusted": True})
        if len(items) >= max(1, min(limit, 100)):
            break
    return {"status": "ok" if items else "missing",
            "messages": items,
            "note": "untrusted UI text; verify before acting on it"}


def _snapshot_id(snapshot: Any) -> str:
    v = snapshot.get("snapshot_id") if isinstance(snapshot, dict) else ""
    return v if isinstance(v, str) else ""


def _package(snapshot: Any) -> str:
    v = snapshot.get("package") if isinstance(snapshot, dict) else ""
    return v if isinstance(v, str) else ""


def build_send_plan(snapshot: Any, conversation_label: str,
                    text: str) -> dict:
    """Build an explicit snapshot-bound step list for one message send
    when the caller already holds a stable snapshot (e.g. the app's own
    UI, or a conversation list that will not navigate). Steps: open
    conversation -> set text -> tap send.

    NOTE: node ids are positional, so this plan is only valid while THAT
    snapshot is current. The mission-bound `MessagingWorkflow` does NOT
    use this directly: it re-resolves each target from a fresh snapshot
    at execution time, so navigation between steps cannot strand it.
    Returns the plan or a structured refusal (missing/ambiguous)."""
    msg = (text or "")[:MAX_TEXT]
    if not msg.strip():
        return {"status": "refused", "detail": "empty message (refused)"}
    conv = find_conversations(snapshot, conversation_label)
    if conv["status"] != "found" or len(conv.get("candidates", [])) != 1:
        return {"status": conv["status"],
                "detail": conv.get("detail", "conversation unresolved"),
                "candidates": conv.get("candidates", [])}
    field = find_text_field(snapshot)
    if field["status"] != "found":
        return {"status": field["status"], "detail": field["detail"],
                "candidates": field.get("candidates", [])}
    send = find_send_button(snapshot)
    if send["status"] != "found":
        return {"status": send["status"], "detail": send["detail"],
                "candidates": send.get("candidates", [])}
    sid, pkg = _snapshot_id(snapshot), _package(snapshot)
    bind = {}
    if sid:
        bind["expected_snapshot_id"] = sid
    if pkg:
        bind["expected_package"] = pkg
    target_pkg = pkg or "unknown.package"
    steps = [
        {"tool": "gui.tap",
         "inputs": {"target_package": target_pkg,
                    "node_id": conv["candidates"][0]["node_id"], **bind},
         "purpose": f"open conversation "
                    f"{conv['candidates'][0]['label'][:60]!r}"},
        {"tool": "gui.set_text",
         "inputs": {"target_package": target_pkg,
                    "node_id": field["field"]["node_id"],
                    "text": msg, **bind},
         "purpose": "compose message (text never echoed back)"},
        {"tool": "gui.tap",
         "inputs": {"target_package": target_pkg,
                    "node_id": send["button"]["node_id"], **bind},
         "purpose": "submit send (single attempt, never retried)"},
    ]
    return {"status": "planned", "steps": steps,
            "fingerprint": send_fingerprint(
                conv["candidates"][0]["label"], msg),
            "warning": "executing sends a real message; each step needs "
                       "its normal approval and runs once only"}


def verify_send(before: Any, after: Any, text: str) -> dict:
    """Compare pre/post snapshots for send evidence. `sent` requires the
    message tail to appear in a NEW text node; anything else is
    `uncertain` (caller must NOT auto-retry)."""
    tail = (text or "")[-40:]
    if not tail.strip():
        return {"status": "uncertain",
                "detail": "empty reference text (cannot verify)"}
    before_texts = {_label(n) for n in _nodes(before)}
    fresh = [t for t in (_label(n) for n in _nodes(after))
             if t and t not in before_texts]
    if any(tail.strip() in t for t in fresh):
        return {"status": "sent",
                "detail": "message tail present in a new UI node"}
    changed = len(fresh) > 0
    return {"status": "uncertain",
            "detail": "no matching new message node; "
                      + ("screen changed but message unconfirmed"
                         if changed else "screen unchanged")
                      + " (do not retry blindly)"}


def send_fingerprint(conversation_label: str, text: str) -> str:
    """Stable idempotency fingerprint for one send (sha256, hex)."""
    h = hashlib.sha256()
    h.update(b"zara-message-send\x00")
    h.update((conversation_label or "").encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update((text or "").encode("utf-8", "replace"))
    return h.hexdigest()


def plan_fingerprint(*, mission_id: str, device_id: str,
                     conversation_label: str, content_fingerprint: str,
                     actions: Any = ACTION_SEQUENCE) -> str:
    """Integrity digest binding an approval to the exact operation it
    authorizes: mission, target device, recipient/conversation identity,
    exact message content (by fingerprint, never plaintext), and the
    ordered action skeleton. Any material change changes the digest and
    invalidates the approval. For equality/integrity, not authentication."""
    payload = {
        "mission_id": mission_id or "",
        "device_id": device_id or "",
        "conversation": conversation_label or "",
        "content": content_fingerprint or "",
        "actions": list(actions or ACTION_SEQUENCE),
    }
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


class SendLedger:
    """Record of message sends that were actually SUBMITTED (the send tap
    applied). A repeat of the same (conversation, text) fingerprint is
    refused instead of double-sending — across repeated requests, resumed
    missions, and (when a path is configured) process restarts.

    Semantics:
    - `seen(fp)` -> True iff fp was already recorded (pure read).
    - `record(fp)` -> records fp; True iff newly recorded.
    - `check_and_record(fp)` -> backward-compatible check+record (True =
      duplicate; first sight is recorded and returns False).
    """

    def __init__(self, ttl_s: float = LEDGER_TTL_S,
                 max_entries: int = LEDGER_MAX,
                 path: Optional[str] = None) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}
        self._db = None
        if path:
            try:
                self._db = sqlite3.connect(path, check_same_thread=False)
                with self._db:
                    self._db.execute(
                        "CREATE TABLE IF NOT EXISTS send_ledger ("
                        "fingerprint TEXT PRIMARY KEY, ts REAL NOT NULL)")
            except Exception:  # noqa: BLE001 — durable ledger is best-effort
                self._db = None

    def _sweep_locked(self, now: float) -> None:
        for k, ts in list(self._seen.items()):
            if now - ts > self.ttl_s:
                del self._seen[k]
        if self._db is not None:
            try:
                with self._db:
                    self._db.execute("DELETE FROM send_ledger WHERE ts < ?",
                                     (now - self.ttl_s,))
            except Exception:  # noqa: BLE001
                pass

    def seen(self, fingerprint: str) -> bool:
        fp = str(fingerprint or "")
        if not fp:
            return False
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            if fp in self._seen:
                return True
            if self._db is not None:
                try:
                    row = self._db.execute(
                        "SELECT 1 FROM send_ledger WHERE fingerprint = ?",
                        (fp,)).fetchone()
                    return row is not None
                except Exception:  # noqa: BLE001
                    return False
            return False

    def record(self, fingerprint: str) -> bool:
        """Record a SUBMITTED send. True iff newly recorded."""
        fp = str(fingerprint or "")
        if not fp:
            return False
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            if fp in self._seen:
                return False
            self._seen[fp] = now
            while len(self._seen) > self.max_entries:
                self._seen.pop(next(iter(self._seen)))
            if self._db is not None:
                try:
                    with self._db:
                        self._db.execute(
                            "INSERT OR IGNORE INTO send_ledger "
                            "(fingerprint, ts) VALUES (?, ?)", (fp, now))
                except Exception:  # noqa: BLE001
                    pass
            return True

    def check_and_record(self, fingerprint: str) -> bool:
        """True = duplicate (already sent, refuse). False = first sight
        (recorded, may proceed). Never raises."""
        try:
            if self.seen(fingerprint):
                return True
            self.record(fingerprint)
            return False
        except Exception:  # noqa: BLE001 — ledger never breaks sends
            return False


class MessagingApprovalStore:
    """Bounded, TTL'd store of mission-bound messaging approvals.

    Each record's BINDING is immutable: it lives in `fingerprint` and the
    fields it covers (mission, device, conversation, content fingerprint,
    action skeleton). The only transitions are pending -> approved ->
    completed / uncertain / failed / cancelled / expired. `create()` is
    idempotent per (mission_id, fingerprint): a duplicate request returns
    the SAME pending approval instead of minting a second one.
    """

    def __init__(self, ttl_s: float = APPROVAL_TTL_S,
                 max_entries: int = APPROVAL_MAX) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._by_id: dict[str, dict] = {}
        self._by_key: dict[tuple, str] = {}

    def _sweep_locked(self, now: float) -> None:
        for a in self._by_id.values():
            if a["state"] == "pending" and now - a["created_at"] > self.ttl_s:
                a["state"] = "expired"
        if len(self._by_id) > self.max_entries:
            ordered = sorted(self._by_id.values(),
                             key=lambda a: a["created_at"])
            for a in ordered[:len(self._by_id) - self.max_entries]:
                if a["state"] == "pending":
                    a["state"] = "expired"
                self._by_id.pop(a["approval_id"], None)

    def create(self, *, mission_id: str, device_id: str,
               conversation_label: str, content_fingerprint: str,
               snapshot_id: str, package: str, text: str,
               who: str = "user") -> tuple:
        """Create (or return the existing pending) approval. Returns
        (record, duplicate: bool). `text` is held in memory only so the
        approval can execute; `view()` never exposes it."""
        fp = plan_fingerprint(mission_id=mission_id, device_id=device_id,
                              conversation_label=conversation_label,
                              content_fingerprint=content_fingerprint)
        # Dedup key deliberately EXCLUDES mission_id: two duplicate requests
        # for the same recipient/content/device must return the SAME pending
        # approval, not mint a second (the ledger is what ultimately stops a
        # double-send; dedup here keeps the approval count at one).
        dkey = plan_fingerprint(mission_id="", device_id=device_id,
                                conversation_label=conversation_label,
                                content_fingerprint=content_fingerprint)
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            key = dkey
            prior = self._by_key.get(key)
            if prior is not None:
                a = self._by_id.get(prior)
                if a is not None and a["state"] == "pending":
                    return a, True
            aid = f"msgappr-{uuid.uuid4().hex[:12]}"
            rec = {
                "approval_id": aid, "mission_id": mission_id or "",
                "device_id": device_id or "",
                "conversation_label": conversation_label or "",
                "content_fingerprint": content_fingerprint or "",
                "snapshot_id": snapshot_id or "", "package": package or "",
                "who": who, "fingerprint": fp,
                "actions": list(ACTION_SEQUENCE),
                "_text": (text or "")[:MAX_TEXT],
                "state": "pending", "created_at": now, "approved_at": None,
                "ended_at": None, "outcome": None, "executions": [],
            }
            self._by_id[aid] = rec
            self._by_key[key] = aid
            return rec, False

    def get(self, approval_id: str) -> dict:
        with self._lock:
            self._sweep_locked(time.time())
            return self._by_id[approval_id]

    def find_pending(self, *, device_id: str, conversation_label: str,
                     content_fingerprint: str) -> Optional[dict]:
        """Return the existing PENDING approval for this recipient/content/
        device, or None. Used to make duplicate requests idempotent (the
        same approval, never a second one or a second mission)."""
        key = plan_fingerprint(mission_id="", device_id=device_id,
                               conversation_label=conversation_label,
                               content_fingerprint=content_fingerprint)
        with self._lock:
            self._sweep_locked(time.time())
            aid = self._by_key.get(key)
            if aid is None:
                return None
            rec = self._by_id.get(aid)
            if rec is not None and rec["state"] == "pending":
                return rec
            return None

    def list_pending(self, limit: int = 50) -> list:
        """Redacted views of every still-PENDING approval, oldest first.

        Companion-app surface (GET /v1/messaging/approvals). Bounded;
        expired/terminal records never appear; plaintext and fingerprint
        stay secret exactly as in `view()`."""
        with self._lock:
            self._sweep_locked(time.time())
            pend = [a for a in self._by_id.values() if a["state"] == "pending"]
            pend.sort(key=lambda a: a["created_at"])
            return [self.view(a) for a in pend[:max(1, int(limit))]]

    def approve(self, approval_id: str) -> dict:
        """pending -> approved; refuse expired/terminal states. Raises
        KeyError (missing) / ValueError (not pending)."""
        with self._lock:
            self._sweep_locked(time.time())
            try:
                a = self._by_id[approval_id]
            except KeyError:
                raise KeyError(approval_id)
            if a["state"] == "pending":
                a["state"] = "approved"
                a["approved_at"] = time.time()
                return a
            raise ValueError(
                f"approval {approval_id} is not awaiting a decision "
                f"({a['state']})")

    def cancel(self, approval_id: str) -> dict:
        with self._lock:
            a = self._by_id[approval_id]
            if a["state"] in ("pending", "approved", "executing"):
                a["state"] = "cancelled"
                a["ended_at"] = time.time()
            return a

    def is_cancelled(self, approval_id: str) -> bool:
        try:
            with self._lock:
                return self._by_id[approval_id]["state"] == "cancelled"
        except KeyError:
            return True

    def finish(self, approval_id: str, state: str, outcome: dict,
               executions: Optional[list] = None) -> dict:
        with self._lock:
            a = self._by_id[approval_id]
            if a["state"] == "cancelled":
                return a  # operator cancel wins over a late outcome
            a["state"] = state
            a["outcome"] = outcome
            a["ended_at"] = time.time()
            if executions is not None:
                a["executions"] = executions
            return a

    @staticmethod
    def view(a: dict) -> dict:
        """Redacted, user-facing view: never includes message plaintext or
        the binding fingerprint (integrity material)."""
        return {
            "approval_id": a["approval_id"],
            "mission_id": a["mission_id"],
            "device_id": a["device_id"],
            "conversation": a["conversation_label"],
            "state": a["state"],
            "actions": list(a["actions"]),
            "created_at": a["created_at"],
            "approved_at": a["approved_at"],
            "outcome": a.get("outcome"),
            "executions": list(a.get("executions") or []),
        }


class MessagingWorkflow:
    """Mission-bound single-approval messaging over the fabric.

    Flow:
      request_send(snapshot, conversation, text, device, mission) ->
        structured approval presentation (nothing runs)
      approve(approval_id) ->
        run the bound actions through the fabric, fresh-inspecting each
        target, verify the send, report succeeded/failed/blocked/stale/
        uncertain/cancelled.

    The workflow never re-implements routing/policy/execution: it calls
    `fabric.execute_on_device`, which re-runs identity/trust/presence/
    capability/governor and the engine's HIGH_RISK pre-dispatch
    re-validation. The single approval only mints the scoped
    authorization (via policy.grant) that the bound actions consume.
    """

    def __init__(self, fabric, missions=None, audit=None,
                 ledger: Optional[SendLedger] = None,
                 approvals: Optional[MessagingApprovalStore] = None,
                 resolver=None) -> None:
        self.fabric = fabric
        self.missions = missions
        self.audit = audit
        self.ledger = ledger or SendLedger()
        self.approvals = approvals or MessagingApprovalStore()
        self.resolver = resolver

    # ----- helpers -----

    def _audit(self, action: str, target: str = "", detail: str = "",
               ok: bool = True) -> None:
        try:
            if self.audit is not None:
                self.audit.record("messaging", action, target, detail[:500],
                                  ok=ok)
        except Exception:  # noqa: BLE001 — audit never breaks a send
            pass

    def _step_decision(self, tool: str, inputs: dict, device_id: str,
                       who: str, approver: str, approval_id: str):
        """Mint the scoped authorization for ONE bound action. Never
        bypasses deny-patterns: a hard-deny decision returns None."""
        try:
            definition = self.fabric.registry.get(tool)
        except KeyError:
            return None
        try:
            decision = self.fabric.policy.decide(
                who, capability=f"tool:{tool}", device_id=device_id,
                resource=tool, risk=definition.risk,
                context={"command": str(inputs)})
        except Exception:  # noqa: BLE001 — fail closed
            return None
        if getattr(decision, "hard_deny", False):
            return None
        from .models import PolicyDecision
        gid = self.fabric.policy.grant(
            approver, f"tool:{tool}", device_id, tool, ttl_s=60,
            used_now=True, source="approval")
        return PolicyDecision(
            allow=True, grant_id=gid, scope=f"tool:{tool}@{device_id}",
            reason=f"mission-bound messaging approval {approval_id}")

    def _execute_step(self, tool: str, inputs: dict, device_id: str,
                      who: str, approver: str, approval_id: str,
                      mission_id: str, executions: list) -> dict:
        """Execute one bound action through the fabric with the scoped
        decision. Records the execution id for later cancellation."""
        decision = self._step_decision(tool, inputs, device_id, who,
                                       approver, approval_id)
        if decision is None:
            return {"stage": "denied", "reason": "policy denies this action"}
        try:
            out = self.fabric.execute_on_device(
                tool, dict(inputs), device_id, who,
                mission_id=mission_id or "", auth=decision)
        except Exception as e:  # noqa: BLE001 — execution error is a message
            return {"stage": "failed", "reason": f"{type(e).__name__}: {e}"}
        eid = out.get("execution_id")
        if eid:
            executions.append(eid)
        return out

    def _inspect(self, device_id: str, who: str, approver: str,
                 approval_id: str, mission_id: str, executions: list) -> dict:
        return self._execute_step("gui.screen.inspect", {}, device_id, who,
                                  approver, approval_id, mission_id,
                                  executions)

    @staticmethod
    def _ok(out: dict) -> bool:
        return out.get("stage") in ("succeeded", "completed", "ok")

    @staticmethod
    def _classify(out: dict) -> tuple:
        """Map a fabric outcome onto a structured messaging status."""
        stage = str(out.get("stage", ""))
        blob = f"{out.get('reason', '')} {out.get('error', '')}".lower()
        if stage in ("rejected", "deferred", "denied") or \
                "not present" in blob or "offline" in blob or \
                "presence" in blob or "not executable" in blob or \
                "trust" in blob or "policy denies" in blob:
            return ("blocked",
                    f"device not usable right now: "
                    f"{out.get('reason', stage)}"[:200])
        if "stale_snapshot" in blob or "stale snapshot" in blob or \
                "snapshot changed" in blob:
            return ("stale",
                    "the screen changed since the approval (stale "
                    "snapshot); refused, nothing was sent")
        if "not clickable" in blob or "not editable" in blob or \
                "not applied" in blob or "missing" in blob:
            return ("failed",
                    f"action refused by the device: "
                    f"{out.get('reason', out.get('error', stage))}"[:200])
        return ("failed", f"action failed: "
                          f"{out.get('reason', out.get('error', stage))}"[:200])

    def _mission_transition(self, mission_id: str, to) -> None:
        if not mission_id or self.missions is None:
            return
        try:
            from .missions import MissionState, TRANSITIONS
            cur = self.missions.get(mission_id).state
            if cur in (MissionState.COMPLETED, MissionState.FAILED,
                       MissionState.CANCELLED):
                return
            if to in TRANSITIONS.get(cur, set()):
                self.missions.transition(mission_id, to)
        except Exception:  # noqa: BLE001 — bookkeeping only
            pass

    def _mission_finish(self, mission_id: str, state: str) -> None:
        from .missions import MissionState
        target = {
            "completed": MissionState.COMPLETED,
            "uncertain": MissionState.FAILED,
            "failed": MissionState.FAILED,
            "cancelled": MissionState.CANCELLED,
        }.get(state)
        if target is None:
            return
        # Failed/uncertain may need to pass through WAITING/EXECUTING edges;
        # transition directly when allowed, else step through EXECUTING.
        self._mission_transition(mission_id, MissionState.EXECUTING)
        self._mission_transition(mission_id, target)

    # ----- request / approve / cancel -----

    def request_send(self, *, snapshot: Optional[dict], conversation: str,
                     text: str, device_id: str, mission_id: str = "",
                     who: str = "user") -> dict:
        """Present ONE approval for the whole send sequence. Executes
        NOTHING. Returns {status, approval_id, message, binding} or a
        structured refusal.

        `snapshot` is OPTIONAL: when the caller already holds a fresh
        conversation-list snapshot, the conversation is resolved and
        pre-verified here (a missing/ambiguous recipient is refused before
        any approval exists). When it is absent, the approval binds the
        caller-supplied recipient label and the conversation is re-resolved
        from a FRESH snapshot INSIDE the approved execution — so a read is
        part of the same single approval, and an unresolvable recipient
        aborts at execution (never guessed)."""
        conversation = (conversation or "").strip()[:120]
        if not conversation:
            return {"status": "refused",
                    "detail": "which conversation should I send to?"}
        conv_label = conversation
        if isinstance(snapshot, dict) and snapshot:
            conv = find_conversations(snapshot, conversation)
            if conv["status"] != "found" or \
                    len(conv.get("candidates", [])) != 1:
                self._audit("messaging.send.refused", device_id,
                            f"conversation={conv['status']}", ok=False)
                return {"status": conv["status"],
                        "detail": conv.get("detail",
                                           "conversation unresolved"),
                        "candidates": conv.get("candidates", [])}
            conv_label = conv["candidates"][0]["label"]
        msg = (text or "").strip()
        if not msg:
            return {"status": "refused", "detail": "empty message (refused)"}
        msg = msg[:MAX_TEXT]
        # The composer field and send button are NOT required here: the
        # request snapshot is typically the conversation list, and layout
        # is volatile. They are re-resolved from FRESH snapshots at
        # execution time; a missing one then aborts honestly before any
        # send (never a guess).
        content_fp = send_fingerprint(conv_label, msg)
        if self.ledger.seen(content_fp):
            self._audit("messaging.send.duplicate_refused", device_id,
                        "content fingerprint already submitted", ok=False)
            return {"status": "duplicate",
                    "detail": "a message with this exact text to this "
                              "conversation was already submitted; "
                              "refusing to send it again"}
        existing = self.approvals.find_pending(
            device_id=device_id, conversation_label=conv_label,
            content_fingerprint=content_fp)
        if existing is not None:
            # Duplicate request: reuse the SAME pending approval and its
            # original mission. No second record, no orphan mission.
            self._audit("messaging.send.duplicate_request", device_id,
                        f"approval={existing['approval_id']} reused")
            return self._present(existing, existing["mission_id"],
                                 device_id, conv_label, content_fp, msg,
                                 duplicate=True)
        # Mission tracking (redacted: never persists message text).
        mid = mission_id
        if not mid and self.missions is not None:
            try:
                from .missions import MissionState
                m = self.missions.create(
                    f"Send a message to {conv_label[:60]}",
                    [{"tool": "messaging.send",
                      "inputs": {"conversation": conv_label[:120],
                                 "content_fingerprint": content_fp[:16]}}],
                    device_id or None)
                mid = m.id
                self.missions.transition(mid, MissionState.PLANNING)
                self.missions.transition(
                    mid, MissionState.WAITING_FOR_PERMISSION)
            except Exception:  # noqa: BLE001 — tracking is best-effort
                mid = ""
        rec, dup = self.approvals.create(
            mission_id=mid, device_id=device_id,
            conversation_label=conv_label, content_fingerprint=content_fp,
            snapshot_id=_snapshot_id(snapshot), package=_package(snapshot),
            text=msg, who=who)
        self._audit("messaging.send.requested", device_id,
                    f"approval={rec['approval_id']} "
                    f"content={content_fp[:16]} "
                    f"conversation={conv_label[:60]}")
        return self._present(rec, mid, device_id, conv_label, content_fp, msg,
                             duplicate=dup)

    def _present(self, rec: dict, mid: str, device_id: str,
                 conv_label: str, content_fp: str, msg: str,
                 *, duplicate: bool) -> dict:
        """Build the user-facing approval presentation (includes the exact
        text so the user can decide; the text is never logged)."""
        view = MessagingApprovalStore.view(rec)
        view["binding"] = {
            "mission_id": mid, "device_id": device_id,
            "conversation": conv_label,
            "content_fingerprint": content_fp[:16],
        }
        view["recipient"] = conv_label
        view["message_text"] = msg  # shown to the user; never logged
        view["status"] = "pending_approval"
        view["duplicate"] = duplicate
        view["message"] = (
            f"Approve sending to '{conv_label}' on "
            f"{device_id or 'the device'}?\nMessage: {msg}\n"
            f"This one approval covers: open conversation, type the "
            f"message, press send, and verify. Any change to the recipient "
            f"or text needs a new approval. Approval id: "
            f"{rec['approval_id']}.")
        return view

    def approve(self, approval_id: str, approver: str = "user") -> dict:
        """Execute ONLY the approved plan. Structural outcomes:
        succeeded | uncertain | failed | blocked | stale | ambiguous |
        duplicate | cancelled | expired | not_approvable | missing."""
        try:
            a = self.approvals.approve(approval_id)
        except KeyError:
            return {"status": "missing",
                    "detail": f"unknown approval {approval_id}"}
        except ValueError as e:
            try:
                cur = self.approvals.get(approval_id)["state"]
            except KeyError:
                cur = "missing"
            if cur == "cancelled":
                return {"status": "cancelled", "approval_id": approval_id,
                        "detail": "approval was cancelled"}
            return {"status": cur, "detail": str(e)[:200],
                    "approval_id": approval_id}
        # Integrity re-check: recompute the binding fingerprint from the
        # stored fields; a tampered/mutated record must not authorize.
        recomputed = plan_fingerprint(
            mission_id=a["mission_id"], device_id=a["device_id"],
            conversation_label=a["conversation_label"],
            content_fingerprint=a["content_fingerprint"],
            actions=a["actions"])
        if recomputed != a["fingerprint"]:
            self.approvals.finish(approval_id, "failed",
                                  {"status": "invalidated",
                                   "detail": "approval binding changed"})
            self._audit("messaging.send.invalidated", a["device_id"],
                        f"approval={approval_id} binding drift", ok=False)
            return {"status": "blocked", "approval_id": approval_id,
                    "detail": "approval binding no longer matches the "
                              "approved plan (refused)"}
        if self.ledger.seen(a["content_fingerprint"]):
            self.approvals.finish(approval_id, "duplicate",
                                  {"status": "duplicate",
                                   "detail": "already submitted"})
            self._audit("messaging.send.duplicate_refused", a["device_id"],
                        f"approval={approval_id} already submitted",
                        ok=False)
            return {"status": "duplicate", "approval_id": approval_id,
                    "detail": "this message was already submitted; "
                              "not resending"}
        from .missions import MissionState
        self._mission_transition(a["mission_id"], MissionState.EXECUTING)
        self._audit("messaging.send.approved", a["device_id"],
                    f"approval={approval_id}")
        return self._run_plan(a, approver)

    def cancel(self, approval_id: str, who: str = "user") -> dict:
        try:
            a = self.approvals.cancel(approval_id)
        except KeyError:
            return {"status": "missing"}
        # Reach any execution already dispatched for this approval.
        for eid in list(a.get("executions", [])):
            try:
                self.fabric.engine.cancel(eid)
            except Exception:  # noqa: BLE001
                pass
        self._audit("messaging.send.cancelled", a["device_id"],
                    f"approval={approval_id}")
        return {"status": "cancelled", "approval_id": approval_id}

    def status(self, approval_id: str) -> dict:
        try:
            return MessagingApprovalStore.view(
                self.approvals.get(approval_id))
        except KeyError:
            return {"status": "missing"}

    # ----- execution -----

    def _run_plan(self, a: dict, approver: str) -> dict:
        approval_id = a["approval_id"]
        device_id = a["device_id"]
        mission_id = a["mission_id"]
        who = a["who"]
        conversation = a["conversation_label"]
        content_fp = a["content_fingerprint"]
        package = a["package"]
        text = a["_text"]
        executions: list[str] = []
        self._audit("messaging.send.executing", device_id,
                    f"approval={approval_id} content={content_fp[:16]}")

        def done(state: str, outcome: dict) -> dict:
            self.approvals.finish(approval_id, state, outcome, executions)
            self._mission_finish(mission_id, state)
            out = dict(outcome)
            out["approval_id"] = approval_id
            out["status"] = outcome.get("status", state)
            out["executions"] = executions
            self._audit("messaging.send.finished", device_id,
                        f"approval={approval_id} status={out['status']}",
                        ok=out["status"] in ("succeeded", "uncertain"))
            return out

        def cancelled() -> bool:
            return self.approvals.is_cancelled(approval_id)

        def inspect() -> dict:
            return self._inspect(device_id, who, approver, approval_id,
                                 mission_id, executions)

        # --- 1. open conversation (fresh resolve; never guess) ---
        if cancelled():
            return done("cancelled", {"status": "cancelled"})
        insp = inspect()
        if not self._ok(insp):
            st, dt = self._classify(insp)
            return done("failed", {"status": st, "detail": dt,
                                   "stage": "open_conversation"})
        snap = insp.get("result") or {}
        conv = find_conversations(snap, conversation)
        if conv["status"] != "found" or len(conv.get("candidates", [])) != 1:
            return done("failed",
                        {"status": "ambiguous"
                         if conv["status"] == "ambiguous" else "blocked",
                         "detail": f"conversation no longer uniquely "
                                   f"resolvable at execution time: "
                                   f"{conv.get('detail', conv['status'])}",
                         "stage": "open_conversation"})
        pkg = package or _package(snap)
        tap_out = self._execute_step(
            "gui.tap",
            {"target_package": pkg,
             "node_id": conv["candidates"][0]["node_id"],
             "expected_snapshot_id": _snapshot_id(snap),
             "expected_package": pkg},
            device_id, who, approver, approval_id, mission_id, executions)
        if not self._ok(tap_out):
            st, dt = self._classify(tap_out)
            if cancelled():
                return done("cancelled", {"status": "cancelled"})
            return done("failed", {"status": st, "detail": dt,
                                   "stage": "open_conversation"})

        # --- 2. compose (fresh snapshot after navigation) ---
        if cancelled():
            return done("cancelled", {"status": "cancelled"})
        insp2 = inspect()
        if not self._ok(insp2):
            st, dt = self._classify(insp2)
            return done("failed", {"status": st, "detail": dt,
                                   "stage": "compose"})
        snap2 = insp2.get("result") or {}
        field = find_text_field(snap2)
        if field["status"] != "found":
            return done("failed",
                        {"status": "blocked",
                         "detail": f"message field unavailable: "
                                   f"{field['detail']}",
                         "stage": "compose"})
        pkg2 = package or _package(snap2)
        set_out = self._execute_step(
            "gui.set_text",
            {"target_package": pkg2, "node_id": field["field"]["node_id"],
             "text": text, "expected_snapshot_id": _snapshot_id(snap2),
             "expected_package": pkg2},
            device_id, who, approver, approval_id, mission_id, executions)
        if not self._ok(set_out):
            st, dt = self._classify(set_out)
            return done("failed", {"status": st, "detail": dt,
                                   "stage": "compose"})

        # --- 3. send (fresh snapshot; single attempt, never retried) ---
        # Cancellation is re-checked here: a cancel during compose must
        # stop BEFORE the send tap (nothing has been sent yet).
        if cancelled():
            return done("cancelled", {"status": "cancelled"})
        insp3 = inspect()
        if not self._ok(insp3):
            st, dt = self._classify(insp3)
            # Nothing sent yet; not recorded in the ledger -> safe to stop.
            return done("failed", {"status": st, "detail": dt,
                                   "stage": "send"})
        snap3 = insp3.get("result") or {}
        send = find_send_button(snap3)
        if send["status"] != "found":
            return done("failed",
                        {"status": "blocked",
                         "detail": f"send button unavailable: "
                                   f"{send['detail']}",
                         "stage": "send"})
        pkg3 = package or _package(snap3)
        send_out = self._execute_step(
            "gui.tap",
            {"target_package": pkg3, "node_id": send["button"]["node_id"],
             "expected_snapshot_id": _snapshot_id(snap3),
             "expected_package": pkg3},
            device_id, who, approver, approval_id, mission_id, executions)
        if not self._ok(send_out):
            st, dt = self._classify(send_out)
            # The send tap was NOT applied: nothing was sent, so the
            # ledger stays clean (a fresh attempt is safe).
            return done("failed", {"status": st, "detail": dt,
                                   "stage": "send"})
        # The send tap WAS applied: this is a potentially-successful send.
        # Record it BEFORE verifying so a crash/restart cannot resend it.
        self.ledger.record(content_fp)

        # --- 4. verify (fresh snapshot; evidence, never assumption) ---
        insp4 = inspect()
        if not self._ok(insp4):
            return done("uncertain",
                        {"status": "uncertain",
                         "detail": "send was submitted but the outcome "
                                   "could not be verified (read failed); "
                                   "not resending",
                         "stage": "verify"})
        after = insp4.get("result") or {}
        ver = verify_send(snap3, after, text)
        if ver["status"] == "sent":
            return done("completed",
                        {"status": "succeeded",
                         "detail": f"message sent to {conversation}; "
                                   f"verified: {ver['detail']}",
                         "verification": ver})
        return done("uncertain",
                    {"status": "uncertain",
                     "detail": f"send submitted but unverified "
                               f"({ver['detail']}); not resending",
                     "verification": ver})
