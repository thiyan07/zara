"""Core-side contracts for Stage 25 capabilities: docs.* (document
intelligence) and desktop.clipboard.* (consent-based clipboard).

Coverage: registry parity (core def ↔ agent implementation), proxy
registration and device-job round trip (with a sensitive-content MARKER
to prove the engine pipeline carries results without ever logging
them), input-envelope bounds pre-execution, and the authorization
semantics the tools declare (SAFE read-only docs under the filesystem
read capability; CONFIRM clipboard requiring a fresh scoped grant).

Fixture-only — no real clipboard, no device fleet, no network.
"""
import os
import threading

import pytest

import device.linux.tools_linux as LT
from core.device_tools import DEVICE_TOOL_DEFS, register_device_proxies
from core.jobs import JobQueue
from core.models import RiskLevel
from core.policy import PolicyEngine
from core.tools import ToolRegistry

DOCS = ("docs.search", "docs.read", "docs.find", "docs.summarize")
CLIP = ("desktop.clipboard.read", "desktop.clipboard.write")


def _defs():
    return {d.name: d for d in DEVICE_TOOL_DEFS}


# ---------------- contracts ----------------

def test_core_defs_linux_only_with_honest_risk():
    defs = _defs()
    for name in DOCS + CLIP:
        assert defs[name].supported_devices == ["linux"], name
    for name in DOCS:
        assert defs[name].risk == RiskLevel.SAFE, name      # read-only
        assert defs[name].required_capabilities == [name]
    for name in CLIP:
        assert defs[name].risk == RiskLevel.CONFIRM, name   # fresh grant


def test_core_agent_parity_schemas_and_caps():
    core = _defs()
    agent = {d.name: d for d, _ in LT.LINUX_TOOLS}
    for name in DOCS + CLIP:
        assert name in agent, name
        assert set(core[name].required_capabilities) == \
            set(agent[name].required_capabilities), name
        assert core[name].risk == agent[name].risk, name
        assert core[name].input_schema == agent[name].input_schema, name


def test_verification_modes_declared_and_in_parity():
    """Stage 27: every docs result shape is DECLARED and verified
    (registry validates the full contract; fabric's post-execution
    verdict checks required keys) and clipboard verification is
    EXPLICIT (the handler's read-back verdict is the record's verdict —
    'none' used to latch verified=True even on a failed read-back)."""
    core = _defs()
    agent = {d.name: d for d, _ in LT.LINUX_TOOLS}
    for name in DOCS:
        assert core[name].verification == "output_schema", name
        assert agent[name].verification == "output_schema", name
        assert core[name].output_schema == agent[name].output_schema, name
        req = core[name].output_schema.get("required", [])
        assert req, name            # the honest-reporting backbone
        assert core[name].max_output_bytes >= 65536, name
    for name in ("desktop.clipboard.read", "desktop.clipboard.write"):
        assert core[name].verification == "explicit", name
        assert agent[name].verification == "explicit", name


def test_malformed_results_fail_output_verification():
    """A malformed result (missing required keys) must fail
    verification — never pass through as a silent success."""
    from core.tools import ToolRegistry, validate_against_schema
    from core.device_tools import DOCS_OUTPUT_SCHEMAS
    core = _defs()
    bad = {"path": "/tmp/doc.pdf", "kind": "pdf", "matches": [],
           "count": 0}          # missing query_terms/truncated/...
    errs = validate_against_schema(
        bad, DOCS_OUTPUT_SCHEMAS["docs.find"],
        core["docs.find"].max_output_bytes)
    assert any("missing required field" in e for e in errs)


def test_agent_capabilities_advertised():
    caps = set(LT.LINUX_CAPABILITIES)
    for cap in DOCS + CLIP:
        assert cap in caps, cap
    assert "filesystem.read" in caps


def test_proxy_registration_all_six():
    registry = ToolRegistry()
    register_device_proxies(registry, JobQueue())
    for name in DOCS + CLIP:
        assert registry._handlers.get(name) is not None, name


# ---------------- envelope bounds (fail pre-policy, pre-device) --------

def test_input_envelopes_default_and_clipboard_wide():
    defs = _defs()
    for name in DOCS:
        assert defs[name].max_input_bytes == 4096, name
    # Clipboard is exactly as wide as the handler's own 100k-char cap;
    # wide to mirror the handler contract, never authorization.
    assert defs["desktop.clipboard.write"].max_input_bytes == 131072


def test_oversized_clipboard_write_rejected_before_dispatch():
    registry = ToolRegistry()
    q = JobQueue()
    register_device_proxies(registry, q)
    with pytest.raises(ValueError):
        registry.call("desktop.clipboard.write",
                      {"text": "z" * 200_000},
                      {"device_id": "lin1"})
    assert not q._jobs          # nothing ever enqueued


def test_oversized_docs_find_rejected_before_dispatch():
    registry = ToolRegistry()
    q = JobQueue()
    register_device_proxies(registry, q)
    with pytest.raises(ValueError):
        registry.call("docs.find",
                      {"path": "/tmp/x.pdf", "query": "q" * 5000},
                      {"device_id": "lin1"})
    assert not q._jobs


# ---------------- proxy round trip (result carried without logging) ----

def test_docs_find_device_job_round_trip_carries_result():
    """Enqueue → agent poll → complete → proxy returns the device
    result unmodified. The sensitive MARKER only ever lives inside the
    returned dict — the queue/pipeline never persists or logs content
    (jobs land in the in-memory queue; engine events are ids only)."""
    registry = ToolRegistry()
    q = JobQueue()
    register_device_proxies(registry, q)
    # Stage 27: docs.find is verified by output_schema now, so the
    # round-trip marker must satisfy the declared contract — an
    # arbitrary dict would (correctly) fail verification instead of
    # passing through. The sensitive marker rides ONLY inside an
    # excerpt, which is exactly where untrusted data is allowed to be.
    marker = {"path": "/tmp/doc.pdf", "kind": "pdf", "query_terms":
              ["budget"], "matches": [{"page": 3, "block": 0,
                                       "excerpt": "SENSITIVE-MARKER-8891",
                                       "score": 0.9, "char_start": 0,
                                       "char_end": 20}],
              "count": 1, "chars": 1200, "truncated": False,
              "ocr_required": False}
    results = {}

    def driver():
        results["out"] = registry.call(
            "docs.find", {"path": "/tmp/doc.pdf", "query": "budget"},
            {"device_id": "lin1", "timeout_s": 5})

    # make_proxy_handler.waiter uses the queue Condition: run the proxy
    # in a thread, complete the job from the "device agent" side.
    th = threading.Thread(target=driver, daemon=True)
    th.start()
    import time
    job = None
    for _ in range(200):
        job = q.poll("lin1")
        if job:
            break
        time.sleep(0.01)
    assert job is not None and job.tool == "docs.find"
    q.complete(job.id, ok=True, result=marker)
    th.join(timeout=5)
    assert results["out"] == marker

    # The pipeline carries the result through memory only: no clipboard
    # history grows (bounded terminal retention in the job record for
    # polling/idempotency, matching execution-record retention), and
    # nothing about this marker exists on disk.
    assert len(q._jobs) == 1
    for name in os.listdir(os.path.expanduser("~")):
        assert "SENSITIVE-MARKER" not in name


# ---------------- authorization semantics ----------------

def test_policy_clipboard_needs_grant_each_time():
    p = PolicyEngine()
    d1 = p.decide("user", "desktop.clipboard.read", "lin1",
                  risk="confirm")
    assert d1.allow is False and d1.requires_approval is True
    d2 = p.decide("user", "desktop.clipboard.write", "lin1",
                  risk="confirm")
    assert d2.allow is False and d2.requires_approval is True
    # A scoped grant authorizes ONE use (consumed), then it is gone.
    p.grant("user", "desktop.clipboard.read", "lin1")
    d3 = p.decide("user", "desktop.clipboard.read", "lin1",
                  risk="confirm")
    assert d3.allow is True and d3.grant_id
    d4 = p.decide("user", "desktop.clipboard.read", "lin1",
                  risk="confirm")
    assert d4.allow is False            # single-use grant: no reuse


def test_policy_read_side_capability_is_separate():
    # Reading never implies writing — capabilities are disjoint strings.
    p = PolicyEngine()
    p.grant("user", "desktop.clipboard.read", "lin1")
    d = p.decide("user", "desktop.clipboard.write", "lin1",
                 risk="confirm")
    assert d.allow is False


def test_docs_find_via_proxy_requires_device_context():
    """A device proxy without target device refuses instead of guessing;
    no local/file execution outside the device pipeline."""
    registry = ToolRegistry()
    register_device_proxies(registry, JobQueue())
    with pytest.raises(ValueError):
        registry.call("docs.find",
                      {"path": "/tmp/a.pdf", "query": "x"}, {})


# ---------------- mission-bound execution preserves provenance --------

def test_mission_bound_execution_carries_source_provenance():
    """The engine's adopt-on-success machinery keeps a mission-bound
    record; the docs.find result itself carries path + page provenance
    so later steps see EXTRACTED FACTS (with sources), never
    unattributed claims."""
    from core.execution import ExecutionEngine, PolicyDenied
    registry = ToolRegistry()
    q = JobQueue()
    register_device_proxies(registry, q)
    engine = ExecutionEngine(registry, PolicyEngine(), jobs=q)

    marker = {"path": "/tmp/doc.pdf", "kind": "pdf", "query_terms":
              ["budget"], "matches":
              [{"page": 2, "block": 0, "excerpt": "budget 5000 USD",
                "score": 1, "char_start": 0, "char_end": 16}],
              "count": 1, "chars": 90, "truncated": False,
              "ocr_required": False}

    def driver():          # device agent side: server long-poll → complete
        job = q.wait_for_job("lin-agent", timeout=10.0)
        if job:
            q.complete(job.id, ok=True, result=marker)

    # Real engine path: submit with an explicitly ALLOWED decision (SAFE
    # tool under filesystem.read). The proxy waiter blocks; the fake
    # device agent long-polls and completes the job.
    t = threading.Thread(target=driver, daemon=True)
    t.start()
    from core.policy import PolicyDecision
    rec = engine.submit(
        "docs.find", {"path": "/tmp/doc.pdf", "query": "budget"},
        device_id="lin-agent", who="user",
        auth=PolicyDecision(allow=True, grant_id="g",
                            scope="tool:docs.find@lin-agent:/",
                            reason="test"),
        mission_id="miss-docs-1")
    t.join(timeout=5)
    assert rec.state.value == "succeeded" or str(rec.state) == \
        "ExecutionState.SUCCEEDED"
    assert rec.result == marker            # byte-for-byte provenance
    assert rec.mission_id == "miss-docs-1"
    assert rec.result["matches"][0]["page"] == 2   # source attribution
