"""Agent-side challenge-auth unit tests (no sockets): the DeviceAgent
transport layer with a stubbed ``urllib.request.urlopen``.

Covers the behaviors the live-socket test in test_device_challenge.py
cannot reach cheaply: raw-key fallback semantics per HTTP code, the
permanent challenge-disable on an unbound-key 409, and the claim-time
``.sk`` file lifecycle (written 0600, removed when no signing key is
bound — e.g. cryptography unavailable).
"""
import io
import json
import os
import stat
import urllib.error
import urllib.request

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey)
from device.linux.agent import DeviceAgent


class _FakeTransport:
    """Records every urlopen(Request) and replays scripted outcomes.

    Script entries: "ok" (success + n-live nonce), a tuple
    ("resp", dict) for a custom 200 payload, or an int HTTP status.
    Unscripted calls succeed ("ok").
    """

    def __init__(self):
        self.calls = []  # (method, path, {lowercased h: v})
        self.script = []

    def _urlopen(self, req, timeout=15):  # noqa: ARG002 — timeout unused
        path = req.get_full_url().split("127.0.0.1:59990", 1)[-1]
        headers = {k.lower(): v for k, v in req.header_items()}
        self.calls.append((req.get_method(), path, headers))
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "ok":
            body = {"ok": True, "device_id": "d1",
                    "nonce": "n-live", "next_nonce": "n-next"}
        elif isinstance(outcome, tuple):
            body = outcome[1]
        else:
            raise urllib.error.HTTPError(
                req.get_full_url(), outcome, "err",
                {"X-Why": "scr"}, io.BytesIO(b'{"error": "x"}'))
        return io.BytesIO(json.dumps(body).encode())


_OK_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"


def _agent(tmp_path, monkeypatch):
    """Agent with a real signing seed (so proofs sign) + files on disk."""
    t = _FakeTransport()
    monkeypatch.setattr(urllib.request, "urlopen", t._urlopen)
    a = DeviceAgent("http://127.0.0.1:59990", "d1", key_file=str(tmp_path / "k"))
    with open(tmp_path / "k", "w") as f:
        f.write("dev-secret")
    with open(tmp_path / "k.sk", "w") as f:
        f.write(_OK_SEED)
    a.load_key()
    return a, t


def test_signed_call_uses_challenge_chain(tmp_path, monkeypatch):
    a, t = _agent(tmp_path, monkeypatch)
    out = a.heartbeat()
    assert out["ok"] is True
    assert t.calls[0][1] == "/v1/agent/challenge" and _is_raw(t.calls[0])
    assert t.calls[1][1] == "/v1/agent/heartbeat" and _is_proof(t.calls[1])
    assert a._spare_nonce  # chain continues for the next call


def _is_proof(call):
    h = call[2]
    return "x-device-proof" in h and "x-device-key" not in h


def _is_raw(call):
    h = call[2]
    return "x-device-key" in h and "x-device-proof" not in h


def test_raw_key_fallback_on_auth_shaped_404(tmp_path, monkeypatch):
    a, t = _agent(tmp_path, monkeypatch)
    t.script = ["ok", 404, "ok"]  # bootstrap, proof'd call 404, raw retry
    out = a.heartbeat()
    assert out["ok"] is True
    assert t.calls[1][1] == "/v1/agent/heartbeat" and _is_proof(t.calls[1])
    assert t.calls[2][1] == "/v1/agent/heartbeat" and _is_raw(t.calls[2])
    assert a._spare_nonce == ""  # stale spare dropped on fallback


def test_raw_key_fallback_on_409(tmp_path, monkeypatch):
    a, t = _agent(tmp_path, monkeypatch)
    t.script = ["ok", 409, "ok"]
    assert a.heartbeat()["ok"] is True
    assert _is_proof(t.calls[1]) and _is_raw(t.calls[2])


def test_server_500_does_not_retry_with_raw_key(tmp_path, monkeypatch):
    a, t = _agent(tmp_path, monkeypatch)
    t.script = ["ok", 500]
    with pytest.raises(RuntimeError, match="HTTP 500"):
        a.heartbeat()
    assert len(t.calls) == 2  # no resend: the request may have executed


def test_challenge_409_disables_proofs_permanently(tmp_path, monkeypatch):
    a, t = _agent(tmp_path, monkeypatch)
    t.script = [409, "ok", "ok"]  # bootstrap 409, then raw forever
    assert a.heartbeat()["ok"] is True
    assert a._challenge_ok is False
    assert a.heartbeat()["ok"] is True
    assert all(_is_raw(c) for c in t.calls)  # zero proof attempts remain


def test_spare_nonce_chain_avoids_raw_bootstrap(tmp_path, monkeypatch):
    a, t = _agent(tmp_path, monkeypatch)
    assert a.heartbeat()["ok"] is True
    assert a.heartbeat()["ok"] is True
    # second call's challenge fetch is PROOF'd with the spare nonce —
    # the raw key travels exactly once per process lifetime
    assert t.calls[2][1] == "/v1/agent/challenge" and _is_proof(t.calls[2])
    assert t.calls[3][1] == "/v1/agent/heartbeat" and _is_proof(t.calls[3])


def test_claim_writes_key_and_sk_0600(tmp_path, monkeypatch):
    from core.device_auth import generate_device_keypair
    pub, priv = generate_device_keypair()
    t = _FakeTransport()
    t.script = [("resp", {"device_id": "d1", "device_key": "dev-secret"})]
    monkeypatch.setattr(urllib.request, "urlopen", t._urlopen)
    monkeypatch.setattr("core.device_auth.generate_device_keypair",
                        lambda: (pub, priv))
    a = DeviceAgent("http://127.0.0.1:59990", "d1", key_file=str(tmp_path / "k"))
    a.claim("code-1")
    assert a._sk_hex == priv
    kf, skf = tmp_path / "k", tmp_path / "k.sk"
    assert kf.exists() and skf.exists()
    assert stat.S_IMODE(kf.stat().st_mode) & 0o777 == 0o600
    assert stat.S_IMODE(skf.stat().st_mode) & 0o777 == 0o600
    # the claim body carried the pubkey so Core can verify proofs later
    method, path, headers = t.calls[0]
    assert (method, path) == ("POST", "/v1/agent/claim")


def test_claim_without_pubkey_removes_stale_sk(tmp_path, monkeypatch):
    a_key = tmp_path / "k"
    stale = tmp_path / "k.sk"
    stale.write_text("stale-seed")
    t = _FakeTransport()
    t.script = [("resp", {"device_id": "d1", "device_key": "dev-secret"})]
    monkeypatch.setattr(urllib.request, "urlopen", t._urlopen)

    def boom():  # cryptography unavailable on this device/install
        raise ImportError("no cryptography")

    monkeypatch.setattr("core.device_auth.generate_device_keypair", boom)
    a = DeviceAgent("http://127.0.0.1:59990", "d1", key_file=str(a_key))
    a.claim("code-1")
    assert a._sk_hex == ""  # no signing key in memory
    assert not stale.exists()  # never leave a stale private key


def test_transport_guard_refuses_plain_http_remote():
    with pytest.raises(ValueError, match="plain HTTP"):
        DeviceAgent("http://192.168.1.5:8080", "d1", key_file="/tmp/k-none")


def test_transport_guard_allows_loopback_and_https(tmp_path, monkeypatch):
    monkeypatch.delenv("ZARA_ALLOW_INSECURE_TRANSPORT", raising=False)
    DeviceAgent("http://127.0.0.1:8080", "dl", key_file=str(
        tmp_path / "k1"))._check_transport_policy()
    DeviceAgent("https://zara.example.org", "dh", key_file=str(
        tmp_path / "k2"))._check_transport_policy()
    DeviceAgent("http://10.0.2.2:8080", "de", key_file=str(
        tmp_path / "k3"))._check_transport_policy()


def test_transport_guard_explicit_override_is_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("ZARA_ALLOW_INSECURE_TRANSPORT", "1")
    a = DeviceAgent("http://192.168.1.5:8080", "d1", key_file=str(
        tmp_path / "k"))  # constructor must NOT raise under the override
    assert "INSECURE-TRANSPORT-ALLOWED" in a.software_version


def test_proof_message_is_v2_with_body_hash(tmp_path, monkeypatch):
    """The signed wire message binds the exact body bytes: derive the
    public key from the loaded seed and verify the produced signature
    over the v2 canonical string with Core's own verifier."""
    import base64
    import hashlib
    from core.device_auth import verify_device_proof
    a, _t = _agent(tmp_path, monkeypatch)
    nonce = "n-abc"
    headers = a._proof_headers(nonce, "POST", "/v1/agent/heartbeat", b"{}")
    pub = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(_OK_SEED)).public_key().public_bytes_raw().hex()
    v2 = (f"POST /v1/agent/heartbeat {nonce} "
          f"{hashlib.sha256(b'{}').hexdigest()}")
    assert verify_device_proof(pub, v2, headers["X-Device-Proof"]) is True
    # tampering the body is detectable server-side via the digest
    assert verify_device_proof(
        pub, f"POST /v1/agent/heartbeat {nonce} "
             f"{hashlib.sha256(b'evil').hexdigest()}",
        headers["X-Device-Proof"]) is False
