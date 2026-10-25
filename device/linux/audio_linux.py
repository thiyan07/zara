"""Stage 22 — audio output routing via PipeWire's wpctl (the OS's own
supported interface).

``audio_status()`` is read-only: devices, sinks, sources and the
current default are parsed from ``wpctl status`` — nothing changes.

``set_output()`` routes the default audio output to a CONFIRMED sink
id. It refuses without ``confirm=True`` (a routing change is audible
and state-changing), refuses unknown sink ids, runs
``wpctl set-default <id>``, and VERIFIES by re-reading the status — a
routing that did not land is reported as failed, never success.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from .discovery_linux import _session_bus_env

_RUN: Optional[Callable] = None


def _run(argv: list, timeout: float = 6.0) -> Optional[str]:
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

#: section headers inside the tree: ├─ Sinks: / ├─ Devices: ...
_SECTION = re.compile(r"^\s*├─ (Devices|Sinks|Sources|Streams|Filters):")
#: top-level group headers (Audio / Video / Settings) end a section
_GROUP = re.compile(r"^\s*(Audio|Video|Settings)\s*$")
#: an item line: │  *   57. Name [vol: 0.63]  OR  │      53. Name [alsa]
_ITEM = re.compile(r"^\s*(?:[│|]\s*)?(?:\*\s*)?(\d+)\.\s+([^\[]+?)\s*"
                   r"(?:\[([^\]]*)\])?\s*$")

_SECTIONS = {"Devices": "devices", "Sinks": "sinks", "Sources":
             "sources", "Streams": "streams", "Filters": None}


def _status() -> Optional[dict]:
    txt = _run(["wpctl", "status"])
    if txt is None:
        return None
    section = None
    out: dict[str, list] = {"devices": [], "sinks": [], "sources": [],
                            "streams": []}
    for line in txt.splitlines():
        if _GROUP.match(line):
            section = None
            continue
        sm = _SECTION.match(line)
        if sm:
            section = _SECTIONS[sm.group(1)]
            continue
        if section is None:
            continue
        im = _ITEM.match(line)
        if not im:
            continue
        pid, name, tag = int(im.group(1)), im.group(2).strip(), \
            im.group(3)
        star = bool(re.search(r"^\s*│?\s*\*", line))
        row = {"id": pid, "name": name.rstrip(), "default": star}
        if tag in ("alsa", "bluez5", "v4l2", "api.alsa"):
            row["backend"] = tag
        if name.startswith(("alsa_input", "alsa_output")) or \
                ".analog-stereo" in name or ".hdmi" in name:
            row["port"] = name
        out[section].append(row)
    if not (out["sinks"] or out["devices"]):
        return None
    return out


def audio_status() -> dict:
    cur = _status()
    if cur is None:
        return {"available": False,
                "detail": "wpctl (PipeWire) unavailable or refused — "
                          "no routing data",
                "sinks": []}
    default = [s for s in cur["sinks"] if s["default"]]
    return {"available": True,
            "devices": cur["devices"], "sinks": cur["sinks"],
            "sources": cur["sources"], "streams": cur["streams"],
            "default_sink": default[0] if default else None,
            "note": "sinks are potential outputs; routing one as the "
                    "default is a confirm-gated operation"}


def set_output(sink_id: int, confirm: bool = False) -> dict:
    before = audio_status()
    if not confirm:
        return {"ok": False, "error": "explicit confirm=true required "
                "to change the default audio output",
                "verification": "default marker re-read after wpctl "
                                "set-default"}
    if before.get("available") is not True or not before.get("sinks"):
        return {"ok": False, "error": "no sinks visible — cannot route "
                f"sink id {sink_id}", "nothing_changed": True}
    ids = [s["id"] for s in before["sinks"]]
    if sink_id not in ids:
        return {"ok": False, "error": f"unknown sink id {sink_id} — "
                f"visible ids: {ids}", "nothing_changed": True}
    out = _run(["wpctl", "set-default", str(sink_id)])
    if out is None:
        return {"ok": False, "verified": False,
                "error": "wpctl set-default refused — routing unchanged",
                "before": (before.get("default_sink") or {}).get("id")}
    after = audio_status()
    new_default = (after.get("default_sink") or {}).get("id") \
        if after.get("available") else None
    verified = new_default == sink_id
    return {"ok": verified, "verified": verified,
            "before": (before.get("default_sink") or {}).get("id"),
            "after": new_default,
            "detail": None if verified else
            "requested default %s but the re-read shows %s — no "
            "success claim" % (sink_id, new_default)}
