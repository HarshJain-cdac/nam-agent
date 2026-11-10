"""The NAM audit log: one append-only row per event.

    nam_events   who (tenant, user), in which call (run_id, flow, case),
                 what (kind, name, effect), how it went (status, error code,
                 attempts, ms) and a small JSON detail

Rows are written for every NAM tool call (kind "tool", stage_tools.py) and
every model attempt ("model", nam_agent/nam/llm.py). Rows are never
updated or deleted: triggers refuse both, so a row, once written, is what
happened.

THE HASH CHAIN (plan Step 17, FRS F33 integrity)
  Each row carries `hash` = sha256(previous row's hash + the row's own
  fields), so editing or removing a row by going round the triggers breaks
  every hash after it. verify() recomputes the chain and names the first
  row that does not match. Rows written before the chain existed have an
  empty hash and are reported as unchained, not as broken.

The detail holds a bounded preview of the arguments, never a result body:
what a step produced is a version of its case (case.py).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from nam_agent.nam import case

_NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS nam_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL DEFAULT ({_NOW}),
    tenant TEXT NOT NULL,
    user TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    flow TEXT NOT NULL DEFAULT '',
    case_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    effect TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 1,
    ms INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS nam_events_by_run ON nam_events (run_id);
CREATE INDEX IF NOT EXISTS nam_events_by_case ON nam_events (case_id, id);
CREATE TRIGGER IF NOT EXISTS nam_events_no_update BEFORE UPDATE ON nam_events
BEGIN SELECT RAISE(ABORT, 'nam_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS nam_events_no_delete BEFORE DELETE ON nam_events
BEGIN SELECT RAISE(ABORT, 'nam_events is append-only'); END;
"""

DETAIL_CAP = 4000     # characters of detail_json kept per row


_KEYS = ("id", "at", "tenant", "user", "run_id", "flow", "case_id", "kind", "name",
         "effect", "status", "error_code", "attempts", "ms")


def ensure(conn: sqlite3.Connection) -> None:
    """Create or migrate these tables, once per database file per process
    (nam_agent.db.once): each call re-running the schema took the write lock."""
    from nam_agent import db

    db.once(conn, "nam.audit", _setup, table="nam_events")


def _setup(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(nam_events)")}
    for column in ("prev_hash", "hash"):
        if column not in have:     # a log from before the chain: kept as it is
            conn.execute(f"ALTER TABLE nam_events ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    conn.commit()


def _digest(prev: str, fields: tuple, detail: str) -> str:
    body = json.dumps([prev, *fields, detail], separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def record(conn: sqlite3.Connection, *, kind: str, name: str, status: str, effect: str = "",
           error_code: str = "", attempts: int = 1, ms: int = 0, case_id: str = "",
           detail: dict | None = None) -> None:
    """One row, as the acting caller (case.current()). Raises if it cannot be
    written: an action that cannot be audited does not count as done."""
    ensure(conn)
    actor = _actor()
    text = json.dumps(detail or {}, sort_keys=True, default=str)
    if len(text) > DETAIL_CAP:
        text = json.dumps({"truncated": True, "preview": text[:DETAIL_CAP - 100]})
    at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with case._write(conn):
        last = conn.execute("SELECT id, hash FROM nam_events ORDER BY id DESC LIMIT 1").fetchone()
        prev = last[1] if last else ""
        row_id = (last[0] if last else 0) + 1
        fields = (row_id, at, actor.tenant, actor.user, actor.run_id, actor.flow,
                  case_id or actor.case_id, kind, name, effect, status, error_code, attempts, ms)
        conn.execute(
            "INSERT INTO nam_events (id, at, tenant, user, run_id, flow, case_id, kind, name, "
            "effect, status, error_code, attempts, ms, detail_json, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*fields, text, prev, _digest(prev, fields, text)))


def verify(conn: sqlite3.Connection) -> dict:
    """Recompute the hash chain: {ok, rows, unchained, broken_at, says}."""
    ensure(conn)
    rows = conn.execute(f"SELECT {', '.join(_KEYS)}, detail_json, prev_hash, hash "
                        "FROM nam_events ORDER BY id").fetchall()
    prev, unchained, started = "", 0, False
    for r in rows:
        fields, detail, prev_hash, digest = tuple(r[:len(_KEYS)]), r[-3], r[-2], r[-1]
        if not digest and not started:
            unchained += 1
            continue
        if not started:
            started, prev = True, prev_hash      # the first chained row follows unchained ones
        if prev_hash != prev or _digest(prev, fields, detail) != digest:
            return {"ok": False, "rows": len(rows), "unchained": unchained,
                    "broken_at": fields[0],
                    "says": f"audit row {fields[0]} does not match its hash: the log was "
                            "changed after it was written"}
        prev = digest
    return {"ok": True, "rows": len(rows), "unchained": unchained, "broken_at": None,
            "says": f"{len(rows) - unchained} chained row(s) verified"}


def events(conn: sqlite3.Connection, *, run_id: str = "", case_id: str = "",
           kind: str = "") -> list[dict]:
    """Rows in the order they were written, filtered by run, case or kind."""
    ensure(conn)
    where, args = [], []
    for column, value in (("run_id", run_id), ("case_id", case_id), ("kind", kind)):
        if value:
            where.append(f"{column} = ?")
            args.append(value)
    sql = f"SELECT {', '.join(_KEYS)}, detail_json FROM nam_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    return [{**dict(zip(_KEYS, r[:-1], strict=True)), "detail": json.loads(r[-1])}
            for r in rows]


def _actor():
    """Who acts: the PR Creation Flow's actor for a chat turn (case.acting),
    else the harness API's (store.acting), else this machine's officer."""
    from nam_agent.nam import store

    return case._ACTOR.get() or store.current()
