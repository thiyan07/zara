"""Stage 27 — measured local behavior budgets (loose, CI-stable).

These are not microbenchmarks of unrelated systems; they pin REGRESSION
bounds on the deterministic routing + mission pipeline using the same
in-process fixtures as the workflow suites, and print the measured
values so stage reports can cite exact numbers. Bounds are deliberately
generous (well above quiet-machine values) — a failure here means a
real structural regression (repeated discovery, unbounded polling,
quadratic continuation), never a noisy neighbor.
"""
import statistics
import time

from core.intent import IntentTurnService, LocalIntentParser
from core.resolver import CapabilityResolver
from tests.test_docwork_nl import (_Agent, _stack_with_agent, _svc,
                                   doc_home)  # noqa: F401
from tests.test_plan_continuation import FixedPlanParser


def _measure(fn, n, warmup=5):
    for _ in range(warmup):
        fn()
    xs = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t0) * 1000.0)
    return xs


def test_resolver_latency_budget(doc_home):
    stack = _stack_with_agent()
    resolver = CapabilityResolver(stack["fabric"])
    # name-resolution for a known capability (the hot NL path)
    xs = _measure(lambda: resolver.resolve_proposal(
        {"preferred_capability": "docs.summarize",
         "preferred_device": "", "constraints": {}}, "user"), 200)
    med = statistics.median(xs)
    p95 = sorted(xs)[int(0.95 * len(xs))]
    print(f"\n[perf] resolver.resolve_proposal median={med:.3f} ms "
          f"p95={p95:.3f} ms (n=200)")
    assert med < 20.0, med       # generous; regression detector only
    assert p95 < 60.0, p95


def test_mission_turn_and_continuation_overhead(doc_home, monkeypatch):
    stack = _stack_with_agent()
    steps = [
        {"capability": "docs.searchtext",
         "parameters": {"path": str(doc_home / "notes"),
                        "query": "normalization"},
         "preferred_device": "", "confidence": 0.9},
        {"capability": "docs.summarize",
         "parameters": {"path": str(doc_home / "notes.md")},
         "preferred_device": "", "confidence": 0.9},
    ]
    svc = _svc_stack_with_plan(stack, steps)
    with _Agent(stack):
        xs = _measure(lambda: svc.handle_utterance("mission turn"), 20)
        med = statistics.median(xs)
        print(f"\n[perf] 2-step SAFE mission turn median={med:.1f} ms "
              f"(n=20)")
        assert med < 250.0, med     # each turn runs real tool handlers
    # duplicate RESOLUTION per turn: one resolve per step plus the
    # engine's own policy pass — counted here to catch accidental
    # quadratic re-resolution growth
    calls = []
    orig = svc.resolver.resolve_proposal

    def counting(*a, **kw):
        calls.append(1)
        return orig(*a, **kw)

    svc.resolver.resolve_proposal = counting
    with _Agent(stack):
        svc.handle_utterance("count resolutions")
    per_step = len(calls) / 2.0
    print(f"[perf] resolve_proposal calls per mission step = "
          f"{per_step:.1f}")
    assert per_step <= 6.0, per_step


def test_cross_document_search_cost(doc_home):
    stack = _stack_with_agent()
    svc = _svc(stack)
    with _Agent(stack):
        first = _measure(
            lambda: svc.handle_utterance(
                "search my notes for normalization"), 8)
        med = statistics.median(first)
        print(f"\n[perf] docs.searchtext turn (discovery + 2 docs) "
              f"median={med:.1f} ms (n=8)")
        assert med < 400.0, med     # discovery is bounded, not cached


def _svc_stack_with_plan(stack, steps):
    return IntentTurnService(stack["fabric"],
                             CapabilityResolver(stack["fabric"]),
                             FixedPlanParser(steps), stack["audit"],
                             stack["missions"])

