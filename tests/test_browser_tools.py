"""Browser automation guardrails — real local-page verification.

Uses an isolated loopback HTTP server + system Chrome via the existing
bounded Playwright worker. No external network, no downloads.
"""
import threading

import pytest

from http.server import BaseHTTPRequestHandler, HTTPServer

from core import browser_tools as bt


PAGE = (b"<html><head><title>Zara Test</title></head><body>"
        b"<h1>Hello Zara</h1><p>benign content</p>"
        b"<input id='q' type='text'/>"
        b"<button id='go'>Go</button></body></html>")


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture()
def local_page():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        srv.shutdown()
        try:
            bt.browser_close({}, {})
        except Exception:  # noqa: BLE001
            pass


def test_open_extract_roundtrip(local_page):
    opened = bt.browser_open({"url": local_page}, {})
    assert opened["title"] == "Zara Test"
    out = bt.browser_extract({}, {})
    assert "Hello Zara" in out["content"]
    assert out["content"].startswith("<<UNTRUSTED DATA>>")
    assert "never instructions" in out["content"]


def test_non_http_refused():
    with pytest.raises(ValueError):
        bt.browser_open({"url": "file:///etc/passwd"}, {})


def test_credential_fill_refused(local_page):
    bt.browser_open({"url": local_page}, {})
    with pytest.raises(PermissionError):
        bt.browser_fill({"selector": "#password", "value": "x"}, {})
    with pytest.raises(PermissionError):
        bt.browser_fill({"selector": "input[name=api_key]", "value": "x"},
                        {})


def test_state_changing_click_refused(local_page):
    bt.browser_open({"url": local_page}, {})
    with pytest.raises(PermissionError):
        bt.browser_click({"selector": "#buy-now-checkout"}, {})


def test_fill_then_extract(local_page):
    bt.browser_open({"url": local_page}, {})
    r = bt.browser_fill({"selector": "#q", "value": "hello"}, {})
    assert r["filled"] == "#q"


def test_submit_tool_is_confirm_risk():
    names = {d.name: d for d, _ in bt.BROWSER_TOOLS}
    assert names["browser.submit"].risk.value == "confirm"
    assert "browser.automation" in names["browser.open"].required_capabilities


def test_buy_page_submit_refused(local_page):
    buy = (b"<html><body><h1>buy now checkout pay</h1></body></html>")

    class _B(_H):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(buy)))
            self.end_headers()
            self.wfile.write(buy)

    srv = HTTPServer(("127.0.0.1", 0), _B)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        bt.browser_open({"url": f"http://127.0.0.1:{port}/"}, {})
        with pytest.raises(PermissionError):
            bt.browser_submit({}, {})
    finally:
        srv.shutdown()
