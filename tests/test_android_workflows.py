"""Stage 28: Android automation + evidence-verified mission surface.

End-to-end on an in-process stack (mirrors tests/test_docwork_nl.py's
harness): utterance → deterministic parser → resolver → policy/engine
(device-job proxy) → REAL agent-side handlers against an in-memory a11y
device double → verification → mission/evidence bookkeeping → message.

Fixture-only: the phone is a fake a11y world, adb is faked at the
module seams (device.linux.android_adb_linux._detect/_list), no real
device, no notification, no screen capture. UI text/labels are treated
as untrusted observation data everywhere.
"""
import threading

import pytest

from core.device_tools import GUI_LAB_TARGETS
from core.intent import IntentTurnService, LocalIntentParser
from core.models import MissionState
from core.resolver import CapabilityResolver
from tests.test_docwork_nl import _last_job, _svc, _stack_with_agent, \
    _Agent  # noqa: F401
from tests.test_intent import enroll_claim, make_stack, register
from tests.test_plan_continuation import FixedPlanParser

AGENT_DEVICE = "lin-host"
ANDROID_DEVICE = "emu28"
LAB = "dev.zara.zara_android"


# ------------------------------------------------------------------
# in-memory a11y device double
# ------------------------------------------------------------------

class _A11yWorld:
    """Bounded fake accessibility surface: elements + a click log. The
    matched node label is untrusted OBSERVATION data; nothing here can
    execute anything and nothing is logged by itself."""

    def __init__(self, elements=None):
        self.package = LAB
        self.elements = list(elements or [])
        self.taps: list[int] = []
        self.typings: list[tuple[int, str]] = []      # (node, text)
        self.navigation: list[str] = []
        self.n = 0

    def _snap(self, elements):
        self.n += 1
        return {"snapshot_id": f"snap{self.n:04d}",
                "package_name": self.package,
                "screen_w": 1080, "screen_h": 2400,
                "elements": [dict(e) for e in elements],
                "supported": True}

    def inspect(self, options=None):
        options = options or {}
        got = self._snap(self.elements)
        return got

    def find(self, pkg, target, options=None):
        self.n += 1
        text = (target or {}).get("text", "")
        node = next((e for e in self.elements
                     if text and e.get("text") == text), None)
        if node is None:
            return None
        return {"status": "MATCH", "package": pkg, "element": node,
                "snapshot_id": f"snap{self.n:04d}"}

    def wait(self, pkg, target, timeout_s=5.0, options=None):
        """Bounded wait mirroring the Dart runner: hard attempt/total
        bound, fixed interval, honest matched/timeout verdicts."""
        n = self.n
        attempts = 0
        deadline_end = n + max(1, int(min(timeout_s, 5.0)))  # attempt cap
        while attempts < deadline_end:
            attempts += 1
            hit = self.find(pkg, target, options)
            if hit:
                return {"package": pkg, "matched": True,
                        "status": "matched", "attempts": attempts,
                        "waited_ms": attempts * 10,
                        "element": hit["element"],
                        "snapshot_id": hit["snapshot_id"],
                        "verified": True}
        return {"package": pkg, "matched": False, "status": "timeout",
                "attempts": attempts, "waited_ms": attempts * 10,
                "verified": False,
                "note": "element never appeared within the bounded wait "
                        "(read-only observation, nothing acted on)"}

    def tap(self, pkg, node_id, options=None):
        node = next((e for e in self.elements
                     if e.get("node_id") == node_id), None)
        if node is None:
            return {"package": pkg, "error": "NODE_MISSING", "applied": False}
        hits = [e for e in self.elements
                if e.get("node_id") == node_id]
        self.taps.append(node_id)
        return {"package": pkg, "applied": True, "verify_state": "clicked",
                "snapshot_id": f"snap{self.n:04d}", "verified": True}

    def set_text(self, pkg, node_id, text, options=None):
        self.typings.append((node_id, text))
        return {"package": pkg, "applied": True,
                "verify_state": "text_matched", "text_matched": True,
                "verified": True}

    def navigate(self, direction, pkg, options=None):
        self.navigation.append(direction)
        return {"package": pkg, "applied": True, "direction": direction,
                "package_before": pkg,
                # Stage 29 explicit-verification mirror of the Dart
                # runner: back is confirmed by a FRESH post-navigation
                # snapshot (package_after from a re-read foreground).
                "package_after": pkg,
                "fresh_snapshot_id": f"snap{self.n:04d}",
                "verified": True}


class _AndroidAgent:
    """Fake paired phone agent: long-polls the queue and executes gui.*
    jobs against the a11y double with the Dart runner's result shapes
    (honest refusals for anything the family refuses)."""

    class _Refused(Exception):
        pass

    def __init__(self, stack, world: _A11yWorld):
        self.q = stack["jobs"]
        self.w = world
        self.stop = threading.Event()
        self.handled: list[str] = []
        self.refuse_acting = False
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self.stop.is_set():
            job = self.q.wait_for_job(ANDROID_DEVICE, timeout=0.4)
            if not job:
                continue
            self.handled.append(job.tool)
            try:
                self.q.complete(job.id, ok=True,
                                result=self._handle(job.tool, job.inputs))
            except self._Refused as e:
                self.q.complete(job.id, ok=False, error=str(e)[:200])
            except Exception as e:  # noqa: BLE001 — honest agent errors
                self.q.complete(job.id, ok=False, error=str(e)[:200])

    def _handle(self, tool, inputs):
        w = self.w
        pkg = inputs.get("target_package") or inputs.get("package") \
            or w.package
        if tool == "gui.screen.inspect":
            return w.inspect(inputs)
        if tool == "gui.wait":
            return w.wait(pkg, inputs.get("target"),
                          float(inputs.get("timeout_s") or 5.0),
                          inputs)
        if tool == "gui.find":
            fm = w.find(pkg, inputs.get("target"), inputs)
            if fm is None:
                raise self._Refused("no element matches the target "
                                    "(refused, never guessed)")
            return fm
        if tool in ("gui.tap", "gui.set_text", "gui.back"):
            if self.refuse_acting or pkg not in GUI_LAB_TARGETS:
                raise self._Refused(
                    "target not in lab allowlist (refused, never executed)")
            if tool == "gui.tap":
                return w.tap(pkg, int(inputs.get("node_id", -1)), inputs)
            if tool == "gui.set_text":
                return w.set_text(pkg, int(inputs.get("node_id", -1)),
                                  str(inputs.get("text", "")), inputs)
            return w.navigate(str(inputs.get("direction", "back")),
                              pkg, inputs)
        raise self._Refused(f"tool {tool} not implemented in the fake agent")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)
        return False


class _LinuxAgent(_Agent):
    """docwork's fake linux agent + the Stage 28 system.android.inventory
    handler (REAL agent-side code, faked adb seams)."""

    def _loop(self):
        from device.linux import android_adb_linux, clipboard_linux, \
            docs_linux
        handlers = {
            "system.android.inventory": android_adb_linux.android_devices,
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
            self.handled.append(job.tool)
            try:
                out = handlers[job.tool](job.inputs, {})
                self.q.complete(job.id, ok=True, result=out)
            except KeyError:
                self.q.complete(
                    job.id, ok=False,
                    error=f"tool {job.tool} not in agent's demonstrator")
            except Exception as e:  # noqa: BLE001 — agent honesty
                self.q.complete(job.id, ok=False, error=str(e)[:200])


def _android_stack(world, dev=ANDROID_DEVICE):
    stack = make_stack()
    enroll_claim(stack, dev)
    register(stack, dev)
    stack["fabric"].advertise([
        {"id": "gui.screen.inspect", "name": "gui.screen.inspect",
         "descriptor_version": "1", "version": "1.0.0", "risk": "confirm",
         "availability": "available"},
        {"id": "gui.wait", "name": "gui.wait", "descriptor_version": "1",
         "version": "1.0.0", "risk": "confirm", "availability": "available"},
        {"id": "gui.find", "name": "gui.find", "descriptor_version": "1",
         "version": "1.0.0", "risk": "confirm", "availability": "available"},
        {"id": "gui.tap", "name": "gui.tap", "descriptor_version": "1",
         "version": "1.0.0", "risk": "high_risk", "availability": "available"},
        {"id": "gui.back", "name": "gui.back", "descriptor_version": "1",
         "version": "1.0.0", "risk": "high_risk", "availability": "available"},
    ], dev)
    return stack


def _fake_adb(monkeypatch, *, ok=True, devices=None, reason=""):
    """Fake the adb seams of the Linux-side inventory module. Never
    runs a real adb binary."""
    import device.linux.android_adb_linux as M

    monkeypatch.setattr(M, "_detect", lambda: {"ok": True, "path": "/x/adb"})
    monkeypatch.setattr(M, "_list", lambda: {
        "ok": ok, "devices": list(devices or []), "stage": "done",
        "reason": reason})
    return M


# ------------------------------------------------------------------
# host-side inventory (system.android.inventory)
# ------------------------------------------------------------------

def test_android_devices_routes_end_to_end_with_a_phone(monkeypatch):
    M = _fake_adb(monkeypatch, devices=[
        {"serial": "ABC123", "state": "device", "model": "Pixel_6"},
    ])
    stack = _stack_with_agent()
    svc = _svc(stack)
    with _LinuxAgent(stack):
        out = svc.handle_utterance("inspect my connected android phone")
    assert out["executed"] is True, out
    assert "1 Android device(s)" in out["message"], out["message"]
    assert "ABC123" in out["message"] and "device" in out["message"]
    job = _last_job(stack)
    assert job is not None and job.tool == "system.android.inventory"
    assert job.result["adb"] is True and job.result["count"] == 1
    assert job.result["devices"][0]["serial"] == "ABC123"
    assert job.result["stage"] == "done"


def test_android_devices_absent_abd_and_empty_phone_are_honest(
        monkeypatch):
    import device.linux.android_adb_linux as M
    monkeypatch.setattr(M, "_detect", lambda: {
        "ok": False, "reason": "adb not found on PATH"})
    monkeypatch.setattr(M, "_list", lambda: {"ok": False, "devices": [],
                                             "stage": "detection"})
    stack = _stack_with_agent()
    svc = _svc(stack)
    with _LinuxAgent(stack):
        out = svc.handle_utterance("list connected android devices")
    assert out["executed"] is True
    assert "unavailable" in out["message"], out["message"]
    job = _last_job(stack)
    assert job.result["adb"] is False and job.result["count"] == 0
    assert "not found" in job.result["reason"]

    # adb present, nothing attached: an honest zero, never a hang.
    M2 = _fake_adb(monkeypatch)
    with _LinuxAgent(stack):
        out2 = svc.handle_utterance("android devices attached")
    assert out2["executed"] is True
    assert "no Android device is currently attached" in out2["message"]
    job2 = _last_job(stack)
    assert job2.result["adb"] is True and job2.result["count"] == 0
    assert "currently attached" in job2.result["note"]


def test_gui_wait_matches_and_reports_named_element():
    world = _A11yWorld([
        {"node_id": 2, "text": "Done", "cls": "android.widget.Button"},
    ])
    stack = _android_stack(world)
    svc = _svc(stack)
    with _AndroidAgent(stack, world):
        # Stage 30: gui.wait is READ-ONLY auto-allow — it runs directly
        # (no approval park; the parking matrix is pinned in
        # test_s30_read_only_allow). The match must still be reported
        # as EVIDENCE only, never an acted change.
        out = svc.handle_utterance(
            f'wait until "Done" appears on {LAB}')
        assert out["executed"] is True, out
        assert '"Done" appeared' in out["message"], out["message"]
        assert "nothing was tapped" in out["message"]
    job = [j for j in stack["jobs"]._jobs.values() if j.tool == "gui.wait"][0]
    assert job.result["matched"] is True and job.result["verified"] is True
    assert len(stack["jobs"]._jobs) == 1   # wait dispatched exactly once
    assert world.taps == []               # wait never acted


def test_gui_wait_timeout_is_an_honest_not_seen():
    world = _A11yWorld([
        {"node_id": 1, "text": "Other", "cls": "android.widget.TextView"},
    ])
    stack = _android_stack(world)
    svc = _svc(stack)
    with _AndroidAgent(stack, world):
        # Stage 30 auto-allow: the bounded wait runs directly and the
        # no-match verdict is honest data (not seen, not guessed).
        out = svc.handle_utterance(
            f'wait until "Done" appears on {LAB} within 1')
        assert out["executed"] is True, out
        assert "never appeared" in out["message"], out["message"]
    job = [j for j in stack["jobs"]._jobs.values() if j.tool == "gui.wait"][0]
    assert job.result["matched"] is False
    assert job.result["verified"] is False
    assert "never appeared" in job.result["note"], job.result


def test_gui_wait_false_positives_are_rejected_not_guessed():
    stack = _stack_with_agent()        # linux stack: gui.wait unknown
    svc = _svc(stack)
    out = svc.handle_utterance('wait until "Done" appears on my phone')
    assert out["executed"] is False    # unknown surface never executes

    world = _A11yWorld([{"node_id": 1, "text": "Done", "cls": "B"}])
    stack2 = _android_stack(world)
    svc2 = _svc(stack2)
    with _AndroidAgent(stack2, world):
        # no quoted payload → no wait proposal, honest clarification
        a = svc2.handle_utterance(f"wait for it to appear on {LAB}")
        assert a["executed"] is False
        # no lab package → clarification, never a guessed package
        b = svc2.handle_utterance('wait until "Done" appears')
        assert b["executed"] is False
        # unrelated phrases keep their own routes
        c = svc2.handle_utterance("wait for my reminder")
        assert c.get("proposal", {}).get("capability") != "gui.wait"


def test_tap_denial_prevents_dispatch_and_approval_runs_once():
    world = _A11yWorld([
        {"node_id": 3, "text": "Send", "cls": "android.widget.Button"},
    ])
    stack = _android_stack(world)
    with _AndroidAgent(stack, world):
        out = stack["fabric"].execute_on_device(
            "gui.tap", {"target_package": LAB, "node_id": 3},
            ANDROID_DEVICE, "u0")
        # HIGH_RISK + RESTRICTED → an approval HOLD, never dispatch
        assert out["stage"] != "succeeded" and out.get("execution_id")
        assert world.taps == [] and not stack["jobs"]._jobs
        deny = stack["engine"].cancel(out["execution_id"])
        assert deny.state.value == "cancelled"
        assert world.taps == []            # denial prevented dispatch

        # approval path: mint → dispatch happens once → applied truth
        out2 = stack["fabric"].execute_on_device(
            "gui.tap", {"target_package": LAB, "node_id": 3},
            ANDROID_DEVICE, "u0")
        ref2 = out2["approval_reference"] \
            if out2.get("approval_reference") else out2["execution_id"]
        rec = stack["engine"].approve(ref2)
        assert rec.state.value == "succeeded"
    jobs = [j for j in stack["jobs"]._jobs.values() if j.tool == "gui.tap"]
    assert len(jobs) == 1 and world.taps == [3]
    assert jobs[0].result["applied"] is True
    assert jobs[0].result["verify_state"] == "clicked"


def test_tap_refusal_carries_actionable_evidence():
    world = _A11yWorld([{"node_id": 3, "text": "Send"}])
    stack = _android_stack(world)
    agent = _AndroidAgent(stack, world)
    agent.refuse_acting = True            # on-device allowlist refusal
    with agent:
        out = stack["fabric"].execute_on_device(
            "gui.tap", {"target_package": LAB, "node_id": 3},
            ANDROID_DEVICE, "u0")
        ref = out["approval_reference"] \
            if out.get("approval_reference") else out["execution_id"]
        rec = stack["engine"].approve(ref)
        assert rec.state.value == "failed", rec
        # the refusal reason (actionable, name the allowlist) survives
        assert "allowlist" in (rec.error or "") or \
            "allowlist" in repr(rec.result)


def test_non_lab_package_never_enters_the_queue():
    stack = _android_stack(_A11yWorld())
    out = stack["fabric"].execute_on_device(
        "gui.tap", {"target_package": "com.whatsapp", "node_id": 1},
        ANDROID_DEVICE, "u0")
    # The ap-approval hold parks first (policy, Core); the record is not
    # dispatchable yet and nothing was queued.
    assert out["stage"] == "waiting_for_permission"
    assert not stack["jobs"]._jobs
    # Even an (impossible) approval cannot skirt the input contract: the
    # enum allowlist refuses before dispatch, honestly, forever.
    stack["engine"].approve(out["execution_id"])
    jobs = [j for j in stack["jobs"]._jobs.values() if j.tool == "gui.tap"]
    assert jobs == []


def test_mission_two_step_preserves_evidence_through_approval_resume():
    world = _A11yWorld([{"node_id": 2, "text": "Done", "cls": "B"}])
    stack = _android_stack(world)
    eng = stack["missions"]
    steps = [
        {"capability": "gui.wait",
         "parameters": {"target_package": LAB,
                        "target": {"text": "Done"}},
         "preferred_device": ANDROID_DEVICE, "confidence": 0.9},
        {"capability": "gui.tap",
         "parameters": {"target_package": LAB, "node_id": 2},
         "preferred_device": ANDROID_DEVICE, "confidence": 0.9},
    ]
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FixedPlanParser(steps), stack["audit"],
                            stack["missions"])
    with _AndroidAgent(stack, world):
        out = svc.handle_utterance("android pair mission")
        mid = out["mission_id"]
        # Stage 30: step 1 (gui.wait) is read-only auto-allow — it RAN
        # directly with its evidence recorded inline; the plan pauses at
        # step 2 (gui.tap), whose approval gate the mission adopts.
        ev1 = [o for o in out["steps"]
               if o["capability"] == "gui.wait"][0]["evidence"]
        assert ev1["matched"] == "True" and ev1["capability"] == "gui.wait"
        assert out.get("approval_reference")
        stack["engine"].approve(out["approval_reference"])
        res = svc.resume_plan(mid)
        assert res["executed"] is True and res["resumed"] is True, res
    assert eng.get(mid).state == MissionState.COMPLETED
    assert world.taps == [2]
    # exactly one tap job ever dispatched; step-1 evidence survives
    taps = [j for j in stack["jobs"]._jobs.values() if j.tool == "gui.tap"]
    assert len(taps) == 1
    verdicts = eng.plan_of(mid)["verdicts"]
    v0 = verdicts.get("0", {})
    assert v0.get("evidence", {}).get("matched") == "True"
    prior = [o for o in res["steps"]
             if o["capability"] == "gui.wait"][0]
    assert prior["verified"] is True
    assert prior["evidence"]["package"] == LAB
    # Stage 28 privacy surface: the evidence projection NEVER carries
    # element text (observation labels stay untrusted data and out of
    # the mission verdict store) and the audit trail stays id-shaped.
    assert "element" not in v0.get("evidence", {})
    assert "Done" not in repr(v0)
    audit_rows = stack["audit"].query(limit=200)
    assert all("Done" not in repr(r) for r in audit_rows)


def test_cancelled_mission_closes_honestly_without_tap_dispatch():
    world = _A11yWorld([{"node_id": 2, "text": "Done", "cls": "B"}])
    stack = _android_stack(world)
    eng = stack["missions"]
    steps = [
        {"capability": "gui.wait",
         "parameters": {"target_package": LAB,
                        "target": {"text": "Done"}},
         "preferred_device": ANDROID_DEVICE, "confidence": 0.9},
        {"capability": "gui.tap",
         "parameters": {"target_package": LAB, "node_id": 2},
         "preferred_device": ANDROID_DEVICE, "confidence": 0.9},
    ]
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            FixedPlanParser(steps), stack["audit"],
                            stack["missions"])
    with _AndroidAgent(stack, world):
        out = svc.handle_utterance("android cancel mission")
        mid = out["mission_id"]
        assert out.get("approval_reference")     # parks at step 1
        stack["engine"].cancel(out["approval_reference"])
        res = svc.resume_plan(mid)
    assert res["executed"] is False
    assert eng.get(mid).state == MissionState.FAILED
    assert world.taps == []            # the protected step never ran
    assert [j for j in stack["jobs"]._jobs.values()
            if j.tool == "gui.tap"] == []
    # terminal missions cannot be reopened
    assert svc.resume_plan(mid)["resumed"] is False


def test_allowlisted_contracts_declared_on_both_sides():
    from core.device_tools import DEVICE_TOOL_DEFS, ANDROID_OUTPUT_SCHEMAS
    names = {d.name: d for d in DEVICE_TOOL_DEFS}
    a = names["system.android.inventory"]
    assert a.verification == "output_schema"
    assert a.output_schema == ANDROID_OUTPUT_SCHEMAS["system.android.inventory"]
    assert a.supported_devices == ["linux"]
    w = names["gui.wait"]
    assert w.verification == "explicit"
    assert sorted(w.input_schema["properties"]["target_package"]["enum"]) \
        == sorted(GUI_LAB_TARGETS)
    assert w.supported_devices == ["android"]
    # agent side: the same schema constant is imported (structural parity)
    import device.linux.android_adb_linux as M
    assert M.ANDROID_OUTPUT_SCHEMAS == ANDROID_OUTPUT_SCHEMAS
