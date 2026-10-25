"""Browser task-level workflows (real browser, loopback pages only).

Extends the pre-existing bounded Playwright layer with verified,
structured workflow primitives:

- `browser.links`: actionable elements as UNTRUSTED structured data
  (``#id`` selectors only for provably safe ids).
- `browser.search`: term-filtered extraction instead of a raw dump.
- `browser.form`: field inspection with secret-typed values MASKED.
- `browser.verify_fill`: re-reads a field to confirm an earlier fill.
- `browser.download`: bounded, no-overwrite save into the scratch
  downloads dir, verified by written byte count.

Classification: real browser (system Chrome) against a loopback HTTP
server on this host — not simulated fakes, but also not a public-web
session; no external network is contacted.
"""
import os
import re
import threading

import pytest
from http.server import BaseHTTPRequestHandler, HTTPServer

from core import browser_tools as bt

DOC_BYTES = b"ledger totals for october"
PAGE = (b"<html><head><title>Zara Workflows</title></head><body>"
        b"<h1>Address Book</h1>"
        b"<a id='doc' href='/doc.txt'>Download ledger</a>"
        b"<a href='/page2'>Next page</a>"
        b"<button id='ok'>Okay</button>"
        b"<button id='weird id with spaces'>Broken</button>"
        b"<input id='q' name='q' type='text'/>"
        b"<input name='pw' type='password'/>"
        b"<input name='cc' type='card'/>"
        b"</body></html>")


def _make_handler(body, ctype):
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            status = 200
            out, out_ctype = body, ctype
            if self.path == "/doc.txt":
                out, out_ctype = DOC_BYTES, "application/octet-stream"
            if self.path == "/missing":
                status, out = 404, b"no"
            self.send_response(status)
            self.send_header("Content-Type", out_ctype)
            self.send_header("Content-Length", str(len(out)))
            if status == 200:
                self.end_headers()
                self.wfile.write(out)
            else:
                self.end_headers()

        def log_message(self, *a):  # quiet
            pass

    return _H


@pytest.fixture()
def site():
    srv = HTTPServer(("127.0.0.1", 0), _make_handler(
        PAGE, "text/html"))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        bt.browser_open({"url": f"http://127.0.0.1:{port}/"}, {})
        yield srv, port
    finally:
        srv.shutdown()
        try:
            bt.browser_close({}, {})
        except Exception:  # noqa: BLE001
            pass


def test_links_are_structured_and_never_invent_selectors(site):
    _, port = site
    out = bt.browser_links({}, {})
    by_id = {l.get("id", l.get("text")): l for l in out["links"]}
    assert out["count"] >= 3
    assert out["note"].startswith("untrusted")
    # A safe id yields a selector; spaces in an id must NOT produce one.
    with_sel = [l for l in out["links"] if "selector" in l]
    assert all(re.match(r"^#[A-Za-z][A-Za-z0-9_-]*$", l["selector"])
               for l in with_sel)
    hrefs = [l["href"] for l in out["links"] if "href" in l]
    assert any(h.endswith("/doc.txt") for h in hrefs)
    # Relative hrefs are resolvable: abs_href is ready for browser.open.
    absed = [l for l in out["links"] if "abs_href" in l]
    assert all(l["abs_href"].startswith(f"http://127.0.0.1:{port}/")
               for l in absed)


def test_search_returns_only_matching_regions(site):
    assert bt.browser_search({"term": "ledger"}, {})["count"] >= 1
    empty = bt.browser_search({"term": "definitely-absent"}, {})
    assert empty["count"] == 0 and empty["hits"] == []
    with pytest.raises(ValueError):
        bt.browser_search({"term": "   "}, {})


def test_form_masks_secret_typed_fields(site):
    out = bt.browser_form({}, {})
    masks = [f for f in out["fields"] if f.get("masked")]
    # password + card-like ("card" type on our fake page) are masked.
    assert len(masks) >= 1
    assert all(f["value"] is None for f in masks)
    # A normal field keeps its selector + emptiness honestly reported.
    texty = [f for f in out["fields"] if "selector" in f]
    assert any(f["selector"] == "#q" and f["filled"] is False
               for f in texty)


def test_verify_fill_confirms_and_detects_mismatch(site):
    bt.browser_fill({"selector": "#q", "value": "hello"}, {})
    ok = bt.browser_verify_fill({"selector": "#q", "value": "hello"}, {})
    assert ok["verified"] is True
    bad = bt.browser_verify_fill({"selector": "#q", "value": "other"}, {})
    assert bad["verified"] is False and "does not match" in bad["detail"]


def test_download_roundtrip_verifies_byte_count(site, tmp_path):
    _, port = site
    real = os.environ.get("ZARA_SCRATCH_DIR", "")

    class _Env:
        def __enter__(self):
            os.environ["ZARA_SCRATCH_DIR"] = str(tmp_path)
            return self

        def __exit__(self, *a):
            if real:
                os.environ["ZARA_SCRATCH_DIR"] = real
            else:
                os.environ.pop("ZARA_SCRATCH_DIR", None)

    with _Env():
        out = bt.browser_download(
            {"url": f"http://127.0.0.1:{port}/doc.txt",
             "name": "october-ledger.bin"}, {})
        assert out["verified"] is True
        assert out["bytes"] == len(DOC_BYTES)
        with open(out["path"], "rb") as f:
            assert f.read() == DOC_BYTES
        with pytest.raises(FileExistsError):
            bt.browser_download(
                {"url": f"http://127.0.0.1:{port}/doc.txt",
                 "name": "october-ledger.bin"}, {})


def test_download_refuses_pages_and_bad_names(site, tmp_path):
    _, port = site
    os.environ["ZARA_SCRATCH_DIR"] = str(tmp_path)
    try:
        with pytest.raises(ValueError) as e:
            bt.browser_download(
                {"url": f"http://127.0.0.1:{port}/", "name": "page.bin"}, {})
        assert "web page" in str(e.value)
        for bad in ("../evil.bin", "a/b.bin", "token.bin", "key.pem"):
            with pytest.raises((ValueError, PermissionError)):
                bt.browser_download(
                    {"url": f"http://127.0.0.1:{port}/doc.txt",
                     "name": bad}, {})
    finally:
        os.environ.pop("ZARA_SCRATCH_DIR", None)


def test_download_size_cap_refuses_before_writing(site, tmp_path):
    _, port = site
    os.environ["ZARA_SCRATCH_DIR"] = str(tmp_path)
    try:
        assert not os.listdir(tmp_path)
        real_cap = bt.DOWNLOAD_MAX_BYTES
        bt.DOWNLOAD_MAX_BYTES = 4
        try:
            with pytest.raises(ValueError) as e:
                bt.browser_download(
                    {"url": f"http://127.0.0.1:{port}/doc.txt",
                     "name": "small.bin"}, {})
            assert "cap" in str(e.value)
        finally:
            bt.DOWNLOAD_MAX_BYTES = real_cap
        assert os.listdir(os.path.join(tmp_path, "downloads")) == []
    finally:
        os.environ.pop("ZARA_SCRATCH_DIR", None)


def test_download_http_error_is_honest(site, tmp_path):
    _, port = site
    os.environ["ZARA_SCRATCH_DIR"] = str(tmp_path)
    try:
        with pytest.raises(Exception) as e:
            bt.browser_download(
                {"url": f"http://127.0.0.1:{port}/missing",
                 "name": "x.bin"}, {})
        assert "404" in str(e.value)
        assert os.listdir(os.path.join(tmp_path, "downloads")) == []
    finally:
        os.environ.pop("ZARA_SCRATCH_DIR", None)
