"""Duplicate-submission guard: idempotency keys on async submit.

A replayed key returns the SAME execution without re-executing.
Malformed keys are 422; keys to reaped records start a new execution
honestly (never a dangling id).
"""
import time

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app

OP = {"Authorization": "Bearer dev-token"}


def _client():
    return TestClient(create_app(build_stack()))


def test_replayed_key_returns_same_execution():
    c = _client()
    body = {"tool": "util.echo", "inputs": {"text": "hi"},
            "device_id": "cloud", "idempotency_key": "dup-001"}
    r1 = c.post("/v1/executions", json=body, headers=OP)
    assert r1.status_code == 201, r1.text
    r2 = c.post("/v1/executions", json=body, headers=OP)
    assert r2.status_code == 201, r2.text
    assert r2.json()["id"] == r1.json()["id"]


def test_different_keys_create_different_executions():
    c = _client()
    ids = set()
    for k in ("k-a", "k-b"):
        r = c.post("/v1/executions",
                   json={"tool": "util.echo", "inputs": {"text": "x"},
                         "device_id": "cloud", "idempotency_key": k},
                   headers=OP)
        assert r.status_code == 201, r.text
        ids.add(r.json()["id"])
    assert len(ids) == 2


def test_no_key_no_dedupe():
    c = _client()
    body = {"tool": "util.echo", "inputs": {"text": "x"},
            "device_id": "cloud"}
    r1 = c.post("/v1/executions", json=body, headers=OP)
    r2 = c.post("/v1/executions", json=body, headers=OP)
    assert r1.json()["id"] != r2.json()["id"]


def test_malformed_key_is_422():
    c = _client()
    r = c.post("/v1/executions",
               json={"tool": "util.echo", "inputs": {},
                     "device_id": "cloud",
                     "idempotency_key": "bad key!! with spaces"},
               headers=OP)
    assert r.status_code == 422, r.text
    r = c.post("/v1/executions",
               json={"tool": "util.echo", "inputs": {},
                     "device_id": "cloud",
                     "idempotency_key": "x" * 129},
               headers=OP)
    assert r.status_code == 422, r.text


def test_replayed_key_does_not_reexecute():
    c = _client()
    body = {"tool": "util.echo", "inputs": {"text": "once"},
            "device_id": "cloud", "idempotency_key": "once-1"}
    r1 = c.post("/v1/executions", json=body, headers=OP)
    eid = r1.json()["id"]
    for _ in range(40):
        st = c.get(f"/v1/executions/{eid}", headers=OP).json()["status"]
        if st == "succeeded":
            break
        time.sleep(0.1)
    r2 = c.post("/v1/executions", json=body, headers=OP)
    assert r2.json()["id"] == eid
    assert r2.json()["status"] == "succeeded"


def test_engine_unit_replay_and_prune_sweep():
    from core.execution import ExecutionEngine
    from core.policy import PolicyEngine
    from core.tools import ToolRegistry
    from core import builtin_tools
    reg = ToolRegistry()
    for d, h in builtin_tools.BUILTINS:
        reg.register(d, h)
    eng = ExecutionEngine(reg, PolicyEngine(), max_terminal_records=2,
                          terminal_retention_s=3600)
    a = eng.submit_async_idem("util.echo", {"text": "a"}, "cloud",
                              idempotency_key="K")
    b = eng.submit_async_idem("util.echo", {"text": "DIFFERENT"}, "cloud",
                              idempotency_key="K")
    assert a.id == b.id
    assert b.inputs == {"text": "a"}  # original params win, honestly
    with pytest.raises(ValueError):
        eng.submit_async_idem("util.echo", {}, "cloud",
                              idempotency_key="no spaces!!")
