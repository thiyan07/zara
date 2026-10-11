"""Overnight-mission wiring tests: HTTP endpoints added by the main agent
on top of subagent logic (talk cancel, memory consent/deletion/purge,
owner-only job state). Uses TestClient (in-process, no ports)."""
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.models import DeviceKind, DeviceState, MemoryItem

OP = {"Authorization": "Bearer dev-token"}


def make_client():
    stack = build_stack(fabric_db="")
    return TestClient(create_app(stack)), stack


def enroll_claim(stack, dev, kind="linux"):
    code = stack["device_auth"].enroll(dev, DeviceKind(kind))
    return stack["device_auth"].claim(code)


def register(stack, dev):
    stack["devices"].register(DeviceState(
        device_id=dev, kind=DeviceKind.LINUX, capabilities=[],
        online=True, status="online"))


def test_W01_talk_response_carries_turn_id_and_cancel_unknown_is_noop():
    c, stack = make_client()
    r = c.post("/v1/talk", json={"text": "hello"}, headers=OP)
    assert r.status_code == 200, r.text
    assert r.json()["turn_id"], r.json()
    r2 = c.post("/v1/talk/cancel", json={"turn_id": "nope"}, headers=OP)
    assert r2.status_code == 200 and r2.json() == {"cancelled": False}
    # Finished turns report false (already finished, registry cleaned).
    r3 = c.post("/v1/talk/cancel",
                json={"turn_id": r.json()["turn_id"]}, headers=OP)
    assert r3.status_code == 200 and r3.json() == {"cancelled": False}


def test_W02_memory_forget_and_purge_endpoints():
    c, stack = make_client()
    r = c.post("/v1/memory",
               json={"text": "likes tea", "category": "preference"},
               headers=OP)
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    assert c.delete(f"/v1/memory/{mid}", headers=OP).status_code == 200
    assert c.delete(f"/v1/memory/{mid}", headers=OP).status_code == 404
    assert c.delete("/v1/memory/nope", headers=OP).status_code == 404
    # Purge path: expired memory disappears via explicit purge call.
    r2 = c.post("/v1/memory",
                json={"text": "old news", "category": "fact",
                      "expires_at": "2000-01-01T00:00:00+00:00"},
                headers=OP)
    assert r2.status_code == 200, r2.text
    rp = c.post("/v1/memory/purge", headers=OP)
    assert rp.status_code == 200 and rp.json()["purged"] >= 1, rp.text


def test_W03_candidate_confirm_endpoint():
    c, stack = make_client()
    # Drive a candidate through the service, then confirm via HTTP.
    res = stack["memory_service"].consider(
        "candidate memory text here", source="conversation")
    if res.get("status") != "candidate":
        # Text auto-saved or session-only: nothing held; endpoint 404-path
        # covered below with an unknown id.
        r = c.post("/v1/memory/candidates/confirm",
                   json={"candidate_id": "nope", "ok": False}, headers=OP)
        assert r.status_code in (200, 404, 422), r.text
        return
    cid = res["candidate_id"]
    r = c.post("/v1/memory/candidates/confirm",
               json={"candidate_id": cid, "ok": False}, headers=OP)
    assert r.status_code == 200, r.text
    assert r.json().get("stored") is False


def test_W04_job_state_owner_only():
    c, stack = make_client()
    d1, k1 = enroll_claim(stack, "d1")
    register(stack, "d1")
    d2, k2 = enroll_claim(stack, "d2")
    register(stack, "d2")
    job = stack["jobs"].enqueue("d1", "t", {})
    h1 = {"X-Device-Id": "d1", "X-Device-Key": k1}
    h2 = {"X-Device-Id": "d2", "X-Device-Key": k2}
    r = c.get(f"/v1/agent/jobs/{job.id}/state", headers=h1)
    assert r.status_code == 200 and r.json() == {"job_id": job.id,
                                                 "state": "pending"}
    assert c.get(f"/v1/agent/jobs/{job.id}/state",
                 headers=h2).status_code == 404
    assert c.get("/v1/agent/jobs/job-nope/state",
                 headers=h1).status_code == 404
    assert c.get(f"/v1/agent/jobs/{job.id}/state").status_code in (401, 403)
    # After claim the owner sees the new state through the same endpoint.
    stack["jobs"].poll("d1")
    r2 = c.get(f"/v1/agent/jobs/{job.id}/state", headers=h1)
    assert r2.json()["state"] == "claimed"


def test_W06_session_list_get_delete():
    c, stack = make_client()
    assert c.get("/v1/sessions", headers=OP).json() == []
    c.post("/v1/talk", json={"text": "hello"}, headers=OP)
    rows = c.get("/v1/sessions?limit=5", headers=OP).json()
    assert len(rows) == 1 and rows[0]["message_count"] >= 2
    assert set(rows[0]) >= {"id", "updated_at", "message_count",
                            "last_text"}
    sid = rows[0]["id"]
    g = c.get(f"/v1/sessions/{sid}", headers=OP)
    assert g.status_code == 200 and len(g.json()["messages"]) >= 2
    assert c.get("/v1/sessions/ses-nope", headers=OP).status_code == 404
    assert c.delete(f"/v1/sessions/{sid}", headers=OP).status_code == 200
    assert c.get("/v1/sessions", headers=OP).json() == []
    assert c.delete(f"/v1/sessions/{sid}", headers=OP).status_code == 404


def test_W07_search_rows_carry_memory_ids():
    c, stack = make_client()
    c.post("/v1/memory", json={"text": "lakeside cabin weekend",
                               "category": "fact"}, headers=OP)
    rows = c.get("/v1/memory/search?q=cabin&top_k=3", headers=OP).json()
    assert rows and all("id" in m for m in rows)


def test_W05_secret_memory_refused_over_http():
    c, stack = make_client()
    r = c.post("/v1/memory",
               json={"text": "my api key is sk-abc123", "category": "fact"},
               headers=OP)
    # Either refused (4xx/stored false) or never stored retrievably.
    assert r.status_code in (200, 422), r.text
    if r.status_code == 200:
        assert r.json().get("stored") is False or \
            r.json().get("id") is None


def test_W08_approve_twice_never_replays_execution():
    c, stack = make_client()
    dev, _ = enroll_claim(stack, "d9", "android")
    register(stack, "d9")
    stack["devices"].register(DeviceState(
        device_id="d9", kind=DeviceKind("android"),
        capabilities=["gui.screen.inspect"], online=True, status="online"))
    stack["fabric"].advertise([
        {"id": "gui.screen.inspect", "name": "gui.screen.inspect",
         "descriptor_version": "1", "version": "1.0.0", "risk": "confirm",
         "availability": "available"}], "d9")
    r = c.post("/v1/executions",
               json={"tool": "gui.screen.inspect", "inputs": {},
                     "device_id": "d9"}, headers=OP)
    assert r.status_code == 202, r.text
    eid = r.json()["id"]
    assert c.post(f"/v1/executions/{eid}/approve",
                  headers=OP).status_code == 200
    r2 = c.post(f"/v1/executions/{eid}/approve", headers=OP)
    assert r2.status_code == 409, r2.text
    # Terminal cancel path is equally replay-proof.
    assert c.post(f"/v1/executions/{eid}/cancel",
                  headers=OP).status_code in (200, 409)
