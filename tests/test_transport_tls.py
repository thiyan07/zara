"""HTTPS transport (production work, docs/TRANSPORT_TLS.md) — REAL
certificate paths over a real socket, not mocked:

1. Core serving TLS with a self-signed cert: default client verification
   FAILS CLOSED (self-signed = connection error, never a silent
   downgrade to http), and a client that explicitly trusts the staged
   cert + checks the hostname gets a verified 200.
2. ZARA_REQUIRE_HTTPS=1: plain HTTP whose SOURCE address is non-loopback
   is refused with a doc pointer; loopback keeps the documented dev
   exemption.

Key material here is test-only, generated per run, lives only under the
pytest tmp dir.
"""
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from ipaddress import ip_address

import pytest
import uvicorn

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import datetime

from core.app import build_stack, create_app


def _lan_ip() -> str:
    """A non-loopback interface address of this host ("" when the
    environment is loopback-only). Connecting (UDP) selects the egress
    interface without sending a packet."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip if ip != "127.0.0.1" else ""
    except OSError:
        return ""


def _make_cert(tmp_path, suffix=""):
    """Self-signed (test CA) leaf with SAN covering this host."""
    import os
    tag = suffix or "main"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         "zara-tls-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    sans = [x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1"))]
    lan = _lan_ip()
    if lan:
        sans.append(x509.IPAddress(ip_address(lan)))
    cert = (x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)  # self-signed root-as-leaf (test only)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .sign(key, hashes.SHA256()))
    import os
    cert_p = os.path.join(str(tmp_path), f"cert_{tag}.pem")
    key_p = os.path.join(str(tmp_path), f"key_{tag}.pem")
    with open(cert_p, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_p, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    with open(cert_p, "rb") as f:
        cert_pem = f.read()
    return cert_p, key_p, cert_pem


def _server(tmp_path, monkeypatch, tls=False, require_https=False):
    """Real uvicorn (TLS optional) on an ephemeral port, started with
    the ZARA_REQUIRE_HTTPS value of THIS test in the process env."""
    monkeypatch.setenv("ZARA_REQUIRE_HTTPS", "1" if require_https else "0")
    app = create_app(build_stack())
    if tls:
        cert_p, key_p, cert_pem = _make_cert(tmp_path)
        config = uvicorn.Config(app, host="0.0.0.0", port=0,
                                ssl_certfile=cert_p, ssl_keyfile=key_p,
                                log_level="error")
    else:
        config = uvicorn.Config(app, host="0.0.0.0", port=0,
                                log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    scheme = "https" if tls else "http"
    return server, port, (scheme, cert_pem if tls else None)


def test_tls_default_verification_fails_closed(tmp_path, monkeypatch):
    """A stock HTTPS client trusts its system CA store; a self-hosted
    self-signed cert must be REFUSED, not silently downgraded."""
    server, port, _ = _server(tmp_path, monkeypatch, tls=True)
    try:
        with pytest.raises(Exception) as e:
            urllib.request.urlopen(
                f"https://127.0.0.1:{port}/v1/health", timeout=5)
        text = str(e.value).lower()
        assert "certificate" in text or "verify" in text, e.value
    finally:
        server.should_exit = True


def test_tls_verified_client_with_staged_trust(tmp_path, monkeypatch):
    """The supported resolution path: stage the self-signed cert as a
    trust anchor (or use a real CA in production) -> verified 200 with
    hostname checking ON."""
    server, port, (_, cert_pem) = _server(tmp_path, monkeypatch, tls=True)
    try:
        trust = tmp_path / "trust.pem"
        trust.write_bytes(cert_pem)
        ctx = ssl.create_default_context(cafile=str(trust))
        ctx.check_hostname = True
        with urllib.request.urlopen(
                f"https://127.0.0.1:{port}/v1/health", timeout=5,
                context=ctx) as r:
            assert r.status == 200
    finally:
        server.should_exit = True


def test_tls_wrong_trust_anchor_rejected(tmp_path, monkeypatch):
    """A DIFFERENT self-signed anchor (wrong CA) fails closed too —
    trust confusion gets nothing."""
    server, port, _ = _server(tmp_path, monkeypatch, tls=True)
    try:
        other, _, _ = _make_cert(tmp_path, suffix="wrong")  # independent cert
        ctx = ssl.create_default_context(cafile=other)
        with pytest.raises(Exception) as e:
            urllib.request.urlopen(
                f"https://127.0.0.1:{port}/v1/health", timeout=5,
                context=ctx)
        assert "certificate" in str(e.value).lower(), e.value
    finally:
        server.should_exit = True


def test_strict_transport_exemptions(tmp_path, monkeypatch):
    """ZARA_REQUIRE_HTTPS=1: loopback plain HTTP stays allowed (dev);
    any non-loopback SOURCE address is refused with a doc pointer. The
    non-loopback leg uses this host's real LAN interface address."""
    server, port, _ = _server(tmp_path, monkeypatch,
                              tls=False, require_https=True)
    try:
        # loopback exemption end-to-end
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/health", timeout=5) as r:
            assert r.status == 200
        lan = _lan_ip()
        if lan:
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(
                    f"http://{lan}:{port}/v1/health", timeout=5)
            assert e.value.code == 403  # noqa: PT017
            assert b"TRANSPORT_TLS" in e.value.read()  # noqa: PT017
        else:
            # loopback-only CI host: non-loopback semantics covered by
            # the forwarded-proto downgrade denial below.
            pass
    finally:
        server.should_exit = True


def test_strict_transport_forwarded_downgrade_denied(tmp_path, monkeypatch):
    """A spoofed/proxied x-forwarded-proto != https on a plain-HTTP
    (loopback-plausible) request is refused: forwarding headers may not
    downgrade the transport classification."""
    server, port, _ = _server(tmp_path, monkeypatch,
                              tls=False, require_https=True)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/health",
            headers={"X-Forwarded-Proto": "http"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 403  # noqa: PT017
    finally:
        server.should_exit = True


def test_strict_transport_off_by_default(tmp_path, monkeypatch):
    """Default (ZARA_REQUIRE_HTTPS unset) keeps today's dev behavior:
    plain loopback http served, no downgrade logic."""
    monkeypatch.delenv("ZARA_REQUIRE_HTTPS", raising=False)
    server, port, _ = _server(tmp_path, monkeypatch,
                              tls=False, require_https=False)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/health",
            headers={"X-Forwarded-Proto": "http"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
    finally:
        server.should_exit = True


def test_linux_agent_over_verified_tls(tmp_path, monkeypatch):
    """Full DeviceAgent flow against an https Core with staged trust:
    urllib reads SSL_CERT_FILE at context creation, so pointing it at
    the staged self-signed CA gives a VERIFIED full chain — claim,
    signed register, heartbeat — no insecure flags anywhere."""
    server, port, (_, cert_pem) = _server(tmp_path, monkeypatch, tls=True)
    try:
        trust = tmp_path / "trust.pem"
        trust.write_bytes(cert_pem)
        monkeypatch.setenv("SSL_CERT_FILE", str(trust))
        ctx = ssl.create_default_context()  # honors SSL_CERT_FILE
        ctx.check_hostname = True
        from device.linux.agent import DeviceAgent
        agent = DeviceAgent(f"https://127.0.0.1:{port}", "tls-agent",
                            key_file=str(tmp_path / "k"))
        # operator-side enroll also over verified TLS (dev token)
        req = urllib.request.Request(
            f"https://127.0.0.1:{port}/v1/agent/enroll", method="POST",
            data=b'{"device_id": "tls-agent", "kind": "linux"}',
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer dev-token"})
        with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
            import json
            code = json.load(r)["pairing_code"]
        agent.claim(code)
        agent.load_key()
        assert agent.register()
        assert agent.heartbeat()
        # raw key no longer travels; proofs over validated TLS
        def _boom():
            raise AssertionError("raw key travelled on TLS transport")
        agent._headers = _boom
        assert agent.heartbeat()
    finally:
        server.should_exit = True
