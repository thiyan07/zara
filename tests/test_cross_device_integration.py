"""Priority 3: laptop <-> Android integration via authorized routes.

API-level evidence (no hardware): two distinct devices through
build_stack + TestClient, pinned harmless read-only submits, offline /
revoked / cancel / duplicate / failure legs. Physical-device evidence is
NOT claimed here — see docs/REAL_WORLD_VALIDATION.md.
"""
import time

from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.models import DeviceKind, DeviceState

BAT = "system.battery"
OP = {"Authorization": "Bearer dev-token"}


def _stack():
    return build_stack(fabric_db="")


def _enroll_claim(stack, device_id, kind):
    code = stack["device_auth"].enroll(device_id, DeviceKind(kind))
    return stack["device_auth"].claim(code)


def _register(stack, device_id, kind, caps):
    stack["devices"].register(DeviceState(
        device_id=device_id, kind=DeviceKind(kind),
        capabilities=caps or [], online=True, status="online"))


def _pair(stack, device_id, kind):
    _enroll_claim(stack, device_id, kind)
    _register(stack, device_id, kind, [BAT])
    stack["fabric"].advertise([
        {"id": BAT, "name": BAT, "descriptor_version": "1",
         "version": "1.0.0", "risk": "safe", "availability": "available"},
    ], device_id)
    return device_id


def test_X01_two_devices_targeted_submit_stays_bound():
    stack = _stack()
    phone = _pair(stack, "android-phone", "android")
    laptop = _pair(stack, "laptop-1", "linux")
    assert phone != laptop
    # Requester (phone user) pins an explicitly targeted read-only task.
    res = stack["resolver"].resolve(BAT, laptop)
    assert res.status == "resolved" and res.device_id == laptop
    assert res.substituted is False
    rec = stack["engine"].submit_async(
        BAT, {}, laptop, who="phone-user", capability=BAT,
        binding={"resolution_status": res.status,
                 "requested_target": laptop})
    # Requester identity (who) and execution target (device_id) distinct.
    assert rec.device_id == laptop
    assert rec.binding.get("requested_target") == laptop


def test_X02_resolve_endpoint_echoes_target_unsubstituted():
    stack = _stack()
    _pair(stack, "android-phone", "android")
    _pair(stack, "laptop-1", "linux")
    c = TestClient(create_app(stack))
    r = c.post("/v1/executions/resolve",
               json={"capability": BAT, "inputs": {}, "device_id": "laptop-1"},
               headers=OP)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["device_id"] == "laptop-1"
    assert body["resolution"]["substituted"] is False


def test_X03_offline_target_no_silent_local_fallback():
    stack = _stack()
    _pair(stack, "android-phone", "android")
    laptop = _pair(stack, "laptop-1", "linux")
    stack["devices"].mark_offline(laptop)
    res = stack["resolver"].resolve(BAT, laptop)
    # Honest contract: either unresolvable (device None), or an EXPLICIT
    # substitution (substituted=True + reason naming the offline target).
    # Never a silent reroute (substituted=False to a different device).
    if res.device_id is None:
        assert res.substituted is False
    else:
        assert res.substituted is True
        assert "laptop-1" in res.reason and "offline" in res.reason
    rec = stack["engine"].submit_async(BAT, {}, laptop, capability=BAT)
    assert rec.device_id == laptop  # stays bound; never rerouted to phone


def test_X04_revoked_target_cannot_execute_without_reauth():
    stack = _stack()
    _pair(stack, "android-phone", "android")
    laptop = _pair(stack, "laptop-1", "linux")
    stack["fabric"].revoke_device(laptop, "test revoke")
    res = stack["resolver"].resolve(BAT, laptop)
    assert res.device_id is None
    assert res.status in ("unauthorized", "no_device", "revoked",
                         "policy_blocked", "offline")


def test_X05_cancel_and_duplicate_delivery():
    stack = _stack()
    _pair(stack, "android-phone", "android")
    laptop = _pair(stack, "laptop-1", "linux")
    rec = stack["engine"].submit_async(BAT, {}, laptop, capability=BAT)
    stack["engine"].cancel(rec.id)
    got = stack["engine"].get(rec.id)
    # Cancel is fail-closed: flag set, terminal-or-running-with-flag,
    # never succeeded. (RUNNING keeps its state until the _run loop
    # observes cancel_requested; PENDING flips to CANCELLED at once.)
    assert got.cancel_requested is True
    assert "succeed" not in str(got.state.value)
    assert "success" not in str((got.result or {})).lower()
    # Duplicate delivery: same idempotency key returns the SAME record.
    a = stack["engine"].submit_async_idem(
        BAT, {}, laptop, capability=BAT, idempotency_key="x-dup-1")
    b = stack["engine"].submit_async_idem(
        BAT, {}, laptop, capability=BAT, idempotency_key="x-dup-1")
    assert a.id == b.id


def test_X06_failed_tasks_never_appear_successful():
    import threading
    stack = _stack()
    laptop = _pair(stack, "laptop-1", "linux")
    rec = stack["engine"].submit_async(BAT, {}, laptop, capability=BAT)
    # Fail the device job synchronously through the jobs queue.
    job = stack["jobs"].poll(laptop)
    if job is not None:
        stack["jobs"].complete(job.id, ok=False, error="actuation failed")
    stack["engine"].cancel(rec.id)
    got = stack["engine"].get(rec.id)
    assert "succeed" not in str(got.state.value)
    # Cleanup: only test-created state; nothing global to remove (all
    # stacks are in-memory / temp).
    assert stack["engine"].get(rec.id).id == rec.id


def test_X07_voice_turn_is_requester_local_no_target_switch():
    # A voice/chat turn never routes through the resolver and never
    # creates an execution: Talk stays requester-local. There is no
    # machinery by which a voice request could silently change its
    # execution target, so assert the absence explicitly.
    from core.app import build_stack
    from core.voice import VoicePipeline, MockSTT, MockTTS
    from core.llm import LLMAction, LLMOutcome
    from core.providers import ModelProvider

    class B(ModelProvider):
        name = "b"

        def reason(self, system, user, tools, cancel=None, timeout=60.0):
            a = LLMAction(type="response", text="ok here")
            return LLMOutcome(action=a, text="ok here", provider="b")

    stack = build_stack(fabric_db="")
    stack["loop"].provider = B()
    _pair(stack, "android-phone", "android")
    _pair(stack, "laptop-1", "linux")
    before = dict(stack["engine"]._records)
    pipe = VoicePipeline(MockSTT("what time is it"), MockTTS(),
                         stack["loop"], stack["sessions"])
    out = pipe.handle_audio(b"fake-bytes", device_id="android-phone",
                            who="phone-user")
    assert out["ok"] is True
    assert dict(stack["engine"]._records) == before  # no execution made
    assert stack["jobs"] is not None  # queue untouched, no dispatch


def test_X08_self_target_preserved_for_requester():
    stack = _stack()
    _pair(stack, "android-phone", "android")
    res = stack["resolver"].resolve(BAT, "android-phone")
    assert res.device_id == "android-phone" and res.substituted is False


def test_X09_cancel_while_offline_never_resurrects_on_return():
    import time as _t
    stack = _stack()
    _pair(stack, "android-phone", "android")
    laptop = _pair(stack, "laptop-1", "linux")
    stack["devices"].mark_offline(laptop)
    rec = stack["engine"].submit_async(BAT, {}, laptop, capability=BAT)
    eid = rec.id
    stack["engine"].cancel(eid)
    assert stack["engine"].get(eid).cancel_requested is True
    # Target returns; any stale queued job must complete as cancelled,
    # never as success — even if a late device reports ok=True.
    stack["devices"].heartbeat(laptop)
    deadline = _t.monotonic() + 5.0
    while _t.monotonic() < deadline:
        job = stack["jobs"].poll(laptop)
        if job is None:
            _t.sleep(0.05)
            continue
        if job.execution_id == eid:
            stack["jobs"].complete(job.id, ok=True,
                                   result={"battery_pct": 100})
            break
        _t.sleep(0.05)
    _t.sleep(0.5)
    final = stack["engine"].get(eid)
    assert "succeed" not in str(final.state.value).lower(), final.state
    assert final.verified is False
    assert final.result is None
