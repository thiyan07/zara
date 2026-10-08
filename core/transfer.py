"""Device Fabric byte transfer — Core-mediated chunked relay (Stage 15).

Architecture (unchanged): LLM proposes; Core (policy + governor) +
Fabric authorize; devices move bytes through Core. Core NEVER executes
payload content; bytes are opaque, integrity-checked, sandboxed.

Protocol:
  request (full auth chain) -> AUTHORIZED -> chunks (strict order,
  bounded, sender+recipient trust re-checked every chunk) -> VERIFYING
  (SHA-256 over reassembled bytes) -> SUCCEEDED | FAILED.
  Cancel/expire any time before terminal; terminal states immutable.

Storage: <root>/<transfer_id>.part while moving, <transfer_id>.bin when
complete. NEVER client-supplied paths or filenames on disk. Filenames
are metadata only (shown to recipient), strictly sanitized.

Memory: bounded — chunks <= MAX_CHUNK_BYTES are appended to disk;
nothing attacker-sized is ever held in RAM. Hashing streams from disk.
"""
from __future__ import annotations
import hashlib
import os
import re
import threading
import time
import uuid

from .fabric import (EXECUTABLE_TRUST, TRANSFER_TRANSITIONS, FabricRegistry,
                     FabricStore, Presence, TransferRecord, TrustState)
from .models import utcnow

# ---------- limits (low-resource: laptop + phone) ----------

MAX_TRANSFER_BYTES = 5 * 1024 * 1024      # 5 MiB single transfer
MAX_CHUNK_BYTES = 64 * 1024               # 64 KiB per chunk
MAX_FILENAME_LEN = 128
MAX_METADATA_BYTES = 2048
MAX_CONCURRENT_PER_DEVICE = 4
CHUNK_TIMEOUT_S = 120.0                   # stall -> expired
TRANSFER_TTL_S = 600.0                    # total lifetime -> expired
PRUNE_TERMINAL_S = 3600.0                 # drop old non-proof records
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled",
                             "rejected", "expired"})
TRANSFER_CAPABILITY = "files.transfer"
TRANSFER_RISK = "confirm"  # byte movement always needs a live temp grant

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^xfer-[0-9a-f]{12}$")
_CTYPE_RE = re.compile(r"^[a-z0-9][a-z0-9.+\-]*/[a-z0-9][a-z0-9.+\-]*$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")


class TransferRejected(ValueError):
    """Authorization or contract refusal. Never a crash; always audited.
    `status` hints the HTTP mapping: 400 malformed contract, 403 refused,
    409 state conflict (terminal/replay/out-of-order)."""

    def __init__(self, reason: str, status: int = 403) -> None:
        super().__init__(reason)
        self.status = status


def sanitize_filename(name) -> str:
    """Flat-namespace filename validation. Rejects (never silently rewrites):
    empty, over-long, absolute/parent paths, separators, traversal (..),
    leading dots (hidden files), null bytes, control chars, non-ASCII,
    shell expansions ($, `, ~). Returns the name unchanged when safe."""
    if not isinstance(name, str):
        raise TransferRejected("filename must be text", 400)
    if not name or len(name) > MAX_FILENAME_LEN:
        raise TransferRejected("filename empty or too long", 400)
    if name != name.strip():
        raise TransferRejected("filename has edge whitespace", 400)
    for bad in ("\x00", "/", "\\", "$", "`", "~", "\n", "\r"):
        if bad in name:
            raise TransferRejected(f"filename contains {bad!r}", 400)
    if name in (".", "..") or name.startswith("."):
        raise TransferRejected("hidden/relative filename refused", 400)
    if not _NAME_RE.match(name):
        raise TransferRejected("filename has unsafe characters", 400)
    if ".." in name:
        raise TransferRejected("traversal refused", 400)
    return name


def _check_sha256(value) -> str:
    if not isinstance(value, str):
        raise TransferRejected("sha256 must be text", 400)
    v = value.strip().lower()
    if not _SHA_RE.match(v):
        raise TransferRejected("sha256 must be 64 hex chars", 400)
    return v


def _check_content_type(value) -> str:
    if value is None or value == "":
        return "application/octet-stream"
    if not isinstance(value, str) or len(value) > 128:
        raise TransferRejected("bad content type", 400)
    v = value.strip().lower()
    if not _CTYPE_RE.match(v):
        raise TransferRejected("bad content type", 400)
    return v


def default_transfer_dir() -> str:
    root = os.environ.get("ZARA_TRANSFER_DIR", "")
    if root:
        return root
    return os.path.join(os.path.expanduser("~"), ".local", "share",
                        "zara", "transfers")


# ---------- disk staging (no shell, no symlinks, no client paths) ----------

class TransferStore:
    """Sandboxed staging area. Disk names derive ONLY from validated
    transfer IDs; client filenames never touch the filesystem."""

    def __init__(self, root: str = "") -> None:
        self.root = os.path.realpath(root or default_transfer_dir())
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:  # noqa: BLE001 — best effort on odd filesystems
            pass

    def _path(self, transfer_id: str, suffix: str) -> str:
        if not _ID_RE.match(transfer_id or ""):
            raise TransferRejected("malformed transfer id")
        cand = os.path.realpath(os.path.join(self.root,
                                             transfer_id + suffix))
        if not cand.startswith(self.root + os.sep):
            raise TransferRejected("storage escape refused")
        if os.path.islink(os.path.join(self.root, transfer_id + suffix)):
            raise TransferRejected("symlink refused")
        return cand

    def staging_path(self, transfer_id: str) -> str:
        return self._path(transfer_id, ".part")

    def final_path(self, transfer_id: str) -> str:
        return self._path(transfer_id, ".bin")

    def append(self, transfer_id: str, data: bytes) -> int:
        path = self.staging_path(transfer_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                     os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as f:
                f.write(data)
        except BaseException:
            # fdopen failure must not leak the descriptor
            try:
                os.close(fd)
            except OSError:  # noqa: BLE001
                pass
            raise
        return os.path.getsize(path)

    def staged_size(self, transfer_id: str) -> int:
        try:
            return os.path.getsize(self.staging_path(transfer_id))
        except OSError:  # noqa: BLE001 — no staging file yet
            return 0

    def finalize(self, transfer_id: str) -> None:
        os.rename(self.staging_path(transfer_id),
                  self.final_path(transfer_id))

    def digest_final(self, transfer_id: str) -> str:
        h = hashlib.sha256()
        with open(self.final_path(transfer_id), "rb") as f:
            for blk in iter(lambda: f.read(65536), b""):
                h.update(blk)
        return h.hexdigest()

    def digest_staging(self, transfer_id: str) -> str:
        h = hashlib.sha256()
        with open(self.staging_path(transfer_id), "rb") as f:
            for blk in iter(lambda: f.read(65536), b""):
                h.update(blk)
        return h.hexdigest()

    def read(self, transfer_id: str, offset: int, length: int) -> bytes:
        if offset < 0 or length <= 0 or length > MAX_CHUNK_BYTES:
            raise TransferRejected("bad read window")
        with open(self.final_path(transfer_id), "rb") as f:
            f.seek(offset)
            return f.read(length)

    def discard(self, transfer_id: str) -> None:
        for suffix in (".part", ".bin"):
            try:
                os.unlink(os.path.join(self.root, transfer_id + suffix))
            except OSError:  # noqa: BLE001 — already gone is fine
                pass


# ---------- engine ----------

class TransferEngine:
    """Byte movement over a FabricRegistry. Owns no trust/policy truth:
    every operation re-derives trust + presence from the registry and
    re-runs policy/governor where the contract requires."""

    def __init__(self, fabric: FabricRegistry,
                 store: TransferStore | None = None) -> None:
        self.fabric = fabric
        self.store = store or TransferStore()
        self._lock = threading.Lock()
        self._records: dict[str, TransferRecord] = {}
        self._rehydrate()

    # ----- persistence / restart -----

    def _persist(self, rec: TransferRecord) -> None:
        try:
            self.fabric.store.save_transfer(rec.model_dump_json(),
                                            rec.transfer_id)
        except Exception:  # noqa: BLE001 — persistence never breaks motion
            pass

    def _rehydrate(self) -> None:
        for raw in self.fabric.store.load_transfers():
            try:
                rec = TransferRecord(**raw)
            except Exception:  # noqa: BLE001 — skip corrupt rows
                continue
            if rec.state in TERMINAL_STATES:
                if rec.state == "succeeded":
                    # Proof must still exist on disk; missing bytes =>
                    # honest failure, never phantom success.
                    try:
                        size = os.path.getsize(
                            self.store.final_path(rec.transfer_id))
                        if size != rec.size_bytes or \
                                self.store.digest_final(
                                    rec.transfer_id) != rec.sha256:
                            rec.state = "failed"
                            rec.error = "stored bytes lost or corrupted"
                    except OSError:  # noqa: BLE001
                        rec.state = "failed"
                        rec.error = "stored bytes missing after restart"
                self._records[rec.transfer_id] = rec
                continue
            # In-flight transfers never survive restart: deterministic
            # expiry, partial bytes discarded, audited.
            rec.state = "expired"
            rec.error = "core restarted mid-transfer"
            rec.updated_at = utcnow().isoformat()
            self.store.discard(rec.transfer_id)
            self._records[rec.transfer_id] = rec
            self._persist(rec)
            self._audit("transfer_expired", rec.source_device,
                        f"{rec.transfer_id} restart-expiry")

    def get(self, transfer_id: str) -> TransferRecord:
        rec = self._records.get(transfer_id)
        if rec is None:
            raise KeyError(f"unknown transfer: {transfer_id}")
        return rec

    def list(self, device_id: str = "") -> list[TransferRecord]:
        out = [r for r in self._records.values()
               if not device_id or r.source_device == device_id
               or r.dest_device == device_id]
        return sorted(out, key=lambda r: r.transfer_id)

    # ----- authorization chain -----

    def _live_grant(self, sender: str, grant_id: str) -> bool:
        if not grant_id:
            return False
        for g in self.fabric.list_grants(sender):
            if g.grant_id == grant_id and \
                    g.covers(TRANSFER_CAPABILITY, TRANSFER_RISK):
                return True
        return False

    def _active_count(self, device_id: str) -> int:
        return sum(1 for r in self._records.values()
                   if r.state not in TERMINAL_STATES and
                   (r.source_device == device_id or
                    r.dest_device == device_id))

    def request(self, sender: str, recipient: str, filename: str,
                size_bytes: int, sha256: str, content_type: str = "",
                grant_id: str = "", who: str = "user",
                metadata: dict | None = None) -> TransferRecord:
        """Full chain: contract -> identity/trust -> presence -> policy ->
        grant -> governor -> concurrency -> AUTHORIZED. Any failure is
        audited (transfer_rejected) and raises TransferRejected."""
        now = utcnow().isoformat()
        clean_name = sanitize_filename(filename)
        digest = _check_sha256(sha256)
        ctype = _check_content_type(content_type)
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) \
                or not 1 <= size_bytes <= MAX_TRANSFER_BYTES:
            raise TransferRejected(
                f"size must be 1..{MAX_TRANSFER_BYTES}", 400)
        meta = dict(metadata or {})
        if len(str(meta)) > MAX_METADATA_BYTES:
            raise TransferRejected("metadata too large", 400)
        if sender == recipient:
            raise TransferRejected("sender and recipient must differ", 400)

        def _refuse(reason: str) -> TransferRejected:
            # NOTE: caller holds self._lock; no locking here (deadlock).
            rec = TransferRecord(
                transfer_id=f"xfer-{uuid.uuid4().hex[:12]}",
                source_device=sender, dest_device=recipient,
                capability=TRANSFER_CAPABILITY, state="rejected",
                filename=clean_name, size_bytes=size_bytes,
                sha256=digest, content_type=ctype, grant_id=grant_id[:64],
                created_at=now, updated_at=now,
                metadata={k[:64]: str(v)[:300]
                          for k, v in list(meta.items())[:20]},
                error=reason[:300])
            self._records[rec.transfer_id] = rec
            self._persist(rec)
            self._audit("transfer_rejected", sender,
                        f"{rec.transfer_id} {sender}->{recipient}: {reason}")
            return TransferRejected(reason)

        with self._lock:
            self._sweep_locked()
            for dev, role in ((sender, "sender"), (recipient, "recipient")):
                trust = self.fabric.trust_of(dev)
                if trust not in EXECUTABLE_TRUST:
                    raise _refuse(
                        f"{role} {dev} trust is {trust}, not executable")
                presence = self.fabric.presence_of(dev)
                if presence not in (Presence.ONLINE, Presence.DEGRADED):
                    raise _refuse(
                        f"{role} {dev} is {presence}; transfers need "
                        f"a live device")
            # Policy still sees every transfer (hard-deny always wins).
            pol = self.fabric.policy.decide(
                who, capability=TRANSFER_CAPABILITY, device_id=sender,
                resource=recipient, risk=TRANSFER_RISK)
            if pol.hard_deny or \
                    (not pol.allow and not pol.requires_approval):
                raise _refuse(f"policy refusal: {pol.reason}")
            # confirm risk: a live sender-scoped temp grant is mandatory.
            if not self._live_grant(sender, grant_id):
                raise _refuse(
                    "confirm-risk transfer needs a live temp grant for "
                    "files.transfer on the sender")
            gate = self.fabric.governor.check(
                self.fabric._snapshot_for(sender),
                min(0.1 + size_bytes / MAX_TRANSFER_BYTES * 0.4, 0.5),
                TRANSFER_RISK)
            if gate.action != "proceed":
                raise _refuse(f"governor {gate.action}: {gate.reason}")
            if self._active_count(sender) >= MAX_CONCURRENT_PER_DEVICE or \
                    self._active_count(recipient) >= MAX_CONCURRENT_PER_DEVICE:
                raise _refuse("too many concurrent transfers")
            rec = TransferRecord(
                transfer_id=f"xfer-{uuid.uuid4().hex[:12]}",
                source_device=sender, dest_device=recipient,
                capability=TRANSFER_CAPABILITY, state="authorized",
                filename=clean_name, size_bytes=size_bytes,
                sha256=digest, content_type=ctype, grant_id=grant_id[:64],
                created_at=now, updated_at=now,
                expires_at=_iso_plus(TRANSFER_TTL_S),
                metadata={k[:64]: str(v)[:300]
                          for k, v in list(meta.items())[:20]})
            self._records[rec.transfer_id] = rec
            self._persist(rec)
        self._audit("transfer_authorized", sender,
                    f"{rec.transfer_id} {sender}->{recipient} "
                    f"{clean_name} {size_bytes}B {digest[:16]}...")
        return rec

    # ----- chunk motion -----

    def post_chunk(self, transfer_id: str, sender: str, seq: int,
                   data: bytes) -> TransferRecord:
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise TransferRejected("empty chunk", 400)
        if len(data) > MAX_CHUNK_BYTES:
            raise TransferRejected(
                f"chunk exceeds {MAX_CHUNK_BYTES}", 400)
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise TransferRejected("bad sequence", 400)
        with self._lock:
            self._sweep_locked()
            rec = self.get(transfer_id)
            if rec.state in TERMINAL_STATES:
                raise TransferRejected(
                    f"transfer is {rec.state}; chunks no longer accepted",
                    409)
            if rec.state not in ("authorized", "running"):
                raise TransferRejected(
                    f"transfer is {rec.state}, not moving", 409)
            if sender != rec.source_device:
                self._audit("transfer_chunk_rejected", sender,
                            f"{transfer_id} wrong sender")
                raise TransferRejected("chunk sender is not the transfer "
                                       "sender")
            # Trust is re-checked on EVERY chunk: revocation mid-transfer
            # cancels motion immediately (never silent continuation).
            for dev in (rec.source_device, rec.dest_device):
                if self.fabric.trust_of(dev) not in EXECUTABLE_TRUST:
                    self._cancel_locked(
                        rec, f"endpoint {dev} lost trust mid-transfer")
                    raise TransferRejected(
                        f"endpoint {dev} revoked; transfer cancelled")
            if seq != rec.seq_expected:
                self._audit("transfer_chunk_rejected", sender,
                            f"{transfer_id} seq {seq} != "
                            f"expected {rec.seq_expected}")
                raise TransferRejected(
                    f"sequence mismatch: expected {rec.seq_expected}", 409)
            if rec.received_bytes + len(data) > rec.size_bytes:
                raise TransferRejected("chunk overruns declared size", 400)
            self.store.append(transfer_id, bytes(data))
            rec.received_bytes += len(data)
            rec.seq_expected += 1
            rec.state = "running"
            rec.updated_at = utcnow().isoformat()
            rec.expires_at = _iso_plus(TRANSFER_TTL_S)
            if rec.received_bytes == rec.size_bytes:
                rec.state = "verifying"
                got = self.store.digest_staging(transfer_id)
                if got == rec.sha256:
                    self.store.finalize(transfer_id)
                    rec.state = "succeeded"
                    rec.completed_at = utcnow().isoformat()
                    rec.verification = f"sha256:{got[:16]}... ok"
                    self._audit("transfer_completed", sender,
                                f"{transfer_id} {rec.size_bytes}B "
                                f"sha256 ok")
                else:
                    self.store.discard(transfer_id)
                    rec.state = "failed"
                    rec.error = "integrity mismatch: hash differs"
                    self._audit("transfer_failed", sender,
                                f"{transfer_id} INTEGRITY MISMATCH")
            self._persist(rec)
            return rec

    # ----- cancel / expire -----

    def cancel(self, transfer_id: str, by: str = "operator") -> TransferRecord:
        with self._lock:
            rec = self.get(transfer_id)
            if rec.state in TERMINAL_STATES:
                raise TransferRejected(
                    f"transfer is {rec.state}; cannot cancel", 409)
            self._cancel_locked(rec, f"cancelled by {by}")
            return rec

    def revoke_device(self, device_id: str) -> list[str]:
        """Eager revocation: cancel every non-terminal transfer touching
        the device (revoked endpoints never move again). Called by the
        revoke path; chunk-time trust checks remain as defense in depth."""
        done = []
        with self._lock:
            for rec in self._records.values():
                if rec.state in TERMINAL_STATES:
                    continue
                if device_id in (rec.source_device, rec.dest_device):
                    self._cancel_locked(
                        rec, f"endpoint {device_id} revoked")
                    done.append(rec.transfer_id)
        return done

    def _cancel_locked(self, rec: TransferRecord, reason: str) -> None:
        rec.state = "cancelled"
        rec.error = reason[:300]
        rec.updated_at = utcnow().isoformat()
        self.store.discard(rec.transfer_id)
        self._persist(rec)
        self._audit("transfer_cancelled", rec.source_device,
                    f"{rec.transfer_id} {reason}")

    def sweep(self) -> list[str]:
        with self._lock:
            return self._sweep_locked()

    def _sweep_locked(self) -> list[str]:
        now = utcnow().isoformat()
        changed = []
        for rec in self._records.values():
            if rec.state in TERMINAL_STATES:
                continue
            if rec.expires_at and rec.expires_at < now:
                reason = "timeout: no progress before deadline"
            elif rec.updated_at and _age_s(rec.updated_at) > \
                    CHUNK_TIMEOUT_S and \
                    rec.state in ("authorized", "running"):
                reason = (f"timeout: stalled "
                          f"({_age_s(rec.updated_at):.0f}s without progress)")
            else:
                continue
            rec.state = "expired"
            rec.error = reason
            rec.updated_at = now
            self.store.discard(rec.transfer_id)
            self._persist(rec)
            self._audit("transfer_expired", rec.source_device,
                        f"{rec.transfer_id} {reason}")
            changed.append(rec.transfer_id)
        # Prune old non-proof terminal records (succeeded kept as proof).
        for tid in [t for t, r in self._records.items()
                    if r.state in TERMINAL_STATES - {"succeeded"} and
                    r.updated_at and
                    _age_s(r.updated_at) > PRUNE_TERMINAL_S]:
            self._records.pop(tid, None)
            try:
                self.fabric.store.delete_transfer(tid)
            except Exception:  # noqa: BLE001
                pass
        return changed

    # ----- recipient side -----

    def pending_for(self, device_id: str) -> dict:
        with self._lock:
            self._sweep_locked()
            return {
                "to_download": [
                    self._summary(r) for r in self._records.values()
                    if r.dest_device == device_id and
                    r.state == "succeeded"],
                "to_continue": [
                    self._summary(r) for r in self._records.values()
                    if r.source_device == device_id and
                    r.state in ("authorized", "running")],
            }

    @staticmethod
    def _summary(rec: TransferRecord) -> dict:
        return {"transfer_id": rec.transfer_id,
                "source_device": rec.source_device,
                "dest_device": rec.dest_device,
                "filename": rec.filename,
                "size_bytes": rec.size_bytes, "sha256": rec.sha256,
                "content_type": rec.content_type, "state": rec.state,
                "received_bytes": rec.received_bytes}

    def read(self, transfer_id: str, recipient: str,
             offset: int, length: int) -> tuple[bytes, TransferRecord]:
        with self._lock:
            rec = self.get(transfer_id)
            if rec.state != "succeeded":
                raise TransferRejected(
                    f"transfer is {rec.state}; bytes not available", 409)
            if recipient != rec.dest_device:
                self._audit("transfer_chunk_rejected", recipient,
                            f"{transfer_id} wrong recipient")
                raise TransferRejected("only the bound recipient may "
                                       "download")
            if self.fabric.trust_of(recipient) not in EXECUTABLE_TRUST:
                raise TransferRejected(
                    f"recipient trust is "
                    f"{self.fabric.trust_of(recipient)}")
            if not isinstance(offset, int) or isinstance(offset, bool) \
                    or offset < 0 or offset >= rec.size_bytes:
                raise TransferRejected("bad offset", 400)
            if not isinstance(length, int) or isinstance(length, bool) \
                    or length <= 0 or length > MAX_CHUNK_BYTES:
                raise TransferRejected("bad length", 400)
            if offset + length > rec.size_bytes:
                raise TransferRejected("read overruns transfer size", 400)
            return self.store.read(transfer_id, offset, length), rec

    def ack(self, transfer_id: str, recipient: str,
            sha256: str) -> tuple[bool, TransferRecord]:
        with self._lock:
            rec = self.get(transfer_id)
            if rec.state != "succeeded":
                raise TransferRejected(
                    f"transfer is {rec.state}; nothing to ack")
            if recipient != rec.dest_device:
                raise TransferRejected("only the bound recipient may ack")
            if self.fabric.trust_of(recipient) not in EXECUTABLE_TRUST:
                raise TransferRejected("recipient trust lost")
            ok = _check_sha256(sha256) == rec.sha256
            rec.recipient_verified = ok
            rec.updated_at = utcnow().isoformat()
            self._persist(rec)
            self._audit("transfer_verified" if ok else
                        "transfer_recipient_mismatch", recipient,
                        f"{transfer_id} recipient sha "
                        f"{'matches' if ok else 'DIFFERS'}")
            return ok, rec

    # ----- audit helper (bytes never logged; sizes/hashes only) -----

    def _audit(self, action: str, target: str, detail: str = "") -> None:
        try:
            self.fabric._audit(action, target, detail[:500])
        except Exception:  # noqa: BLE001 — audit never breaks motion
            pass


def _iso_plus(seconds: float) -> str:
    from datetime import timedelta
    return (utcnow() + timedelta(seconds=seconds)).isoformat()


def _age_s(iso: str) -> float:
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)  # legacy naive => UTC
        return (utcnow() - ts).total_seconds()
    except Exception:  # noqa: BLE001 — unparseable => not old
        return 0.0


def engine_from_env(fabric: FabricRegistry) -> TransferEngine:
    """Transfer engine over the fabric registry. Storage root from
    ZARA_TRANSFER_DIR (default ~/.local/share/zara/transfers)."""
    return TransferEngine(fabric, TransferStore())
