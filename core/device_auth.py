"""Device authentication — per-device identity, no shared secrets.

Flow: enroll (dev-operator, pairing code) -> claim (device, one-time) ->
per-device key stored 0600 on the device. Core verifies via key hash.
Dev token remains for local development only.
"""
from __future__ import annotations
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
        return code

    def claim(self, pairing_code: str, ttl_days: int = 365) -> tuple[str, str]:
        """Device-side step. Exchanges pairing code for a device key (once)."""
        with self._lock:
            device_id = self._pending.pop(_hash(pairing_code), None)
            if device_id is None:
                raise ValueError("invalid or expired pairing code")
            cred = self._creds[device_id]
            if cred.paired:
                raise ValueError("pairing code already used")
            key = f"zara-dev-{secrets.token_urlsafe(24)}"
            cred.key_hash = _hash(key)
            cred.paired = True
            cred.pairing_code_hash = ""
            cred.expires_at = (utcnow() + timedelta(days=ttl_days)).isoformat()
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
            return new_key

    def is_revoked(self, device_id: str) -> bool:
        with self._lock:
            cred = self._creds.get(device_id)
            return cred is None or cred.revoked
