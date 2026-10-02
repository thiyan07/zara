"""Zara Linux device agent — execution body for the laptop, not an assistant.

Loop: heartbeat + job poll (adaptive interval, backoff on failure) ->
execute claimed job with local allowlisted tools -> post result.
Offline: queue heartbeats/events locally (capped), sync on reconnect.
Shutdown: device_disconnect + graceful stop. No busy polling.
"""
from __future__ import annotations
import json
import os
import signal
import sys
import threading
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from device.linux.tools_linux import (  # noqa: E402
    LINUX_TOOLS, LINUX_CAPABILITIES, system_battery, system_network)
from core.protocol import HeartbeatPayload  # noqa: E402

KEY_FILE_MODE = 0o600
HEARTBEAT_S = 30.0
POLL_IDLE_S = 5.0
POLL_BACKOFF_MAX_S = 60.0
OFFLINE_QUEUE_CAP = 200


class DeviceAgent:
    def __init__(self, core_url: str, device_id: str,
                 key_file: str = "") -> None:
        self.core_url = core_url.rstrip("/")
        self.device_id = device_id
        self.key_file = key_file or os.path.expanduser(
            f"~/.config/zara/{device_id}.key")
        self._key = ""
        self._stop = threading.Event()
        self._offline_queue: list[dict] = []
        self._tools = {d.name: h for d, h in LINUX_TOOLS}
        self.software_version = "zara-linux-agent 2.0.0"

    # ---------- identity ----------

    def load_key(self) -> str:
        with open(self.key_file) as f:
            self._key = f.read().strip()
        return self._key

    def claim(self, pairing_code: str) -> str:
        body = json.dumps({"pairing_code": pairing_code}).encode()
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
        self._key = data["device_key"]
        return data["device_id"]

    def _headers(self) -> dict:
        return {"Content-Type": "application/json",
                "X-Device-Id": self.device_id, "X-Device-Key": self._key}

    def _post(self, path: str, payload: dict, timeout: int = 15) -> dict:
        req = urllib.request.Request(f"{self.core_url}{path}",
                                     data=json.dumps(payload).encode(),
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{path}: HTTP {e.code} {e.read()[:200]}")

    # ---------- lifecycle ----------

    def register(self) -> dict:
        return self._post("/v1/agent/register", {
            "capabilities": LINUX_CAPABILITIES,
            "software_version": self.software_version,
            "kind": "linux"})

    def heartbeat(self) -> dict:
        try:
            bat = system_battery({}, {})
            net = system_network({}, {})
            hb = HeartbeatPayload(
                battery_pct=bat.get("battery_pct"),
                charging=bat.get("charging", False),
                network=net.get("network", "unknown")).model_dump()
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

    def poll_job(self) -> dict | None:
        try:
            out = self._post("/v1/agent/jobs/poll", {})
        except (OSError, RuntimeError):
            return None
        return out.get("job")

    def run_one_job(self) -> bool:
        job = self.poll_job()
        if not job:
            return False
        job_id, tool, inputs = job["job_id"], job["tool"], job.get("inputs", {})
        handler = self._tools.get(tool)
        if handler is None:
            self._post("/v1/agent/jobs/result",
                       {"job_id": job_id, "ok": False,
                        "error": f"tool not implemented on device: {tool}"})
            return True
        try:
            result = handler(dict(inputs), {"device_id": self.device_id})
            if not isinstance(result, dict):
                raise ValueError("tool must return a dict")
            self._post("/v1/agent/jobs/result",
                       {"job_id": job_id, "ok": True, "result": result})
        except Exception as e:  # noqa: BLE001 — report, don't crash
            try:
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

    # ---------- main loop ----------

    def serve_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        self.load_key()
        self.register()
        last_hb = 0.0
        backoff = POLL_IDLE_S
        while not self._stop.is_set():
            try:
                now = time.time()
                if now - last_hb >= HEARTBEAT_S:
                    self.heartbeat()
                    last_hb = now
                busy = self.run_one_job()
                backoff = POLL_IDLE_S if busy else POLL_IDLE_S
                self._stop.wait(1.0 if busy else POLL_IDLE_S)
            except (OSError, RuntimeError):
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
