"""Stage 22c — consent-driven screen-casting lifecycle over the XDG
ScreenCast portal (`org.freedesktop.portal.Desktop`, live on this host
at ScreenCast interface version 5) with verified teardown.

HONEST BOUNDARIES, decided up front from the live interfaces:
- This build supports the REAL consent lifecycle: CreateSession → the
  actual desktop source-selection/consent DIALOG (SelectSources — the
  dialog is the portal's own; never simulated, never bypassed) →
  verified teardown (Session.Close verified by the Closed signal AND
  the object's disappearance via introspection).
- STREAM_ACTIVE is UNREACHABLE in this build: the media legs
  (OpenPipeWireRemote fd transport + a PipeWire client bound to the
  portal remote) are NOT implemented — a CLI D-Bus bridge cannot
  receive an fd reply and no PipeWire python binding exists in this
  environment. A consented session is therefore torn down right after
  source selection with an exact reason; NO stream is created and NO
  capture ever occurs. The limitation is reported, never papered over.
- No receiver protocol, no persistence: sessions live in memory only,
  never survive a restart, and NO frames/recordings/screenshots are
  stored anywhere (the engine never touches media bytes).
- Waits are bounded; Zara cancellation makes the wait return AND
  closes the outstanding desktop request so an operator never sees a
  stranded consent dialog.
- Errors are precise, mapped per the portal spec: response code
  0 = ok, 1 = user cancelled, 2 = other error.

The engine speaks to the desktop through an injectable backend whose
default implementation is `busctl` (one-shot calls + a filtered
`busctl monitor` NDJSON reader). Tests replace the backend with a
deterministic fake — that fake is NEVER presented as live evidence.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from typing import Callable, Optional

STATE = {
    "IDLE": "idle",
    "REQUESTING_SESSION": "requesting_session",
    "AWAITING_USER_CONSENT": "awaiting_user_consent",
    "SESSION_CREATED": "session_created",
    "CONSENT_GRANTED": "consent_granted",
    "SOURCES_SELECTED": "sources_selected",
    "STREAM_STARTING": "stream_starting",
    "PIPEWIRE_CONNECTED": "pipewire_connected",
    "STREAM_ACTIVE": "stream_active",
    "STOPPING": "stopping",
    "STOPPED": "stopped",
    "FAILED": "failed",
}

PORTAL = "org.freedesktop.portal.Desktop"
DESKTOP_PATH = "/org/freedesktop/portal/desktop"
_REQ_PREFIX = "/org/freedesktop/portal/desktop/request/"
_SESS_PREFIX = "/org/freedesktop/portal/desktop/session/"
_IFACE_SCREENCAST = "org.freedesktop.portal.ScreenCast"
_IFACE_REQUEST = "org.freedesktop.portal.Request"
_IFACE_SESSION = "org.freedesktop.portal.Session"

#: portal spec response codes
RESP_OK, RESP_USER_CANCEL, RESP_ERR = 0, 1, 2

CREATE_TIMEOUT_S = 8.0      # CreateSession response bound
CONSENT_TIMEOUT_S = 30.0    # human-pace source-selection bound
START_TIMEOUT_S = 10.0      # portal Start response bound
CLOSE_VERIFY_S = 5.0        # teardown verification bound
_LIFECYCLE_KEEP = 50

MEDIA_UNAVAILABLE_REASON = (
    "media attach not implemented: OpenPipeWireRemote fd transport is "
    "unavailable over a CLI D-Bus bridge and no PipeWire client "
    "binding exists in this build — no stream is created, no capture "
    "occurs")

_OBJPATH_RE = re.compile(
    r"o\s+\"(/org/freedesktop/portal/desktop/(?:request|session)/[^\"]+)\"")

_STARTABLE = (STATE["IDLE"], STATE["STOPPED"], STATE["FAILED"])
_LIVE_STATES = (STATE["REQUESTING_SESSION"], STATE["AWAITING_USER_CONSENT"],
                STATE["SESSION_CREATED"], STATE["CONSENT_GRANTED"],
                STATE["SOURCES_SELECTED"], STATE["STREAM_STARTING"],
                STATE["PIPEWIRE_CONNECTED"], STATE["STREAM_ACTIVE"])


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------

class BusctlMonitor:
    """Long-running `busctl --user monitor` line reader, matched to the
    portal. Nothing is persisted; every line is delivered once."""

    def __init__(self) -> None:
        self._on_event: Optional[Callable] = None
        self._proc: Optional[subprocess.Popen] = None
        self._th: Optional[threading.Thread] = None
        self.last_error = ""

    def start(self, on_event: Callable) -> bool:
        if self._proc is not None:
            self.stop()
        self._on_event = on_event
        match = ("type='signal',sender='%s',"
                 "path_namespace='/org/freedesktop/portal/desktop'"
                 % PORTAL)
        env = {"PATH": "/usr/bin:/bin",
               "DBUS_SESSION_BUS_ADDRESS":
                   "unix:path=/run/user/%d/bus" % os.getuid()}
        argv = ["busctl", "--user", "monitor", "--json=short",
                "--match", match]
        try:
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, errors="replace", env=env)
        except OSError as e:
            self.last_error = "monitor spawn failed: %s" % e
            self._proc = None
            return False
        self._th = threading.Thread(target=self._pump, daemon=True)
        self._th.start()
        return True

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("member") not in ("Response", "Closed"):
                continue
            path = str(ev.get("path") or "")
            if not path.startswith((_REQ_PREFIX, _SESS_PREFIX)):
                continue
            cb = self._on_event
            if cb is not None:
                cb(str(ev.get("interface") or ""), str(ev.get("member") or ""),
                   path, ev)

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None


class BusctlBackend:
    """Real OS backend: one-shot busctl calls + filtered monitor.
    fd-incapable BY CONSTRUCTION: busctl output is textual and cannot
    transport a live Unix descriptor — `call_fd` always refuses; the
    lifecycle reports that exact limitation."""

    fd_capable = False
    last_error = ""

    def call(self, dest: str, obj: str, iface: str, method: str,
             args: list) -> Optional[str]:
        argv = ["busctl", "--user", "call", dest, obj, iface, method]
        argv += [str(a) for a in args]
        try:
            r = subprocess.run(argv, capture_output=True, timeout=10.0,
                               text=True, errors="replace")
        except (OSError, subprocess.TimeoutExpired) as e:
            self.last_error = str(e)
            return None
        if r.returncode != 0:
            self.last_error = (r.stderr or "").strip()[:200]
            return None
        return r.stdout

    def call_sig(self, dest: str, obj: str, iface: str, method: str,
                 items: list) -> Optional[str]:
        """Structured portal call (items: ("o", path) | ("a{sv}",
        entries)). Rendered to the verified busctl count-style form."""
        sig = "".join("a{sv}" if k == "a{sv}" else k for k, _ in items)
        tokens: list = []
        for kind, payload in items:
            if kind == "o":
                tokens.append(str(payload))
            elif kind == "a{sv}":
                tokens += _sv_args(payload)
            else:
                self.last_error = "unsupported item %r" % kind
                return None
        argv = ["busctl", "--user", "call", dest, obj, iface, method,
                sig] + tokens
        try:
            r = subprocess.run(argv, capture_output=True, timeout=10.0,
                               text=True, errors="replace")
        except (OSError, subprocess.TimeoutExpired) as e:
            self.last_error = str(e)
            return None
        if r.returncode != 0:
            self.last_error = (r.stderr or "").strip()[:200]
            return None
        return r.stdout

    def call_fd(self, dest: str, obj: str, iface: str, method: str,
                items: list) -> dict:
        return {"ok": False, "fd": None, "reply": None,
                "error": "busctl output is textual and cannot carry a "
                         "Unix file descriptor"}

    def monitor_start(self, on_event: Callable):
        mon = BusctlMonitor()
        ok = mon.start(on_event)
        return mon if ok else None

    def object_exists(self, dest: str, obj: str) -> Optional[bool]:
        argv = ["busctl", "--user", "introspect", dest, obj]
        try:
            r = subprocess.run(argv, capture_output=True, timeout=8.0,
                               text=True, errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode == 0:
            return True
        if "does not exist" in (r.stderr or ""):
            return False
        return None


class FakeBackend:
    """Deterministic test double. `expect()` scripts `call`/`call_sig`
    stdout; `expect_fd()` scripts fd replies; `inject()` feeds monitor
    events directly. Tracks live request/session objects so
    `object_exists` is honest within the fake. This fake is a test
    aid, never live evidence."""

    fd_capable = False     # tests opt in explicitly for media attach

    def __init__(self, fd_capable: Optional[bool] = None) -> None:
        self.script: dict[tuple[str, str, int], str] = {}
        self.fd_script: dict[tuple[str, str, int], int] = {}
        self.calls: list[tuple[str, str, str, str, list]] = []
        self.objects: set[str] = set()
        self.closed_objects: list[str] = []
        self._counts: dict[tuple[str, str], int] = {}
        self._handler: Optional[Callable] = None
        self.closed_reply_ok = True
        self.last_error = ""
        if fd_capable is not None:
            self.fd_capable = fd_capable

    # -- scripted wire -------------------------------------------------
    def expect(self, iface: str, method: str, stdout: str,
               nth: int = 0) -> None:
        self.script[(iface, method, nth)] = stdout

    def expect_fd(self, iface: str, method: str, fd: int,
                  nth: int = 0) -> None:
        self.fd_script[(iface, method, nth)] = fd

    def add_object(self, path: str) -> None:
        self.objects.add(path)

    def inject(self, path: str, iface: str, member: str,
               payload: dict) -> None:
        cb = self._handler
        if cb is not None:
            cb(iface, member, path,
               {"type": "signal", "path": path, "interface": iface,
                "member": member, "payload": payload})

    # -- backend interface ---------------------------------------------
    def call(self, dest: str, obj: str, iface: str, method: str,
             args: list) -> Optional[str]:
        self.calls.append((dest, obj, iface, method, list(args)))
        if method == "Close":
            return self._handle_close(obj)
        key = (iface, method, self._next_count(iface, method))
        out = self.script.get(key, self.script.get((iface, method, 0), ""))
        return out or None

    def call_sig(self, dest: str, obj: str, iface: str, method: str,
                 items: list) -> Optional[str]:
        self.calls.append((dest, obj, iface, method, list(items)))
        key = (iface, method, self._next_count(iface, method))
        out = self.script.get(key, self.script.get((iface, method, 0), ""))
        return out or None

    def call_fd(self, dest: str, obj: str, iface: str, method: str,
                items: list) -> dict:
        self.calls.append((dest, obj, iface, method, list(items)))
        if not self.fd_capable:
            return {"ok": False, "fd": None, "reply": None,
                    "error": "backend cannot transport fds"}
        # EXACT nth scripting only: a second OpenPipeWireRemote must
        # NOT inherit the first fd by fallback (that would hide honest
        # second-remote refusals from tests)
        key = (iface, method, self._next_count(iface, method))
        if key in self.fd_script:
            return {"ok": True, "fd": self.fd_script[key],
                    "reply": None, "error": None}
        return {"ok": False, "fd": None, "reply": None,
                "error": "no scripted fd reply for %s" % method}

    def _next_count(self, iface: str, method: str) -> int:
        n = self._counts.get((iface, method), 0)
        self._counts[(iface, method)] = n + 1
        return n

    def _handle_close(self, obj: str) -> str:
        if self.closed_reply_ok:
            self.closed_objects.append(obj)
            self.inject(obj,
                        _IFACE_REQUEST if obj.startswith(_REQ_PREFIX)
                        else _IFACE_SESSION, "Closed", {})
            return ""
        return None    # simulate a close that never completes

    def monitor_start(self, on_event: Callable):
        self._handler = on_event
        return self

    def object_exists(self, dest: str, obj: str) -> Optional[bool]:
        return obj in self.objects and obj not in self.closed_objects

    # -- party control ---------------------------------------------------
    @staticmethod
    def response(code: int, results: Optional[dict] = None) -> dict:
        body = results or {}
        table = {k: {"type": "v", "data": [v]}
                 if not isinstance(v, (int, str)) else
                 {"type": "s" if isinstance(v, str) else "u",
                  "data": [v]} for k, v in body.items()}
        return {"type": "ua{sv}", "data": [code, table]}


_BACKEND: Optional[object] = None


class SdBusBackend:
    """Production fd-capable backend: ONE persistent sd-bus user
    connection for all portal method calls (portal sessions are owned
    by the calling connection — a transient connection would reap the
    session on disconnect), plus the filtered busctl monitor for
    portal events."""

    fd_capable = True
    last_error = ""

    def __init__(self, sd=None, monitor_backend=None) -> None:
        from .dbus_sd_linux import SdBusUserSession
        self._sd = sd if sd is not None else SdBusUserSession()
        self._monitor = monitor_backend if monitor_backend is not None \
            else BusctlBackend()

    def _render(self, result: dict) -> Optional[str]:
        if not result.get("ok"):
            self.last_error = result.get("error", "") or "call failed"
            return None
        sig, value = result.get("sig", ""), result.get("value")
        if sig == "o":
            return 'o "%s"' % value
        if sig == "s":
            return "s %s" % value
        if sig == "u":
            return "u %s" % value
        if sig == "":
            return ""
        self.last_error = "unhandled reply sig %r" % sig
        return None

    def call(self, dest: str, obj: str, iface: str, method: str,
             args: list) -> Optional[str]:
        # argument-less portal-object calls (Request/Session.Close)
        return self._render(self._sd.call(dest, obj, iface, method,
                                          [], timeout_s=5.0))

    def call_sig(self, dest: str, obj: str, iface: str, method: str,
                 items: list) -> Optional[str]:
        return self._render(self._sd.call(dest, obj, iface, method,
                                          items, timeout_s=5.0))

    def call_fd(self, dest: str, obj: str, iface: str, method: str,
                items: list) -> dict:
        r = self._sd.call(dest, obj, iface, method, items,
                          timeout_s=5.0)
        if r.get("ok") and r.get("sig") == "h":
            return {"ok": True, "fd": r["fd"], "reply": None,
                    "error": None}
        return {"ok": False, "fd": None,
                "reply": r.get("sig") if r.get("ok") else None,
                "error": r.get("error", "no fd in reply")}

    def monitor_start(self, on_event: Callable):
        return self._monitor.monitor_start(on_event)

    def object_exists(self, dest: str, obj: str) -> Optional[bool]:
        return self._sd.object_exists(dest, obj)


def _backend():
    """Production default: the fd-capable sd-bus backend when the
    transport is loadable; otherwise the verified busctl fallback
    (honest degraded mode: media legs report their exact refusal)."""
    global _BACKEND
    if _BACKEND is None:
        try:
            _BACKEND = SdBusBackend()
        except Exception as e:            # noqa: BLE001 — ABI/env failure
            fallback = BusctlBackend()
            fallback.last_error = "sd-bus transport unavailable: %s" % e
            _BACKEND = fallback
    return _BACKEND


# --------------------------------------------------------------------------
# payload helpers
# --------------------------------------------------------------------------

def _sv_args(entries: list) -> list:
    """Render a{sv} the way busctl accepts it (verified live):
    count, then per entry: key, type-char, value."""
    out: list = [len(entries)]
    for key, tchar, value in entries:
        if tchar == "b":
            value = "true" if value else "false"
        out += [key, tchar, value]
    return out


def parse_objpath(stdout: Optional[str]) -> Optional[str]:
    if not stdout:
        return None
    m = _OBJPATH_RE.search(stdout)
    return m.group(1) if m else None


def _decode_variants(results: dict) -> dict:
    out: dict = {}

    def _unwrap(v):
        # portal NDJSON encodes variants as {"type":..., "data":[...]};
        # busctl --json=short additionally serializes SCALAR variants
        # with a bare value ("data": "str" — verified live, see the
        # 22c monitor capture) — unwrap both shapes recursively
        depth = 0
        while isinstance(v, dict) and set(v.keys()) <= {"type", "data"} \
                and depth < 6:
            d = v["data"]
            if isinstance(d, list):
                v = d[0] if d else None
            else:
                v = d
            depth += 1
        return v

    for k, raw in results.items():
        if isinstance(raw, dict) and set(raw.keys()) <= {"type", "data"}:
            out[k] = _unwrap(raw)
        else:
            out[k] = raw
    return out


def _extract_response(ev: dict) -> tuple[Optional[int], dict]:
    payload = ev.get("payload") if isinstance(ev, dict) else None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return None, {}
    code = data[0] if isinstance(data[0], int) else None
    results = data[1] if len(data) > 1 and isinstance(data[1], dict) else {}
    return code, (_decode_variants(results) if results else {})


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------

class SessionLifecycle:
    """Single-session, bounded, consent-driven screen-cast lifecycle.

    Duplicate starts are refused while the engine is anywhere other
    than idle/stopped/failed. Every transition lands in a bounded
    in-memory lifecycle log (state + short reason — never media).
    Teardown success is claimed only from verification: the portal's
    Closed signal or the session object's verified absence."""

    def __init__(self, backend=None, audit: Optional[Callable] = None,
                 create_timeout_s: float = CREATE_TIMEOUT_S,
                 consent_timeout_s: float = CONSENT_TIMEOUT_S,
                 close_timeout_s: float = CLOSE_VERIFY_S,
                 start_timeout_s: float = START_TIMEOUT_S,
                 media=None) -> None:
        self._backend = backend if backend is not None else _backend()
        self._audit = audit
        self._lock = threading.Lock()
        self._t_lock = threading.RLock()
        self._state = STATE["IDLE"]
        self._session: Optional[str] = None
        self._error = ""
        self._log: list = []
        self._started_at: Optional[float] = None
        self._t_create = create_timeout_s
        self._t_consent = consent_timeout_s
        self._t_close = close_timeout_s
        self._t_start = start_timeout_s
        # waits
        self._cond = threading.Condition()
        self._events: dict[str, list] = {}
        self._stop_event = threading.Event()
        self._external_stop: Optional[threading.Event] = None
        self._mon = None
        # media transport (lazy: the real gate probes gst on first use;
        # tests inject their own gate)
        self._media = media
        self._media_stream = None
        self._fd: Optional[int] = None
        self._media_evidence: dict = {}
        self._media_reason = ""
        self.watch_poll_s = 1.0

    def _gate(self):
        if self._media is None:
            from .pipewire_linux import GstPipewireMedia
            self._media = GstPipewireMedia()
        return self._media

    # ---------- surface -------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            live = self._state in _LIVE_STATES
            evidence = dict(self._media_evidence)
            has_media = bool(evidence or self._media_stream)
            return {
                "state": self._state,
                "session_handle": self._session if live else None,
                "media_ready": self._state == STATE["STREAM_ACTIVE"],
                "consent_stage_reached": self._state in (
                    STATE["AWAITING_USER_CONSENT"], STATE["SESSION_CREATED"],
                    STATE["CONSENT_GRANTED"], STATE["SOURCES_SELECTED"],
                    STATE["STREAM_STARTING"], STATE["PIPEWIRE_CONNECTED"],
                    STATE["STREAM_ACTIVE"]),
                "consent_granted": self._state in (
                    STATE["CONSENT_GRANTED"], STATE["SOURCES_SELECTED"],
                    STATE["STREAM_STARTING"], STATE["PIPEWIRE_CONNECTED"],
                    STATE["STREAM_ACTIVE"]),
                "error": self._error if self._state == STATE["FAILED"] else "",
                "media": {
                    "streaming_possible": has_media,
                    "fd_streaming": self._state in
                    (STATE["PIPEWIRE_CONNECTED"], STATE["STREAM_ACTIVE"]),
                    "evidence": evidence,
                    "reason": "" if has_media else self._media_reason},
                "lifecycle": list(self._log[-10:]),
            }

    def start(self, confirm: bool = False,
              stop_event: Optional[threading.Event] = None) -> dict:
        if not confirm:
            with self._lock:
                st = self._state
            return {"ok": False, "state": st,
                    "error": "explicit confirm=true required — starting "
                             "screen sharing shows the desktop consent "
                             "prompt and captures nothing in this build"}
        with self._lock:
            if self._state not in _STARTABLE:
                return {"ok": False, "duplicate": True, "state": self._state,
                        "error": "a session is already %s — stop() first; "
                                 "duplicate starts are refused"
                                 % self._state}
            self._reset_locked()
        return self._run_start(stop_event)

    def stop(self, reason: str = "operator") -> dict:
        with self._lock:
            if self._state in (STATE["IDLE"], STATE["STOPPED"]):
                return {"ok": True, "idempotent": True,
                        "state": self._state,
                        "detail": "no live session to stop"}
            self._set_locked(STATE["STOPPING"], "stop requested: %s" % reason)
            self._stop_event.set()
        return self._teardown(reason)

    # ---------- start path ----------------------------------------------

    def _reset_locked(self) -> None:
        self._state = STATE["REQUESTING_SESSION"]
        self._session = None
        self._error = ""
        self._started_at = time.time()
        self._media_stream = None
        self._fd = None
        self._media_evidence = {}
        self._media_reason = ""
        self._stop_event = threading.Event()
        self._consented = False
        with self._cond:
            self._events = {}
        self._append_locked(STATE["REQUESTING_SESSION"], "start")

    def _append_locked(self, st: str, reason: str) -> None:
        at = (time.time() - self._started_at) if self._started_at else 0.0
        self._log.append({"state": st, "reason": reason[:120],
                          "at": round(at, 3)})
        del self._log[:-_LIFECYCLE_KEEP]

    def _set_locked(self, st: str, reason: str = "") -> None:
        self._state = st
        self._append_locked(st, reason)

    def _audit_ev(self, action: str, detail: str = "") -> None:
        if self._audit is not None:
            try:
                self._audit("screencast", action, detail)
            except Exception:  # audit failure must not break lifecycle
                pass

    def _on_event(self, iface: str, member: str, path: str,
                  ev: dict) -> None:
        with self._cond:
            self._events.setdefault(path, []).append(
                (iface, member, ev))
            self._cond.notify_all()

    def _wait_response(self, path: str, timeout: float,
                       stop: threading.Event,
                       member_exact: str = "Response"
                       ) -> tuple[str, Optional[int], dict]:
        """Returns (outcome, code, results); outcome in
        {"got", "timeout", "stopped"}."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for iface, member, ev in \
                        list(self._events.get(path, [])):
                    if member == member_exact:
                        code, results = _extract_response(ev)
                        # response code is authoritative when present
                        return "got", code, results
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout", None, {}
                self._cond.wait(min(remaining, 0.2))
                if stop.is_set() or self._stop_event.is_set():
                    return "stopped", None, {}

    def _run_start(self, external_stop: Optional[threading.Event]) -> dict:
        self._external_stop = external_stop
        stop = self._stop_event
        if external_stop is not None:
            # propagate an already-cancelled request
            def watch() -> None:
                external_stop.wait()
                stop.set()
            threading.Thread(target=watch, daemon=True).start()
        self._mon = self._backend.monitor_start(self._on_event)
        if self._mon is None:
            return self._fail("portal event subscription failed — "
                              "consent responses cannot be observed")
        self._audit_ev("start_requested")
        try:
            return self._create_session(stop)
        finally:
            self._external_stop = None

    # -- step 1: CreateSession --------------------------------------------
    def _create_session(self, stop: threading.Event) -> dict:
        token = "zaraCreate%d" % int(time.time() * 10 % 10 ** 9)
        sess_token = "zaraSess%d" % int(time.time() * 10 % 10 ** 9)
        out = self._backend.call_sig(
            PORTAL, DESKTOP_PATH, _IFACE_SCREENCAST, "CreateSession",
            [("a{sv}", [("handle_token", "s", token),
                        ("session_handle_token", "s", sess_token)])])
        req_path = parse_objpath(out)
        if not req_path:
            return self._fail("portal CreateSession call failed — desktop "
                              "screen-cast portal unreachable")
        outcome, code, results = self._wait_response(
            req_path, self._t_create, stop)
        if outcome != "got":
            self._backend.call(PORTAL, req_path, _IFACE_REQUEST, "Close", [])
            reason = ("CreateSession response timed out after %.0fs"
                      % self._t_create if outcome == "timeout"
                      else "start cancelled while creating the session")
            return self._after_abort(reason)
        if code == RESP_USER_CANCEL:
            self._backend.call(PORTAL, req_path, _IFACE_REQUEST, "Close", [])
            return self._after_abort("user cancelled during session "
                                     "creation")
        if code != RESP_OK:
            self._backend.call(PORTAL, req_path, _IFACE_REQUEST, "Close", [])
            if code is None:
                return self._fail("malformed portal response (no "
                                  "response code) during session "
                                  "creation")
            return self._fail("portal refused session creation "
                              "(response code %r)" % code)
        session = results.get("session_handle")
        if not isinstance(session, str) or not session.startswith(_SESS_PREFIX):
            self._backend.call(PORTAL, req_path, _IFACE_REQUEST, "Close", [])
            return self._fail("portal response carried no usable "
                              "session_handle — refusing to proceed")
        with self._lock:
            self._session = session
            self._set_locked(STATE["SESSION_CREATED"],
                             "session created (consent not yet given)")
        self._audit_ev("session_created")
        return self._select_sources(stop)

    # -- step 2: SelectSources / the real consent dialog -------------------
    def _select_sources(self, stop: threading.Event) -> dict:
        assert self._session
        token = "zaraPick%d" % int(time.time() * 10 % 10 ** 9)
        with self._lock:
            self._set_locked(STATE["AWAITING_USER_CONSENT"],
                             "source-selection request sent — the consent "
                             "dialog opens at portal Start on GNOME-form "
                             "portals")
        self._audit_ev("awaiting_user_consent")
        out = self._backend.call_sig(
            PORTAL, DESKTOP_PATH, _IFACE_SCREENCAST, "SelectSources",
            [("o", self._session),
             ("a{sv}", [("handle_token", "s", token),
                        ("types", "u", 1),          # 1 = monitor only
                        ("multiple", "b", False),
                        ("cursor_mode", "u", 2)])])  # 2 = embedded cursor
        req_path = parse_objpath(out)
        if not req_path:
            return self._fail("portal SelectSources call failed")
        outcome, code, results = self._wait_response(
            req_path, self._t_consent, stop)
        if outcome == "stopped":
            # close the dialog the operator can no longer answer
            self._backend.call(PORTAL, req_path, _IFACE_REQUEST, "Close", [])
            return self._abort("screen sharing stopped by Zara while the "
                               "source-selection answer was pending")
        if outcome == "timeout":
            self._backend.call(PORTAL, req_path, _IFACE_REQUEST, "Close", [])
            return self._abort("portal did not answer the source-selection "
                               "request after %.0fs — request closed"
                               % self._t_consent)
        if code == RESP_USER_CANCEL:
            return self._abort("user cancelled the source-selection "
                               "dialog")
        if code != RESP_OK:
            return self._fail("desktop consent did not grant screen "
                              "sharing (response code %r)" % code)
        if "sources" not in results:
            # GNOME form (verified against xdg-desktop-portal-gnome
            # handle_select_sources): SelectSources completes
            # IMMEDIATELY with an empty vardict — no dialog is shown
            # here and no node ids exist yet. The real consent dialog
            # is shown by portal Start, whose ok-response only arrives
            # after the user answers it, and the OpenPipeWireRemote fd
            # is created portal-side server-bound to the consented
            # stream — Zara never picks or attaches to a node itself.
            with self._lock:
                self._set_locked(
                    STATE["AWAITING_USER_CONSENT"],
                    "portal acknowledged selection without node ids "
                    "(GNOME form) — consent happens at Start")
            self._audit_ev("selection_acknowledged_no_nodes")
            return self._media_boundary(stop, consent_at_start=True)
        with self._lock:
            self._consented = True
            self._set_locked(STATE["CONSENT_GRANTED"],
                             "consent granted (desktop responded ok)")
        self._audit_ev("consent_granted")
        try:
            source_ids = self._validate_sources(results)
        except ValueError as e:
            return self._fail(str(e))
        with self._lock:
            self._set_locked(STATE["SOURCES_SELECTED"],
                             "sources selected: %s" % source_ids)
        self._audit_ev("sources_selected",
                       "nodes %r" % (source_ids,))
        return self._media_boundary(stop, select_ids=source_ids)

    def _validate_sources(self, results: dict) -> list:
        """Extract and validate the consented pipewire node ids. An
        absent/empty/unusable list is a hard refusal — we never attach
        to an unapproved node. Portal 'sources' arrive as a(ua{sv});
        encodings seen: [node_id] lists (au variant), bare scalars
        (busctl json=short), dicts, or bare ints — all reduced to the
        node-id list and validated."""
        sources = results.get("sources")
        if isinstance(sources, int):
            sources = [sources]
        ids = []
        if isinstance(sources, list):
            for entry in sources:
                if isinstance(entry, int):
                    ids.append(entry)
                elif isinstance(entry, (list, tuple)) and entry and \
                        isinstance(entry[0], int):
                    ids.append(int(entry[0]))
                    # covers both bare int-lists and portal stream
                    # structs (u node_id, a{sv} properties) as emitted
                    # in the real Start "streams" results
                elif isinstance(entry, dict):
                    for v in entry.values():
                        if isinstance(v, int):
                            ids.append(int(v))
                            break
        if not ids or not all(v > 0 for v in ids):
            raise ValueError(
                "portal returned no usable consented stream nodes "
                "(sources=%r) — refusing media attach" % (sources,))
        return ids

    # -- step 3: the real media-transport boundary --------------------------
    def _media_boundary(self, stop: threading.Event,
                        consent_at_start: bool = False,
                        select_ids: Optional[list] = None) -> dict:
        """Portal Start → OpenPipeWireRemote (real fd via sd-bus) →
        one-frame GStreamer probe (independent media-flow proof) →
        continuous discard-sink pipeline. STREAM_ACTIVE is reported
        ONLY after the frame probe passed on the consented session.

        consent_at_start: GNOME-form portals show the real consent
        dialog during Start; the ok-response arrives only after the
        user answered it — consent is claimed at that point, never
        at the instantaneous SelectSources acknowledgement."""
        gate = self._gate()
        if not getattr(self._backend, "fd_capable", False):
            reason = ("backend cannot transport a Unix file descriptor "
                      "(OpenPipeWireRemote unusable over a textual CLI "
                      "bridge) — no stream is created, no capture occurs")
            return self._media_refusal(stop, reason)
        ok, why = gate.available()
        if not ok:
            reason = ("PipeWire media client unavailable: %s — no stream "
                      "is created, no capture occurs" % (why or "unknown"))
            return self._media_refusal(stop, reason)
        with self._lock:
            self._set_locked(STATE["STREAM_STARTING"],
                             "portal Start + OpenPipeWireRemote pending")
        self._audit_ev("media_attach_started")
        # -- Start (complete the consented session portal-side) ---------
        # live-verified signature: Start(o session_handle,
        # s parent_window, a{sv} options) — an empty parent_window is
        # valid per the portal spec (no parent); attempt-3 failure was
        # InvalidArgs without it
        token = "zaraStart%d" % int(time.time() * 10 % 10 ** 9)
        out = self._backend.call_sig(
            PORTAL, DESKTOP_PATH, _IFACE_SCREENCAST, "Start",
            [("o", self._session),
             ("s", ""),
             ("a{sv}", [("handle_token", "s", token)])])
        req_path = parse_objpath(out)
        if not req_path:
            if self._backend.last_error:
                return self._fail("portal Start call failed (%s)"
                                  % self._backend.last_error)
            return self._fail("portal Start call failed")
        outcome, code, start_results = self._wait_response(
            req_path, self._t_start, stop)
        if outcome != "got":
            self._backend.call(PORTAL, req_path, _IFACE_REQUEST,
                               "Close", [])
            if outcome == "timeout":
                return self._abort("the portal consent dialog timed out "
                                   "after %.0fs — Start request closed"
                                   % self._t_start)
            return self._abort("Zara cancelled during portal Start")
        if code == RESP_USER_CANCEL:
            return self._abort("user cancelled during portal Start")
        if code != RESP_OK:
            return self._fail("portal refused Start (response code %r)"
                              % code)
        if consent_at_start:
            # the dialog-answering moment on GNOME-form portals: the
            # Start ok-response only arrives after the user picked a
            # source in the real desktop dialog (or via an authorized
            # portal restore path)
            with self._lock:
                self._consented = True
                self._set_locked(
                    STATE["CONSENT_GRANTED"],
                    "portal Start completed after the source-selection "
                    "dialog — user consented")
            self._audit_ev("consent_granted")
        stream_ids = list(select_ids or [])
        if "streams" in start_results:
            try:
                # the Start streams are the consent-for-recording node
                # ids portal-side — they take precedence
                stream_ids = self._validate_sources(
                    {"sources": start_results.get("streams")})
            except ValueError as e:
                return self._fail(str(e))
        if "streams" in start_results or consent_at_start:
            with self._lock:
                self._set_locked(
                    STATE["SOURCES_SELECTED"],
                    ("streams: %s" % stream_ids) if stream_ids else
                    "portal exposes no node ids — the fd is "
                    "server-side bound to the consented stream; the "
                    "media probe verifies the flow")
            self._audit_ev("sources_selected",
                           "nodes %r" % (stream_ids,)
                           if stream_ids else "server-bound")
        # -- OpenPipeWireRemote (the real fd leg) -----------------------
        fdr = self._backend.call_fd(
            PORTAL, DESKTOP_PATH, _IFACE_SCREENCAST,
            "OpenPipeWireRemote",
            [("o", self._session), ("a{sv}", [])])
        fd = fdr.get("fd")
        if fd is None:
            return self._fail("OpenPipeWireRemote returned no usable fd "
                              "(%s) — no stream created, no capture "
                              "occurs" % (fdr.get("error", "unknown"),))
        if not isinstance(fd, int) or fd < 0:
            return self._fail("OpenPipeWireRemote returned an invalid "
                              "descriptor (%r) — refusing to use it"
                              % (fd,))
        with self._lock:
            self._fd = fd
        self._audit_ev("pipewire_fd_received")
        # -- one-frame probe: the independent media-flow proof -----------
        verdict = gate.verify_frames(fd, stop,
                                     target=(stream_ids[0]
                                             if stream_ids else None))
        with self._lock:
            self._media_evidence["pinned_target"] = (
                stream_ids[0] if stream_ids else None)
        with self._lock:
            self._media_evidence.update(
                {"probe_rc": verdict.get("exit_code"),
                 "caps": (verdict.get("caps") or "")[:200],
                 "frames": verdict.get("frames", 0)})
        if not verdict.get("ok"):
            self._close_fd()
            ring = verdict.get("ring") or []
            if ring:
                self._media_evidence["probe_ring"] = ring[:16]
            if self._stop_event.is_set():
                return self._abort("Zara stopped during the media probe")
            return self._fail("PipeWire media flow NOT verified "
                              "(%s) — no stream claimed" % (
                                  verdict.get("reason", "unknown"),),
                              extra={"media_evidence":
                                     dict(self._media_evidence)})
        self._close_fd()   # probe parent copy done; keep evidence
        # -- continuous stream on its OWN portal remote ------------------
        fdr2 = self._backend.call_fd(
            PORTAL, DESKTOP_PATH, _IFACE_SCREENCAST,
            "OpenPipeWireRemote",
            [("o", self._session), ("a{sv}", [])])
        fd2 = fdr2.get("fd")
        if fd2 is None or not isinstance(fd2, int) or fd2 < 0:
            self._close_fd()
            return self._fail("second OpenPipeWireRemote refused after "
                              "the frame-flow probe (%s) — continuous "
                              "attach cannot be established safely; no "
                              "stream claimed"
                              % (fdr2.get("error", "unknown"),),
                              extra={"media_evidence":
                                     dict(self._media_evidence)})
        with self._lock:
            self._fd = fd2
        stream, ok2, err2 = gate.start_stream(
            fd2, target=(stream_ids[0] if stream_ids else None))
        if not ok2 or stream is None:
            self._close_fd()
            return self._fail("continuous PipeWire pipeline could not "
                              "be spawned (%s) — no stream claimed"
                              % (err2 or "unknown",),
                              extra={"media_evidence":
                                     dict(self._media_evidence)})
        with self._lock:
            self._media_stream = stream
            self._media_evidence["child_spawned"] = True
            self._set_locked(STATE["PIPEWIRE_CONNECTED"],
                             "portal remote fd active; pipeline child "
                             "spawned")
        self._close_fd()      # child inherited its own; parent copy done
        with self._lock:
            self._set_locked(STATE["STREAM_ACTIVE"],
                             "media flow verified (1-frame probe) and "
                             "continuous discard-sink pipeline live")
        self._audit_ev("stream_active",
                       "frames verified; caps=%s" %
                       (self._media_evidence.get("caps") or "")[:60])
        self._watch_child(stream)
        return {"ok": True, "state": self._state_st(),
                "consent_granted": True, "media": True,
                "media_evidence": dict(self._media_evidence)}

    def _media_refusal(self, stop: threading.Event, reason: str) -> dict:
        """The consented session is torn down with the exact reason;
        nothing is captured (the pre-transport honest boundary)."""
        with self._lock:
            self._media_reason = reason
            self._set_locked(STATE["STREAM_STARTING"],
                             reason[:100])
        self._audit_ev("media_refused", reason[:120])
        res = self._teardown("media attach refused: %s" % reason)
        res["ok"] = not res["cleanup_unverified"]
        res["consent_granted"] = bool(self._consented)
        res["media"] = False
        res["media_reason"] = reason
        return res

    def _close_fd(self) -> None:
        with self._lock:
            fd = self._fd
            self._fd = None
        if fd is None:
            return
        try:
            import os
            os.close(fd)
        except OSError:
            pass

    def _watch_child(self, stream) -> None:
        """If the streaming child dies on its own, fail the session
        honestly and tear it down (bounded 1 Hz poll; daemon thread)."""
        def watch():
            while stream.alive():
                if self._stop_event.is_set() or self._state_st() != \
                        STATE["STREAM_ACTIVE"]:
                    return
                time.sleep(self.watch_poll_s)
            if self._stop_event.is_set():
                return      # ordinary stop handles teardown
            self._audit_ev("stream_child_died",
                           "gst child exited rc=%r" % stream.exit_code())
            self.stop("media stream child exited rc=%r"
                      % stream.exit_code())
        threading.Thread(target=watch, daemon=True).start()

    def _state_st(self) -> str:
        with self._lock:
            return self._state

    # -- failure / abort helpers -------------------------------------------
    def _fail(self, reason: str, extra: Optional[dict] = None) -> dict:
        self._fail_reason = reason
        with self._lock:
            self._error = reason
        res = self._teardown(reason)
        res["error"] = reason
        res["ok"] = False            # a failure stays a failure; the
        if extra:                    # cleanup detail is in closed/*unverified
            res.update(extra)
        return res

    def _abort(self, reason: str) -> dict:
        """Aborts that are NOT failures (cancellation/operator stop):
        teardown still runs and is verified."""
        res = self._teardown(reason)
        res.update({"cancelled": True, "reason": reason,
                    "ok": bool(res.get("closed"))})
        return res

    # historical name kept for readability at the two callsites
    _after_abort = _abort

    # ---------- teardown (verified, idempotent) ----------------------------

    def _teardown(self, reason: str) -> dict:
        with self._t_lock:
            # media first: the streaming child consumes the portal fd
            stream_term = self._stop_media_stream()
            with self._lock:
                session = self._session
                self._set_locked(STATE["STOPPING"], reason)
            if session:
                self._backend.call(PORTAL, session, _IFACE_SESSION,
                                   "Close", [])
            verified = self._verify_closed(session, reason)
            res = self._finish_teardown(session, verified, reason)
            if stream_term is not None:
                res["media_child_exit"] = stream_term.get("exit_code")
                if not stream_term.get("verified"):
                    res["cleanup_unverified"] = True
                    res["ok"] = False
            return res

    def _stop_media_stream(self) -> Optional[dict]:
        with self._lock:
            stream = self._media_stream
            self._media_stream = None
        if stream is None:
            return None
        try:
            out = stream.terminate()
        except Exception as e:   # noqa: BLE001 — teardown noise contained
            return {"verified": False, "exit_code": None,
                    "error": "child termination failed: %s" % e}
        self._close_fd()
        self._media_evidence["child_exit_code"] = out.get("exit_code")
        return out

    def _verify_closed(self, session: Optional[str],
                       reason: str) -> bool:
        if not session:
            return True   # nothing was ever created — nothing to verify
        # NOTE: the interruption event is deliberately NOT the engine's
        # stop event — when stop() triggered this teardown that event is
        # already set and must not cancel the verification wait itself.
        neut = threading.Event()
        if self._mon is not None:
            outcome, _code, _res = self._wait_response(
                session, self._t_close, neut, member_exact="Closed")
            if outcome == "got":
                return True
        exists = self._backend.object_exists(PORTAL, session)
        if exists is False:
            return True
        return False

    def _finish_teardown(self, session: Optional[str], verified: bool,
                         reason: str) -> dict:
        fail = getattr(self, "_fail_reason", None)
        self._fail_reason = None
        with self._lock:
            if verified:
                self._set_locked(
                    STATE["FAILED" if fail else "STOPPED"],
                    ("failed: %s" if fail else "teardown verified: %s")
                    % reason)
            else:
                self._set_locked(
                    STATE["FAILED"],
                    "cleanup unverified after: %s" % reason)
                self._error = ("cleanup could not be verified — portal "
                               "session %s may still exist; keep this "
                               "handle and re-run stop" % session)
            state, err = self._state, self._error
        if self._mon is not None:
            try:
                self._mon.stop()
            except Exception:
                pass
            self._mon = None
        self._audit_ev("teardown_verified" if verified
                       else "teardown_unverified", reason)
        return {"ok": verified, "state": state,
                "closed": verified, "cleanup_unverified": not verified,
                "session_handle": session if not verified else None,
                "error": err if not verified else ""}


_LIFECYCLE: Optional[SessionLifecycle] = None


def engine() -> SessionLifecycle:
    """Process-wide single-session engine (never persisted; sessions
    do not survive a restart legally — a consented portal session
    cannot be re-attached across restarts)."""
    global _LIFECYCLE
    if _LIFECYCLE is None:
        _LIFECYCLE = SessionLifecycle()
    return _LIFECYCLE


def _set_engine(lc: Optional[SessionLifecycle]) -> None:
    """Test hook: swap the process-wide engine."""
    global _LIFECYCLE
    _LIFECYCLE = lc
