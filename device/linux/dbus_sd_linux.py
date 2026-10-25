"""Stage 23 — real Unix-fd D-Bus transport via systemd's sd-bus.

WHY THIS MODULE EXISTS (stage-22c blocker, resolved):
`OpenPipeWireRemote` returns a `h` (Unix fd) reply — a fact confirmed
live against the real portal (interface v5 on this host). A CLI bridge
(`busctl call` textual output) fundamentally cannot transport a live
file descriptor across process boundaries, so the consent-driven
lifecycle stopped right before media. This module adds the missing,
DOCUMENTED transport using the sd-bus C API shipped in
`libsystemd.so.0` (already present on this host — zero new
dependencies), driven through `ctypes`:

- one persistent user-session-bus connection per process (portal
  sessions are owned by the CALLING connection; a transient
  connection would close the session on disconnect),
- full `a{sv}` marshalling through the documented
  open/append/close container sequence (no invented formats),
- reply reading for `o` (object path), `s` (string), `u` (uint),
  and `h`: `sd_bus_message_read_basic(..., 'h', ...)` dups the fd
  into OUR process — the real fd-passing mechanism,
- errors surface the real D-Bus error name; nothing is invented.

Boundaries:
- No magazine of variants: only the types the portal API needs
  (o/s/u/b/h). Anything else is refused, not guessed.
- The fd we receive belongs to the caller of `call_fd`; the receiver
  must close it (portal spec: closed with the message's fd list;
  sd-bus dup semantics mean WE own it).
- Thread-safety: one connection guarded by a lock; calls serialize.
- No second execution engine: this is transport plumbing used by the
  existing ScreenCast lifecycle through the same backend interface.
"""
from __future__ import annotations

import ctypes
import threading
from typing import Optional

SD_BUS_TYPE = {"s": 0x73, "o": 0x6F, "u": 0x75, "b": 0x62, "h": 0x68}


class _SdBusError(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p),
                ("message", ctypes.c_char_p),
                ("_need_free", ctypes.c_int)]


def _load_lib():
    lib = ctypes.CDLL("libsystemd.so.0")
    lib.sd_bus_open_user.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.sd_bus_open_user.restype = ctypes.c_int
    lib.sd_bus_message_new_method_call.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
        ctypes.c_char_p]
    lib.sd_bus_message_new_method_call.restype = ctypes.c_int
    lib.sd_bus_message_append_basic.argtypes = [
        ctypes.c_void_p, ctypes.c_char, ctypes.c_void_p]
    lib.sd_bus_message_append_basic.restype = ctypes.c_int
    lib.sd_bus_message_open_container.argtypes = [
        ctypes.c_void_p, ctypes.c_char, ctypes.c_char_p]
    lib.sd_bus_message_open_container.restype = ctypes.c_int
    lib.sd_bus_message_close_container.argtypes = [
        ctypes.c_void_p]
    lib.sd_bus_message_close_container.restype = ctypes.c_int
    lib.sd_bus_message_read_basic.argtypes = [
        ctypes.c_void_p, ctypes.c_char, ctypes.c_void_p]
    lib.sd_bus_message_read_basic.restype = ctypes.c_int
    lib.sd_bus_message_get_signature.argtypes = [
        ctypes.c_void_p, ctypes.c_int]
    lib.sd_bus_message_get_signature.restype = ctypes.c_char_p
    lib.sd_bus_call.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64,
        ctypes.POINTER(_SdBusError), ctypes.POINTER(ctypes.c_void_p)]
    lib.sd_bus_call.restype = ctypes.c_int
    lib.sd_bus_message_unref.argtypes = [ctypes.c_void_p]
    lib.sd_bus_message_unref.restype = ctypes.c_void_p
    lib.sd_bus_unref.argtypes = [ctypes.c_void_p]
    lib.sd_bus_unref.restype = ctypes.c_void_p
    lib.sd_bus_error_free.argtypes = [ctypes.POINTER(_SdBusError)]
    lib.sd_bus_error_free.restype = None
    lib.sd_bus_flush_close_unref = getattr(
        lib, "sd_bus_flush_close_unref", lib.sd_bus_unref)
    lib.sd_bus_flush_close_unref.argtypes = [ctypes.c_void_p]
    return lib


_LIB = None
_LIB_LOCK = threading.Lock()


def _lib():
    global _LIB
    with _LIB_LOCK:
        if _LIB is None:
            _LIB = _load_lib()
    return _LIB


def build_a_sv(msg, entries, lib):
    """Marshal a{sv} through the documented container sequence.
    entries: list of (key:str, typechar:str, value); only s/o/u/b/h."""
    r = lib.sd_bus_message_open_container(msg, ord("a"), b"{sv}")
    if r < 0:
        return r
    for key, tchar, value in entries:
        if tchar not in SD_BUS_TYPE:
            return -95
        # dict-entry container type is SD_BUS_TYPE_DICT_ENTRY ('e');
        # '{' is only the SIGNATURE character (verified: ord('{') → EINVAL)
        r = lib.sd_bus_message_open_container(msg, ord("e"), b"sv")
        if r < 0:
            return r
        kb = key.encode()
        r = lib.sd_bus_message_append_basic(msg, ord("s"), _p_str(kb))
        if r < 0:
            return r
        vb = tchar.encode()
        r = lib.sd_bus_message_open_container(msg, ord("v"), vb)
        if r < 0:
            return r
        p = _p_value(tchar, value)
        if p is None:
            return -95
        r = lib.sd_bus_message_append_basic(msg, ord(tchar), p)
        if r < 0:
            return r
        r = lib.sd_bus_message_close_container(msg)   # closes 'v'
        if r < 0:
            return r
        r = lib.sd_bus_message_close_container(msg)   # closes '{sv}'
        if r < 0:
            return r
    return lib.sd_bus_message_close_container(msg)    # closes 'a'


def _p_str(b: bytes):
    # sd-bus copies strings during append; p IS the string pointer
    holder = ctypes.c_char_p(b)
    return ctypes.cast(holder, ctypes.c_void_p)


def _p_value(tchar: str, value):
    if tchar in ("s", "o"):
        holder = ctypes.c_char_p(value.encode()
                                 if isinstance(value, str) else value)
        return ctypes.cast(holder, ctypes.c_void_p)
    if tchar == "u":
        holder = ctypes.c_uint32(int(value))
        return ctypes.cast(ctypes.pointer(holder), ctypes.c_void_p)
    if tchar == "b":
        holder = ctypes.c_ubyte(1 if value else 0)
        return ctypes.cast(ctypes.pointer(holder), ctypes.c_void_p)
    if tchar == "h":
        holder = ctypes.c_int(int(value))
        return ctypes.cast(ctypes.pointer(holder), ctypes.c_void_p)
    return None


class SdBusUserSession:
    """Persistent ctypes sd-bus connection on the user session bus."""

    def __init__(self, lib=None) -> None:
        self._lib = lib if lib is not None else _lib()
        self._bus: Optional[ctypes.c_void_p] = None
        self._lock = threading.Lock()

    # ---- connection management --------------------------------------
    def _bus_or_none(self) -> Optional[ctypes.c_void_p]:
        with self._lock:
            if self._bus is None:
                bus = ctypes.c_void_p()
                if self._lib.sd_bus_open_user(
                        ctypes.byref(bus)) < 0 or not bus.value:
                    return None
                self._bus = bus
            return self._bus

    def close(self) -> None:
        with self._lock:
            if self._bus is not None:
                self._lib.sd_bus_flush_close_unref(self._bus)
                self._bus = None

    # ---- marshalling --------------------------------------------------
    def _new_call(self, bus, dest: str, path: str, iface: str,
                  member: str):
        m = ctypes.c_void_p()
        r = self._lib.sd_bus_message_new_method_call(
            bus, ctypes.byref(m),
            dest.encode(), path.encode(), iface.encode(),
            member.encode())
        if r < 0 or not m.value:
            return None
        return m

    def _marshal_body(self, msg, items: list) -> int:
        """items: list of ("o"|"s", value) or ("a{sv}", entries)."""
        for kind, payload in items:
            if kind == "a{sv}":
                r = build_a_sv(msg, payload, self._lib)
            elif kind in ("o", "s"):
                p = _p_value(kind, payload)
                r = -95 if p is None else \
                    self._lib.sd_bus_message_append_basic(
                        msg, ord(kind), p)
            else:
                return -95
            if r < 0:
                return r
        return 0

    # ---- calls ----------------------------------------------------------
    def call(self, dest: str, path: str, iface: str, member: str,
             items: Optional[list] = None,
             timeout_s: float = 5.0) -> dict:
        """Documented call. Returns {"ok", "sig", "value"|"fd"|"error"}:
        - 'o'/'s' replies: value = str; 'u' replies: value = int;
        - 'h' replies: fd = live dup'd fd owned by the CALLER;
        - failures: error = real D-Bus error name (or local reason)."""
        items = items or []
        bus = self._bus_or_none()
        if bus is None:
            return {"ok": False, "error": "user session bus "
                    "unavailable via sd-bus"}
        msg = self._new_call(bus, dest, path, iface, member)
        if msg is None:
            return {"ok": False, "error": "method-call message "
                    "allocation failed"}
        try:
            r = self._marshal_body(msg, items)
            if r < 0:
                return {"ok": False, "error": "argument marshalling "
                        "refused (unsupported type in %r)" % (items,)}
            err = _SdBusError()
            reply = ctypes.c_void_p()
            with self._lock:
                rc = self._lib.sd_bus_call(
                    bus, msg, int(timeout_s * 1_000_000),
                    ctypes.byref(err), ctypes.byref(reply))
            if rc < 0:
                name = err.name or b""
                desc = err.message or b""
                self._lib.sd_bus_error_free(ctypes.byref(err))
                return {"ok": False, "error":
                        (name or ("local errno %d" % -rc)).decode(
                            errors="replace"),
                        "detail": desc.decode(errors="replace")}
            self._lib.sd_bus_error_free(ctypes.byref(err))
            try:
                if not reply.value:
                    return {"ok": False, "error": "empty reply"}
                sig = self._lib.sd_bus_message_get_signature(
                    reply, 1) or b""
                sig_s = sig.decode(errors="replace")
                if sig_s == "":
                    # documented no-body replies (e.g. Session.Close)
                    return {"ok": True, "sig": "", "value": None}
                if sig_s.startswith("o"):
                    p = ctypes.c_char_p()
                    if self._lib.sd_bus_message_read_basic(
                            reply, ord("o"), ctypes.byref(p)) < 0:
                        return {"ok": False, "error": "object-path "
                                "reply unreadable"}
                    return {"ok": True, "sig": sig_s,
                            "value": p.value.decode(errors="replace")}
                if sig_s.startswith("s"):
                    p = ctypes.c_char_p()
                    if self._lib.sd_bus_message_read_basic(
                            reply, ord("s"), ctypes.byref(p)) < 0:
                        return {"ok": False, "error": "string reply "
                                "unreadable"}
                    return {"ok": True, "sig": sig_s,
                            "value": p.value.decode(errors="replace")}
                if sig_s.startswith("u"):
                    out = ctypes.c_uint32()
                    if self._lib.sd_bus_message_read_basic(
                            reply, ord("u"), ctypes.byref(out)) < 0:
                        return {"ok": False, "error": "uint reply "
                                "unreadable"}
                    return {"ok": True, "sig": sig_s, "value": out.value}
                if sig_s.startswith("h"):
                    out = ctypes.c_int()
                    if self._lib.sd_bus_message_read_basic(
                            reply, ord("h"), ctypes.byref(out)) < 0:
                        return {"ok": False, "error": "fd reply "
                                "unreadable"}
                    # documented: read 'h' does NOT dup — the fd stays in
                    # the message's possession; dup it so we own it after
                    # the message is unref'd
                    import fcntl
                    import os
                    try:
                        dup_fd = fcntl.fcntl(out.value, fcntl.F_DUPFD_CLOEXEC,
                                             3)
                    except OSError as e:
                        return {"ok": False, "error": "fd duplication "
                                "failed: %s" % e}
                    return {"ok": True, "sig": sig_s, "fd": dup_fd}
                return {"ok": False, "error": "reply signature %r is "
                        "not handled" % sig_s}
            finally:
                if reply.value:
                    self._lib.sd_bus_message_unref(reply)
        finally:
            self._lib.sd_bus_message_unref(msg)

    def object_exists(self, dest: str, path: str) -> Optional[bool]:
        """Real absence probe. GDBus introspection returns an empty
        node for ANY sub-path of the portal (verified live), so absence
        is detected with a documented Properties.GetAll on the Session
        interface: a GONE object raises UnknownMethod with detail
        'Object does not exist at path …'; a LIVE object without that
        interface raises InvalidArgs 'No such interface'."""
        r = self.call(dest, path, "org.freedesktop.DBus.Properties",
                      "GetAll", [("s", "org.freedesktop.portal.Session")],
                      timeout_s=4.0)
        if r["ok"]:
            return True
        name = r.get("error", "")
        detail = r.get("detail", "")
        if name == "org.freedesktop.DBus.Error.InvalidArgs" or \
                "No such interface" in detail:
            return True
        if "does not exist" in detail or \
                name in ("org.freedesktop.DBus.Error.UnknownObject",
                         "org.freedesktop.DBus.Error.NoSuchObject"):
            return False
        return None      # transport/local problems: cannot tell
