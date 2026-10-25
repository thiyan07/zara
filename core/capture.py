"""Capture routing (P6-b29, 2026-10-24) — the GTD-style bridge
between jotted thoughts and actions, deterministic and local.

The user says "capture <text>" (or uses the inbox/file verbs), and
items land UNSORTED in one additive sqlite table (ZARA_CAPTURE_DB;
memory when unset). Filing moves them into one of the BUCKETS named
exactly like the method (zrwang/gtd + gtd-skill as the design
interface — never their code, stdlib re-implementation):

  next_action | project | someday | waiting
  (plus drop → gone, not "done")

Honesty rules:
- a capture is a DIARY item: stored verbatim, never interpreted as an
  instruction, never auto-filed (the flow never guesses buckets);
- reports count the QUEUES, never invent progress; the weekly review
  reads THIS service's open_count()/counts() — one computation, two
  surfaces;
- "show my inbox" lists unsorted items with their numbers, so file
  commands reference what the user actually saw.

Batch-32 addition (2026-10-26): the todo.txt grammar subset
(github.com/todotxt — spec referenced, never their code): a captured
text may carry `due:YYYY-MM-DD` (the spec's key:value metadata rule)
and a leading priority `(A)`–`(Z)` (the spec's Rule 1: priority is
always FIRST in the line). The text stays VERBATIM (the diary rule
holds); the parsed values live in a sidecar (capture_meta) keyed by
the item id. When any unsorted item carries metadata, the inbox view
sorts by due-then-priority-then-arrival and annotates items — and
`file N` uses the SAME ordering, so the numbers never lie. An invalid
due date never fills a fake value: the capture stays verbatim, no
date is stored, and the reply names the invalid token.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time as _time
import uuid
from datetime import date, datetime
from typing import Callable, Optional

_TEXT_MAX = 200

# todo.txt grammar subset (batch-32): priority FIRST in the line
# (spec Rule 1), due date as key:value metadata (spec rule). Never
# their code — two regexes against the same documented grammar.
_TODO_PRI_RE = re.compile(r"^\s*\(\s*(?P<p>[A-Za-z])\s*\)\s+(?P<t>\S.*)$")
_TODO_DUE_RE = re.compile(r"\bdue[:=]\s*(?P<d>\d{4}-\d{2}-\d{2})\b",
                          re.I)
_SENTINEL_DUE = "9999-12-31"


def extract_todo_tokens(text: str) -> tuple:
    """(due, pri, problem) from a captured line. Values are validated
    (a real date, one A–Z priority); anything invalid is REPORTED in
    `problem` and left unstored — no fake dates are ever filled."""
    text = (text or "").strip()
    pri = None
    m = _TODO_PRI_RE.match(text)
    if m:
        pri = m.group("p").upper()
    m2 = re.search(r"\bpri[:=]\s*([A-Za-z])\b", text)
    if m2 and pri is None:
        pri = m2.group(1).upper()
    due = None
    invalid_due = False
    for dm in _TODO_DUE_RE.finditer(text):
        raw = dm.group("d")
        try:
            date.fromisoformat(raw)
            if due is None:
                due = raw
        except ValueError:
            invalid_due = True
    problem = ("a due: token was not a real date — captured verbatim,"
               " no date set" if (invalid_due and due is None) else "")
    return due, pri, problem


def todo_sortkey(r: dict):
    """due asc, priority asc (missing = last), then arrival order."""
    pri = (r.get("pri") or "Z").upper()
    due = r.get("due") or _SENTINEL_DUE
    return (due, pri, r["at"])

BUCKETS = ("next_action", "project", "someday", "waiting")
_SET_WORDS = {
    "next action": "next_action",
    "next-action": "next_action",
    "action": "next_action",
    "project": "project",
    "someday": "someday",
    "someday maybe": "someday",
    "waiting": "waiting",
    "waiting for": "waiting",
    "trash": "__trash__",
    "drop": "__trash__",
}
_LIST_WORDS = {
    "next actions": "next_action",
    "next action": "next_action",
    "projects": "project",
    "project": "project",
    "someday": "someday",
    "waiting for": "waiting",
    "waiting": "waiting",
}


class CaptureStore:
    """One additive table; memory fallback when no db path."""

    def __init__(self, audit=None, db_path: str = "",
                 now: Optional[Callable[[], float]] = None) -> None:
        self.audit = audit
        self._now = now or (lambda: _time.time())
        self._lock = threading.RLock()
        self._db = None
        self._mem: list[dict] = []
        self._meta: dict[str, dict] = {}   # id -> {due, pri}
        if db_path:
            self._db = sqlite3.connect(db_path,
                                       check_same_thread=False)
            self._setup_db()

    def _setup_db(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS capture_items ("
                "id TEXT PRIMARY KEY, text TEXT NOT NULL, "
                "bucket TEXT DEFAULT 'unsorted', "
                "at REAL NOT NULL, local_date TEXT NOT NULL, "
                "who TEXT)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS capture_meta ("
                "id TEXT PRIMARY KEY, due TEXT, pri TEXT)")
            self._db.commit()

    def _audit(self, action: str, detail: str = "") -> None:
        try:
            if self.audit is not None:
                self.audit.record("capture", action, "", detail[:200])
        except Exception:  # noqa: BLE001 — audit never breaks capture
            pass

    def _mk(self, text: str, bucket: str = "unsorted") -> dict:
        now = self._now()
        return {"id": f"cap-{uuid.uuid4().hex[:10]}",
                "text": text, "bucket": bucket, "at": now,
                "local_date": datetime.fromtimestamp(now)
                .astimezone().strftime("%Y-%m-%d"), "who": "user",
                "due": None, "pri": None, "todo_problem": ""}

    def capture(self, text: str) -> list[dict]:
        text = " ".join(text.split())[:_TEXT_MAX]
        if not text or len(text) < 2:
            raise CaptureValueError("What should I capture?")
        # brain-dump support: 'and'/'newline/';' separated notes stay
        # separate items — each is ITS OWN thought, never one blob
        parts = [p.strip(" .,") for p in
                 re.split(r";|\band\b|\n", text)]
        parts = [p for p in parts if len(p) >= 2]
        if not parts:
            raise CaptureValueError("What should I capture?")
        rows = []
        for p in parts[:10]:
            rows.append(self._mk(p))
        # todo.txt subset (batch-32): due:YYYY-MM-DD anywhere + (A)
        # priority first; text stays verbatim, values go to sidecar
        for r in rows:
            due, pri, problem = extract_todo_tokens(r["text"])
            if due or pri:
                self._attach_meta(r["id"], due, pri)
                r["due"], r["pri"] = due, pri
            r["todo_problem"] = problem
        with self._lock:
            self._mem.extend(rows)
            if self._db is not None:
                for r in rows:
                    self._db.execute(
                        "INSERT INTO capture_items (id, text, bucket,"
                        " at, local_date, who) VALUES (?,?,?,?,?,?)",
                        (r["id"], r["text"], r["bucket"], r["at"],
                         r["local_date"], r["who"]))
                    if r.get("due") or r.get("pri"):
                        self._db.execute(
                            "INSERT OR REPLACE INTO capture_meta"
                            " (id, due, pri) VALUES (?,?,?)",
                            (r["id"], r.get("due"), r.get("pri")))
                self._db.commit()
        self._audit("capture_captured", f"n={len(rows)}")
        return rows

    def _attach_meta(self, item_id: str, due: Optional[str],
                     pri: Optional[str]) -> None:
        with self._lock:
            if self._db is not None:
                self._db.execute(
                    "INSERT OR REPLACE INTO capture_meta VALUES"
                    " (?,?,?)", (item_id, due, pri))
                self._db.commit()
            else:
                self._meta[item_id] = {"due": due, "pri": pri}

    def _rows(self, bucket=None) -> list[dict]:
        """Memory-store view; db callers use `_rows_db`."""
        return [r for r in self._mem
                if bucket is None or r.get("bucket") == bucket]

    def open_count(self) -> int:
        """The weekly-review hook: unsorted items waiting to be
        filed."""
        return len(self.list_by("unsorted"))

    def counts(self) -> dict:
        out = {}
        for b in ("unsorted",) + BUCKETS:
            out[b] = len(self.list_by(b))
        return out

    def list_by(self, bucket: str, limit: int = 12) -> list[dict]:
        rows = (self._mem if self._db is None
                else self._rows_db())
        got = [r for r in rows if r.get("bucket") == bucket]
        got.sort(key=lambda r: r["at"])
        return got[:limit]

    def _rows_db(self) -> list[dict]:
        with self._lock:
            rs = self._db.execute(
                "SELECT id, text, bucket, at, local_date, who FROM "
                "capture_items").fetchall()
        out = [{"id": r[0], "text": r[1], "bucket": r[2], "at": r[3],
                "local_date": r[4], "who": r[5]} for r in rs]
        for r in out:
            meta = self._meta_row(r["id"])
            r.update(meta)
        return out

    def _meta_row(self, item_id: str) -> dict:
        if self._db is not None:
            with self._lock:
                got = self._db.execute(
                    "SELECT due, pri FROM capture_meta WHERE id=?",
                    (item_id,)).fetchone()
            return {"due": got[0] if got else None,
                    "pri": got[1] if got else None}
        return dict(self._meta.get(item_id, {"due": None,
                                             "pri": None}))

    def _drop(self, row: dict) -> None:
        row["bucket"] = "__dropped__"
        if self._db is not None:
            with self._lock:
                self._db.execute(
                    "UPDATE capture_items SET bucket='__dropped__' "
                    "WHERE id=?", (row["id"],))
                self._db.commit()
        else:
            for r in self._mem:
                if r["id"] == row["id"]:
                    r["bucket"] = "__dropped__"

    def _set_bucket(self, row: dict, bucket: str) -> None:
        row["bucket"] = bucket
        if self._db is not None:
            with self._lock:
                self._db.execute(
                    "UPDATE capture_items SET bucket=? WHERE id=?",
                    (bucket, row["id"]))
                self._db.commit()
        else:
            for r in self._mem:
                if r["id"] == row["id"]:
                    r["bucket"] = bucket

    def unsorted_view(self, limit: int = 50) -> list[dict]:
        """The inbox the user SEES: sorted by todo.txt due/priority
        when any item carries metadata, else arrival order — one
        ordering for the listing AND the `file N` numbering."""
        rows = self.list_by("unsorted", limit=limit)
        if any(r.get("due") or r.get("pri") for r in rows):
            rows = sorted(rows, key=todo_sortkey)
        return rows

    def file(self, seq: int, target: str) -> dict:
        """File the seq-th UNSORTED item (1-based, same ordering the
        inbox list showed) into target bucket — or drop it."""
        seq = int(seq)
        if seq < 1:
            raise CaptureValueError("Which item number?")
        opens = self.unsorted_view(limit=50)
        if seq > len(opens) or not opens:
            raise CaptureValueError(
                f"I only see {len(opens)} unsorted item(s). Say "
                f"'show my inbox'.")
        row = opens[seq - 1]
        if target == "__trash__":
            with self._lock:
                self._drop(row)
            self._audit("capture_dropped", row["id"])
            return {"row": row, "action": "dropped"}
        if target not in BUCKETS:
            raise CaptureValueError(
                f"Unknown bucket '{target}'.")
        with self._lock:
            self._set_bucket(row, target)
        self._audit("capture_filed", f"{row['id']} -> {target}")
        return {"row": row, "action": target}


class CaptureValueError(ValueError):
    """Hard refusal (unknown item / unknown bucket)."""


# ------------------------------------------------------------------
# NL surface
# ------------------------------------------------------------------

_ANCHOR_RE = re.compile(
    r"^\s*capture\b|^\s*jot\b|\bshow my inbox\b|\bmy capture inbox\b"
    r"|^\s*file\s+\d+\s+as\b|^my (?:next actions?|projects?|someday|"
    r"waiting(?:\s+for)?)\b|^what(?:'s| is) my next action\b"
    r"|^\s*file all\b", re.I)
_EXCLUDE_RE = re.compile(
    r"\b(remind|reminder|routine|calendar|expense|grocer|groceries|"
    r"water|medication)\b", re.I)


def parse_capture_intent(utterance: str) -> dict:
    raw = " ".join((utterance or "").strip().split())
    low = raw.lower()
    # capture/jot utterances carry DIARY text — only the anchor itself
    # matters; ledger/reminder nouns in the captured text are data.
    if _EXCLUDE_RE.search(low) and \
            re.match(r"^(?:capture|jot)\b", low) is None:
        return {"op": "unknown"}
    m = re.match(r"^(?:capture|jot)(?:\s*down|:\s*)?\s*(?P<t>.+)$",
                 raw, re.I)
    if m:
        return {"op": "capture", "text": m.group("t")}
    if re.fullmatch(r"show(?: my| the)? (?:capture )?inbox"
                    "|(?:what(?:'s| is) in) my inbox", raw, re.I):
        return {"op": "inbox"}
    m = re.fullmatch(r"file\s+(?P<n>\d+)(?:\s+as\s+)?(?P<t>[a-z ]+)",
                     raw.lower())
    if m:
        t = " ".join(m.group("t").split())
        return {"op": "file", "n": int(m.group("n")),
                "target": _SET_WORDS.get(t, t)}
    m = re.fullmatch(r"(?:(?:show|list)(?:\s+my)?|my)\s+"
                     r"(?P<t>next actions?|projects?|someday|"
                     r"waiting(?:\s+for)?)", low)
    if m:
        t = " ".join(m.group("t").split())
        word = ("someday" if t.startswith("someday")
                else ("next_action"
                      if t.startswith("next") else
                      ("project" if t.startswith("project")
                       else "waiting")))
        return {"op": "list_bucket", "bucket": word}
    m = re.fullmatch(r"what(?:'s| is) my next action", low)
    if m:
        return {"op": "next"}
    return {"op": "unknown"}


def _bucket_list_message(store: CaptureStore, bucket: str) -> str:
    label = {"next_action": "next actions", "project": "projects",
             "someday": "someday items", "waiting": "waiting for"}
    items = store.list_by(bucket)
    if not items:
        return f"{label[bucket].capitalize()}: none."
    return (f"{label[bucket].capitalize()} ({len(items)}): " +
            "; ".join(f"{i + 1}. {r['text'][:60]}"
                      for i, r in enumerate(items[:8])) + ".")


def handle_capture_utterance(store: CaptureStore, utterance: str,
                             who: str = "user") -> Optional[dict]:
    op = parse_capture_intent(utterance)
    kind = op.get("op")
    if kind == "unknown":
        return None
    try:
        if kind == "capture":
            rows = store.capture(op["text"])
            n = len(rows)
            first = rows[0]["text"][:60]
            rest = f" (+{n - 1} more)" if n > 1 else ""
            note = " ".join(r.get("todo_problem") for r in rows
                            if r.get("todo_problem"))
            tail = (f" " + note if note else "")
            return {"message": f"Captured{rest}: '{first}'."
                               f"{tail} Say 'show my inbox' to file "
                               f"things.",
                    "executed": False, "captured": n}
        if kind == "inbox":
            items = store.unsorted_view(limit=50)
            c = store.counts()
            if not items:
                return {"message": "Capture inbox clear. "
                                   f"Opened to file: "
                                   f"{c['next_action']} next "
                                   f"actions, {c['project']} "
                                   f"projects, {c['someday']} "
                                   f"someday, {c['waiting']} "
                                   f"waiting.",
                        "executed": False, "counts": c}
            body = "; ".join(
                f"{i + 1}. {r['text'][:50]}"
                for i, r in enumerate(items[:10]))
            more = (f" … and {len(items) - 10} more"
                    if len(items) > 10 else "")
            sorted_note = ""
            if any(r.get("due") or r.get("pri") for r in items):
                sorted_note = " (todo.txt order: by due, priority)"
            problems = [r.get("todo_problem") for r in items
                        if r.get("todo_problem")]
            note = (f" Note: {problems[0]}." if problems else "")
            return {"message": f"Capture inbox ({len(items)})"
                               f"{sorted_note}: {body}{more}. File "
                               f"with e.g. 'file 1 as next action', "
                               f"'file 1 as project', 'file 1 as "
                               f"someday', 'file 1 as waiting', or "
                               f"'file 1 as trash'.{note}",
                    "executed": False, "counts": c}
        if kind == "file":
            res = store.file(op["n"], op["target"])
            if res["action"] == "dropped":
                return {"message": f"Dropped "
                                   f"'{res['row']['text'][:60]}' — "
                                   f"nothing was destroyed, the "
                                   f"item is gone from the queues.",
                        "executed": False, "dropped": True}
            label = res["action"].replace("_", " ")
            return {"message": f"Filed as {label}: "
                               f"'{res['row']['text'][:60]}'.",
                    "executed": False, "filed": res["action"]}
        if kind == "list_bucket":
            return {"message": _bucket_list_message(
                store, op["bucket"]), "executed": False}
        if kind == "next":
            items = store.list_by("next_action")
            if not items:
                return {"message": "No filed next action yet. "
                                   "Capture something and file it "
                                   "as a next action.",
                        "executed": False, "next": None}
            if any(r.get("due") or r.get("pri") for r in items):
                items = sorted(items, key=todo_sortkey)
                # the verbatim text already carries the tokens — no
                # duplicate annotation (observed live: "(A) … · (A)")
                head = items[0]["text"][:80]
                return {"message": f"Next action: '{head}' "
                                   f"(todo.txt order: by due, "
                                   f"priority).",
                        "executed": False, "next": items[0]}
            return {"message": f"Next action: "
                               f"'{items[0]['text'][:80]}' "
                               f"(oldest first).",
                    "executed": False, "next": items[0]}
    except CaptureValueError as e:
        return {"message": f"I won't do that: {e}",
                "executed": False, "refused": True}
    return {"message": "I couldn't process that capture ask.",
            "executed": False}


class CaptureFlow:
    def __init__(self, store: CaptureStore) -> None:
        self.store = store

    def wants_turn(self, clean: str) -> bool:
        c = (clean or "").strip()
        # a capture-ANCHORED utterance carries diary text — the
        # exclusion nouns are the captured content, not a route away
        # (observed live 2026-10-26: "capture water the plants" fell
        # to the generic parser because the exclusions hid the anchor)
        if _EXCLUDE_RE.search(c) and \
                re.match(r"^(?:capture|jot)\b", c) is None:
            return False
        return _ANCHOR_RE.match(c) is not None

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        return handle_capture_utterance(self.store, clean, who)
