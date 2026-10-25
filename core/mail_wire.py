"""Read-only mail wire (P6-b29, 2026-10-24) — the user-approved email
add-in, DORMANT until it is configured.

Design interface taken from the repo hunt (never their code):
- elie222/inbox-zero + Dan7ares/email-digest-triage-bot: rules-first
  classification, one grouped digest back to the user (stdlib only —
  imaplib + email, no third-party dependency);
- private-triage-agent + jakejars' workflow template: the wire is
  READ-ONLY forever in this build — folders open with readonly=True
  and every body fetch is BODY.PEEK, so the mailbox's \\Seen state is
  never touched and nothing in the account can change;
- user decision 2026-10-24: mail is added but NOT used for now, and
  the assistant NEVER drafts a mail unless the user first asks it to
  draft — and even then drafting stays DISABLED in this build (the
  ask is answered honestly; no editor, no queued send).

Startup state: ZARA_IMAP_HOST/USER/PASSWORD unset (or a host that
isn't reachable) → a check answers honestly that the mail wire is
off. The lab has blocked TLS egress, so the default lab deployment is
exactly that state. Passwords never print, never land in audit or
messages (redaction is defense in depth: bodies are fetched as
HEADERS ONLY in this build — no body text is surfaced).
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import ssl
from typing import Optional

_LIMIT = 20
_RULES = (
    # (bucket, subject/sender regexes) — rules-FIRST classification,
    # deterministic, cheapest wins; generic otherwise
    ("newsletter", re.compile(
        r"newsletter|unsubscribe|digest|promo|deals?\b|weekly "
        r"roundup|list-manage", re.I)),
    ("meeting", re.compile(
        r"invitation|invite\b|meeting|calendar|ics attachment|"
        r"videoconference|webex|zoom\.|teams\b", re.I)),
    ("finance", re.compile(
        r"invoice|payment|receipt|transaction|statement|otp\b|"
        r"bank\b|credit card|payment confirmation", re.I)),
    ("travel", re.compile(
        r"boarding pass|flight|itinerary|train ticket|hotel "
        r"booking|trip\b", re.I)),
    ("security", re.compile(
        r"security alert|sign[- ]?in|login attempt|new device|"
        r"password (?:reset|change)|suspicious", re.I)),
)


def classify(subject: str, sender: str) -> str:
    for name, rx in _RULES:
        if rx.search(subject or "") or rx.search(sender or ""):
            return name
    return "other"


class MailWire:
    """Read-only IMAP reader. All network work sits behind `_connect`
    (the seam tests use; production uses the real IMAP4_SSL)."""

    def __init__(self, audit=None, env: Optional[dict] = None) -> None:
        self.audit = audit
        self._env = dict(os.environ if env is None else env)
        self._captured: dict = {}

    def _log(self, action: str, detail: str = "") -> None:
        try:
            if self.audit is not None:
                self.audit.record("mail", action, "", detail[:200])
        except Exception:  # noqa: BLE001 — audit never breaks events
            pass

    def _vals(self) -> dict:
        env = self._env
        host = (env.get("ZARA_IMAP_HOST") or "").strip()
        user = (env.get("ZARA_IMAP_USER") or "").strip()
        pw = (env.get("ZARA_IMAP_PASSWORD") or "").strip()
        try:
            port = int(env.get("ZARA_IMAP_PORT") or 993)
        except ValueError:
            port = 993
        return {"host": host, "port": port, "user": user, "pw": pw}

    def configured(self) -> bool:
        v = self._vals()
        return bool(v["host"] and v["user"] and v["pw"])

    def _connect(self, vals: dict):
        """Real wire: IMAP4_SSL. Never marks \\Seen anywhere."""
        ctx = ssl.create_default_context()
        return imaplib.IMAP4_SSL(vals["host"], vals["port"],
                                 ssl_context=ctx)

    def check_unseen(self, limit: int = _LIMIT) -> dict:
        """UNSEEN header digest. Read-only end-to-end: readonly
        select + BODY.PEEK, and only headers are parsed for the
        digest (note: with this build's header-only fetch no body
        content is copied anywhere)."""
        v = self._vals()
        if not self.configured():
            return {"ok": False, "reason": "not_configured",
                    "unseen": 0, "buckets": {}, "items": []}
        self._log("mail_check_started", f"host={v['host']}")
        try:
            M = self._connect(v)
            M.login(v["user"], v["pw"])
            typ, _ = M.select("INBOX", readonly=True)
            if typ != "OK":
                raise RuntimeError("INBOX select failed")
            typ, data = M.search(None, "UNSEEN")
            if typ != "OK":
                raise RuntimeError("UNSEEN search failed")
            ids = (data[0] or b"").split()
            digest = {"ok": True, "unseen": len(ids), "buckets": {},
                      "items": [], "readonly": True}
            for num in ids[:limit]:
                typ, d = M.fetch(num, "(BODY.PEEK[HEADER])")
                if typ != "OK" or not d or not d[0]:
                    continue
                raw = d[0][1] if isinstance(d[0], tuple) else b""
                msg = email.message_from_bytes(raw)
                sender = msg.get("From", "") or ""
                subject = msg.get("Subject", "") or ""
                bucket = classify(subject, sender)
                digest["buckets"][bucket] = \
                    digest["buckets"].get(bucket, 0) + 1
                if len(digest["items"]) < 10:
                    digest["items"].append(
                        {"bucket": bucket, "from": sender[:60],
                         "subject": re.sub(r"\s+", " ",
                                           subject)[:80]})
            try:
                M.logout()
            except Exception:  # noqa: BLE001
                pass
            self._log("mail_check_done",
                      f"unseen={digest['unseen']}")
            return digest
        except Exception as e:  # noqa: BLE001 — honest failure, no
            msg = str(e)                        # secrets, no retry
            self._log("mail_check_failed", msg[:200])
            return {"ok": False, "reason": "connection",
                    "detail": msg[:160], "unseen": 0, "buckets": {},
                    "items": []}


_MAIL_ANCHOR_RE = re.compile(
    r"^(?:check|read|scan|show)(?: my| the)? (?:e-?mail|mail|inbox)"
    r"(?: inbox| mail)?(?: for (?:a )?digest|count)?\s*[\s.,!?]*$|"
    r"^how (?:many )?(?:unseen|unread) e-?mails?", re.I)
_MAIL_EXCLUDE_RE = re.compile(
    r"\b(remind|reminder|routine|calendar|capture|grocer)\b|"
    r"\bmy inbox\b", re.I)


def _digest_message(d: dict) -> str:
    if d["ok"]:
        n = d["unseen"]
        if n == 0:
            return ("Inbox: no unseen mail — nothing fetched beyond "
                    "the headers needed to notice that (read-only).")
        parts = ", ".join(f"{k} {v}"
                          for k, v in sorted(d["buckets"].items()))
        lines = "; ".join(f"[{i['bucket']}] {i['from'][:40]} — "
                          f"{i['subject'][:50]}"
                          for i in d["items"][:6])
        more = (f" (+{n - len(d['items'])} more)" if n > len(d.get(
            "items") or []) else "")
        return (f"Inbox: {n} unseen — {parts}. {lines}{more}. "
                f"(read-only wire; nothing was opened, marked, or "
                f"sent)")
    if d["reason"] == "not_configured":
        return ("Mail: the read-only wire is off — set ZARA_IMAP_"
                "HOST, ZARA_IMAP_USER and ZARA_IMAP_PASSWORD (an app "
                "password) on the laptop to turn it on. Nothing was "
                "queried.")
    return (f"Mail: connection failed ({d.get('detail', '')[:100]}). "
            f"Check the host or app password; nothing changed in "
            f"your mailbox.")


class MailFlow:
    """Turn surface: 'check my email' → digest. Drafting asks are
    answered honestly: DISABLED by user decision."""

    _DRAFT_RE = re.compile(
        r"\b(?:draft|write|compose|reply to)\b.*\b(?:e-?mail|mail|"
        r"inbox|it|them)\b|\b(?:e-?mail|mail)\b.*\b(?:draft|"
        r"reply)\b", re.I)

    def __init__(self, wire: MailWire) -> None:
        self.wire = wire

    def wants_turn(self, clean: str) -> bool:
        c = (clean or "").strip()
        if _MAIL_ANCHOR_RE.match(c) and \
                not _MAIL_EXCLUDE_RE.search(c):
            return True
        return bool(self._DRAFT_RE.search(c)) and \
            not _MAIL_EXCLUDE_RE.search(c)

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        c = (clean or "").strip()
        if self._DRAFT_RE.search(c):
            return {"message":
                    "Drafting mail is disabled in this build by your "
                    "own rule (never draft unless you first ask). "
                    "The read-only wire can still report what's "
                    "unseen; say 'check my email' for the digest.",
                    "executed": False, "draft": "disabled"}
        d = self.wire.check_unseen()
        return {"message": _digest_message(d), "executed": False,
                "mail": d if d["ok"] else d["reason"]}
