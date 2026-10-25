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


# ---------- fire hook: observability trigger, zero authority ----------

def test_S20_fire_hook_called_once_with_payload():
    import time as _t
    from core import scheduler as _S
    seen = []
    s = _S.Scheduler(on_fire=lambda job: seen.append(dict(job)))
    jid = s.schedule_once("ping", 0.02, {"k": "v"})
    deadline = _t.time() + 5
    while not seen and _t.time() < deadline:
        _t.sleep(0.02)
    assert len(seen) == 1
    assert seen[0]["id"] == jid and seen[0]["payload"] == {"k": "v"}
    assert [f["id"] for f in s.fired()] == [jid]
    assert s.pending() == []


def test_S21_cancel_before_fire_suppresses_hook():
    import time as _t
    from core import scheduler as _S
    seen = []
    s = _S.Scheduler(on_fire=lambda job: seen.append(job))
    jid = s.schedule_once("never", 0.05)
    assert s.cancel(jid) is True
    _t.sleep(0.2)
    assert seen == [] and s.fired() == []


def test_S22_hook_exception_kills_nothing():
    import time as _t
    from core import scheduler as _S

    def bad(job):
        raise RuntimeError("hook blew up")

    s = _S.Scheduler(on_fire=bad)
    jid = s.schedule_once("x", 0.02)
    deadline = _t.time() + 5
    while not s.fired() and _t.time() < deadline:
        _t.sleep(0.02)
    assert [f["id"] for f in s.fired()] == [jid]


def test_S23_fired_ledger_bounded_and_pending_consistent():
    from core import scheduler as _S
    s = _S.Scheduler()
    for i in range(_S.Scheduler.FIRED_KEEP_MAX + 5):
        jid = f"j{i}"
        s._jobs[jid] = {"id": jid, "name": "n", "run_at": 0.0,
                        "payload": {}, "cancelled": False}
        s._fire(jid)
    assert len(s.fired()) == _S.Scheduler.FIRED_KEEP_MAX
    assert s.pending() == []


def test_S24_stack_scheduler_announces_and_audits():
    import time as _t
    from core.app import build_stack
    stack = build_stack(fabric_db="")
    heard = []
    stack["bus"].subscribe("scheduled_event", lambda ev: heard.append(ev))
    jid = stack["sched"].schedule_once("probe", 0.02)
    deadline = _t.time() + 5
    while not heard and _t.time() < deadline:
        _t.sleep(0.02)
    assert heard and heard[0].payload.get("job_id") == jid
    # the audit row is written AFTER the bus publish (by design); poll
    # for it instead of racing the write window
    rows = stack["audit"].query()
    deadline = _t.time() + 5
    while not any(r["actor"] == "scheduler" and r["action"] ==
                  "timer_fired" for r in rows) and _t.time() < deadline:
        _t.sleep(0.02)
        rows = stack["audit"].query()
    assert any(r["actor"] == "scheduler" and r["action"] == "timer_fired"
               for r in rows), [(r["actor"], r["action"]) for r in rows][-5:]


# ---------- persistence + restart recovery (SQLite, rollback-safe) ----------

def test_S30_future_job_survives_restart_and_fires_once():
    import time as _t
    from core import scheduler as _S
    import tempfile, os
    db = os.path.join(tempfile.mkdtemp(), "sched.db")
    seen = []
    s1 = _S.Scheduler(on_fire=lambda job: seen.append(job["id"]),
                      db_path=db)
    jid = s1.schedule_once("later", 0.3, {"n": 1})
    # Simulate process death before due: kill the live timer thread
    # without marking cancelled (del alone would let it fire).
    s1._jobs[jid]["timer"].cancel()
    del s1
    seen2 = []
    s2 = _S.Scheduler(on_fire=lambda job: seen2.append(job["id"]),
                      db_path=db)
    assert any(j["id"] == jid for j in s2.pending())
    deadline = _t.time() + 5
    while not seen2 and _t.time() < deadline:
        _t.sleep(0.02)
    assert seen2 == [jid]
    assert seen == []  # first instance never fired
    assert s2.pending() == []


def test_S31_due_on_recovery_fires_exactly_once():
    import time as _t
    from core import scheduler as _S
    import tempfile, os
    db = os.path.join(tempfile.mkdtemp(), "sched.db")
    s1 = _S.Scheduler(db_path=db)
    jid = s1.schedule_once("soon", 0.05)
    _t.sleep(0.2)  # let it fire in instance 1
    assert [f["id"] for f in s1.fired()] == [jid]
    del s1
    seen = []
    s2 = _S.Scheduler(on_fire=lambda job: seen.append(job["id"]),
                      db_path=db)
    _t.sleep(0.2)
    assert seen == []  # already fired: recovery must not re-announce
    assert s2.pending() == []


def test_S32_cancel_persists_across_restart():
    import time as _t
    from core import scheduler as _S
    import tempfile, os
    db = os.path.join(tempfile.mkdtemp(), "sched.db")
    s1 = _S.Scheduler(db_path=db)
    jid = s1.schedule_once("nope", 0.05)
    assert s1.cancel(jid) is True
    del s1
    seen = []
    s2 = _S.Scheduler(on_fire=lambda job: seen.append(job["id"]),
                      db_path=db)
    _t.sleep(0.2)
    assert seen == [] and s2.pending() == [] and s2.fired() == []


def test_S33_broken_db_path_raises_loudly():
    from core import scheduler as _S
    import tempfile, os
    bad = os.path.join(tempfile.mkdtemp(), "no-such-dir", "s.db")
    with pytest.raises(Exception):
        _S.Scheduler(db_path=bad)


def test_S34_firing_creates_no_execution_and_needs_no_approval():
    import time as _t
    from core.app import build_stack
    stack = build_stack(fabric_db="")
    jid = stack["sched"].schedule_once("probe2", 0.02)
    deadline = _t.time() + 5
    while not stack["sched"].fired() and _t.time() < deadline:
        _t.sleep(0.02)
    assert [f["id"] for f in stack["sched"].fired()] == [jid]
    # The hook announces; nothing was submitted, granted, or approved.
    assert stack["engine"]._records == {}
    assert stack["jobs"].pending() == [] if hasattr(
        stack["jobs"], "pending") else True


# ---------- scheduled dispatch: due timer -> authorized submit path ----------

def _wait_for(cond, deadline_s=5.0):
    import time as _t
    end = _t.time() + deadline_s
    while _t.time() < end:
        if cond():
            return True
        _t.sleep(0.02)
    return cond()


def test_S40_future_timer_does_not_fire_early():
    from core.app import build_stack
    stack = build_stack(fabric_db="")
    jid = stack["sched"].schedule_once(
        "future", 30.0, {"exec": {"tool": "system.battery",
                                  "device_id": "cloud", "inputs": {}}})
    import time as _t
    _t.sleep(0.3)
    assert stack["sched"].fired() == []
    assert stack["engine"]._records == {}
    assert stack["sched"].cancel(jid) is True


def test_S41_due_timer_submits_one_safe_execution():
    from core.app import build_stack
    stack = build_stack(fabric_db="")
    jid = stack["sched"].schedule_once(
        "poll-battery", 0.02, {"exec": {"tool": "system.battery",
                                        "device_id": "cloud", "inputs": {}}})
    # _sched_fire creates the engine record BEFORE writing the
    # dispatch_submitted audit row, so waiting on the record count can
    # observe the record before the audit write lands (thread preemption
    # under load). Wait on the evidence this test actually asserts.
    def _submitted():
        return any(r["actor"] == "scheduler" and
                   r["action"] == "dispatch_submitted"
                   for r in stack["audit"].query())
    assert _wait_for(_submitted)
    recs = list(stack["engine"]._records.values())
    assert len(recs) == 1
    assert recs[0].binding.get("scheduled_job_id") == jid
    rows = stack["audit"].query()
    assert any(r["actor"] == "scheduler" and r["action"] == "timer_fired"
               for r in rows)
    assert any(r["actor"] == "scheduler" and
               r["action"] == "dispatch_submitted" for r in rows)


def test_S42_repeated_fire_never_duplicates_submission():
    from core.app import build_stack
    stack = build_stack(fabric_db="")
    jid = stack["sched"].schedule_once(
        "once", 0.02, {"exec": {"tool": "system.battery",
                                "device_id": "cloud", "inputs": {}}})
    assert _wait_for(lambda: len(stack["engine"]._records) == 1)
    # Forced refire (poll race / restart re-announce): idempotency key
    # collapses into the same execution.
    stack["sched"]._fire(jid)
    stack["sched"]._fire(jid)
    import time as _t
    _t.sleep(0.2)
    assert len(stack["engine"]._records) == 1


def test_S43_confirm_risk_parks_for_approval_never_auto_runs():
    from core.app import build_stack
    from core.models import ExecutionState
    stack = build_stack(fabric_db="")
    # filesystem.write is CONFIRM-gated: the due timer must park it.
    jid = stack["sched"].schedule_once(
        "nightly-write", 0.02,
        {"exec": {"tool": "filesystem.write", "device_id": "cloud",
                  "inputs": {"path": "scratch/n.txt", "text": "hi"}}})
    assert _wait_for(lambda: len(stack["engine"]._records) == 1)
    rec = list(stack["engine"]._records.values())[0]
    assert rec.binding.get("scheduled_job_id") == jid
    assert rec.state == ExecutionState.WAITING_FOR_PERMISSION
    assert rec.result is None  # nothing executed without approval


def test_S44_revoked_target_never_reports_success():
    from core.app import build_stack
    from core.models import DeviceKind, DeviceState
    stack = build_stack(fabric_db="")
    code = stack["device_auth"].enroll("doomed", DeviceKind("linux"))
    stack["device_auth"].claim(code)
    stack["devices"].register(DeviceState(
        device_id="doomed", kind=DeviceKind("linux"),
        capabilities=["system.battery"], online=True, status="online"))
    stack["fabric"].revoke_device("doomed", "test revoke")
    jid = stack["sched"].schedule_once(
        "doomed-poll", 0.02, {"exec": {"tool": "system.battery",
                                       "device_id": "doomed", "inputs": {}}})
    assert _wait_for(lambda: stack["sched"].fired() != [])
    import time as _t
    _t.sleep(0.3)
    # Identical to a manual submit at a revoked device: the request is
    # audited, and any resulting record stays unverified with no result —
    # the revoked agent can never authenticate its poll. Success is
    # never reported.
    rows = stack["audit"].query()
    assert any(r["actor"] == "scheduler" and r["action"] in (
        "dispatch_submitted", "dispatch_rejected") for r in rows), \
        [(r["actor"], r["action"]) for r in rows][-8:]
    mine = [r for r in stack["engine"]._records.values()
            if r.binding.get("scheduled_job_id") == jid]
    for rec in mine:
        assert rec.state.value != "succeeded"
        assert rec.verified is False
        assert rec.result is None


def test_S45_cancel_prevents_future_submission():
    import time as _t
    from core.app import build_stack
    stack = build_stack(fabric_db="")
    jid = stack["sched"].schedule_once(
        "cancelled-job", 0.05, {"exec": {"tool": "system.battery",
                                         "device_id": "cloud", "inputs": {}}})
    assert stack["sched"].cancel(jid) is True
    _t.sleep(0.3)
    assert stack["engine"]._records == {}
    rows = stack["audit"].query()
    assert not any(r["actor"] == "scheduler" and
                   r["action"] == "dispatch_submitted" for r in rows)


def test_S46_unknown_tool_rejected_loudly_never_completed():
    from core.app import build_stack
    stack = build_stack(fabric_db="")
    jid = stack["sched"].schedule_once(
        "bad-tool", 0.02, {"exec": {"tool": "nope.not_a_tool",
                                    "device_id": "cloud", "inputs": {}}})
    assert _wait_for(lambda: stack["sched"].fired() != [])
    import time as _t
    _t.sleep(0.2)
    assert stack["engine"]._records == {}
    rows = stack["audit"].query()
    assert any(r["actor"] == "scheduler" and
               r["action"] == "dispatch_rejected" for r in rows)
    assert not any(r["actor"] == "scheduler" and
                   r["action"] == "dispatch_submitted" for r in rows)


def test_S47_restart_after_processed_exec_never_resubmits():
    import tempfile, os
    import time as _t
    from core import scheduler as _S
    from core.app import build_stack
    db = os.path.join(tempfile.mkdtemp(), "sched.db")
    stack = build_stack(fabric_db="")
    submitted = []

    def hook(job):
        req = (job.get("payload") or {}).get("exec")
        if isinstance(req, dict):
            rec = stack["engine"].submit_async_idem(
                req["tool"], {}, "cloud",
                who="scheduler", binding={"scheduled_job_id": job["id"]},
                idempotency_key=f"sched-{job['id']}")
            submitted.append(rec.id)

    s1 = _S.Scheduler(on_fire=hook, db_path=db)
    jid = s1.schedule_once("nightly", 0.05,
                           {"exec": {"tool": "system.battery"}})
    assert _wait_for(lambda: len(submitted) == 1)
    del s1
    # Restart: the fired flag persisted, so recovery must not re-announce
    # and must not submit a second execution.
    seen = []
    s2 = _S.Scheduler(on_fire=lambda job: seen.append(job["id"]), db_path=db)
    _t.sleep(0.3)
    assert seen == [] and s2.pending() == []
    assert len(submitted) == 1
