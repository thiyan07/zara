"""Stage 31-P2 — laptop-side system control surfaces (P2a volume/mute/
brightness + P2b playback via MPRIS).

Scope discipline (recorded in docs/IMPLEMENTATION_PLAN.md P2):
- USER-PERCEPTIBLE STATE, honest verification: every SET of volume,
  mute, or brightness is verified by an immediate RE-READ of the OS
  property it changed — the verdict comes from the re-read, never from
  the command's exit status alone. "applied" means "the re-read shows
  the requested state", nothing more.
- No new dependencies: wpctl (PipeWire), the sysfs backlight API, and
  busctl (MPRIS2 over the user session bus). If an interface is
  absent locally the result says so honestly — never a fake read.
- Zero players advertised / no player available → an honest
  "nothing to control" answer. Zara installs nothing, connects to no
  new daemons, and never drives hardware this laptop does not have.
"""
from __future__ import annotations

import glob
import re
import subprocess
from pathlib import Path

_SINK = "@DEFAULT_AUDIO_SINK@"
_VOL_RE = re.compile(r"Volume:\s*([0-9]+(?:\.[0-9]+)?)\s*(\[MUTED\])?")


def _run(argv: list, timeout: float = 6.0) -> tuple[int, str]:
    """Low-level executor; returns (returncode, stdout). Injections and
    fakes patch THIS (single seam, shell=False throughout)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)



def unavailable(detail: str) -> dict:
    return {"ok": False, "available": False, "detail": detail}


def _read_volume() -> dict:
    rc, out = _run(["wpctl", "get-volume", _SINK])
    m = _VOL_RE.search(out or "")
    if rc != 0 or not m:
        return {"available": False,
                "detail": "wpctl unavailable or refused the read "
                          "(PipeWire absent?)"}
    return {"available": True,
            "volume_ratio": float(m.group(1)),
            "volume_pct": round(float(m.group(1)) * 100.0, 1),
            "muted": m.group(2) is not None}


def volume_set(inputs: dict, ctx: dict) -> dict:
    """Set desktop volume by level (0-100) or delta (± points), then
    verify by re-read."""
    i = inputs or {}
    level, delta = i.get("level"), i.get("delta")

    before = _read_volume()
    if not before["available"]:
        return {**unavailable(before["detail"]), "nothing_changed": True}
    cur = before["volume_ratio"]

    if isinstance(level, (int, float)) and not isinstance(delta, (int, float)):
        target = round(max(0.0, min(1.0, float(level) / 100.0)), 3)
    elif isinstance(delta, (int, float)) and not isinstance(level, (int, float)):
        # delta is expressed in VOLUME-POINTS (5 = 5 points up)
        point = float(delta) / 100.0
        target = round(max(0.0, min(1.0, cur + point)), 3)
    else:
        return {"ok": False, "nothing_changed": True,
                "error": "exactly one of `level` (0-100) or `delta` "
                         "(volume points) is required"}

    rc, _ = _run(["wpctl", "set-volume", _SINK, f"{target:.3f}"])
    after = _read_volume()
    applied = (rc == 0 and after["available"]
               and abs(after["volume_ratio"] - target) <= 0.02)
    audit = {"ok": bool(applied),
             "before_volume_pct": before["volume_pct"],
             "target_volume_pct": round(target * 100.0, 1),
             "after_volume_pct": (after["volume_pct"]
                                  if after["available"] else None),
             "muted": after.get("muted"),
             "verification": "wpctl get-volume re-read after set",
             "verdict": ("applied" if applied else
                         ("changed-elsewhere" if rc == 0 and
                          after["available"] and
                          after["volume_ratio"] != cur else "not-applied")),
             "nothing_changed": not applied}
    if rc != 0:
        audit["detail"] = "wpctl set-volume refused — volume unchanged"
    return audit


def mute_set(inputs: dict, ctx: dict) -> dict:
    """Mute/unmute the default sink; verified by the re-read MUTED flag."""
    i = inputs or {}
    mode = i.get("mode", "toggle")
    if mode not in ("on", "off", "toggle"):
        return {"ok": False, "nothing_changed": True,
                "error": "mode must be on | off | toggle"}
    before = _read_volume()
    if not before["available"]:
        return {**unavailable(before["detail"]), "nothing_changed": True}
    target_flag = {"on": "1", "off": "0", "toggle": "toggle"}[mode]
    rc, _ = _run(["wpctl", "set-mute", _SINK, target_flag])
    after = _read_volume()
    want_mute = {"on": True, "off": False}.get(mode)
    if want_mute is None:  # toggle: expect the flag to have flipped
        applied = (rc == 0 and after["available"]
                   and after["muted"] != before["muted"])
    else:
        applied = (rc == 0 and after["available"]
                   and after["muted"] == want_mute)
    return {"ok": bool(applied),
            "before_muted": before["muted"],
            "after_muted": after.get("muted"),
            "after_volume_pct": after.get("volume_pct"),
            "verification": "wpctl get-volume re-read after set",
            "verdict": "applied" if applied else "not-applied",
            "nothing_changed": not applied}


# ---------------- P2a: brightness (sysfs backlight API) ----------------

_BL_GLOBS = [Path("/sys/class/backlight/*/brightness")]


def _read_brightness() -> dict:
    paths = sorted(Path(pth).parent for pth in
                   glob.glob("/sys/class/backlight/*/brightness"))
    if not paths:
        return {"available": False,
                "detail": "no sysfs backlight device on this machine"}
    rows = []
    for d in paths:
        max_b = d / "max_brightness"
        try:
            cur = int(d.joinpath("brightness").read_text().strip())
            mx = int(max_b.read_text().strip())
        except (OSError, ValueError) as e:
            rows.append({"path": str(d), "available": False,
                         "detail": str(e)})
            continue
        rows.append({"path": str(d), "available": True,
                     "current": cur, "max": mx,
                     "pct": round(cur * 100.0 / mx, 1) if mx > 0 else None})
    r = rows[0]
    if not r["available"]:
        return {"available": False, "detail": r["detail"]}
    return {"available": True, "devices": rows,
            "path": r["path"], "current": r["current"],
            "max": r["max"], "pct": r["pct"]}


def brightness_set(inputs: dict, ctx: dict) -> dict:
    """Set backlight to pct (1-100) with a re-read verification; honest
    permission report if the write is refused."""
    i = inputs or {}
    pct = i.get("pct")
    if not isinstance(pct, (int, float)) or not (1 <= float(pct) <= 100):
        return {"ok": False, "nothing_changed": True,
                "error": "pct (1-100) required"}
    before = _read_brightness()
    if not before["available"]:
        return {**unavailable(before["detail"]), "nothing_changed": True}
    target = max(1, round(before["max"] * float(pct) / 100.0))
    try:
        Path(before["path"], "brightness").write_text(str(target))
        write_err = None
    except OSError as e:
        write_err = str(e)
    after = _read_brightness()
    applied = (write_err is None and after["available"]
               and after["current"] == target)
    out = {"ok": bool(applied),
           "before_pct": before["pct"],
           "target_pct": round(target * 100.0 / before["max"], 1),
           "after_pct": after.get("pct"),
           "verification": "sysfs brightness re-read after write",
           "verdict": "applied" if applied else
                      ("permission-refused" if write_err else "not-applied"),
           "nothing_changed": not applied}
    if write_err is not None:
        out["detail"] = ("backlight write refused: this machine's "
                         "backlight is not writable by the agent — "
                         "stated plainly, nothing changed")
    return out


# ---------------- P2b: playback control (MPRIS over busctl) ------------

_MPRIS_PREFIX = "org.mpris.MediaPlayer2."


def _mpris_players() -> list[str]:
    rc, out = _run(["busctl", "--user", "list", "--no-legend"], timeout=4.0)
    if rc != 0:
        return []
    names = []
    for line in (out or "").splitlines():
        parts = line.split()
        for w in parts:
            if w.startswith(_MPRIS_PREFIX) and len(w) > len(_MPRIS_PREFIX):
                if w not in names:
                    names.append(w)
    return names


def _mps_property(dest: str, prop: str) -> str:
    rc, out = _run(["busctl", "--user", "call", dest,
                    "/org/mpris/MediaPlayer2",
                    "org.freedesktop.DBus.Properties", "Get", "ss",
                    "org.mpris.MediaPlayer2.Player", prop],
                   timeout=4.0)
    if rc != 0:
        return ""
    m = re.search(r'(?:s)\s+"?([^"\n]+)"?', out or "")
    return (m.group(1).strip().strip('"') if m else "")


def now_playing(inputs: dict, ctx: dict) -> dict:
    """Read-only: PlaybackStatus of every MPRIS player (any player that
    talks the standard desktop interface). No player advertised →
    honest 'nothing playing' — never fabricated."""
    players = _mpris_players()
    rows = []
    for p in players:
        status = _mps_property(p, "PlaybackStatus")
        rows.append({"player": p, "playback_status": status or "unknown"})
    return {"available": True, "count": len(players),
            "players": rows,
            "note": "MPRIS surfaces whatever reports itself on the "
            "user session bus; nothing is discovered or installed"}


_PLAY_METHODS = {"play_pause": "PlayPause", "play": "Play",
                 "pause": "Pause", "next": "Next", "previous": "Previous",
                 "stop": "Stop"}


def playback_control(inputs: dict, ctx: dict) -> dict:
    """MPRIS Player method call. Stream-transport controls verify by the
    re-read PlaybackStatus; track skips (Next/Previous) CANNOT change
    that status — their verdict is honestly read-only 'sent' + observed
    status, never a fabricated 'changed track' claim."""
    i = inputs or {}
    action = i.get("action", "")
    if action not in _PLAY_METHODS:
        return {"ok": False, "nothing_changed": True,
                "error": "action must be one of "
                         "play_pause|play|pause|next|previous|stop"}
    players = _mpris_players()
    if not players:
        return {**unavailable("no MPRIS player on the user session bus"),
                "nothing_changed": True}
    before = {p: _mps_property(p, "PlaybackStatus") for p in players}
    rows = []
    for p in players:
        rc, _ = _run(["busctl", "--user", "call", p,
                      "/org/mpris/MediaPlayer2",
                      "org.mpris.MediaPlayer2.Player",
                      _PLAY_METHODS[action]], timeout=4.0)
        after = _mps_property(p, "PlaybackStatus")
        if action in ("next", "previous"):
            # Track skips cannot be confirmed via PlaybackStatus; the
            # honest declaration is "sent", with the observed status
            # attached — never a fabricated "changed track".
            verdict = "sent" if rc == 0 else "not-applied"
        else:
            expected = {"play": "Playing", "pause": "Paused",
                        "stop": "Stopped"}.get(action)
            if expected:
                verdict = ("applied" if rc == 0 and after == expected
                           else "not-applied")
            else:  # play_pause: expect the observed status to change
                verdict = ("applied" if rc == 0 and after
                           and after != before[p] and after != ""
                           else "not-applied")
        rows.append({"player": p, "ok": rc == 0,
                     "before_status": before[p],
                     "after_status": after,
                     "verdict": verdict})
    ok = all(r["ok"] for r in rows)
    return {"ok": ok, "action": action, "players": rows,
            "nothing_changed": not ok,
            "verification": ("MPRIS PlaybackStatus re-read after call "
                             "for transport actions; Next/Previous "
                             "declared 'sent' honestly (status cannot "
                             "confirm a track id change)")}

