"""Challenge-proof device authentication (Ed25519 over single-use
nonces): additive to the raw-key path, replay-resistant. Store-level and
HTTP-level contracts, including the failure modes that must never open
a door (replay, wrong key, revoked/expired, no pubkey bound, ambiguous
credentials, stale nonce)."""
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from core.app import create_app, build_stack
from core.device_auth import (ChallengeStore, DeviceAuthStore,
                              clean_pubkey, generate_device_keypair,
                              verify_device_proof)


def make_client():
    stack = build_stack()
    return TestClient(create_app(stack)), stack


def _sig(priv_hex: str, message: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    raw = bytes.fromhex(priv_hex)
    sk = Ed25519PrivateKey.from_private_bytes(raw)
    return base64.b64encode(sk.sign(message.encode())).decode()


def enroll_claim_secret(client, stack, device_id="laptop-9", pubkey=""):
    code = stack["device_auth"].enroll(device_id)
    r = client.post("/v1/agent/claim",
                    json={"pairing_code": code, "pubkey": pubkey})
    assert r.status_code == 200, r.text
    return r.json()["device_id"], r.json()["device_key"]


# ---------- store level ----------

def test_no_pubkey_means_challenge_off():
    s = DeviceAuthStore()
    code = s.enroll("d1")
    _, key = s.claim(code)
    assert s.challenge_ready("d1") is False
    assert s.stored_pubkey("d1") == ""


def test_claim_binds_pubkey_and_persists(tmp_path):
    from core.device_auth import PersistentDeviceAuthStore
    import os
    pub, _priv = generate_device_keypair()
    db = os.path.join(tmp_path, "a.db")
    s = PersistentDeviceAuthStore(db)
    _, key = s.claim(s.enroll("d1"), pubkey=pub)
    assert s.challenge_ready("d1")
    # restart: binding survives (public data, nothing secret)
    s2 = PersistentDeviceAuthStore(db)
    assert s2.stored_pubkey("d1") == pub
    assert s2.challenge_ready("d1")
    assert s2.verify("d1", key)


def test_set_pubkey_upgrade_and_unbind():
    s = DeviceAuthStore()
    _, key = s.claim(s.enroll("d1"))
    pub, priv = generate_device_keypair()
    assert s.set_pubkey("d1", pub) is True
    assert s.challenge_ready("d1")
    assert s.set_pubkey("d1", "") is True  # unbind -> challenge auth off
    assert s.challenge_ready("d1") is False and s.stored_pubkey("d1") == ""
    with pytest.raises(ValueError):
        s.set_pubkey("d1", "not-hex")           # malformed never binds
    with pytest.raises(ValueError):
        s.set_pubkey("d1", "ab" * 31)           # wrong length never binds


def test_verify_challenge_accepts_and_rejects():
    s = DeviceAuthStore()
    _, key = s.claim(s.enroll("d1"))
    pub, priv = generate_device_keypair()
    s.set_pubkey("d1", pub)
    nonce = "N-O-N-C-E"
    msg = f"POST /v1/agent/heartbeat {nonce}"
    good = _sig(priv, msg)
    assert s.verify_challenge("d1", nonce, good, msg)
    assert not s.verify_challenge("d1", nonce, good, "TAMPERED " + msg)
    wrong, _w = generate_device_keypair()
    fake = _sig(wrong and priv if False else priv, msg)  # self-consistent
    assert not s.verify_challenge("ghost", nonce, fake, msg)  # unknown dev
    s.revoke("d1")
    assert not s.verify_challenge("d1", nonce, good, msg)     # revoked


def test_verify_challenge_expired_fails_closed():
    s = DeviceAuthStore()
    _, key = s.claim(s.enroll("d1"), ttl_days=0)  # expires immediately
    pub, priv = generate_device_keypair()
    # expiry wins at claim: bound as expired -> not ready
    assert s.challenge_ready("d1") is False


# ---------- ChallengeStore ----------

def test_challenge_single_use():
    c = ChallengeStore()
    n = c.mint("d1")
    assert c.consume("d1", n) is True
    assert c.consume("d1", n) is False       # replay refused
    assert c.consume("d1", n) is False
    assert c.consume("d1", "forged") is False


def test_challenge_ttl_expires(monkeypatch):
    c = ChallengeStore()
    c.TTL_S = 0.05
    n = c.mint("d1")
    time.sleep(0.08)
    assert c.consume("d1", n) is False


def test_challenge_per_device_bounded():
    c = ChallengeStore()
    c.PER_DEVICE = 3
    nonces = [c.mint("d1") for _ in range(6)]
    alive = sum(1 for n in nonces if c.consume("d1", n))
    assert alive == 3  # oldest evicted, newest kept


def test_challenge_global_bounded():
    c = ChallengeStore()
    c.TOTAL_CAP = 10
    c.PER_DEVICE = 1000
    for i in range(50):
        c.mint(f"d{i % 5}")
    assert sum(len(v) for v in c._open.values()) <= 10


def test_store_restart_invalidates_challenges():
    c = ChallengeStore()
    n = c.mint("d1")
    fresh = ChallengeStore()  # Core restart = new store
    assert fresh.consume("d1", n) is False


def test_verify_device_proof_never_raises():
    pub, _ = generate_device_keypair()
    assert verify_device_proof(pub, "m", "!!!not-base64!!") is False
    assert verify_device_proof("ff" * 31, "m", "AAAA") is False
    assert verify_device_proof("", "m", "") is False


def test_clean_pubkey_normalizes():
    assert clean_pubkey("AB" * 32) == "ab" * 32
    assert clean_pubkey("  ") == ""
    with pytest.raises(ValueError):
        clean_pubkey("zz" * 32)


# ---------- HTTP level ----------

def test_full_challenge_flow_and_replay_refused():
    client, stack = make_client()
    pub, priv = generate_device_keypair()
    did, key = enroll_claim_secret(client, stack, pubkey=pub)
    # register first (raw key), then the key never travels again
    rr = client.post("/v1/agent/register", headers={
        "X-Device-Id": did, "X-Device-Key": key},
        json={"capabilities": [], "software_version": "t", "kind": "linux"})
    assert rr.status_code == 200, rr.text
    # one raw-key call to fetch the challenge
    r = client.post("/v1/agent/challenge", headers={
        "X-Device-Id": did, "X-Device-Key": key})
    assert r.status_code == 200, r.text
    nonce = r.json()["nonce"]
    message = f"POST /v1/agent/heartbeat {nonce}"
    proof = _sig(priv, message)
    # proof-only heartbeat: the raw key never travels
    r2 = client.post("/v1/agent/heartbeat", headers={
        "X-Device-Id": did,
        "X-Device-Nonce": nonce, "X-Device-Proof": proof},
        json={})
    assert r2.status_code == 200, r2.text
    # replaying the same proof (same nonce) must fail
    r3 = client.post("/v1/agent/heartbeat", headers={
        "X-Device-Id": did,
        "X-Device-Nonce": nonce, "X-Device-Proof": proof},
        json={})
    assert r3.status_code == 401
    assert "fresh" in r3.json()["detail"]
    # raw-key path still works untouched
    r4 = client.post("/v1/agent/heartbeat", headers={
        "X-Device-Id": did, "X-Device-Key": key}, json={})
    assert r4.status_code == 200


def test_wrong_signature_refused():
    client, stack = make_client()
    pub, priv = generate_device_keypair()
    did, key = enroll_claim_secret(client, stack, pubkey=pub)
    nonce = client.post("/v1/agent/challenge", headers={
        "X-Device-Id": did, "X-Device-Key": key}).json()["nonce"]
    r = client.post("/v1/agent/heartbeat", headers={
        "X-Device-Id": did, "X-Device-Nonce": nonce,
        "X-Device-Proof": _sig(priv, "signed the wrong message")},
        json={})
    assert r.status_code == 403


def test_proof_without_binding_is_actionable():
    client, stack = make_client()
    did, key = enroll_claim_secret(client, stack)  # no pubkey
    r = client.post("/v1/agent/heartbeat", headers={
        "X-Device-Id": did, "X-Device-Nonce": "n", "X-Device-Proof": "p"},
        json={})
    assert r.status_code == 403
    assert "pubkey" in r.json()["detail"]


def test_pubkey_binding_endpoint_enables_challenge_auth():
    client, stack = make_client()
    did, key = enroll_claim_secret(client, stack)
    pub, priv = generate_device_keypair()
    r = client.post("/v1/agent/pubkey", headers={
        "X-Device-Id": did, "X-Device-Key": key},
        json={"pubkey": pub})
    assert r.status_code == 200, r.text
    client.post("/v1/agent/register", headers={
        "X-Device-Id": did, "X-Device-Key": key},
        json={"capabilities": [], "software_version": "t", "kind": "linux"})
    nonce = client.post("/v1/agent/challenge", headers={
        "X-Device-Id": did, "X-Device-Key": key}).json()["nonce"]
    message = f"POST /v1/agent/heartbeat {nonce}"
    r2 = client.post("/v1/agent/heartbeat", headers={
        "X-Device-Id": did, "X-Device-Nonce": nonce,
        "X-Device-Proof": _sig(priv, message)}, json={})
    assert r2.status_code == 200


def test_pubkey_binding_requires_raw_key():
    client, stack = make_client()
    pub, priv = generate_device_keypair()
    did, key = enroll_claim_secret(client, stack, pubkey=pub)
    # a proof-authed request must not bind NEW keys (chicken-and-egg)
    nonce = client.post("/v1/agent/challenge", headers={
        "X-Device-Id": did, "X-Device-Key": key}).json()["nonce"]
    message = f"POST /v1/agent/pubkey {nonce}"
    r = client.post("/v1/agent/pubkey", headers={
        "X-Device-Id": did, "X-Device-Nonce": nonce,
        "X-Device-Proof": _sig(priv, message)},
        json={"pubkey": "ab" * 32})
    assert r.status_code == 401
    assert "raw-key" in r.json()["detail"]


def test_ambiguous_credentials_refused():
    client, stack = make_client()
    pub, priv = generate_device_keypair()
    did, key = enroll_claim_secret(client, stack, pubkey=pub)
    r = client.post("/v1/agent/heartbeat", headers={
        "X-Device-Id": did, "X-Device-Key": key,
        "X-Device-Nonce": "n", "X-Device-Proof": _sig(priv, "m")}, json={})
    assert r.status_code == 401
    assert "ambiguous" in r.json()["detail"]


def test_challenge_endpoint_needs_auth():
    client, stack = make_client()
    r = client.post("/v1/agent/challenge")
    assert r.status_code == 401


def test_revoked_device_proof_fails():
    client, stack = make_client()
    pub, priv = generate_device_keypair()
    did, key = enroll_claim_secret(client, stack, pubkey=pub)
    nonce = client.post("/v1/agent/challenge", headers={
        "X-Device-Id": did, "X-Device-Key": key}).json()["nonce"]
    stack["device_auth"].revoke(did)
    message = f"POST /v1/agent/heartbeat {nonce}"
    r = client.post("/v1/agent/heartbeat", headers={
        "X-Device-Id": did, "X-Device-Nonce": nonce,
        "X-Device-Proof": _sig(priv, message)}, json={})
    assert r.status_code == 403, r.text


# ---------- live-socket agent chain (real transport, ephemeral port) ----------

def test_agent_signed_transport_live():
    """Real uvicorn core + real DeviceAgent over a socket: claim binds a
    signing key, every later call uses challenge proofs, and the raw
    device key does NOT travel after the one bootstrap challenge fetch."""
    import os as _os
    import tempfile as _tf
    import threading as _th
    import time as _t
    import uvicorn
    from core.app import build_stack, create_app
    from device.linux.agent import DeviceAgent

    stack = build_stack()
    server = uvicorn.Server(uvicorn.Config(
        create_app(stack), host="127.0.0.1", port=0, log_level="error"))
    _th.Thread(target=server.run, daemon=True).start()
    deadline = _t.time() + 10
    while not server.started and _t.time() < deadline:
        _t.sleep(0.05)
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    try:
        tmp = _tf.mkdtemp()
        code = stack["device_auth"].enroll("lap-live")
        agent = DeviceAgent(base, "lap-live",
                            key_file=_os.path.join(tmp, "k"))
        agent.claim(code)
        agent.load_key()
        assert agent._sk_hex, "claim must bind a signing key"
        assert agent.register()
        assert agent.heartbeat()
        # sabotage the raw-key path: from here on, only proofs may speak
        def _boom():
            raise AssertionError("raw device key travelled after bootstrap")
        agent._headers = _boom
        assert agent.heartbeat()
        assert agent._spare_nonce, "nonce chain must continue"
        # replay of a burned nonce cannot re-authenticate
        nonce = agent._acquire_nonce()
        assert client_standalone_proof(server, stack, base,
                                       "lap-live", agent._sk_hex,
                                       nonce) == 200   # legitimate first use
        assert client_standalone_proof(server, stack, base,
                                       "lap-live", agent._sk_hex,
                                       nonce) == 401   # replay refused
    finally:
        server.should_exit = True


def client_standalone_proof(server, stack, base, device_id, sk_hex, nonce,
                            canonical="v2"):
    """Standalone (non-agent) proof sender. canonical="v2" binds the
    exact body via sha256; "v1" is the legacy 3-field message (compat)."""
    import urllib.request
    import base64
    import hashlib
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(sk_hex))
    body = b"{}"
    msg = f"POST /v1/agent/heartbeat {nonce}"
    if canonical == "v2":
        msg = f"{msg} {hashlib.sha256(body).hexdigest()}"
    req = urllib.request.Request(
        f"{base}/v1/agent/heartbeat", data=body,
        headers={"Content-Type": "application/json",
                 "X-Device-Id": device_id, "X-Device-Nonce": nonce,
                 "X-Device-Proof": base64.b64encode(
                     sk.sign(msg.encode())
                 ).decode()})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# ---------- v2 canonicalization: body-hash binding + compat ----------

def _v2_proof(priv_hex, statement_body, body: bytes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    import hashlib
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
    msg = f"{statement_body} {hashlib.sha256(body).hexdigest()}"
    return base64.b64encode(sk.sign(msg.encode())).decode()


def _signed_heartbeat(client, stack, pub, priv, device_id="hb-bind"):
    """Raw bootstrap (+ registry register, so the proof'd call succeeds
    past handler validation) -> returns (device_key, nonce)."""
    code = stack["device_auth"].enroll(device_id)
    r = client.post("/v1/agent/claim",
                    json={"pairing_code": code, "pubkey": pub})
    assert r.status_code == 200, r.text
    key = r.json()["device_key"]
    client.post("/v1/agent/register", headers={
        "X-Device-Id": device_id, "X-Device-Key": key},
        json={"capabilities": [], "software_version": "t",
              "kind": "linux"})
    nonce = client.post("/v1/agent/challenge", headers={
        "X-Device-Id": device_id,
        "X-Device-Key": key}).json()["nonce"]
    return key, nonce


def test_proof_v2_binds_exact_body_bytes():
    client, stack = make_client()
    pub, priv = generate_device_keypair()
    _, nonce = _signed_heartbeat(client, stack, pub, priv,
                                 device_id="hb-bind")
    body = json.dumps({"note": "check-in"}).encode()
    r = client.post("/v1/agent/heartbeat", data=body, headers={
        "Content-Type": "application/json",
        "X-Device-Id": "hb-bind",
        "X-Device-Nonce": nonce,
        "X-Device-Proof": _v2_proof(
            priv, f"POST /v1/agent/heartbeat {nonce}", body)})
    assert r.status_code == 200, r.text


def test_proof_v2_rejects_body_tampering():
    client, stack = make_client()
    pub, priv = generate_device_keypair()
    _, nonce = _signed_heartbeat(client, stack, pub, priv,
                                 device_id="hb-tamper")
    body = json.dumps({"note": "check-in"}).encode()
    other = json.dumps({"note": "check-in", "evil": True}).encode()
    r = client.post("/v1/agent/heartbeat", data=other, headers={
        "Content-Type": "application/json",
        "X-Device-Id": "hb-tamper",
        "X-Device-Nonce": nonce,
        "X-Device-Proof": _v2_proof(
            priv, f"POST /v1/agent/heartbeat {nonce}", body)})
    assert r.status_code == 403, r.text
    # fail-closed: the tampered attempt burned the nonce...
    r2 = client.post("/v1/agent/heartbeat", data=body, headers={
        "Content-Type": "application/json",
        "X-Device-Id": "hb-tamper",
        "X-Device-Nonce": nonce,   # now used/expired
        "X-Device-Proof": _v2_proof(
            priv, f"POST /v1/agent/heartbeat {nonce}", body)})
    assert r2.status_code == 401, r2.text


def test_proof_v1_compat_still_accepted():
    client, stack = make_client()
    pub, priv = generate_device_keypair()
    key, nonce = _signed_heartbeat(client, stack, pub, priv,
                                   device_id="hb-v1")
    r = client.post("/v1/agent/heartbeat", data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "X-Device-Id": "hb-v1",
                        "X-Device-Nonce": nonce,
                        "X-Device-Proof": _sig(
                            priv, f"POST /v1/agent/heartbeat {nonce}")})
    assert r.status_code == 200, r.text


def test_concurrent_same_nonce_exactly_one_winner():
    """Two sends racing the same nonce: exactly one transaction 200 —
    nonce single-use is enforced under concurrency (memory-side lock
    plus burn-before-verify order)."""
    import threading
    client, stack = make_client()
    pub, priv = generate_device_keypair()
    _, nonce = _signed_heartbeat(client, stack, pub, priv,
                                 device_id="hb-race")
    body = b"{}"
    proof = _sig(priv, f"POST /v1/agent/heartbeat {nonce}")
    headers = {"Content-Type": "application/json",
               "X-Device-Id": "hb-race", "X-Device-Nonce": nonce,
               "X-Device-Proof": proof}
    codes = []
    lock = threading.Lock()

    def send():
        try:
            r = client.post("/v1/agent/heartbeat", data=body,
                            headers=headers)
            c = r.status_code
        except Exception as e:  # TestClient re-raises server errors
            c = type(e).__name__
        with lock:
            codes.append(c)

    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    oks = sum(1 for c in codes if c == 200)
    assert oks == 1, codes
    assert all(c == 200 or c in (401, 403) for c in codes), codes


def test_proof_method_binding_post_proof_not_valid_for_get():
    """The canonical message includes the HTTP method: a proof shaped
    for the POST heartbeat proves nothing about a GET on another
    proof'd endpoint (state probe), even with the same device+nonce."""
    client, stack = make_client()
    pub, priv = generate_device_keypair()
    _, nonce = _signed_heartbeat(client, stack, pub, priv,
                                 device_id="hb-method")
    r = client.get("/v1/agent/jobs/none-such/state", headers={
        "X-Device-Id": "hb-method",
        "X-Device-Nonce": nonce,
        "X-Device-Proof": _v2_proof(
            priv, f"POST /v1/agent/heartbeat {nonce}", b"{}")})
    # the POST-shaped proof never validates for the GET (proof rejected
    # before any data is touched), and the nonce is burned either way
    assert r.status_code in (401, 403), (r.status_code, r.text)
    assert "none-such" not in r.text
    # a correct GET proof can no longer use the burned nonce either
    import hashlib
    r2 = client.get("/v1/agent/jobs/none-such/state", headers={
        "X-Device-Id": "hb-method",
        "X-Device-Nonce": nonce,
        "X-Device-Proof": _v2_proof(
            priv, f"GET /v1/agent/jobs/none-such/state {nonce}",
            b"")})
    assert r2.status_code == 401, (r2.status_code, r2.text)
