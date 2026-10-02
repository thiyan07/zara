"""SQLite persistence. Schema is portable: same tables map 1:1 to PostgreSQL
(+ pgvector column for memory.embedding) on Oracle. See docs for PG DDL."""
from __future__ import annotations
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
  id TEXT PRIMARY KEY, category TEXT NOT NULL, text TEXT NOT NULL,
  source TEXT DEFAULT '', confidence REAL DEFAULT 0.5, importance REAL DEFAULT 0.5,
  embedding TEXT DEFAULT '', metadata TEXT DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT
);
CREATE TABLE IF NOT EXISTS missions (
  id TEXT PRIMARY KEY, goal TEXT NOT NULL, state TEXT NOT NULL,
  device_id TEXT, steps TEXT DEFAULT '[]', checkpoints TEXT DEFAULT '[]',
  approvals TEXT DEFAULT '[]', retries INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3,
  error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY, kind TEXT NOT NULL, capabilities TEXT DEFAULT '[]',
  online INTEGER DEFAULT 1, battery_pct REAL, charging INTEGER DEFAULT 0,
  network TEXT DEFAULT 'unknown', last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT, action TEXT,
  target TEXT, detail TEXT, ok INTEGER
);
"""

# Postgres target (Oracle Always Free) — memory.embedding becomes vector(N):
#   CREATE TABLE memory (... embedding vector(384), ...);
#   CREATE INDEX ON memory USING ivfflat (embedding vector_cosine_ops);
# All other tables copy verbatim with TEXT->TEXT, REAL->DOUBLE PRECISION.

class Database:
    def __init__(self, path: str = "assistant.db") -> None:
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple = ()):
        with self._lock, self._db:
            return self._db.execute(sql, params).fetchall()
