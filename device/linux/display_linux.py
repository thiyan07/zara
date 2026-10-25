"""Stage 21 — interactive-display operations (supported interfaces only).

Two supported channels, used read-only by default:

- Mutter DisplayConfig (``org.gnome.Mutter.DisplayConfig``) via the user
  session bus — the SAME interface discovery already uses.
- ``xrandr`` on X11 sessions (openly documented; not the GNOME "unsafe
  mode").

``describe_config`` and ``preview_config`` are read-only. ``apply_config``
refuses to run without ``confirm=True``, re-reads geometry to VERIFY the
requested layout, and reports the precise failure when the display or
protocol doesn't support something (a non-interactive HDMI panel cannot
become a touch panel — hardware limitation, reported honestly). GNOME
unsafe/debug modes are never used.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from .discovery_linux import _session_bus_env

# (test hook) command runner: argv -> stdout, None on absence/failure.
_RUN: Optional[Callable] = None

#: read directly (a kernel table, not a subprocess) — keeps test mocks
#: clean; tests patch this constant
_INPUT_DEVICES = "/proc/bus/input/devices"


def _run(argv: list, timeout: float = 8.0) -> Optional[str]:
    if _RUN is not None:
        return _RUN(argv, timeout)
    import subprocess
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout,
                           env=_session_bus_env() or None)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", "replace")


# ------------------------ current geometry (read-only) --------------

_XRANDR_ON = re.compile(
    r"^([\w\-\.]+) connected (?:primary )?(\d+)x(\d+)\+(\d+)\+(\d+)",
    re.M)
_XRANDR_PRIMARY = re.compile(r"^([\w\-\.]+) connected primary", re.M)
_XRANDR_OFF = re.compile(r"^([\w\-\.]+) disconnected", re.M)


def _xrandr_current() -> Optional[dict]:
    txt = _run(["xrandr", "--query"])
    if txt is None:
        return None
    monitors = []
    prim = _XRANDR_PRIMARY.search(txt)
    prim_name = prim.group(1) if prim else None
    for m in _XRANDR_ON.finditer(txt):
        name, w, h, x, y = m.groups()
        monitors.append({"name": name, "state": "on",
                         "w": int(w), "h": int(h),
                         "x": int(x), "y": int(y),
                         "primary": name == prim_name})
    if not monitors:
        return None
    return {"backend": "xrandr", "monitors": monitors}


def _mutter_current() -> Optional[dict]:
    """GetCurrentState via busctl --json (interface already aligned with
    discovery_linux). Actual shape (seen live on this laptop):
    [serial, heads(a(monitors)), logical a(iiduba(head,a{sv})), props]
    where each head = [(connector, vendor, model, serial), modes, props]
    and each mode = [name, w, h, refresh, scale, scales, flags] with
    the active mode flagged by an ``is-current`` GVariant."""
    import json as _json
    txt = _run(["busctl", "--user", "--json=short", "call",
                "org.gnome.Mutter.DisplayConfig",
                "/org/gnome/Mutter/DisplayConfig",
                "org.gnome.Mutter.DisplayConfig", "GetCurrentState"])
    if txt is None:
        return None
    try:
        data = _json.loads(txt)
        value = data.get("data")
        if not isinstance(value, list) or len(value) < 4:
            return None
        heads, logical = value[1], value[2]
    except Exception:
        return None

    def _flag(d, name):
        v = (d or {}).get(name)
        if isinstance(v, dict):
            return bool(v.get("data", True))
        return bool(v)

    by_conn: dict = {}
    for head in (heads or []):
        try:
            key = head[0]                    # (connector, vendor,
            modes = head[1]
            props = head[2] or {}
            for md in (modes or []):
                flags = md[6] if len(md) > 6 else {}
                if _flag(flags, "is-current"):
                    by_conn[key[0]] = {"w": int(md[1]), "h": int(md[2]),
                                       "refresh": md[3],
                                       "is_builtin":
                                       _flag(props, "is-builtin"),
                                       "display_name":
                                       (props.get("display-name") or {})
                                       .get("data", "")}
        except Exception:  # noqa: BLE001 — one odd head never kills all
            continue
    monitors = []
    for lm in (logical or []):
        try:
            # busctl json wraps the (ssss) head struct as a 1-element
            # list of a 4-list: [['eDP-1', 'CMN', ...]]
            head_ref = lm[5]
            if (isinstance(head_ref, list) and len(head_ref) == 1
                    and isinstance(head_ref[0], list)):
                name = head_ref[0][0]
            elif isinstance(head_ref, list) and head_ref and \
                    isinstance(head_ref[0], str):
                name = head_ref[0]
            else:
                name = str(head_ref)
            info = by_conn.get(name) or {}
            w, h = int(info.get("w") or 0), int(info.get("h") or 0)
            monitors.append({
                "name": name,
                "state": "on" if w else "off",
                "x": int(lm[0]), "y": int(lm[1]),
                "w": w, "h": h,
                "primary": bool(lm[4]),
                "refresh": info.get("refresh"),
                "is_builtin": bool(info.get("is_builtin")),
                "backend": "mutter"})
        except Exception:
            continue
    if not monitors:
        return None
    return {"backend": "mutter", "monitors": monitors}


def _live() -> Optional[dict]:
    return _mutter_current() or _xrandr_current()


def describe_config() -> dict:
    """Read-only current-layout report (single/extended/duplicated plus
    per-monitor geometry and honest capability notes)."""
    cur = _live()
    if cur is None:
        return {"available": False,
                "detail": "neither mutter DisplayConfig nor xrandr "
                          "answered (headless session?)",
                "supported_modes": []}
    on = [mm for mm in cur["monitors"] if mm["state"] == "on"]
    base = on[0]
    if len(on) == 1:
        layout = "single"
    elif all(mm["x"] == base["x"] and mm["y"] == base["y"]
             for mm in on[1:]):
        layout = "duplicated"
    else:
        layout = "extended"
    return {"available": True, "backend": cur["backend"],
            "layout_inferred": layout,
            "monitors": [{k: mm.get(k) for k in
                          ("name", "state", "x", "y", "w", "h",
                           "primary")} for mm in cur["monitors"]],
            "limited_actions": [
                "a panel gains touch only when the panel itself reports "
                "touch input; a mirrored laptop cannot gain input",
                "screen streaming/tab sharing is a separate supported "
                "protocol (portals/MediaProjection), not layout config",
                "ChangeBacklight is deprecated and not used"],
            "touch": _touch_inputs(),
            "streaming": stream_status(),
            "supported_modes": (["single", "extend_right", "duplicate"]
                                if on else [])}


def _ext_name(cur: dict) -> Optional[str]:
    ext = [mm for mm in cur["monitors"]
           if mm["state"] == "on" and not mm["name"].lower().startswith(
               ("edp", "lvds", "dsi"))]
    return ext[0]["name"] if ext else None


def _primary_name(cur: dict) -> Optional[str]:
    on = [mm for mm in cur["monitors"] if mm["state"] == "on"]
    prim = [mm for mm in on if mm.get("primary")]
    if prim:
        return prim[0]["name"]
    builtin = [mm for mm in on if mm["name"].lower().startswith(
        ("edp", "lvds", "dsi"))]
    return (builtin[0]["name"] if builtin else
            (on[0]["name"] if on else None))


# --------------------------- preview ---------------------------

def preview_config(mode: str) -> dict:
    """Compute the exact change WITHOUT running it."""
    if mode not in ("single", "extend_right", "duplicate"):
        return {"ok": False,
                "error": "mode must be single|extend_right|duplicate"}
    cur = _live()
    if cur is None:
        return {"ok": False, "error": "no reachable display backend"}
    prim, ext = _primary_name(cur), _ext_name(cur)
    n_on = sum(1 for m in cur["monitors"] if m["state"] == "on")
    if n_on < 1:
        return {"ok": False, "error": "no active display detected"}
    if mode in ("extend_right", "duplicate") and not ext:
        return {"ok": False,
                "error": "mode %s needs a second active display — %d "
                         "active" % (mode, n_on),
                "limitation": "hardware/connection state"}
    propose = {"single": {"prim": prim, "ext": None},
               "extend_right": {"prim": prim, "ext": ext,
                                "relative": "right-of"},
               "duplicate": {"prim": prim, "ext": ext,
                             "relative": "same-as"}}[mode]
    if cur["backend"] == "xrandr":
        argv = ["xrandr", "--output", prim, "--auto", "--primary"]
        if mode == "single":
            if ext:
                argv += ["--output", ext, "--off"]
        elif mode == "extend_right":
            argv += ["--output", ext, "--auto", "--right-of", prim]
        elif mode == "duplicate":
            argv += ["--output", ext, "--auto", "--same-as", prim]
    else:  # mutter: payload assembled from the live serial at apply
        argv = ["gdbus", "call", "--session", "--dest",
                "org.gnome.Mutter.DisplayConfig", "--object-path",
                "/org/gnome/Mutter/DisplayConfig", "--method",
                "org.gnome.Mutter.DisplayConfig.ApplyMonitorsConfig",
                "<payload from live GetCurrentState — see apply>"]
    return {"ok": True,
            "current_layout": describe_config().get("layout_inferred"),
            "proposes": propose, "command": argv,
            "no_changes_run": True,
            "note": "preview only — nothing was executed; apply needs "
                    "your explicit confirm"}


# ------------------------ apply (confirm-gated) ----------------------

_GOALS = {"single": "single", "extend_right": "extended",
          "duplicate": "duplicated"}


def apply_config(mode: str, confirm: bool = False) -> dict:
    """Apply ONE requested layout change. Requires confirm=True.
    Verification re-reads geometry; a change that did not land is
    reported as failed — never claimed as success."""
    if not confirm:
        return {"ok": False, "error": "explicit confirm=true required "
                "to change the desktop display layout",
                "verification": "geometry re-read after the call"}
    cur = _live()
    if cur is None:
        return {"ok": False, "error": "no reachable display backend"}
    if mode not in _GOALS:
        return {"ok": False, "error": "mode must be "
                "single|extend_right|duplicate"}
    prim, ext = _primary_name(cur), _ext_name(cur)
    before = describe_config().get("layout_inferred")
    if cur["backend"] == "xrandr":
        argv = ["xrandr", "--output", prim, "--auto", "--primary"]
        if mode == "single":
            if ext:
                argv = ["xrandr", "--output", ext, "--off"]
        elif not ext:
            return {"ok": False, "verified": False,
                    "error": "%s needs a second active display "
                             "(hardware limitation until one is "
                             "connected)" % mode,
                    "before": before}
        elif mode == "extend_right":
            argv += ["--output", ext, "--auto", "--right-of", prim]
        else:
            argv += ["--output", ext, "--auto", "--same-as", prim]
        out = _run(argv)
        if out is None:
            return {"ok": False, "verified": False,
                    "error": "backend refused the configuration call — "
                             "layout unchanged",
                    "attempted": " ".join(argv[:8]) + ("..." if
                                                       len(argv) > 8
                                                       else "")}
    else:
        # Mutter apply: designed, but not yet validated against a real
        # external display — refusing is more protective than guessing.
        return {"ok": False, "verified": False, "error":
                "Mutter ApplyMonitorsConfig path is designed but not "
                "validated on a real external display yet — refusing "
                "to apply; the X11 xrandr path is the supported path "
                "on this session",
                "before": before}
    descs = describe_config()
    after = descs.get("layout_inferred")
    goal = _GOALS[mode]
    verified = after == goal
    return {"ok": verified, "verified": verified,
            "before": before, "after": after,
            "monitors": descs.get("monitors"),
            "detail": None if verified else
            "requested %s but geometry reads %s — no success claim"
            % (goal, after)}


def registered_state_note() -> str:
    return ("layout change requires operator confirm and is verified "
            "by re-reading geometry; no unsafe GNOME mode is used")


# -------- Stage 22: touch inputs + screen-streaming availability ----

def _touch_inputs() -> dict:
    """Honest touch-reporting read directly from the kernel's own table
    (/proc/bus/input/devices). Touchscreens counted; a touchpad is NOT
    attributed to any external display. Attribution of a USB touch
    device to one display is INFERRED-only — reported, never claimed."""
    try:
        with open(_INPUT_DEVICES, encoding="utf-8",
                  errors="replace") as fh:
            txt = fh.read()
    except OSError:
        return {"detectable": False,
                "detail": "input device table unreadable — touch "
                          "capability unknown"}
    names = re.findall(r"^N: Name=\"([^\"]+)\"", txt, re.M)
    screens = [n for n in names if re.search(r"touch\s*screen|touchscreen",
                                             n, re.I)]
    pads = [n for n in names if re.search(r"touchpad", n, re.I)]
    pens = [n for n in names if re.search(r"\bstylus|\bpen\b|\bfinger",
                                          n, re.I)]
    return {"detectable": True,
            "touchscreens": screens, "touchpads": pads,
            "stylus_or_finger_class": pens,
            "external_touch_confirmed": False,
            "attribution": "inferred-only — a USB touch device cannot "
                           "be attributed to one display without a "
                           "protocol handshake; capability stays "
                           "unknown until then"}


def stream_status() -> dict:
    """Screen-casting availability probe (read-only). GNOME's supported
    capture path is the xdg-desktop-portal ScreenCast portal (which
    owns the user-consent dialog) + PipeWire; whether this session can
    actually create a session, and whether a receiver exists, cannot be
    claimed without a real consented session — so the answer is
    availability + exact limitation, and the stream operation stays
    unavailable."""
    listing = _run(["busctl", "--user", "list"])
    has_cast = bool(listing) and "org.gnome.Mutter.ScreenCast" in listing
    # all portals live under ONE bus name: org.freedesktop.portal.Desktop
    has_portal = bool(listing) and \
        "org.freedesktop.portal.Desktop" in listing
    receivers = _receiver_count()
    return {"available": True,
            "mutter_screencast_interface": has_cast,
            "portal_bus_present": has_portal,
            "known_receivers_reported": receivers,
            "supported_now": False,
            "limitation": "consent lives in the ScreenCast PORTAL "
                          "(present: %s); a receiver protocol stack "
                          "(AirPlay/GCast/RAOP mirroring: pairing + "
                          "stream framing) is NOT implemented — no "
                          "stream can be created, advertised "
                          "receivers stay unverified identities only"
                          % ("yes" if has_portal else "no"),
            "note": "files NEVER need this path — cross-device content "
                    "moves through Core relay missions; this is "
                    "screen-mirroring only"}


def _receiver_count() -> int:
    """Bounded receiver discovery (1.5 s, fixed receiver type list)
    for the honesty ledger only — never a trust grant or a stream."""
    try:
        if _NDISCOVER is None:
            from .netdisco_linux import discover as _nd
            fn = _nd
        else:
            fn = _NDISCOVER
        out = fn(timeout_=1.5,
                 services=["_airplay._tcp", "_raop._tcp",
                           "_googlecast._tcp", "_DIAL-service._tcp",
                           "_spotify-connect._tcp"])
        return len(out.get("advertisements", [])) \
            if out.get("available") else 0
    except Exception:  # noqa: BLE001 — probe must never break describe
        return 0


#: test hook replacing the real network discovery (see netdisco_linux)
_NDISCOVER: Optional[Callable] = None
