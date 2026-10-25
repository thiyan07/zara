"""Stage 23 — media-transport boundary: GStreamer `pipewiresrc` as the
real PipeWire client for portal ScreenCast sessions.

CHOSEN IMPLEMENTATION (documented decision from live inspection):
- D-Bus fd transport: systemd sd-bus (`libsystemd.so.0`, already
  installed) via ctypes — the /documented/ Unix-fd mechanism; see
  `dbus_sd_linux.py`. Zero new dependencies, MIT-style systemd LGPL
  system library, no vendoring.
- PipeWire client: **GStreamer `pipewiresrc`** (plugin libgstpipewire.so
  v1.6.2 installed here, MIT/X11, shipped by the PipeWire project) —
  the supported, maintained PipeWire client binding, driven by
  `gst-launch-1.0` child processes:
    * verification probe: one bounded pipeline
      `pipewiresrc fd=<remote> num-buffers=1 ! fakesink sync=false`
      — exit code 0 with EOS means AT LEAST ONE REAL MEDIA FRAME
      flowed (this is the independent media-flow proof; fakesink
      discards, nothing is written anywhere),
    * continuous stream: the same src without num-buffers while the
      user session is active — frames are continuously delivered and
      discarded (no persistence, bounded child memory).
- The fd handed to the child is the portal-provided OpenPipeWireRemote
  fd passed with subprocess pass_fds (never through text). Parent
  closes its copy after spawning; the fd never outlives use.

HONEST BOUNDARIES
- No receiver protocol: media goes to a discard sink on THIS machine;
  nothing is sent anywhere.
- No media persistence: no buffers are copied by Zara; only
  negotiated-caps metadata strings (resolution/format) are recorded.
- All waits are bounded; the child path is cancellable (SIGTERM then
  SIGKILL after a small grace) and teardown VERIFIES the child's exit.
- Missing pieces are reported with exact reasons, never papered over.
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from typing import Callable, Optional

PROBE_TIMEOUT_S = 8.0
_CHILD_TERM_S = 3.0
_RING_LINES = 96               # bounded stderr ring (lines)
_RING_LINE_MAX = 220           # chars per line

_GST_ARGS_PROBE = ["gst-launch-1.0", "-v", "pipewiresrc",
                   "fd={fd}", "stream-properties=props,media.type=Video,"
                   "media.class=Stream/Input/Video",
                   "num-buffers=1", "!", "fakesink", "sync=false"]
_GST_ARGS_STREAM = ["gst-launch-1.0", "-v", "pipewiresrc",
                    "fd={fd}", "stream-properties=props,media.type=Video,"
                    "media.class=Stream/Input/Video",
                    "!", "fakesink", "sync=false"]


_gst_cache: dict = {"checked": 0.0, "ok": False, "reason": "not checked"}
_GST_TTL_S = 60.0


def gst_available() -> tuple[bool, str]:
    """Cached (60 s) availability of gst-launch + the pipewiresrc
    element; cheap enough for status surfaces."""
    import shutil
    now = time.monotonic()
    if now - _gst_cache["checked"] < _GST_TTL_S:
        return _gst_cache["ok"], _gst_cache["reason"]
    if shutil.which("gst-launch-1.0") is None:
        out = (False, "gst-launch-1.0 is missing — prerequisite: "
               "gstreamer1.0-tools package")
    else:
        try:
            r = subprocess.run(["gst-inspect-1.0", "pipewiresrc"],
                               capture_output=True, timeout=10.0,
                               text=True)
            if r.returncode != 0 or "PipeWire source" not in r.stdout:
                out = (False, "pipewiresrc element is missing — "
                       "prerequisite: gstreamer1.0-pipewire package")
            else:
                out = (True, "")
        except (OSError, subprocess.TimeoutExpired) as e:
            out = (False, "gst-inspect-1.0 probe failed: %s" % e)
    _gst_cache.update({"checked": now, "ok": out[0], "reason": out[1]})
    return out


def backend_supports_fd(backend) -> tuple[bool, str]:
    """fd transport capability check — refuses a CLI bridge honestly."""
    if getattr(backend, "fd_capable", False):
        return True, ""
    return False, ("D-Bus backend cannot transport Unix file "
                   "descriptors (textual CLI bridge) — OpenPipeWireRemote "
                   "is only usable through a real fd-capable transport")


class MediaStream:
    """One live gst-launch child. Bounded stderr ring; verified
    termination; never persists media."""

    def __init__(self, proc: subprocess.Popen, fds: list) -> None:
        self._proc = proc
        self._fds = fds
        self._ring: deque = deque(maxlen=_RING_LINES)
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        proc = self._proc
        if proc.stderr is None:
            return
        for line in proc.stderr:
            with self._lock:
                self._ring.append(line.strip()[:_RING_LINE_MAX])

    def alive(self) -> bool:
        return self._proc.poll() is None

    def exit_code(self) -> Optional[int]:
        return self._proc.poll()

    def caps_line(self) -> str:
        with self._lock:
            for line in reversed(self._ring):
                if "video/x-raw" in line or "video/" in line:
                    return line
            return ""

    def describe(self) -> dict:
        with self._lock:
            ring = list(self._ring)[-8:]
        return {"alive": self.alive(), "exit_code": self.exit_code(),
                "caps": self.caps_line()[:200], "log_ring": ring}

    def terminate(self) -> dict:
        """Verified child shutdown: SIGTERM, grace, SIGKILL; report the
        actual exit."""
        proc = self._proc
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=_CHILD_TERM_S)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    return {"verified": False, "exit_code": None,
                            "error": "gst child did not exit within "
                                     "grace"}
        rc = proc.poll()
        self._reader.join(timeout=1.0)
        for fd in list(self._fds):
            try:
                import os
                os.close(fd)
            except OSError:
                pass
        self._fds = []
        # a verified stop means the child EXITED (rc observed);
        # rc==0 is a clean EOS, negative means the requested SIGTERM
        return {"verified": rc is not None, "exit_code": rc,
                "clean": rc == 0}


class GstPipewireMedia:
    """The real media gate used by the ScreenCast lifecycle."""

    def __init__(self, gst_ok: Optional[bool] = None,
                 gst_reason: str = "",
                 verify_timeout_s: float = PROBE_TIMEOUT_S) -> None:
        if gst_ok is None:
            ok, reason = gst_available()
            gst_ok, gst_reason = ok, reason or ""
        self._gst_ok = gst_ok
        self._gst_reason = gst_reason
        self._t_verify = verify_timeout_s
        self.probes: list = []
        self.spawned: list = []

    # ---- capability -----------------------------------------------------
    def available(self) -> tuple[bool, str]:
        if not self._gst_ok:
            return False, (self._gst_reason or "gst pipewiresrc "
                           "unavailable")
        return True, ""

    # ---- probe (frame-flow proof) ---------------------------------------
    def verify_frames(self, fd: int,
                      stop: Optional[threading.Event] = None,
                      target: Optional[int] = None) -> dict:
        """One bounded one-frame pipeline. Returns {"ok": frames>=1
        flowed, "frames":1, "caps": str, "error": ...}. target (the
        consented node id from the portal) is bound via pipewiresrc's
        numeric `path` property: it becomes pw_stream_connect's
        target_id — the exact deprecated-but-intended bind "the portal
        and some example apps only provide the object id"
        (pipewire 1.6.2 src/gst/gstpipewiresrc.c:1177). target-object
        is NOT used: it parses numeric strings as SERIALS and fails
        'target not found' on mutter session streams (live-verified)."""
        argv = _GST_ARGS_PROBE[:]
        argv[3] = "fd=%d" % fd
        if target is not None:
            argv.insert(4, "path=%s" % target)
        out = self._run_case(argv, fd, stop, self._t_verify)
        if out["outcome"] == "eos_ok":
            return {"ok": True, "frames": 1, "caps": out["caps"],
                    "error": ""}
        return {"ok": False, "frames": 0, "caps": out["caps"],
                "reason": out.get("detail", out["outcome"]),
                "ring": out.get("ring") or []}

    # ---- continuous stream ----------------------------------------------
    def start_stream(self, fd: int,
                     target: Optional[int] = None) -> tuple[
                             Optional[MediaStream], bool, str]:
        """Spawn the continuous discard-sink pipeline. The child keeps
        its inherited fd via pass_fds (the only descriptor leaked to
        the child); the parent's copy is closed right after spawn.
        target: consented node id — pinned via the numeric `path`
        property (see verify_frames for why not target-object)."""
        argv = _GST_ARGS_STREAM[:]
        argv[3] = "fd=%d" % fd
        if target is not None:
            argv.insert(4, "path=%s" % target)
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True, errors="replace",
                pass_fds=(fd,))
        except OSError as e:
            return None, False, "gst child spawn failed: %s" % e
        if proc.poll() is not None:
            return None, False, ("gst child exited immediately (rc %r)"
                                 % proc.poll())
        ms = MediaStream(proc, [])
        try:
            import os
            os.close(fd)
        except OSError:
            pass
        return ms, True, ""

    # ---- plumbing --------------------------------------------------------
    def _run_case(self, argv: list, fd: int, stop,
                  timeout_s: float) -> dict:
        ring: deque = deque(maxlen=_RING_LINES)
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True, errors="replace",
                pass_fds=(fd,))
        except OSError as e:
            return {"outcome": "spawn_failed", "caps": "",
                    "detail": str(e)}
        def pump() -> None:
            for line in proc.stderr or []:
                ring.append(line.strip()[:_RING_LINE_MAX])
        th = threading.Thread(target=pump, daemon=True)
        th.start()
        deadline = time.monotonic() + timeout_s
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if time.monotonic() > deadline:
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                finally:
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
                th.join(timeout=0.5)
                caps = next((l for l in reversed(ring)
                             if "video/x-raw" in l or "video/" in l), "")
                return {"outcome": "timeout", "caps": caps[:200],
                        "detail": "one-frame probe did not complete "
                                  "within %.0fs" % timeout_s,
                        "ring": [l[:_RING_LINE_MAX]
                                 for l in list(ring)[-16:]]}
            if stop is not None and stop.is_set():
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                th.join(timeout=0.5)
                return {"outcome": "cancelled", "caps": "", "detail":
                        "probe cancelled"}
            time.sleep(0.05)
        rc = proc.poll()
        th.join(timeout=1.0)
        caps = next((l for l in reversed(ring)
                     if "video/x-raw" in l or "video/" in l), "")
        # exit code remains the truth: 0 == EOS reached == at least
        # one buffer was prerolled through fakesink; -v stderr feeds
        # the bounded ring with the negotiation detail
        failed = rc != 0
        return {"outcome": "eos_ok" if not failed else "pipeline_failed",
                "caps": caps[:200], "exit_code": rc,
                "detail": "" if not failed else
                ("gst pipeline rc=%s: %s" % (rc, _tail(ring, 3))),
                # bounded stderr tail: diagnostics metadata (never
                # frame content) for exact failure-stage reporting
                "ring": [] if not failed else
                [l[:_RING_LINE_MAX] for l in list(ring)[-16:]]}


def _tail(ring: deque, n: int) -> str:
    return " | ".join(list(ring)[-n:])[:400]
