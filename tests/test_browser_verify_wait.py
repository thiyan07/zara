"""browser.wait — the verify-after-act step (Stage 24c round 2).

Research basis (docs/RESEARCH_2026-10-20.md § round-2 adoption):
Playwright's own best practice is **web-first assertions** — wait on an
observable outcome with a bounded poll instead of a single-shot manual
check. Zara's browser.click/fill/submit returned one url/title snapshot
with no way to wait for the consequence; browser.wait closes that gap
inside the SAME bounded Playwright worker (no new dependency, no
competing framework).

Tests:
- pure validation (guardrails, caps, bounded timeout),
- a fake-page evaluation round (errors recorded, never swallowed),
- REAL local-page verification through the existing Playworker worker
  (loopback HTTP server; no external network) — satisfied path,
  timeout-honesty path, and the registry contract.
"""
import threading

import pytest

from http.server import BaseHTTPRequestHandler, HTTPServer

from core import browser_tools as bt


# ------------------------------------------------------------------
# pure validation
# ------------------------------------------------------------------

def test_wait_inputs_rejects_empty_and_unknown_conditions():
    with pytest.raises(ValueError, match="non-empty object"):
        bt._wait_inputs({"conditions": {}})
    with pytest.raises(ValueError, match="unknown condition"):
        bt._wait_inputs({"conditions": {"nope": "x"}})
    with pytest.raises(ValueError, match="non-empty string"):
        bt._wait_inputs({"conditions": {"url_contains": ""}})
    with pytest.raises(ValueError, match="cap"):
        bt._wait_inputs({"conditions": {"url_contains": "x" * 400}})


def test_wait_inputs_bounds_the_timeout():
    for bad in (0.1, 31.0, "soon", "0"):
        with pytest.raises(ValueError, match="timeout_s"):
            bt._wait_inputs({"conditions": {"url_contains": "x"},
                             "timeout_s": bad})
    assert bt._wait_inputs({"conditions": {"url_contains": "x"},
                            "timeout_s": 0.2})[1] == 0.2
    assert bt._wait_inputs({"conditions": {"url_contains": "x"},
                            "timeout_s": 30.0})[1] == 30.0


def test_wait_inputs_defaults_and_clamps():
    conds, t = bt._wait_inputs({"conditions": {"text_present": "hi"}})
    assert conds == {"text_present": "hi"} and t == 8.0


class _FakePage:
    """Minimal page surface for _condition_status evaluation rounds."""

    def __init__(self, url="", title="", body="", visible=False):
        self.url, self._title, self._body = url, title, body
        self._visible = visible

    def title(self):
        return self._title

    def inner_text(self, _sel):
        return self._body

    def is_visible(self, sel):
        if "(" in sel:  # malformed-selector probe
            raise RuntimeError("bad selector syntax")
        return self._visible

    def wait_for_timeout(self, _ms):  # no-op
        pass


def test_condition_status_records_errors_never_swallows():
    page = _FakePage(url="http://x/done", title="All done",
                     body="payment complete", visible=True)
    status, errors = bt._condition_status(
        page, {"url_contains": "done", "title_contains": "done",
               "text_present": "complete", "selector_visible": "div#ok"})
    assert status == {"url_contains": True, "title_contains": True,
                      "text_present": True, "selector_visible": True}
    bad = bt._condition_status(
        page, {"selector_visible": "span[boom("})[1]
    assert "bad selector" in bad["selector_visible"]


def test_condition_status_transient_counts_unmet_but_visible():
    class _MidNav(_FakePage):
        def inner_text(self, _sel):
            raise RuntimeError("Execution context was destroyed")

    page = _MidNav(url="", title="", body="", visible=False)
    status, errors = bt._condition_status(
        page, {"text_present": "anything"})
    assert status["text_present"] is False
    assert "context was destroyed" in errors["text_present"]


def test_wait_result_shape():
    page = _FakePage(url="http://x/", title="t")
    out = bt._wait_result(page, {"url_contains": "x"},
                          {"url_contains": True}, {},
                          start=__import__("time").monotonic() - 0.5,
                          satisfied=True)
    assert out["satisfied"] is True and out["elapsed_ms"] >= 400
    assert out["conditions"] == {"url_contains": True}
    assert out["errors"] == {}


# ------------------------------------------------------------------
# real local-page verification through the bounded worker
# ------------------------------------------------------------------

PAGE_BYTES = (b"<html><head><title>Zara Wait</title></head><body>"
              b"<h1>Landing</h1><p id='msg'>wait here</p>"
              b"<div id='target' hidden>target</div></body></html>")


class _WaitH(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE_BYTES)))
        self.end_headers()
        self.wfile.write(PAGE_BYTES)

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture()
def wait_page():
    srv = HTTPServer(("127.0.0.1", 0), _WaitH)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    try:
        # open the page here: every wait runs against a REAL loaded page
        bt.browser_open({"url": url}, {})
        yield url
    finally:
        srv.shutdown()
        try:
            bt.browser_close({}, {})
        except Exception:  # noqa: BLE001
            pass


@pytest.mark.usefixtures("wait_page")
def test_wait_satisfied_immediately():
    out = bt.browser_wait(
        {"conditions": {"title_contains": "Zara Wait",
                        "text_present": "Landing",
                        "url_contains": "127.0.0.1"},
         "timeout_s": 5.0}, {})
    assert out["satisfied"] is True and out["elapsed_ms"] >= 0
    assert all(out["conditions"].values()) and out["errors"] == {}


@pytest.mark.usefixtures("wait_page")
def test_wait_timeout_is_honest_and_names_unmet():
    out = bt.browser_wait(
        {"conditions": {"text_present": "text that never appears"},
         "timeout_s": 1.0}, {})
    assert out["satisfied"] is False
    assert out["conditions"]["text_present"] is False
    # the page snapshot is still returned for diagnosis
    assert "127.0.0.1" in out["url"]


@pytest.mark.usefixtures("wait_page")
def test_wait_bad_selector_reports_error_not_crash():
    out = bt.browser_wait(
        {"conditions": {"selector_visible": "div[pattern("},
         "timeout_s": 1.2}, {})
    assert out["satisfied"] is False
    assert out["errors"]["selector_visible"]


def test_wait_registry_entry_contract():
    names = {d.name: d for d, _ in bt.BROWSER_TOOLS}
    d = names["browser.wait"]
    assert d.risk.value == "safe" and \
        d.permission.value == "restricted"
    assert "browser.automation" in d.required_capabilities
