"""Stage 29 — mined hardening (OpenDroid-derived patterns, none of the
runtime): (A) the never-auto-approve class — standing/ambient grants
never unlock irreversible actions; per-use human approval is the only
path, and the approval paths still complete; (B) mutating gui.* actions
carry explicit verification — the device's OWN re-read confirm, never
the ack.

Seams: fake agents/stack from tests/test_android_workflows.py (real
pipeline code, doubles at the device boundary). Fixture-only: the phone
is an in-memory a11y world; no real device, no implied validation.
"""
import pytest
from types import SimpleNamespace

from core.device_tools import DEVICE_TOOL_DEFS
from core.fabric import FabricRegistry
from core.models import PermissionLevel, RiskLevel, ToolDefinition
from core.execution import ExecutionEngine, ExecutionState
from core.policy import PolicyEngine, NEVER_AUTO_APPROVE_CAPS
from core.tools import ToolRegistry
from tests.test_android_workflows import (
    LAB,
    ANDROID_DEVICE,
    _A11yWorld,
    _AndroidAgent,
    _android_stack,
)

NEVER_AUTO = sorted(NEVER_AUTO_APPROVE_CAPS)


def _verifier(tool: str, rec) -> tuple[bool, str]:
    """FabricRegistry.verify_device_result unbound against a stub — only
    its registry lookups and audit sink exist here; nothing device-y."""
    reg = ToolRegistry()
    for d in DEVICE_TOOL_DEFS:
        reg.register(d, lambda i, c: {})
    stub = SimpleNamespace()
    stub.registry = reg
    audits: list[tuple] = []
    stub._audit = lambda action, target, detail="": audits.append(
        (action, target, detail))
    return FabricRegistry.verify_device_result(stub, tool, rec), audits


# ------------------------------------------------------------------
# A: never-auto-approve class (policy layer)
# ------------------------------------------------------------------

def test_never_auto_standing_grant_cannot_unlock_class():
    """A standing (ambient) grant for a never-auto capability is
    ignored: every member requires explicit per-use human approval."""
    for cap, risk in (("storage.delete", RiskLevel.CONFIRM),
                      ("filesystem.delete", RiskLevel.CONFIRM),
                      ("messaging.send", RiskLevel.CONFIRM),
                      ("android.app.force_stop", RiskLevel.HIGH_RISK)):
        eng = PolicyEngine()
        gid = eng.grant("user", f"tool:{cap}", "linux", cap, ttl_s=300,
                        source="standing")
        assert eng._grants[gid]["source"] == "standing"
        dec = eng.decide("user", f"tool:{cap}", "linux", cap, risk=risk)
        assert dec.allow is False, cap
        assert dec.requires_approval is True, cap
        assert "never-auto" in dec.reason, cap
        # the grant was never consumed by the refusal
        assert eng._grants[gid]["used"] is False


def test_never_auto_ignores_prefix_spellings():
    """tool:/capability: spellings hit the same rule (bare-name
    membership), so no aliasing around the class."""
    for spelling in ("tool:storage.delete", "capability:storage.delete"):
        eng = PolicyEngine()
        eng.grant("user", spelling, "*", "*", ttl_s=300)
        dec = eng.decide("user", spelling, "linux", "storage.delete",
                         risk=RiskLevel.SAFE)
        assert dec.allow is False and dec.requires_approval is True


def test_class_survives_a_risk_label_regress():
    """Belt-and-braces: a future SAFE/CONFIRM relabel cannot widen a
    never-auto action — membership outranks the risk label. It still
    parks (not hard-deny): the approval path must stay possible."""
    eng = PolicyEngine()
    eng.grant("user", "tool:storage.delete", "linux", "storage.delete",
              ttl_s=300)
    dec = eng.decide("user", "tool:storage.delete", "linux",
                     "storage.delete", risk=RiskLevel.SAFE)
    assert dec.allow is False and dec.requires_approval is True
    assert not dec.hard_deny
    assert "never-auto" in dec.reason


def test_non_class_confirm_tools_keep_grant_flow():
    """No overreach: the rule narrows ONLY the four members — ordinary
    CONFIRM tools still consume a scoped standing grant."""
    eng = PolicyEngine()
    gid = eng.grant("user", "tool:desktop.clipboard.write", "linux",
                    "desktop.clipboard.write", ttl_s=300)
    dec = eng.decide("user", "tool:desktop.clipboard.write", "linux",
                     "desktop.clipboard.write", risk=RiskLevel.CONFIRM)
    assert dec.allow is True and dec.grant_id == gid
    assert eng._grants[gid]["used"] is True


def test_decide_never_consumes_even_approval_sourced_grants():
    """Approval paths dispatch with their own injected auth — the
    decide() grant store is not involved even for approval-sourced
    mints. So an approval-sourced leftover can never auto-bless a later
    never-auto request (no rescue via the store)."""
    eng = PolicyEngine()
    eng.grant("user", "tool:storage.delete", "linux", "storage.delete",
              ttl_s=300, source="approval")
    dec = eng.decide("user", "tool:storage.delete", "linux",
                     "storage.delete", risk=RiskLevel.CONFIRM)
    assert dec.allow is False and dec.requires_approval is True


def test_never_auto_park_and_per_use_approval_completes(tmp_path):
    """The class parks; the real per-use approval path (engine hold →
    approve → mint source='approval' → injected auth) completes the run.
    Policy outranks tags here on purpose: the def is left UNtagged to
    prove membership, not a decorator, is the enforcement."""
    calls = []
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="storage.delete",
        description="stub for the policy-behavior proof",
        input_schema={"type": "object",
                      "required": ["path", "confirm"],
                      "properties": {"path": {"type": "string"},
                                     "confirm": {"type": "boolean"}}},
        permission=PermissionLevel.CONFIRM, risk=RiskLevel.CONFIRM),
        lambda i, c: calls.append(i["path"]) or {"deleted": True})
    eng = ExecutionEngine(reg, PolicyEngine())

    parked = eng.submit("storage.delete", {"path": "a.txt",
                                           "confirm": True}, "cloud",
                        who="u1")
    assert parked.state == ExecutionState.WAITING_FOR_PERMISSION
    assert parked.auth.allow is False
    assert "never-auto" in parked.auth.reason
    assert calls == []                       # nothing ran while parked

    done = eng.approve(parked.id)
    assert done.state == ExecutionState.SUCCEEDED
    assert done.auth.allow is True           # injected auth
    assert calls == ["a.txt"]                # ran exactly once
    # the approval mint carries approval provenance
    eggs = [g for g in eng.policy._grants.values()
            if g["capability"] == "tool:storage.delete"]
    assert eggs and all(g["source"] == "approval" for g in eggs)


# ------------------------------------------------------------------
# A: registry tags are derived from the policy set (single source)
# ------------------------------------------------------------------

def test_registry_never_auto_tags_match_policy_set():
    reg_core = ToolRegistry()
    for d in DEVICE_TOOL_DEFS:
        reg_core.register(d, lambda i, c: {})
    reg_linux = ToolRegistry()
    from device.linux.tools_linux import LINUX_TOOLS
    for d, h in LINUX_TOOLS:
        reg_linux.register(d, h)
    for reg in (reg_core, reg_linux):
        for d in reg.list():
            assert d.never_auto_approve == (d.name in
                                            NEVER_AUTO_APPROVE_CAPS), d.name
    for reg in (reg_core, reg_linux):
        for name in NEVER_AUTO_APPROVE_CAPS:
            try:
                d = reg.get(name)
            except KeyError:
                continue  # not a registry tool; the set keeps the record
            assert d.never_auto_approve, name


# ------------------------------------------------------------------
# B: mutating gui.* — explicit, device-owned verification
# ------------------------------------------------------------------

def test_tap_approval_runs_once_and_verifies_natively():
    world = _A11yWorld([
        {"node_id": 3, "text": "Send", "cls": "android.widget.Button"},
    ])
    stack = _android_stack(world)
    with _AndroidAgent(stack, world):
        out = stack["fabric"].execute_on_device(
            "gui.tap", {"target_package": LAB, "node_id": 3},
            ANDROID_DEVICE, "u0")
        assert out["stage"] == "waiting_for_permission"
        rec = stack["engine"].approve(out["execution_id"])
        assert rec.state.value == "succeeded"
        # explicit latch: the device's OWN re-read confirm ('clicked'),
        # carried into the record and the fabric verdict
        assert rec.result.get("verified") is True
        assert rec.verified is True
        ok, why = stack["fabric"].verify_device_result("gui.tap", rec)
        assert ok is True and "explicit" in why


def test_ack_only_device_shape_is_honestly_unverified():
    """The old ack-only result shape (no verified flag) must NOT latch a
    true verdict: explicit mode reports the flag absent and the record
    stays verified=False — even though the step succeeded."""

    class _LegacyAgent(_AndroidAgent):
        def _handle(self, tool, inputs):
            out = super()._handle(tool, inputs)
            if tool == "gui.tap" and isinstance(out, dict):
                out.pop("verified", None)   # pretend an older device
            return out

    world = _A11yWorld([{"node_id": 3, "text": "Send"}])
    stack = _android_stack(world)
    with _LegacyAgent(stack, world):
        out = stack["fabric"].execute_on_device(
            "gui.tap", {"target_package": LAB, "node_id": 3},
            ANDROID_DEVICE, "u0")
        rec = stack["engine"].approve(out["execution_id"])
        assert rec.state.value == "succeeded"       # the tap HAPPENED
        assert rec.verified is False                # ...but unverified
        ok, why = stack["fabric"].verify_device_result("gui.tap", rec)
        assert ok is False and "absent" in why


def test_back_verdict_keys_on_fresh_post_nav_snapshot():
    world = _A11yWorld([{"node_id": 1, "text": "X"}])
    stack = _android_stack(world)
    with _AndroidAgent(stack, world):
        out = stack["fabric"].execute_on_device(
            "gui.back", {"target_package": LAB, "direction": "back"},
            ANDROID_DEVICE, "u0")
        rec = stack["engine"].approve(out["execution_id"])
        assert rec.state.value == "succeeded"
        # the fake mirrors the Dart contract: fresh post-navigation
        # snapshot => package_after present => verified true
        assert rec.result.get("package_after") == LAB
        assert rec.result.get("fresh_snapshot_id")
        assert rec.result.get("verified") is True
        assert rec.verified is True


def test_scroll_never_self_reports_verified():
    """scroll has no automatic native re-read: its verified verdict is
    false even on success (explicit mode, flag carried false)."""
    rec = SimpleNamespace(
        state=SimpleNamespace(value="succeeded"), device_id="phone")
    rec.result = {"applied": True, "verify_state":
                  "requested (compare snapshots for movement)",
                  "verified": False}
    (ok, why), _ = _verifier("gui.scroll", rec)
    assert ok is False and "explicit" in why


def test_failed_gui_step_never_verifies():
    rec = SimpleNamespace(
        state=SimpleNamespace(value="failed"), device_id="phone",
        result={"applied": False, "verified": False})
    (ok, why), _ = _verifier("gui.tap", rec)
    assert ok is False and "state=" in why


def test_explicit_latch_reasons_are_actionable():
    """The flag-absent reason names the contract so a misbehaving device
    agent is diagnosable from the record alone; failed verification is
    audited ('verification_failed') on the device id."""
    rec = SimpleNamespace(
        state=SimpleNamespace(value="succeeded"), device_id="phone")
    rec.result = {"applied": True}
    (ok, why), audits = _verifier("gui.scroll", rec)
    assert ok is False and "absent" in why
    assert audits and audits[-1][0] == "verification_failed"
    assert audits[-1][1] == "phone"
