"""Canonical scheduler tests: goldens, delegation equivalence,
determinism, explanation writer, non-authority.

Goldens below were captured from the PRE-migration implementations
(resolver._rank_key, fabric._rank, Router.route, DeviceManager.select
on a 5-device fleet: m1/linux/88, a1/android/5, c1/cloud/None,
m2/linux/88, zz/android/100). New code must match byte-for-byte;
divergence is a migration bug, never a golden update.
"""
import json

import pytest

from core import scheduler as S
from core.app import build_stack
from core.models import DeviceKind, DeviceState

# ---------- golden vectors (pre-migration capture) ----------

GOLD_FABRIC_RANK = {
    "m1": (1, 0, -88.0),
    "a1": (2, 0, -5.0),
    "c1": (0, 0, 1.0),
    "m2": (1, 0, -88.0),
    "zz": (2, 0, -100.0),
}
GOLD_RESOLVER_ORDER = ["c1", "m1", "m2", "zz", "a1"]
GOLD_ROUTE_WINNER = "c1"
GOLD_ROUTER_WINNER = "c1"
GOLD_SELECT_WINNER = "c1"


def make_fleet():
    stack = build_stack()
    for dev, kind, batt in [("m1", "linux", 88.0), ("a1", "android", 5.0),
                            ("c1", "cloud", None), ("m2", "linux", 88.0),
                            ("zz", "android", 100.0)]:
        code = stack["device_auth"].enroll(dev, DeviceKind(kind))
        stack["device_auth"].claim(code)
        stack["devices"].register(DeviceState(
            device_id=dev, kind=DeviceKind(kind),
            capabilities=["system.battery"], online=True, status="online"))
        if batt is not None:
            stack["devices"].heartbeat(dev, battery_pct=batt)
        stack["fabric"].advertise([
            {"id": "system.battery", "name": "system.battery",
             "descriptor_version": "1", "version": "1.0.0", "risk": "safe",
             "availability": "available"}], dev)
    return stack


# ---------- goldens ----------

def test_S01_fabric_rank_golden():
    stack = make_fleet()
    for dev, key in GOLD_FABRIC_RANK.items():
        v = stack["fabric"].device_view(dev)
        assert stack["fabric"]._rank(v) == key, dev
        assert S.fabric_rank_key(v.device_type, v.presence,
                                 v.battery_pct) == key, dev


def test_S02_resolver_order_golden():
    stack = make_fleet()
    r = stack["resolver"].resolve("system.battery", "")
    assert [c.device_id for c in r.candidates] == GOLD_RESOLVER_ORDER
    assert r.device_id == GOLD_ROUTE_WINNER


def test_S03_route_router_select_golden():
    stack = make_fleet()
    assert stack["fabric"].route_capability(
        "system.battery", "user").device_id == GOLD_ROUTE_WINNER
    assert stack["router"].route(
        "system.battery", "user").device_id == GOLD_ROUTER_WINNER
    assert stack["devices"].select(["system.battery"]).device_id == \
        GOLD_SELECT_WINNER


# ---------- delegation equivalence ----------

def test_S04_canonical_keys_match_legacy_shapes():
    # Spot-check key assembly against hand-computed legacy values.
    assert S.resolver_rank_key(True, True, False, False, "trusted",
                               "online", "linux", 88.0, (1, 0, 0),
                               "m1") == \
        (False, False, True, True, 0, 0, 1, -88.0, (-1, 0, 0), "m1")
    assert S.fabric_rank_key("android", "online", 5.0) == (2, 0, -5.0)
    assert S.fabric_rank_key("cloud", "stale", None) == (0, 1, 1.0)
    assert S.kind_device_key("linux", "m1") == (1, "m1")
    assert S.kind_device_key("unknown-kind", "x") == (9, "x")


def test_S05_tiebreak_device_id_asc():
    a = S.resolver_rank_key(True, True, False, False, "trusted", "online",
                            "linux", 50.0, (1,), "b-dev")
    b = S.resolver_rank_key(True, True, False, False, "trusted", "online",
                            "linux", 50.0, (1,), "a-dev")
    assert sorted([a, b]) == [b, a]


def test_S06_input_permutation_stable():
    stack = make_fleet()
    r1 = stack["resolver"].resolve("system.battery", "")
    # Re-advertise in reverse order on a fresh stack: same winner.
    stack2 = build_stack()
    for dev, kind, batt in [("zz", "android", 100.0), ("m2", "linux", 88.0),
                            ("a1", "android", 5.0), ("m1", "linux", 88.0),
                            ("c1", "cloud", None)]:
        code = stack2["device_auth"].enroll(dev, DeviceKind(kind))
        stack2["device_auth"].claim(code)
        stack2["devices"].register(DeviceState(
            device_id=dev, kind=DeviceKind(kind),
            capabilities=["system.battery"], online=True, status="online"))
        if batt is not None:
            stack2["devices"].heartbeat(dev, battery_pct=batt)
        stack2["fabric"].advertise([
            {"id": "system.battery", "name": "system.battery",
             "descriptor_version": "1", "version": "1.0.0", "risk": "safe",
             "availability": "available"}], dev)
    r2 = stack2["resolver"].resolve("system.battery", "")
    assert [c.device_id for c in r2.candidates] == GOLD_RESOLVER_ORDER


def test_S07_order_candidates_no_mutation():
    items = [{"device_id": "b"}, {"device_id": "a"}]
    out = S.order_candidates(items, key_fn=lambda d: d["device_id"])
    assert [d["device_id"] for d in out] == ["a", "b"]
    assert [d["device_id"] for d in items] == ["b", "a"]


# ---------- explanation writer ----------

def test_S08_explain_deterministic_bounded():
    kw = {"request_id": "req-abc123", "capability": "system.battery",
          "constraints": {"risk_max": "safe"},
          "candidates": [
              {"device_id": "emu-lab", "eligible": True,
               "reason_code": "eligible", "detail": "ok"},
              {"device_id": "old-phone", "eligible": False,
               "reason_code": "presence-offline", "detail": "stale"}],
          "selected": "emu-lab",
          "tiebreak": {"decided_by": "device_id-asc", "tied_with": []},
          "telemetry": {"as_of": "2026-10-09T00:00:00+00:00",
                        "stale": False},
          "approval_required": False, "execution_id": "exec-abc123"}
    a = S.explain_decision(**kw)
    b = S.explain_decision(**kw)
    assert a == b
    assert a["schema"] == "zara.sched-explain/v1"
    assert list(a.keys()) == ["schema", "request_id", "capability",
                              "strategy", "constraints", "candidates",
                              "selected", "tiebreak", "telemetry",
                              "approval_required", "execution_ref",
                              "truncated"]
    assert len(json.dumps(a, sort_keys=True)) <= S.EXPLAIN_MAX_CHARS


def test_S09_explain_redacts_secrets_and_caps_size():
    rec = S.explain_decision(
        request_id="req-1", capability="system.battery",
        constraints={"api_key": "sk-secret", "risk_max": "safe"},
        candidates=[{"device_id": "d%d" % i, "eligible": True,
                     "reason_code": "eligible",
                     "detail": "token abcdef " * 50} for i in range(50)],
        selected="d0")
    blob = json.dumps(rec)
    assert "sk-secret" not in blob and "token abcdef" not in blob
    assert len(rec["candidates"]) <= S.EXPLAIN_MAX_CANDIDATES
    assert len(blob) <= S.EXPLAIN_MAX_CHARS or rec["truncated"] is True


def test_S10_explain_closed_codes_and_failure_safe():
    rec = S.explain_decision(candidates=[
        {"device_id": "d1", "eligible": True,
         "reason_code": "bogus-code", "detail": "x"}])
    assert rec["candidates"][0]["reason_code"] == "eligible"
    rec = S.explain_decision(request_id="!!!bad id!!!",
                             candidates="not-a-list")
    assert rec["selected"] is None
    assert rec["request_id"] == "req-unknown"


def test_S11_explain_never_exposes_fingerprint():
    rec = S.explain_decision(
        request_id="req-1", capability="x",
        execution_id="exec-abc123")
    blob = json.dumps(rec)
    assert "fingerprint" not in blob and "grant" not in blob


# ---------- non-authority ----------

def test_S12_scheduler_imports_nothing_authoritative():
    import inspect
    src = inspect.getsource(S)
    # Ranking/explanation section only (the legacy Scheduler timer class
    # above is pre-existing Stage-1 code, untouched).
    sec = src[src.find("Canonical scheduler primitives"):]
    for banned in ("PolicyEngine", "PolicyDecision(", ".grant(",
                   "registry.call", "engine.submit", "engine.approve",
                   "device_auth", "jobs.enqueue", "FabricStore",
                   "trust_of", "authorize_capability", "policy.decide",
                   "governor.check", "import threading", "import socket",
                   "import subprocess",
                   "open("):
        assert banned not in sec, banned


def test_S13_ranking_cannot_authorize_revoked():
    stack = make_fleet()
    stack["fabric"].revoke_device("c1", "test")
    r = stack["resolver"].resolve("system.battery", "")
    assert r.device_id != "c1"
    # Canonical key of a revoked candidate still sorts, but resolve
    # never selects it: ranking ≠ authorization.
    k = S.resolver_rank_key(False, True, False, False, "revoked", "online",
                            "cloud", 100.0, (1,), "c1")
    assert k[0] is True  # not-authorized sorts after authorized
