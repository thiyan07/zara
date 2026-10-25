"""P5 feature-hunt ⑳ (2026-10-24) — "is my internet actually
working" (system.connectivity).

Three bounded probe layers beyond the NIC-up state
(system.network answers interface state; this answers REACHABILITY):
keyless DNS resolution, raw TCP to allowlisted public relay
endpoints, an end-to-end HTTP(S) 200x check (DNS+TCP+TLS+HTTP —
catches captive portals and proxies that NIC/DNS probes miss).

Contract pinned here:
- the wrapper's total wall time is bounded (~ worst-case ~30 s: every
  layer runs in a daemon thread joined on a BUDGET — a dead resolver
  cannot hang the tool; a timeout is honestly "unreachable");
- verdicts are DESCRIPTIVE labels (full / limited_dns / limited_web /
  offline), never attitude, never predictions;
- the web check tries TWO keyless probe URLs and succeeds on the
  first 2xx — one blocked endpoint is not evidence of no internet;
- keyless endpoints only; the detail says "diagnostic endpoints",
  never "the internet works";
- timing invariant: the device def's proxy wait > 60 s (test_T0);
- the matcher owns ONLY the end-to-end internet ask; bare network/
  wifi aliases and wifi settings/set asks keep their own surfaces;
  absent capability falls through;
- the deterministic formatter renders the verdict + measured
  numbers (never a bare "Done" and never invented latencies).
"""
import pytest

import sys
import os
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

from core.builtin_tools import (  # noqa: E402
    system_connectivity, system_top)


def test_real_probe_bounded_and_structured():
    import time as _t
    t0 = _t.monotonic()
    out = system_connectivity({}, {})
    wall = _t.monotonic() - t0
    assert wall < 32.0, out            # every layer bounded
    assert out["ok"] is True
    assert out["verdict"] in ("full", "limited_dns", "limited_web",
                              "offline")
    assert len(out["dns"]) == 2 and len(out["tcp"]) == 3
    for r in out["tcp"]:
        assert r["addr"].endswith(":53")
        if r["ok"]:
            assert r["ms"] is not None and r["ms"] > 0
    web = out["https"]
    assert isinstance(web, dict) and "ok" in web
    if web["ok"]:
        assert web["status"] and 200 <= web["status"] < 300
        assert web["ms"] is not None
    else:
        assert web["error"]
    assert "diagnostic endpoints" in out["detail"]


def test_timing_invariant_device_def():
    from core.device_tools import DEVICE_TOOL_DEFS
    d = next(d for d in DEVICE_TOOL_DEFS if d.name == "system.connectivity")
    assert d.timeout_s > 60.0
    assert "system.health" in d.required_capabilities
    assert d.risk.value == "safe"
    assert "linux" in d.supported_devices


def _parser():
    from core.intent import LocalIntentParser
    return LocalIntentParser()


def _ctx():
    from core.intent import ParserContext, ParserDevice
    return ParserContext(
        known_capabilities={"system.connectivity", "system.network",
                            "system.wifi.state", "system.wifi.settings",
                            "system.health"},
        devices=[ParserDevice(device_id="laptop-1", kind="linux")])


def test_matcher_internet_shapes():
    p = _parser()
    for ask in ("is my internet down", "is my internet working",
                "is my internet up", "check my internet",
                "run an internet connectivity check",
                "why is the internet so slow"):
        out = p.parse(ask, _ctx())
        assert out.capability == "system.connectivity", (ask, out)


def test_matcher_leaves_other_network_surfaces():
    p = _parser()
    keep = {"internet": "system.network",
            "network status": "system.network",
            "wifi status": "system.wifi.state",
            "open wifi settings": "system.wifi.settings",
            "turn on wifi": "system.network"}
    for ask, want in keep.items():
        out = p.parse(ask, _ctx())
        assert out.capability == want, (ask, out.capability)


def test_matcher_absent_capability_falls_through():
    from core.intent import ParserContext
    p = _parser()
    out = p.parse("is my internet down", ParserContext(
        known_capabilities={"system.network"}))
    assert out.capability != "system.connectivity"


def _fmt(result):
    from core.intent import IntentTurnService, IntentProposal
    prop = IntentProposal(request_id="t", utterance="x",
                          capability="system.connectivity",
                          parameters={}, confidence=0.8,
                          needs_clarification=False, source="local")
    return IntentTurnService._success_message(
        prop, {"result": result, "stage": "succeeded"})


def test_formatter_full_verdict():
    msg = _fmt({"ok": True, "verdict": "full",
                "dns": [{"name": "one.one.one.one", "ok": True,
                         "ms": 2.4, "error": ""},
                        {"name": "dns.google", "ok": True, "ms": 0.9,
                         "error": ""}],
                "tcp": [{"addr": "1.1.1.1:53", "ok": True, "ms": 12.0},
                        {"addr": "8.8.8.8:53", "ok": True, "ms": 11.0},
                        {"addr": "9.9.9.9:53", "ok": False, "ms": None}],
                "https": {"ok": True, "ms": 118.0, "status": 204,
                          "error": ""}})
    assert msg.startswith("Internet: working end to end — ")
    assert "DNS ok on 2/2 names" in msg
    assert "1.1.1.1 12.0 ms, 8.8.8.8 11.0 ms" in msg
    assert "web check ok (118.0 ms, status 204)" in msg
    assert "keyless diagnostic endpoints only" in msg


def test_formatter_limited_web_verdict():
    msg = _fmt({"ok": True, "verdict": "limited_web",
                "dns": [{"name": "one.one.one.one", "ok": True,
                         "ms": 2.0, "error": ""},
                        {"name": "dns.google", "ok": True, "ms": 1.0,
                         "error": ""}],
                "tcp": [{"addr": "1.1.1.1:53", "ok": True, "ms": 30.0},
                        {"addr": "8.8.8.8:53", "ok": True, "ms": 20.0},
                        {"addr": "9.9.9.9:53", "ok": True, "ms": 20.0}],
                "https": {"ok": False, "ms": None, "status": None,
                          "error": "timed out"}})
    assert "raw connectivity answers but the web check fails" in msg
    assert "web check failed (timed out)" in msg


def test_formatter_offline_and_dns_limited():
    msg = _fmt({"ok": True, "verdict": "offline"})
    assert msg.startswith("No connectivity measured right now")
    msg = _fmt({"ok": True, "verdict": "limited_dns",
                "dns": [{"name": "one.one.one.one", "ok": False,
                         "ms": 3000.0, "error": "timed out"},
                        {"name": "dns.google", "ok": False, "ms": 3000.0,
                         "error": "timed out"}],
                "tcp": [{"addr": "1.1.1.1:53", "ok": True, "ms": 15.0},
                        {"addr": "8.8.8.8:53", "ok": True, "ms": 14.0},
                        {"addr": "9.9.9.9:53", "ok": True, "ms": 13.0}],
                "https": {"ok": False, "ms": None, "status": None,
                          "error": "timed out"}})
    assert "without working name resolution" in msg
    assert "relay connects: 1.1.1.1 15.0 ms" in msg
