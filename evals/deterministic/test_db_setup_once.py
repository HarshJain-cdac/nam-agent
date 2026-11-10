"""DETERMINISTIC EVAL — schema setup runs once per database, not per connection.

The dashboard opens a connection on every /api/data poll, and every NAM store
call ran its schema script. A schema script takes the write lock even when the
tables exist, so with several browsers polling while a chat turn wrote, the
polls failed with "sqlite3.OperationalError: database is locked".
"""

from __future__ import annotations

import sqlite3

from nam_agent import db


def test_connect_sets_up_a_database_once(tmp_path, monkeypatch):
    calls = []
    real = db._setup
    monkeypatch.setattr(db, "_setup", lambda conn: (calls.append(1), real(conn)))
    for _ in range(5):
        db.connect(tmp_path).close()
    assert calls == [1]
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT count(*) FROM chat_log").fetchone()[0] == 0


def test_a_deleted_database_is_set_up_again(tmp_path):
    db.connect(tmp_path).close()
    (tmp_path / "state.db").unlink()
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT count(*) FROM chat_log").fetchone()[0] == 0


def test_in_memory_databases_are_always_set_up():
    calls = []
    for _ in range(2):
        db.once(sqlite3.connect(":memory:"), "t", lambda c: calls.append(1))
    assert calls == [1, 1]


def test_reads_wait_for_a_write_instead_of_failing(tmp_path):
    assert db.BUSY_TIMEOUT_MS >= 10000
    conn = db.connect(tmp_path)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS


def test_nam_store_setup_runs_once(tmp_path, monkeypatch):
    from nam_agent.nam import store

    calls = []
    real = store._setup
    monkeypatch.setattr(store, "_setup", lambda conn: (calls.append(1), real(conn)))
    conn = db.connect(tmp_path)
    for _ in range(4):
        store.ensure(conn)
    assert calls == [1]
