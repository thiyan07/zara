"""Closed constraint model tests: schema, normalization, evaluation.

Proves the 4-key closed model (platform/min_version/risk_max/online_only),
unknown-key stripping, fail-closed invalids, determinism, serialization,
oversized/malformed input, revoked/unauthorized denial, high-risk
interaction, and that constraints never authorize. No sleeps/drains.
"""
import pytest
from pydantic import ValidationError

from core.app import build_stack
from core.models import DeviceKind, DeviceState, ResolveConstraints
from core.resolver import CONSTRAINT_ALLOWLIST, normalize_constraints

INSPECT = "gui.screen.inspect"
TAP = "gui.tap"
BAT = "system.battery"


def make_stack(fabric_db=""):
    return build_stack(fabric_db=fabric_db)


def enroll_claim(stack, device_id, kind="linux"):
    code = stack["device_auth"].enroll(device_id, DeviceKind(kind))
    return stack["device_auth"].claim(code)


def register(stack, device_id, kind="linux", caps=None):
    stack["devices"].register(DeviceState(
        device_id=device_id, kind=DeviceKind(kind),
        capabilities=caps or [], online=True, status="online"))


def paired_android(stack, dev="emu-lab"):
    enroll_claim(stack, dev, "android")
    register(stack, dev, "android", [INSPECT, TAP, BAT])
    stack["fabric"].advertise([
        {"id": INSPECT, "name": INSPECT, "descriptor_version": "1",
         "version": "1.0.0", "risk": "confirm", "availability": "available"},
        {"id": TAP, "name": TAP, "descriptor_version": "1",
         "version": "1.0.0", "risk": "high_risk",
         "availability": "available"},
        {"id": BAT, "name": BAT, "descriptor_version": "1",
         "version": "1.0.0", "risk": "safe", "availability": "available"},
    ], dev)
    return dev


# ---------- schema ----------

def test_K00_allowlist_documented():
    assert CONSTRAINT_ALLOWLIST == ("platform", "min_version", "risk_max",
                                    "online_only")


def test_K01_model_accepts_valid():
    c = ResolveConstraints(platform="any", min_version="1.0.0",
                           risk_max="safe", online_only=True)
    assert c.platform == "any" and c.online_only is True
    assert ResolveConstraints().model_dump() == {
        "platform": "", "min_version": "", "risk_max": "",
        "online_only": False}


def test_K02_model_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        ResolveConstraints(color="red")


def test_K03_model_rejects_wrong_types():
    with pytest.raises(ValidationError):
        ResolveConstraints(platform=42)
    with pytest.raises(ValidationError):
        ResolveConstraints(platform=["x"])
    with pytest.raises(ValidationError):
        ResolveConstraints(online_only=[True])


def test_K04_model_bounds_lengths():
    with pytest.raises(ValidationError):
        ResolveConstraints(platform="x" * 65)
    with pytest.raises(ValidationError):
        ResolveConstraints(risk_max="y" * 17)


# ---------- normalization ----------

def test_K05_normalize_none_and_model():
    assert normalize_constraints(None) == {}
    c = ResolveConstraints(platform="any")
    assert normalize_constraints(c) == {
        "platform": "any", "min_version": "", "risk_max": "",
        "online_only": False}
    assert normalize_constraints("junk") == {}
    assert normalize_constraints([1, 2]) == {}


def test_K06_normalize_strips_unknown_keeps_typed():
    out = normalize_constraints({"platform": "any", "color": "red",
                                 "risk_max": 42, "online_only": True})
    assert out == {"platform": "any", "online_only": True}
    assert "color" not in out and "risk_max" not in out


# ---------- evaluation ----------

def test_K07_platform_match_mismatch():
    stack = make_stack()
    dev = paired_android(stack)
    assert stack["resolver"].resolve(
        BAT, dev, constraints={"platform": "any"}).device_id == dev
    assert stack["resolver"].resolve(
        BAT, dev, constraints={"platform": "ios"}).device_id is None


def test_K08_min_version_boundaries():
    stack = make_stack()
    dev = paired_android(stack)
    assert stack["resolver"].resolve(
        BAT, dev, constraints={"min_version": "0.9.9"}).device_id == dev
    assert stack["resolver"].resolve(
        BAT, dev, constraints={"min_version": "1.0.0"}).device_id == dev
    assert stack["resolver"].resolve(
        BAT, dev, constraints={"min_version": "1.0.1"}).device_id is None


def test_K09_risk_ladder():
    stack = make_stack()
    dev = paired_android(stack)
    assert stack["resolver"].resolve(
        TAP, dev, constraints={"risk_max": "safe"}).device_id is None
    r = stack["resolver"].resolve(
        TAP, dev, constraints={"risk_max": "high_risk"})
    assert r.device_id == dev and r.status == "requires_approval"


def test_K10_online_only():
    stack = make_stack()
    dev = paired_android(stack)
    assert stack["resolver"].resolve(
        BAT, dev, constraints={"online_only": True}).device_id == dev
    stack["devices"].mark_offline(dev)
    assert stack["resolver"].resolve(
        BAT, dev, constraints={"online_only": True}).device_id is None
    r = stack["resolver"].resolve(BAT, dev)
    assert r.status == "offline"  # honest verdict without the flag


def test_K11_empty_equals_none():
    stack = make_stack()
    dev = paired_android(stack)
    a = stack["resolver"].resolve(BAT, dev, constraints={}).model_dump()
    b = stack["resolver"].resolve(BAT, dev, constraints=None).model_dump()
    c = stack["resolver"].resolve(BAT, dev).model_dump()
    assert a == b == c


def test_K12_conjunctive_and_contradictory():
    stack = make_stack()
    dev = paired_android(stack)
    r = stack["resolver"].resolve(BAT, dev, constraints={
        "platform": "any", "min_version": "1.0.0", "risk_max": "safe",
        "online_only": True})
    assert r.device_id == dev
    r = stack["resolver"].resolve(BAT, dev, constraints={
        "platform": "ios", "risk_max": "safe"})
    # No silent cross-device pick: pinned device filtered → device None.
    # (Status vocabulary for filtered pinned candidates is pre-existing.)
    assert r.device_id is None


def test_K13_deterministic():
    stack = make_stack()
    dev = paired_android(stack)
    outs = [stack["resolver"].resolve(
        BAT, dev, constraints={"platform": "any"}).model_dump()
        for _ in range(20)]
    assert len({repr(o) for o in outs}) == 1


def test_K14_revoked_never_resolves():
    stack = make_stack()
    dev = paired_android(stack)
    stack["fabric"].revoke_device(dev, "test")
    for c in ({}, {"online_only": False}, {"risk_max": "high_risk"},
              {"platform": "any"}):
        assert stack["resolver"].resolve(BAT, dev,
                                         constraints=c).device_id is None


def test_K15_high_risk_still_parks():
    stack = make_stack()
    dev = paired_android(stack)
    r = stack["resolver"].resolve(TAP, dev,
                                  constraints={"risk_max": "high_risk"})
    assert r.status == "requires_approval"
    rec = stack["engine"].submit_async(
        TAP, {"target_package": "dev.zara.lab.privtest"}, dev)
    from core.models import ExecutionState as ES
    assert rec.state == ES.WAITING_FOR_PERMISSION
    assert stack["jobs"].poll(dev) is None


def test_K16_forged_proposal_fields_ignored():
    stack = make_stack()
    dev = paired_android(stack)
    clean = stack["resolver"].resolve_proposal(
        {"capability": TAP, "device_id": dev})
    forged = stack["resolver"].resolve_proposal(
        {"capability": TAP, "device_id": dev, "authorized": True,
         "trust": "trusted", "allow": True, "grant_id": "g-forged"})
    assert clean.status == forged.status == "requires_approval"
    assert clean.device_id == forged.device_id == dev


def test_K17_proposal_constraints_allowlisted():
    stack = make_stack()
    dev = paired_android(stack)
    r = stack["resolver"].resolve_proposal(
        {"capability": BAT, "device_id": dev,
         "constraints": {"platform": "ios", "color": "red"}})
    assert r.device_id is None  # platform filtered; color stripped
    r = stack["resolver"].resolve_proposal(
        {"capability": BAT, "device_id": dev, "constraints": "junk"})
    assert r.device_id == dev  # non-dict ignored, resolves normally
