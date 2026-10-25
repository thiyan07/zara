"""Stage 24b — ADB observation layer (read-only, detection-only).

Adopted from the 2026-10-20 GitHub research (docs/RESEARCH_2026-10-20.md):
the Android computer-use agents (NexusAndroid, droidjig, omni-use) all
share an observe→decide→act loop over an ADB transport. Zara adopts the
OBSERVE half, and consciously narrower: per Zara's device-security
policy (docs/DEVICE_SECURITY.md, core/builtin_tools.py parity table)
the phone stays behind the paired-device protocol — THIS MODULE exposes
no input injection, no screen streaming, no shell escape hatches. It is
a bounded, read-only observation surface for inventory and UI-tree
metadata, honest about absence (no adb → refusal, never a fake
success).

Bounded by convention:
- every subprocess call has an explicit timeout (never unbounded),
- output strings are size-capped in the returned dicts,
- no file persistence beyond the caller's expectations (nothing is
  written outside return values),
- failures are returned as {"ok": False, ...} with the exact stage,
  never raised past the tool surface.
"""

import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

_BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")

ADB_TIMEOUT_S = 10.0          # one bounded adb call
UI_DUMP_TIMEOUT_S = 6.0       # uiautomator dump is slow on old devices
_UI_NODE_CAP = 400            # max UI nodes parsed (bounded memory)
_STR_CAP = 200                # chars per string field (bounded)
_DEV_LINE_MAX = 40            # max devices parsed
_ERR_CAP = 200                # chars per error detail

# state-verification contract (conditions are AND-combined; each must
# match at least one observed node)
_UI_COND_KEYS = ("text", "text_contains", "desc", "desc_contains",
                 "cls", "clickable")
_VERIFY_ATTEMPT_MAX = 3       # max dumps per verification
_VERIFY_INTERVAL_MAX = 2.0    # s between dumps
_VERIFY_COND_MAX = 8          # max simultaneous conditions


def _run(argv: list, timeout_s: float) -> dict:
    """One bounded adb invocation. Returns ok-out with capped strings."""
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "stage": "spawn", "detail": str(e)[:_ERR_CAP]}
    if r.returncode != 0:
        err = ((r.stderr or b"") + (r.stdout or b"")).decode(
            "utf-8", "replace").strip()[:_ERR_CAP]
        return {"ok": False, "stage": "adb", "detail": err}
    out = (r.stdout or b"").decode("utf-8", "replace")
    return {"ok": True, "out": out, "stage": "done"}


def adb_available() -> dict:
    """adb presence check (detection). Truthful: absent → ok False with
    the exact reason, never a fabricated device list."""
    path = shutil.which("adb")
    if path is None:
        return {"ok": False, "reason": "adb not found on PATH — "
                                       "Android device inventory is "
                                       "unavailable (detection only)"}
    return {"ok": True, "path": path}


def parse_devices(text: str) -> list:
    """Parse `adb devices -l` output (unit-testable; pure)."""
    out = []
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith(("List of devices", "* daemon")):
            continue
        parts = line.split()
        if not parts:
            continue
        entry = {"serial": parts[0][:64], "state": parts[1][:24]}
        for kv in parts[2:]:
            if ":" in kv:
                k, _, v = kv.partition(":")
                if k in ("model", "device", "usb", "transport_id",
                         "product"):
                    entry[k] = v[:64]
        out.append(entry)
        if len(out) >= _DEV_LINE_MAX:
            break
    return out


def list_devices() -> dict:
    """`adb devices -l` → bounded device inventory."""
    check = adb_available()
    if not check["ok"]:
        return {"ok": False, "reason": check["reason"], "devices": [],
                "stage": "detection"}
    argv = ["adb", "devices", "-l"]
    r = _run(argv, ADB_TIMEOUT_S)
    if not r["ok"]:
        return {"ok": False, "reason": "adb devices failed at stage "
                "%s: %s" % (r["stage"], r["detail"]), "devices": [],
                "stage": r["stage"]}
    return {"ok": True, "devices": parse_devices(r["out"]),
            "stage": "done"}


def parse_bounds(text: str):
    """`[x1,y1][x2,y2]` → ((x1, y1), (x2, y2)) or None (pure; unit
    target). Strict full-string match — truncated/half bounds never
    pass."""
    if not text:
        return None
    m = _BOUNDS_RE.match(text.strip())
    if not m:
        return None
    x1, y1, x2, y2 = (int(v) for v in m.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1), (x2, y2)


def ui_nodes(xml_text: str) -> list:
    """Parse a uiautomator XML dump into bounded observation items.
    Read-only metadata: text/content-desc/class/clickable/bounds center
    — never media, never keystrokes."""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return [{"error": "uiautomator xml parse failed: %s" % e}]
    for node in root.iter("node"):
        bounds = parse_bounds(node.get("bounds") or "")
        center = None
        if bounds:
            center = (round((bounds[0][0] + bounds[1][0]) / 2),
                      round((bounds[0][1] + bounds[1][1]) / 2))
        out.append({
            "text": (node.get("text") or "")[:_STR_CAP],
            "desc": (node.get("content-desc") or "")[:_STR_CAP],
            "cls": (node.get("class") or "")[:64],
            "clickable": (node.get("clickable") == "true"),
            "bounds": node.get("bounds") or "",
            "center": center,
        })
        if len(out) >= _UI_NODE_CAP:
            break
    return out


def _match_one(item: dict, cond: dict) -> bool:
    """Item satisfies one condition iff every declared key matches
    (pure; unit target). Unknown keys never match — typos must fail
    loudly, not silently."""
    if "clickable" in cond:
        if item.get("clickable") is not bool(cond["clickable"]):
            return False
    for key in ("text", "desc", "cls"):
        if key in cond and item.get(key) != cond[key]:
            return False
    for key in ("text_contains", "desc_contains"):
        if key in cond:
            sub = cond[key]
            field = "text" if key == "text_contains" else "desc"
            if sub not in item.get(field, ""):
                return False
    return True


def _validate_expect(expect: list) -> list:
    """Validate the expect list (pure; unit target)."""
    if not isinstance(expect, list) or not expect:
        raise ValueError("expect must be a non-empty list of condition "
                         "objects with one or more of: "
                         + ", ".join(_UI_COND_KEYS))
    if len(expect) > _VERIFY_COND_MAX:
        raise ValueError("too many conditions (%d > %d)"
                         % (len(expect), _VERIFY_COND_MAX))
    clean = []
    for c in expect:
        if not isinstance(c, dict) or not c:
            raise ValueError("each expect entry must be a non-empty "
                             "object")
        unknown = sorted(set(c) - set(_UI_COND_KEYS))
        if unknown:
            raise ValueError("unknown condition key(s): %s"
                             % ", ".join(unknown))
        for k in _UI_COND_KEYS:
            if k not in c:
                continue
            if k == "clickable":
                if not isinstance(c[k], bool):
                    raise ValueError("clickable must be a boolean")
            elif not isinstance(c[k], str) or not c[k]:
                raise ValueError("condition %r must be a non-empty "
                                 "string" % k)
            if k != "clickable" and len(c[k]) > _STR_CAP:
                raise ValueError("condition %r exceeds the %d-char cap"
                                 % (k, _STR_CAP))
        clean.append(dict(c))
    return clean


def match_state(items: list, expect: list) -> dict:
    """Evaluate validated expectations over parsed UI nodes (pure; unit
    target). Truthful per-condition evidence: the first matching item's
    identity is returned; failed conditions name the single key that
    broke them when determinable."""
    clean = _validate_expect(expect)
    per = []
    satisfied = True
    for cond in clean:
        hit = None
        for item in items:
            if _match_one(item, cond):
                hit = item
                break
        if hit is not None:
            per.append({"condition": cond, "matched": True,
                        "evidence": {k: hit.get(k) for k in
                                     ("text", "desc", "cls", "bounds",
                                      "center")}})
        else:
            # name the single key that broke the closest candidate when
            # determinable — makes failures actionable without noise
            near_hit = None
            for item in items:
                others = {k: cond[k] for k in cond if k != "clickable"}
                if _match_one(item, others):
                    near_hit = item
                    break
            if "clickable" in cond and near_hit is not None:
                reason = ("node matched in kind but had clickable=%s"
                          % near_hit.get("clickable"))
            else:
                reason = "no observed node matched"
            satisfied = False
            per.append({"condition": cond, "matched": False,
                        "reason": reason})
    return {"ok": True, "satisfied": satisfied,
            "per_condition": per}


def verify_ui(serial: str = None, expect: list = None,
              max_attempts: int = 2, interval_s: float = 1.0) -> dict:
    """Observe the phone's UI tree and verify expected state — the
    verification half of the observe→act loop, matching what
    android-use-class agents do AFTER the paired-device protocol
    executes an action. Read-only: dumps bounded metadata only; a
    failed verification never triggers an act and never invents
    success."""
    if expect is None:
        raise ValueError("expect is required")
    clean = _validate_expect(expect)
    max_attempts = max(1, min(int(max_attempts), _VERIFY_ATTEMPT_MAX))
    interval_s = max(0.0, min(float(interval_s), _VERIFY_INTERVAL_MAX))
    started = time.monotonic()

    def _ms() -> int:
        return int((time.monotonic() - started) * 1000)

    last = None
    for attempt in range(1, max_attempts + 1):
        dump = ui_dump(serial)
        if not dump["ok"]:
            return {"ok": False, "stage": dump.get("stage", "dump"),
                    "reason": dump.get("reason"), "attempt": attempt,
                    "satisfied": False, "total_ms": _ms(),
                    "dump_elapsed_ms": dump.get("elapsed_ms")}
        last = match_state(dump["items"], clean)
        if last["satisfied"]:
            return {"ok": True, "satisfied": True, "attempt": attempt,
                    "per_condition": last["per_condition"],
                    "total_ms": _ms(),
                    "nodes_observed": dump.get("nodes"),
                    "compressed": dump.get("compressed")}
        if attempt < max_attempts and interval_s > 0:
            time.sleep(interval_s)
    return {"ok": True, "satisfied": False,
            "attempt": max_attempts,
            "per_condition": (last or {}).get("per_condition", []),
            "total_ms": _ms()}


def ui_dump(serial: str = None) -> dict:
    """One bounded `uiautomator dump` observation over adb exec-out.
    Observation only — the returned items are UI-tree METADATA (ids,
    labels, bounds). No taps/keys are ever sent from here: input stays
    with the paired-device protocol (device-security policy).

    Speed round (2026-10-20 research): `uiautomator dump` costs 1–5 s
    per call round-trip — the slow half of any verification loop. The
    `--compressed` layout flag yields a much smaller XML (faster
    transfer + parse). We therefore try compressed FIRST and fall back
    to the plain dump on an adb-level failure (older builds reject the
    flag); the elapsed time and the mode are reported honestly in every
    result so the caller can tune."""
    check = adb_available()
    if not check["ok"]:
        return {"ok": False, "reason": check["reason"], "items": [],
                "stage": "detection"}
    base = ["adb"]
    if serial:
        base += ["-s", serial]
    started = time.monotonic()
    attempts = []
    for flagged, dump_argv in ((True, ["uiautomator", "dump",
                                       "--compressed", "/dev/tty"]),
                               (False, ["uiautomator", "dump",
                                        "/dev/tty"])):
        r = _run(base + ["exec-out"] + dump_argv, UI_DUMP_TIMEOUT_S)
        attempts.append("compressed" if flagged else "plain")
        if r["ok"]:
            mode = ("compressed" if flagged else "plain")
            break
        if r["stage"] != "adb":
            # spawn/transport failure — a retry with a different flag
            # cannot help; report honestly
            return {"ok": False, "reason": "uiautomator dump failed at "
                    "stage %s: %s" % (r["stage"], r["detail"]), "items": [],
                    "stage": r["stage"],
                    "elapsed_ms": int((time.monotonic() - started)
                                      * 1000),
                    "attempts": attempts}
    else:
        return {"ok": False, "reason": "uiautomator dump failed at "
                "stage adb: %s" % r["detail"], "items": [],
                "stage": "adb",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "attempts": attempts}
    text = r["out"]
    # some builds print a banner line before the xml
    if text.lstrip().startswith(("UI hierarchies dumped to:",
                                 "<hierarchy")):
        start = text.find("<")
        text = text[start:] if start > 0 else text
    if "<hierarchy" not in text:
        return {"ok": False, "reason": "no UI-hierarchy XML in the "
                "dump output (device may have a system-level dump "
                "restriction)", "items": [], "stage": "parse",
                "compressed": mode == "compressed",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "bytes": len(text), "attempts": attempts}
    items = ui_nodes(text)
    return {"ok": True, "items": items, "stage": "done", "nodes":
            len(items), "bytes": len(text),
            "compressed": mode == "compressed",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "attempts": attempts}
