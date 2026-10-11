"""HTTP-burst retention lock-in test (Task 11 follow-up).

Why this file exists: Task 11's live "prune failure" was traced to probe
methodology, not product code — sub-threshold totals (rate limiter caps
single-window bursts below the lazy threshold), unlogged 429s on the very
submits meant to cross it, and in-flight (RUNNING) records correctly
skipped mid-burst with no post-quiescence trigger afterwards. Nothing in
the product was broken, so there is no before/after fix pair. This test
locks in the CORRECT live-proven behavior through the real HTTP path
(TestClient runs the full app stack incl. middleware): synchronous
submits complete inline (zero in-flight ambiguity), small parametric caps
keep it fast, and it asserts eviction of the oldest terminal record plus
survival of a parked (active) approval across threshold crossings.

Contract pinned: terminal-only eviction, newest-kept, actives (incl.
WAITING_FOR_PERMISSION) never reaped, evicted ids read as unknown 404.
"""
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.models import DeviceKind, DeviceState, ExecutionState

OP = {"Authorization": "Bearer dev-token"}
ECHO = "util.echo"
INSPECT = "gui.screen.inspect"


def make_stack(small_cap=5):
    stack = build_stack(fabric_db="")
    # Parametric caps exercise the same lazy-trigger code path as the
    # 500/24 h defaults, deterministically and fast.
    stack["engine"]._max_terminal = small_cap
    stack["jobs"]._max_terminal = small_cap
    return stack


def paired_android(stack, dev="emu-lab"):
    code = stack["device_auth"].enroll(dev, DeviceKind("android"))
    stack["device_auth"].claim(code)
    stack["devices"].register(DeviceState(
        device_id=dev, kind=DeviceKind("android"),
        capabilities=[INSPECT], online=True, status="online"))
    stack["fabric"].advertise([
        {"id": INSPECT, "name": INSPECT, "descriptor_version": "1",
         "version": "1.0.0", "risk": "confirm", "availability": "available"},
    ], dev)
    return dev


def test_H01_http_burst_evicts_oldest_terminal_keeps_parked():
    stack = make_stack(small_cap=5)
    client = TestClient(create_app(stack))
    dev = paired_android(stack)
    # Parked approval = active work that retention must never reap.
    r = client.post("/v1/executions",
                    json={"tool": INSPECT, "inputs": {}, "device_id": dev},
                    headers=OP)
    assert r.status_code == 202, r.text
    parked_id = r.json()["id"]
    # Anchor + enough sync submits to cross 2*cap twice over. Sync
    # submits complete inline, so every record is terminal at every
    # trigger — no in-flight ambiguity, fully deterministic.
    r = client.post("/v1/exec",
                    json={"tool": ECHO, "inputs": {"text": "anchor"},
                          "device_id": "cloud"}, headers=OP)
    assert r.status_code == 200, r.text
    anchor_id = r.json()["id"]
    for i in range(12):
        r = client.post("/v1/exec",
                        json={"tool": ECHO, "inputs": {"text": f"f{i}"},
                              "device_id": "cloud"}, headers=OP)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "succeeded"
    # Oldest terminal evicted, newest retained, parked approval survives.
    assert client.get(f"/v1/executions/{anchor_id}",
                      headers=OP).status_code == 404
    assert stack["engine"].get(parked_id).state == \
        ExecutionState.WAITING_FOR_PERMISSION
    r = client.get(f"/v1/executions/{parked_id}", headers=OP)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "waiting_for_permission"
    # Bounded tail: terminal records retained at most cap + slack.
    assert len(stack["engine"]._records) <= 2 * 5 + 2


def test_H02_evicted_then_resubmitted_id_never_reused():
    stack = make_stack(small_cap=3)
    client = TestClient(create_app(stack))
    seen = set()
    for i in range(10):
        r = client.post("/v1/exec",
                        json={"tool": ECHO, "inputs": {"text": f"g{i}"},
                              "device_id": "cloud"}, headers=OP)
        assert r.status_code == 200, r.text
        assert r.json()["id"] not in seen  # ids never re-issued
        seen.add(r.json()["id"])
    assert len(seen) == 10
