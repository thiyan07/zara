"""Device credential persistence (ZARA_DEVICE_AUTH_DB): the honest
core-restart reconnect story.

Deliverable: a previously enrolled TRUSTED device keeps verifying its
stored key across a Core restart — no manual re-pair, no trust bypass:
- Revoked/expired/unclaimed states fail closed after restart.
- Pairing codes stay single-use and window-bound; consumed/expired
  pending rows are purged at restore.
- Only SHA-256 DIGESTS are persisted (no secret material at rest).
- Concurrent claims race safely: exactly one winner.

Memory-mode behavior is unchanged (empty path = DeviceAuthStore)."""
from datetime import datetime, timedelta, timezone
import tempfile
import threading

import pytest

from core.device_auth import DeviceAuthStore, PersistentDeviceAuthStore
from core.models import DeviceKind, utcnow


# ---------- helpers ----------

def _pair(store: DeviceAuthStore, device_id: str = "zara-emulator"):
    code = store.enroll(device_id, DeviceKind.ANDROID)
    _id, key = store.claim(code)
    assert _id == device_id
    return code, key


# ---------- initial trusted enrollment ----------

def test_P1_enroll_claim_then_reconnect_after_restart():
    """Restart simulation: a NEW store over the same DB verifies the
    previously claimed device key — automatic reconnect, no re-pair."""
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/device_auth.db"
        s1 = PersistentDeviceAuthStore(path)
        _code, key = _pair(s1)
        assert s1.verify("zara-emulator", key)
        # Core restart: fresh process, same database.
        s2 = PersistentDeviceAuthStore(path)
        assert s2.verify("zara-emulator", key)
        assert not s2.is_revoked("zara-emulator")


def test_P2_restart_with_fabric_tombstones_revocations_survive():
    """A device revoked before a restart stays revoked after it; its old
    key (and any key) fails, and re-pairing works only through a fresh
    operator enroll (the credential-lifting path the protocol already
    has: claim clears the tombstone)."""
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/device_auth.db"
        s1 = PersistentDeviceAuthStore(path)
        _, key = _pair(s1, "phone-1")
        assert s1.revoke("phone-1")
        assert not s1.verify("phone-1", key)
        s2 = PersistentDeviceAuthStore(path)
        assert s2.is_revoked("phone-1")
        assert not s2.verify("phone-1", key)
        # Re-enroll (operator recovery) → fresh pairing code mints and
        # the new credential verifies; the OLD key must stay dead.
        _, new_key = _pair(s2, "phone-1")
        assert s2.verify("phone-1", new_key)
        assert not s2.verify("phone-1", key)


def test_P3_pairing_code_survives_restart_and_is_single_use():
    """An unclaimed code minted before restart can still be claimed once
    after restart; a second claim with the same code is refused."""
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/device_auth.db"
        s1 = PersistentDeviceAuthStore(path)
        code = s1.enroll("laptop-1")  # unclaimed
        s2 = PersistentDeviceAuthStore(path)
        _id, key = s2.claim(code)
        assert _id == "laptop-1"
        with pytest.raises(ValueError):
            _ = s2.claim(code)


def test_P4_expired_pairing_code_fails_closed_after_restart():
    """The pairing window is enforced from the stored credential expiry:
    a code minted 'long ago' (fake stale timestamps) mints no key."""
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/device_auth.db"
        s1 = PersistentDeviceAuthStore(path)
        code = s1.enroll("phone-2")
        # Force the credential's expiry into the past (simulates a code
        # that crossed its 600 s window before the restart).
        cred = s1._creds["phone-2"]
        cred.expires_at = (utcnow() - timedelta(seconds=1)).isoformat()
        s1._save_cred(cred)
        # drop the still-fresh row bookkeeping isn't needed: pending row
        # lives; the expiry gate is what refuses the claim.
        s2 = PersistentDeviceAuthStore(path)
        with pytest.raises(ValueError):
            _ = s2.claim(code)


def test_P5_expired_device_key_fails_after_restart():
    """A key past its stored expiry verifies nowhere after restart."""
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/device_auth.db"
        s = PersistentDeviceAuthStore(path)
        _, key = _pair(s, "phone-3")
        cred = s._creds["phone-3"]
        cred.expires_at = (utcnow() - timedelta(seconds=1)).isoformat()
        s._save_cred(cred)
        assert not s.verify("phone-3", key)


def test_P6_revoked_pending_pairing_is_dropped():
    """Revoke before claim: the pending code must not mint a key, both
    in the live store and after a restart (rows purge on revoke)."""
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/device_auth.db"
        s = PersistentDeviceAuthStore(path)
        code = s.enroll("phone-4")
        assert s.revoke("phone-4")
        with pytest.raises(ValueError):
            _ = s.claim(code)
        # No lingering pending row is resurrected after restart:
        s2 = PersistentDeviceAuthStore(path)
        with pytest.raises(ValueError):
            _ = s2.claim(code)


def test_P7_concurrent_claims_single_winner():
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/device_auth.db"
        s = PersistentDeviceAuthStore(path)
        code = s.enroll("phone-5")
        results = []
        errors = []

        def _claim():
            try:
                results.append(s.claim(code)[1])
            except ValueError as e:
                errors.append(str(e))

        threads = [threading.Thread(target=_claim) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 1  # one winner mints the key
        assert len(errors) == 7  # every loser fails closed
        assert not s.verify("phone-5", "")


def test_P8_unregistered_device_and_unknown_key_rejected():
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/device_auth.db"
        s = PersistentDeviceAuthStore(path)
        _pair(s, "phone-6")
        _pair(s, "phone-7")
        # wrong device using another device's key impossible server-side
        assert not s.verify("phone-6", "not-a-key")
        assert not s.verify("unknown-device", "")
        # restarting does not magically admit unknown devices
        s2 = PersistentDeviceAuthStore(path)
        assert not s2.verify("unknown-device", "")


def test_P9_rotation_survives_restart():
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/device_auth.db"
        s1 = PersistentDeviceAuthStore(path)
        _, key = _pair(s1, "phone-8")
        new_key = s1.rotate("phone-8", key)
        assert not s1.verify("phone-8", key)
        assert s1.verify("phone-8", new_key)
        s2 = PersistentDeviceAuthStore(path)
        assert not s2.verify("phone-8", key)
        assert s2.verify("phone-8", new_key)


def test_P10_no_secret_material_at_rest():
    """Only digests may touch the DB: the raw device key and pairing
    code must not appear anywhere in the persisted bytes."""
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/device_auth.db"
        s = PersistentDeviceAuthStore(path)
        code, key = _pair(s, "phone-9")
        blob = open(path, "rb").read()
        blob += code.encode()  # also the unclaimed-code path is digest-only
        assert key.encode() not in blob
        assert code.encode() not in open(path, "rb").read()


def test_P11_memory_mode_unchanged():
    """Empty path keeps the plain memory store (tests / ephemeral dev)."""
    s = DeviceAuthStore()
    assert type(s) is DeviceAuthStore
    _, key = _pair(s, "mem-1")
    assert s.verify("mem-1", key)


def test_P12_duplicate_enroll_rejected_while_paired_alive():
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/device_auth.db"
        s = PersistentDeviceAuthStore(path)
        _pair(s, "phone-10")
        with pytest.raises(ValueError):
            s.enroll("phone-10")


def test_P15_credential_db_is_owner_only():
    """: The DB holding credential digests is chmod 0600 (the same
    discipline as the device-side key files), not world-readable."""
    import os
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/device_auth.db"
        PersistentDeviceAuthStore(path)
        assert (os.stat(path).st_mode & 0o777) == 0o600


# ---------- HTTP-level restart integration ----------
#
# The restart semantic asserted here is exactly the live experience of a
# device agent: register/heartbeat with the CLAIMED key must succeed on
# a fresh Core process (server restart), a forged key is 403, and a
# formally revoked device stays 403 after restart.

def test_P13_http_reconnect_after_core_restart_no_repair():
    from core.app import build_stack, create_app
    from fastapi.testclient import TestClient
    H = {"Authorization": "Bearer dev-token"}
    with tempfile.TemporaryDirectory() as d:
        adb = f"{d}/device_auth.db"
        stack1 = build_stack(device_auth_db=adb, audit_db=":memory:")
        c1 = TestClient(create_app(stack1))
        r = c1.post("/v1/agent/enroll", json={"device_id": "droide",
                                              "kind": "android"}, headers=H)
        assert r.status_code == 200
        claim = c1.post("/v1/agent/claim",
                        json={"pairing_code": r.json()["pairing_code"]})
        assert claim.status_code == 200
        key = claim.json()["device_key"]
        dh = {"X-Device-Id": "droide", "X-Device-Key": key}
        assert c1.post("/v1/agent/register",
                       json={"capabilities": ["system.battery"],
                             "software_version": "t", "kind": "android"},
                       headers=dh).status_code == 200
        # --- Core restart (new app over the same DB) ---
        stack2 = build_stack(device_auth_db=adb, audit_db=":memory:")
        c2 = TestClient(create_app(stack2))
        reg2 = c2.post("/v1/agent/register",
                       json={"capabilities": ["system.battery"],
                             "software_version": "t", "kind": "android"},
                       headers=dh)
        assert reg2.status_code == 200  # automatic reconnect, no re-pair
        hb = c2.post("/v1/agent/heartbeat",
                     json={"charging": False, "network": "wifi",
                           "online": True}, headers=dh)
        assert hb.status_code == 200
        # forged key on the restarted core is 403
        forged = dict(dh)
        forged["X-Device-Key"] = key + "x"
        assert c2.post("/v1/agent/heartbeat",
                       json={"charging": False, "network": "wifi",
                             "online": True}, headers=forged).status_code \
            == 403
        # unknown device pairing code replay is refused
        assert c2.post("/v1/agent/claim",
                       json={"pairing_code": r.json()["pairing_code"]}
                       ).status_code == 403


def test_P14_http_revoked_device_cannot_reconnect_after_restart():
    from core.app import build_stack, create_app
    from fastapi.testclient import TestClient
    H = {"Authorization": "Bearer dev-token"}
    with tempfile.TemporaryDirectory() as d:
        adb = f"{d}/device_auth.db"
        stack1 = build_stack(device_auth_db=adb, audit_db=":memory:")
        c1 = TestClient(create_app(stack1))
        r = c1.post("/v1/agent/enroll", json={"device_id": "laptop-2",
                                              "kind": "linux"}, headers=H)
        claim = c1.post("/v1/agent/claim",
                        json={"pairing_code": r.json()["pairing_code"]})
        key = claim.json()["device_key"]
        dh = {"X-Device-Id": "laptop-2", "X-Device-Key": key}
        assert c1.post("/v1/agent/register",
                       json={"capabilities": ["system.battery"],
                             "software_version": "t", "kind": "linux"},
                       headers=dh).status_code == 200
        assert c1.post("/v1/agent/revoke?device_id=laptop-2",
                       headers=H).status_code == 200
        assert c1.post("/v1/agent/jobs/poll", headers=dh).status_code == 403
        # --- restart: revoked credential stays dead on the new process ---
        stack2 = build_stack(device_auth_db=adb, audit_db=":memory:")
        c2 = TestClient(create_app(stack2))
        assert c2.post("/v1/agent/register",
                       json={"capabilities": ["system.battery"],
                             "software_version": "t", "kind": "linux"},
                       headers=dh).status_code == 403
        assert c2.post("/v1/agent/heartbeat",
                       json={"charging": False, "network": "wifi",
                             "online": True}, headers=dh).status_code == 403
        assert c2.post("/v1/agent/jobs/poll", headers=dh).status_code == 403
        # recovery is deliberate: fresh operator enroll mints a usable
        # credential that reconnects; the old revoked key never does
        r2 = c2.post("/v1/agent/enroll", json={"device_id": "laptop-2",
                                               "kind": "linux"}, headers=H)
        claim2 = c2.post("/v1/agent/claim",
                         json={"pairing_code": r2.json()["pairing_code"]})
        dh2 = {"X-Device-Id": "laptop-2",
               "X-Device-Key": claim2.json()["device_key"]}
        # The recovered device re-registers with its fresh credential
        # (the normal agent boot sequence), then heartbeats.
        assert c2.post("/v1/agent/register",
                       json={"capabilities": ["system.battery"],
                             "software_version": "t", "kind": "linux"},
                       headers=dh2).status_code == 200
        assert c2.post("/v1/agent/heartbeat",
                       json={"charging": False, "network": "wifi",
                             "online": True}, headers=dh2).status_code == 200
