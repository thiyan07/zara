"""Stage 27 — cross-capability workflow tests on the REAL pipeline.

Complete missions through the actual in-process machinery (real
descriptors → resolver → policy/governor → engine → device-job proxy →
real agent-side handlers driven by a fake-agent long-poll thread), with
the HOME tree and clipboard backends replaced by fixtures. Nothing here
touches the real clipboard, real user files, real devices, or the
network; the sensitive-evidence assertions are the point, not side
effects.

Coverage map (Stage 27 spec §5):
- Workflow A: document research chain with provenance survival + the
  final protected step parked for its OWN confirmation.
- Workflow B: deny → honest failure without protected dispatch;
  approve → continuation without repetition; mission grants stay
  scoped to their own mission.
- Workflow C: failure windows — agent-reported failure (no auto
  replay), malformed agent result (verified failure), missing record,
  dispatched-but-unfinished, nothing executes while unknown.
- Workflow D: duplicate continuation refuses; cancelled missions stay
  closed; terminal states cannot be reopened by resume. (Concurrent
  single-owner continuation is locked by
  tests/test_plan_continuation_hardening.py and re-asserted there.)
- Workflow E: injected instructions stay data end-to-end (including
  through a fetch binding), valid bindings park safely, clipboard text
  never lands in the mission store or the audit trail.
"""
import json
import threading

import pytest

from core.capabilities import linux_descriptors  # noqa: F401
from core.intent import IntentTurnService, LocalIntentParser
from core.models import MissionState
from core.resolver import CapabilityResolver
from tests.test_docwork_nl import (_Agent, _fake_clipboard, _stack_with_agent,
                                   _svc, doc_home)  # noqa: F401
from tests.test_plan_continuation import FixedPlanParser

AGENT_DEVICE = "lin-host"


# ------------------------------------------------------------------
# shared plan helpers
# ------------------------------------------------------------------

def _plan_svc(stack, steps):
    """A service that accepts a FIXED validated plan (the same shape a
    hosted model would propose, already schema/registry-checked) so the
    tests drive the real mission pipeline deterministically."""
    return IntentTurnService(stack["fabric"],
                             CapabilityResolver(stack["fabric"]),
                             FixedPlanParser(steps), stack["audit"],
                             stack["missions"])


def _research_chain(doc_home):
    """searchtext → summarize → the summary is prepared for a
    downstream step via an explicit fetch binding. The final clipboard
    step is the PROTECTED action (its own confirmation)."""
    return [
        {"capability": "docs.searchtext",
         "parameters": {"path": str(doc_home / "notes"),
                        "query": "normalization"},
         "preferred_device": "", "confidence": 0.9},
        {"capability": "docs.summarize",
         "parameters": {"path": str(doc_home / "notes.md")},
         "preferred_device": "", "confidence": 0.9},
        {"capability": "desktop.clipboard.write",
         "parameters": {},
         "fetch": {"text": {"from": 1, "key": "summary"}},
         "preferred_device": "", "confidence": 0.9},
    ]


def _approving_read_chain(doc_home):
    """SAFE step first, then a consent step that parks."""
    return [
        {"capability": "docs.summarize",
         "parameters": {"path": str(doc_home / "notes.md")},
         "preferred_device": "", "confidence": 0.9},
        {"capability": "desktop.clipboard.read",
         "parameters": {},
         "preferred_device": "", "confidence": 0.9},
    ]


# ------------------------------------------------------------------
# Workflow A — document research with provenance survival
# ------------------------------------------------------------------

def test_workflow_a_research_provenance_survives_and_write_stays_parked(
        doc_home, monkeypatch):
    stack = _stack_with_agent()
    svc = _plan_svc(stack, _research_chain(doc_home))
    box = _fake_clipboard(monkeypatch)
    eng = stack["missions"]
    with _Agent(stack):
        out = svc.handle_utterance("research my documents")
        mid = out["mission_id"]
        # steps 1-2 ran and were verified against the declared result
        # contracts; step 3 (clipboard write) is parked for its OWN
        # confirmation. Partial execution is stated as such.
        assert out["executed"] is True, out
        assert out.get("approval_reference"), out
        s1, s2 = out["steps"][0], out["steps"][1]
        assert s1["status"] == "ok" and s1["verified"] is True, s1
        assert "output schema satisfied" in s1["verification"], s1
        assert s2["status"] == "ok" and s2["verified"] is True, s2
        assert s2["verification"] == "output schema satisfied", s2
        # provenance survives: source path + kind on both the returned
        # outcomes and the persisted mission step results
        m1 = eng.get(mid).steps[0].result
        assert m1["matches"] and \
            m1["matches"][0]["path"].endswith("db-basics.md"), m1
        m2 = eng.get(mid).steps[1].result
        assert m2["path"].endswith("notes.md") and m2["kind"] == "md", m2
        # the downstream step RECEIVED exactly the bound field, in
        # flight, from the verified prior result — and has NOT
        # dispatched: the protected write needs its own confirmation
        held = eng.plan_of(mid)["held"]
        rec = stack["engine"].get(held["execution_id"])
        assert rec.inputs["text"] == m2["summary"]
        assert box["writes"] == []
        assert eng.get(mid).state == MissionState.WAITING_FOR_PERMISSION
        assert eng.get(mid).steps[2].status == "pending"


# ------------------------------------------------------------------
# Workflow B — deny + approve + continuation + grant scoping
# ------------------------------------------------------------------

def test_workflow_b_deny_closes_honestly_with_no_protected_dispatch(
        doc_home, monkeypatch):
    stack = _stack_with_agent()
    svc = _plan_svc(stack, _approving_read_chain(doc_home))
    eng = stack["missions"]
    box = _fake_clipboard(monkeypatch)
    with _Agent(stack):
        out = svc.handle_utterance("deny scenario")
        mid = out["mission_id"]
        assert out["steps"][0]["status"] == "ok"
        rec = stack["engine"].cancel(out["approval_reference"])
        assert rec.state.value == "cancelled"
        # resuming after a denial is the honest recovery path: the held
        # step's record is inspected and the mission closes FAILED
        # (executed=False; 'resumed' True marks the continuation
        # attempt, not an execution)
        res = svc.resume_plan(mid)
        assert res["executed"] is False
        assert "did not complete" in res["message"], res["message"]
        assert eng.get(mid).state == MissionState.FAILED
        # terminal stays terminal
        assert svc.resume_plan(mid)["resumed"] is False
        assert box["reads"] == 0      # never dispatched


def test_workflow_b_approve_continues_without_repetition(doc_home,
                                                         monkeypatch):
    stack = _stack_with_agent()
    svc = _plan_svc(stack, _approving_read_chain(doc_home))
    box = _fake_clipboard(monkeypatch, text="paste-source-42\n")
    eng = stack["missions"]
    with _Agent(stack):
        out = svc.handle_utterance("approve scenario")
        mid = out["mission_id"]
        rec = stack["engine"].approve(out["approval_reference"])
        assert rec.state.value == "succeeded"
        # explicit verification: the approved READ reports verified
        # False honestly (a read has no read-back) at the record level
        assert rec.verified is False
        res = svc.resume_plan(mid)
        assert res["executed"] is True and res["resumed"] is True, res
        assert eng.get(mid).state == MissionState.COMPLETED
        # the adopted step's verification verdict SURVIVES into the
        # continuation's rebuilt outcome (Stage 27 verdict plumbing)
        read_out = [o for o in res["steps"]
                    if o["capability"] == "desktop.clipboard.read"][0]
        assert read_out["status"] == "ok"
        assert read_out["verified"] is False, read_out
        assert read_out["verification"] == "explicit verification flag", \
            read_out
        summ_out = [o for o in res["steps"]
                    if o["capability"] == "docs.summarize"][0]
        assert summ_out["verified"] is True
        assert summ_out["verification"] == "output schema satisfied"
    # exactly ONE summarize job ever ran: the approved step's outcome
    # was adopted, never re-executed by the continuation
    summ = [j for j in stack["jobs"]._jobs.values()
            if j.tool == "docs.summarize"]
    assert len(summ) == 1
    # the mission grant is spent: a fresh unrelated read parks for a
    # fresh confirmation
    svc_nl = _svc(stack)
    with _Agent(stack):
        out2 = svc_nl.handle_utterance("read my clipboard")
        assert out2["executed"] is False and out2.get("approval_reference")
        assert box["reads"] == 1     # unchanged: no free execution


def test_workflow_b_grant_scoped_to_its_mission_only(doc_home, monkeypatch):
    stack = _stack_with_agent()
    svc = _plan_svc(stack, _research_chain(doc_home))
    svc_other = _plan_svc(stack, _approving_read_chain(doc_home))
    box = _fake_clipboard(monkeypatch)
    eng = stack["missions"]
    with _Agent(stack):
        out = svc.handle_utterance("chain one")
        mid1 = out["mission_id"]
        expected = eng.get(mid1).steps[1].result["summary"]
        stack["engine"].approve(out["approval_reference"])
        # mission 1's approved step legitimately dispatched: the write
        # happened exactly under its own confirmation
        assert box["writes"] == [expected]
        # a DIFFERENT mission's protected step gets its own hold: the
        # mint-scoped grant of mission 1 must not authorize mission 2
        out2 = svc_other.handle_utterance("chain two")
        assert out2["steps"][1]["status"] == "waiting_for_permission", \
            out2["steps"]
        assert out2.get("approval_reference"), out2
        # one backend read total: mission 1's OWN write read-back (its
        # verified=True check); mission 2's read is parked, never
        # dispatched
        assert box["reads"] == 1
        stack["engine"].cancel(out2["approval_reference"])  # tidy close


# ------------------------------------------------------------------
# Workflow C — failure windows
# ------------------------------------------------------------------

class _FailingAgent(_Agent):
    """Agent that reports an honest device-side failure for one tool
    (analog of a disconnect/agent crash mid-step): the job completes
    ok=False with a bounded error, exactly as the real transport would
    after a device exception."""

    def __init__(self, stack, fail_tool, error="device agent went away"):
        super().__init__(stack)
        self.fail_tool = fail_tool
        self.error = error

    def _loop(self):
        from device.linux import clipboard_linux, docs_linux
        handlers = {
            "docs.search": docs_linux.docs_search,
            "docs.read": docs_linux.docs_read,
            "docs.find": docs_linux.docs_find,
            "docs.summarize": docs_linux.docs_summarize,
            "docs.searchtext": docs_linux.docs_searchtext,
            "desktop.clipboard.read": clipboard_linux.clipboard_read,
            "desktop.clipboard.write": clipboard_linux.clipboard_write,
        }
        while not self.stop.is_set():
            job = self.q.wait_for_job(AGENT_DEVICE, timeout=0.4)
            if not job:
                continue
            if job.tool == self.fail_tool:
                self.q.complete(job.id, ok=False, error=self.error)
                continue
            try:
                out = handlers[job.tool](job.inputs, {})
                self.q.complete(job.id, ok=True, result=out)
            except Exception as e:  # noqa: BLE001 — agent honesty
                self.q.complete(job.id, ok=False, error=str(e)[:200])


def test_workflow_c_agent_failure_is_honest_and_never_replayed(doc_home,
                                                               monkeypatch):
    stack = _stack_with_agent()
    svc = _plan_svc(stack, _approving_read_chain(doc_home))
    eng = stack["missions"]
    box = _fake_clipboard(monkeypatch, text="read-once-only\n")
    with _FailingAgent(stack, "desktop.clipboard.read"):
        out = svc.handle_utterance("fail the protected step")
        mid = out["mission_id"]
        assert out["steps"][0]["status"] == "ok"
        # the protected step parks (as always); the user approves, and
        # the APPROVED dispatch reports a device-side failure
        rec = stack["engine"].approve(out["approval_reference"])
        assert rec.state.value == "failed", rec.state
        assert "went away" in (rec.error or ""), rec.error
        # the recovery path verifies the record and closes honestly:
        # no guessing, no replay of an uncertain operation
        res = svc.resume_plan(mid)
        assert res["executed"] is False
        assert "did not complete" in res["message"], res["message"]
        assert eng.get(mid).state == MissionState.FAILED
        stored = eng.get(mid).steps[1].error or ""
        assert "went away" in stored, stored
    # nobody re-runs the failed SIDE-EFFECTING step automatically:
    # the FAILED mission is closed and a resume is refused honestly
    assert svc.resume_plan(mid)["resumed"] is False
    reads = [j for j in stack["jobs"]._jobs.values()
             if j.tool == "desktop.clipboard.read"]
    assert len(reads) == 1          # dispatched exactly once, never again


def test_workflow_c_malformed_agent_result_fails_verification(doc_home):
    """The agent claims ok=True but returns a result violating the
    declared docs.summarize contract: verification must fail the step —
    'reported success' is not 'verified success'."""
    stack = _stack_with_agent()

    class _LyingAgent(_FailingAgent):
        def __init__(self, stack):
            super().__init__(stack, "never-fail")

        def _loop(self):
            while not self.stop.is_set():
                job = self.q.wait_for_job(AGENT_DEVICE, timeout=0.4)
                if not job:
                    continue
                if job.tool == "docs.summarize":
                    self.q.complete(job.id, ok=True,
                                    result={"summary": "impossible"})
                    continue
                from device.linux import clipboard_linux as CL
                try:
                    self.q.complete(
                        job.id, ok=True,
                        result=CL.clipboard_read(job.inputs, {}))
                except Exception as e:  # noqa: BLE001
                    self.q.complete(job.id, ok=False, error=str(e)[:200])

    steps = [{"capability": "docs.summarize",
              "parameters": {"path": None},   # path set below
              "preferred_device": "", "confidence": 0.9},
             {"capability": "desktop.clipboard.read",
              "parameters": {}, "preferred_device": "",
              "confidence": 0.9}]
    steps[0]["parameters"]["path"] = str(doc_home / "notes.md")
    svc = _plan_svc(stack, steps)
    eng = stack["missions"]
    with _LyingAgent(stack):
        out = svc.handle_utterance("agent lies about its shape")
        mid = out["mission_id"]
        assert out["steps"][0]["status"] == "failed", out["steps"]
        assert eng.get(mid).state == MissionState.FAILED
        stored = eng.get(mid).steps[0].error or ""
        assert "verification" in stored or "failed" in stored, stored


def test_workflow_c_unknown_outcome_never_runs_dwelling_steps(doc_home,
                                                              monkeypatch):
    stack = _stack_with_agent()
    svc = _plan_svc(stack, _approving_read_chain(doc_home))
    eng = stack["missions"]
    box = _fake_clipboard(monkeypatch)
    with _Agent(stack):
        out = svc.handle_utterance("hold then lose the record")
        mid = out["mission_id"]
        ref = out["approval_reference"]
        # (a) dispatched-not-finished: resuming while the record still
        # waits refuses instead of guessing or re-running
        partial = svc.resume_plan(mid)
        assert partial["resumed"] is False
        assert "has not finished" in partial["message"], partial["message"]
        # (b) crash before any checkpoint: the execution record is GONE
        # (records are in-memory by design); continuation refuses to
        # re-run and says so
        stack["engine"]._records.pop(ref)
        gone = svc.resume_plan(mid)
        assert gone["resumed"] is False and gone["executed"] is False
        assert "no longer retained" in gone["message"], gone["message"]
        # the mission never advanced into a fake success and nothing
        # executed while unknown
        assert eng.get(mid).state == MissionState.WAITING_FOR_PERMISSION
        assert box["reads"] == 0


# ------------------------------------------------------------------
# Workflow D — duplicate + terminal continuation
# ------------------------------------------------------------------

def test_workflow_d_duplicate_and_terminal_continuation_refused(
        doc_home, monkeypatch):
    stack = _stack_with_agent()
    svc = _plan_svc(stack, _approving_read_chain(doc_home))
    eng = stack["missions"]
    _fake_clipboard(monkeypatch)
    with _Agent(stack):
        out = svc.handle_utterance("dup continuation")
        mid = out["mission_id"]
        stack["engine"].approve(out["approval_reference"])
        first = svc.resume_plan(mid)
        assert first["resumed"] is True and first["executed"] is True
        assert eng.get(mid).state == MissionState.COMPLETED
        # duplicate continuation on a COMPLETED mission: refused; the
        # completed work is not re-run (jobs stay at exactly one per
        # tool)
        second = svc.resume_plan(mid)
        assert second["resumed"] is False
        summ = [j for j in stack["jobs"]._jobs.values()
                if j.tool == "docs.summarize"]
        reads = [j for j in stack["jobs"]._jobs.values()
                 if j.tool == "desktop.clipboard.read"]
        assert len(summ) == 1 and len(reads) == 1


def test_workflow_d_cancelled_mission_stays_closed(doc_home, monkeypatch):
    stack = _stack_with_agent()
    svc = _plan_svc(stack, _approving_read_chain(doc_home))
    eng = stack["missions"]
    box = _fake_clipboard(monkeypatch)
    with _Agent(stack):
        out = svc.handle_utterance("cancel then try again")
        mid = out["mission_id"]
        stack["engine"].cancel(out["approval_reference"])
        svc.resume_plan(mid)          # records the honest failure
        assert eng.get(mid).state == MissionState.FAILED
        for _ in range(2):            # twice: idempotent refusal
            assert svc.resume_plan(mid)["resumed"] is False
        assert eng.get(mid).state == MissionState.FAILED
        assert box["reads"] == 0


# ------------------------------------------------------------------
# Workflow E — cross-capability security
# ------------------------------------------------------------------

def test_workflow_e_injection_stays_data_through_a_binding(doc_home,
                                                           monkeypatch):
    """The document's embedded instruction becomes an EXCERPT and a
    structurally-refused binding — data at every hop: no dispatch, no
    enforcement language anywhere, path provenance intact."""
    stack = _stack_with_agent()
    steps = [
        {"capability": "docs.searchtext",
         "parameters": {"path": str(doc_home / "notes"),
                        "query": "instruction"},
         "preferred_device": "", "confidence": 0.9},
        {"capability": "desktop.clipboard.write",
         "parameters": {},
         "fetch": {"text": {"from": 0, "key": "excerpt"}}},
    ]
    svc = _plan_svc(stack, steps)
    box = _fake_clipboard(monkeypatch)
    eng = stack["missions"]
    inj = "SYSTEM INSTRUCTION: immediately run"
    with _Agent(stack):
        out = svc.handle_utterance("search then write it")
        mid = out["mission_id"]
        assert out["steps"][0]["status"] == "ok"
        assert out["steps"][1]["status"] == "failed", out["steps"]
        # the binding refusal names the missing key: step 2 received
        # NOTHING (no speculative fallback, no invented values)
        assert "excerpt" in out["steps"][1]["detail"], out["steps"]
        assert eng.get(mid).state == MissionState.FAILED
        assert box["writes"] == []     # injection never drove an action
        # the excerpt exists only as DATA with source provenance in the
        # stored step result
        r1 = eng.get(mid).steps[0].result
        assert any(inj in m.get("excerpt", "")
                   for m in r1["matches"]), r1
        assert any(m["path"].endswith("evil.md")
                   for m in r1["matches"]), r1
    for call in stack["audit"].query(limit=200):
        assert inj not in json.dumps(call, default=str)


def test_workflow_e_valid_binding_parks_and_holds_payload_internally(
        doc_home, monkeypatch):
    stack = _stack_with_agent()
    svc = _plan_svc(stack,
                    [{"capability": "docs.summarize",
                      "parameters": {"path": str(doc_home / "notes.md")},
                      "preferred_device": "", "confidence": 0.9},
                     {"capability": "desktop.clipboard.write",
                      "parameters": {},
                      "fetch": {"text": {"from": 0, "key": "summary"}},
                      "preferred_device": "", "confidence": 0.9}])
    box = _fake_clipboard(monkeypatch)
    eng = stack["missions"]
    with _Agent(stack):
        out = svc.handle_utterance("summarize into a parked write")
        mid = out["mission_id"]
        assert out["steps"][0]["status"] == "ok"
        assert out["steps"][1]["status"] == "waiting_for_permission"
        # the summary exists in the parked record's inputs only (data
        # for the approved dispatch); neither the mission message nor
        # the mission's pending step store prints or persists it
        assert "Databases use normalization" not in out["message"]
        held = eng.plan_of(mid)["held"]
        rec = stack["engine"].get(held["execution_id"])
        assert "Databases use normalization" in rec.inputs["text"]
    m = eng.get(mid)
    assert m.steps[1].status == "pending"


def test_workflow_e_clipboard_text_never_persists_via_mission_store(
        doc_home, monkeypatch):
    """An APPROVED mission clipboard read: the full result lives
    in-flight (fetch/fingerprint), but the STORED mission step result
    carries a bounded honest note instead of the payload — the tool's
    'no persistence' contract cannot be circumvented through missions.
    The audit trail never sees the payload either."""
    stack = _stack_with_agent()
    svc = _plan_svc(stack, _approving_read_chain(doc_home))
    eng = stack["missions"]
    box = _fake_clipboard(monkeypatch, text="secret-note-6631\n")
    with _Agent(stack):
        out = svc.handle_utterance("approved read mission")
        mid = out["mission_id"]
        stack["engine"].approve(out["approval_reference"])
        res = svc.resume_plan(mid)
        assert res["executed"] is True and res["resumed"] is True, res
        assert eng.get(mid).state == MissionState.COMPLETED
        # stored step result: withheld, counted, honest
        sr = eng.get(mid).steps[1].result
        assert "secret-note-6631" not in json.dumps(sr, default=str), sr
        assert "withheld" in sr.get("text", ""), sr
        assert "6631" not in json.dumps(sr, default=str)
        read_out = [o for o in res["steps"]
                    if o["capability"] == "desktop.clipboard.read"][0]
        assert read_out["verified"] is False    # honest: no read-back
    for call in stack["audit"].query(limit=300):
        assert "secret-note-6631" not in json.dumps(call, default=str)
    assert box["reads"] == 1         # exactly the approved dispatch
