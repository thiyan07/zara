#!/usr/bin/env python3
"""Isolated end-to-end journey for the assistant gateway (stdlib only).

Spawns an all-memory server on a verified-free port with a distinct token,
exercises talk / sessions / streaming / memory / executions / cancel /
policy-gating / failure-honesty, revokes the probe device, stops the server
by exact PID, and exits 0 only if every step passes.

No third-party deps: urllib + stdlib only. Sanitized output: status codes,
counts, ids and statuses only -- never tokens, keys, pairing codes, or
payload dumps.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = "/tmp/opencode"
LOG_PATH = os.path.join(LOG_DIR, "demo_e2e_server.log")

# Child server code lives in-script: all-memory stack on $E2E_PORT.
CHILD_CODE = r"""
import os
from core.app import build_stack, create_app
import uvicorn
port = int(os.environ["E2E_PORT"])
app = create_app(build_stack())
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
"""

MEMORY_ENV_KEYS = (
    "ZARA_MEMORY_DB", "ZARA_MISSION_DB", "ZARA_AUDIT_DB",
    "ZARA_SESSION_DB", "ZARA_FABRIC_DB", "ZARA_CHAT_DB",
)

_results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str) -> bool:
    _results.append((name, ok, detail))
    print(f"STEP {name}: {'PASS' if ok else 'FAIL'} ({detail})", flush=True)
    return ok


def http(method: str, url: str, token: str = "", body=None,
         extra_headers: dict | None = None, timeout: float = 15.0):
    """Returns (status_code, parsed_json_or_text). Never raises on HTTP
    errors; network errors propagate to the caller."""
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw) if raw else None
            except ValueError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            return e.code, json.loads(raw) if raw else None
        except ValueError:
            return e.code, raw


def wait_for(url: str, timeout_s: float = 30.0):
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            st, body = http("GET", url, timeout=3.0)
            if st == 200 and isinstance(body, dict):
                return body
            last = f"code={st}"
        except Exception as e:  # noqa: BLE001 -- still starting
            last = type(e).__name__
        time.sleep(0.25)
    raise TimeoutError(f"ready timeout ({last})")


def poll_until(desc: str, fn, timeout_s: float = 15.0, interval: float = 0.2):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = fn()
        if last is not None:
            return last
        time.sleep(interval)
    raise TimeoutError(f"poll timeout: {desc} (last={last!r})")


def parse_sse(text: str):
    """Minimal inline SSE parse -> [(event_name, payload_dict)]."""
    events = []
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
        name = None
        data = None
        for line in chunk.split("\n"):
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if name is not None and data is not None:
            events.append((name, json.loads(data)))
    return events


def pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))  # OS-verified free port
        port = s.getsockname()[1]
    finally:
        s.close()
    if port == 8080:
        raise RuntimeError("refusing reserved port 8080")
    return port


def port_is_free(port: int, retries: int = 10) -> bool:
    # SO_REUSEADDR: our own recent client connections may sit in
    # TIME_WAIT; that must not read as "port still held". Retry briefly
    # while the listener socket itself drains.
    for _ in range(retries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.5)
        finally:
            s.close()
    return False


def main() -> int:
    llm_env = (os.environ.get("LLM_PROVIDER", "echo") or "echo").strip().lower()
    mock_or_real = "MOCK" if llm_env in ("echo", "scripted") else "REAL"

    port = pick_free_port()
    token = secrets.token_hex(16)
    base = f"http://127.0.0.1:{port}"
    proc = None
    probe_id = ""
    fail_detail = ""

    try:
        # ---- 1. isolated server -------------------------------------
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            env = {k: v for k, v in os.environ.items()
                   if k not in MEMORY_ENV_KEYS}
            env["ASSISTANT_TOKEN"] = token
            env["E2E_PORT"] = str(port)
            logfh = open(LOG_PATH, "a", buffering=1)  # noqa: PTH123
            proc = subprocess.Popen(
                [sys.executable, "-c", CHILD_CODE],
                env=env, cwd=REPO, stdout=logfh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL)
            child_pid = proc.pid
            ready = wait_for(f"{base}/v1/ready", timeout_s=30.0)
            provider = str((ready.get("checks") or {}).get("provider", "?"))
            ok = bool(ready.get("ready")) and provider != "?"
            report("1-start-ready",
                   ok, f"provider={provider} [{mock_or_real} "
                       f"LLM_PROVIDER={llm_env}] port-verified-free pid-ok")
            if not ok:
                return 1
        except Exception as e:  # noqa: BLE001
            report("1-start-ready", False, f"startup failed: {type(e).__name__}")
            return 1

        # ---- 2. talk turns + session proof ---------------------------
        try:
            st1, t1 = http("POST", f"{base}/v1/talk", token,
                           {"text": "e2e hello turn one"})
            assert isinstance(t1, dict), f"code={st1}"
            sid = t1.get("session_id", "")
            tid1 = t1.get("turn_id", "")
            st2, t2 = http("POST", f"{base}/v1/talk", token,
                           {"text": "e2e hello turn two", "session_id": sid})
            assert isinstance(t2, dict), f"code={st2}"
            tid2 = t2.get("turn_id", "")
            st3, sess = http("GET", f"{base}/v1/sessions", token)
            assert isinstance(sess, list), f"code={st3}"
            match = [s for s in sess if s.get("id") == sid]
            ok = (st1 == 200 and st2 == 200 and bool(sid) and bool(tid1)
                  and bool(tid2) and tid1 != tid2
                  and t2.get("session_id") == sid
                  and len(sess) == 1 and len(match) == 1
                  and match[0].get("message_count") == 4)
            nmsg = match[0].get("message_count", "?") if match else "?"
            report("2-talk-session", ok,
                   f"turns=2 session-ok msgs={nmsg} sessions={len(sess)}")
            if not ok:
                fail_detail = f"codes={st1}/{st2}/{st3}"
        except Exception as e:  # noqa: BLE001
            report("2-talk-session", False, type(e).__name__)

        # ---- 3. streaming turn ---------------------------------------
        try:
            st, raw = http("POST", f"{base}/v1/talk/stream", token,
                           {"text": "e2e stream probe"}, timeout=90.0)
            events = parse_sse(raw) if isinstance(raw, str) else []
            names = [n for n, _ in events]
            ok = (st == 200 and len(names) >= 2 and names[0] == "turn"
                  and names[-1] == "done")
            report("3-talk-stream", ok,
                   f"frames={len(names)} first={names[0] if names else '?'} "
                   f"last={names[-1] if names else '?'}")
            if not ok:
                fail_detail = f"code={st} names={names[:6]}"
        except Exception as e:  # noqa: BLE001
            report("3-talk-stream", False, type(e).__name__)

        # ---- 4. memory remember/search/forget/purge ------------------
        try:
            uniq = f"e2e-{secrets.token_hex(4)} redwood cinder"
            st1, rem = http("POST", f"{base}/v1/memory", token,
                            {"text": f"e2e probe memory {uniq}",
                             "category": "episodic"})
            mid = (rem or {}).get("id", "") if isinstance(rem, dict) else ""
            q = urllib.parse.quote(uniq.split()[0])
            st2, hits1 = http("GET", f"{base}/v1/memory/search?q={q}", token)
            found = any(h.get("id") == mid for h in (hits1 or []))
            st3, _ = (http("DELETE", f"{base}/v1/memory/{mid}", token)
                      if mid else (0, None))
            st4, hits2 = http("GET", f"{base}/v1/memory/search?q={q}", token)
            clean = not any(h.get("id") == mid for h in (hits2 or []))
            st5, purged = http("POST", f"{base}/v1/memory/purge", token,
                               {}, timeout=15.0)
            pcount = (purged or {}).get("purged", "?") if isinstance(
                purged, dict) else "?"
            ok = (st1 == 200 and bool(mid) and found and st3 == 200 and clean
                  and st5 == 200)
            report("4-memory", ok,
                   f"remembered found={found} forgotten clean={clean} "
                   f"purged={pcount}")
            if not ok:
                fail_detail = f"codes={st1}/{st2}/{st3}/{st4}/{st5}"
        except Exception as e:  # noqa: BLE001
            report("4-memory", False, type(e).__name__)

        # ---- 5. async echo -> succeeded; CONFIRM parks ---------------
        try:
            st1, sub = http("POST", f"{base}/v1/executions", token,
                            {"tool": "util.echo",
                             "inputs": {"text": "e2e-task"}})
            eid = (sub or {}).get("id", "") if isinstance(sub, dict) else ""

            def _echo_done():
                st, v = http("GET", f"{base}/v1/executions/{eid}", token)
                if isinstance(v, dict) and v.get("status") == "succeeded":
                    return v
                return None

            view = poll_until("echo succeeded", _echo_done) if eid else None
            st2, park = http("POST", f"{base}/v1/executions", token,
                             {"tool": "shell.safe_readonly",
                              "inputs": {"command": "echo hi"}})
            pstatus = (park or {}).get("status", "?") if isinstance(
                park, dict) else "?"
            ok = (st1 == 201 and view is not None and st2 == 202
                  and pstatus == "waiting_for_permission")
            report("5-task-echo-park", ok,
                   f"echo={view.get('status') if view else '?'} "
                   f"confirm-tool={pstatus} (left parked)")
            if not ok:
                fail_detail = f"codes={st1}/{st2}"
        except Exception as e:  # noqa: BLE001
            report("5-task-echo-park", False, type(e).__name__)

        # ---- 6. cancel async; late result cannot revive ---------------
        # Enrolls one probe device (also used for step 9 poll/result).
        try:
            probe_id = f"e2e-probe-{secrets.token_hex(4)}"
            st0, enr = http("POST", f"{base}/v1/agent/enroll", token,
                            {"device_id": probe_id, "kind": "linux"})
            code = (enr or {}).get("pairing_code", "")
            st1, claim = http("POST", f"{base}/v1/agent/claim", "",
                              {"pairing_code": code})
            dkey = (claim or {}).get("device_key", "")
            dev_h = {"X-Device-Id": probe_id, "X-Device-Key": dkey}
            st2, _ = http("POST", f"{base}/v1/agent/register", "",
                          {"capabilities": ["system.battery"],
                           "software_version": "e2e"},
                          extra_headers=dev_h)
            st3, sub = http("POST", f"{base}/v1/executions", token,
                            {"tool": "system.battery", "inputs": {},
                             "device_id": probe_id})
            ceid = (sub or {}).get("id", "") if isinstance(sub, dict) \
                else ""

            def _job_visible():
                st, v = http("GET", f"{base}/v1/executions/{ceid}", token)
                if isinstance(v, dict) and v.get("job_id"):
                    return v
                return None

            jview = poll_until("job visible", _job_visible) if ceid else None
            jid = (jview or {}).get("job_id", "")
            st4, canc = http("POST", f"{base}/v1/executions/{ceid}/cancel",
                             token)

            def _cancelled():
                st, v = http("GET", f"{base}/v1/executions/{ceid}", token)
                if isinstance(v, dict) and v.get("status") == "cancelled":
                    return v
                return None

            cview = poll_until("cancelled", _cancelled) if ceid else None
            st5, poll = http("POST", f"{base}/v1/agent/jobs/poll", "",
                             {}, extra_headers=dev_h)
            no_job = isinstance(poll, dict) and poll.get("job") is None
            st6, _ = http("POST", f"{base}/v1/agent/jobs/result", "",
                          {"job_id": jid, "ok": True,
                           "result": {"note": "late"}},
                          extra_headers=dev_h)
            st7, final = http("GET", f"{base}/v1/executions/{ceid}", token)
            still = isinstance(final, dict) and final.get(
                "status") == "cancelled"
            ok = (st0 == 200 and st1 == 200 and st2 == 200 and st3 == 201
                  and bool(jid) and cview is not None and no_job
                  and st6 == 200 and still)
            report("6-cancel-late-result", ok,
                   f"cancelled={cview is not None} poll-empty={no_job} "
                   f"late-result-ignored={still}")
            if not ok:
                fail_detail = (f"codes={st0}/{st1}/{st2}/{st3}/{st4}/"
                               f"{st5}/{st6}/{st7}")
        except Exception as e:  # noqa: BLE001
            report("6-cancel-late-result", False, type(e).__name__)

        # ---- 7. scratch tool policy gating ---------------------------
        try:
            st, sub = http("POST", f"{base}/v1/executions", token,
                           {"tool": "filesystem.write",
                            "inputs": {"name": "e2e.txt", "text": "hello"}})
            v = sub if isinstance(sub, dict) else {}
            ok = (st == 202 and v.get("status") == "waiting_for_permission"
                  and v.get("approval_required") is True)
            report("7-scratch-gating", ok,
                   f"filesystem.write={v.get('status', '?')} (never auto-runs)")
            if not ok:
                fail_detail = f"code={st}"
        except Exception as e:  # noqa: BLE001
            report("7-scratch-gating", False, type(e).__name__)

        # ---- 8. failure honesty --------------------------------------
        try:
            a, _ = http("GET", f"{base}/v1/executions/exec-does-not-exist",
                        token)
            b, _ = http("POST", f"{base}/v1/executions", token,
                        {"tool": "no.such.tool", "inputs": {}})
            c, _ = http("POST", f"{base}/v1/executions", token,
                        {"tool": "util.echo",
                         "inputs": {"text": "x" * 6000}})
            d, _ = http("GET", f"{base}/v1/sessions", "wrong-token")
            ok = (a == 404 and b == 404 and c == 422 and d in (401, 403))
            report("8-failure-honesty", ok,
                   f"unknown-exec={a} bad-tool={b} oversize={c} "
                   f"bad-token={d}")
            if not ok:
                fail_detail = f"codes={a}/{b}/{c}/{d}"
        except Exception as e:  # noqa: BLE001
            report("8-failure-honesty", False, type(e).__name__)

        # ---- 9. revoke probe, stop server by PID, port free ----------
        try:
            rcode = "?"
            if probe_id:
                st, _ = http("POST",
                             f"{base}/v1/agent/revoke?device_id={probe_id}",
                             token)
                rcode = st
            ok = (rcode == 200)
            report("9-cleanup", ok and True, f"probe-revoked={rcode}")
            if not ok:
                fail_detail = f"revoke={rcode}"
        except Exception as e:  # noqa: BLE001
            report("9-cleanup", False, type(e).__name__)
    finally:
        if proc is not None:
            child_pid = proc.pid  # exact PID only, never pattern-kill
            try:
                proc.terminate()
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    pass
            free = port_is_free(port)
            # Amend step 9 detail with stop/port outcome is done via a
            # trailing line to keep per-step output single-line.
            print(f"SERVER STOP pid={child_pid} port-free={free}", flush=True)
            for i, (name, ok, detail) in enumerate(_results):
                if name == "9-cleanup":
                    _results[i] = (name, ok and free,
                                   f"{detail} stopped port-free={free}")
                    print(f"STEP 9-cleanup: "
                          f"{'PASS' if ok and free else 'FAIL'} "
                          f"({detail} stopped port-free={free})", flush=True)
                    break

    failed = [(n, d) for n, ok, d in _results if not ok]
    print(f"SUMMARY {len(_results) - len(failed)}/{len(_results)} passed",
          flush=True)
    for n, d in failed:
        print(f"FAILED {n}: {d}", flush=True)
    if fail_detail and failed:
        print(f"LAST-FAIL-HINT {fail_detail}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
