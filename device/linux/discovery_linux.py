"""Linux host discovery adapters (Stage 20).

One function, ``discover_snapshot()``, returns the PeripheralRegistry
item list for this host by reading ONLY supported, read-only OS
interfaces, each through a tolerant wrapper:

- Block/removable storage: ``lsblk -J`` — class ``usb_storage`` for
  removable media, with durabel identity from serial/uuid (never the
  device path alone).
- Phones over USB/ADB: ``adb devices`` — class ``android_adb`` only
  for physically connected, authorized adb devices.
- USB enumeration: ``lsusb`` — coarse class mapping (mass storage,
  HID, audio, video, communications/MTP phones) at detection level.
- Displays: X11 → ``xrandr --query``; GNOME Wayland → the SUPPORTED
  Mutter DisplayConfig D-Bus interface via ``busctl --json`` (not
  unsafe-mode; a real per-session interface). Connecting vs. stated
  resolution/mode distinctions are preserved.
- Audio: ``aplay -l`` / ``arecord -l`` ALSA-compat enumeration.

Missing tools, denied D-Bus, or absent sessions are honest absence:
the capability is reported in ``discovery_snapshot_meta`` as
unavailable with the reason. Nothing here enables unsafe settings or
writes anything. Bounded timeouts on every command.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

ADB = os.path.expanduser("~/Android/Sdk/platform-tools/adb")


def _run(argv: list, timeout: float = 6.0, env_ok: bool = True) -> str | None:
    """Bounded read-only helper. Returns stdout or None (absent/failed),
    so each adapter can honestly degrade."""
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout,
                           text=True, errors="replace", check=False)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _audio_cards() -> list[dict]:
    out: list[dict] = []
    for name, argv in (("output", ["aplay", "-l"]),
                       ("input", ["arecord", "-l"])):
        txt = _run(argv)
        if not txt:
            continue
        for m in re.finditer(
                r"card (\d+): ([^,]+), device (\d+): ([^\[\n]+)", txt):
            card = f"card{m.group(1)}"
            durable = f"audio-{card}-{m.group(3)}-{m.group(4).strip()[:24]}"
            out.append({
                "device_class": "audio",
                "label": f"{name} audio: {m.group(2).strip()}",
                "identity": {"durable": durable},
                "connection": "internal",
                "capabilities": {name: True},
                "mounts": [],
            })
    return out


def _session_bus_env() -> dict:
    """busctl --user needs DBUS_SESSION_BUS_ADDRESS; when this process
    was not launched from the desktop session, use the standard
    per-user bus path. Deterministic, read-only, no guessing."""
    import getpass
    env = dict(os.environ)
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        path = f"/run/user/{os.getuid()}/bus"
        if os.path.exists(path):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={path}"
    env.pop("DBUS_STARTER_BUS_TYPE", None)
    env.pop("DBUS_STARTER_ADDRESS", None)
    return env


def _run_env(argv: list, timeout: float = 6.0) -> str | None:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout,
                           text=True, errors="replace", check=False,
                           env=_session_bus_env())
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _displays(session: str) -> list[dict]:
    items: list[dict] = []
    if session != "wayland":
        txt = _run(["xrandr", "--query"])
        if txt:
            for m in re.finditer(
                    r"^([\w\-\.]+) connected (?:primary )?(\d+)x(\d+)"
                    r"((?:\+\d+\+\d+))?", txt, re.M):
                name, w, h, pos = m.group(1), m.group(2), m.group(3), \
                    m.group(4) or ""
                is_ext = not name.lower().startswith(("edp", "lvds", "dsi"))
                out = {
                    "device_class": "display",
                    "label": f"Display {name} ({w}x{h})",
                    "identity": {"durable": f"xrandr-{name}"},
                    "connection": "hdmi" if is_ext else "internal",
                    "capabilities": {
                        "resolution_detected": True,
                        "width": int(w), "height": int(h),
                        "extend_supported": True,   # xrandr extends reliably
                        "duplicate_supported": True,
                        "interactive_touch_reported": False,
                        "audio_route_supported": None,  # unknown: honest
                    },
                    "mounts": [],
                }
                out["capabilities"]["geometry_offset"] = pos
                out["capabilities"]["session"] = "x11"
                items.append(out)
        for m in re.finditer(r"^([\w\-\.]+) disconnected", txt or "", re.M):
            items.append({
                "device_class": "display",
                "label": f"Display {m.group(1)} (disconnected)",
                "identity": {"durable": f"xrandr-{m.group(1)}"},
                "connection": "hdmi", "capabilities": {},
                "mounts": [],
            })
        return items
    # Wayland: GNOME Mutter DisplayConfig (supported, no unsafe mode)
    txt = _run_env(["busctl", "--user", "--json=short", "call",
                "org.gnome.Mutter.DisplayConfig",
                "/org/gnome/Mutter/DisplayConfig",
                "org.gnome.Mutter.DisplayConfig", "GetCurrentState"])
    if not txt:
        return []  # honest absence: interface unavailable/denied
    try:
        body = json.loads(txt)
        val = body.get("data") or []
        monitors = val[1] if len(val) > 1 else []
        for mon in monitors:
            if not (isinstance(mon, list) and len(mon) >= 2):
                continue
            conn = mon[0] if isinstance(mon[0], list) else None
            name = (conn[0] if isinstance(conn, list) and len(conn) > 0
                    else "") or f"c{len(items)}"
            maker = (conn[1][:3] if isinstance(conn, list)
                     and len(conn) > 1 else "")
            modes = mon[1] if isinstance(mon[1], list) else []
            width = height = 0
            current = preferred = ""
            res_re = re.compile(r"^(\d+)x(\d+)")
            for md in modes:
                if not (isinstance(md, (list, tuple)) and len(md) >= 3
                        and isinstance(md[0], str)):
                    continue
                m = res_re.match(md[0])
                if not m:
                    continue
                w, h = int(m.group(1)), int(m.group(2))
                flags = md[-1] if isinstance(md[-1], dict) else {}
                def _flag(k):
                    v = (flags.get(k) or {}).get("data")
                    return bool(v)
                if _flag("is-current"):
                    current, width, height = md[0], w, h
                elif _flag("is-preferred") and not preferred:
                    preferred, width, height = md[0], w, h
            if not width:
                continue
            is_builtin = name.lower().startswith(("edp", "lvds", "dsi"))
            items.append({
                "device_class": "display",
                "label": f"Display {name} ({width}x{height})"
                         + (f" ({maker})" if maker else ""),
                "identity": {"durable": f"mutter-{name}"},
                "connection": "internal" if is_builtin else "hdmi",
                "capabilities": {
                    "resolution_detected": True,
                    "width": width, "height": height,
                    # GNOME extend/duplicate run through display
                    # Settings; direct programmatic control needs a
                    # real protocol — reported, not claimed.
                    "session": "wayland",
                    "current_mode": current or None,
                    "preferred_mode": preferred or None,
                },
                "mounts": [],
            })
    except Exception:  # noqa: BLE001
        return []
    return items


def _int_or(s) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _lsusb_items() -> list[dict]:
    txt = _run(["lsusb"])
    if not txt:
        return []
    items = []
    for line in txt.splitlines():
        m = re.match(r"Bus \d+ Device \d+: ID ([0-9a-f]{4}:[0-9a-f]{4}) "
                     r"(.*)", line)
        if not m:
            continue
        vidpid, label = m.group(1), m.group(2).strip()
        low = label.lower()
        if "root hub" in low:
            continue  # host bus controllers are not peripherals
        if any(k in low for k in ("webcam", "camera")):
            dclass = "camera"
        elif any(k in low for k in ("keyboard", "mouse")):
            dclass = "hid"
        elif any(k in low for k in ("headset", "speaker", "microphone")):
            dclass = "audio"
        else:
            dclass = "otherusb"
        items.append({
            "device_class": dclass,
            "label": label[:64] or dclass,
            "identity": {"durable": f"usb-{vidpid}"},
            "connection": "usb", "capabilities": {}, "mounts": [],
        })
    return items
USB_CLASSES = {"mass": "usb_storage", "storage": "usb_storage",
               "human interface": "hid", "audio": "audio"}
MASS_STORAGE_HINTS = ("mass storage", "storage", "mtp", "card reader")


def _block_items() -> list[dict]:
    txt = _run(["lsblk", "-J", "-b", "-o", "NAME,RM,RO,SIZE,TYPE,"
                "MOUNTPOINTS,MOUNTPOINT,FSTYPE,SERIAL,MODEL,VENDOR,UUID"])
    if not txt:
        return []
    try:
        data = (json.loads(txt).get("blockdevices") or [])
    except ValueError:
        return []
    items = []

    def walk(nodes):
        for n in nodes:
            name = n.get("name", "")
            if n.get("type") == "disk" and name and not re.match(
                    r"(^sr\d$|^zram\d$|^md\d+$)", name):
                size = int(n.get("size") or 0)
                rm = bool(n.get("rm"))
                model = (n.get("model") or "").strip()
                serial = (n.get("serial") or "").strip()
                uuidv = (n.get("uuid") or "").strip()
                fsv = (n.get("fstype") or "").strip()
                # child mounts (partitions)
                mounts = []
                for m in (n.get("mountpoints") or []):
                    if isinstance(m, list):
                        mounts += [x for x in m if x]
                if n.get("mountpoint"):
                    mounts.append(n["mountpoint"])
                for child in (n.get("children") or []):
                    for m in (child.get("mountpoints") or []):
                        if isinstance(m, list):
                            mounts += [x for x in m if x]
                    if child.get("mountpoint"):
                        mounts.append(child["mountpoint"])
                mounts = sorted(set(m or "" for m in mounts
                                    if m and m != "[SWAP]"))
                ro = bool(n.get("ro"))
                if rm:
                    dclass = "usb_storage"
                elif fsv and ro:
                    dclass = "removable_disk"
                else:
                    # walk subnodes only for continuing; internal disk
                    # is NOT a connected peripheral — skip, but keep
                    # walking children (e.g. MD on an external bay).
                    walk(n.get("children") or [])
                    continue
                items.append({
                    "device_class": dclass,
                    "label": " ".join(x for x in (model, _fmt_size(size))
                                     if x) or f"Block {name}",
                    "identity": {"durable": serial or uuidv or name},
                    "connection": "usb" if rm else "ata",
                    "capabilities": {
                        "size_bytes": size, "filesystem": fsv or "unknown",
                        "read": True, "write": not ro,
                        "mountpoints": mounts, "device_node": name,
                    },
                    "mounts": [{"mountpoint": m, "fs": fsv}
                               for m in mounts],
                })
            if n.get("type") == "disk" and name and re.match(
                    r"(^sr\d$)", name):
                items.append({
                    "device_class": "removable_disk",
                    "label": f"Optical {name}",
                    "identity": {"durable": f"optical-{name}"},
                    "connection": "ata",
                    "capabilities": {
                        "filesystem": n.get("fstype") or "unknown",
                        "read": True, "write": bool(n.get("rm")),
                    },
                    "mounts": [],
                })
            walk(n.get("children") or [])

    walk(data)
    return items


def _fmt_size(b: int) -> str:
    for unit, div in (("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if b >= div:
            return f"{b / div:.1f}{unit}"
    return f"{b}B"


def _adb_phones() -> list[dict]:
    exe = shutil.which("adb") or (ADB if os.path.exists(ADB) else None)
    if not exe:
        return []
    txt = _run([exe, "devices", "-l"], timeout=5)
    if not txt:
        return []
    items = []
    for line in txt.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or not line.strip():
            continue
        serial, status = parts[0], parts[1]
        if status == "unauthorized":
            items.append({
                "device_class": "android_adb",
                "label": "Android device (unauthorized)",
                "identity": {"durable": f"adb-{serial}"},
                "connection": "usb",
                "capabilities": {"authorized": False},
                "mounts": [],
            })
        elif status == "device":
            model = ""
            for kv in parts[2:]:
                if kv.startswith("model:"):
                    model = kv.split(":", 1)[1]
            items.append({
                "device_class": "android_adb",
                "label": f"Android device {model}".strip(),
                "identity": {"durable": f"adb-{serial}"},
                "connection": "usb",
                "capabilities": {
                    "authorized": True,
                    "battery": True, "apps": True, "filesystem": True,
                },
                "mounts": [],
            })
    return items


def discover_snapshot(session: str = "wayland") -> tuple[list[dict], dict]:
    """One bounded, read-only host discovery pass.

    Returns (items, meta). meta lists which adapter ran / was absent
    with reasons, so capability gaps are visible and honest.
    """
    meta = {"session": session, "adapters": {}}
    if shutil.which("lsblk"):
        meta["adapters"]["block"] = "ok"
    else:
        meta["adapters"]["block"] = "lsblk absent — removable storage "\
            "undiscoverable until installed"
    items = _block_items() + _lsusb_items()
    meta["adapters"]["usb"] = "ok" if shutil.which("lsusb") else \
        "lsusb absent"
    phones = _adb_phones()
    meta["adapters"]["adb"] = "ok" if phones else \
        ("adb absent" if not (shutil.which("adb") or os.path.exists(ADB))
         else "no authorized device")
    items += phones
    bt, bt_meta = _bluetooth_items()
    meta["adapters"]["bluetooth"] = bt_meta
    items += bt
    mtp, mtp_meta = _gio_mtp_items()
    meta["adapters"]["mtp"] = mtp_meta
    items += mtp
    net_meta = _network_services_meta()
    meta["adapters"]["network_services"] = net_meta
    disp = _displays(session)
    meta["adapters"]["displays"] = ("ok (xrandr)" if session != "wayland"
                                    else ("ok (Mutter DisplayConfig)"
                                          if disp else
                                          "Mutter DisplayConfig absent/"
                                          "denied"))
    items += disp
    audio = _audio_cards()
    meta["adapters"]["audio"] = "ok" if audio else "no audio capture/play"
    items += audio
    return items, meta


# ---------------- Stage 22: bluetooth / MTP / network services -------

#: Well-known BlueZ 16-bit service UUIDs -> protocol/service name. These
#: are FACTS from the device's own advertised record (SDP), used only to
#: describe what the device CLAIMS; nothing here implies a successful
#: protocol handshake or a permission to use a service.
_BT_SERVICES = {
    "0000110a": "audio-source (device can stream audio out)",
    "0000110b": "audio-sink (device can receive audio)",
    "0000110d": "a2dp",
    "0000110c": "avrcp-target",
    "0000110e": "avrcp-controller",
    "00001105": "obex-object-push (file push protocol)",
    "00001112": "headset",
    "0000111f": "handsfree-ag (phone-side HFP)",
    "0000111e": "handsfree-hf",
    "00001115": "panu",
    "00001116": "nap",
    "0000112f": "pbap (phonebook access)",
    "00001132": "map (message access)",
    "00001200": "pnp-info",
    "00001800": "gap",
    "00001801": "gatt",
}

_MAC = re.compile(r"^Device ([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\s+(.+)$")


def _bluetooth_items(env_ok: bool = True) -> tuple[list[dict], str]:
    """Registered BlueZ devices + their advertised services. Strictly
    read-only: listing and info only — never pair/connect/scan here."""
    if not shutil.which("bluetoothctl"):
        return [], "bluetoothctl absent — bluetooth undiscoverable "\
                   "until BlueZ tools installed"
    listing = _run_env(["bluetoothctl", "devices"], timeout=6.0)
    if listing is None:
        return [], "bluetoothctl refused (BlueZ down or no controller?)"
    out: list[dict] = []
    for m in _MAC.finditer(listing):
        mac, name = m.group(1).upper(), m.group(2).strip()
        info = _run_env(["bluetoothctl", "info", mac], timeout=6.0) or ""

        def _yes(k):
            return bool(re.search(rf"^\s*{k}: yes\s*$", info, re.M))

        services, unknown = [], 0
        for um in re.finditer(r"^\s*UUID:\s*(.+?)\s*\(([0-9a-fA-F]{8}-"
                              r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                              r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\)\s*$",
                              info, re.M):
            svc = _BT_SERVICES.get(um.group(2)[:8])
            if svc:
                services.append(svc)
            else:
                unknown += 1
        out.append({
            "device_class": "bluetooth",
            "label": f"Bluetooth: {name}",
            "identity": {"durable": f"bt-{mac}"},
            "connection": "bluetooth",
            "capabilities": {
                "paired": _yes("Paired"),
                "trusted": _yes("Trusted"),
                "connected": _yes("Connected"),
                "blocked": _yes("Blocked"),
                "advertised_services": services,
                "unidentified_service_uuids": unknown,
                "service_use_inferred_only": True,
                "note": "services come from the device's own advertised "
                        "record; nothing here implies a successful "
                        "handshake or permission to use them",
            },
            "source": "bluetoothctl",
        })
    return out, ("ok" if out else "controller reachable, no registered "
                                 "devices")


_MTP_MONITOR = re.compile(r"VolumeMonitor(MTP|PTP|GPhoto2)", re.I)


def _gio_mtp_items() -> tuple[list[dict], str]:
    """MTP/PTP phones connected over USB as seen by the desktop's GVFS
    layer (`gio mount -li`). Presence of a gvfs mount is the CONFIRMED
    signal that the OS exposes file-level access; nothing is guessed
    from USB ids alone."""
    if not shutil.which("gio"):
        return [], "gio (GVFS) absent — MTP/PTP classification "\
                   "unavailable on this host"
    txt = _run_env(["gio", "mount", "-li"], timeout=8.0)
    if txt is None:
        return [], "gio mount failed (gvfs unavailable?)"
    out: list[dict] = []
    blocks = re.split(r"(?=^\s*Mount\()", txt, flags=re.M)
    for b in blocks:
        if not b.lstrip().startswith("Mount("):
            continue
        name_line = b.splitlines()[0].strip()
        label = re.sub(r"^Mount\(\d+\):\s*", "", name_line).strip()
        typ = re.search(r"^\s*Type:\s*(\S+)\s*\(([^)]*)\)", b, re.M)
        mon = typ.group(2) if typ else ""
        ids = re.findall(r"^\s*(?:Id|Default location):\s*(\S+)", b, re.M)
        uri = next((i for i in ids if i.startswith(("mtp://", "mtp%3A",
                                                    "gphoto2://",
                                                    "gphoto2%3A"))), None)
        mon_hit = _MTP_MONITOR.search(mon)
        if not uri and not mon_hit:
            continue  # an ordinary local/UDisks mount — not MTP/PTP
        proto = ("mtp" if (mon_hit and "mtp" in mon_hit.group(1).lower())
                 or (uri or "").lower().startswith("mtp")
                 else "ptp" if mon_hit or (uri or "").startswith("gphoto")
                 else None)
        if proto is None:
            continue
        out.append({
            "device_class": "mtp",
            "label": f"MTP/PTP mount: {label}",
            "identity": {"durable": uri or f"gvfs-{proto}-{label}"},
            "connection": "usb",
            "capabilities": {
                "protocol_confirmed": proto,   # from the OS's own identifier
                "io_backend": "gvfs file-level access (OS-managed)",
                "note": "file transfer over this mount goes through the "
                        "OS's gvfs layer and its permissions; Core relay "
                        "missions remain the cross-device path for "
                        "content TrustLevel 'trusted' sources",
            },
            "mounts": [],
            "source": "gio",
        })
    return out, ("ok" if out else
                 "gio available; no MTP/PTP mounts mounted right now")


def _network_services_meta() -> str:
    """Network-device discovery honestly reports the supported scanner
    state — Core NEVER port-scans or probes beyond mDNS boundaries."""
    if shutil.which("avahi-browse"):
        return "avahi-browse available (mDNS; scan runs on demand)"
    if shutil.which("dns-sd") or os.path.exists("/usr/bin/mdns-scan"):
        return "mdns scanner binary present"
    return ("mDNS scanner absent (avahi-tools not installed) — LAN "
            "device discovery unavailable; Core deliberately does not "
            "port-scan")

