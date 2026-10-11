"""SessionStore history: optional SQLite persistence, redaction, helpers."""
import json
import sqlite3

import pytest

from core.sessions import COMPACT_KEEP, MAX_MESSAGES, SessionStore


def db_rows(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT id, data FROM sessions").fetchall()
    finally:
        con.close()


def test_write_reopen_messages_intact(tmp_path):
    db = str(tmp_path / "hist.db")
    s1 = SessionStore(db_path=db)
    sess = s1.create()
    s1.append(sess.id, "user", "hello world")
    s1.append(sess.id, "assistant", "hi there")
    sid = sess.id

    s2 = SessionStore(db_path=db)  # simulated restart
    got = s2.get(sid)
    assert [(m.role, m.text) for m in got.messages] == [
        ("user", "hello world"), ("assistant", "hi there")]


def test_restart_drops_nothing(tmp_path):
    db = str(tmp_path / "hist.db")
    s1 = SessionStore(db_path=db)
    sess = s1.create(user="u1")
    for i in range(10):
        s1.append(sess.id, "user" if i % 2 == 0 else "assistant",
                  f"msg-{i}")
    before = [(m.role, m.text) for m in s1.get(sess.id).messages]
    s2 = SessionStore(db_path=db)
    after = [(m.role, m.text) for m in s2.get(sess.id).messages]
    assert after == before and len(after) == 10


def test_memory_mode_unchanged(tmp_path):
    store = SessionStore()
    s = store.create()
    store.append(s.id, "user", "m0")
    fresh = SessionStore()  # no db: nothing carries over
    with pytest.raises(KeyError):
        fresh.get(s.id)
    # in-memory compact + expire semantics preserved
    for i in range(65):
        store.append(s.id, "user", f"m{i}")
    got = store.get(s.id)
    assert got.compacted > 0 and len(got.messages) <= MAX_MESSAGES
    assert store.expire_idle("2999-01-01T00:00:00+00:00") >= 1


def test_secrets_redacted_in_stored_text(tmp_path):
    db = str(tmp_path / "hist.db")
    store = SessionStore(db_path=db)
    sess = store.create()
    raw = "please use api-key: hunter-secret-999 and Bearer tok-abc-123"
    store.append(sess.id, "user", raw)
    kept = store.get(sess.id).messages[-1].text
    assert "hunter-secret-999" not in kept and "tok-abc-123" not in kept
    assert "[REDACTED]" in kept
    for _, data in db_rows(db):  # raw tokens never hit disk either
        assert "hunter-secret-999" not in data
        assert "tok-abc-123" not in data


def test_compaction_respected_in_db(tmp_path):
    db = str(tmp_path / "hist.db")
    store = SessionStore(db_path=db)
    sess = store.create()
    for i in range(65):
        store.append(sess.id, "user", f"m{i}")
    rows = db_rows(db)
    assert len(rows) == 1
    stored = json.loads(rows[0][1])
    assert len(stored["messages"]) <= MAX_MESSAGES
    assert stored["messages"][0]["text"].startswith("[compacted ")
    assert len(stored["messages"][-COMPACT_KEEP:]) == COMPACT_KEEP


def test_expire_idle_deletes_db_rows(tmp_path):
    db = str(tmp_path / "hist.db")
    store = SessionStore(db_path=db)
    sess = store.create()
    store.append(sess.id, "user", "old news")
    assert len(db_rows(db)) == 1
    assert store.expire_idle("2999-01-01T00:00:00+00:00") >= 1
    assert db_rows(db) == []
    with pytest.raises(KeyError):
        SessionStore(db_path=db).get(sess.id)


def test_list_recent_bounded_and_redacted(tmp_path):
    db = str(tmp_path / "hist.db")
    store = SessionStore(db_path=db)
    ids = []
    for i in range(3):
        sess = store.create()
        ids.append(sess.id)
        store.append(sess.id, "user", f"note-{i}")
    store.append(ids[0], "user",
                 "api-key: list-secret-456 " + "x" * 200)
    recent = store.list_recent(limit=2)
    assert len(recent) == 2
    assert set(recent[0]) == {"id", "updated_at", "message_count",
                              "last_text"}
    assert all(len(r["last_text"]) <= 120 for r in recent)
    blob = json.dumps(recent)
    assert "list-secret-456" not in blob
    mem_only = SessionStore()  # list_recent works without a DB too
    ms = mem_only.create()
    mem_only.append(ms.id, "user", "plain")
    assert mem_only.list_recent(limit=5)[0]["id"] == ms.id


def test_delete_removes_and_second_delete_false(tmp_path):
    db = str(tmp_path / "hist.db")
    store = SessionStore(db_path=db)
    sess = store.create()
    store.append(sess.id, "user", "bye")
    assert store.delete(sess.id) is True
    assert store.delete(sess.id) is False
    assert db_rows(db) == []
    with pytest.raises(KeyError):
        store.get(sess.id)
    assert SessionStore().delete("ses-missing") is False


def test_pending_reply_only_when_last_is_user(tmp_path):
    db = str(tmp_path / "hist.db")
    store = SessionStore(db_path=db)
    assert store.pending_reply("ses-missing") is False
    empty = store.create()
    assert store.pending_reply(empty.id) is False
    assert store.turn_status(empty.id) == "unknown"
    assert store.turn_status("ses-missing") == "unknown"
    store.append(empty.id, "user", "question?")
    assert store.pending_reply(empty.id) is True
    assert store.turn_status(empty.id) == "active"
    store.append(empty.id, "assistant", "answer.")
    assert store.pending_reply(empty.id) is False
    assert store.turn_status(empty.id) == "completed"
    # interrupted turn survives restart as pending, never auto-completed
    store2 = SessionStore(db_path=db)
    assert store2.pending_reply(empty.id) is False
    store2.append(empty.id, "user", "follow-up?")
    assert SessionStore(db_path=db).pending_reply(empty.id) is True


def test_corrupt_db_row_skipped_on_load(tmp_path):
    db = str(tmp_path / "hist.db")
    store = SessionStore(db_path=db)
    sess = store.create()
    store.append(sess.id, "user", "keep me")
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS sessions "
                    "(id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        con.execute("INSERT OR REPLACE INTO sessions (id, data) "
                    "VALUES (?, ?)", ("ses-corrupt", "{not valid json"))
        con.execute("INSERT OR REPLACE INTO sessions (id, data) "
                    "VALUES (?, ?)", ("ses-wrong-shape",
                                      json.dumps({"bogus": 1})))
        con.commit()
    finally:
        con.close()
    reopened = SessionStore(db_path=db)  # must not raise
    assert reopened.get(sess.id).messages[-1].text == "keep me"
    with pytest.raises(KeyError):
        reopened.get("ses-corrupt")
