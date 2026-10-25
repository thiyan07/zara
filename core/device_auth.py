"""Device authentication — per-device identity, no shared secrets.

Flow: enroll (dev-operator, pairing code) -> claim (device, one-time) ->
per-device key stored 0600 on the device. Core verifies via key hash.
Dev token remains for local development only.
"""
from __future__ import annotations
import base64
import hashlib
import secrets
import threading
from datetime import timedelta
from typing import Optional
from pydantic import BaseModel
from .models import DeviceKind, utcnow


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class DeviceCredential(BaseModel):
    device_id: str
    kind: DeviceKind = DeviceKind.LINUX
    key_hash: str = ""
    pairing_code_hash: str = ""
    paired: bool = False
    revoked: bool = False
    created_at: str = ""
    expires_at: Optional[str] = None
    pubkey: str = ""  # Ed25519 public key (hex) for challenge proofs


class ChallengeStore:
    """Single-use, TTL-bounded auth challenges per device (memory only).

    A device proves possession of its signing key for ONE request by
    answering a server-minted nonce; the nonce dies on use, so a replay
    of the same proof can never re-authenticate. Core restarts invalidate
    all open challenges (clients re-challenge or fall back to raw-key
    auth — both paths stay honest). Only nonce DIGESTS are kept; the
    challenge itself is worthless without the device's private key.
    """

    TTL_S = 300          # a stale nonce mints no proof
    PER_DEVICE = 5       # cap open challenges per device
    TOTAL_CAP = 1000     # global bound; minting stays O(1)

    def __init__(self) -> None:
        self._open: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def mint(self, device_id: str) -> str:
        """Atomically mint one nonce for the device (bounded state)."""
        import time as _t
        nonce = secrets.token_urlsafe(32)
        digest = _hash(nonce)
        now = _t.monotonic()
        with self._lock:
            slot = self._open.setdefault(device_id, {})
            # prune expired everywhere, globally evict the oldest on overflow
            for d, n in self._open.items():
                for h, exp in list(n.items()):
                    if exp <= now:
                        del n[h]
            while sum(len(n) for n in self._open.values()) >= self.TOTAL_CAP:
                oldest = min(((exp, d, h) for d, n in self._open.items()
                              for h, exp in n.items()), default=None)
                if oldest is None:
                    break
                del self._open[oldest[1]][oldest[2]]
            while len(slot) >= self.PER_DEVICE:
                oldest = min(slot.items(), key=lambda kv: kv[1])
                del slot[oldest[0]]
            slot[digest] = now + self.TTL_S
        return nonce

    def consume(self, device_id: str, nonce: str) -> bool:
        """Atomically validate-and-burn one open challenge."""
        import time as _t
        digest = _hash(nonce)
        now = _t.monotonic()
        with self._lock:
            slot = self._open.get(device_id)
            if not slot or digest not in slot:
                return False
            if slot.pop(digest) <= now:
                return False
        return True

    def open_count(self, device_id: str) -> int:
        with self._lock:
            return len(self._open.get(device_id, {}))


def clean_pubkey(pubkey: str) -> str:
    """Normalize a caller-supplied Ed25519 public key: lowercase hex,
    exactly 64 hex chars (32 bytes). Empty stays empty (challenge auth
    simply stays off for that device); anything else is a hard error so
    malformed keys can never silently downgrade identity binding."""
    p = (pubkey or "").strip().lower()
    if not p:
        return ""
    if len(p) != 64 or any(c not in "0123456789abcdef" for c in p):
        raise ValueError("pubkey must be 64 hex chars (32-byte Ed25519)")
    return p


def verify_device_proof(pubkey_hex: str, message: str, sig_b64: str) -> bool:
    """Verify an Ed25519 proof signed by the device's registered key.
    Any malformed material verifies False — never raises, so a bad
    client cannot turn proof handling into a 500."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        from cryptography.exceptions import InvalidSignature
        raw = bytes.fromhex(pubkey_hex)
        if len(raw) != 32:
            return False
        Ed25519PublicKey.from_public_bytes(raw).verify(
            base64.b64decode(sig_b64), message.encode())
        return True
    except (InvalidSignature, ValueError, TypeError, ImportError):
        return False


def generate_device_keypair() -> tuple[str, str]:
    """Fresh Ed25519 keypair for a device: (pubkey_hex, privkey_hex).
    The private half never leaves the device's own storage."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    sk = Ed25519PrivateKey.generate()
    return (sk.public_key().public_bytes_raw().hex(),
            sk.private_bytes_raw().hex())


class DeviceAuthStore:
    PAIRING_TTL_S = 600

    def __init__(self) -> None:
        self._creds: dict[str, DeviceCredential] = {}
        self._pending: dict[str, str] = {}  # pairing_hash -> device_id
        self._lock = threading.Lock()

    def enroll(self, device_id: str, kind: DeviceKind = DeviceKind.LINUX) -> str:
        """Operator-side step. Returns a one-time pairing code (show once)."""
        code = f"zara-pair-{secrets.token_urlsafe(9)}"
        with self._lock:
            if device_id in self._creds and not self._creds[device_id].revoked:
                raise ValueError(f"device already enrolled: {device_id}")
            cred = DeviceCredential(device_id=device_id, kind=kind,
                                    pairing_code_hash=_hash(code),
                                    created_at=utcnow().isoformat(),
                                    expires_at=(utcnow() + timedelta(
                                        seconds=self.PAIRING_TTL_S)).isoformat())
            self._creds[device_id] = cred
            self._pending[_hash(code)] = device_id
            self._save_cred(cred, code_hash=_hash(code))
        return code

    def claim(self, pairing_code: str, ttl_days: int = 365,
              pubkey: str = "") -> tuple[str, str]:
        """Device-side step. Exchanges pairing code for a device key (once).
        The device MAY register an Ed25519 public key at the same time to
        enable challenge-proof auth (never replayed raw-key use)."""
        with self._lock:
            device_id = self._pending.pop(_hash(pairing_code), None)
            if device_id is None:
                raise ValueError("invalid or expired pairing code")
            cred = self._creds[device_id]
            if cred.revoked:
                raise ValueError("device revoked")
            # Task 9: the pairing window (PAIRING_TTL_S, set at enroll) is
            # enforced — a stale code mints no key. Missing/malformed
            # expiry fails closed (re-enroll is the recovery path).
            if not cred.expires_at or cred.expires_at < utcnow().isoformat():
                raise ValueError("pairing code expired")
            if cred.paired:
                raise ValueError("pairing code already used")
            pubkey = clean_pubkey(pubkey)
            key = f"zara-dev-{secrets.token_urlsafe(24)}"
            cred.key_hash = _hash(key)
            cred.paired = True
            cred.pairing_code_hash = ""
            cred.expires_at = (utcnow() + timedelta(days=ttl_days)).isoformat()
            cred.pubkey = pubkey
            self._save_cred(cred, drop_pending=_hash(pairing_code))
            return device_id, key

    def verify(self, device_id: str, key: str) -> bool:
        with self._lock:
            cred = self._creds.get(device_id)
            if cred is None or cred.revoked or not cred.paired:
                return False
            if cred.expires_at and cred.expires_at < utcnow().isoformat():
                return False
            return secrets.compare_digest(cred.key_hash, _hash(key))

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            cred = self._creds.get(device_id)
            if cred is None:
                return False
            cred.revoked = True
            # A revoked device must not mint a key from a pairing code
            # issued before revocation: drop its pending entries so
            # claim() cannot succeed without a fresh post-revoke enroll.
            self._pending = {k: v for k, v in self._pending.items()
                             if v != device_id}
            self._save_cred(cred, drop_pending=None)
            return True

    def rotate(self, device_id: str, old_key: str,
               ttl_days: int = 365) -> str:
        """Credential rotation: old key dies with the call, new key returned
        once. A stolen old key is useless after rotation."""
        from datetime import timedelta
        with self._lock:
            cred = self._creds.get(device_id)
            if cred is None or cred.revoked or not cred.paired:
                raise ValueError("unknown or revoked device")
            if not secrets.compare_digest(cred.key_hash, _hash(old_key)):
                raise ValueError("rotation requires the current key")
            new_key = f"zara-dev-{secrets.token_urlsafe(24)}"
            cred.key_hash = _hash(new_key)
            cred.expires_at = (utcnow() + timedelta(days=ttl_days)).isoformat()
            self._save_cred(cred)
            return new_key

    def _save_cred(self, cred: DeviceCredential,
                   code_hash: str = "",
                   drop_pending: str = "") -> None:
        """Persistence hook (no-op in the memory store). Called under the
        lock after every state change. `code_hash` mirrors a fresh
        pending pairing row; `drop_pending` removes one consumed row."""
        del cred, code_hash, drop_pending

    def is_revoked(self, device_id: str) -> bool:
        with self._lock:
            cred = self._creds.get(device_id)
            return cred is None or cred.revoked

    # ---- challenge-proof auth (additive; raw-key path unchanged) ----

    def set_pubkey(self, device_id: str, pubkey: str) -> bool:
        """Bind or replace the device's challenge-signing public key.
        Bound by Core over the raw-key path, which proves possession of
        the device key the same way every request already does. Fail
        closed: unknown/revoked/unpaired/expired devices are refused,
        and a malformed key never silently binds."""
        pubkey = clean_pubkey(pubkey)
        with self._lock:
            cred = self._creds.get(device_id)
            if cred is None or not cred.paired:
                return False
            if cred.revoked:
                return False
            if cred.expires_at and cred.expires_at < utcnow().isoformat():
                return False
            cred.pubkey = pubkey
            self._save_cred(cred)
            return True

    def stored_pubkey(self, device_id: str) -> str:
        with self._lock:
            cred = self._creds.get(device_id)
            return cred.pubkey if cred else ""

    def challenge_ready(self, device_id: str) -> bool:
        """True exactly when a device may authenticate by proof: its
        credential is current (exists, paired, unrevoked, unexpired)
        AND a signing public key is bound."""
        with self._lock:
            cred = self._creds.get(device_id)
            if cred is None or not cred.paired or cred.revoked:
                return False
            if cred.expires_at and cred.expires_at < utcnow().isoformat():
                return False
            return bool(cred.pubkey)

    def verify_challenge(self, device_id: str, nonce: str,
                         sig_b64: str, message: str) -> bool:
        """Verify one challenge-response proof. Memory-side nonce rules
        (single-use, TTL) are enforced by the caller's ChallengeStore;
        this method validates THAT proof against the registered pubkey
        while holding the store lock, so a concurrent revocation or
        expiry can never slip past verification."""
        with self._lock:
            cred = self._creds.get(device_id)
            if cred is None or not cred.paired or cred.revoked:
                return False
            if cred.expires_at and cred.expires_at < utcnow().isoformat():
                return False
            if not cred.pubkey or not nonce or not sig_b64:
                return False
            return self._verify_pubkey(cred.pubkey, message, sig_b64)

    @staticmethod
    def _verify_pubkey(pubkey_hex: str, message: str, sig_b64: str) -> bool:
        return verify_device_proof(pubkey_hex, message, sig_b64)


class PersistentDeviceAuthStore(DeviceAuthStore):
    """SQLite-backed DeviceAuthStore: per-device credential state
    (key hashes, paired/revoked, expiry) and unclaimed pairing codes
    survive a Core restart.

    Why: the device agent keeps its key (0600 file / Keystore-backed
    secure storage) and reconnects with it at every boot. Without
    persistence, ANY Core restart invalidates every stored credential,
    so each device is forced through manual re-pairing — the exact
    experience this component must not have. With persistence, a
    previously enrolled TRUSTED device reconnects automatically after
    any Core restart; revocation tombstones stay dead (fabric already
    persists those); a lost/reset device still re-pairs through a
    fresh operator enroll.

    Security: only SHA-256 DIGESTS are written (key/pairing material
    itself leaves the process only at mint time, in the HTTP response).
    Restart semantics match the in-memory store exactly: expiry and
    revocation fail closed, zero trust is inferred from a database
    row beyond the recorded facts, and the pairing window is enforced
    through the credential's own stored expiry."""

    def __init__(self, path: str) -> None:
        import os as _os
        import sqlite3 as _sqlite3
        super().__init__()
        self._db = _sqlite3.connect(path, check_same_thread=False)
        # Credential digests at rest get the same file discipline as the
        # device-side key files (KEY_FILE_MODE): 0600, best-effort so odd
        # filesystems (or restricted WAL handles) cannot break startup.
        # Applied AFTER connect: sqlite creates the DB on first open, so
        # pre-connect chmod would silently miss the initial creation.
        for suffix in ("", "-wal", "-shm"):
            try:
                _os.chmod(path + suffix, 0o600)
            except OSError:  # noqa: BLE001 — hardening, never fatal
                pass
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS device_auth ("
                "device_id TEXT PRIMARY KEY, kind TEXT,"
                "key_hash TEXT DEFAULT '', pairing_code_hash TEXT DEFAULT '',"
                "paired INTEGER DEFAULT 0, revoked INTEGER DEFAULT 0,"
                "created_at TEXT DEFAULT '', expires_at TEXT,"
                "pubkey TEXT DEFAULT '')")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS device_auth_pending ("
                "pairing_hash TEXT PRIMARY KEY, device_id TEXT NOT NULL)")
            # Additive migration: pre-pubkey databases keep their state;
            # the new column fills with '' (challenge auth simply stays
            # off until the device binds a signing key).
            cols = {row[1] for row in self._db.execute(
                "PRAGMA table_info(device_auth)").fetchall()}
            if "pubkey" not in cols:
                self._db.execute(
                    "ALTER TABLE device_auth ADD COLUMN pubkey TEXT DEFAULT ''")
        # Rehydrate: digests and public keys only (nothing secret). Skip
        # corrupt rows, never trust them.
        for row in self._db.execute(
                "SELECT device_id, kind, key_hash, pairing_code_hash, "
                "paired, revoked, created_at, expires_at, pubkey "
                "FROM device_auth").fetchall():
            try:
                cred = DeviceCredential(
                    device_id=row[0], kind=DeviceKind(row[1]),
                    key_hash=row[2] or "", pairing_code_hash=row[3] or "",
                    paired=bool(row[4]), revoked=bool(row[5]),
                    created_at=row[6] or "",
                    expires_at=row[7] if row[7] else None,
                    pubkey=row[8] if len(row) > 8 else "")
                self._creds[cred.device_id] = cred
            except Exception:  # noqa: BLE001 — skip corrupt rows
                continue
        for phash, device_id in self._db.execute(
                "SELECT pairing_hash, device_id FROM device_auth_pending"
        ).fetchall():
            # A consumed (already paired) or removed credential makes the
            # pending row stale: drop it instead of serving it.
            if device_id in self._creds and not self._creds[device_id].paired:
                self._pending[phash] = device_id
            else:
                self._drop_pending_db(phash)

    def _save_cred(self, cred: DeviceCredential,
                   code_hash: str = "",
                   drop_pending: str = "") -> None:
        with self._db:
            if code_hash:
                self._db.execute(
                    "INSERT OR REPLACE INTO device_auth_pending VALUES (?,?)",
                    (code_hash, cred.device_id))
            if drop_pending:
                self._db.execute(
                    "DELETE FROM device_auth_pending WHERE pairing_hash = ?",
                    (drop_pending,))
            elif drop_pending is None:
                # revoke(): drop every pending row for this device
                self._db.execute(
                    "DELETE FROM device_auth_pending WHERE device_id = ?",
                    (cred.device_id,))
            self._db.execute(
                "INSERT OR REPLACE INTO device_auth "
                "(device_id, kind, key_hash, pairing_code_hash, paired, "
                "revoked, created_at, expires_at, pubkey) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (cred.device_id, cred.kind.value, cred.key_hash,
                 cred.pairing_code_hash, int(cred.paired), int(cred.revoked),
                 cred.created_at, cred.expires_at or "", cred.pubkey))

    def _drop_pending_db(self, pairing_hash: str) -> None:
        with self._db:
            self._db.execute(
                "DELETE FROM device_auth_pending WHERE pairing_hash = ?",
                (pairing_hash,))
