"""Stage 17 tests: deterministic resolver + safe escalation ladder.

Security battery (mission items 1-27) + consistency invariants, unit-level
via CapabilityResolver and API-level via the REAL FastAPI stack. The
resolver owns no authority: every executable verdict comes from the
existing authorize/route pipeline. No hardware; nothing physical.
"""
import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.models import DeviceKind, DeviceState, ToolDefinition

OP = {"Authorization": "Bearer dev-token"}


def make_stack(fabric_db=""):
    return build_stack(fabric_db=fabric_db)


def enroll_claim(stack, device_id, kind="linux"):
    code = stack["device_auth"].enroll(device_id, DeviceKind(kind))
    return stack["device_auth"].claim(code)


def register(stack, device_id, kind="linux", caps=None):
    stack["devices"].register(DeviceState(
        device_id=device_id, kind=DeviceKind(kind),
        capabilities=caps or [], online=True, status="online"))


def doc(cap="system.battery", **kw):
    d = {"id": cap, "name": cap, "descriptor_version": "1",
         "version": "1.0.0", "risk": "safe"}
    d.update(kw)
    return d


def resolver(stack=None):
    stack = stack or make_stack()
    from core.resolver import CapabilityResolver
    return CapabilityResolver(stack["fabric"]), stack


def pair_api(client, device_id="d1", kind="linux", caps=None):
    r = client.post("/v1/agent/enroll",
                    json={"device_id": device_id, "kind": kind}, headers=OP)
    assert r.status_code == 200, r.text
    r = client.post("/v1/agent/claim",
                    json={"pairing_code": r.json()["pairing_code"]})
    assert r.status_code == 200, r.text
    h = {"X-Device-Id": device_id,
         "X-Device-Key": r.json()["device_key"]}
    r = client.post("/v1/agent/register",
                    json={"capabilities": caps or [], "kind": kind,
                          "software_version": "t"}, headers=h)
    assert r.status_code == 200, r.text
    return h


# ---------- 1-2: unknown capability / device ----------

def test_01_nonexistent_capability():
    r, _ = resolver()
    out = r.resolve("no.such.thing")
    assert out.status == "no_capability" and out.device_id is None
    assert out.required_escalation == 5  # HUMAN: someone must define it


def test_02_nonexistent_device():
    r, stack = resolver()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    stack["fabric"].advertise([doc()], "d1")
    out = r.resolve("system.battery", "ghost-phone")
    assert out.status == "no_device" and out.device_id is None


# ---------- 3-6: trust / pairing / presence ----------

def test_03_revoked_device_never_executable():
    r, stack = resolver()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    stack["fabric"].advertise([doc()], "d1")
    stack["fabric"].revoke_device("d1")
    out = r.resolve("system.battery", "d1")
    assert out.device_id is None and out.status in (
        "unauthorized", "no_device")


def test_04_unpaired_device_no_candidates():
    r, stack = resolver()
    stack["fabric"].advertise([doc()], "stranger")
    out = r.resolve("system.battery")
    assert out.device_id is None
    assert all(c.device_id != "stranger" or not c.authorized
               for c in out.candidates)


def test_05_offline_device_supported_not_executable():
    r, stack = resolver()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    stack["fabric"].advertise([doc()], "d1")
    stack["devices"].mark_offline("d1")
    out = r.resolve("system.battery", "d1")
    assert out.status == "offline" and out.device_id is None
    # …but the device is still reported (existence ≠ reachability).
    assert out.candidates and out.candidates[0].device_id == "d1"


def test_06_stale_device_flagged_not_healthy():
    r, stack = resolver()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    stack["fabric"].advertise([doc()], "d1")
    stack["fabric"].mark_stale("d1")
    out = r.resolve("system.battery", "d1")
    # Stage 16 invariant: stale snapshots still route — but the verdict
    # must surface staleness explicitly (status + flag + reason).
    assert out.status == "resolved" and out.device_id == "d1"
    assert out.candidates and out.candidates[0].stale is True
    assert "stale" in out.reason


# ---------- 7-9: support / availability ----------

def test_07_unsupported_capability_on_pinned_device():
    r, stack = resolver()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    stack["fabric"].advertise([doc()], "d1")
    out = r.resolve("photo.read", "d1")
    assert out.status in ("no_capability", "no_device")


def test_08_unavailable_capability_not_executable():
    r, stack = resolver()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    stack["fabric"].advertise([doc(availability="unavailable",
                                   availability_reason="hardware missing")],
                              "d1")
    out = r.resolve("system.battery", "d1")
    assert out.status == "unavailable"
    assert out.candidates[0].available is False


def test_09_permission_required_preserves_reason():
    r, stack = resolver()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    stack["fabric"].advertise([doc(availability="os_denied",
                                   availability_reason="need RECORD_AUDIO",
                                   os_permission_granted=False)], "d1")
    out = r.resolve("system.battery", "d1")
    assert out.status == "unavailable"
    assert "RECORD_AUDIO" in out.candidates[0].availability_reason


# ---------- 10-12: governor / policy / approval ----------

def test_10_governor_blocked():
    r, stack = resolver()
    enroll_claim(stack, "low-phone", "android")
    register(stack, "low-phone", "android")
    d = stack["devices"].get("low-phone")
    d.battery_pct = 5.0
    d.charging = False
    stack["fabric"].advertise([doc(risk="high_risk")], "low-phone")
    out = r.resolve("system.battery", "low-phone")
    assert out.status == "governor_blocked", out.status


def test_11_policy_denied_or_approval_gated():
    r, stack = resolver()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    stack["fabric"].advertise(
        [doc(cap="files.delete", risk="high_risk")], "d1")
    out = r.resolve("files.delete", "d1")
    assert out.status in ("policy_blocked", "requires_approval",
                          "governor_blocked"), out.status
    # Either way it is NOT silently resolved.
    assert not (out.status == "resolved" and out.device_id == "d1")


def test_12_approval_required_transfer():
    r, stack = resolver()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    from core.capabilities import transfer_descriptor
    raw = transfer_descriptor()
    raw.pop("_limits", None)
    stack["fabric"].advertise([raw], "d1")
    out = r.resolve("files.transfer", "d1")
    assert out.status == "requires_approval", out.status
    assert out.candidates[0].approval_required is True
    # Resolve grants NOTHING: transfer still needs a live sender grant.
    assert out.candidates[0].escalation == 0  # NATIVE rung


# ---------- 13-17: hallucinated + fake proposal fields ----------

def test_13_hallucinated_capability():
    r, _ = resolver()
    out = r.resolve_proposal({"preferred_capability": "system.shell",
                              "preferred_device": "d1"})
    assert out.status == "no_capability"


def test_14_hallucinated_device():
    r, stack = resolver()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    stack["fabric"].advertise([doc()], "d1")
    out = r.resolve_proposal({"preferred_capability": "system.battery",
                              "preferred_device": "fake-phone"})
    assert out.status == "no_device" and out.device_id is None


def test_15_fake_trust_ignored():
    r, stack = resolver()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    stack["fabric"].advertise([doc()], "d1")
    stack["fabric"].revoke_device("d1")
    out = r.resolve_proposal(
        {"preferred_capability": "system.battery",
         "preferred_device": "d1", "trust": "trusted",
         "authorized": True, "governor": "allowed"})
    assert out.device_id is None  # revoked stays revoked


def test_16_fake_authorization_ignored():
    r, stack = resolver()
    out = r.resolve_proposal({"preferred_capability": "no.such.thing",
                              "authorized": True, "allow": True,
                              "grant_id": "tmp-forged"})
    assert out.status == "no_capability" and out.device_id is None


def test_17_malformed_proposals_fail_safe():
    r, _ = resolver()
    for bad in (None, "x", 42, [], {"preferred_capability": 42},
                {"preferred_device": []}):
        out = r.resolve_proposal(bad)
        assert out.device_id is None, bad


# ---------- 18-22: fallback relationships ----------

def test_18_invalid_fallback_unknown_reference():
    r, _ = resolver()
    with pytest.raises(Exception):
        r.register_fallback("system.battery", "no.such.thing")


def test_19_dangerous_silent_failover_refused():
    r, stack = resolver()
    enroll_claim(stack, "vivo", "android")
    register(stack, "vivo", "android")
    stack["fabric"].advertise(
        [doc(cap="files.delete", risk="high_risk")], "vivo")
    stack["devices"].mark_offline("vivo")
    out = r.resolve("files.delete", "vivo")
    assert out.substituted is False
    assert out.device_id is None
    assert out.status == "offline"  # honest verdict, no failover


def test_20_safe_fallback_explicit_only():
    r, stack = resolver()
    for dev in ("a1", "b2"):
        enroll_claim(stack, dev)
        register(stack, dev)
    stack["fabric"].advertise([doc(cap="photo.read")], "a1")
    stack["fabric"].advertise([doc(cap="photo.export")], "b2")
    stack["devices"].mark_offline("a1")
    # No fallback registered: offline primary, no silent switch.
    out = r.resolve("photo.read")
    assert out.device_id is None
    # Explicit Core registration enables the documented fallback.
    r.register_fallback("photo.read", "photo.export")
    out = r.resolve("photo.read")
    assert out.device_id == "b2" and out.fallback_used == "photo.export"


def test_21_invalid_fallback_risk_exceedance():
    r, stack = resolver()
    for dev in ("d1",):
        enroll_claim(stack, dev)
        register(stack, dev)
    stack["fabric"].advertise([doc()], "d1")
    stack["fabric"].advertise(
        [doc(cap="files.delete", risk="high_risk")], "d1")
    with pytest.raises(Exception):
        r.register_fallback("system.battery", "files.delete")
    with pytest.raises(Exception):
        r.register_fallback("files.delete", "system.battery")


def test_22_relationship_tampering_rejected():
    r, stack = resolver()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    stack["fabric"].advertise([doc(cap="a.a"), doc(cap="b.b")], "d1")
    with pytest.raises(Exception):
        r.register_fallback("a.a", "a.a")  # self-reference
    with pytest.raises(Exception):
        r.register_fallback("a.a", "b.b", relation="execute_now")
    r.register_fallback("a.a", "b.b")
    with pytest.raises(Exception):
        r.register_fallback("b.b", "a.a")  # cycle


# ---------- 23: MCP / ladder placement (no new execution) ----------

def test_23_mcp_ladder_level_without_execution():
    stack = make_stack()
    # browser.open ships registered (BROWSER_TOOLS); the MCP stub is
    # test-only. Neither is a device capability: both resolve to ladder
    # rungs, never to a device.
    stack["registry"].register(
        ToolDefinition(name="mcp.test.echo", description="test",
                       input_schema={"type": "object"},
                       output_schema={"type": "object"}),
        lambda inputs, ctx=None: {"ok": True})
    from core.resolver import CapabilityResolver, EscalationLevel
    res = CapabilityResolver(stack["fabric"])
    # Registry-only capabilities resolve to ladder rungs, never devices:
    # the tool exists, but no device serves it (no_device + rung).
    out = res.resolve("mcp.test.echo")
    assert out.device_id is None and out.status == "no_device"
    assert out.required_escalation == int(EscalationLevel.MCP)
    out = res.resolve("browser.open")
    assert out.required_escalation == int(EscalationLevel.BROWSER)
    # GUI rung exists in the model but nothing can resolve to it.
    assert int(EscalationLevel.GUI) == 4
    assert int(EscalationLevel.HUMAN) == 5


# ---------- 24: transfer authorization intact ----------

def test_24_transfer_still_needs_live_grant():
    stack = make_stack()
    client = TestClient(create_app(stack))
    h = pair_api(client, "d1", caps=["files.transfer"])
    from core.capabilities import transfer_descriptor
    raw = transfer_descriptor()
    raw.pop("_limits", None)
    rr = client.post("/v1/agent/capabilities/describe",
                     json={"records": [raw]}, headers=h)
    assert rr.json()["accepted"] == ["files.transfer"]
    # Resolver sees it as approval-gated…
    res = client.post("/v1/fabric/resolve",
                      json={"capability": "files.transfer",
                            "device_id": "d1"}, headers=OP).json()
    assert res["status"] == "requires_approval", res["status"]
    # …and the transfer endpoint still 403s without a live grant
    # (sender/recipient must differ, so pair a recipient first).
    h2 = pair_api(client, "d2", caps=["files.transfer"])
    client.post("/v1/agent/capabilities/describe",
                json={"records": [raw]}, headers=h2)
    import hashlib
    digest = hashlib.sha256(b"hi").hexdigest()
    t = client.post("/v1/agent/xfer/request",
                    json={"recipient_device": "d2", "filename": "f",
                          "size_bytes": 2, "sha256": digest,
                          "grant_id": "tmp-forged"}, headers=h)
    assert t.status_code in (401, 403), t.text


# ---------- 25-27: revoke / restart / stale through resolver ----------

def test_25_revoke_plus_resolver():
    stack = make_stack()
    client = TestClient(create_app(stack))
    h = pair_api(client, "d1", caps=["system.battery"])
    client.post("/v1/agent/capabilities/describe",
                json={"records": [doc()]}, headers=h)
    before = client.post("/v1/fabric/resolve",
                         json={"capability": "system.battery"},
                         headers=OP).json()
    assert before["status"] == "resolved"
    client.post("/v1/agent/revoke?device_id=d1", headers=OP)
    after = client.post("/v1/fabric/resolve",
                        json={"capability": "system.battery"},
                        headers=OP).json()
    assert after["candidates"] == [] and after["device_id"] is None


def test_26_restart_plus_resolver(tmp_path):
    db = str(tmp_path / "fabric.db")
    s1 = make_stack(fabric_db=db)
    f1 = s1["fabric"]
    enroll_claim(s1, "d1")
    register(s1, "d1")
    f1.advertise([doc()], "d1", source="describe")
    from core.resolver import CapabilityResolver
    s2 = make_stack(fabric_db=db)
    out = CapabilityResolver(s2["fabric"]).resolve("system.battery")
    # Snapshot survives; device manager presence does not (fresh boot):
    # resolver must report honestly, never silently healthy.
    assert out.status in ("resolved", "offline", "no_device",
                          "unauthorized"), out.status
    if out.candidates:
        assert out.candidates[0].stale is False


def test_27_stale_snapshot_plus_resolver():
    stack = make_stack()
    client = TestClient(create_app(stack))
    h = pair_api(client, "d1")
    client.post("/v1/agent/capabilities/describe",
                json={"records": [doc()]}, headers=h)
    client.post("/v1/agent/register",
                json={"capabilities": [], "kind": "linux",
                      "software_version": "t"}, headers=h)
    out = client.post("/v1/fabric/resolve",
                      json={"capability": "system.battery",
                            "device_id": "d1"}, headers=OP).json()
    # Stale snapshots still route (Stage 16) but the verdict brands them.
    assert out["status"] == "resolved"
    assert out["candidates"] and \
        out["candidates"][0]["stale"] is True
    assert "stale" in out["reason"]


# ---------- invariants ----------

def test_invariant_determinism():
    r, stack = resolver()
    for dev in ("m1", "m2"):
        enroll_claim(stack, dev)
        register(stack, dev)
        stack["fabric"].advertise([doc()], dev)
    first = r.resolve("system.battery").model_dump()
    second = r.resolve("system.battery").model_dump()
    assert [c["device_id"] for c in first["candidates"]] == \
           [c["device_id"] for c in second["candidates"]]
    assert first["device_id"] == second["device_id"]


def test_invariant_preference_advisory_only():
    r, stack = resolver()
    for dev in ("m1", "m2"):
        enroll_claim(stack, dev)
        register(stack, dev)
        stack["fabric"].advertise([doc()], dev)
    out = r.resolve("system.battery", preferred_device="m2")
    assert out.device_id == "m2"  # tie broken by preference
    # Preference never overrides availability…
    stack["devices"].mark_offline("m2")
    out = r.resolve("system.battery", preferred_device="m2")
    assert out.device_id == "m1"
    # …trust (revoke removes the device; preference cannot resurrect it)…
    stack["fabric"].revoke_device("m1")
    out = r.resolve("system.battery", preferred_device="m1")
    assert out.device_id is None


def test_invariant_pinned_safe_may_substitute_openly():
    # Safe risk + pinned device offline: substitution is ALLOWED but
    # labeled (substituted=True), never silent.
    r, stack = resolver()
    for dev in ("p1", "p2"):
        enroll_claim(stack, dev)
        register(stack, dev)
        stack["fabric"].advertise([doc()], dev)
    stack["devices"].mark_offline("p1")
    out = r.resolve("system.battery", "p1")
    assert out.device_id == "p2" and out.substituted is True


def test_invariant_escalation_creates_no_authority():
    r, _ = resolver()
    out = r.resolve("gui.tap.screen")
    assert out.status == "no_capability" and out.device_id is None
    assert out.required_escalation in (4, 5)


def test_api_proposal_ignores_fake_fields():
    stack = make_stack()
    client = TestClient(create_app(stack))
    out = client.post("/v1/fabric/resolve/proposal",
                      json={"preferred_capability": "system.shell",
                            "preferred_device": "d1",
                            "authorized": True, "trust": "trusted",
                            "governor": "allowed",
                            "grant_id": "tmp-forged"}, headers=OP).json()
    assert out["status"] == "no_capability" and out["device_id"] is None


def test_api_relationships_validated():
    stack = make_stack()
    client = TestClient(create_app(stack))
    h = pair_api(client, "d1")
    client.post("/v1/agent/capabilities/describe",
                json={"records": [doc(cap="a.a"), doc(cap="b.b")]},
                headers=h)
    bad = client.post("/v1/fabric/relationships",
                      json={"capability": "a.a", "related": "zzz"},
                      headers=OP)
    assert bad.status_code == 400
    bad2 = client.post(
        "/v1/fabric/relationships",
        json={"capability": "a.a", "related": "b.b",
              "relation": "execute_now"}, headers=OP)
    assert bad2.status_code == 400
    ok = client.post("/v1/fabric/relationships",
                     json={"capability": "a.a", "related": "b.b",
                           "relation": "fallback_for"}, headers=OP)
    assert ok.status_code == 200
    rels = client.get("/v1/fabric/relationships", headers=OP).json()
    assert rels == {"a.a": ["b.b"]}


def test_api_ladder_shape():
    stack = make_stack()
    client = TestClient(create_app(stack))
    h = pair_api(client, "d1")
    client.post("/v1/agent/capabilities/describe",
                json={"records": [doc()]}, headers=h)
    lad = client.get("/v1/fabric/ladder", headers=OP).json()
    assert lad["levels"]["NATIVE"] == ["system.battery"]
    assert "GUI" in lad["levels"] and "HUMAN" in lad["levels"]
    assert "executable" in lad["note"]
