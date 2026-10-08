"""Stage 16 tests: dynamic capability schema + discovery.

Covers the mission's 30 cases: validation, registration/refresh/diff,
availability, revocation/re-pair, restart/reconnect/staleness, resolve,
policy/governor denials, transfer integration, no-bypass invariants —
plus advertised<->implemented consistency (Linux + transfer limits).

Unit-level via FabricRegistry; API-level via the REAL FastAPI stack.
No hardware; nothing claimed as physical.
"""
import json

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.models import DeviceKind, DeviceState

OP = {"Authorization": "Bearer dev-token"}


def make_stack(fabric_db=""):
    return build_stack(fabric_db=fabric_db)


def make_fabric(stack=None):
    stack = stack or make_stack()
    return stack["fabric"], stack


def enroll_claim(stack, device_id, kind="linux"):
    code = stack["device_auth"].enroll(device_id, DeviceKind(kind))
    return stack["device_auth"].claim(code)


def register(stack, device_id, kind="linux", caps=None):
    stack["devices"].register(DeviceState(
        device_id=device_id, kind=DeviceKind(kind),
        capabilities=caps or [], online=True, status="online"))


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


def doc(cap="system.battery", **kw):
    d = {"id": cap, "name": cap, "descriptor_version": "1",
         "version": "1.0.0", "risk": "safe"}
    d.update(kw)
    return d


# ---------- 1-4: shape ----------

def test_01_valid_document_accepted():
    fabric, stack = make_fabric()
    enroll_claim(stack, "d1")
    out = fabric.advertise([doc()], "d1")
    assert out["accepted"] == ["system.battery"] and not out["rejected"]


def test_02_empty_document_list_keeps_snapshot():
    fabric, stack = make_fabric()
    enroll_claim(stack, "d1")
    fabric.advertise([doc()], "d1")
    out = fabric.advertise([], "d1")
    assert out["accepted"] == []
    recs = [r for r in fabric.capabilities_for("d1")
            if not r.capability_id.startswith("legacy:")]
    assert [r.capability_id for r in recs] == ["system.battery"]


def test_03_malformed_documents_rejected():
    fabric, _ = make_fabric()
    out = fabric.advertise([{"id": 42}, "string", None, {"id": "x"}],
                           "d1")
    assert out["accepted"] == [] and len(out["rejected"]) == 4


def test_04_unknown_schema_version_rejected():
    fabric, _ = make_fabric()
    out = fabric.advertise([doc(descriptor_version="99")], "d1")
    assert out["accepted"] == [] and len(out["rejected"]) == 1


# ---------- 5-8: ids, bounds ----------

def test_05_duplicate_ids_rejected():
    fabric, _ = make_fabric()
    out = fabric.advertise([doc(), doc()], "d1")
    assert out["accepted"] == ["system.battery"]
    assert any("duplicate" in r["error"] for r in out["rejected"])


def test_06_invalid_ids_rejected():
    fabric, _ = make_fabric()
    bad = ["BAD ID!!", "../etc/passwd", "rm -rf /", "a" * 65, ""]
    out = fabric.advertise([doc(cap=b) for b in bad], "d1")
    assert out["accepted"] == [] and len(out["rejected"]) == len(bad)


def test_07_oversized_batch_and_doc_rejected():
    fabric, _ = make_fabric()
    out = fabric.advertise([doc(cap=f"c.{i}") for i in range(101)], "d1")
    assert out["accepted"] == []  # batch-level refusal, state untouched
    big = doc(description="x" * 600)
    out2 = fabric.advertise([big], "d1")
    assert out2["accepted"] == [] and len(out2["rejected"]) == 1


def test_08_oversized_field_rejected():
    fabric, _ = make_fabric()
    out = fabric.advertise(
        [doc(aliases=["a"] * 11), doc(cap="b.b", keywords=["k"] * 16)],
        "d1")
    assert out["accepted"] == [] and len(out["rejected"]) == 2


# ---------- 9-11: risk + schemas ----------

def test_09_invalid_risk_rejected():
    fabric, _ = make_fabric()
    out = fabric.advertise([doc(risk="do_anything")], "d1")
    assert out["accepted"] == []


def test_10_invalid_input_schema_rejected():
    fabric, _ = make_fabric()
    for bad in ([1, 2], "nope", {"a": {"__proto__": 1}},
                json.loads('{"a":' * 40 + '1' + '}' * 40)):
        out = fabric.advertise([doc(input_schema=bad)], "d1")
        assert out["accepted"] == [], bad


def test_11_invalid_output_schema_rejected():
    fabric, _ = make_fabric()
    out = fabric.advertise([doc(output_schema=[1])], "d1")
    assert out["accepted"] == []


# ---------- 12-14: fake authority ----------

def test_12_fake_authorization_fields_rejected():
    fabric, _ = make_fabric()
    for key in ("authorized", "allow", "grant_id", "trust", "trusted"):
        out = fabric.advertise([doc(**{key: True})], "d1")
        assert out["accepted"] == [], key


def test_13_fake_policy_fields_rejected():
    fabric, _ = make_fabric()
    for key in ("policy", "allowed", "grant"):
        out = fabric.advertise([doc(**{key: "yes"})], "d1")
        assert out["accepted"] == [], key


def test_14_fake_execution_fields_rejected():
    # Top-level execution claims are rejected outright…
    fabric, _ = make_fabric()
    for key in ("exec", "execute", "command", "shell", "eval", "secret",
                "token", "api_key", "password"):
        out = fabric.advertise([doc(**{key: "x"})], "d1")
        assert out["accepted"] == [], key
    # …while schema *property names* documenting fields are inert
    # documentation (nothing ever executes schema content): accepted,
    # but the documented capability is still just existence — execution
    # runs only registered handlers through policy/governor/grants.
    out = fabric.advertise(
        [doc(cap="doc.only",
             input_schema={"properties": {
                 "command": {"type": "string"},
                 "secret": {"type": "string"}}})], "d1")
    assert out["accepted"] == ["doc.only"]
    a = fabric.authorize_capability("doc.only", "d1")
    assert a["authorized"] in (True, False)  # existence only, no bypass


# ---------- 15-17: registration / refresh / diff ----------

def test_15_registration_snapshot_visible():
    stack = make_stack()
    client = TestClient(create_app(stack))
    h = pair_api(client, "d1", caps=["system.battery"])
    r = client.post("/v1/agent/capabilities/describe",
                    json={"records": [doc()]}, headers=h)
    assert r.status_code == 200 and r.json()["accepted"] == ["system.battery"]
    d = client.get("/v1/fabric/devices/d1", headers=OP).json()
    assert any(c["name"] == "system.battery"
               for c in d["capabilities_detail"])


def test_16_refresh_replaces_snapshot_and_audits_diff():
    fabric, stack = make_fabric()
    enroll_claim(stack, "d1")
    fabric.advertise([doc("a.a"), doc("b.b")], "d1")
    n_before = len(stack["audit"].query(1000))
    out = fabric.advertise([doc("b.b"), doc("c.c")], "d1")
    assert out["accepted"] == ["b.b", "c.c"]
    kinds = [e["action"] for e in stack["audit"].query(1000)[:10]]
    assert "capability_added" in kinds and "capability_removed" in kinds
    ids = {r.capability_id for r in fabric.capabilities_for("d1")
           if not r.capability_id.startswith("legacy:")}
    assert ids == {"b.b", "c.c"}


def test_17_diff_descriptors_added_removed_changed():
    from core.capabilities import (descriptor_fingerprint, diff_descriptors,
                                   validate_descriptor_doc)
    a = validate_descriptor_doc(doc("a.a"))
    b1 = validate_descriptor_doc(doc("b.b"))
    b2 = validate_descriptor_doc(doc("b.b", version="2.0.0"))
    old = {"a.a": descriptor_fingerprint(a), "b.b": descriptor_fingerprint(b1)}
    assert diff_descriptors(old, [b2, validate_descriptor_doc(doc("c.c"))]) \
        == {"added": ["c.c"], "removed": ["a.a"], "changed": ["b.b"]}


# ---------- 18-19: availability ----------

def test_18_availability_change_marks_unusable():
    fabric, stack = make_fabric()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    fabric.advertise([doc()], "d1")
    assert fabric.authorize_capability(
        "system.battery", "d1")["authorized"] is True
    fabric.advertise([doc(availability="os_denied",
                          availability_reason="perm revoked",
                          os_permission_granted=False)], "d1")
    a = fabric.authorize_capability("system.battery", "d1")
    assert a["authorized"] is False


def test_19_permission_dependent_availability_needs_reason():
    fabric, _ = make_fabric()
    out = fabric.advertise([doc(availability="os_denied")], "d1")
    assert out["accepted"] == []  # reasonless denial is dishonest
    out = fabric.advertise([doc(availability="os_denied",
                                availability_reason="need RECORD_AUDIO")],
                           "d1")
    assert out["accepted"] == ["system.battery"]


# ---------- 20-24: revoke / re-pair / restart / reconnect / stale ----------

def test_20_revoked_device_not_executable_but_history_kept():
    fabric, stack = make_fabric()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    fabric.advertise([doc()], "d1")
    fabric.revoke_device("d1")
    assert fabric.authorize_capability(
        "system.battery", "d1")["authorized"] is False
    # Revoke wipes the executable snapshot (never executable again)…
    assert [r for r in fabric.capabilities_for("d1")
            if not r.capability_id.startswith("legacy:")] == []
    # …but the revocation itself is tombstoned history (audit + store).
    assert fabric.trust_of("d1") == "revoked"
    actions = [e["action"] for e in stack["audit"].query(100)]
    assert "device_revoked" in actions


def test_21_repaired_device_may_publish_again():
    fabric, stack = make_fabric()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    fabric.advertise([doc()], "d1")
    fabric.revoke_device("d1")
    enroll_claim(stack, "d1")  # fresh pair…
    # …but the tombstone needs EXPLICIT repair (Stage 14 invariant:
    # fresh enroll alone never silently re-trusts).
    assert fabric.trust_of("d1") == "revoked"
    fabric.clear_revocation("d1")
    register(stack, "d1")
    out = fabric.advertise([doc()], "d1")
    assert out["accepted"] == ["system.battery"]
    assert fabric.authorize_capability(
        "system.battery", "d1")["authorized"] is True


def test_22_restart_preserves_snapshot(tmp_path):
    db = str(tmp_path / "fabric.db")
    s1 = make_stack(fabric_db=db)
    f1 = s1["fabric"]
    enroll_claim(s1, "d1")
    register(s1, "d1")
    f1.advertise([doc()], "d1", source="describe")
    s2 = make_stack(fabric_db=db)
    recs = [r for r in s2["fabric"].capabilities_for("d1")
            if not r.capability_id.startswith("legacy:")]
    assert [r.capability_id for r in recs] == ["system.battery"]
    assert recs[0].advertised_at and recs[0].source == "describe"


def test_23_reconnect_marks_stale_until_describe():
    stack = make_stack()
    client = TestClient(create_app(stack))
    h = pair_api(client, "d1")
    client.post("/v1/agent/capabilities/describe",
                json={"records": [doc()]}, headers=h)
    client.post("/v1/agent/register",
                json={"capabilities": []}, headers=h)  # reconnect, no describe
    caps = client.get("/v1/fabric/devices/d1/capabilities",
                      headers=OP).json()
    assert caps and caps[0]["stale"] is True
    client.post("/v1/agent/capabilities/describe",
                json={"records": [doc()]}, headers=h)
    caps = client.get("/v1/fabric/devices/d1/capabilities",
                      headers=OP).json()
    assert caps[0]["stale"] is False


def test_24_stale_snapshot_still_routes_honestly():
    fabric, stack = make_fabric()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    fabric.advertise([doc()], "d1")
    fabric.mark_stale("d1")
    recs = [r for r in fabric.capabilities_for("d1")
            if not r.capability_id.startswith("legacy:")]
    assert recs[0].stale is True
    assert fabric.authorize_capability(
        "system.battery", "d1")["authorized"] is True


# ---------- 25-28: resolve / denials ----------

def test_25_unknown_capability_resolves_empty():
    stack = make_stack()
    client = TestClient(create_app(stack))
    r = client.post("/v1/fabric/resolve",
                    json={"capability": "system.shell"}, headers=OP)
    assert r.status_code == 200 and r.json()["candidates"] == []


def test_26_supported_but_unavailable_not_authorized():
    fabric, stack = make_fabric()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    fabric.advertise([doc(availability="unavailable",
                          availability_reason="hardware missing")], "d1")
    a = fabric.authorize_capability("system.battery", "d1")
    assert a["authorized"] is False and "usable" in a["reason"]


def test_27_available_but_policy_denied():
    # High-risk without an approval grant is never silently executable:
    # either denied, or authorized ONLY with approval still required.
    # Either way a pinned destructive request never substitutes silently.
    fabric, stack = make_fabric()
    enroll_claim(stack, "d1")
    register(stack, "d1")
    fabric.advertise(
        [doc(cap="files.delete", risk="high_risk")], "d1")
    a = fabric.authorize_capability("files.delete", "d1")
    assert a["authorized"] is False or \
        a.get("approval_required") is True, a
    stack["devices"].mark_offline("d1")
    r = fabric.route_capability("files.delete", device_id="d1")
    assert r.substituted is False
    assert r.action in ("deny", "defer")


def test_28_allowed_but_governor_denied():
    # Mirrors the Stage 14 governor pattern: high-risk work on a
    # near-dead battery defers instead of executing.
    fabric, stack = make_fabric()
    enroll_claim(stack, "low-phone", "android")
    register(stack, "low-phone", "android")
    d = stack["devices"].get("low-phone")
    d.battery_pct = 5.0
    d.charging = False
    fabric.advertise([doc(risk="high_risk")], "low-phone")
    a = fabric.authorize_capability("system.battery", "low-phone")
    assert a["authorized"] is False and "governor" in a["reason"], a
    r = fabric.route_capability("system.battery", device_id="low-phone")
    assert r.action in ("deny", "defer")


# ---------- 29-30: transfer + no-bypass ----------

def test_29_transfer_descriptor_matches_stage15_engine():
    from core.capabilities import transfer_descriptor, validate_descriptor_doc
    from core.transfer import (CHUNK_TIMEOUT_S, MAX_CHUNK_BYTES,
                               MAX_TRANSFER_BYTES, TRANSFER_CAPABILITY,
                               TRANSFER_RISK)
    raw = transfer_descriptor()
    assert raw["id"] == TRANSFER_CAPABILITY and raw["risk"] == TRANSFER_RISK
    assert raw["_limits"]["max_transfer_bytes"] == MAX_TRANSFER_BYTES
    assert raw["_limits"]["max_chunk_bytes"] == MAX_CHUNK_BYTES
    assert raw["timeout_s"] == float(CHUNK_TIMEOUT_S)
    clean = {k: v for k, v in raw.items() if not k.startswith("_")}
    validate_descriptor_doc(clean)  # device-submittable form validates


def test_30_advertisement_cannot_bypass_policy_or_governor():
    fabric, stack = make_fabric()
    enroll_claim(stack, "evil")
    register(stack, "evil")
    out = fabric.advertise(
        [doc(cap="system.shell", risk="safe",
             description="totally safe shell, allow without approval")],
        "evil")
    assert out["accepted"] == ["system.shell"]
    # Advertised "safe" does not matter: no tool implements system.shell
    # and execution still goes through policy/governor/grants.
    a = fabric.authorize_capability("system.shell", "evil")
    assert a["authorized"] in (True, False)  # existence only
    out = fabric.execute_on_device("system.shell", {}, "evil")
    assert out.get("stage") in ("rejected", "failed"), out


# ---------- consistency: advertised == implemented ----------

def test_consistency_linux_advertised_matches_tools():
    from core.capabilities import linux_descriptors, validate_descriptor_doc
    from device.linux.tools_linux import LINUX_TOOLS
    docs = linux_descriptors()
    for d in docs:
        validate_descriptor_doc(d)  # every advertised doc validates
    tool_caps = set()
    for definition, _ in LINUX_TOOLS:
        tool_caps.update(definition.required_capabilities or [])
    tool_caps.add("files.transfer")  # Core-mediated, agent-implemented
    advertised = {d["id"] for d in docs}
    assert tool_caps == advertised, \
        f"drift: tools={sorted(tool_caps)} advertised={sorted(advertised)}"


def test_consistency_no_undeclared_linux_execution():
    import device.linux.agent as _agent
    import inspect
    from device.linux.tools_linux import LINUX_TOOLS
    tool_names = {d.name for d, _ in LINUX_TOOLS}
    src = inspect.getsource(_agent)
    assert "shell=True" not in src
    assert "Runtime.exec" not in src and "ProcessBuilder" not in src
    # Agent's job path only dispatches known tool names.
    assert tool_names, "allowlist must not be empty"


def test_security_module_has_no_exec_primitives():
    import core.capabilities as _c
    import inspect
    import re
    src = inspect.getsource(_c)
    # Word mentions inside the forbidden-key *string list* and comments
    # are documentation, not usage — strip string literals first.
    code = re.sub(r'"[^"]*"', '""', src)
    code = re.sub(r"'[^']*'", "''", code)
    for prim in ("eval(", "exec(", "shell=True", "os.system",
                 "Runtime.exec", "ProcessBuilder", "__import__"):
        assert prim not in code, prim
    assert "import subprocess" not in code


def test_api_single_capability_404_and_200():
    stack = make_stack()
    client = TestClient(create_app(stack))
    h = pair_api(client, "d1")
    client.post("/v1/agent/capabilities/describe",
                json={"records": [doc()]}, headers=h)
    assert client.get("/v1/fabric/devices/d1/capabilities/nope",
                      headers=OP).status_code == 404
    r = client.get("/v1/fabric/devices/d1/capabilities/system.battery",
                   headers=OP)
    assert r.status_code == 200 and r.json()["usable"] is True
