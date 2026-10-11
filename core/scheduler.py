"""Scheduler — foundation for scheduled + event-driven jobs."""
from __future__ import annotations
import threading
import time
import uuid

class Scheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._fired: list[dict] = []

    def schedule_once(self, name: str, delay_s: float, payload: dict | None = None) -> str:
        jid = f"job-{uuid.uuid4().hex[:12]}"
        job = {"id": jid, "name": name, "run_at": time.time() + delay_s,
               "payload": payload or {}, "cancelled": False}
        with self._lock:
            self._jobs[jid] = job
        t = threading.Timer(delay_s, self._fire, args=(jid,))
        t.daemon = True
        job["timer"] = t
        t.start()
        return jid

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job["cancelled"] = True
            timer = job.get("timer")
        if timer:
            timer.cancel()
        return True

    def _fire(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["cancelled"]:
                return
            self._fired.append(job)

    def fired(self) -> list[dict]:
        with self._lock:
            return list(self._fired)

    def pending(self) -> list[dict]:
        with self._lock:
            return [j for j in self._jobs.values() if not j["cancelled"]
                    and j not in self._fired]


"""Canonical scheduler primitives — ranking + explanation (pure only).

This module owns ALL ranking logic. Legacy entry points delegate here;
no independent copies of ranking expressions may exist elsewhere.

HARD RULES (lint-level, see tests/test_scheduler.py):
- No imports of policy/engine/registry/jobs/auth/store modules.
- No I/O, no locks, no threads, no network, no disk, no subprocess.
- No weights, scores, learned ranking, or inferred preferences.
- Ranking never authorizes/approves/grants/revokes/dispatches.
- Pure functions only: same inputs → same outputs, no mutation.

Ranking strategies (named, explicit):
- "lexicographic" — the promoted CapabilityResolver._rank_key order.
- "fabric-kind" — FabricRegistry._rank order (+ device_id tiebreak).
- "kind-device" — Router.route / DeviceManager.select order.
"""

import json
import re

STRATEGY_LEXICOGRAPHIC = "lexicographic"
STRATEGY_FABRIC_KIND = "fabric-kind"
STRATEGY_KIND_DEVICE = "kind-device"
SCHEDULER_VERSION = "v1"

KIND_ORDER = {"cloud": 0, "linux": 1, "android": 2}
TRUST_RANK = {"trusted": 0, "temporarily_trusted": 1, "restricted": 2,
              "pairing": 3, "discovered": 4, "unknown": 5, "revoked": 6}
PRESENCE_RANK = {"online": 0, "degraded": 1, "reconnecting": 2, "stale": 3,
                 "offline": 4, "unknown": 5}

# Explanation budgets (mirror audit/redact caps elsewhere).
EXPLAIN_MAX_CHARS = 2000
EXPLAIN_MAX_CANDIDATES = 32
EXPLAIN_MAX_TIED = 8
EXPLAIN_MAX_DETAIL = 200
EXPLAIN_SCHEMA = "zara.sched-explain/v1"

REASON_CODES = frozenset({
    "eligible", "trust-ineligible", "presence-offline", "presence-stale",
    "os-unusable", "policy-deny", "policy-requires-approval",
    "governor-defer", "constraint-mismatch", "revoked", "stale-snapshot",
    "unknown-telemetry-defer", "no-capability",
})

_SECRET_RE = re.compile(
    r"api[_-]?key|password|secret|token|bearer|grant|fingerprint|"
    r"device_key|pairing|passwd|credential|private[_-]?key",
    re.IGNORECASE)
_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_REQ_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def kind_rank(kind: str) -> int:
    return KIND_ORDER.get(kind, 9)


def resolver_rank_key(authorized: bool, available: bool,
                      pinned_match: bool, preferred_match: bool,
                      trust: str, presence: str, kind: str,
                      battery: float | None, version: tuple,
                      device_id: str,
                      kind_ranks: dict | None = None) -> tuple:
    """Canonical lexicographic key (promoted _rank_key order)."""
    try:
        batt = float(battery)
        if batt != batt:  # NaN
            batt = -1.0
    except (TypeError, ValueError):
        batt = -1.0
    kr = kind_ranks if kind_ranks is not None else KIND_ORDER
    return (not authorized, not available, not pinned_match,
            not preferred_match, TRUST_RANK.get(trust, 9),
            PRESENCE_RANK.get(presence, 9), kr.get(kind, 9), -batt,
            tuple(-p for p in version), device_id)


def fabric_rank_key(device_type: str, presence: str,
                    battery: float | None) -> tuple:
    """Canonical fabric-kind key (promoted _rank order). Tiebreak stays
    with the caller (device_id asc), exactly as today."""
    try:
        batt = float(battery)
        if batt != batt:
            batt = -1.0
    except (TypeError, ValueError):
        batt = -1.0
    return (kind_rank(device_type), 0 if presence == "online" else 1,
            -batt)


def kind_device_key(kind_value: str, device_id: str) -> tuple:
    """Canonical kind-device key (Router.route / DeviceManager.select)."""
    return (KIND_ORDER.get(kind_value, 9), device_id)


def order_candidates(items: list, key_fn) -> list:
    """Stable order by key_fn; never mutates inputs. Ties impossible to
    misorder: every canonical key ends with device_id asc."""
    return sorted(items, key=key_fn)


def _clean_str(value, limit: int) -> str:
    if not isinstance(value, str):
        value = str(value)
    value = _SECRET_RE.sub("[redacted]", value)
    return value[:limit]


def explain_decision(request_id: str = "", capability: str = "",
                     strategy: str = STRATEGY_LEXICOGRAPHIC,
                     constraints: dict | None = None,
                     candidates: list | None = None,
                     selected: str | None = None,
                     tiebreak: dict | None = None,
                     telemetry: dict | None = None,
                     approval_required: bool = False,
                     execution_id: str | None = None) -> dict:
    """Deterministic bounded explanation record. Pure: never raises,
    never authorizes, never mutates inputs. Fixed field order; closed
    reason codes; secret scan; size budget with truncation flag."""
    try:
        rid = request_id if isinstance(request_id, str) and \
            _REQ_RE.match(request_id) else "req-unknown"
        cands = []
        for c in (candidates or [])[:EXPLAIN_MAX_CANDIDATES]:
            if not isinstance(c, dict):
                continue
            did = c.get("device_id", "")
            did = did if isinstance(did, str) and _ID_RE.match(did) \
                else "unknown-device"
            code = c.get("reason_code", "eligible")
            code = code if code in REASON_CODES else "eligible"
            cands.append({
                "device_id": did,
                "eligible": bool(c.get("eligible", False)),
                "reason_code": code,
                "detail": _clean_str(c.get("detail", ""),
                                     EXPLAIN_MAX_DETAIL)})
        tb = tiebreak if isinstance(tiebreak, dict) else {}
        tied = [d for d in tb.get("tied_with", [])
                if isinstance(d, str)][:EXPLAIN_MAX_TIED]
        rec = {
            "schema": EXPLAIN_SCHEMA,
            "request_id": rid,
            "capability": _clean_str(capability, 64),
            "strategy": {"name": strategy, "version": SCHEDULER_VERSION},
            "constraints": {k: _clean_str(v, 64) for k, v in
                            (constraints or {}).items()
                            if isinstance(k, str)} if isinstance(
                                constraints, dict) else {},
            "candidates": cands,
            "selected": selected if isinstance(selected, str) and
            _ID_RE.match(selected) else None,
            "tiebreak": {"decided_by": _clean_str(
                tb.get("decided_by", "device_id-asc"), 64),
                "tied_with": tied},
            "telemetry": {"as_of": _clean_str(
                (telemetry or {}).get("as_of", ""), 64),
                "stale": bool((telemetry or {}).get("stale", False))},
            "approval_required": bool(approval_required),
            "execution_ref": {"execution_id": execution_id
                              if isinstance(execution_id, str) and
                              _ID_RE.match(execution_id) else None},
            "truncated": False,
        }
        blob = json.dumps(rec, sort_keys=True)
        if len(blob) > EXPLAIN_MAX_CHARS:
            rec["candidates"] = rec["candidates"][:8]
            rec["tiebreak"]["tied_with"] = []
            rec["truncated"] = True
            blob = json.dumps(rec, sort_keys=True)
            if len(blob) > EXPLAIN_MAX_CHARS:
                return {"schema": EXPLAIN_SCHEMA, "request_id": rid,
                        "capability": rec["capability"],
                        "candidates": [],
                        "selected": rec["selected"],
                        "error": "explain-unavailable",
                        "truncated": True}
        return rec
    except Exception:
        return {"schema": EXPLAIN_SCHEMA, "request_id": "req-unknown",
                "capability": "", "selected": None,
                "error": "explain-unavailable", "truncated": False}
