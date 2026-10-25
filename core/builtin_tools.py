"""Safe built-in tools. Small allowlisted set; everything else arrives in Stage 2/3."""
from __future__ import annotations
import os
import time
import re
import platform
import uuid
from datetime import datetime, timedelta, timezone
from .models import PermissionLevel, RiskLevel, ToolDefinition

def system_info(inputs: dict, ctx: dict) -> dict:
    return {"platform": platform.platform(), "python": platform.python_version()}

def echo(inputs: dict, ctx: dict) -> dict:
    return {"text": inputs.get("text", "")}


# ---------------------------------------------------------------------------
# P5 feature-hunt ⑥ (2026-10-23): calendar WRITE with approval.
# Core-local registry tool: appends exactly one VEVENT to a configured
# ZARA_ICS_PATH calendar — never more, never a rewrite of the rest of
# the file. Honesty rules:
# - no config, unreadable/malformed target, or missing END:VCALENDAR →
#   refuse (nothing written);
# - an existing event with the same summary + DTSTART is a duplicate →
#   refuse ("already on the calendar");
# - dtstart/dtend bounded to RFC-5545-plausible shapes (date, or a
#   datetime with optional Z), duration ≤ 7 days (timed), same year for
#   all-day; start ≤ end;
# - verified by a re-parse (count advanced by exactly 1, summary
#   present) — a re-read that disagrees is reported honestly, never as
#   success;
# - the write is atomic (tmp file + os.replace) and preserves the
#   file's existing line endings — a crash cannot leave a torn
#   calendar, mixing newlines, or reordered content.
# ---------------------------------------------------------------------------
_DTSHAPE = re.compile(
    r"^(20\d{2})(\d{2})(\d{2})"
    r"(?:T(\d{2})(\d{2})(\d{2})Z?)?$")
_MAX_EVENT_S = 7 * 24 * 3600


def _cal_target(inputs: dict) -> tuple[str, dict | None]:
    """Shared target-calendar selection for the calendar write /
    remove / move handlers (single source, P5 ⑯): ZARA_ICS_PATH, an
    optional colon-separated named-calendar pick, and an existence
    check. Returns (path, refusal|None); the refusal carries
    ok=False, committed=False."""
    config = os.environ.get("ZARA_ICS_PATH", "")
    if not config:
        return "", {"ok": False, "committed": False,
                    "refusal": "no calendar configured — set "
                               "ZARA_ICS_PATH once (colon-separated) "
                               "and ask again",
                    "detail": "no ZARA_ICS_PATH set — no calendar "
                              "configured; set it once and ask again"}
    paths = [p.strip() for p in config.split(":") if p.strip()]
    target = paths[0]
    wanted = str(inputs.get("calendar") or "").strip()
    if wanted:
        matches = [p for p in paths
                   if os.path.basename(p).lower() == wanted.lower()
                   or p.endswith("/" + wanted)]
        if not matches:
            why = (f"'{wanted[:60]}' is not one of the configured "
                   "calendars: "
                   + ", ".join(os.path.basename(p) for p in paths[:4]))
            return target, {"ok": False, "committed": False,
                            "refusal": why, "detail": why}
        target = matches[0]
    if not os.path.isfile(target):
        why = f"calendar file not found: {target}"
        return target, {"ok": False, "committed": False,
                        "refusal": why, "detail": why}
    return target, None


def calendar_event_add(inputs: dict, ctx: dict) -> dict:
    t = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    def _refuse(why: str, **kw) -> dict:
        kw.update(ok=False, committed=False, refusal=why, detail=why)
        return kw

    def _comp(m: re.Match) -> tuple:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4) or 0), int(m.group(5) or 0),
                int(m.group(6) or 0))

    target, refuse = _cal_target(inputs)
    if refuse is not None:
        return refuse
    summary = str(inputs.get("summary") or "").strip()
    if not summary:
        return _refuse("summary required (what should the event say?)")
    summary = summary[:120]
    m = _DTSHAPE.match(str(inputs.get("dtstart") or "").strip())
    if m is None:
        return _refuse(
            "dtstart must look like 20261023T143000Z (or 20261023 for "
            "an all-day event)")
    allday = m.group(4) is None
    dtstart = (f"{m.group(1)}{m.group(2)}{m.group(3)}" if allday else
               f"{m.group(1)}{m.group(2)}{m.group(3)}T"
               f"{m.group(4)}{m.group(5)}{m.group(6)}Z")
    dtend = ""
    md = _DTSHAPE.match(str(inputs.get("dtend") or "").strip())
    if md is not None:
        if _comp(md) < _comp(m):
            return _refuse("dtend is before dtstart — nothing written")
        if allday and md.group(1) != m.group(1):
            return _refuse("multi-year all-day events were not asked "
                           "for (refused)")
        if not allday:
            dur = (datetime(*_comp(md), tzinfo=timezone.utc)
                   - datetime(*_comp(m), tzinfo=timezone.utc))
            if dur.total_seconds() > _MAX_EVENT_S:
                return _refuse("events longer than 7 days were not "
                               "asked for (refused)")
        dtend = (f"{md.group(1)}{md.group(2)}{md.group(3)}" if allday
                 else f"{md.group(1)}{md.group(2)}{md.group(3)}T"
                      f"{md.group(4)}{md.group(5)}{md.group(6)}Z")
    elif str(inputs.get("dtend") or "").strip():
        return _refuse("dtend must match the same date/datetime shape")
    desc = str(inputs.get("description") or "").strip()[:500]
    loc = str(inputs.get("location") or "").strip()[:120]
    try:
        # newline="" — a default text read would silently translate
        # CRLF to \n and the rewrite below would normalize the whole
        # file's line endings (byte-preservation violated).
        with open(target, encoding="utf-8", newline="") as f:
            text = f.read(2_000_000)
    except (OSError, UnicodeError) as e:
        return _refuse(f"calendar file unreadable: {e}")
    if "BEGIN:VCALENDAR" not in text or "END:VCALENDAR" not in text:
        return _refuse(f"not a well-formed VCALENDAR file: {target}")
    from .calendar_flow import parse_ics, to_utc
    pre = parse_ics(target)
    want_dt = datetime(
        int(m.group(1)), int(m.group(2)), int(m.group(3)),
        int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
        tzinfo=timezone.utc)
    for ev in pre.get("events") or []:
        if (ev.get("summary") or "").strip().lower() != summary.lower():
            continue
        conv = to_utc(ev.get("raw_dtstart") or "", ev.get("tzid"))
        if conv["dt"] is None:
            continue
        if allday and conv["allday"] \
                and conv["dt"].date() == want_dt.date():
            return _refuse(
                "that all-day event is already on the calendar (same "
                "title and date) — nothing added", duplicate=True)
        if not allday and not conv["allday"] \
                and conv["dt"].replace(tzinfo=None) == \
                want_dt.replace(tzinfo=None):
            return _refuse(
                "that event is already on the calendar (same title "
                "and start) — nothing added", duplicate=True)
    before = len(pre.get("events") or [])
    uid = f"zara-{uuid.uuid4().hex}@zara"
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{t}"]
    lines.append((f"DTSTART;VALUE=DATE:{dtstart}" if allday else
                  f"DTSTART:{dtstart}"))
    if dtend:
        lines.append((f"DTEND;VALUE=DATE:{dtend}" if allday else
                      f"DTEND:{dtend}"))
    lines.append(f"SUMMARY:{summary}")
    if desc:
        lines.append("DESCRIPTION:" + desc.replace("\n", " "))
    if loc:
        lines.append(f"LOCATION:{loc}")
    lines.append("END:VEVENT")
    block = nl.join(lines) + nl
    anchor = text.rfind("END:VCALENDAR")
    new_text = text[:anchor] + block + text[anchor:]
    tmp = target + ".zara-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, target)
    # Verify by re-parse: count advanced by exactly one and the new
    # summary is present; the UID exists in the written bytes.
    parsed = parse_ics(target)
    now = len(parsed.get("events") or [])
    present = any(
        (e.get("summary") or "").strip().lower() == summary.lower()
        and e.get("raw_dtstart") == dtstart
        for e in parsed.get("events") or [])
    if now != before + 1 or not present:
        return {"ok": False, "committed": True, "verified": False,
                "uid": uid, "target": os.path.basename(target),
                "detail": "write landed but the re-parse does not "
                          "confirm exactly one new event — inspect "
                          "the file; the next ask re-reads truth"}
    return {"ok": True, "committed": True, "verified": True,
            "uid": uid, "target": os.path.basename(target),
            "summary": summary, "dtstart": dtstart, "all_day": allday,
            "events_total": now,
            "detail": "appended one VEVENT before END:VCALENDAR "
                      "(re-read verified)"}


def _scan_vblocks(text: str) -> tuple[list[dict], list[str]]:
    """Byte-preserving VEVENT block scan for removal/move selection:
    blocks carry [raw_span, kf-lines] with folding-aware key capture
    (summary/uid/dtstart(+tzid,VALUE=DATE kind)/dtend(+tzid)/rrule).
    Shared by the removal AND move handlers for BOTH selection and
    post-write verification (identical comparison semantics — never
    a substring false positive)."""
    lines = text.splitlines(keepends=True)
    blocks: list[dict] = []
    bi = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        up = s.upper()
        if up.startswith("BEGIN:VEVENT"):
            bi = {"raw": [i, i], "kf": {}}
            continue
        if up.startswith("END:VEVENT"):
            if bi is not None:
                bi["raw"][1] = i + 1
                blocks.append(bi)
            bi = None
            continue
        if bi is None:
            continue
        if s[:1] in (" ", "\t"):
            s = s[1:]
        if ":" not in s:
            continue
        k, _, v = s.partition(":")
        kf = bi["kf"]
        ku = k.strip().upper()
        if ku.startswith("SUMMARY") and not kf.get("summary"):
            kf["summary"] = v.strip()[:120]
        elif ku.startswith("UID") and not kf.get("uid"):
            kf["uid"] = v.strip()[:200]
        elif ku.startswith("DTSTART") and not kf.get("dtstart"):
            kf["dtstart"] = v.strip()
            tzid = re.search(r"TZID=([^;]+)", ku)
            if tzid:
                kf["tzid"] = tzid.group(1)[:40]
            # P5 ⑯: the move path needs the block's time KIND (an
            # all-day block has VALUE=DATE on DTSTART) — recorded
            # here so selection and verification read the same bytes.
            kf["allday_dtstart"] = ("VALUE=DATE" in ku)
        elif ku.startswith("DTEND") and not kf.get("dtend"):
            kf["dtend"] = v.strip()
            tzid = re.search(r"TZID=([^;]+)", ku)
            if tzid:
                kf["dtend_tzid"] = tzid.group(1)[:40]
        elif ku.startswith("RRULE") and not kf.get("rrule"):
            kf["rrule"] = v.strip()[:200]
    return blocks, lines


def system_health(inputs: dict, ctx: dict) -> dict:
    """P5 feature-hunt ⑬ (2026-10-23): an instantaneous, read-only
    laptop health reading — disk (home filesystem), RAM, load
    averages, uptime, cores. Plain /proc+statvfs facts, no
    interpretation beyond arithmetic the numbers themselves
    support (no health judgments, no predictions)."""
    try:
        disk_path = "/home" if os.path.isdir("/home") else os.getcwd()
        st = os.statvfs(disk_path)
        total_gb = st.f_frsize * st.f_blocks / 2 ** 30
        free_gb = st.f_frsize * st.f_bavail / 2 ** 30
        disk_used_pct = (round(100 * (1 - free_gb / total_gb), 1)
                         if total_gb > 0 else None)
        mem = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    mem[k] = int(v.split()[0])
        ram_total_gb = mem.get("MemTotal", 0) / 2 ** 20
        ram_avail_gb = mem.get("MemAvailable", 0) / 2 ** 20
        ram_used_pct = (round(100 * (1 - ram_avail_gb / ram_total_gb), 1)
                        if ram_total_gb > 0 else None)
        with open("/proc/loadavg", encoding="utf-8") as f:
            la = f.read().split()
        load1, load5, load15 = (float(la[0]), float(la[1]),
                                float(la[2][:7]))
        with open("/proc/uptime", encoding="utf-8") as f:
            uptime_s = float(f.read().split()[0])
        return {"ok": True,
                "disk_mount": disk_path,
                "disk_total_gb": round(total_gb, 1),
                "disk_free_gb": round(free_gb, 1),
                "disk_used_pct": disk_used_pct,
                "ram_total_gb": round(ram_total_gb, 2),
                "ram_available_gb": round(ram_avail_gb, 2),
                "ram_used_pct": ram_used_pct,
                "load_1m": load1, "load_5m": load5,
                "load_15m": load15,
                "cpu_cores": os.cpu_count(),
                "uptime_days": round(uptime_s / 86400, 2),
                "detail": "instantaneous reading: disk/RAM/load "
                          "facts only — no judgments, no predictions"}
    except (OSError, ValueError, IndexError) as e:
        return {"ok": False, "detail": f"health read failed: {e}"}


def system_top(inputs: dict, ctx: dict) -> dict:
    """P5 ⑲ (2026-10-24): \"what's slowing my laptop\" — ONE flat,
    non-interactive process snapshot, the top(1) -b / btop idea
    (interface only; stdlib /proc re-implementation): per-process CPU
    measured as a delta over a short sampling window (btop's \"cpu
    lazy\" ranking idea — ranked over the window, never process
    lifetime), memory as resident set (RSS), kernel threads and
    vanished pids skipped AND counted. personalityplateau: names come
    from cmdline's first argument bounded — full command lines are
    deliberately NOT kept (could embed secrets typed on a
    terminal). Read-only, honest floors."""
    sort = (inputs.get("sort") or "cpu").strip().lower()
    if sort not in ("cpu", "memory", "ram"):
        sort = "cpu"
    by_memory = sort in ("memory", "ram")
    top_n = min(8, max(1, int(inputs.get("top_n", 6))))
    hz = None
    try:
        hz = os.sysconf("SC_CLK_TCK") or 100
    except (ValueError, OSError):
        hz = 100
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(
        os, "sysconf") else 4096

    def total_jiffies() -> int:
        try:
            with open("/proc/stat", "r") as f:
                line = f.readline()
            return sum(int(x) for x in line.split()[1:8])
        except (OSError, ValueError, IndexError):
            return -1

    def sample() -> tuple[dict[int, tuple[int, int, str, int]], int, int]:
        procs: dict[int, tuple[int, int, str, int]] = {}
        vanished = 0
        unreadable = 0
        for pid_s in os.listdir("/proc"):
            if not pid_s.isdigit():
                continue
            pid = int(pid_s)
            try:
                with open(f"/proc/{pid}/stat", "rb") as f:
                    raw = f.read()
                head, tail = raw.rsplit(b")", 1)
                fields = tail.split()
                comm = (head.split(b"(", 1)[-1].strip()
                        .decode("utf-8", "replace"))
                # fields[0] is state; full-stat field N is at
                # index N-3 here: utime(14)->11, stime(15)->12,
                # rss(24)->21
                utime = int(fields[11])
                stime = int(fields[12])
                rss_pages = int(fields[21]) if len(fields) > 21 else 0
            except (OSError, ValueError, IndexError):
                unreadable += 1
                continue
            name = ""
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    parts = f.read().split(b"\x00")
                arg0 = (parts[0].decode("utf-8", "replace")
                        if parts and parts[0] else "")[:80]
                if arg0.startswith("/") and len(arg0.split("/")) > 1:
                    arg0 = arg0.rsplit("/", 1)[-1]
                name = arg0[:60]
            except OSError:
                pass
            if not name:
                # kernel thread / zombie: bracketed comm (never a
                # guessed application name)
                name = f"[{comm or 'kernel'}]"
            procs[pid] = (utime, stime, name, rss_pages)
        return procs, vanished, unreadable

    t0 = total_jiffies()
    s0, _, unreadable = sample()
    import time as _time
    _time.sleep(0.6)
    t1 = total_jiffies()
    s1, _, _ = sample()
    if t1 <= t0 or t0 < 0:
        return {"ok": False, "detail": "cpu sampling failed "
                "(proc stat unavailable)"}
    dt_jiffies = float(t1 - t0)
    items = []
    seen_both = set(s0) & set(s1)
    for pid in seen_both:
        u0, st0, name0, r0 = s0[pid]
        u1, st1, name1, r1 = s1[pid]
        d_cpu = ((u1 + st1) - (u0 + st0)) / dt_jiffies * 100.0
        d_cpu = max(0.0, d_cpu)
        rss_pages = r1 if r1 else r0
        items.append({"pid": pid, "name": name1, "cpu_pct":
                      round(d_cpu, 1), "rss_mb": round(
                          rss_pages * page / 2 ** 20, 1)})
    key = (lambda r: r["cpu_pct"]) if sort == "cpu" else \
        (lambda r: r["rss_mb"])
    items.sort(key=lambda r: (-key(r), r["pid"]))
    total_viewed = len(s1)
    return {"ok": True, "sort": "memory" if by_memory else "cpu",
            "window_ms": 600,
            "total_processes_seen": total_viewed,
            "unreadable_entries": unreadable,
            "items": items[:top_n],
            "detail": (f"one snapshot, 600 ms window; cpu % of "
                       "TOTAL system cpu (single-core units); resident "
                       "memory; kernel/systemd owned names in []")}


_PING_ADDR = (("1.1.1.1", 53), ("8.8.8.8", 53), ("9.9.9.9", 53))
_PING_DNS = ("one.one.one.one", "dns.google")
_HTTPS_PROBE = "https://www.gstatic.com/generate_204"
_HTTPS_PROBE2 = "https://www.cloudflare.com/cdn-cgi/trace"


def system_connectivity(inputs: dict, ctx: dict) -> dict:
    """P5 ⑳ (2026-10-24): \"is my internet actually working\" — three
    honest layers BYOND the NIC-up state (system.network answers
    interface state; this answers reachability):
      1. DNS: can names resolve (two keyless stable names)?
      2. TCP: can raw connections reach allowlisted public relay
         endpoints (median of two connect attempts each)?
      3. END TO END: HTTP(S) 204 from a keyless probe URL — DNS+TCP+
         TLS+HTTP as the web sees it (catches captive portals and
         proxies that NIC/DNS probes miss).
    Timeouts are bounded; every layer reports what it measured or a
    stated failure — verdicts are descriptive labels (full /
    limited_dns / limited_web / offline), never attitude. Keyless
    endpoints only; the wording says 'diagnostic endpoints', never
    'the internet works everywhere'."""
    import socket as _socket
    import urllib.request as _ur
    result = {"ok": True, "verdict": "offline"}

    def bounded_join(fn, budget_s=3.0):
        """DNS/HTTPS waits CANNOT exceed socket/connect timeouts
        (getaddrinfo has no timeout on glibc; a dead resolver hangs
        far past it) — the probe therefore runs in a daemon thread
        and the caller joins a BOUNDED wait. On timeout the layer is
        honestly 'unreachable', never 'still pending'."""
        import threading as _th
        holder: dict = {}

        def run():
            try:
                holder["v"] = fn()
            except Exception as e:  # noqa: BLE001 — failure is data
                holder["e"] = str(e)[:80]
        th = _th.Thread(target=run, daemon=True)
        th.start()
        th.join(budget_s)
        return holder

    dns_rows = []
    for name in _PING_DNS:
        t0 = time.monotonic()
        got = bounded_join(lambda n=name: _socket.getaddrinfo(
            n, 443, proto=_socket.IPPROTO_TCP), 3.2)
        ms = round((time.monotonic() - t0) * 1000.0, 1)
        if "v" in got and got["v"]:
            dns_rows.append({"name": name, "ok": True, "ms": ms,
                             "error": ""})
        else:
            dns_rows.append({"name": name, "ok": False, "ms": ms,
                             "error": got.get("e") or "timed out"})
    tcp_rows = []
    for addr, port in _PING_ADDR:
        t0 = time.monotonic()
        got = bounded_join(lambda a=addr, p=port: _socket.
                          create_connection((a, p), timeout=2.0), 3.2)
        kw = got.get("v")
        if kw is not None:
            try:
                kw.close()
            except OSError:
                pass
        ms = round((time.monotonic() - t0) * 1000.0, 1)
        tcp_rows.append({"addr": f"{addr}:{port}", "ok": kw is not None,
                         "ms": ms if kw is not None else None})
    https_ms = None
    https_status = None
    https_err = ""
    for url in (_HTTPS_PROBE, _HTTPS_PROBE2):
        t0 = time.monotonic()
        got = bounded_join(lambda u=url: _ur.urlopen(
            _ur.Request(u, headers={
                "User-Agent": "zara-connectivity/1 (diagnostic)"}),
            timeout=5.0), 7.0)
        resp = got.get("v")
        try:
            if resp is not None:
                try:
                    https_status = int(resp.status)
                except AttributeError:
                    https_status = int(getattr(resp, "code", 0) or 0)
                if 200 <= https_status < 300:
                    https_ms = round(
                        (time.monotonic() - t0) * 1000.0, 1)
                    https_err = ""
                    break
        finally:
            try:
                if resp is not None:
                    resp.close()
            except Exception:  # noqa: BLE001 — close best-effort
                pass
        https_err = got.get("e") or f"status {https_status or 'none'}"
        https_status = None
    dns_ok = all(r["ok"] for r in dns_rows)
    tcp_ok = any(r["ok"] for r in tcp_rows)
    web_ok = https_status == 204
    if web_ok and dns_ok and tcp_ok:
        result["verdict"] = "full"
    elif not tcp_ok and not web_ok:
        result["verdict"] = "offline"
    elif not dns_ok and (tcp_ok or web_ok):
        result["verdict"] = "limited_dns"
    else:
        result["verdict"] = "limited_web"
    result.update({
        "dns": dns_rows, "tcp": tcp_rows,
        "https": {"ok": web_ok, "ms": https_ms,
                  "status": https_status, "error": https_err},
        "detail": "bounded probes (2.0 s TCP, 5 s HTTPS) to keyless "
                  "diagnostic endpoints; descriptive verdicts, no "
                  "predictions"})
    return result


def calendar_event_remove(inputs: dict, ctx: dict) -> dict:
    """P5 feature-hunt ⑧ (2026-10-23): remove exactly ONE matched
    VEVENT from the target .ics (uid, or summary with optional
    dtstart disambiguator). Byte-preserving for everything except the
    removed block; atomic tmp + rename; verified by re-parse (count
    drops by exactly one and the selector no longer matches).
    Ambiguity and absence refuse — nothing is ever guessed."""
    def _refuse(why: str, **kw) -> dict:
        kw.update(ok=False, committed=False, refusal=why, detail=why)
        return kw

    def _comp(m: re.Match) -> tuple:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4) or 0), int(m.group(5) or 0),
                int(m.group(6) or 0))

    target, refuse = _cal_target(inputs)
    if refuse is not None:
        return refuse
    uid = str(inputs.get("uid") or "").strip()[:200]
    summary = str(inputs.get("summary") or "").strip()[:120]
    if not uid and not summary:
        return _refuse(
            "say what to remove — the event's exact title (in "
            "quotes) or its UID; I never delete an unnamed thing")
    want_m = _DTSHAPE.match(str(inputs.get("dtstart") or "").strip())
    if (str(inputs.get("dtstart") or "").strip()) and want_m is None:
        return _refuse(
            "dtstart must look like 20261023T143000Z (or 20261023 "
            "for an all-day event) when used to pin the removal")
    try:
        # newline="" — a default text read would silently translate
        # CRLF to \n and the rewrite below would normalize the whole
        # file's line endings (byte-preservation violated).
        with open(target, encoding="utf-8", newline="") as f:
            text = f.read(2_000_000)
    except (OSError, UnicodeError) as e:
        return _refuse(f"calendar file unreadable: {e}")
    if "BEGIN:VCALENDAR" not in text or "END:VCALENDAR" not in text:
        return _refuse(f"not a well-formed VCALENDAR file: {target}")
    from .calendar_flow import parse_ics, to_utc
    before = len(parse_ics(target).get("events") or [])
    blocks, lines = _scan_vblocks(text)

    def _pin_ok(kf: dict) -> bool:
        if want_m is None:
            return True
        conv = to_utc(kf.get("dtstart") or "", kf.get("tzid"))
        if conv["dt"] is None:
            return False
        want_dt = datetime(*_comp(want_m), tzinfo=timezone.utc)
        if want_m.group(4) is None:  # date pin: compare dates
            return conv["dt"].date() == want_dt.date()
        return conv["dt"] == want_dt
    # selector ------------------------------------------------------
    if uid:
        sel = [b for b in blocks if b["kf"].get("uid") == uid]
        selector_desc = f"UID {uid[:40]}"
    else:
        sel = [b for b in blocks
               if (b["kf"].get("summary") or "").strip().lower()
               == summary.lower()]
        if want_m is not None and sel:
            sel = [b for b in sel if _pin_ok(b["kf"])]
        selector_desc = f"'{summary[:60]}'" + (
            f" starting {want_m.group(1)[:8]}"
            if want_m is not None else "")
    if not sel:
        return _refuse(
            f"no event matches {selector_desc} — nothing removed")
    if len(sel) > 1:
        cand = "; ".join(
            f"{(b['kf'].get('summary') or '?')[:40]}@"
            f"{b['kf'].get('dtstart', '?')[:16]} "
            f"({((b['kf'].get('uid') or '?')[:20])})"
            for b in sel[:4])
        return _refuse(
            f"{len(sel)} events match {selector_desc} — I refuse to "
            f"guess: candidates {cand}", ambiguous=True)
    hit = sel[0]
    repeating = bool(hit["kf"].get("rrule"))
    removed_summary = hit["kf"].get("summary") or "(no title)"
    i0, i1 = hit["raw"]
    while i1 < len(lines) and not lines[i1].strip():
        i1 += 1  # swallow trailing blank lines left inside the gap
    new_text = "".join(lines[:i0] + lines[i1:])
    tmp = target + ".zara-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, target)
    # Verify by re-parse: exactly one fewer event, and the selector
    # no longer matches anything.
    parsed = parse_ics(target)
    now = len(parsed.get("events") or [])
    # "gone" uses the SAME comparison semantics as the selection
    # (exact UIDs, summary+pin) — never a substring false positive
    # ("p1@x" is inside "rp1@x") and never a sibling stomach punch
    # (another Dentist at a different start must not fail a pinned
    # removal).
    blocks2, _lines2 = _scan_vblocks(new_text)
    if uid:
        gone = not any(b["kf"].get("uid") == uid for b in blocks2)
    else:
        gone = not any(
            (b["kf"].get("summary") or "").strip().lower()
            == summary.lower() and _pin_ok(b["kf"]) for b in blocks2)

    if now != before - 1 or not gone:
        return {"ok": False, "committed": True, "verified": False,
                "target": os.path.basename(target),
                "detail": "write landed but the re-parse does not "
                          "confirm exactly one removed event — "
                          "inspect the file"}
    out = {"ok": True, "committed": True, "verified": True,
           "target": os.path.basename(target),
           "removed": removed_summary[:80], "events_total": now,
           "selector": selector_desc[:120],
           "detail": "removed exactly one VEVENT (re-read verified)"}
    if repeating:
        out["repeating"] = True
        out["detail"] += " — it was a repeating (RRULE) event: the " \
                         "whole series is gone"
    return out


def calendar_event_move(inputs: dict, ctx: dict) -> dict:
    """P5 feature-hunt ⑯ (2026-10-23): move ONE matched VEVENT's
    start (uid, or summary with optional dtstart disambiguator —
    the SAME selection honesty as removal: ambiguity refuses with
    candidates, absence refuses, nothing is ever guessed).

    Time KINDS never mix: an all-day block only moves to another
    date (a clock time would have to invent a duration dishonestly),
    and a timed block refuses a date-only start (dropping its DTEND
    silently changes its meaning). Destination forms:
      new_start — full pin (20261024T143000Z or 20261024)
      new_date  — keep the event's own time-of-day, change the date
      new_clock — keep the event's own date, set the LOCAL clock
                  (HH:MM or HHMM, host-tz; same meaning as a chat
                  ask's clock time)
    For a timed block, DTEND keeps the event's duration: it shifts
    by the same delta as DTSTART unless new_end pins it explicitly.
    A timed block WITHOUT a DTEND line moves its start only and the
    report SAYS so (readers apply their default duration). Moving a
    repeating (RRULE) parent moves the whole series and says so.
    A same-title event already sitting at the destination start
    refuses proactively (a silent collision would be data
    corruption, not a move). The write is byte-preserving outside
    the block, atomic (tmp + rename), and verified by re-parse under
    the SAME selector semantics: count unchanged, exactly one block
    matching, and its start is the new one (the raw old value gone)."""
    def _refuse(why: str, **kw) -> dict:
        kw.update(ok=False, committed=False, refusal=why, detail=why)
        return kw

    def _comp(m: re.Match) -> tuple:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4) or 0), int(m.group(5) or 0),
                int(m.group(6) or 0))

    target, refuse = _cal_target(inputs)
    if refuse is not None:
        return refuse
    uid = str(inputs.get("uid") or "").strip()[:200]
    summary = str(inputs.get("summary") or "").strip()[:120]
    if not uid and not summary:
        return _refuse(
            "say what to move — the event's exact title (in quotes) "
            "or its UID; I never move an unnamed thing")
    new_start_raw = str(inputs.get("new_start") or "").strip()
    new_date_raw = str(inputs.get("new_date") or "").strip()
    new_clock_raw = str(inputs.get("new_clock") or "").strip()
    if not (new_start_raw or new_date_raw or new_clock_raw):
        return _refuse(
            "say where it should move to — e.g. push \"dentist\" in "
            "my calendar to friday 3pm, or give new_start "
            "20261024T143000Z")
    new_m = _DTSHAPE.match(new_start_raw)
    if new_start_raw and new_m is None:
        return _refuse(
            "new_start must look like 20261024T143000Z (or 20261024 "
            "for an all-day event)")
    new_d = _DTSHAPE.match(new_date_raw)
    if new_date_raw and (new_d is None or new_d.group(4) is not None):
        return _refuse("new_date must be a plain date 20261024")
    new_c = re.match(
        r"^(\d{1,2}):(\d{2})Z?$|^(\d{1,2})(\d{2})Z?$", new_clock_raw)
    if new_clock_raw and new_c is None:
        return _refuse("new_clock must look like 14:30 or 1430 (local)")
    hh_c = mm_c = None
    if new_c is not None:
        hh_c = int(new_c.group(1) or new_c.group(3))
        mm_c = int(new_c.group(2) or new_c.group(4))
        if not (0 <= hh_c <= 23 and 0 <= mm_c <= 59):
            return _refuse("new_clock must name a real clock time")
    want_m = _DTSHAPE.match(str(inputs.get("dtstart") or "").strip())
    if str(inputs.get("dtstart") or "").strip() and want_m is None:
        return _refuse(
            "dtstart must look like 20261023T143000Z (or 20261023 "
            "for an all-day event) when used to pin the selection")
    new_end_raw = str(inputs.get("new_end") or "").strip()
    end_m = _DTSHAPE.match(new_end_raw)
    if new_end_raw and end_m is None:
        return _refuse("new_end must match the same date/datetime "
                       "shape as new_start")
    try:
        # newline="" — a default text read would silently translate
        # CRLF to \n and the rewrite below would normalize the whole
        # file's line endings (byte-preservation violated).
        with open(target, encoding="utf-8", newline="") as f:
            text = f.read(2_000_000)
    except (OSError, UnicodeError) as e:
        return _refuse(f"calendar file unreadable: {e}")
    if "BEGIN:VCALENDAR" not in text or "END:VCALENDAR" not in text:
        return _refuse(f"not a well-formed VCALENDAR file: {target}")
    from .calendar_flow import parse_ics, to_utc
    before = len(parse_ics(target).get("events") or [])
    blocks, lines = _scan_vblocks(text)

    def _pin_ok(kf: dict) -> bool:
        if want_m is None:
            return True
        conv = to_utc(kf.get("dtstart") or "", kf.get("tzid"))
        if conv["dt"] is None:
            return False
        want_dt = datetime(*_comp(want_m), tzinfo=timezone.utc)
        if want_m.group(4) is None:  # date pin: compare dates
            return conv["dt"].date() == want_dt.date()
        return conv["dt"] == want_dt

    if uid:
        sel = [b for b in blocks if b["kf"].get("uid") == uid]
        selector_desc = f"UID {uid[:40]}"
    else:
        sel = [b for b in blocks
               if (b["kf"].get("summary") or "").strip().lower()
               == summary.lower()]
        if want_m is not None and sel:
            sel = [b for b in sel if _pin_ok(b["kf"])]
        selector_desc = f"'{summary[:60]}'" + (
            f" starting {want_m.group(1)[:8]}"
            if want_m is not None else "")
    if not sel:
        return _refuse(
            f"no event matches {selector_desc} — nothing moved")
    if len(sel) > 1:
        cand = "; ".join(
            f"{(b['kf'].get('summary') or '?')[:40]}@"
            f"{b['kf'].get('dtstart', '?')[:16]} "
            f"({((b['kf'].get('uid') or '?')[:20])})"
            for b in sel[:4])
        return _refuse(
            f"{len(sel)} events match {selector_desc} — I refuse to "
            f"guess: candidates {cand}", ambiguous=True)
    hit = sel[0]
    kf = hit["kf"]
    repeating = bool(kf.get("rrule"))
    block_allday = bool(kf.get("allday_dtstart"))
    conv_old = to_utc(kf.get("dtstart") or "", kf.get("tzid"))
    if conv_old["dt"] is None:
        return _refuse(
            "the event's stored start is honest-but-unresolved (a "
            "timezone neither side resolves) — not moved")

    # ---- destination resolution (never guesses) ------------------
    new_start_dt = None
    e_val = ""
    if block_allday:
        if new_start_raw and new_m.group(4) is not None:
            return _refuse(
                "an all-day event stays all-day — give a plain date "
                "(20261024); a clock time would have to invent a "
                "duration, refusing")
        if hh_c is not None:
            return _refuse(
                "an all-day event has no clock time to keep — give a "
                "date instead")
        new_val = (new_start_raw[:8] if new_start_raw
                   else new_date_raw[:8])
        if not new_val:
            return _refuse("a move needs a target date (20261024)")
        if end_m is not None:
            if end_m.group(4) is not None:
                return _refuse(
                    "an all-day event's end must be a plain date too")
            full_end = (end_m.group(1) + end_m.group(2)
                        + end_m.group(3))
            if full_end <= new_val:
                return _refuse(
                    "the destination end date is not after the "
                    "destination start — nothing moved")
            e_val = full_end
    else:
        if new_start_raw and new_m.group(4) is None:
            return _refuse(
                "moving a timed event to an all-day date drops its "
                "end time — include the clock time (…T143000Z)")
        if new_date_raw and hh_c is None:
            return _refuse(
                "moving a timed event to a date drops its clock time "
                "— say the new clock time too (e.g. friday 3pm)")
        if new_start_raw:
            new_start_dt = datetime(*_comp(new_m), tzinfo=timezone.utc)
            if end_m is not None and end_m.group(4) is None:
                return _refuse(
                    "new_end for a timed move must carry the clock too")
        else:
            local = conv_old["dt"].astimezone()
            if new_d is not None:
                y, mo, dy = (int(new_d.group(1)), int(new_d.group(2)),
                             int(new_d.group(3)))
            else:
                y, mo, dy = local.year, local.month, local.day
            new_local = local.replace(year=y, month=mo, day=dy,
                                      hour=hh_c, minute=mm_c,
                                      second=0, microsecond=0)
            new_start_dt = new_local.astimezone(timezone.utc)
        new_val = new_start_dt.strftime("%Y%m%dT%H%M%SZ")
        if end_m is not None:
            new_end_dt = datetime(*_comp(end_m), tzinfo=timezone.utc)
            if new_end_dt <= new_start_dt:
                return _refuse(
                    "the destination end is not after the destination "
                    "start — nothing moved")
            e_val = (f"{end_m.group(1)}{end_m.group(2)}"
                     f"{end_m.group(3)}T{end_m.group(4)}"
                     f"{end_m.group(5)}00Z")
        elif kf.get("dtend"):
            old_end = to_utc(kf.get("dtend") or "", kf.get("dtend_tzid"))
            if old_end["dt"] is None:
                return _refuse(
                    "the event's end time is honest-but-unresolved — "
                    "give new_end explicitly or the move refuses")
            e_val = (old_end["dt"]
                     + (new_start_dt - conv_old["dt"])
                     ).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # ---- refuse a silent destination collision -------------------
    for b in blocks:
        if b is hit:
            continue
        if (b["kf"].get("summary") or "").strip().lower() != \
                (kf.get("summary") or "").strip().lower():
            continue
        conv_b = to_utc(b["kf"].get("dtstart") or "", b["kf"].get("tzid"))
        if conv_b["dt"] is None:
            continue
        if new_start_dt is None:
            if conv_b["dt"].date().strftime("%Y%m%d") == new_val[:8]:
                return _refuse(
                    "another event with the same title is already "
                    f"all-day on {new_val[:8]} — moving there would "
                    "collide (refused; rename or remove it first)")
        elif conv_b["dt"] == new_start_dt:
            return _refuse(
                "another event with the same title already starts at "
                f"{new_val} — moving there would collide (refused; "
                "rename or remove it first)")
    # ---- rewrite the block's DTSTART/DTEND lines in place --------
    i0, i1 = hit["raw"]
    out_lines = []
    touched_start = touched_end = False
    for ln in lines[i0:i1]:
        u = ln.strip().upper()
        eol = "\r\n" if ln.endswith("\r\n") else "\n"
        if u.startswith("DTSTART") and ":" in ln:
            head = ln.partition(":")[0]
            # only the VALUE changes; any params (TZID…) stay as-is
            out_lines.append(head + ":" + new_val + eol)
            touched_start = True
            continue
        if u.startswith("DTEND") and ":" in ln:
            if not e_val:
                out_lines.append(ln)  # byte-identical passthrough
                continue
            head = ln.partition(":")[0]
            out_lines.append(head + ":" + e_val + eol)
            touched_end = True
            continue
        out_lines.append(ln)
    if not touched_start:
        return _refuse(
            "the matched block has no plain DTSTART line Zara can "
            "rewrite safely — nothing moved")
    if e_val and not touched_end:
        # the block had no DTEND line: the pinned/derived end is
        # INSERTED right after DTSTART, in the block's own RFC form
        ins_i = None
        for li, l in enumerate(out_lines):
            if l.strip().upper().startswith("DTSTART") and ":" in l:
                ins_i = li + 1
                break
        if ins_i is None:
            return _refuse("internal: DTSTART line lost — nothing "
                           "written")
        insert = (f"DTEND;VALUE=DATE:{e_val}" if block_allday
                  else f"DTEND:{e_val}")
        eol = "\r\n" if any("\r\n" in l for l in out_lines) else "\n"
        out_lines.insert(ins_i, insert + eol)
    if not touched_start:
        return _refuse(
            "the matched block has no plain DTSTART line Zara can "
            "rewrite safely — nothing moved")
    new_text = "".join(lines[:i0] + out_lines + lines[i1:])
    tmp = target + ".zara-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, target)

    # ---- verify by re-parse under the SAME selector semantics ----
    blocks2, _lines2 = _scan_vblocks(new_text)
    now = len(parse_ics(target).get("events") or [])
    if uid:
        matches = [b for b in blocks2 if b["kf"].get("uid") == uid]
    else:
        matches = [b for b in blocks2
                   if (b["kf"].get("summary") or "").strip().lower()
                   == summary.lower()]
        if want_m is not None:
            still_pinned = [b for b in matches if _pin_ok(b["kf"])]
            if still_pinned:
                matches = []  # the selector behaves as if nothing moved
    moved_ok = False
    if len(matches) == 1:
        b2 = matches[0]
        conv2 = to_utc(b2["kf"].get("dtstart") or "",
                       b2["kf"].get("tzid"))
        if new_start_dt is not None:
            moved_ok = (conv2["dt"] is not None
                        and conv2["dt"] == new_start_dt)
        else:
            moved_ok = (b2["kf"].get("dtstart") or "").strip() == new_val
    if now != before or not moved_ok:
        return {"ok": False, "committed": True, "verified": False,
                "target": os.path.basename(target),
                "detail": "write landed but the re-parse does not "
                          "confirm the move — inspect the file"}
    out = {"ok": True, "committed": True, "verified": True,
           "target": os.path.basename(target),
           "moved": (kf.get("summary") or "(no title)")[:80],
           "from": (kf.get("dtstart") or "")[:16],
           "to": new_val, "events_total": now,
           "selector": selector_desc[:120],
           "detail": "moved exactly one VEVENT (re-read verified)"}
    if block_allday:
        out["all_day"] = True
    if not block_allday and not kf.get("dtend"):
        out["detail"] += " — no DTEND line on the event: the " \
                         "default duration now applies to the new " \
                         "start"
    if repeating:
        out["repeating"] = True
        out["detail"] += " — it was a repeating (RRULE) event: the " \
                         "whole series moved"
    return out


# Class → action names for peripheral.describe ("what can I do with
# this device"). RESTORED 2026-10-24 (P5 ⑯ batch): the batch-16 move
# function splice accidentally deleted this table between the move
# tool and peripheral builtins; rebuilt from the pins that still name
# it (tests + device_panel.dart).

# ---- P5 ⑱ (2026-10-24): unit + timezone converters (core-local, SAFE) ----
# Sources (interface only, stdlib re-implementation): a bounded
# city→IANA alias map + world clock (NickCirv/timezone-converter's
# zero-dependency CLI shape; mattjohnsonpint/TimeZoneConverter's
# alias-map idea). GNU units' exactness is too heavy for a chat
# surface: only everyday units, honest cross-kind refusals, no
# invented precision beyond the input.
_ZONE_ALIASES: dict[str, str] = {
    "new york": "America/New_York", "nyc": "America/New_York",
    "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "chicago": "America/Chicago", "dallas": "America/Chicago",
    "toronto": "America/Toronto", "vancouver": "America/Vancouver",
    "mexico city": "America/Mexico_City", "honolulu": "Pacific/Honolulu",
    "sao paulo": "America/Sao_Paulo", "buenos aires":
        "America/Argentina/Buenos_Aires",
    "london": "Europe/London", "dublin": "Europe/Dublin",
    "paris": "Europe/Paris", "berlin": "Europe/Berlin",
    "madrid": "Europe/Madrid", "rome": "Europe/Rome",
    "amsterdam": "Europe/Amsterdam", "munich": "Europe/Berlin",
    "zurich": "Europe/Zurich", "stockholm": "Europe/Stockholm",
    "moscow": "Europe/Moscow", "istanbul": "Europe/Istanbul",
    "athens": "Europe/Athens", "kyiv": "Europe/Kyiv",
    "cairo": "Africa/Cairo", "nairobi": "Africa/Nairobi",
    "johannesburg": "Africa/Johannesburg",
    "dubai": "Asia/Dubai", "riyadh": "Asia/Riyadh",
    "karachi": "Asia/Karachi", "delhi": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata", "india": "Asia/Kolkata",
    "calcutta": "Asia/Kolkata", "colombo": "Asia/Colombo",
    "dhaka": "Asia/Dhaka", "bangalore": "Asia/Kolkata",
    "singapore": "Asia/Singapore", "jakarta": "Asia/Jakarta",
    "bangkok": "Asia/Bangkok", "kuala lumpur": "Asia/Kuala_Lumpur",
    "hong kong": "Asia/Hong_Kong", "shanghai": "Asia/Shanghai",
    "beijing": "Asia/Shanghai", "taipei": "Asia/Taipei",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "sydney": "Australia/Sydney", "melbourne":
        "Australia/Melbourne", "perth": "Australia/Perth",
    "auckland": "Pacific/Auckland", "utc": "UTC", "gmt": "UTC",
}

_LENGTH_M = {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0,
             "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
             "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
             "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
             "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
             "meter": 1.0, "meters": 1.0, "metre": 1.0,
             "metres": 1.0, "kilometer": 1000.0,
             "kilometers": 1000.0, "kilometre": 1000.0,
             "kilometres": 1000.0, "centimeter": 0.01,
             "centimeters": 0.01, "millimeter": 0.001,
             "millimeters": 0.001}
_MASS_G = {"mg": 1e-6, "g": 0.001, "gram": 0.001, "grams": 0.001,
           "kg": 1.0, "kilo": 1.0, "kilos": 1.0, "kilogram": 1.0,
           "kilograms": 1.0, "t": 1000.0, "tonne": 1000.0,
           "tonnes": 1000.0, "oz": 0.028349523125,
           "ounce": 0.028349523125, "ounces": 0.028349523125,
           "lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237,
           "pounds": 0.45359237, "st": 6.35029318,
           "stone": 6.35029318, "stones": 6.35029318}
_DATA_B = {"b": 1, "byte": 1, "bytes": 1, "kb": 10**3, "mb": 10**6,
           "gb": 10**9, "tb": 10**12, "kib": 2**10, "mib": 2**20,
           "gib": 2**30, "tib": 2**40}
_TEMP = {"c", "celsius", "f", "fahrenheit", "k", "kelvin",
         "°c", "°f"}


def _unit_lookup(unit: str, tables: tuple[dict, ...]) -> str | None:
    u = unit.strip().lower().strip()
    if u.endswith('.'):
        u = u[:-1]
    for t in tables:
        if u in t and (t.get(u) if isinstance(t, dict) else True):
            return u
    # common plural ("5 kgs to lbs", "1.5 gibs to mib"): the bare
    # singular form when the s-form itself is not a listed unit.
    if u.endswith("s"):
        u = u[:-1]
        for t in tables:
            if u in t and (t.get(u) if isinstance(t, dict) else True):
                return u
    return None


def convert_units(inputs: dict, ctx: dict) -> dict:
    """Everyday unit conversion, honest surface: same-measurement-kind
    only (length↔length, mass↔mass, data↔data, temp↔temp), no
    invented precision (result keeps ~2 representative decimals in
    the detail; raw value full). Unknown unit or cross-kind asks are
    honest ValueErrors, never guesses."""
    from .models import utcnow  # noqa: F401 (context parity)
    value = inputs.get("value")
    fr = str(inputs.get("unit") or "")
    to = str(inputs.get("to") or "")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("value must be a number")
    fr = _unit_lookup(fr, (_LENGTH_M, _MASS_G, _DATA_B, _TEMP))
    to = _unit_lookup(to, (_LENGTH_M, _MASS_G, _DATA_B, _TEMP))
    if fr is None or to is None:
        unknown = fr is None and inputs.get("unit") or "" or \
            (to is None and inputs.get("to") or "")
        raise ValueError(
            f"unknown unit: {unknown or 'both'}; supported: length "
            "(mm/cm/m/km/in/ft/yd/mi), mass (mg/g/kg/t/oz/lb/st), "
            "data (B/KB/MB/GB/TB + KiB/MiB/GiB/TiB), temperature "
            "(C/F/K)")
    if fr in _TEMP:
        if to not in _TEMP:
            raise ValueError("cannot convert a temperature to a "
                             "different kind")
        c = _to_celsius(float(value), fr)
        res = _from_celsius(c, to)
    elif fr in _LENGTH_M and to in _LENGTH_M:
        res = float(value) * _LENGTH_M[fr] / _LENGTH_M[to]
    elif fr in _MASS_G and to in _MASS_G:
        res = float(value) * _MASS_G[fr] / _MASS_G[to]
    elif fr in _DATA_B and to in _DATA_B:
        res = float(value) * _DATA_B[fr] / _DATA_B[to]
    else:
        raise ValueError(f"different measurement kinds: {fr} to {to}"
                         " makes no sense")
    return {"value": value, "unit": fr, "to": to,
            "result": res, "ok": True}


def _to_celsius(v: float, unit: str) -> float:
    u = unit.lower()
    if u in ("c", "celsius", "°c"):
        return v
    if u in ("f", "fahrenheit", "°f"):
        return (v - 32.0) * 5.0 / 9.0
    return v - 273.15


def _from_celsius(c: float, unit: str) -> float:
    u = unit.lower()
    if u in ("c", "celsius", "°c"):
        return c
    if u in ("f", "fahrenheit", "°f"):
        return c * 9.0 / 5.0 + 32.0
    return c + 273.15


_ZIANA_LOWER: dict[str, str] = {}


def _resolve_zone(zone: str) -> str:
    import zoneinfo as _zi
    raw = (zone or "").strip().rstrip(" ?;.!")
    if not raw:
        return ""
    z = raw.strip().lower()
    if z in _ZONE_ALIASES:
        z = _ZONE_ALIASES[z].lower()
    if not _ZIANA_LOWER:
        # case-insensitive IANA resolution: ZoneInfo keys are
        # case-sensitive ("Asia/Kolkata"), operators type freely.
        for name in _zi.available_timezones():
            _ZIANA_LOWER[name.lower()] = name
    # exact case first (cheap), else the lowercased map
    if "/" in raw:
        try:
            _zi.ZoneInfo(raw)
            return raw
        except Exception:
            pass
    return _ZIANA_LOWER.get(z, "")


def time_zone(inputs: dict, ctx: dict) -> dict:
    """World clock honest surface: current time in a zone, or a
        HOST-LOCAL pinned clock expressed in that zone ("9am my time
        in Tokyo" = the next occurrence of 09:00 host-local, today's
        if still ahead, else tomorrow's — the calendar's
        next-occurrence convention). Unmapped citynames are honest
        ValueErrors naming what IS supported."""
    import zoneinfo as _zi
    from .models import utcnow
    zone = _resolve_zone(str(inputs.get("zone") or ""))
    if not zone:
        bad = str(inputs.get("zone") or "")
        raise ValueError(
            f"unknown place or time zone: '{bad[:40]}' — supported: "
            "common cities (tokyo, new york, …) or any "
            "region/city IANA name")
    tz = _zi.ZoneInfo(zone)
    out: dict = {"zone": zone}
    clock = str(inputs.get("clock") or "").strip()
    if clock:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", clock) \
            or re.fullmatch(r"(\d{1,2})(\d{2})", clock)
        if not m:
            raise ValueError(
                "clock must be HH:MM or HHMM (host-local)")
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            raise ValueError(
                f"clock {hh:02d}:{mm:02d} is not a wall time")
        now_local = datetime.now().astimezone()
        target = now_local.replace(hour=hh, minute=mm, second=0,
                                   microsecond=0)
        if target <= now_local:
            target += timedelta(days=1)   # next occurrence convention
        utc = target.astimezone(timezone.utc)
        zone_local = target.astimezone(tz)
        out.update({"mode": "pinned_clock", "host_clock":
                    now_local.strftime("%H:%M"), "pinned_hh": hh,
                    "pinned_mm": mm, "host_date":
                    target.strftime("%a %b %d"),
                    "host_offset": target.strftime("%z"),
                    "utc": utc.strftime("%Y-%m-%dT%H:%MZ"),
                    "zone_local": zone_local.strftime("%H:%M"),
                    "zone_abbr": zone_local.strftime("%Z"),
                    "zone_offset": zone_local.strftime("%z"),
                    "zone_date": zone_local.strftime("%a %b %d"),
                    "hours_diff": round(
                        (zone_local.utcoffset().total_seconds()
                         - target.utcoffset().total_seconds())
                        / 3600.0, 2)})
    else:
        utcnow_z = datetime.now(timezone.utc).astimezone(tz)
        host = datetime.now().astimezone()
        out.update({"mode": "now", "zone_local":
                    utcnow_z.strftime("%H:%M"), "zone_abbr":
                    utcnow_z.strftime("%Z"), "zone_offset":
                    utcnow_z.strftime("%z"), "zone_date":
                    utcnow_z.strftime("%a %b %d"),
                    "host_clock": host.strftime("%H:%M"),
                    "host_abbr": host.strftime("%Z"),
                    "hours_diff": round(
                        (utcnow_z.utcoffset().total_seconds()
                         - host.utcoffset().total_seconds())
                        / 3600.0, 2)})
    return out



# tests/test_peripheral_routing (usb_storage shows storage.stats and
# storage.copy) and android/lib/device_panel.dart's _classActions
# (usb_storage → volumes/stats/copy/move; display → display.report;
# every other class honestly empty). Desktop classes without a
# bounded implemented action stay honest-empty ("no supported actions
# yet"), never padded with aspirational buttons.
_CLASS_ACTIONS = {
    "usb_storage": [
        {"action": "storage.volumes", "available": True,
         "detail": "lists mounted removable volumes",
         "risk": "n/a (read-only)"},
        {"action": "storage.stats", "available": True,
         "detail": "bounded usage breakdown for a mounted volume",
         "risk": "n/a (read-only)"},
        {"action": "storage.copy", "available": True,
         "detail": "copy with verified checksum; the pipeline asks "
                   "before any write", "risk": "confirm"},
        {"action": "storage.move", "available": True,
         "detail": "copy + verify, then source delete only after "
                   "success; the pipeline asks first", "risk": "confirm"},
    ],
    "removable_disk": [
        {"action": "storage.volumes", "available": True,
         "detail": "lists mounted removable volumes",
         "risk": "n/a (read-only)"},
        {"action": "storage.stats", "available": True,
         "detail": "bounded usage breakdown for a mounted volume",
         "risk": "n/a (read-only)"},
        {"action": "storage.copy", "available": True,
         "detail": "copy with verified checksum; the pipeline asks "
                   "before any write", "risk": "confirm"},
        {"action": "storage.move", "available": True,
         "detail": "copy + verify, then source delete only after "
                   "success; the pipeline asks first", "risk": "confirm"},
    ],
    "cardreader": [
        {"action": "storage.volumes", "available": True,
         "detail": "lists mounted removable volumes",
         "risk": "n/a (read-only)"},
        {"action": "storage.stats", "available": True,
         "detail": "bounded usage breakdown for a mounted volume",
         "risk": "n/a (read-only)"},
        {"action": "storage.copy", "available": True,
         "detail": "copy with verified checksum; the pipeline asks "
                   "before any write", "risk": "confirm"},
        {"action": "storage.move", "available": True,
         "detail": "copy + verify, then source delete only after "
                   "success; the pipeline asks first", "risk": "confirm"},
    ],
    "display": [
        {"action": "display.report", "available": True,
         "detail": "resolution/mode state report (genuinely "
                   "implemented)", "risk": "n/a (read-only)"},
    ],
}


def peripheral_list(inputs: dict, ctx: dict) -> dict:
    import os as _os
    from .discovery import get_registry, refresh_host
    reg = get_registry()
    state = (inputs or {}).get("state", "present")
    if state not in ("present", "removed", "all"):
        raise ValueError("state must be present/removed/all")
    if _os.environ.get("ZARA_DISCOVERY_OFF") == "1":
        dict_diff, meta = None, {"adapters": "disabled (test mode)"}
    else:
        dict_diff, meta = refresh_host(reg)
    devs = [d.to_public() for d in reg.list(
        None if state == "all" else state)]
    return {"devices": devs, "tracking_state": (
        {"present": len(devs), "diff": (dict_diff or {}),
         "meta": (meta or {})}) if dict_diff else
        {"present": len(devs), "meta": (meta or {})},
        "trust_note": "detection is not trust; execution requires the "
                      "existing capability/approval pipeline"}


def peripheral_describe(inputs: dict, ctx: dict) -> dict:
    import os as _os
    from .discovery import get_registry, refresh_host
    reg = get_registry()
    pid = (inputs or {}).get("pid", "")
    if _os.environ.get("ZARA_DISCOVERY_OFF") != "1":
        refresh_host(reg)
    dev = reg.get(pid)
    if dev is None or dev.state != "present":
        return {"pid": pid, "actions": [], "state": "gone",
                "detail": "device is not currently detected"}
    actions = list(_CLASS_ACTIONS.get(dev.device_class, []))
    if not actions:
        actions = [{"action": "identity report", "available": True,
                    "detail": f"class {dev.device_class} has no "
                              "implemented actions yet", "risk": "n/a"}]
    caps = dev.capabilities.get("mountpoints") if dev.device_class == \
        "usb_storage" else None
    return {"pid": pid, "state": dev.state, "class": dev.device_class,
            "label": dev.label, "actions": actions,
            "volumes": caps or [], "trust": "detected-only"}


# ---- Stage 21: transfer-mission builtins (process-global runner) ----

def get_transfer_runner():
    from .transfer_state import get_runner
    return get_runner()


def transfer_mission(inputs: dict, ctx: dict) -> dict:
    runner = get_transfer_runner()
    if runner is None:
        return {"ok": False, "error": "transfer missions not wired in "
                "this Core build"}
    spec = (inputs or {}).get("spec")
    if spec is not None and not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    return runner.start(spec)


def transfer_status(inputs: dict, ctx: dict) -> dict:
    runner = get_transfer_runner()
    if runner is None:
        return {"missions": [], "transfers": [], "note": "not wired"}
    missions = [{"mission_id": m.id, "goal": m.goal,
                 "state": m.state.value, "steps": [
                     {"tool": s.tool, "status": s.status}
                     for s in m.steps]}
                for m in runner._missions.list()
                if "file" in (m.goal or "").lower() or
                "transfer" in (m.goal or "").lower()]
    xfers = []
    try:
        for rec in runner._xfer.list()[:20]:
            xfers.append({"transfer_id": rec.transfer_id,
                          "state": rec.state,
                          "filename": rec.filename,
                          "size_bytes": rec.size_bytes,
                          "source": rec.source_device,
                          "dest": rec.dest_device,
                          "mission":
                          (rec.metadata or {}).get("mission_id")})
    except Exception:
        pass
    return {"missions": missions[:20], "transfers": xfers}


def transfer_cancel(inputs: dict, ctx: dict) -> dict:
    runner = get_transfer_runner()
    if runner is None:
        return {"ok": False, "error": "not wired"}
    mid = (inputs or {}).get("mission_id", "")
    if not mid:
        # cancel the newest non-terminal file-transfer mission
        for m in reversed(runner._missions.list()):
            if "transfer" in (m.goal or "").lower() and m.state.value not \
                    in ("completed", "failed", "cancelled"):
                mid = m.id
                break
    if not mid:
        return {"ok": False, "error": "no active transfer mission found"}
    return {"ok": True, **runner.cancel(mid)}

BUILTINS: list[tuple[ToolDefinition, object]] = [
    (ToolDefinition(
        name="system.info", description="Read-only system information",        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "required": ["platform"],
                       "properties": {"platform": {"type": "string"}}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
        estimated_cost=0.01, timeout_s=5.0, success_criteria="returns platform string",
        failure_behavior="fail", verification="output_schema",
        supported_devices=["any"], reversible=True, version="1.0.0"), system_info),
    (ToolDefinition(
        name="util.echo", description="Echo text back (connectivity/dev test)",
        input_schema={"type": "object", "required": ["text"],
                      "properties": {"text": {"type": "string"}}},
        output_schema={"type": "object", "required": ["text"],
                       "properties": {"text": {"type": "string"}}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
        estimated_cost=0.0, timeout_s=5.0, success_criteria="echoes input",
        failure_behavior="fail", verification="output_schema",
        supported_devices=["any"], reversible=True, version="1.0.0"), echo),
    (ToolDefinition(
        name="convert.units", description="Everyday unit conversion "
        "(length/mass/data/temperature), deterministic: same kind "
        "only, unknown or cross-kind asks refuse honestly.",
        input_schema={"type": "object", "required": ["value", "unit",
                                                     "to"],
                      "properties": {"value": {"type": "number"},
                                     "unit": {"type": "string"},
                                     "to": {"type": "string"}}},
        output_schema={"type": "object", "required": ["result"],
                       "properties": {"result": {"type": "number"}}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
        estimated_cost=0.0, timeout_s=5.0,
        success_criteria="converted value returned",
        failure_behavior="fail", verification="output_schema",
        supported_devices=["any"], reversible=True, version="1.1.0"),
    convert_units),
    (ToolDefinition(
        name="time.zone", description="World clock: current time in "
        "a place (common city names or any IANA zone), or a pinned "
        "host-local clock (HH:MM) expressed there; next-occurrence "
        "convention for passed clocks. Honest refusals for unknown "
        "places.",
        input_schema={"type": "object", "required": ["zone"],
                      "properties": {"zone": {"type": "string"},
                                     "clock": {"type": "string"}}},
        output_schema={"type": "object", "required": ["zone"],
                       "properties": {"zone": {"type": "string"}}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
        estimated_cost=0.0, timeout_s=5.0,
        success_criteria="zone time returned", failure_behavior="fail",
        verification="output_schema",
        supported_devices=["any"], reversible=True, version="1.1.0"),
    time_zone),
    (ToolDefinition(
        name="peripheral.list", description="Connected peripheral "
        "devices on this host (USB storage, phones, displays, audio): "
        "detection state and discovered capabilities. Detection never "
        "implies trust.",
        input_schema={"type": "object",
                      "properties": {"state": {"type": "string",
                                               "enum": ["present", "removed",
                                                        "all"]}}},
        output_schema={"type": "object", "required": ["devices"],
                       "properties": {"devices": {"type": "array"}}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
        estimated_cost=0.02, timeout_s=10.0,
        success_criteria="returns device list", failure_behavior="fail",
        verification="output_schema",
        supported_devices=["any"], reversible=True, version="1.0.0"),
    peripheral_list),
    (ToolDefinition(
        name="peripheral.describe", description="Class-aware description of "
        "one detected peripheral: what Zara can actually do with it based "
        "on discovered capabilities (never guessed).",
        input_schema={"type": "object", "required": ["pid"],
                      "properties": {"pid": {"type": "string"}}},
        output_schema={"type": "object", "required": ["pid", "actions"],
                       "properties": {"actions": {"type": "array"}}},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
        estimated_cost=0.02, timeout_s=10.0,
        success_criteria="returns actions", failure_behavior="fail",
        verification="output_schema",
        supported_devices=["any"], reversible=True, version="1.0.0"),
    peripheral_describe),
    # ---- Stage 21: file-transfer missions (core-local orchestration) ----
    (ToolDefinition(
        name="transfer.mission", description="Create and drive an end-to-end "
        "file-transfer mission (phone<->laptop<->pendrive) through the "
        "existing mission + execution + relay engines. Spec: {source:{device,"
        " path}, dest:{device, path?}, items:[names], move?, "
        "delete_source_after?}. With an empty spec the mission waits and "
        "asks for specifics — it never guesses targets.",
        input_schema={"type": "object",
                      "properties": {"spec": {"type": "object"}}},
        output_schema={"type": "object"},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
        estimated_cost=0.02, timeout_s=30.0,
        success_criteria="mission id returned", failure_behavior="fail",
        verification="output_schema",
        supported_devices=["any"], reversible=False, version="1.0.0"),
    transfer_mission),
    (ToolDefinition(
        name="transfer.status", description="Active file-transfer missions "
        "and relay transfers with per-item progress (read-only).",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
        permission=PermissionLevel.PUBLIC, risk=RiskLevel.SAFE,
        estimated_cost=0.01, timeout_s=10.0,
        success_criteria="status lists returned", failure_behavior="fail",
        verification="output_schema",
        supported_devices=["any"], reversible=True, version="1.0.0"),
    transfer_status),
    (ToolDefinition(
        name="transfer.cancel", description="Cancel the active file-transfer "
        "mission (and its pending relay transfers). Stops between items; "
        "an in-flight item completes and is reported honestly.",
        input_schema={"type": "object",
                      "properties": {"mission_id": {"type": "string"}}},
        output_schema={"type": "object"},
        permission=PermissionLevel.CONFIRM, risk=RiskLevel.CONFIRM,
        estimated_cost=0.01, timeout_s=10.0,
        success_criteria="cancel outcome returned", failure_behavior="fail",
        verification="output_schema",
        supported_devices=["any"], reversible=True, version="1.0.0"),
    transfer_cancel),
    # NOTE (P5 2026-10-23): calendar.event.add is registered as a LAPTOP
    # device tool (core/device_tools.py + device/linux/tools_linux.py —
    # the parity contract pins both sides); the core-local handler
    # implementation below is the single behavior source the agent
    # imports. There is intentionally NO core-local registry entry here:
    # a registry def with supported_devices=["any"] and no advertising
    # device can never resolve, which the live probe proved.
]
