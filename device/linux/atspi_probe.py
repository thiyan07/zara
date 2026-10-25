"""AT-SPI2 one-shot probe helper (read OR act on ONE node per call).

Runs under /usr/bin/python3 (system dbus bindings); the project interpreter
only ever spawns it as a subprocess with a JSON request on argv[1] and reads
a JSON response on stdout. Bounded: per-call D-Bus timeouts, a soft overall
deadline, truncated strings, capped node counts. Any failure returns
{"ok": false, ...} — never a traceback shaped like data.

Ops:
  tree      {app_filter, max_nodes, max_depth, include_text, text_chars}
  node      {bus, path, text_chars}
  do_action {bus, path, action}        (action: click|press|toggle)
  grab_focus {bus, path}
  set_text  {bus, path, text, mode}    (mode: replace|insert)
  get_text  {bus, path, max_chars}
"""
import json
import sys
import time

try:
    import dbus
except ImportError:
    print(json.dumps({"ok": False, "error": "python dbus bindings missing "
                                            "(need python3-dbus)"}))
    sys.exit(0)

AI = "org.a11y.atspi.Accessible"
PI = "org.freedesktop.DBus.Properties"
CALL_TIMEOUT = 8
DEADLINE_S = 25

STATES = {
    0: "invalid", 1: "active", 2: "armed", 3: "busy", 4: "checked",
    5: "defunct", 6: "editable", 7: "enabled", 8: "expandable",
    9: "expanded", 10: "focusable", 11: "focused", 12: "horizontal",
    13: "iconified", 14: "modal", 15: "multi-line", 16: "multiselectable",
    17: "opaque", 18: "pressed", 19: "resizable", 20: "selectable",
    21: "selected", 22: "sensitive", 23: "showing", 24: "single-line",
    25: "stale", 26: "transient", 27: "vertical", 28: "visible",
    29: "manages-descendants", 30: "indeterminate", 31: "required",
    32: "truncated", 33: "animated", 34: "invalid-entry",
    35: "supports-autocomplete", 36: "selectable-text", 37: "is-default",
    38: "visited", 39: "checkable", 40: "has-popup", 41: "has-tooltip",
    42: "read-only",
}

CLICK_ACTIONS = ("click", "press", "toggle")


def decode_states(raw):
    names = []
    bits = []
    for word_i, word in enumerate(list(raw)[:2]):
        w = int(word)
        for b in range(32):
            if w & (1 << b):
                idx = word_i * 32 + b
                bits.append(idx)
                if idx in STATES:
                    names.append(STATES[idx])
    return names, bits


def _if(obj, iface):
    return dbus.Interface(obj, iface)


def _call(iface, method, *args):
    return getattr(iface, method)(*args, timeout=CALL_TIMEOUT)


def acc(bus, bname, path):
    return _if(bus.get_object(bname, path), AI)


def props(bus, bname, path):
    return _if(bus.get_object(bname, path), PI)


def comp(bus, bname, path):
    return _if(bus.get_object(bname, path), "org.a11y.atspi.Component")


def act(bus, bname, path):
    return _if(bus.get_object(bname, path), "org.a11y.atspi.Action")


def txt(bus, bname, path):
    return _if(bus.get_object(bname, path), "org.a11y.atspi.Text")


def edtxt(bus, bname, path):
    return _if(bus.get_object(bname, path), "org.a11y.atspi.EditableText")


def node_record(bus, bname, path, node_id, include_text=False,
                text_chars=300):
    """Full record for one live node. Raises on defunct/unreachable
    (callers treat that as stale, never as data)."""
    a = acc(bus, bname, path)
    p = props(bus, bname, path)
    try:
        role_name = str(_call(a, "GetRoleName"))
    except Exception:
        role_name = ""
    try:
        role_num = int(_call(a, "GetRole"))
    except Exception:
        role_num = -1
    try:
        name = str(_call(p, "Get", AI, "Name"))[:120]
    except Exception:
        name = ""
    try:
        states, bits = decode_states(_call(a, "GetState"))
    except Exception:
        states, bits = [], []
    try:
        ifaces = sorted(str(i) for i in _call(a, "GetInterfaces"))
    except Exception:
        ifaces = []
    rec = {"id": node_id, "bus": str(bname), "path": str(path),
           "role": role_name or ("role_%d" % role_num), "role_num": role_num,
           "name": name, "states": states, "state_bits": bits,
           "focused": "focused" in states}
    if "org.a11y.atspi.Action" in ifaces:
        try:
            acts = _call(act(bus, bname, path), "GetActions")
            rec["actions"] = [str(t[0]) for t in acts][:8]
        except Exception:
            rec["actions"] = []
    else:
        rec["actions"] = []
    if "org.a11y.atspi.Component" in ifaces:
        try:
            x, y, w, h = _call(comp(bus, bname, path), "GetExtents", 0)
            rec["bounds"] = [int(x), int(y), int(w), int(h)]
        except Exception:
            rec["bounds"] = None
    else:
        rec["bounds"] = None
    if include_text and ("org.a11y.atspi.Text" in ifaces or
                         "org.a11y.atspi.EditableText" in ifaces):
        try:
            t = _call(txt(bus, bname, path), "GetText", 0,
                      max(1, min(int(text_chars), 1000)))
            rec["text"] = str(t)[:int(text_chars)]
        except Exception:
            rec["text"] = None
    rec["editable"] = "org.a11y.atspi.EditableText" in ifaces
    return rec


def op_tree(bus, req):
    max_nodes = max(1, min(int(req.get("max_nodes", 150)), 400))
    max_depth = max(0, min(int(req.get("max_depth", 4)), 8))
    include_text = bool(req.get("include_text", False))
    text_chars = max(1, min(int(req.get("text_chars", 300)), 1000))
    want = str(req.get("app_filter", "") or "").lower()
    deadline = time.monotonic() + DEADLINE_S
    children = _call(acc(bus, "org.a11y.atspi.Registry",
                         "/org/a11y/atspi/accessible/root"), "GetChildren")
    apps = []
    for (bname, path) in children:
        try:
            nm = str(_call(props(bus, bname, path), "Get", AI, "Name"))
        except Exception:
            continue
        if want and want not in nm.lower():
            continue
        apps.append((str(bname), str(path), nm))
    budget = [max_nodes]
    skipped = [0]
    focused = [None]
    out_apps = []
    apps_seen = len(children)
    for (bname, path, nm) in apps:
        nodes = []

        def walk(cb, cp, idx_path, depth):
            if budget[0] <= 0 or time.monotonic() > deadline:
                return
            if depth > max_depth:
                return
            if "null" in cp:
                skipped[0] += 1
                return
            budget[0] -= 1
            try:
                rec = node_record(bus, cb, cp, idx_path, include_text,
                                  text_chars)
            except Exception:
                skipped[0] += 1
                return
            nodes.append(rec)
            if rec["focused"] and focused[0] is None:
                focused[0] = {"app": nm, "node_id": idx_path}
            try:
                kids = _call(acc(bus, cb, cp), "GetChildren")
            except Exception:
                skipped[0] += 1
                return
            for i, (kb, kp) in enumerate(kids):
                sub = str(i) if not idx_path else idx_path + "." + str(i)
                walk(str(kb), str(kp), sub, depth + 1)
                if budget[0] <= 0 or time.monotonic() > deadline:
                    break

        # App root itself is node id "" (addressable for actions too).
        try:
            root_rec = node_record(bus, bname, path, "", include_text,
                                   text_chars)
            nodes.append(root_rec)
            if root_rec["focused"] and focused[0] is None:
                focused[0] = {"app": nm, "node_id": ""}
        except Exception:
            skipped[0] += 1
            continue
        try:
            kids = _call(acc(bus, bname, path), "GetChildren")
        except Exception:
            kids = []
        for i, (kb, kp) in enumerate(kids):
            walk(str(kb), str(kp), str(i), 1)
            if budget[0] <= 0 or time.monotonic() > deadline:
                break
        out_apps.append({"app": nm, "bus": bname, "nodes": nodes})
        if budget[0] <= 0 or time.monotonic() > deadline:
            break
    return {"apps": out_apps, "focused": focused[0], "skipped": skipped[0],
            "truncated": budget[0] <= 0, "apps_seen": apps_seen}


def op_node(bus, req):
    return node_record(bus, req["bus"], req["path"], "",
                       bool(req.get("include_text", False)),
                       int(req.get("text_chars", 300)))


def op_do_action(bus, req):
    want = str(req.get("action", "click")).lower()
    a = acc(bus, req["bus"], req["path"])
    pre = node_record(bus, req["bus"], req["path"], "")
    try:
        names = [str(t[0]).lower() for t in _call(a, "GetActions")]
    except Exception as e:
        return {"applied": False,
                "error": "actions unreadable: %s" % str(e)[:150]}
    idx = names.index(want) if want in names else -1
    if idx < 0:
        for cand in CLICK_ACTIONS:
            if cand in names:
                idx = names.index(cand)
                want = cand
                break
    if idx < 0:
        return {"applied": False, "requested": req.get("action"),
                "available": sorted(names)[:8],
                "error": "no clickable action (refused)"}
    try:
        done = bool(_call(a, "DoAction", idx))
    except Exception as e:
        return {"applied": False, "action": want,
                "error": str(e)[:200]}
    try:
        post = node_record(bus, req["bus"], req["path"], "")
    except Exception:
        post = {"error": "node unreadable after action"}
    return {"applied": done, "action": want,
            "pre_states": pre.get("states", []),
            "post_states": post.get("states", [])}


def op_grab_focus(bus, req):
    pre = node_record(bus, req["bus"], req["path"], "")
    try:
        done = bool(_call(comp(bus, req["bus"], req["path"]), "GrabFocus"))
    except Exception as e:
        return {"applied": False, "error": str(e)[:200]}
    try:
        post = node_record(bus, req["bus"], req["path"], "")
    except Exception:
        post = {"states": []}
    focused_now = "focused" in post.get("states", [])
    return {"applied": done and focused_now,
            "toolkit_ok": done, "focused": focused_now,
            "pre_states": pre.get("states", []),
            "post_states": post.get("states", [])}


def op_set_text(bus, req):
    text = req.get("text", "")
    if not isinstance(text, str) or len(text) > 2000:
        return {"applied": False, "error": "bad text"}
    mode = req.get("mode", "replace")
    try:
        ifaces = list(_call(acc(bus, req["bus"], req["path"]),
                            "GetInterfaces"))
    except Exception as e:
        return {"applied": False,
                "error": "node unreadable: %s" % str(e)[:150]}
    if "org.a11y.atspi.EditableText" not in ifaces:
        return {"applied": False, "error": "not an editable control",
                "interfaces": sorted(str(i) for i in ifaces)}
    ed = edtxt(bus, req["bus"], req["path"])
    t = txt(bus, req["bus"], req["path"])
    try:
        if mode == "insert":
            cur = str(_call(t, "GetText", 0, 4000))
            ok = bool(_call(ed, "InsertText", len(cur), text, len(text)))
            check = cur + text
        else:
            ok = bool(_call(ed, "SetTextContents", text))
            check = text
    except Exception as e:
        return {"applied": False, "error": str(e)[:200]}
    try:
        after = str(_call(t, "GetText", 0, max(len(check), 1) + 16))
    except Exception:
        after = None
    applied = ok and (after is not None) and (
        check[:4000] in after if mode == "insert"
        else after[:len(check)] == check)
    return {"applied": bool(applied), "toolkit_ok": ok, "mode": mode,
            "chars": len(text)}


def op_get_text(bus, req):
    try:
        t = _call(txt(bus, req["bus"], req["path"]), "GetText", 0,
                  max(1, min(int(req.get("max_chars", 500)), 2000)))
        return {"text": str(t)}
    except Exception as e:
        return {"error": str(e)[:200]}


# ---------------- read-batch (one spawn, N read-only ops) ----------------
# Speed evidence (2026-10-20 benchmarks): a single spawn+connect dominates
# one op (~40 ms) while the D-Bus work inside may be ~ms. The `multi` op
# executes several READ-ONLY ops in ONE helper invocation: same ops, same
# deadlines, same capping — state-changing ops (do_action / grab_focus /
# set_text) are structurally excluded so batching can never reorder a
# negative check before a write. Additive; every existing op is untouched.

READ_DISPATCH = {"node": op_node, "tree": op_tree, "get_text": op_get_text}
MAX_MULTI_OPS = 8
_MULTI_OP_KEY = "multi"


def op_apps(bus, req):
    """Fast app-list read: registry children + Name ONLY (no tree walk).
    Costs one roundtrip + one Name read per app (vs the full tree),
    which is what bounded presence checks need after a launch."""
    want = str(req.get("app_filter", "") or "").lower()
    try:
        max_apps = max(1, min(int(req.get("max_apps", 40)), 64))
    except (TypeError, ValueError):
        max_apps = 40
    names, seen = [], 0
    try:
        children = _call(acc(bus, "org.a11y.atspi.Registry",
                             "/org/a11y/atspi/accessible/root"),
                         "GetChildren")
    except Exception as e:
        return {"apps": [], "registry_apps": 0, "error": str(e)[:160]}
    for (bname, path) in children:
        try:
            nm = str(_call(props(bus, str(bname), str(path)), "Get",
                           AI, "Name"))
        except Exception:
            continue
        if want and want not in nm.lower():
            continue
        names.append(nm[:120])
        if len(names) >= max_apps:
            break
    return {"apps": names, "registry_apps": len(children)}


def op_multi(bus, req):
    """Run N read-only child ops in one helper invocation. Each entry is
    dispatched with the SAME validation and capping as a standalone
    call; a child failure is recorded per-entry and never kills the
    batch (deadline excepted)."""
    ops = req.get("ops")
    if not isinstance(ops, list) or not ops:
        return {"results": [],
                "error": "ops must be a non-empty list of read-only ops"}
    if len(ops) > MAX_MULTI_OPS:
        return {"results": [],
                "error": "too many ops (%d > %d)"
                         % (len(ops), MAX_MULTI_OPS)}
    out = []
    deadline = time.monotonic() + DEADLINE_S
    for entry in ops:
        if time.monotonic() > deadline:
            out.append({"ok": False, "error": "batch deadline exceeded"})
            continue
        op_name = entry.get("op") if isinstance(entry, dict) else None
        handler = READ_DISPATCH.get(op_name) if op_name else None
        if handler is None:
            out.append({"ok": False,
                        "error": "unsupported child op (refused)"})
            continue
        try:
            out.append({"ok": True, "result": handler(bus, entry)})
        except Exception as e:  # noqa: BLE001 — per-entry, batch lives on
            out.append({"ok": False, "error": str(e)[:200]})
    return {"results": out}


def bus_connect():
    try:
        sbus = dbus.SessionBus()
        b = sbus.get_object("org.a11y.Bus", "/org/a11y/bus")
        addr = b.GetAddress(dbus_interface="org.a11y.Bus",
                            timeout=CALL_TIMEOUT)
        return dbus.bus.BusConnection(str(addr), mainloop=None)
    except Exception:
        import os
        uid = os.getuid()
        return dbus.bus.BusConnection(
            "unix:path=/run/user/%d/at-spi/bus" % uid, mainloop=None)


def main():
    try:
        req = json.loads(sys.argv[1])
    except Exception:
        print(json.dumps({"ok": False, "error": "bad request JSON"}))
        return
    try:
        bus = bus_connect()
    except Exception as e:
        print(json.dumps({"ok": False,
                          "error": "a11y bus unreachable: %s" % str(e)[:200]}))
        return
    op = req.get("op", "")
    try:
        if op == "tree":
            res = op_tree(bus, req)
        elif op == "node":
            res = op_node(bus, req)
        elif op == "do_action":
            res = op_do_action(bus, req)
        elif op == "grab_focus":
            res = op_grab_focus(bus, req)
        elif op == "set_text":
            res = op_set_text(bus, req)
        elif op == "get_text":
            res = op_get_text(bus, req)
        elif op == "multi":
            res = op_multi(bus, req)
        elif op == "apps":
            res = op_apps(bus, req)
        else:
            print(json.dumps({"ok": False, "error": "unknown op (refused)"}))
            return
        print(json.dumps({"ok": True, "result": res}, default=str))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)[:300]}))


if __name__ == "__main__":
    main()
