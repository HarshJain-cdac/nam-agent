"""One SQLite file (state.db) holds everything NAM harness remembers and does.

This mirrors the Hermes approach on the whiteboard: SQLite + FTS5, no server.
Open it yourself anytime:  sqlite3 .nam/state.db '.tables'
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

SCHEMA = """
-- Flagship-task artifact: events the calendar tool creates. The deterministic
-- eval asserts directly on rows in this table ("did the meeting trigger?").
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    start TEXT NOT NULL,           -- ISO 8601
    "end" TEXT,
    attendees TEXT DEFAULT '',     -- comma-separated
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

-- Semantic memory: durable facts about you, your people, your projects.
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,         -- who/what the fact is about, e.g. 'alex'
    content TEXT NOT NULL,         -- the fact itself
    source TEXT DEFAULT 'user',    -- 'user' (told directly) or 'consolidation'
    created_at TEXT DEFAULT (datetime('now')),
    valid_from TEXT DEFAULT (datetime('now'))  -- when this version became current
);
-- Earlier versions of a fact: an update closes the old version here instead
-- of overwriting it, so "what was true before" still has an answer. Deleting
-- a fact (forgetting it) deletes its history too.
CREATE TABLE IF NOT EXISTS fact_history (
    id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL,
    subject TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    valid_from TEXT,
    valid_to TEXT DEFAULT (datetime('now')),
    reason TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    subject, content, content=facts, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, subject, content) VALUES (new.id, new.subject, new.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, content) VALUES ('delete', old.id, old.subject, old.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, content) VALUES ('delete', old.id, old.subject, old.content);
    INSERT INTO facts_fts(rowid, subject, content) VALUES (new.id, new.subject, new.content);
END;

-- Episodic memory: dated things that happened (past chats, distilled).
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    happened_at TEXT NOT NULL,     -- ISO 8601 date of the episode
    summary TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    summary, content=episodes, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, summary) VALUES (new.id, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary) VALUES ('delete', old.id, old.summary);
END;

-- Raw chat log ("save the messages" box). Consolidation reads from here.
-- session_id tags each row with which conversation it belongs to, so the
-- dashboard can offer "New chat" and switch between past sessions (like a
-- chat app). Everything shares this one table — sessions are just a label.
CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,            -- 'user' | 'assistant'
    content TEXT NOT NULL,
    consolidated INTEGER DEFAULT 0,
    session_id TEXT DEFAULT 'default',
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent column upgrades for databases created before a
    column existed. SQLite has no 'ADD COLUMN IF NOT EXISTS', so we check."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_log)").fetchall()}
    if "session_id" not in cols:
        conn.execute("ALTER TABLE chat_log ADD COLUMN session_id TEXT DEFAULT 'default'")
        conn.commit()
    if "source" not in cols:
        # where a message came from (api, or dashboard: the chat page)
        conn.execute("ALTER TABLE chat_log ADD COLUMN source TEXT DEFAULT 'cli'")
        conn.commit()
    if "meta" not in cols:
        # per-turn telemetry as JSON on the assistant row (gate decision,
        # latency, iterations, tools) — so reopening a thread still shows how
        # each answer was produced, not just the plain text.
        conn.execute("ALTER TABLE chat_log ADD COLUMN meta TEXT")
        conn.commit()
    fact_cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
    if fact_cols and "valid_from" not in fact_cols:
        # when the current version of a fact became true (fact_history keeps
        # the earlier ones). Left empty on existing rows: a fact with none
        # counts from created_at (the store reads COALESCE), and an UPDATE here
        # would rewrite every row through the full-text index's trigger.
        conn.execute("ALTER TABLE facts ADD COLUMN valid_from TEXT")
        conn.commit()


# Schema setups this process has already run, per database file. A schema
# script takes the write lock even when every table exists, and it ran on
# every connection (the dashboard opens one per /api/data poll) and on every
# NAM store call: with several browsers polling while a chat turn wrote, the
# polls failed with "database is locked". Keyed by the file's identity
# (device, inode), so a database deleted and created again is set up again.
_READY: set[tuple] = set()
_READY_LOCK = threading.Lock()

# How long a statement waits for another connection's write to finish before
# giving up. A chat turn or a NAM step can hold the write lock for a few
# seconds; a read that waits it out is better than a page that errors. (WAL
# would let reads run alongside a write, but .nam can sit on a network
# filesystem, where WAL's shared-memory file is not safe.)
BUSY_TIMEOUT_MS = 15000


def once(conn: sqlite3.Connection, name: str, setup: Callable[[sqlite3.Connection], None],
         table: str | None = None) -> None:
    """Run `setup(conn)` the first time this process sees this database file
    under `name`; an in-memory database is set up every time. `table`, one the
    setup creates, is checked with a plain read: a file deleted and created
    again can get the same inode number, and must still be set up."""
    row = conn.execute("PRAGMA database_list").fetchone()
    path = row[2] if row else ""
    try:
        st = os.stat(path) if path else None
    except OSError:
        st = None
    if st is None:
        setup(conn)
        return
    key = (st.st_dev, st.st_ino, name)
    if key in _READY and (table is None or _has(conn, table)):
        return
    _READY.discard(key)
    with _READY_LOCK:
        if key not in _READY:
            setup(conn)
            _READY.add(key)


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def _setup(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)


def connect(home: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    # check_same_thread=False lets the dashboard's threaded HTTP server reuse
    # one agent connection across worker threads (guarded by a lock).
    conn = sqlite3.connect(home / "state.db", check_same_thread=check_same_thread,
                           timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    once(conn, "core", _setup, table="chat_log")
    return conn
