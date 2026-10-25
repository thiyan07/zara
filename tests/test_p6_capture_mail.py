"""Capture routing + read-only mail wire (P6-b29) — parsing, filing,
honest degradation, and the mail wire's read-only guarantees with a
fake IMAP seam. No real network is touched."""
from core.capture import (BUCKETS, CaptureStore, parse_capture_intent,
                          handle_capture_utterance, CaptureFlow)
from core.mail_wire import MailWire, MailFlow, classify


class ListAudit:
    def __init__(self):
        self.entries = []

    def record(self, source, action, target="", detail=""):
        self.entries.append((source, action, target, detail))


# ---------------- capture gate ----------------

def test_capture_gate_and_exclusions():
    f = CaptureFlow(CaptureStore())
    assert f.wants_turn("capture call the dentist tomorrow")
    assert f.wants_turn("show my inbox")
    assert f.wants_turn("file 1 as next action")
    assert f.wants_turn("my next actions")
    assert f.wants_turn("what's my next action")
    assert not f.wants_turn("remind me to capture the moment")
    assert not f.wants_turn("show my capture inbox of water")
    assert not f.wants_turn("show my reminders")
    # batch-32 live fix: the anchored form carries diary text — the
    # exclusion nouns are the CONTENT, not a route away
    assert f.wants_turn("capture water the plants")
    assert f.wants_turn("jot: medication note for mom")
    assert handle_capture_utterance(CaptureStore(),
                                    "capture water the plants") \
        ["message"].startswith("Captured")


def test_parse_shapes():
    op = parse_capture_intent("capture call the dentist tomorrow")
    assert op["op"] == "capture" and "dentist" in op["text"]
    op = parse_capture_intent("show my inbox")
    assert op["op"] == "inbox"
    op = parse_capture_intent("file 2 as project")
    assert op["op"] == "file" and op["n"] == 2 \
        and op["target"] == "project"
    op = parse_capture_intent("file 3 as waiting for")
    assert op["target"] == "waiting"
    op = parse_capture_intent("file 1 as trash")
    assert op["target"] == "__trash__"
    op = parse_capture_intent("show my someday")
    assert op["op"] == "list_bucket" and op["bucket"] == "someday"


# ---------------- capture lifecycle ----------------

def test_capture_brain_dump_splits():
    store = CaptureStore()
    rows = handle_capture_utterance(
        store, "capture call the dentist; buy cat food "
               "and book flight")["message"]
    assert rows is not None
    items = store.list_by("unsorted")
    assert len(items) == 3
    assert any("cat food" in r["text"] for r in items)


def test_file_and_buckets():
    store = CaptureStore()
    handle_capture_utterance(store, "capture pay the power bill")
    handle_capture_utterance(store, "capture fix the fence")
    res = handle_capture_utterance(store, "file 1 as next action")
    assert "Filed as next action" in res["message"]
    res = handle_capture_utterance(store, "file 1 as someday")
    assert "Filed as someday" in res["message"]
    b = handle_capture_utterance(store, "my next actions")
    assert "power bill" in b["message"]
    assert store.counts()["unsorted"] == 0
    assert store.counts()["next_action"] == 1


def test_drop_is_named_not_erased_silently():
    store = CaptureStore()
    handle_capture_utterance(store, "capture nonsense idea")
    res = handle_capture_utterance(store, "file 1 as trash")
    assert "Dropped" in res["message"]
    assert store.list_by("unsorted") == []


def test_next_action_oldest_first():
    store = CaptureStore()
    handle_capture_utterance(store, "capture first thing")
    handle_capture_utterance(store, "capture second thing")
    handle_capture_utterance(store, "file 1 as next action")
    handle_capture_utterance(store, "file 1 as next action")
    r = handle_capture_utterance(store, "what's my next action")
    assert "'first thing'" in r["message"]


def test_file_out_of_range_refused():
    store = CaptureStore()
    r = handle_capture_utterance(store, "file 5 as project")
    assert r["refused"] is True


def test_persistence(tmp_path):
    db = str(tmp_path / "cap.db")
    store = CaptureStore(db_path=db)
    handle_capture_utterance(store, "capture water the plant")
    store2 = CaptureStore(db_path=db)
    assert len(store2.list_by("unsorted")) == 1


# ---------------- mail wire ----------------

def test_classify_rules_first():
    assert classify("Weekly digest: top stories", "a@b.com") == \
        "newsletter"
    assert classify("Re: question", "zoom.us <no-reply@zoom.us>") \
        in ("meeting", "other")
    assert classify("Payment receipt #42", "billing@shop.io") == \
        "finance"
    assert classify("Security alert: new sign-in",
                    "no-reply@x.com") == "security"
    assert classify("", "") == "other"


def test_mail_wire_dormant_without_config():
    wire = MailWire(env={})
    assert wire.configured() is False
    d = wire.check_unseen()
    assert d["ok"] is False and d["reason"] == "not_configured"
    msg = MailFlow(wire).turn("check my email")["message"]
    assert "off" in msg and "Nothing was queried" in msg


class _FakeIMAP:
    """Drives the seam end-to-end read-only: readonly select +
    BODY.PEEK fetch of two unseen notes."""

    def __init__(self, host, port, ssl_context=None):
        self.host = host
        self.calls = []

    def login(self, u, p):
        self.calls.append(("login", u, p))

    def select(self, mailbox, readonly=False):
        assert readonly is True, "wire MUST select readonly"
        self.calls.append(("select", mailbox, readonly))
        return ("OK", [b"1"])

    def search(self, charset, *crit):
        assert "".join(crit) == "UNSEEN"
        return ("OK", [b"1 2"])

    def fetch(self, num, spec):
        assert "PEEK" in spec, spec
        raw = (b"From: Zoom <no-reply@zoom.us>\r\n"
               b"Subject: Meeting invitation: demo\r\n\r\nbody"
               if num == b"1" else
               b"From: weekly@news.io\r\n"
               b"Subject: newsletter digest\r\n\r\nbody")
        return ("OK", [(b"1", raw)])

    def logout(self):
        self.calls.append(("logout",))


def test_mail_wire_readonly_end_to_end():
    wire = MailWire(env={"ZARA_IMAP_HOST": "m.example",
                         "ZARA_IMAP_PORT": "993",
                         "ZARA_IMAP_USER": "me@example",
                         "ZARA_IMAP_PASSWORD": "app-pass-ok"})
    assert wire.configured() is True
    captured = {}

    fake = [None]

    def connect(_vals):
        captured.update(_vals)
        return _FakeIMAP(_vals["host"], _vals["port"])

    wire._connect = connect
    d = wire.check_unseen()
    assert d["ok"] is True and d["unseen"] == 2
    assert d["buckets"].get("meeting") == 1
    assert d["buckets"].get("newsletter") == 1
    assert captured["pw"] == "app-pass-ok"
    audit_dump = " ".join(
        (e[1] + " " + (e[2] or "")) for e in MailAuditLog(wire.  audit)
    ) if wire.audit else ""
    msg = MailFlow(wire).turn("check my email")["message"]
    assert "read-only" in msg and "never" in msg or "read-only" in msg
    # password never lands in the message even on failure shapes
    assert "app-pass-ok" not in msg


def MailAuditLog(a):
    return getattr(a, "entries", []) if a is not None else []


def test_draft_ask_refuses_and_teaches():
    wire = MailWire(env={})
    r = MailFlow(wire).turn("draft a reply to the last email")
    assert "disabled" in r["message"] and r["executed"] is False
    r2 = MailFlow(wire).turn("write an email to the manager")
    assert "disabled" in r2["message"]


def test_mail_gate_exclusions():
    f = MailFlow(MailWire(env={}))
    assert f.wants_turn("check my email")
    assert f.wants_turn("how many unread emails")
    assert not f.wants_turn("show my inbox")
    assert not f.wants_turn("remind me to check email at 5")
    assert not f.wants_turn("check my calendar")


# ---------------- todo.txt subset (batch-32): due + priority tokens
# (design from github.com/todotxt — text stays verbatim, values in a
# sidecar, invalid dates never filled) ----------------

def test_todo_tokens_extract_and_validate():
    from core.capture import extract_todo_tokens
    due, pri, problem = extract_todo_tokens("(A) call mom due:2026-11-02")
    assert due == "2026-11-02" and pri == "A" and problem == ""
    due, pri, problem = extract_todo_tokens("call mom due:2026-99-99")
    assert due is None and "not a real date" in problem
    due, pri, problem = extract_todo_tokens("plain note, nothing here")
    assert due is None and pri is None and problem == ""
    due, pri, problem = extract_todo_tokens("(a) ship the draft pri:b")
    assert pri == "A"          # leading form wins (spec Rule 1)


def test_capture_tokens_stored_verbatim_with_meta():
    store = CaptureStore()
    out = handle_capture_utterance(
        store, "capture (A) call mom due:2026-11-02")
    assert out is not None
    items = store.list_by("unsorted")
    assert items[0]["text"] == "(A) call mom due:2026-11-02"  # verbatim
    assert items[0]["due"] == "2026-11-02" and items[0]["pri"] == "A"


def test_inbox_sorts_by_due_then_priority():
    store = CaptureStore()
    handle_capture_utterance(store, "capture write annual review "
                                      "due:2026-12-01")
    handle_capture_utterance(store, "capture (B) buy milk")
    handle_capture_utterance(store, "capture (A) call mom "
                                      "due:2026-11-02")
    out = handle_capture_utterance(store, "show my inbox")
    assert "todo.txt order" in out["message"]
    body = out["message"]
    first = body.split(": ", 1)[1][:60]
    assert "1. (A) call mom" in first     # due first, (A) first among none
    # file numbering follows the SORTED view the user saw:
    r = handle_capture_utterance(store, "file 1 as next action")
    assert "call mom" in r["message"]


def test_file_numbering_matches_sorted_inbox_after_meta_case():
    store = CaptureStore()
    handle_capture_utterance(store, "capture (C) someday-tune x")
    handle_capture_utterance(store, "capture pay rent due:2026-11-01")
    handle_capture_utterance(store, "capture (A) meds pickup")
    out = handle_capture_utterance(store, "show my inbox")
    assert "1. pay rent" in out["message"]          # due beats pri
    r = handle_capture_utterance(store, "file 2 as next action")
    assert "meds pickup" in r["message"]            # number 2 = (A)


def test_invalid_due_creates_no_fake_date():
    store = CaptureStore()
    out = handle_capture_utterance(store, "capture call mom "
                                            "due:2026-99-99")
    items = store.list_by("unsorted")
    assert items[0]["due"] is None
    assert "not a real date" in out["message"]


def test_next_ask_prefers_due_priority_only_with_meta():
    store = CaptureStore()
    handle_capture_utterance(store, "capture old oldest item")
    assert "No filed next action" in handle_capture_utterance(
        store, "what's my next action")["message"]
    handle_capture_utterance(store, "file 1 as next action")
    out2 = handle_capture_utterance(store, "what's my next action")
    assert "oldest first" in out2["message"]     # no meta → unchanged
    # a capture WITH todo.txt metadata joins the same bucket
    _ = handle_capture_utterance(
        store, "capture (A) quiet draft due:2026-11-02")
    row = store.unsorted_view()[0]
    assert row["due"] == "2026-11-02" and row["pri"] == "A"
    store._set_bucket(row, "next_action")
    out3 = handle_capture_utterance(store, "what's my next action")
    assert "todo.txt order" in out3["message"]
    assert "2026-11-02" in out3["message"]


def test_capture_todo_meta_persists(tmp_path):
    db = str(tmp_path / "zara_capture.db")
    s1 = CaptureStore(db_path=db)
    s1.capture("(A) call mom due:2026-11-02")
    s2 = CaptureStore(db_path=db)
    row = s2.list_by("unsorted")[0]
    assert row["due"] == "2026-11-02" and row["pri"] == "A"
