"""Stage 30 — read-only observation stops prompting (dummy mining).

Two claims, both policy-level and membership-based:
A. The read-only observation family (gui.screen.inspect, gui.find,
   gui.wait, android.notifications.read) is allowed by decide() WITHOUT
   approval — no prompt currency spent on harmless reads.
B. The relaxation cannot leak: never-auto outranks read-only
   membership (a mislabelled member still parks), DENY_PATTERNS still
   outrank, and non-members with the same risk label still park.
Evidence/verdict semantics are untouched: the allow is NOT a
verification claim (only the Stage-28 native re-read is).
"""
from __future__ import annotations

import pytest

from core.device_tools import DEVICE_TOOL_DEFS
from core.models import RiskLevel
from core.policy import (NEVER_AUTO_APPROVE_CAPS, PolicyEngine,
                         READ_ONLY_AUTO_ALLOW_CAPS)

READ_ONLY_FAMILY = ["gui.screen.inspect", "gui.find", "gui.wait",
                    "android.notifications.read"]

MUTATING_STILL_PARKED = ["gui.tap", "gui.set_text", "gui.scroll",
                         "gui.back", "storage.delete", "filesystem.delete",
                         "android.app.force_stop", "messaging.send"]


# ---------------- A: read-only family allows without approval ----------

def test_read_only_family_allows_without_any_grant():
    eng = PolicyEngine()
    for cap in READ_ONLY_FAMILY:
        d = eng.decide("u", cap, risk="confirm")
        assert d.allow is True, cap
        assert d.requires_approval is False, cap
        assert "read-only observation" in d.reason, cap


def test_read_only_allow_is_independent_of_grant_store():
    """No grant is needed OR consumed; a leftover unrelated grant
    changes nothing and is never touched."""
    eng = PolicyEngine()
    gid = eng.grant("u", "some.other.cap", ttl_s=999)
    d = eng.decide("u", "gui.find", risk="confirm")
    assert d.allow is True and d.requires_approval is False
    assert d.grant_id is None
    # the unrelated grant is still live (never consumed by the allow)
    assert any(g["used"] is False for g in eng._grants.values())


def test_read_only_members_are_registered_defs():
    defs = {d.name: d for d in DEVICE_TOOL_DEFS}
    for cap in READ_ONLY_FAMILY:
        assert cap in defs, f"{cap} must be a real registered tool"
        # Derived tagging agrees with the policy set (single source).
        assert defs[cap].read_only_auto_allow is True, cap


def test_derived_tag_two_way_parity():
    """Every def carrying the derived tag is a policy member; every
    policy member has a def (no unwidened ghost members later)."""
    defs = {d.name for d in DEVICE_TOOL_DEFS}
    tagged = {d.name for d in DEVICE_TOOL_DEFS if d.read_only_auto_allow}
    assert tagged == READ_ONLY_AUTO_ALLOW_CAPS & defs
    assert READ_ONLY_FAMILY and set(READ_ONLY_FAMILY) <= tagged


# ---------------- B: the relaxation cannot leak ------------------------

def test_never_auto_outranks_read_only_membership(monkeypatch):
    """Regression guard: a read-only member that ever joins the
    never-auto class parks again — read-only membership cannot
    un-park the dangerous class (branch order in decide())."""
    eng = PolicyEngine()
    monkeypatch.setattr(
        "core.policy.NEVER_AUTO_APPROVE_CAPS",
        frozenset(NEVER_AUTO_APPROVE_CAPS | {"gui.find"}))
    d = eng.decide("u", "gui.find", risk="confirm")
    assert d.allow is False
    assert d.requires_approval is True
    assert "never-auto" in d.reason


def test_mislabelled_read_only_needs_no_relabel_and_others_still_park():
    """The allow is membership-based — the tools' CONFIRM labels stay
    untouched (a future relabel is neither the mechanism nor needed),
    and non-members with the same CONFIRM label still park."""
    defs = {d.name: d for d in DEVICE_TOOL_DEFS}
    for cap in READ_ONLY_FAMILY:
        # Still CONFIRM-labelled: the POLICY allows, not a relabel.
        assert defs[cap].risk == RiskLevel.CONFIRM, cap
    eng = PolicyEngine()
    for cap in MUTATING_STILL_PARKED:
        d = eng.decide("u", cap, risk="confirm")
        assert d.allow is False, cap
        assert d.requires_approval is True, cap


def test_deny_patterns_outrank_read_only():
    eng = PolicyEngine()
    d = eng.decide("u", "gui.screen.inspect", resource="shadow",
                   risk="confirm")
    assert d.allow is False
    assert d.hard_deny is True


def test_membership_sets_are_disjoint():
    """No tool is simultaneously 'prompt-free read-only' and 'never
    auto-approve' — the two sets must stay disjoint."""
    assert READ_ONLY_AUTO_ALLOW_CAPS & NEVER_AUTO_APPROVE_CAPS == set()


def test_policy_matrix_snapshot():
    """Documented decision matrix: cap → outcome under Stage 30."""
    eng = PolicyEngine()
    outcomes: dict[str, tuple[bool, bool]] = {}
    for cap in sorted(set(READ_ONLY_FAMILY) | set(MUTATING_STILL_PARKED)):
        d = eng.decide("u", cap, risk="confirm")
        outcomes[cap] = (d.allow, d.requires_approval)
    for cap in READ_ONLY_FAMILY:
        assert outcomes[cap] == (True, False), cap
    for cap in MUTATING_STILL_PARKED:
        assert outcomes[cap] == (False, True), cap
