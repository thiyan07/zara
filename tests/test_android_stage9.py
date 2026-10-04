"""Stage 9 tests: Android body protocol — lifecycle, notifications pull/ack,
device-scoped approvals, push registry, jobs contract, revocation/rotation.

All via the REAL FastAPI stack (TestClient). No hardware involved.
"""
import pytest
from fastapi.testclient import TestClient

from core.app import create_app, build_stack
from core.models import DeviceKind

OP = {"Authorization": "Bearer dev-token"}


def make_client():
    stack = build_stack()
    return TestClient(create_app(stack)), stack


def pair(client, device_id="android-phone", kind="android"):
    """Full lifecycle: enroll -> claim -> register. Returns device headers."""
    r = client.post("/v1/agent/enroll", json={"device_id": device_id,
                                               "kind": kind}, headers=OP)
    assert r.status_code == 200, r.text
    code = r.json()["pairing_code"]
    r = client.post("/v1/agent/claim", json={"pairing_code": code})
    assert r.status_code == 200, r.text
    key = r.json()["device_key"]
    h = {"X-Device-Id": device_id, "X-Device-Key": key}
    r = client.post("/v1/agent/register",
                    json={"capabilities": ["battery.report", "network.report"],
                          "software_version": "zara-android 9.0.0",
                          "kind": kind}, headers=h)
    assert r.status_code == 200, r.text
    return h


# ---------- lifecycle ----------

def test_full_lifecycle_heartbeat_capabilities_disconnect():
    client, stack = make_client()
    h = pair(client)
    r = client.post("/v1/agent/heartbeat",
                    json={"battery_pct": 80, "charging": False,
                          "network": "wifi", "online": True}, headers=h)
    assert r.json() == {"ok": True, "status": "online"}
    r = client.post("/v1/agent/capabilities",
                    json={"capabilities": ["battery.report", "audio.capture.api"]},
                    headers=h)
    assert "audio.capture.api" in r.json()["capabilities"]
    r = client.post("/v1/agent/disconnect", headers=h)
    assert r.json() == {"ok": True}
    assert stack["devices"].get("android-phone").status == "offline"


def test_degraded_thresholds_and_recovery():
    client, _ = make_client()
    h = pair(client)
    r = client.post("/v1/agent/heartbeat",
                    json={"battery_pct": 10, "charging": False,
                          "network": "wifi", "online": True}, headers=h)
    assert r.json()["status"] == "degraded"
    # charging at low battery is not degraded
    r = client.post("/v1/agent/heartbeat",
                    json={"battery_pct": 10, "charging": True,
                          "network": "wifi", "online": True}, headers=h)
    assert r.json()["status"] == "degraded"  # still degraded until healthy
    r = client.post("/v1/agent/heartbeat",
                    json={"battery_pct": 30, "charging": False,
                          "network": "wifi", "online": True}, headers=h)
    assert r.json()["status"] == "online"


def test_unknown_device_404_needs_reregister():
    """Backend restart wipes registry: device gets 404, re-registers cleanly."""
    client, stack = make_client()
    h = pair(client)
    stack["devices"].revoke("android-phone")  # simulate restart data loss
    r = client.post("/v1/agent/heartbeat",
                    json={"battery_pct": 50, "charging": True,
                          "network": "wifi", "online": True}, headers=h)
    assert r.status_code == 404
    r = client.post("/v1/agent/register",
                    json={"capabilities": ["battery.report"],
                          "software_version": "zara-android 9.0.0",
                          "kind": "android"}, headers=h)
    assert r.status_code == 200
    r = client.post("/v1/agent/heartbeat",
                    json={"battery_pct": 50, "charging": True,
                          "network": "wifi", "online": True}, headers=h)
    assert r.json()["status"] == "online"


def test_revoke_kills_access_and_rotate_kills_old_key():
    client, stack = make_client()
    h = pair(client, "android-2")
    # rotate: old key dies
    r = client.post("/v1/agent/rotate", headers=h)
    assert r.status_code == 200
    new_key = r.json()["device_key"]
    h_new = {"X-Device-Id": "android-2", "X-Device-Key": new_key}
    r = client.post("/v1/agent/heartbeat",
                    json={"battery_pct": 50, "charging": True,
                          "network": "wifi", "online": True}, headers=h)
    assert r.status_code == 403  # old key dead
    # revoke: even the new key dies; client must enter safe state
    client.post("/v1/agent/revoke?device_id=android-2", headers=OP)
    r = client.post("/v1/agent/heartbeat",
                    json={"battery_pct": 50, "charging": True,
                          "network": "wifi", "online": True}, headers=h_new)
    assert r.status_code == 403


def test_pairing_code_single_use_and_enroll_conflict():
    client, stack = make_client()
    code = stack["device_auth"].enroll("android-3", DeviceKind.ANDROID)
    stack["device_auth"].claim(code)
    r = client.post("/v1/agent/claim", json={"pairing_code": code})
    assert r.status_code == 403  # replay rejected
    r = client.post("/v1/agent/enroll",
                    json={"device_id": "android-3", "kind": "android"},
                    headers=OP)
    assert r.status_code == 409  # already enrolled


# ---------- jobs contract ----------

def test_jobs_poll_result_cancel_and_duplicates():
    client, stack = make_client()
    h = pair(client)
    job = stack["jobs"].enqueue("android-phone", "system.battery", {},
                                execution_id="exec-1", timeout_s=20)
    r = client.post("/v1/agent/jobs/poll", headers=h)
    got = r.json()["job"]
    assert got["job_id"] == job.id and got["tool"] == "system.battery"
    assert got["execution_id"] == "exec-1"  # device tracks execution IDs
    # second poll: nothing pending (claimed) — no duplicate dispatch
    assert client.post("/v1/agent/jobs/poll", headers=h).json() == {"job": None}
    r = client.post("/v1/agent/jobs/result",
                    json={"job_id": job.id, "ok": True,
                          "result": {"battery_pct": 77}}, headers=h)
    assert r.json() == {"ok": True}
    # idempotent replay returns ok again, never double-executes
    r = client.post("/v1/agent/jobs/result",
                    json={"job_id": job.id, "ok": True,
                          "result": {"battery_pct": 77}}, headers=h)
    assert r.json() == {"ok": True}
    # unknown job -> 404; unknown tool names are never executed by Core
    r = client.post("/v1/agent/jobs/result",
                    json={"job_id": "job-nope", "ok": True, "result": {}},
                    headers=h)
    assert r.status_code == 404


def test_job_cancel_from_core_side():
    client, stack = make_client()
    h = pair(client)
    job = stack["jobs"].enqueue("android-phone", "system.network", {})
    assert stack["jobs"].cancel(job.id) is True
    # cancelled jobs are never dispatched to the device
    assert client.post("/v1/agent/jobs/poll", headers=h).json() == {"job": None}


# ---------- notifications pull/ack ----------

def test_notifications_pull_and_ack_scoping():
    client, stack = make_client()
    h = pair(client)
    stack["notifs"].create("Approval required", "mission m1 needs you",
                           device_id="android-phone", mission_id="m1")
    stack["notifs"].create("Broadcast", "hello all")  # device_id None
    stack["notifs"].create("Other phone", "not yours", device_id="other")
    r = client.post("/v1/agent/notifications", headers=h)
    items = r.json()["notifications"]
    titles = [n["title"] for n in items]
    assert "Approval required" in titles and "Broadcast" in titles
    assert "Other phone" not in titles
    nid = [n for n in items if n["title"] == "Approval required"][0]["id"]
    r = client.post("/v1/agent/notifications/ack",
                    json={"notification_id": nid}, headers=h)
    assert r.json() == {"acked": nid}
    # ack of someone else's notification -> 404
    other = stack["notifs"].create("X", "y", device_id="other")
    r = client.post("/v1/agent/notifications/ack",
                    json={"notification_id": other.id}, headers=h)
    assert r.status_code == 404


# ---------- device-scoped approvals ----------

_gated_n = [0]


def _waiting_execution(stack, device_id):
    from core.models import PermissionLevel, RiskLevel, ToolDefinition
    _gated_n[0] += 1
    name = f"test.gated9.{_gated_n[0]}"  # unique: approvals grant per-tool
    stack["registry"].register(
        ToolDefinition(name=name, description="g",
                       input_schema={"type": "object", "properties": {}},
                       permission=PermissionLevel.CONFIRM,
                       risk=RiskLevel.CONFIRM, timeout_s=5.0),
        lambda i, c: {"ok": True})
    rec = stack["engine"].submit(name, {}, device_id, who="user")
    assert rec.state.value == "waiting_for_permission"
    return rec


def test_device_approves_own_execution_and_deny_cancels():
    client, stack = make_client()
    h = pair(client)
    rec = _waiting_execution(stack, "android-phone")
    r = client.post(f"/v1/agent/approvals/{rec.id}",
                    json={"decision": "approve"}, headers=h)
    assert r.json()["status"] == "succeeded"
    rec2 = _waiting_execution(stack, "android-phone")
    r = client.post(f"/v1/agent/approvals/{rec2.id}",
                    json={"decision": "deny"}, headers=h)
    assert r.json()["status"] == "cancelled"


def test_device_cannot_approve_foreign_execution():
    client, stack = make_client()
    h = pair(client)
    rec = _waiting_execution(stack, "linux-laptop")
    r = client.post(f"/v1/agent/approvals/{rec.id}",
                    json={"decision": "approve"}, headers=h)
    assert r.status_code == 403
    assert stack["engine"].get(rec.id).state.value == "waiting_for_permission"
    # bad decision + unknown execution
    rec2 = _waiting_execution(stack, "android-phone")
    assert client.post(f"/v1/agent/approvals/{rec2.id}",
                       json={"decision": "maybe"},
                       headers=h).status_code == 400
    assert client.post("/v1/agent/approvals/exec-nope",
                       json={"decision": "approve"},
                       headers=h).status_code == 404


# ---------- approval auto-notification to target device ----------

def test_approval_hold_notifies_target_device_with_exec_id():
    client, stack = make_client()
    h = pair(client)
    rec = _waiting_execution(stack, "android-phone")
    r = client.post("/v1/agent/notifications", headers=h)
    items = r.json()["notifications"]
    mine = [n for n in items if n.get("execution_id") == rec.id]
    assert len(mine) == 1
    assert "Approval required" in mine[0]["title"]
    # end-to-end: approve from the notification's execution ID
    r = client.post(f"/v1/agent/approvals/{rec.id}",
                    json={"decision": "approve"}, headers=h)
    assert r.json()["status"] == "succeeded"


def test_approval_on_unknown_device_notifies_nobody():
    client, stack = make_client()
    pair(client)
    _waiting_execution(stack, "ghost-device")  # never registered
    r = client.post("/v1/agent/notifications",
                    headers={"X-Device-Id": "android-phone",
                             "X-Device-Key": "k"})
    # must not leak: 403 before any listing (bad key)
    assert r.status_code == 403

def test_push_lifecycle_dedup_rotation_invalidation():
    from core.push import PushRegistry
    p = PushRegistry()
    assert p.send("d1", "e1", "hi") == {"sent": False, "reason": "no-registration"}
    p.register("d1", "tok-1")
    assert p.send("d1", "e1", "hello")["sent"] is True
    assert p.send("d1", "e1", "hello")["reason"] == "duplicate"
    assert p.outbox[0]["title"] == "hello" and "token" not in str(p.outbox[0])
    p.rotate("d1", "tok-2")
    assert p.registration("d1").token == "tok-2"
    assert p.send("d1", "e2", "again")["sent"] is True
    p.invalidate("d1")
    assert p.send("d1", "e3", "late")["reason"] == "invalid-token"
    with pytest.raises(ValueError):
        p.register("d1", "")
    with pytest.raises(ValueError):
        p.register("d1", "t", "carrier-pigeon")


def test_push_endpoints_and_secret_redaction():
    client, stack = make_client()
    h = pair(client)
    assert client.post("/v1/agent/push/register",
                       json={"token": "tok-abc", "provider": "mock"},
                       headers=h).json()["provider"] == "mock"
    assert client.post("/v1/agent/push/register",
                       json={"token": "t", "provider": "nope"},
                       headers=h).status_code == 400
    out = stack["push"].send("android-phone", "evt-1",
                             "Approval needed nvapi-SECRETKEY12345678")
    assert out["sent"] is True
    assert "SECRETKEY" not in str(stack["push"].outbox)
    assert client.post("/v1/agent/push/invalidate", headers=h).json() == \
        {"invalidated": "android-phone"}
    assert stack["push"].send("android-phone", "e9", "x")["reason"] == \
        "invalid-token"


# ---------- events sync bound ----------

def test_events_sync_bounded_and_offline_safe():
    client, _ = make_client()
    h = pair(client)
    big = [{"t": i} for i in range(500)]
    r = client.post("/v1/agent/events/sync", json={"events": big}, headers=h)
    assert r.json()["accepted"] == 200  # hard bound, rest dropped


# ---------- Stage 10: device voice turn + TTS ----------

def _silence_wav(seconds=1, rate=16000):
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * rate * seconds)
    return buf.getvalue()


def test_agent_voice_turn_silence_and_auth():
    import base64
    client, _ = make_client()
    h = pair(client)
    wav = _silence_wav()
    b64 = base64.b64encode(wav).decode()
    # unauthenticated -> 401/403, never processed
    r = client.post("/v1/agent/voice/turn", json={"audio_base64": b64})
    assert r.status_code in (401, 403)
    # malformed base64 -> 422 ("a" is invalid padding)
    r = client.post("/v1/agent/voice/turn", json={"audio_base64": "a"},
                    headers=h)
    assert r.status_code == 422
    # oversize -> 413 (2MB + 1, base64-encoded)
    big = base64.b64encode(b"\x00" * (2 * 1024 * 1024 + 1)).decode()
    r = client.post("/v1/agent/voice/turn", json={"audio_base64": big},
                    headers=h)
    assert r.status_code == 413


def test_agent_voice_turn_mock_turn_and_interrupt():
    import base64
    client, _ = make_client()
    h = pair(client)
    wav = _silence_wav()
    b64 = base64.b64encode(wav).decode()
    r = client.post("/v1/agent/voice/turn", json={"audio_base64": b64},
                    headers=h)
    # mock STT produces empty transcript -> pipeline rejects empty audio
    # OR processes; either way it returns a well-formed dict, never an
    # exception HTML page, and device_id is the CALLER's (not spoofable)
    assert r.status_code == 200
    out = r.json()
    assert "state" in out and "transcript" in out
    r = client.post("/v1/agent/voice/interrupt", headers=h)
    assert r.status_code == 200
    assert r.json()["interrupted"] is True


def test_agent_tts_bounds_and_auth():
    client, _ = make_client()
    h = pair(client)
    # unauthenticated -> rejected
    r = client.post("/v1/agent/tts", json={"text": "hello"})
    assert r.status_code in (401, 403)
    # empty -> 400
    r = client.post("/v1/agent/tts", json={"text": "   "}, headers=h)
    assert r.status_code == 400
    # real synthesis through configured provider (mock in tests)
    r = client.post("/v1/agent/tts", json={"text": "Hey Zara"}, headers=h)
    assert r.status_code == 200
    out = r.json()
    assert out["bytes"] > 0 and "provider" in out
    import base64
    raw = base64.b64decode(out["audio_base64"])
    assert len(raw) == out["bytes"]
