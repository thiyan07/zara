"""Zara Linux device agent — execution body for the laptop, not an assistant.

Loop: heartbeat + job poll (adaptive interval, backoff on failure) ->
execute claimed job with local allowlisted tools -> post result.
Offline: queue heartbeats/events locally (capped), sync on reconnect.
Shutdown: device_disconnect + graceful stop. No busy polling.
"""
from __future__ import annotations
import hashlib
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from device.linux.tools_linux import (  # noqa: E402
    LINUX_TOOLS, LINUX_CAPABILITIES, system_battery, system_network,
    system_telemetry)
from core.protocol import HeartbeatPayload  # noqa: E402

KEY_FILE_MODE = 0o600
HEARTBEAT_S = 30.0
POLL_IDLE_S = 5.0
POLL_BACKOFF_MAX_S = 60.0
# Bounded server-side long poll window for job claims (see poll_job):
# sub-second claim latency for one held request per window — lower idle
# request rate than the old 5 s tick, battery-friendly by design.
POLL_WAIT_S = float(os.environ.get("ZARA_AGENT_POLL_WAIT_S", "25.0"))
OFFLINE_QUEUE_CAP = 200


class DeviceAgent:
    def __init__(self, core_url: str, device_id: str,
                 key_file: str = "",
                 cancel_checker: Optional[Callable[[str], bool]] = None,
                 ) -> None:
        self.core_url = core_url.rstrip("/")
        self.device_id = device_id
        self.key_file = key_file or os.path.expanduser(
            f"~/.config/zara/{device_id}.key")
        self._key = ""
        self._sk_file = self.key_file + ".sk"  # Ed25519 private key (hex)
        self._sk_hex = ""
        # Challenge-auth availability for this process: ON optimistically,
        # flipped off permanently on an AUTH-shaped rejection (older core,
        # unbound key) — every call then falls back to the raw-key path
        # honestly. Network failures fall back per-call without disabling.
        self._challenge_ok = True
        # Unused chained nonce (server-minted) for the next proof-auth
        # request; the raw key travels once per process lifetime.
        self._spare_nonce = ""
        self._stop = threading.Event()
        self._offline_queue: list[dict] = []
        self._tools = {d.name: h for d, h in LINUX_TOOLS}
        self.software_version = "zara-linux-agent 2.0.0"
        # Pluggable cancellation probe. Default (None) performs an HTTP
        # GET against the recommended endpoint
        #   GET /v1/agent/jobs/{job_id}/state  (device-authed, owner-only)
        # returning {"state": "<state>"}; stubbed in tests so the agent
        # works without network. Any probe error fails closed to
        # "not cancelled" (backoff/reconnect path unchanged).
        self._cancel_checker = cancel_checker
        # last: the allow-marker amends software_version's final value
        self._check_transport_policy()

    # ---------- identity ----------

    # Loopback hosts where plain HTTP is an acceptable (local) transport;
    # 10.0.2.2 is the Android-emulator NAT alias for the host loopback.
    PLAINTEXT_OK_HOSTS = ("localhost", "127.0.0.1", "::1", "10.0.2.2")

    def _check_transport_policy(self) -> None:
        """Refuse credentials over plain HTTP to anything that is not a
        documented local alias: phone-home theft of the device key (or a
        building-local attacker) is exactly what signatures cannot
        prevent — TLS can. Override ONLY with the explicit
        ZARA_ALLOW_INSECURE_TRANSPORT=1 (e.g. a trusted flat LAN), which
        is logged loudly so the choice is visible in diagnostics."""
        parsed = urllib.parse.urlsplit(self.core_url)
        if parsed.scheme != "http":
            return  # https or unknown scheme: TLS stack owns validation
        if parsed.hostname in self.PLAINTEXT_OK_HOSTS:
            return
        if os.environ.get("ZARA_ALLOW_INSECURE_TRANSPORT") == "1":
            # granted deliberately; make the loud marker float into any
            # future log inspection of this agent process
            self.software_version = (self.software_version
                                     + " [INSECURE-TRANSPORT-ALLOWED]")
            return
        raise ValueError(
            f"refusing plain HTTP to non-local core '{self.core_url}': "
            "use https:// (TLS) or set ZARA_ALLOW_INSECURE_TRANSPORT=1 "
            "for a trusted local network; see docs/TRANSPORT_TLS.md")

    def load_key(self) -> str:
        with open(self.key_file) as f:
            self._key = f.read().strip()
        try:
            with open(self._sk_file) as f:
                self._sk_hex = f.read().strip()
        except OSError:
            self._sk_hex = ""  # pre-challenge credential: raw-key only
        return self._key

    def claim(self, pairing_code: str) -> str:
        # Fresh device key => fresh challenge-signing keypair. The public
        # half is sent with the claim (binding is authorized by the pairing
        # code ceremony itself); the private half is written 0600 next to
        # the key file and never leaves the device.
        try:
            from core.device_auth import generate_device_keypair
            pubkey, privkey = generate_device_keypair()
            self._sk_hex = privkey
        except ImportError:  # cryptography unavailable: raw-key credential
            pubkey = ""
        body = json.dumps({"pairing_code": pairing_code,
                           "pubkey": pubkey}).encode()
        req = urllib.request.Request(f"{self.core_url}/v1/agent/claim",
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        os.makedirs(os.path.dirname(self.key_file), exist_ok=True)
        fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     KEY_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            f.write(data["device_key"])
        os.chmod(self.key_file, KEY_FILE_MODE)
        if pubkey:
            fd = os.open(self._sk_file, os.O_WRONLY | os.O_CREAT
                         | os.O_TRUNC, KEY_FILE_MODE)
            with os.fdopen(fd, "w") as f:
                f.write(privkey)
            os.chmod(self._sk_file, KEY_FILE_MODE)
        else:
            try:  # no signing possible: never leave a stale private key
                os.unlink(self._sk_file)
            except OSError:
                pass
        self._key = data["device_key"]
        return data["device_id"]

    def _headers(self) -> dict:
        return {"Content-Type": "application/json",
                "X-Device-Id": self.device_id, "X-Device-Key": self._key}

    # ---- challenge-proof transport (additive; raw-key path fallback) ----

    AUTH_FALLBACK_CODES = (401, 403, 404, 409)

    def _sign_proof(self, nonce: str, method: str, path: str,
                    body: bytes = b"") -> str:
        """Ed25519 signature over the canonical v2 message
        "METHOD path nonce sha256(body)" (path excludes any query
        string — the server binds exactly those fields plus the exact
        request bytes)."""
        import base64 as _b64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey)
        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self._sk_hex))
        canonical = (f"{method} {path} {nonce} "
                     f"{hashlib.sha256(body).hexdigest()}")
        return _b64.b64encode(
            sk.sign(canonical.encode())).decode()

    def _proof_headers(self, nonce: str, method: str, path: str,
                       body: bytes = b"") -> dict:
        return {"Content-Type": "application/json",
                "X-Device-Id": self.device_id,
                "X-Device-Nonce": nonce,
                "X-Device-Proof": self._sign_proof(nonce, method, path,
                                                   body)}

    def _acquire_nonce(self) -> str | None:
        """One usable single-use nonce: spend the chained spare when one
        is held (proof-authed challenge fetch — the raw key never
        travels), else fetch with the raw key (first use, or after a
        restart/eviction). Returns None when challenge auth is impossible
        for this credential (no signing key bound — permanent for this
        process); auth refusals surface as the raw path would."""
        if self._spare_nonce:
            try:
                spare = self._spare_nonce
                self._spare_nonce = ""
                with urllib.request.urlopen(urllib.request.Request(
                        f"{self.core_url}/v1/agent/challenge", data=b"",
                        headers=self._proof_headers(
                            spare, "POST", "/v1/agent/challenge")),
                        timeout=15) as r:
                    data = json.load(r)
                self._spare_nonce = data.get("next_nonce", "")
                return data.get("nonce")
            except urllib.error.HTTPError:
                pass  # stale/evicted spare: raw-key bootstrap below
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    f"{self.core_url}/v1/agent/challenge", data=b"",
                    headers=self._headers()), timeout=15) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 409:  # credential has no bound signing key
                self._challenge_ok = False
                return None
            raise RuntimeError(
                f"challenge: HTTP {e.code} {e.read()[:200]}") from e
        self._spare_nonce = data.get("next_nonce", "")
        return data.get("nonce")

    def _authed_request(self, method: str, path: str,
                        payload: dict | None, timeout: int = 15) -> dict:
        body = json.dumps(payload).encode() if payload is not None else b""
        # Challenge-proof transport first (key stays on disk); an
        # auth-shaped rejection means the server did NOT execute the
        # request (auth deps run before handlers), so falling back to the
        # raw-key path once cannot duplicate a consequential action.
        if self._challenge_ok and self._sk_hex:
            nonce = self._acquire_nonce()
            if nonce is not None:
                req = urllib.request.Request(
                    f"{self.core_url}{path}",
                    data=body if method == "POST" else None,
                    headers=self._proof_headers(
                        nonce, method, path.split("?", 1)[0], body),
                    method=method)
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        return json.load(r)
                except urllib.error.HTTPError as e:
                    if e.code not in self.AUTH_FALLBACK_CODES:
                        raise RuntimeError(
                            f"{path}: HTTP {e.code} {e.read()[:200]}")
                    self._spare_nonce = ""
        req = urllib.request.Request(
            f"{self.core_url}{path}",
            data=body if method == "POST" else None,
            headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{path}: HTTP {e.code} {e.read()[:200]}")

    def _post(self, path: str, payload: dict, timeout: int = 15) -> dict:
        return self._authed_request("POST", path, payload, timeout)

    # ---------- lifecycle ----------

    def register(self) -> dict:
        return self._post("/v1/agent/register", {
            "capabilities": LINUX_CAPABILITIES,
            "software_version": self.software_version,
            "kind": "linux"})

    def describe_capabilities(self) -> dict:
        """Stage 16: advertise versioned capability descriptors built
        from the safe tool allowlist (single source of truth — what is
        advertised is exactly what is implemented, no more)."""
        from core.capabilities import linux_descriptors
        return self._post("/v1/agent/capabilities/describe",
                          {"records": linux_descriptors()}, timeout=30)

    def heartbeat(self) -> dict:
        try:
            bat = system_battery({}, {})
            net = system_network({}, {})
            telem = system_telemetry({}, {})
            hb = HeartbeatPayload(
                battery_pct=bat.get("battery_pct"),
                charging=bat.get("charging", False),
                network=net.get("network", "unknown"),
                cpu_pct=telem.get("cpu_pct"),
                mem_pct=telem.get("mem_pct"),
                low_power_mode=telem.get("low_power_mode"),
                storage_free_mb=telem.get("storage_free_mb"),
                storage_total_mb=telem.get("storage_total_mb"),
                battery_temp_c=telem.get("battery_temp_c"),
                thermal_state=telem.get("thermal_state")).model_dump()
        except Exception:  # noqa: BLE001 — probes must never kill the agent
            hb = HeartbeatPayload().model_dump()
        try:
            out = self._post("/v1/agent/heartbeat", hb)
            self._sync_queued()
            return out
        except (OSError, RuntimeError) as e:
            self._queue_offline({"type": "heartbeat", "payload": hb,
                                 "error": str(e)[:200]})
            raise

    def poll_job(self, wait_secs: float = POLL_WAIT_S) -> dict | None:
        """Claim one queued job. wait_secs > 0 opts into the bounded
        server-side long poll (core's agent_poll contract): the server
        holds THIS request until a job arrives or the window expires, so
        idle request rate stays ~1 per window while claim latency drops
        from up-to-POLL_IDLE_S to sub-second. Cores older than the
        wait_secs parameter (unknown query param ignored -> instant empty
        response) and zero-arg poll stubs in tests keep working via the
        module default."""
        try:
            if wait_secs > 0:
                out = self._post("/v1/agent/jobs/poll?wait_secs="
                                 f"{wait_secs:.1f}", {})
            else:
                out = self._post("/v1/agent/jobs/poll", {})
        except (OSError, RuntimeError):
            return None
        return out.get("job")

    def is_job_cancelled(self, job_id: str) -> bool:
        """Pluggable pre/post-execution cancellation probe.

        Stub path (tests / offline): delegate to the injected callable.
        Default path: GET /v1/agent/jobs/{job_id}/state (device-authed,
        owner-only, served by JobQueue.job_state_for). Any transport or
        parse error returns False — cancellation must be positively
        observed, never inferred from a failed probe.
        """
        if self._cancel_checker is not None:
            try:
                return bool(self._cancel_checker(job_id))
            except Exception:  # noqa: BLE001 — probe failure ≠ cancelled
                return False
        try:
            data = self._authed_request(
                "GET", f"/v1/agent/jobs/{job_id}/state", None)
            return data.get("state") == "cancelled"
        except Exception:  # noqa: BLE001 — OSError/HTTPError/parse errors
            return False

    def run_one_job(self) -> bool:
        job = self.poll_job()
        if not job:
            return False
        job_id, tool, inputs = job["job_id"], job["tool"], job.get("inputs", {})
        # Semantics: the server is the source of truth for cancellation.
        # complete() on a cancelled job is a no-op returning the recorded
        # cancelled outcome, so we STILL report ok=False
        # error="cancelled by operator" instead of going silent — the
        # report is idempotent and keeps server terminal state consistent
        # (cancelled stays cancelled; never resurrected to done/failed).
        if self.is_job_cancelled(job_id):
            try:
                self._post("/v1/agent/jobs/result",
                           {"job_id": job_id, "ok": False,
                            "error": "cancelled by operator"})
            except (OSError, RuntimeError):
                pass
            return True
        handler = self._tools.get(tool)
        if handler is None:
            self._post("/v1/agent/jobs/result",
                       {"job_id": job_id, "ok": False,
                        "error": f"tool not implemented on device: {tool}"})
            return True
        try:
            result = handler(dict(inputs), {"device_id": self.device_id,
                                            "agent": self})
            if not isinstance(result, dict):
                raise ValueError("tool must return a dict")
            # Post-execution re-check: a long-running tool may have been
            # cancelled mid-run. Downgrade the would-be success to a
            # cancelled report so the device never resurrects a
            # server-cancelled job to done. complete() is no-op-safe, so
            # this report preserves the cancelled terminal state.
            if self.is_job_cancelled(job_id):
                self._post("/v1/agent/jobs/result",
                           {"job_id": job_id, "ok": False,
                            "error": "cancelled by operator"})
            else:
                self._post("/v1/agent/jobs/result",
                           {"job_id": job_id, "ok": True, "result": result})
        except Exception as e:  # noqa: BLE001 — report, don't crash
            try:
                if self.is_job_cancelled(job_id):
                    self._post("/v1/agent/jobs/result",
                               {"job_id": job_id, "ok": False,
                                "error": "cancelled by operator"})
                else:
                    self._post("/v1/agent/jobs/result",
                               {"job_id": job_id, "ok": False,
                                "error": f"{type(e).__name__}: {e}"[:500]})
            except (OSError, RuntimeError):
                pass
        return True

    def _queue_offline(self, item: dict) -> None:
        self._offline_queue.append(item)
        self._offline_queue = self._offline_queue[-OFFLINE_QUEUE_CAP:]

    def _sync_queued(self) -> None:
        if not self._offline_queue:
            return
        pending = self._offline_queue
        self._offline_queue = []
        try:
            self._post("/v1/agent/events/sync", {"events": pending})
        except (OSError, RuntimeError):
            self._offline_queue = (pending + self._offline_queue)[-OFFLINE_QUEUE_CAP:]

    def disconnect(self) -> None:
        try:
            self._post("/v1/agent/disconnect", {})
        except (OSError, RuntimeError):
            pass

    # ---------- Stage 15: byte transfers (Core-mediated, chunked) ----------

    def xfer_request(self, recipient: str, filename: str, data: bytes,
                     content_type: str = "application/octet-stream",
                     grant_id: str = "") -> dict:
        import hashlib as _hl
        return self._post("/v1/agent/xfer/request", {
            "recipient_device": recipient, "filename": filename,
            "size_bytes": len(data),
            "sha256": _hl.sha256(data).hexdigest(),
            "content_type": content_type, "grant_id": grant_id})

    def xfer_upload(self, transfer_id: str, data: bytes,
                    chunk: int = 65536) -> dict:
        import base64 as _b64
        out: dict = {}
        seq = 0
        for i in range(0, len(data), chunk):
            out = self._post(f"/v1/agent/xfer/{transfer_id}/chunk", {
                "seq": seq,
                "data_base64": _b64.b64encode(
                    data[i:i + chunk]).decode()})
            seq += 1
        return out

    def xfer_pending(self) -> dict:
        import urllib.request as _urllib
        req = _urllib.Request(f"{self.core_url}/v1/agent/xfer/pending",
                              headers=self._headers())
        try:
            with _urllib.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"xfer/pending: HTTP {e.code} {e.read()[:200]}")

    def xfer_download(self, transfer_id: str,
                      size_bytes: int = -1) -> tuple[bytes, dict]:
        """Download all chunks, verify SHA-256 locally, ack. Returns
        (bytes, ack_response). Raises on hash mismatch (no false ack)."""
        import base64 as _b64
        import hashlib as _hl
        import urllib.request as _urllib
        import urllib.parse as _up
        out = bytearray()
        expected = size_bytes
        if expected < 0:
            # Core refuses reads past the declared size ("read overruns
            # transfer size"), so the first block must not assume 64 KiB.
            # Learn the declared size from the recipient's pending list
            # (the GET-style single-record route above 404s on this
            # device surface; the pending list is the truthful record).
            # Small transfers failed here before — observed live
            # 2026-10-25.
            try:
                for r in self.xfer_pending().get("to_download", []):
                    if r.get("transfer_id") == transfer_id:
                        expected = int(r.get("size_bytes", -1))
                        break
            except (RuntimeError, ValueError):
                expected = -1
        offset = 0
        while True:
            length = 65536 if expected < 0 else min(65536, expected - offset)
            if length <= 0:
                break
            qs = _up.urlencode({"offset": offset, "length": length})
            req = _urllib.Request(
                f"{self.core_url}/v1/agent/xfer/{transfer_id}/bytes?{qs}",
                headers=self._headers())
            try:
                with _urllib.urlopen(req, timeout=30) as r:
                    blk = json.load(r)
            except urllib.error.HTTPError as e:
                raise RuntimeError(
                    f"xfer/bytes: HTTP {e.code} {e.read()[:200]}")
            raw = _b64.b64decode(blk["data_base64"])
            out += raw
            expected = blk["size_bytes"]
            offset += blk["length"]
            if offset >= expected or not raw:
                break
        data = bytes(out)
        digest = _hl.sha256(data).hexdigest()
        if digest != blk["sha256"]:
            raise ValueError("recipient hash mismatch: refusing ack")
        ack = self._post(f"/v1/agent/xfer/{transfer_id}/ack",
                         {"sha256": digest})
        return data, ack

    # ---------- main loop ----------

    def serve_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        self.load_key()
        self.register()
        try:
            # Stage 16: capability snapshot follows registration (refresh
            # semantics; no re-pair needed when capabilities change).
            self.describe_capabilities()
        except (OSError, RuntimeError):
            pass  # describe retries on next boot; strings still route
        last_hb = 0.0
        backoff = POLL_IDLE_S
        auth_failures = 0
        while not self._stop.is_set():
            try:
                now = time.time()
                if now - last_hb >= HEARTBEAT_S:
                    self.heartbeat()
                    last_hb = now
                busy = self.run_one_job()
                backoff = POLL_IDLE_S if busy else POLL_IDLE_S
                auth_failures = 0
                self._stop.wait(1.0 if busy else POLL_IDLE_S)
            except (OSError, RuntimeError) as e:
                # Revoked keys surface as 401/403 on heartbeat/register
                # (poll swallows to None). Three consecutive auth failures
                # mean the pairing is dead — stop with an operator-
                # actionable message instead of backing off forever and
                # hammering Core. Transient/network errors keep retrying.
                if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                    auth_failures += 1
                    if auth_failures >= 3:
                        print(f"agent {self.device_id}: REVOKED or key "
                              f"invalid (3 consecutive auth failures) — "
                              f"ask the operator for a fresh pairing code "
                              f"and re-run with --claim <code>",
                              flush=True)
                        self._stop.set()
                        return
                else:
                    auth_failures = 0
                self._stop.wait(backoff)
                backoff = min(backoff * 2, POLL_BACKOFF_MAX_S)
        self.disconnect()


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Zara Linux device agent")
    ap.add_argument("--core", default="http://127.0.0.1:8080")
    ap.add_argument("--device-id", default="laptop-1")
    ap.add_argument("--claim", default="", help="pairing code to claim once")
    ap.add_argument("--key-file", default="")
    args = ap.parse_args()
    agent = DeviceAgent(args.core, args.device_id, args.key_file)
    if args.claim:
        print("claimed:", agent.claim(args.claim))
        return
    agent.serve_forever()


if __name__ == "__main__":
    main()
