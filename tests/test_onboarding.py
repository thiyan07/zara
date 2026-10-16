"""End-to-end onboarding UX flows: enroll→claim→register→discover→revoke.

Covers the user-facing lifecycle the fleet panel and agent depend on:
discovery freshness, revocation end-state, re-pair repair, expiry,
duplicate registration, wrong-device isolation, and the Linux agent's
stop-on-revoked behavior. Server machinery itself is covered elsewhere;
these tests pin the workflows.
"""
import threading

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app

OP = {"Authorization": "Bearer dev-token"}


def make_client():
    stack = build_stack(fabric_db="")
    return TestClient(create_app(stack)), stack


def dhev(dev, key):
    return {"X-Device-Id": dev, "X-Device-Key": key}


def onboard(client, dev="laptop-1", caps=None):
    r = client.post("/v1/agent/enroll",
                    json={"device_id": dev, "kind": "linux"}, headers=OP)
    assert r.status_code == 200, r.text
    code = r.json()["pairing_code"]
    r = client.post("/v1/agent/claim", json={"pairing_code": code})
    assert r.status_code == 200, r.text
    key = r.json()["device_key"]
    h = dhev(dev, key)
    r = client.post("/v1/agent/register",
                    json={"capabilities": caps or ["system.battery"],
                          "software_version": "t",
                          "kind": "linux"}, headers=h)
    assert r.status_code == 200, r.text
    r = client.post("/v1/agent/heartbeat", json={}, headers=h)
    assert r.status_code == 200, r.text
    return key, h


def test_onboard_then_discover_fresh():
    client, stack = make_client()
    key, h = onboard(client, caps=["system.battery", "desktop.session"])
    r = client.get("/v1/fabric/devices", headers=OP)
    assert r.status_code == 200
    devs = {d["device_id"]: d for d in r.json()}
    assert "laptop-1" in devs
    assert devs["laptop-1"]["presence"] in ("online", "unknown")
    r = client.get("/v1/fabric/devices/laptop-1/capabilities", headers=OP)
    assert r.status_code == 200


def test_revoke_blocks_poll_heartbeat_result():
    client, stack = make_client()
    key, h = onboard(client)
    stack["jobs"].enqueue("laptop-1", "system.battery", {})
    r = client.post("/v1/agent/revoke?device_id=laptop-1", headers=OP)
    assert r.status_code == 200
    assert client.post("/v1/agent/jobs/poll", headers=h).status_code == 403
    assert client.post("/v1/agent/heartbeat", json={},
                       headers=h).status_code == 403
    # Pending job swept; nothing claimable anymore.
    assert stack["jobs"].poll("laptop-1") is None


def test_revoke_then_reclaim_repairs_with_new_key():
    client, stack = make_client()
    old_key, h = onboard(client)
    client.post("/v1/agent/revoke?device_id=laptop-1", headers=OP)
    r = client.post("/v1/agent/enroll",
                    json={"device_id": "laptop-1", "kind": "linux"},
                    headers=OP)
    assert r.status_code == 200, r.text
    r = client.post("/v1/agent/claim",
                    json={"pairing_code": r.json()["pairing_code"]})
    assert r.status_code == 200, r.text
    new_key = r.json()["device_key"]
    assert new_key != old_key
    # Old key stays dead; new key works end to end.
    assert client.post("/v1/agent/heartbeat", json={},
                       headers=dhev("laptop-1", old_key)).status_code == 403
    r = client.post("/v1/agent/heartbeat", json={},
                    headers=dhev("laptop-1", new_key))
    assert r.status_code in (200, 404)  # 404 until re-registered
    r = client.post("/v1/agent/register",
                    json={"capabilities": ["system.battery"],
                          "software_version": "t",
                          "kind": "linux"},
                    headers=dhev("laptop-1", new_key))
    assert r.status_code == 200
    assert client.post("/v1/agent/heartbeat", json={},
                       headers=dhev("laptop-1", new_key)).status_code == 200


def test_expired_pairing_code_refused():
    client, stack = make_client()
    r = client.post("/v1/agent/enroll",
                    json={"device_id": "laptop-9", "kind": "linux"},
                    headers=OP)
    code = r.json()["pairing_code"]
    stack["device_auth"]._creds["laptop-9"].expires_at = \
        "2000-01-01T00:00:00"
    r = client.post("/v1/agent/claim", json={"pairing_code": code})
    assert r.status_code == 403


def test_duplicate_registration_updates_not_duplicates():
    client, stack = make_client()
    key, h = onboard(client, caps=["system.battery"])
    r = client.post("/v1/agent/register",
                    json={"capabilities": ["system.network"],
                          "software_version": "t2",
                          "kind": "linux"}, headers=h)
    assert r.status_code == 200
    r = client.get("/v1/devices", headers=OP)
    mine = [d for d in r.json() if d["device_id"] == "laptop-1"]
    assert len(mine) == 1
    assert mine[0]["capabilities"] == ["system.network"]


def test_wrong_device_poll_isolation():
    client, stack = make_client()
    key_a, _ = onboard(client, dev="dev-a")
    key_b, _ = onboard(client, dev="dev-b")
    stack["jobs"].enqueue("dev-a", "system.battery", {})
    r = client.post("/v1/agent/jobs/poll", headers=dhev("dev-b", key_b))
    assert r.status_code == 200
    assert r.json()["job"] is None
    r = client.post("/v1/agent/jobs/poll", headers=dhev("dev-a", key_a))
    assert r.json()["job"] is not None


def test_agent_stops_after_consecutive_403(monkeypatch, tmp_path):
    import device.linux.agent as ag
    keyf = tmp_path / "d.key"
    keyf.write_text("dead-key")
    agent = ag.DeviceAgent("http://127.0.0.1:9", "laptop-1",
                           key_file=str(keyf))
    monkeypatch.setattr(agent, "register", lambda: {"registered": "x"})
    monkeypatch.setattr(
        agent, "heartbeat",
        lambda: (_ for _ in ()).throw(
            RuntimeError("/v1/agent/heartbeat: HTTP 403 revoked")))
    monkeypatch.setattr(agent, "run_one_job", lambda: False)
    monkeypatch.setattr(ag, "POLL_IDLE_S", 0.05)
    monkeypatch.setattr(ag, "HEARTBEAT_S", 0.05)
    monkeypatch.setattr(ag, "POLL_BACKOFF_MAX_S", 0.1)
    disconnected = []
    monkeypatch.setattr(agent, "disconnect", lambda: disconnected.append(1))
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        agent.serve_forever()
    assert agent._stop.is_set()
    assert "REVOKED" in buf.getvalue()
    assert "--claim" in buf.getvalue()
    assert disconnected == []  # no point calling home while revoked


def test_agent_retries_transient_errors(monkeypatch, tmp_path):
    import device.linux.agent as ag
    keyf = tmp_path / "d.key"
    keyf.write_text("k")
    agent = ag.DeviceAgent("http://127.0.0.1:9", "laptop-1",
                           key_file=str(keyf))
    calls = {"hb": 0, "jobs": 0}
    monkeypatch.setattr(agent, "register", lambda: {"registered": "x"})

    def hb():
        calls["hb"] += 1
        if calls["hb"] < 3:
            raise OSError("down")
        return {"ok": True}

    def one():
        calls["jobs"] += 1
        if calls["jobs"] >= 2:
            agent._stop.set()
        return False

    monkeypatch.setattr(agent, "heartbeat", hb)
    monkeypatch.setattr(agent, "run_one_job", one)
    monkeypatch.setattr(ag, "POLL_IDLE_S", 0.05)
    monkeypatch.setattr(ag, "HEARTBEAT_S", 0.0)
    monkeypatch.setattr(ag, "POLL_BACKOFF_MAX_S", 0.1)
    disconnected = []
    monkeypatch.setattr(agent, "disconnect", lambda: disconnected.append(1))
    agent.serve_forever()
    assert calls["hb"] >= 3  # retried through the transient outage
    assert disconnected == [1]  # normal exit still disconnects


def test_revoked_target_rejected_fast_at_submit():
    client, stack = make_client()
    key, h = onboard(client)
    client.post("/v1/agent/revoke?device_id=laptop-1", headers=OP)
    r = client.post("/v1/executions",
                    json={"tool": "system.battery", "inputs": {},
                          "device_id": "laptop-1"}, headers=OP)
    assert r.status_code == 409, r.text
    assert "revoked" in r.json()["detail"]
    # Unknown devices keep fail-open behavior (no trust record).
    r = client.post("/v1/executions",
                    json={"tool": "system.battery", "inputs": {},
                          "device_id": "never-seen"}, headers=OP)
    assert r.status_code in (201, 202), r.text


def test_revoked_target_rejected_fast_on_sync_exec():
    client, stack = make_client()
    key, h = onboard(client)
    client.post("/v1/agent/revoke?device_id=laptop-1", headers=OP)
    r = client.post("/v1/exec",
                    json={"tool": "system.battery", "inputs": {},
                          "device_id": "laptop-1"}, headers=OP)
    assert r.status_code == 409, r.text
    assert "revoked" in r.json()["detail"]
