"""Recipient download must clamp the first read to the transfer's
declared size: Core refuses reads past that size ("read overruns
transfer size"), so a sub-64KiB relay transfer used to fail its very
first fetch (observed live 2026-10-25). No sockets — urlopen stubbed.
"""
import hashlib
import io
import json
import urllib.request

from device.linux.agent import DeviceAgent


class _FakeUrlopen:
    """Replays scripted bodies per call and records requests."""

    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=15):  # noqa: ARG001
        url = req.get_full_url()
        self.calls.append((url, req.data))
        # path table drives the response: record -> bytes -> ack handled
        # by order-of-use in the test
        return io.BytesIO(b"")


def test_xfer_download_clamps_first_block_to_declared_size(tmp_path,
                                                           monkeypatch):
    payload = b"hello relay" * 10  # 110 bytes — well under one chunk
    digest = hashlib.sha256(payload).hexdigest()
    key = tmp_path / "k"
    key.write_text("dev-secret")
    a = DeviceAgent("http://127.0.0.1:59990", "d1",
                    key_file=str(key))  # key loaded lazily on _headers

    bodies = {
        "record": {"transfer_id": "xfer-t", "filename": "small.txt",
                   "size_bytes": len(payload), "sha256": digest,
                   "state": "succeeded"},
        "block": {"transfer_id": "xfer-t", "offset": 0,
                  "length": len(payload), "size_bytes": len(payload),
                  "sha256": digest,
                  "data_base64": __import__("base64").b64encode(
                      payload).decode()},
        "ack": {"transfer_id": "xfer-t", "recipient_verified": True},
    }
    urlopen = _FakeUrlopen()
    served = []

    def fake(req, timeout=15):
        url = req.get_full_url()
        urlopen.calls.append((url, url.split("?", 1)[-1]
                              if "?" in url else ""))
        if url.endswith("/agent/xfer/pending"):
            served.append({"pending": True})
            return io.BytesIO(json.dumps({
                "to_download": [bodies["record"]],
                "to_continue": [],
            }).encode())
        if url.endswith("/agent/xfer/xfer-t"):
            served.append(bodies["record"])
            return io.BytesIO(json.dumps(bodies["record"]).encode())
        if "/agent/xfer/xfer-t/bytes?" in url:
            served.append(bodies["block"])
            return io.BytesIO(json.dumps(bodies["block"]).encode())
        if url.endswith("/agent/xfer/xfer-t/ack"):
            served.append(bodies["ack"])
            return io.BytesIO(json.dumps(bodies["ack"]).encode())
        raise AssertionError(f"unexpected request: {url}")
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    data, ack = a.xfer_download("xfer-t")
    assert data == payload
    assert ack["recipient_verified"] is True
    # exactly ONE bytes read, and it asked for the full 110B, never 65536
    byte_urls = [c for c in urlopen.calls if "/bytes?" in c[0]]
    assert len(byte_urls) == 1
    qs = byte_urls[0][1]
    assert "length=110" in qs and "offset=0" in qs
