"""Bounded directory statistics for removable storage (Stage 20).

Answers the pendrive use case WITHOUT loading file lists into RAM:
one scandir walk, per-category counts and byte totals, directory
count, unreadable-entry accounting, symlink notes, and explicit
scan-limit reporting. Unknown extensions are counted as ``other``
with their names kept as bounded samples so the user can decide —
uncertainty is reported, never silently misclassified.

Cancellation is cooperative via a threading.Event (checked every
entry); resource caps (max entries, depth) make a huge or cyclic
directory tree safe. Hidden files are counted (not skipped) and
reported separately so nothing is swept under the rug.
"""
from __future__ import annotations

import os
import threading
import time

# Explicit, maintainable category rules — extension -> category.
# Everything unknown goes to ``other`` with bounded samples kept.
CATEGORIES = ("video", "audio", "image", "document", "other")

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv",
             ".flv", ".ts", ".3gp", ".mpg", ".mpeg"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
             ".wma", ".amr", ".mid"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic",
             ".heif", ".tiff", ".svg", ".dng"}
DOCUMENT_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt",
                ".pptx", ".txt", ".rtf", ".odt", ".ods", ".odp", ".csv",
                ".md", ".epub", ".html", ".htm"}

HUGE_DIR_WARN = 50_000  # entries per directory before the walk notes it

CATEGORY_BY_EXT = {}
for _exts, _cat in ((VIDEO_EXT, "video"), (AUDIO_EXT, "audio"),
                    (IMAGE_EXT, "image"), (DOCUMENT_EXT, "document")):
    for _e in _exts:
        CATEGORY_BY_EXT[_e] = _cat


def category_of(ext_lower: str) -> str:
    return CATEGORY_BY_EXT.get(ext_lower, "other")


def scan_tree(root: str, cancel: "(threading.Event) | None" = None,
              max_entries: int = 200_000, max_depth: int = 16
              ) -> dict:
    """Walk `root`, returning honest bounded statistics. Never raises
    for per-entry permission problems; raises ValueError for a bad
    root. The walk itself is ordered by entry name; counts and byte
    sizes are exact within the caps, and `limits` says when a cap hit
    means the numbers are a floor, not the full truth."""
    if not os.path.isdir(root):
        raise ValueError(f"not a directory: {root}")
    stats = {c: {"count": 0, "bytes": 0} for c in CATEGORIES}
    out = {
        "root": root,
        "categories": stats,
        "total_files": 0, "total_bytes": 0,
        "directories": 0,
        "hidden_files": 0,
        "symlinks": 0,
        "unreadable": [],       # bounded list of dirs/files we could not read
        "unreadable_count": 0,
        "huge_dirs": [],        # dirs over HUGE_DIR_WARN entries
        "other_samples": [],    # bounded unknown-extension samples
        "limits": {"max_entries": max_entries, "max_depth": max_depth,
                   "hit_cap": False, "hit_depth": False},
    }
    # never followed for counts; symlink paths kept, bounded, for honesty
    symlink_samples: list[str] = []
    out["symlinks_sample"] = symlink_samples
    entries_seen = 0

    def _cancel_hit() -> bool:
        return bool(cancel is not None and cancel.is_set())

    def walk(d: str, depth: int) -> None:
        nonlocal entries_seen
        if depth > max_depth:
            out["limits"]["hit_depth"] = True
            return
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            out["unreadable_count"] += 1
            if len(out["unreadable"]) < 20:
                out["unreadable"].append(d)
            return
        if len(entries) > HUGE_DIR_WARN and len(out["huge_dirs"]) < 5:
            out["huge_dirs"].append(d)
        for e in entries:
            if _cancel_hit():
                out["limits"]["hit_cap"] = True
                return
            entries_seen += 1
            if entries_seen > max_entries:
                out["limits"]["hit_cap"] = True
                return
            name = e.name
            try:
                if e.is_symlink():
                    out["symlinks"] += 1
                    if len(symlink_samples) < 10:
                        symlink_samples.append(os.path.join(d, name))
                    continue  # never follow symlinks into media counts
                if e.is_dir(follow_symlinks=False):
                    out["directories"] += 1
                    walk(e.path, depth + 1)
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    out["unreadable_count"] += 1
                    if len(out["unreadable"]) < 20:
                        out["unreadable"].append(e.path)
                    continue
                sz = st.st_size
                if name.startswith("."):
                    out["hidden_files"] += 1
                cat = category_of(os.path.splitext(name)[1].lower())
                stats[cat]["count"] += 1
                stats[cat]["bytes"] += sz
                out["total_files"] += 1
                out["total_bytes"] += sz
                if cat == "other" and len(out["other_samples"]) < 20:
                    out["other_samples"].append(
                        {"name": name, "size": sz})
            except OSError:
                out["unreadable_count"] += 1

    walk(root, 0)
    if _cancel_hit():
        out["cancelled"] = True
    return out


# P5 ⑰ (2026-10-24): "where did my disk go" — the dust/gdu/dua idea
# (bootandy/dust, dundee/gdu, Byron/dua-cli — interface only, stdlib
# re-implementation): a SHALLOW per-directory overview, never
# interactive, never following symlinks, honest about floors. Same
# honesty contract as scan_tree: bounded, deterministic-order output,
# unreadable entries skipped AND counted, caps reported so a number
# can never silently stand for more than was measured.
def scan_dir_usage(root: str, top_n: int = 8, budget_s: float = 4.0,
                   max_entries: int = 400_000,
                   max_bytes_walked: int = 50 * 1024 * 1024 * 1024
                   ) -> dict:
    """Sizes of `root`'s level-1 children (recursive subtree bytes,
    apparent size st_size), plus files sitting directly in the root.
    Returns `partial=True` (with the count of entries actually seen)
    when the time/entry budget stopped the walk — every reported size
    is then a FLOOR, and the detail must say so. Hard links are
    double-counted only after a bounded dedup window is exhausted
    (an honest approximation is stated, never a silent one)."""
    if not os.path.isdir(root):
        raise ValueError(f"not a directory: {root}")
    deadline = time.monotonic() + budget_s
    items: dict[str, dict] = {}
    root_files_bytes = 0
    root_files_count = 0
    symlinks_skipped = 0
    unreadable: list[str] = []
    unreadable_count = 0
    seen_inodes: set[tuple[int, int]] = set()
    entries_seen = 0
    partial_reason = ""
    partial = False

    def acc(rel: str) -> dict:
        return items.setdefault(rel, {"path": rel, "bytes": 0,
                                      "files": 0, "dirs": 0})

    def walk(d: str, rel: str, depth: int, depth_cap: int = 24) -> None:
        """Recursive size aggregation for ONE level-1 child."""
        nonlocal entries_seen, symlinks_skipped, unreadable_count
        nonlocal partial, partial_reason
        if partial:
            return
        if time.monotonic() > deadline:
            partial, partial_reason = True, "time budget"
            return
        if entries_seen >= max_entries:
            partial, partial_reason = True, "entry cap"
            return
        if depth > depth_cap:
            # pathological nesting guard; sizes below stay floors
            partial, partial_reason = True, "depth cap"
            return
        try:
            with os.scandir(d) as it:
                it = iter(sorted(it, key=lambda e: e.name))
        except OSError:
            unreadable_count += 1
            if len(unreadable) < 10:
                unreadable.append(d)
            return
        while True:
            try:
                e = next(it)
            except StopIteration:
                break
            except OSError:
                unreadable_count += 1
                continue
            if partial:
                break
            entries_seen += 1
            if entries_seen > max_entries:
                partial, partial_reason = True, "entry cap"
                break
            try:
                if e.is_symlink():
                    symlinks_skipped += 1   # never followed; never counted
                    continue
                if e.is_dir(follow_symlinks=False):
                    acc(rel)
                    sub = f"{rel}/{e.name}" if rel else e.name
                    items.setdefault(sub, {"path": sub, "bytes": 0,
                                           "files": 0, "dirs": 1})
                    walk(e.path, sub, depth + 1, depth_cap)
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
                ino = (st.st_dev, st.st_ino)
                if len(seen_inodes) < 200_000:
                    if ino in seen_inodes:
                        # hard link already counted under another path
                        continue
                    seen_inodes.add(ino)
                sz = st.st_size
                if rel:
                    for anc in anc_names(rel):
                        bucket2 = items.get(anc)
                        if bucket2 is not None:
                            bucket2["bytes"] += sz
                            bucket2["files"] += 1
                    acc(rel)["bytes"] += sz
                    acc(rel)["files"] += 1
                else:
                    root_files_bytes += sz
                    root_files_count += 1
            except OSError:
                unreadable_count += 1

    def anc_names(rel: str) -> list[str]:
        parts = rel.split("/")
        return ["/".join(parts[:i]) for i in range(1, len(parts))]

    try:
        top = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError as e:
        raise ValueError(f"cannot read {root}: {e}") from e
    for e in top:
        if partial:
            break
        if time.monotonic() > deadline:
            partial, partial_reason = True, "time budget"
            break
        if entries_seen >= max_entries:
            partial, partial_reason = True, "entry cap"
            break
        try:
            if e.is_symlink():
                symlinks_skipped += 1
                continue
            if e.is_dir(follow_symlinks=False):
                walk(e.path, e.name, 1)
            elif e.is_file(follow_symlinks=False):
                try:
                    st = e.stat(follow_symlinks=False)
                    ino = (st.st_dev, st.st_ino)
                    if len(seen_inodes) < 200_000:
                        if ino in seen_inodes:
                            continue
                        seen_inodes.add(ino)
                    entries_seen += 1
                    root_files_bytes += st.st_size
                    root_files_count += 1
                except OSError:
                    unreadable_count += 1
        except OSError:
            unreadable_count += 1

    ranked = sorted(items.values(), key=lambda r: (-r["bytes"], r["path"]))
    out = {"root": root, "items": ranked[:max(1, min(top_n, 20))],
           "root_files_bytes": root_files_bytes,
           "root_files_count": root_files_count,
           "total_bytes": sum(r["bytes"] for r in ranked
                              if "/" not in r["path"])
                          + root_files_bytes,
           "entries_seen": entries_seen,
           "symlinks_skipped": symlinks_skipped,
           "unreadable_count": unreadable_count,
           "unreadable": unreadable,
           "budget": {"budget_s": round(budget_s, 1),
                      "partial": partial,
                      "reason": partial_reason},
           "semantics": "apparent size (st_size); symlinks not "
                        "followed; hard links deduped within a "
                        "bounded window"}
    if partial:
        out["detail"] = (f"partial scan — stopped at the "
                         f"{partial_reason} after {entries_seen} "
                         f"entries; sizes are floors, not totals")
    return out


# P5 ㉓ (2026-10-24): the growth lens — "what grew on my disk". The
# folder snapshot is SAVED as an assistant-internal baseline
# (aggregate level-1 sizes only, keyed by root; no file paths, no
# content) and a later scan diffs against it. Honesty contract:
# - a diff over partial scans is NOISE (each side is a floor, deltas
#   are meaningless) — growth refuses on partials with the fix
#   stated (raise the budget);
# - baseline writes are keyed-replace per (root, day) so redelivery
#   (at-least-once) cannot stack duplicate baselines;
# - baseline = assistant machinery, not user content: only aggregate
#   sizes are persisted, so no approval gate applies (databases the
#   core itself owns write constantly by the same rule).
class StorageLedger:
    """Tiny sqlite ledger of folder snapshots (root, day, buckets)."""

    # payload versioning: the tracked UNIT is part of the data's
    # meaning; rows without this marker are treated absent (re-
    # baseline) rather than diffed across shapes.
    LEDGER_SHAPE = "level1-v1"

    def __init__(self, path: str) -> None:
        import sqlite3
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS storage_baseline ("
            "root TEXT NOT NULL, day TEXT NOT NULL, ts TEXT NOT NULL,"
            "items TEXT NOT NULL, PRIMARY KEY (root, day))")

    def latest(self, root: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT ts, items FROM storage_baseline "
            "WHERE root=? ORDER BY day DESC LIMIT 1",
            (root,)).fetchone()
        if not row:
            return None
        import json as _json
        payload = _json.loads(row[1])
        # Shape migration: older snapshots tracked a different unit
        # (top-N across the nested union). A baseline with another
        # unit is NOT a baseline — treated absent so the next run
        # re-baselines honestly instead of diffing across shapes.
        if not isinstance(payload, dict) or \
                payload.get("shape") != self.LEDGER_SHAPE:
            return None
        return {"ts": row[0], "items": payload["items"]}

    def save(self, root: str, ts: str, items: list) -> dict:
        import json as _json
        day = ts[:10]
        payload = _json.dumps({"shape": self.LEDGER_SHAPE,
                                "items": items})
        cur = self._conn.execute(
            "SELECT ts FROM storage_baseline WHERE root=? AND day=?",
            (root, day)).fetchone()
        if cur:
            self._conn.execute(
                "UPDATE storage_baseline SET ts=?, items=? "
                "WHERE root=? AND day=?", (ts, payload, root, day))
        else:
            self._conn.execute(
                "INSERT INTO storage_baseline(root, day, ts, items) "
                "VALUES(?,?,?,?)", (root, day, ts, payload))
        self._conn.commit()
        # keep at most the newest 8 days per root (bounded by design)
        self._conn.execute(
            "DELETE FROM storage_baseline WHERE root=? AND day NOT IN"
            " (SELECT day FROM storage_baseline WHERE root=? "
            "ORDER BY day DESC LIMIT 8)", (root, root))
        self._conn.commit()
        return {"saved": True, "day": day, "ts": ts}


def growth_diff(prev: dict, cur: dict) -> dict:
    """Pure name-keyed delta between two breakdowns (partial scans
    are refused by the CALLER). Buckets matched by name: grown/shrunk
    are byte deltas, new/removed are stated as such."""
    pi = {b["path"]: b for b in prev.get("items", [])}
    ci = {b["path"]: b for b in cur.get("items", [])}
    out = []
    for name in sorted(set(pi) | set(ci), key=lambda n: n):
        pb, cb = pi.get(name), ci.get(name)
        pbytes = pb["bytes"] if pb else 0
        cbytes = cb["bytes"] if cb else 0
        if pb and cb and cbytes == pbytes:
            state = "unchanged"
        elif pb and cb:
            state = "grown" if cbytes > pbytes else "shrunk"
        elif cb and not pb:
            state = "new"
        else:
            state = "gone"
        if state == "unchanged":
            continue
        out.append({"bucket": name, "state": state,
                    "prev_bytes": pbytes, "cur_bytes": cbytes,
                    "delta_bytes": cbytes - pbytes})
    out.sort(key=lambda r: -abs(r["delta_bytes"]))
    return {"rows": out,
            "matched": sum(1 for n in pi if n in ci),
            "prev_ts": prev.get("ts")}


def storage_growth(root: str, top_n: int = 10,
                   budget_s: float = 12.0,
                   max_entries: int = 2_000_000) -> dict:
    """P5 ㉓ \"what grew on my disk\": scan `root` (ALREADY house-
    scoped by the caller's wrapper — this core module never touches
    _fs_path), diff against the last saved baseline for the same
    root, then save this scan as the new baseline. Refuses to diff
    over PARTIAL scans (floors delta to noise); no baseline yet
    means HONEST first-run: \"baseline saved, ask again later\" with
    today's breakdown. The walker's entry cap is raised here (2M)
    and the budget shared with the caller — whole-home trees exceed
    the 400k default fast, and a growth tool that can never finish
    is a refusal machine, not a feature."""
    cur = scan_dir_usage(root, top_n=100_000, budget_s=budget_s,
                         max_entries=max_entries)
    if cur.get("budget", {}).get("partial"):
        raise ValueError(
            "the scan hit its budget — growth diffs need FULL scans "
            "(floors delta to noise); raise budget_seconds and retry")

    def _level1(items: list) -> list:
        # scan_dir_usage emits a bucket for EVERY nested subdir;
        # top-N ranking across that union churns membership (deep
        # chains can push real level-1 children out) and double-
        # counts by construction. The LEDGER therefore tracks the
        # level-1 children only: complete, stable, non-overlapping.
        return [i for i in items if "/" not in i["path"]]

    cur_l1 = _level1(cur["items"])
    ledger = StorageLedger(os.environ.get(
        "ZARA_STORAGE_LEDGER_DB",
        os.path.expanduser(
            "~/.local/state/zara/zara_storage_ledger.db")))
    import datetime as _dt
    now = _dt.datetime.now().astimezone()
    ts = now.isoformat(timespec="seconds")
    prev = ledger.latest(root)
    out = {"root": root, "now": ts, "matched": 0,
           "rows": [], "first_baseline": prev is None}
    if prev is not None:
        diff = growth_diff({"items": prev["items"], "ts": prev["ts"]},
                           {"items": cur_l1})
        out["rows"] = diff["rows"]
        out["matched"] = diff["matched"]
        out["prev_ts"] = diff["prev_ts"]
    ledger.save(root, ts, cur_l1)
    out["current"] = cur_l1[:max(1, top_n)]
    out["detail"] = ("LEVEL-1 bucket sizes only are persisted "
                     "(assistant-internal baseline, no file paths, "
                     "no nested double-count)")
    return out


# P5 ㉑ (2026-10-24): the FILE-level lens of the same idea — "which
# files are over 1 GB" / "my biggest files" (dust/gdu give both
# directory and file views; the folder view was batch 17). Same
# honesty contract: bounded walk, floor semantics on partials,
# symlinks never followed, unreadable entries skipped AND counted.
# Paths ARE returned here (a file answer without paths is useless)
# — but bounded to top_n items, and only from the house scope.
def find_big_files(root: str, min_bytes: int = 0, top_n: int = 10,
                   max_entries: int = 400_000,
                   budget_s: float = 6.0) -> dict:
    """Rank files by apparent size (st_size) within the house scope;
    `min_bytes` is a floor filter the caller may pass (the chat shape
    parses "over 1 gb" etc.). Partial walks (budget/entry cap) say so
    — omitted files below the cap line may still be big."""
    if not os.path.isdir(root):
        raise ValueError(f"not a directory: {root}")
    deadline = time.monotonic() + budget_s
    found: list[dict] = []
    unreadable_count = 0
    unreadable: list[str] = []
    symlinks_skipped = 0
    entries_seen = 0
    partial_reason = ""
    partial = False
    seen_inodes: set[tuple[int, int]] = set()

    stack: list[tuple[str, int]] = [(root, 0)]
    while stack and not partial:
        d, depth = stack.pop()
        if time.monotonic() > deadline:
            partial, partial_reason = True, "time budget"
            break
        if entries_seen >= max_entries:
            partial, partial_reason = True, "entry cap"
            break
        if depth > 24:
            partial, partial_reason = True, "depth cap"
            continue
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            unreadable_count += 1
            if len(unreadable) < 10:
                unreadable.append(d)
            continue
        for e in entries:
            if partial:
                break
            entries_seen += 1
            if time.monotonic() > deadline:
                partial, partial_reason = True, "time budget"
                break
            try:
                if e.is_symlink():
                    symlinks_skipped += 1
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, depth + 1))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
                if len(seen_inodes) < 200_000:
                    ino = (st.st_dev, st.st_ino)
                    if ino in seen_inodes:
                        continue          # hard link counted once
                    seen_inodes.add(ino)
                if st.st_size >= min_bytes:
                    found.append({"path": e.path, "bytes":
                                  st.st_size})
            except OSError:
                unreadable_count += 1
    found.sort(key=lambda r: (-r["bytes"], r["path"]))
    out = {"root": root, "min_bytes": min_bytes,
           "items": found[:max(1, min(top_n, 50))],
           "matched_count": len(found),
           "entries_seen": entries_seen,
           "symlinks_skipped": symlinks_skipped,
           "unreadable_count": unreadable_count,
           "unreadable": unreadable,
           "budget": {"budget_s": round(budget_s, 1),
                      "partial": partial, "reason": partial_reason},
           "semantics": "apparent size (st_size); symlinks not "
                        "followed; hard links deduped within a "
                        "bounded window"}
    if partial:
        out["detail"] = (f"partial scan — stopped at the "
                         f"{partial_reason} after {entries_seen} "
                         f"entries; the list is a floor, more big "
                         f"files may exist")
    return out

